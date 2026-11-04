import io
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

# ReportLab para geração das etiquetas em PDF
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# --------------------------------------------------
# CONFIGURAÇÃO
# --------------------------------------------------

PASTA = Path(__file__).resolve().parent
BANCO = PASTA / "loja.db"

st.set_page_config(
    page_title="Controle SoniaVT",
    page_icon="📦",
    layout="wide"
)


# --------------------------------------------------
# BANCO DE DADOS
# --------------------------------------------------

def conectar():
    conexao = sqlite3.connect(BANCO, timeout=10)
    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA foreign_keys = ON")
    return conexao


def inicializar_banco():
    with conectar() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS clientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                telefone TEXT DEFAULT '',
                observacoes TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS estoque (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                produto TEXT NOT NULL UNIQUE,
                quantidade REAL NOT NULL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS pedidos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente_id INTEGER NOT NULL,
                data_pedido TEXT NOT NULL,
                data_entrega TEXT NOT NULL,
                tipo_entrega TEXT NOT NULL DEFAULT 'Entrega',
                local_entrega TEXT NOT NULL,
                observacoes TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Pendente',
                data_conclusao TEXT,
                FOREIGN KEY (cliente_id) REFERENCES clientes(id)
            );

            CREATE TABLE IF NOT EXISTS itens_pedido (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id INTEGER NOT NULL,
                produto TEXT NOT NULL,
                quantidade REAL NOT NULL,
                unidade TEXT NOT NULL DEFAULT 'un',
                separado INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (pedido_id)
                    REFERENCES pedidos(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS historico_etiquetas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_geracao TEXT NOT NULL,
                data_filtro_entrega TEXT NOT NULL,
                total_pedidos INTEGER NOT NULL,
                detalhes_pedidos TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS retiradas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome_cliente TEXT NOT NULL,
                itens TEXT NOT NULL,
                observacoes TEXT DEFAULT '',
                data_registro TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pendente',
                data_conclusao TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_pedidos_data
            ON pedidos(data_entrega);

            CREATE INDEX IF NOT EXISTS idx_itens_pedido
            ON itens_pedido(pedido_id);
        """)


inicializar_banco()


# --------------------------------------------------
# FUNÇÕES AUXILIARES DE ESTOQUE, CLIENTES E PEDIDOS
# --------------------------------------------------

def proximo_sabado():
    hoje = date.today()
    dias = (5 - hoje.weekday()) % 7
    return hoje + timedelta(days=dias)


def listar_clientes():
    with conectar() as con:
        return con.execute("SELECT * FROM clientes ORDER BY nome").fetchall()


def cliente_tem_pedidos(cliente_id):
    with conectar() as con:
        resultado = con.execute(
            "SELECT COUNT(*) AS total FROM pedidos WHERE cliente_id = ?",
            (cliente_id,)
        ).fetchone()
        return resultado["total"] > 0


def listar_estoque():
    with conectar() as con:
        return con.execute("SELECT * FROM estoque ORDER BY produto").fetchall()


def cadastrar_item_estoque(produto, quantidade):
    with conectar() as con:
        con.execute("""
            INSERT INTO estoque (produto, quantidade)
            VALUES (?, ?)
        """, (produto.strip(), float(quantidade)))


def atualizar_item_estoque(item_id, produto, quantidade):
    with conectar() as con:
        con.execute("""
            UPDATE estoque
            SET produto = ?, quantidade = ?
            WHERE id = ?
        """, (produto.strip(), float(quantidade), item_id))


def excluir_item_estoque(item_id):
    with conectar() as con:
        con.execute("DELETE FROM estoque WHERE id = ?", (item_id,))


def baixar_estoque(produto, quantidade):
    with conectar() as con:
        con.execute("""
            UPDATE estoque
            SET quantidade = quantidade - ?
            WHERE produto = ?
        """, (float(quantidade), produto))


def listar_pedidos(data_filtro=None, status=None):
    sql = """
        SELECT
            p.*,
            c.nome AS cliente,
            c.telefone AS telefone,
            (
                SELECT COUNT(*)
                FROM itens_pedido i
                WHERE i.pedido_id = p.id
            ) AS total_itens,
            (
                SELECT COUNT(*)
                FROM itens_pedido i
                WHERE i.pedido_id = p.id AND i.separado = 1
            ) AS itens_separados
        FROM pedidos p
        JOIN clientes c ON c.id = p.cliente_id
        WHERE 1 = 1
    """
    parametros = []

    if data_filtro:
        sql += " AND p.data_entrega = ?"
        parametros.append(str(data_filtro))

    if status:
        sql += " AND p.status = ?"
        parametros.append(status)

    sql += " ORDER BY p.data_entrega DESC, c.nome"

    with conectar() as con:
        return con.execute(sql, parametros).fetchall()


def obter_pedido(pedido_id):
    with conectar() as con:
        return con.execute("""
            SELECT
                p.*,
                c.nome AS cliente,
                c.telefone AS telefone
            FROM pedidos p
            JOIN clientes c ON c.id = p.cliente_id
            WHERE p.id = ?
        """, (pedido_id,)).fetchone()


def obter_itens(pedido_id):
    with conectar() as con:
        return con.execute("""
            SELECT *
            FROM itens_pedido
            WHERE pedido_id = ?
            ORDER BY id
        """, (pedido_id,)).fetchall()


def atualizar_status_pedido(pedido_id):
    itens = obter_itens(pedido_id)
    if not itens:
        return

    if all(item["separado"] for item in itens):
        novo_status = "Pronto para entrega"
    else:
        novo_status = "Pendente"

    with conectar() as con:
        con.execute("""
            UPDATE pedidos
            SET status = ?
            WHERE id = ? AND status != 'Entregue'
        """, (novo_status, pedido_id))


def formatar_data(valor):
    if not valor:
        return "-"
    return date.fromisoformat(valor).strftime("%d/%m/%Y")


def salvar_historico_etiquetas(data_entrega, pedidos):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    resumo_list = []
    for p in pedidos:
        itens = obter_itens(p["id"])
        itens_txt = " + ".join([f"{i['produto']}" for i in itens])
        resumo_list.append(f"Pedido #{p['id']} - Para: {p['cliente']} | Itens: {itens_txt}")
    
    detalhes = "\n".join(resumo_list)
    with conectar() as con:
        con.execute("""
            INSERT INTO historico_etiquetas (data_geracao, data_filtro_entrega, total_pedidos, detalhes_pedidos)
            VALUES (?, ?, ?, ?)
        """, (agora, str(data_entrega), len(pedidos), detalhes))


def listar_historico_etiquetas():
    with conectar() as con:
        return con.execute("SELECT * FROM historico_etiquetas ORDER BY id DESC").fetchall()


# --------------------------------------------------
# FUNÇÕES AUXILIARES DE RETIRADAS
# --------------------------------------------------

def criar_retirada(nome_cliente, itens, observacoes):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conectar() as con:
        con.execute("""
            INSERT INTO retiradas (nome_cliente, itens, observacoes, data_registro, status)
            VALUES (?, ?, ?, ?, 'Pendente')
        """, (nome_cliente.strip(), itens.strip(), observacoes.strip(), agora))


def listar_retiradas(status=None):
    sql = "SELECT * FROM retiradas WHERE 1=1"
    parametros = []
    if status:
        sql += " AND status = ?"
        parametros.append(status)
    sql += " ORDER BY id DESC"
    with conectar() as con:
        return con.execute(sql, parametros).fetchall()


def atualizar_retirada(retirada_id, nome_cliente, itens, observacoes):
    with conectar() as con:
        con.execute("""
            UPDATE retiradas
            SET nome_cliente = ?, itens = ?, observacoes = ?
            WHERE id = ?
        """, (nome_cliente.strip(), itens.strip(), observacoes.strip(), retirada_id))


def concluir_retirada(retirada_id):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conectar() as con:
        con.execute("""
            UPDATE retiradas
            SET status = 'Concluída', data_conclusao = ?
            WHERE id = ?
        """, (agora, retirada_id))


def excluir_retirada(retirada_id):
    with conectar() as con:
        con.execute("DELETE FROM retiradas WHERE id = ?", (retirada_id,))


# --------------------------------------------------
# GERADOR DE ETIQUETAS EM PDF
# --------------------------------------------------

def gerar_pdf_etiquetas(pedidos):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=1 * cm,
        rightMargin=1 * cm,
        topMargin=1 * cm,
        bottomMargin=1 * cm
    )

    styles = getSampleStyleSheet()
    
    style_de = ParagraphStyle('EtiquetaDe', parent=styles['Normal'], fontSize=13, leading=16, fontName='Helvetica-Bold', textColor=colors.HexColor('#222222'))
    style_para = ParagraphStyle('EtiquetaPara', parent=styles['Normal'], fontSize=15, leading=18, fontName='Helvetica-Bold', textColor=colors.HexColor('#000000'))
    style_itens = ParagraphStyle('EtiquetaItens', parent=styles['Normal'], fontSize=9.5, leading=12, fontName='Helvetica', textColor=colors.HexColor('#111111'))
    style_obs = ParagraphStyle('EtiquetaObs', parent=styles['Normal'], fontSize=9, leading=11, fontName='Helvetica-Oblique', textColor=colors.HexColor('#444444'))

    cards = []

    for p in pedidos:
        itens = obter_itens(p["id"])
        itens_txt = " + ".join([i['produto'] for i in itens])
        
        info_obs = p['local_entrega']
        if p['observacoes']:
            info_obs += f" ({p['observacoes']})"

        conteudo = [
            Paragraph("<b>De: Sônia Vende Tudo</b>", style_de),
            Paragraph(f"<b>Para: {p['cliente']}</b>", style_para),
            Paragraph(f"{itens_txt}", style_itens),
            Paragraph(f"<b>Obs:</b> {info_obs}", style_obs)
        ]

        t_card = Table([[c] for c in conteudo], colWidths=[9.2 * cm])
        t_card.setStyle(TableStyle([
            ('BOX', (0, 0), (-1, -1), 1.2, colors.HexColor('#333333')),
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#FFFFFF')),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ]))
        cards.append(t_card)

    rows = []
    for i in range(0, len(cards), 2):
        if i + 1 < len(cards):
            rows.append([cards[i], cards[i + 1]])
        else:
            rows.append([cards[i], ""])

    grid = Table(rows, colWidths=[9.5 * cm, 9.5 * cm])
    grid.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))

    doc.build([grid])
    buffer.seek(0)
    return buffer.getvalue()


# --------------------------------------------------
# MENU
# --------------------------------------------------

pagina = st.sidebar.radio(
    "Menu principal",
    [
        "Painel inicial",
        "Clientes",
        "Estoque",
        "Pedidos",
        "Checklist",
        "Retiradas",
        "Etiquetas e Histórico",
        "Histórico Geral"
    ]
)

st.sidebar.divider()
st.sidebar.caption("Sistema local com banco SQLite")


# --------------------------------------------------
# PAINEL INICIAL
# --------------------------------------------------

if pagina == "Painel inicial":
    logo_path = PASTA / "logo.jpg"
    if logo_path.exists():
        st.image(str(logo_path), width=400)
    else:
        st.title("📦 Controle de pedidos e entregas")

    st.write("Organize a separação e as entregas da sua loja.")

    pedidos = listar_pedidos()
    clientes = listar_clientes()
    estoque = listar_estoque()
    retiradas_pendentes = listar_retiradas(status="Pendente")

    pendentes = [p for p in pedidos if p["status"] == "Pendente"]
    prontos = [p for p in pedidos if p["status"] == "Pronto para entrega"]
    entregues = [p for p in pedidos if p["status"] == "Entregue"]

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Clientes", len(clientes))
    col2.metric("Itens no Estoque", len(estoque))
    col3.metric("Pedidos pendentes", len(pendentes))
    col4.metric("Prontos p/ entrega", len(prontos))
    col5.metric("Retiradas pend.", len(retiradas_pendentes))

    st.divider()
    st.subheader("📈 Quantidade de Itens Entregues por Data")

    dados_grafico = []
    for p in entregues:
        if p["data_conclusao"]:
            data_str = p["data_conclusao"][:10]
            itens_ped = obter_itens(p["id"])
            qtd_itens = sum([i["quantidade"] for i in itens_ped])
            dados_grafico.append({"Data": data_str, "Itens Entregues": qtd_itens})

    if dados_grafico:
        df_graf = pd.DataFrame(dados_grafico)
        df_agrupado = df_graf.groupby("Data", as_index=False)["Itens Entregues"].sum()
        df_agrupado = df_agrupado.sort_values("Data")
        df_agrupado.set_index("Data", inplace=True)
        st.line_chart(df_agrupado)
    else:
        st.info("Ainda não há registros de pedidos entregues com data de conclusão para exibir no gráfico.")

    st.divider()
    st.subheader("Próximo sábado")
    data_sabado = proximo_sabado()
    st.write(formatar_data(str(data_sabado)))

    pedidos_sabado = listar_pedidos(data_filtro=data_sabado)

    if pedidos_sabado:
        dados = []
        for p in pedidos_sabado:
            dados.append({
                "Pedido": p["id"],
                "Cliente": p["cliente"],
                "Local": p["local_entrega"],
                "Status": p["status"],
                "Itens separados": f'{p["itens_separados"]}/{p["total_itens"]}'
            })
        st.dataframe(pd.DataFrame(dados), width='stretch', hide_index=True)
    else:
        st.info("Nenhum pedido cadastrado para essa data.")


# --------------------------------------------------
# CLIENTES
# --------------------------------------------------

elif pagina == "Clientes":
    st.title("👥 Cadastro de clientes")

    with st.form("form_cliente", clear_on_submit=True):
        nome = st.text_input("Nome do cliente *")
        telefone = st.text_input("Telefone ou WhatsApp")
        observacoes = st.text_area("Observações")
        salvar = st.form_submit_button("Cadastrar cliente")

        if salvar:
            if not nome.strip():
                st.error("Informe o nome do cliente.")
            else:
                with conectar() as con:
                    con.execute("""
                        INSERT INTO clientes (nome, telefone, observacoes)
                        VALUES (?, ?, ?)
                    """, (nome.strip(), telefone.strip(), observacoes.strip()))
                st.success("Cliente cadastrado!")
                st.rerun()

    st.divider()
    st.subheader("Clientes cadastrados")
    clientes = listar_clientes()

    if not clientes:
        st.info("Cadastre seu primeiro cliente.")
    else:
        for c in clientes:
            with st.container(border=True):
                col1, col2, col3 = st.columns([4, 1, 1])

                with col1:
                    st.markdown(f"**{c['nome']}**")
                    if c["telefone"]:
                        st.caption(f"📞 {c['telefone']}")
                    if c["observacoes"]:
                        st.caption(c["observacoes"])

                with col2:
                    if st.button("✏️ Editar", key=f"editar_cli_{c['id']}"):
                        st.session_state[f"editando_cliente_{c['id']}"] = True

                with col3:
                    if st.button("🗑️ Excluir", key=f"excluir_cli_{c['id']}"):
                        st.session_state[f"confirmar_exclusao_cli_{c['id']}"] = True

                if st.session_state.get(f"editando_cliente_{c['id']}", False):
                    with st.form(f"form_editar_cli_{c['id']}"):
                        novo_nome = st.text_input("Nome", value=c["nome"])
                        novo_telefone = st.text_input("Telefone", value=c["telefone"] or "")
                        novas_obs = st.text_area("Observações", value=c["observacoes"] or "")

                        col_salvar, col_cancelar = st.columns(2)
                        with col_salvar:
                            salvar_edicao = st.form_submit_button("Salvar alterações", type="primary")
                        with col_cancelar:
                            cancelar = st.form_submit_button("Cancelar")

                        if salvar_edicao:
                            if not novo_nome.strip():
                                st.error("O nome é obrigatório.")
                            else:
                                with conectar() as con:
                                    con.execute("""
                                        UPDATE clientes
                                        SET nome = ?, telefone = ?, observacoes = ?
                                        WHERE id = ?
                                    """, (novo_nome.strip(), novo_telefone.strip(), novas_obs.strip(), c["id"]))
                                st.success("Cliente atualizado!")
                                st.session_state[f"editando_cliente_{c['id']}"] = False
                                st.rerun()

                        if cancelar:
                            st.session_state[f"editando_cliente_{c['id']}"] = False
                            st.rerun()

                if st.session_state.get(f"confirmar_exclusao_cli_{c['id']}", False):
                    if cliente_tem_pedidos(c["id"]):
                        st.error("Este cliente possui pedidos cadastrados. Exclua os pedidos primeiro.")
                        if st.button("Fechar", key=f"fechar_erro_cli_{c['id']}"):
                            st.session_state[f"confirmar_exclusao_cli_{c['id']}"] = False
                            st.rerun()
                    else:
                        st.warning(f"Tem certeza que deseja excluir o cliente **{c['nome']}**?")
                        col_sim, col_nao = st.columns(2)
                        with col_sim:
                            if st.button("Sim, excluir", key=f"confirma_exc_cli_{c['id']}", type="primary"):
                                with conectar() as con:
                                    con.execute("DELETE FROM clientes WHERE id = ?", (c["id"],))
                                st.success("Cliente excluído!")
                                st.session_state[f"confirmar_exclusao_cli_{c['id']}"] = False
                                st.rerun()
                        with col_nao:
                            if st.button("Cancelar", key=f"cancela_exc_cli_{c['id']}"):
                                st.session_state[f"confirmar_exclusao_cli_{c['id']}"] = False
                                st.rerun()


# --------------------------------------------------
# ESTOQUE (PÁGINA CRUD)
# --------------------------------------------------

elif pagina == "Estoque":
    st.title("📦 Controle de Estoque")

    aba_cad, aba_ger = st.tabs(["➕ Cadastrar Item", "📋 Gerenciar Estoque"])

    with aba_cad:
        st.subheader("Adicionar Novo Item ao Estoque")
        with st.form("form_novo_estoque", clear_on_submit=True):
            nome_produto = st.text_input("Nome do Produto *", placeholder="Ex.: Panela de Pressão")
            qtd_produto = st.number_input("Quantidade Inicial *", min_value=0.0, step=1.0, value=1.0)
            
            salvar_est = st.form_submit_button("Cadastrar no Estoque", type="primary")

            if salvar_est:
                if not nome_produto.strip():
                    st.error("Informe o nome do produto.")
                else:
                    try:
                        cadastrar_item_estoque(nome_produto, qtd_produto)
                        st.success(f"Item '{nome_produto}' cadastrado com sucesso!")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("Já existe um produto com este nome no estoque.")

    with aba_ger:
        st.subheader("Itens Cadastrados no Estoque")
        busca_est = st.text_input("Buscar produto no estoque", key="busca_estoque_txt")

        lista_est = listar_estoque()

        if busca_est.strip():
            b_lower = busca_est.strip().lower()
            lista_est = [e for e in lista_est if b_lower in e["produto"].lower()]

        if not lista_est:
            st.info("Nenhum item encontrado no estoque.")
        else:
            for item in lista_est:
                with st.container(border=True):
                    c1, c2 = st.columns([4, 2])
                    with c1:
                        st.markdown(f"### 📦 {item['produto']}")
                        st.write(f"**Quantidade disponível:** {item['quantidade']:g}")

                    with c2:
                        col_b1, col_b2 = st.columns(2)
                        with col_b1:
                            if st.button("✏️ Editar", key=f"edit_est_{item['id']}"):
                                st.session_state[f"editando_est_{item['id']}"] = True
                        with col_b2:
                            if st.button("🗑️ Excluir", key=f"del_est_{item['id']}"):
                                st.session_state[f"confirm_del_est_{item['id']}"] = True

                    if st.session_state.get(f"editando_est_{item['id']}", False):
                        st.divider()
                        with st.form(f"form_edit_est_{item['id']}"):
                            novo_nome = st.text_input("Produto", value=item["produto"])
                            nova_qtd = st.number_input("Quantidade", value=float(item["quantidade"]), step=1.0)

                            cs1, cs2 = st.columns(2)
                            with cs1:
                                btn_salvar_est = st.form_submit_button("Salvar alterações", type="primary")
                            with cs2:
                                btn_canc_est = st.form_submit_button("Cancelar")

                            if btn_salvar_est:
                                if not novo_nome.strip():
                                    st.error("O nome do produto é obrigatório.")
                                else:
                                    try:
                                        atualizar_item_estoque(item["id"], novo_nome, nova_qtd)
                                        st.session_state[f"editando_est_{item['id']}"] = False
                                        st.success("Estoque atualizado!")
                                        st.rerun()
                                    except sqlite3.IntegrityError:
                                        st.error("Já existe outro produto com este mesmo nome.")

                            if btn_canc_est:
                                st.session_state[f"editando_est_{item['id']}"] = False
                                st.rerun()

                    if st.session_state.get(f"confirm_del_est_{item['id']}", False):
                        st.warning(f"Tem certeza que deseja excluir **{item['produto']}** do estoque?")
                        c_sim, c_nao = st.columns(2)
                        with c_sim:
                            if st.button("Sim, excluir", key=f"conf_del_estoque_{item['id']}", type="primary"):
                                excluir_item_estoque(item["id"])
                                st.session_state[f"confirm_del_est_{item['id']}"] = False
                                st.success("Item excluído do estoque!")
                                st.rerun()
                        with c_nao:
                            if st.button("Cancelar", key=f"canc_del_estoque_{item['id']}"):
                                st.session_state[f"confirm_del_est_{item['id']}"] = False
                                st.rerun()


# --------------------------------------------------
# PEDIDOS (NOVO PEDIDO & GERENCIAR PEDIDOS)
# --------------------------------------------------

elif pagina == "Pedidos":
    st.title("📦 Gerenciamento de Pedidos")

    aba_novo, aba_gerenciar = st.tabs(["➕ Novo pedido", "📋 Gerenciar pedidos"])

    with aba_novo:
        clientes = listar_clientes()
        estoque_atual = listar_estoque()

        if not clientes:
            st.warning("Cadastre um cliente antes de criar um pedido.")
        elif not estoque_atual:
            st.warning("Cadastre itens no **Estoque** antes de criar um pedido para poder selecioná-los.")
        else:
            opcoes_clientes = {
                f'{c["nome"]} (ID {c["id"]})': c["id"]
                for c in clientes
            }

            with st.form("form_pedido"):
                cliente_escolhido = st.selectbox(
                    "Cliente",
                    list(opcoes_clientes.keys())
                )

                data_pedido = st.date_input(
                    "Data do pedido",
                    value=date.today()
                )

                data_entrega = st.date_input(
                    "Data prevista da entrega",
                    value=proximo_sabado()
                )

                local_entrega = st.text_input(
                    "Local de entrega / Estação / Endereço",
                    placeholder="Ex.: Estação Vila das Belezas ou Endereço"
                )

                observacoes = st.text_area("Observações (ex.: Entregar para fulano)")

                st.markdown("### Selecione os produtos do estoque:")
                
                opcoes_produtos_estoque = [f"{e['produto']} (Disp: {e['quantidade']:g})" for e in estoque_atual]
                
                itens_df = pd.DataFrame([
                    {"Produto": opcoes_produtos_estoque[0], "Quantidade": 1.0}
                ])

                itens_editados = st.data_editor(
                    itens_df,
                    num_rows="dynamic",
                    width='stretch',
                    hide_index=True,
                    column_config={
                        "Produto": st.column_config.SelectboxColumn(
                            "Produto do Estoque",
                            options=opcoes_produtos_estoque,
                            required=True
                        ),
                        "Quantidade": st.column_config.NumberColumn(
                            "Quantidade", min_value=0.01, step=1.0, required=True
                        )
                    },
                    key="tabela_itens_novo_pedido"
                )

                salvar_pedido = st.form_submit_button("Salvar pedido e dar baixa no estoque")

                if salvar_pedido:
                    itens_validos = itens_editados.dropna(subset=["Produto"]).copy()
                    
                    if not local_entrega.strip():
                        st.error("Informe o local de entrega.")
                    elif data_entrega < data_pedido:
                        st.error("A entrega não pode ser anterior ao pedido.")
                    elif itens_validos.empty:
                        st.error("Adicione pelo menos um produto.")
                    else:
                        cliente_id = opcoes_clientes[cliente_escolhido]

                        with conectar() as con:
                            cursor = con.execute("""
                                INSERT INTO pedidos (
                                    cliente_id, data_pedido, data_entrega,
                                    tipo_entrega, local_entrega, observacoes, status
                                )
                                VALUES (?, ?, ?, ?, ?, ?, 'Pendente')
                            """, (
                                cliente_id,
                                str(data_pedido),
                                str(data_entrega),
                                "Entrega",
                                local_entrega.strip(),
                                observacoes.strip()
                            ))

                            pedido_id = cursor.lastrowid

                            for _, item in itens_validos.iterrows():
                                prod_str = str(item["Produto"])
                                nome_prod_limpo = prod_str.split(" (Disp:")[0].strip()
                                qtd_pedida = float(item["Quantidade"])

                                con.execute("""
                                    INSERT INTO itens_pedido
                                    (pedido_id, produto, quantidade, unidade)
                                    VALUES (?, ?, ?, 'un')
                                """, (
                                    pedido_id,
                                    nome_prod_limpo,
                                    qtd_pedida
                                ))

                                baixar_estoque(nome_prod_limpo, qtd_pedida)

                        st.success(f"Pedido #{pedido_id} cadastrado e estoque atualizado com sucesso!")

    with aba_gerenciar:
        pedidos = listar_pedidos()

        if not pedidos:
            st.info("Nenhum pedido cadastrado.")
        else:
            col_filtro1, col_filtro2 = st.columns(2)
            with col_filtro1:
                filtro_status = st.selectbox(
                    "Filtrar por status",
                    ["Todos", "Pendente", "Pronto para entrega", "Entregue"],
                    key="filtro_status_gerenciar"
                )
            with col_filtro2:
                busca = st.text_input("Buscar por cliente ou nº do pedido", key="busca_gerenciar_pedido")

            pedidos_filtrados = pedidos
            if filtro_status != "Todos":
                pedidos_filtrados = [p for p in pedidos_filtrados if p["status"] == filtro_status]

            if busca.strip():
                busca_lower = busca.strip().lower()
                pedidos_filtrados = [
                    p for p in pedidos_filtrados
                    if busca_lower in p["cliente"].lower()
                    or busca_lower in str(p["id"])
                ]

            if not pedidos_filtrados:
                st.info("Nenhum pedido encontrado.")
            else:
                for p in pedidos_filtrados:
                    with st.container(border=True):
                        col_info, col_acoes = st.columns([5, 2])

                        with col_info:
                            st.markdown(
                                f"**Pedido #{p['id']}** — {p['cliente']}  \n"
                                f"📅 Entrega: {formatar_data(p['data_entrega'])} | "
                                f"Status: **{p['status']}**  \n"
                                f"📍 Local: {p['local_entrega']}  \n"
                                f"Itens: {p['itens_separados']}/{p['total_itens']} separados"
                            )

                        with col_acoes:
                            if st.button("🗑️ Excluir", key=f"excluir_ped_{p['id']}"):
                                st.session_state[f"confirmar_exclusao_ped_{p['id']}"] = True

                        if st.session_state.get(f"confirmar_exclusao_ped_{p['id']}", False):
                            st.warning(f"Tem certeza que deseja excluir o **Pedido #{p['id']}** de **{p['cliente']}**?")
                            col_sim, col_nao = st.columns(2)
                            with col_sim:
                                if st.button("Sim, excluir", key=f"confirma_exc_ped_{p['id']}", type="primary"):
                                    with conectar() as con:
                                        con.execute("DELETE FROM pedidos WHERE id = ?", (p["id"],))
                                    st.success(f"Pedido #{p['id']} excluído!")
                                    st.session_state[f"confirmar_exclusao_ped_{p['id']}"] = False
                                    st.rerun()
                            with col_nao:
                                if st.button("Cancelar", key=f"cancela_exc_ped_{p['id']}"):
                                    st.session_state[f"confirmar_exclusao_ped_{p['id']}"] = False
                                    st.rerun()


# --------------------------------------------------
# CHECKLIST (SEPARAÇÃO & ENTREGAS DE SÁBADO)
# --------------------------------------------------

elif pagina == "Checklist":
    st.title("📋 Central de Checklists")

    aba_separacao, aba_entregas = st.tabs(["✅ Separação", "🚚 Entregas de sábado"])

    with aba_separacao:
        st.subheader("Checklist de separação")
        st.write("Marque os produtos conforme forem colocados na sacola.")

        pedidos = [p for p in listar_pedidos() if p["status"] != "Entregue"]

        if not pedidos:
            st.info("Não há pedidos pendentes de separação.")
        else:
            opcoes = {
                f'Pedido #{p["id"]} - {p["cliente"]} ({p["itens_separados"]}/{p["total_itens"]} separados)': p["id"]
                for p in pedidos
            }

            pedido_label = st.selectbox("Selecione o pedido", list(opcoes.keys()), key="select_sep_pedido")
            pedido_id = opcoes[pedido_label]
            pedido = next(p for p in pedidos if p["id"] == pedido_id)

            st.write(f'**Cliente:** {pedido["cliente"]}')
            st.write(f'**Entrega:** {formatar_data(pedido["data_entrega"])}')
            st.write(f'**Local:** {pedido["local_entrega"]}')

            itens = obter_itens(pedido_id)

            with st.form(f"form_separacao_{pedido_id}"):
                marcacoes = {}
                for item in itens:
                    marcacoes[item["id"]] = st.checkbox(
                        f'{item["produto"]} — {item["quantidade"]:g}',
                        value=bool(item["separado"]),
                        key=f'separacao_{item["id"]}'
                    )

                salvar_separacao = st.form_submit_button("Salvar checklist")

                if salvar_separacao:
                    with conectar() as con:
                        for item_id, separado in marcacoes.items():
                            con.execute("""
                                UPDATE itens_pedido
                                SET separado = ?
                                WHERE id = ? AND pedido_id = ?
                            """, (int(separado), item_id, pedido_id))

                    atualizar_status_pedido(pedido_id)
                    st.success("Checklist de separação atualizado.")
                    st.rerun()

            separados = sum(item["separado"] for item in itens)
            st.progress(separados / len(itens) if itens else 0)

    with aba_entregas:
        st.subheader("Checklist de entregas")

        data_entrega = st.date_input(
            "Data das entregas",
            value=proximo_sabado(),
            key="data_checklist_entregas"
        )

        pedidos_sab = listar_pedidos(data_filtro=data_entrega)

        if not pedidos_sab:
            st.info("Nenhum pedido encontrado para essa data.")
        else:
            for p in pedidos_sab:
                with st.container(border=True):
                    st.subheader(f'Pedido #{p["id"]} — {p["cliente"]}')
                    st.write(f'**Local:** {p["local_entrega"]}')
                    st.write(f'**Separação:** {p["itens_separados"]}/{p["total_itens"]} itens')

                    if p["status"] == "Entregue":
                        st.success(f'Entregue em {formatar_data(p["data_conclusao"])}')
                    elif p["status"] != "Pronto para entrega":
                        st.warning("Pedido ainda não está totalmente separado.")
                    else:
                        st.info("Pedido pronto para entrega.")

                    with st.expander("Conferir produtos"):
                        for item in obter_itens(p["id"]):
                            marca = "✅" if item["separado"] else "⬜"
                            st.write(f'{marca} {item["produto"]} — {item["quantidade"]:g}')

                    if p["status"] == "Pronto para entrega":
                        if st.button("Confirmar pedido entregue", key=f'entregue_{p["id"]}', type="primary"):
                            agora = date.today().isoformat()
                            with conectar() as con:
                                con.execute("""
                                    UPDATE pedidos
                                    SET status = 'Entregue', data_conclusao = ?
                                    WHERE id = ? AND status = 'Pronto para entrega'
                                """, (agora, p["id"]))
                            st.success("Entrega registrada!")
                            st.rerun()


# --------------------------------------------------
# RETIRADAS
# --------------------------------------------------

elif pagina == "Retiradas":
    st.title("🛍️ Retiradas de Itens Comprados")

    aba_nova, aba_gerenciar, aba_hist_retiradas = st.tabs([
        "➕ Cadastrar Retirada",
        "📋 Retiradas Pendentes",
        "📚 Histórico de Retiradas Concluídas"
    ])

    with aba_nova:
        st.subheader("Nova Retirada Presencial")
        with st.form("form_nova_retirada", clear_on_submit=True):
            nome_cli = st.text_input("Nome do Cliente *", placeholder="Ex.: Maria Souza")
            itens_ret = st.text_area("Item / Itens a retirar *", placeholder="Ex.: Panela de Pressão Electrolux + Conjunto de Copos")
            obs_ret = st.text_input("Observações (OBS)", placeholder="Ex.: Já pago via PIX / Retira no sábado à tarde")

            salvar_ret = st.form_submit_button("Cadastrar Retirada", type="primary")

            if salvar_ret:
                if not nome_cli.strip():
                    st.error("Informe o nome do cliente.")
                elif not itens_ret.strip():
                    st.error("Informe pelo menos um item para retirada.")
                else:
                    criar_retirada(nome_cli, itens_ret, obs_ret)
                    st.success("Retirada cadastrada com sucesso!")
                    st.rerun()

    with aba_gerenciar:
        st.subheader("Retiradas Aguardando Busca")
        busca_ret = st.text_input("Buscar por cliente ou item", key="busca_ret_pendente")

        retiradas_pendentes = listar_retiradas(status="Pendente")

        if busca_ret.strip():
            b_lower = busca_ret.strip().lower()
            retiradas_pendentes = [
                r for r in retiradas_pendentes
                if b_lower in r["nome_cliente"].lower() or b_lower in r["itens"].lower()
            ]

        if not retiradas_pendentes:
            st.info("Nenhuma retirada pendente encontrada.")
        else:
            for r in retiradas_pendentes:
                with st.container(border=True):
                    col_det, col_act = st.columns([4, 2])

                    with col_det:
                        st.markdown(f"### 👤 {r['nome_cliente']}")
                        st.markdown(f"📦 **Itens:** {r['itens']}")
                        if r["observacoes"]:
                            st.caption(f"📝 **OBS:** {r['observacoes']}")
                        st.caption(f"📅 Cadastrado em: {r['data_registro']}")

                    with col_act:
                        if st.button("✅ Confirmar Retirado", key=f"concluir_ret_{r['id']}", type="primary"):
                            concluir_retirada(r["id"])
                            st.success("Retirada concluída e salva no histórico!")
                            st.rerun()

                        col_btn1, col_btn2 = st.columns(2)
                        with col_btn1:
                            if st.button("✏️ Editar", key=f"edit_ret_btn_{r['id']}"):
                                st.session_state[f"editando_ret_{r['id']}"] = True

                        with col_btn2:
                            if st.button("🗑️ Excluir", key=f"del_ret_btn_{r['id']}"):
                                st.session_state[f"confirm_del_ret_{r['id']}"] = True

                    if st.session_state.get(f"editando_ret_{r['id']}", False):
                        st.divider()
                        with st.form(f"form_edit_ret_{r['id']}"):
                            novo_nome_r = st.text_input("Nome", value=r["nome_cliente"])
                            novos_itens_r = st.text_area("Itens", value=r["itens"])
                            novas_obs_r = st.text_input("OBS", value=r["observacoes"] or "")

                            c_save, c_canc = st.columns(2)
                            with c_save:
                                b_salvar = st.form_submit_button("Salvar alterações", type="primary")
                            with c_canc:
                                b_canc = st.form_submit_button("Cancelar")

                            if b_salvar:
                                if not novo_nome_r.strip() or not novos_itens_r.strip():
                                    st.error("Preencha o nome e os itens.")
                                else:
                                    atualizar_retirada(r["id"], novo_nome_r, novos_itens_r, novas_obs_r)
                                    st.session_state[f"editando_ret_{r['id']}"] = False
                                    st.success("Retirada atualizada!")
                                    st.rerun()

                            if b_canc:
                                st.session_state[f"editando_ret_{r['id']}"] = False
                                st.rerun()

                    if st.session_state.get(f"confirm_del_ret_{r['id']}", False):
                        st.warning(f"Tem certeza que deseja excluir a retirada de **{r['nome_cliente']}**?")
                        cs, cn = st.columns(2)
                        with cs:
                            if st.button("Sim, excluir", key=f"conf_del_r_{r['id']}", type="primary"):
                                excluir_retirada(r["id"])
                                st.session_state[f"confirm_del_ret_{r['id']}"] = False
                                st.success("Retirada excluída!")
                                st.rerun()
                        with cn:
                            if st.button("Cancelar", key=f"canc_del_r_{r['id']}"):
                                st.session_state[f"confirm_del_ret_{r['id']}"] = False
                                st.rerun()

    with aba_hist_retiradas:
        st.subheader("Histórico de Itens Já Retirados")
        retiradas_concluidas = listar_retiradas(status="Concluída")

        if not retiradas_concluidas:
            st.info("Nenhuma retirada foi concluída ainda.")
        else:
            dados_hist = []
            for r in retiradas_concluidas:
                dados_hist.append({
                    "ID": r["id"],
                    "Cliente": r["nome_cliente"],
                    "Itens": r["itens"],
                    "Observações": r["observacoes"] or "-",
                    "Data do Cadastro": r["data_registro"],
                    "Data da Retirada": r["data_conclusao"]
                })

            df_hist = pd.DataFrame(dados_hist)
            st.dataframe(df_hist, width='stretch', hide_index=True)

            csv_ret = df_hist.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 Exportar Histórico de Retiradas (CSV)",
                data=csv_ret,
                file_name="historico_retiradas.csv",
                mime="text/csv"
            )


# --------------------------------------------------
# ETIQUETAS E HISTÓRICO
# --------------------------------------------------

elif pagina == "Etiquetas e Histórico":
    st.title("🏷️ Etiquetas de Entrega")

    aba_gerar, aba_historico = st.tabs(["📄 Gerar Etiquetas Atuais", "📚 Histórico de Etiquetas"])

    with aba_gerar:
        data_entrega = st.date_input("Selecione a data de entrega", value=proximo_sabado(), key="dt_etiq")
        pedidos = listar_pedidos(data_filtro=data_entrega)

        if not pedidos:
            st.info("Nenhum pedido encontrado para esta data.")
        else:
            st.success(f"Encontrados {len(pedidos)} pedido(s) para a entrega de {formatar_data(str(data_entrega))}.")
            
            st.subheader("Pré-visualização do formato das etiquetas:")
            for p in pedidos:
                itens = obter_itens(p["id"])
                itens_txt = " + ".join([i['produto'] for i in itens])
                info_obs = p['local_entrega'] + (f" ({p['observacoes']})" if p['observacoes'] else "")
                
                st.info(
                    f"**De: Sônia Vende Tudo**  \n"
                    f"**Para: {p['cliente']}**  \n"
                    f"{itens_txt}  \n"
                    f"*Obs:* {info_obs}"
                )

            pdf_bytes = gerar_pdf_etiquetas(pedidos)

            col1, col2 = st.columns(2)
            with col1:
                if st.download_button(
                    label="📄 Baixar PDF com Todas as Etiquetas (Folha A4)",
                    data=pdf_bytes,
                    file_name=f"etiquetas_{data_entrega}.pdf",
                    mime="application/pdf",
                    type="primary"
                ):
                    salvar_historico_etiquetas(data_entrega, pedidos)
                    st.toast("Lote de etiquetas registrado no histórico!")

    with aba_historico:
        st.subheader("Histórico de PDFs / Lotes Gerados")
        historico = listar_historico_etiquetas()

        if not historico:
            st.info("Nenhum histórico de etiquetas foi registrado ainda.")
        else:
            for h in historico:
                with st.expander(f"Lote de {formatar_data(h['data_filtro_entrega'])} - Gerado em: {h['data_geracao']} ({h['total_pedidos']} pedidos)"):
                    st.text(h['detalhes_pedidos'])


# --------------------------------------------------
# HISTÓRICO GERAL
# --------------------------------------------------

elif pagina == "Histórico Geral":
    st.title("📚 Histórico geral de pedidos")

    col1, col2 = st.columns(2)
    with col1:
        data_inicio = st.date_input("Data inicial", value=date.today() - timedelta(days=30), key="historico_inicio")
    with col2:
        data_fim = st.date_input("Data final", value=date.today(), key="historico_fim")

    status_filtro = st.selectbox("Status do pedido", ["Todos", "Pendente", "Pronto para entrega", "Entregue"])

    todos = listar_pedidos()
    filtrados = [
        p for p in todos
        if data_inicio.isoformat() <= p["data_entrega"] <= data_fim.isoformat()
        and (status_filtro == "Todos" or p["status"] == status_filtro)
    ]

    if filtrados:
        dados = []
        for p in filtrados:
            dados.append({
                "Pedido": p["id"],
                "Cliente": p["cliente"],
                "Telefone": p["telefone"],
                "Data prevista": formatar_data(p["data_entrega"]),
                "Local": p["local_entrega"],
                "Status": p["status"],
                "Data de conclusão": formatar_data(p["data_conclusao"])
            })

        historico_df = pd.DataFrame(dados)
        st.dataframe(historico_df, width='stretch', hide_index=True)
    else:
        st.info("Nenhum pedido encontrado nesse período.")