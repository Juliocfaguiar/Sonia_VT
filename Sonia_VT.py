import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

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

            CREATE TABLE IF NOT EXISTS pedidos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente_id INTEGER NOT NULL,
                data_pedido TEXT NOT NULL,
                data_entrega TEXT NOT NULL,
                tipo_entrega TEXT NOT NULL,
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

            CREATE INDEX IF NOT EXISTS idx_pedidos_data
            ON pedidos(data_entrega);

            CREATE INDEX IF NOT EXISTS idx_itens_pedido
            ON itens_pedido(pedido_id);
        """)


inicializar_banco()


# --------------------------------------------------
# FUNÇÕES AUXILIARES
# --------------------------------------------------

def proximo_sabado():
    hoje = date.today()
    dias = (5 - hoje.weekday()) % 7
    return hoje + timedelta(days=dias)


def listar_clientes():
    with conectar() as con:
        return con.execute(
            "SELECT * FROM clientes ORDER BY nome"
        ).fetchall()


def obter_cliente(cliente_id):
    with conectar() as con:
        return con.execute(
            "SELECT * FROM clientes WHERE id = ?",
            (cliente_id,)
        ).fetchone()


def cliente_tem_pedidos(cliente_id):
    with conectar() as con:
        resultado = con.execute(
            "SELECT COUNT(*) AS total FROM pedidos WHERE cliente_id = ?",
            (cliente_id,)
        ).fetchone()
        return resultado["total"] > 0


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


# --------------------------------------------------
# MENU
# --------------------------------------------------

st.sidebar.title("📦 Minha Loja")

pagina = st.sidebar.radio(
    "Menu principal",
    [
        "Painel inicial",
        "Clientes",
        "Novo pedido",
        "Gerenciar pedidos",
        "Separação",
        "Entregas de sábado",
        "Histórico"
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

    pendentes = [p for p in pedidos if p["status"] == "Pendente"]
    prontos = [p for p in pedidos if p["status"] == "Pronto para entrega"]
    entregues = [p for p in pedidos if p["status"] == "Entregue"]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Clientes", len(clientes))
    col2.metric("Pedidos pendentes", len(pendentes))
    col3.metric("Prontos para entrega", len(prontos))
    col4.metric("Pedidos entregues", len(entregues))

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
        st.dataframe(
            pd.DataFrame(dados),
            use_container_width=True,
            hide_index=True
        )
    else:
        st.info("Nenhum pedido cadastrado para essa data.")


# --------------------------------------------------
# CLIENTES
# --------------------------------------------------

elif pagina == "Clientes":
    st.title("👥 Cadastro de clientes")

    # Formulário de novo cliente
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
                        INSERT INTO clientes
                        (nome, telefone, observacoes)
                        VALUES (?, ?, ?)
                    """, (
                        nome.strip(),
                        telefone.strip(),
                        observacoes.strip()
                    ))
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

                # Formulário de edição
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
                                    """, (
                                        novo_nome.strip(),
                                        novo_telefone.strip(),
                                        novas_obs.strip(),
                                        c["id"]
                                    ))
                                st.success("Cliente atualizado!")
                                st.session_state[f"editando_cliente_{c['id']}"] = False
                                st.rerun()

                        if cancelar:
                            st.session_state[f"editando_cliente_{c['id']}"] = False
                            st.rerun()

                # Confirmação de exclusão
                if st.session_state.get(f"confirmar_exclusao_cli_{c['id']}", False):
                    if cliente_tem_pedidos(c["id"]):
                        st.error(
                            "Este cliente possui pedidos cadastrados. "
                            "Exclua os pedidos primeiro ou altere o cliente nos pedidos."
                        )
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
# NOVO PEDIDO
# --------------------------------------------------

elif pagina == "Novo pedido":
    st.title("🛍️ Cadastrar pedido")

    clientes = listar_clientes()

    if not clientes:
        st.warning("Cadastre um cliente antes de criar um pedido.")
        st.stop()

    opcoes_clientes = {
        f'{c["nome"]} (ID {c["id"]})': c["id"]
        for c in clientes
    }

    st.caption(
        "Adicione uma linha para cada produto. "
        "Você pode incluir quantos itens precisar."
    )

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

        tipo_entrega = st.selectbox(
            "Tipo de entrega",
            [
                "Estação de trem",
                "Estação de metrô",
                "Entrega pessoal"
            ]
        )

        local_entrega = st.text_input(
            "Local combinado",
            placeholder="Ex.: Estação da Luz ou endereço combinado"
        )

        observacoes = st.text_area("Observações do pedido")

        itens_df = pd.DataFrame([
            {"Produto": "", "Quantidade": 1.0, "Unidade": "un"}
        ])

        itens_editados = st.data_editor(
            itens_df,
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            column_config={
                "Produto": st.column_config.TextColumn(
                    "Produto",
                    required=True
                ),
                "Quantidade": st.column_config.NumberColumn(
                    "Quantidade",
                    min_value=0.01,
                    step=1.0,
                    required=True
                ),
                "Unidade": st.column_config.SelectboxColumn(
                    "Unidade",
                    options=["un", "pacote", "caixa", "kg", "g", "litro", "m"],
                    required=True
                )
            },
            key="tabela_itens_novo_pedido"
        )

        salvar_pedido = st.form_submit_button("Salvar pedido")

        if salvar_pedido:
            itens_validos = itens_editados.copy()
            itens_validos["Produto"] = (
                itens_validos["Produto"].fillna("").astype(str).str.strip()
            )
            itens_validos = itens_validos[
                itens_validos["Produto"] != ""
            ]

            if not local_entrega.strip():
                st.error("Informe o local combinado para a entrega.")
            elif data_entrega < data_pedido:
                st.error("A entrega não pode ser anterior ao pedido.")
            elif itens_validos.empty:
                st.error("Adicione pelo menos um produto.")
            elif (
                itens_validos["Quantidade"].isna().any()
                or (itens_validos["Quantidade"] <= 0).any()
            ):
                st.error("Todas as quantidades devem ser maiores que zero.")
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
                        tipo_entrega,
                        local_entrega.strip(),
                        observacoes.strip()
                    ))

                    pedido_id = cursor.lastrowid

                    for _, item in itens_validos.iterrows():
                        con.execute("""
                            INSERT INTO itens_pedido
                            (pedido_id, produto, quantidade, unidade)
                            VALUES (?, ?, ?, ?)
                        """, (
                            pedido_id,
                            str(item["Produto"]).strip(),
                            float(item["Quantidade"]),
                            str(item["Unidade"])
                        ))

                st.success(f"Pedido #{pedido_id} cadastrado com sucesso!")


# --------------------------------------------------
# GERENCIAR PEDIDOS (EDIÇÃO E EXCLUSÃO)
# --------------------------------------------------

elif pagina == "Gerenciar pedidos":
    st.title("📋 Gerenciar pedidos")

    pedidos = listar_pedidos()

    if not pedidos:
        st.info("Nenhum pedido cadastrado.")
        st.stop()

    # Filtros
    col_filtro1, col_filtro2 = st.columns(2)
    with col_filtro1:
        filtro_status = st.selectbox(
            "Filtrar por status",
            ["Todos", "Pendente", "Pronto para entrega", "Entregue"]
        )
    with col_filtro2:
        busca = st.text_input("Buscar por cliente ou nº do pedido")

    # Aplicar filtros
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
        st.info("Nenhum pedido encontrado com os filtros aplicados.")
        st.stop()

    st.caption(f"{len(pedidos_filtrados)} pedido(s) encontrado(s)")

    for p in pedidos_filtrados:
        with st.container(border=True):
            col_info, col_acoes = st.columns([5, 2])

            with col_info:
                st.markdown(
                    f"**Pedido #{p['id']}** — {p['cliente']}  \n"
                    f"📅 Entrega: {formatar_data(p['data_entrega'])} | "
                    f"Status: **{p['status']}**  \n"
                    f"📍 {p['tipo_entrega']} — {p['local_entrega']}  \n"
                    f"Itens: {p['itens_separados']}/{p['total_itens']} separados"
                )

            with col_acoes:
                if st.button("✏️ Editar", key=f"editar_ped_{p['id']}"):
                    st.session_state["pedido_editando"] = p["id"]
                    st.rerun()

                if st.button("🗑️ Excluir", key=f"excluir_ped_{p['id']}"):
                    st.session_state[f"confirmar_exclusao_ped_{p['id']}"] = True

            # Confirmação de exclusão
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

    # ========== FORMULÁRIO DE EDIÇÃO DE PEDIDO ==========
    if "pedido_editando" in st.session_state and st.session_state["pedido_editando"]:
        pedido_id = st.session_state["pedido_editando"]
        pedido = obter_pedido(pedido_id)

        if not pedido:
            st.error("Pedido não encontrado.")
            del st.session_state["pedido_editando"]
            st.stop()

        st.divider()
        st.subheader(f"✏️ Editando Pedido #{pedido_id}")

        clientes = listar_clientes()
        opcoes_clientes = {
            f'{c["nome"]} (ID {c["id"]})': c["id"]
            for c in clientes
        }

        # Encontrar o valor atual do cliente no selectbox
        cliente_atual_label = next(
            (label for label, cid in opcoes_clientes.items() if cid == pedido["cliente_id"]),
            list(opcoes_clientes.keys())[0]
        )

        with st.form("form_editar_pedido"):
            cliente_escolhido = st.selectbox(
                "Cliente",
                list(opcoes_clientes.keys()),
                index=list(opcoes_clientes.keys()).index(cliente_atual_label)
            )

            data_pedido = st.date_input(
                "Data do pedido",
                value=date.fromisoformat(pedido["data_pedido"])
            )

            data_entrega = st.date_input(
                "Data prevista da entrega",
                value=date.fromisoformat(pedido["data_entrega"])
            )

            tipo_entrega = st.selectbox(
                "Tipo de entrega",
                [
                    "Estação de trem",
                    "Estação de metrô",
                    "Entrega pessoal"
                ],
                index=[
                    "Estação de trem",
                    "Estação de metrô",
                    "Entrega pessoal"
                ].index(pedido["tipo_entrega"])
                if pedido["tipo_entrega"] in [
                    "Estação de trem",
                    "Estação de metrô",
                    "Entrega pessoal"
                ] else 0
            )

            local_entrega = st.text_input(
                "Local combinado",
                value=pedido["local_entrega"]
            )

            observacoes = st.text_area(
                "Observações do pedido",
                value=pedido["observacoes"] or ""
            )

            # Itens atuais
            itens_atuais = obter_itens(pedido_id)
            if itens_atuais:
                itens_df = pd.DataFrame([
                    {
                        "Produto": i["produto"],
                        "Quantidade": i["quantidade"],
                        "Unidade": i["unidade"]
                    }
                    for i in itens_atuais
                ])
            else:
                itens_df = pd.DataFrame([
                    {"Produto": "", "Quantidade": 1.0, "Unidade": "un"}
                ])

            st.markdown("**Itens do pedido**")
            itens_editados = st.data_editor(
                itens_df,
                num_rows="dynamic",
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Produto": st.column_config.TextColumn("Produto", required=True),
                    "Quantidade": st.column_config.NumberColumn(
                        "Quantidade", min_value=0.01, step=1.0, required=True
                    ),
                    "Unidade": st.column_config.SelectboxColumn(
                        "Unidade",
                        options=["un", "pacote", "caixa", "kg", "g", "litro", "m"],
                        required=True
                    )
                },
                key=f"tabela_itens_editar_{pedido_id}"
            )

            col_salvar, col_cancelar = st.columns(2)
            with col_salvar:
                salvar = st.form_submit_button("Salvar alterações", type="primary")
            with col_cancelar:
                cancelar = st.form_submit_button("Cancelar edição")

            if cancelar:
                del st.session_state["pedido_editando"]
                st.rerun()

            if salvar:
                itens_validos = itens_editados.copy()
                itens_validos["Produto"] = (
                    itens_validos["Produto"].fillna("").astype(str).str.strip()
                )
                itens_validos = itens_validos[itens_validos["Produto"] != ""]

                if not local_entrega.strip():
                    st.error("Informe o local combinado.")
                elif data_entrega < data_pedido:
                    st.error("A entrega não pode ser anterior ao pedido.")
                elif itens_validos.empty:
                    st.error("Adicione pelo menos um produto.")
                elif (
                    itens_validos["Quantidade"].isna().any()
                    or (itens_validos["Quantidade"] <= 0).any()
                ):
                    st.error("Todas as quantidades devem ser maiores que zero.")
                else:
                    cliente_id = opcoes_clientes[cliente_escolhido]

                    with conectar() as con:
                        # Atualiza o pedido
                        con.execute("""
                            UPDATE pedidos SET
                                cliente_id = ?,
                                data_pedido = ?,
                                data_entrega = ?,
                                tipo_entrega = ?,
                                local_entrega = ?,
                                observacoes = ?
                            WHERE id = ?
                        """, (
                            cliente_id,
                            str(data_pedido),
                            str(data_entrega),
                            tipo_entrega,
                            local_entrega.strip(),
                            observacoes.strip(),
                            pedido_id
                        ))

                        # Remove itens antigos e insere os novos
                        con.execute(
                            "DELETE FROM itens_pedido WHERE pedido_id = ?",
                            (pedido_id,)
                        )

                        for _, item in itens_validos.iterrows():
                            con.execute("""
                                INSERT INTO itens_pedido
                                (pedido_id, produto, quantidade, unidade, separado)
                                VALUES (?, ?, ?, ?, 0)
                            """, (
                                pedido_id,
                                str(item["Produto"]).strip(),
                                float(item["Quantidade"]),
                                str(item["Unidade"])
                            ))

                    # Atualiza status após alteração dos itens
                    atualizar_status_pedido(pedido_id)

                    st.success(f"Pedido #{pedido_id} atualizado com sucesso!")
                    del st.session_state["pedido_editando"]
                    st.rerun()


# --------------------------------------------------
# SEPARAÇÃO
# --------------------------------------------------

elif pagina == "Separação":
    st.title("✅ Checklist de separação")
    st.write("Marque os produtos conforme forem colocados na sacola.")

    pedidos = [
        p for p in listar_pedidos()
        if p["status"] != "Entregue"
    ]

    if not pedidos:
        st.info("Não há pedidos pendentes de separação.")
        st.stop()

    opcoes = {
        f'Pedido #{p["id"]} - {p["cliente"]} '
        f'({p["itens_separados"]}/{p["total_itens"]} separados)': p["id"]
        for p in pedidos
    }

    pedido_label = st.selectbox(
        "Selecione o pedido",
        list(opcoes.keys())
    )
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
                f'{item["produto"]} — {item["quantidade"]:g} '
                f'{item["unidade"]}',
                value=bool(item["separado"]),
                key=f'separacao_{item["id"]}'
            )

        salvar_separacao = st.form_submit_button(
            "Salvar checklist"
        )

        if salvar_separacao:
            with conectar() as con:
                for item_id, separado in marcacoes.items():
                    con.execute("""
                        UPDATE itens_pedido
                        SET separado = ?
                        WHERE id = ? AND pedido_id = ?
                    """, (
                        int(separado), item_id, pedido_id
                    ))

            atualizar_status_pedido(pedido_id)
            st.success("Checklist de separação atualizado.")
            st.rerun()

    separados = sum(item["separado"] for item in itens)

    st.progress(separados / len(itens) if itens else 0)
    st.caption(f"{separados} de {len(itens)} itens separados.")


# --------------------------------------------------
# ENTREGAS DE SÁBADO
# --------------------------------------------------

elif pagina == "Entregas de sábado":
    st.title("🚚 Checklist de entregas")

    data_entrega = st.date_input(
        "Data das entregas",
        value=proximo_sabado(),
        key="data_checklist_entregas"
    )

    tipo_filtro = st.selectbox(
        "Filtrar por tipo de entrega",
        [
            "Todos",
            "Estação de trem",
            "Estação de metrô",
            "Entrega pessoal"
        ]
    )

    pedidos = listar_pedidos(data_filtro=data_entrega)

    if tipo_filtro != "Todos":
        pedidos = [
            p for p in pedidos
            if p["tipo_entrega"] == tipo_filtro
        ]

    if not pedidos:
        st.info("Nenhum pedido encontrado para essa data.")
        st.stop()

    for p in pedidos:
        with st.container(border=True):
            st.subheader(f'Pedido #{p["id"]} — {p["cliente"]}')
            st.write(f'**Tipo:** {p["tipo_entrega"]}')
            st.write(f'**Local:** {p["local_entrega"]}')
            st.write(
                f'**Separação:** {p["itens_separados"]}/'
                f'{p["total_itens"]} itens'
            )

            if p["status"] == "Entregue":
                st.success(
                    f'Entregue em {formatar_data(p["data_conclusao"])}'
                )

            elif p["status"] != "Pronto para entrega":
                st.warning("Pedido ainda não está totalmente separado.")

            else:
                st.info("Pedido pronto para entrega.")

            with st.expander("Conferir produtos"):
                for item in obter_itens(p["id"]):
                    marca = "✅" if item["separado"] else "⬜"
                    st.write(
                        f'{marca} {item["produto"]} — '
                        f'{item["quantidade"]:g} {item["unidade"]}'
                    )

            if p["status"] == "Pronto para entrega":
                if st.button(
                    "Confirmar pedido entregue",
                    key=f'entregue_{p["id"]}',
                    type="primary"
                ):
                    agora = date.today().isoformat()

                    with conectar() as con:
                        con.execute("""
                            UPDATE pedidos
                            SET status = 'Entregue',
                                data_conclusao = ?
                            WHERE id = ?
                              AND status = 'Pronto para entrega'
                        """, (agora, p["id"]))

                    st.success("Entrega registrada!")
                    st.rerun()


# --------------------------------------------------
# HISTÓRICO
# --------------------------------------------------

elif pagina == "Histórico":
    st.title("📚 Histórico de pedidos e entregas")

    col1, col2 = st.columns(2)

    with col1:
        data_inicio = st.date_input(
            "Data inicial",
            value=date.today() - timedelta(days=30),
            key="historico_inicio"
        )

    with col2:
        data_fim = st.date_input(
            "Data final",
            value=date.today(),
            key="historico_fim"
        )

    status_filtro = st.selectbox(
        "Status do pedido",
        ["Todos", "Pendente", "Pronto para entrega", "Entregue"]
    )

    if data_inicio > data_fim:
        st.error("A data inicial deve ser anterior à data final.")
        st.stop()

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
                "Tipo": p["tipo_entrega"],
                "Local": p["local_entrega"],
                "Itens separados": f'{p["itens_separados"]}/{p["total_itens"]}',
                "Status": p["status"],
                "Data de conclusão": formatar_data(p["data_conclusao"])
            })

        historico_df = pd.DataFrame(dados)

        st.dataframe(
            historico_df,
            use_container_width=True,
            hide_index=True
        )

        csv = historico_df.to_csv(index=False).encode("utf-8-sig")

        st.download_button(
            "Exportar histórico em CSV",
            data=csv,
            file_name=f"historico_{data_inicio}_{data_fim}.csv",
            mime="text/csv"
        )

        st.subheader("Detalhes dos pedidos")

        pedido_id = st.selectbox(
            "Consultar itens de um pedido",
            [p["id"] for p in filtrados],
            format_func=lambda pid: next(
                f'Pedido #{p["id"]} - {p["cliente"]}'
                for p in filtrados if p["id"] == pid
            )
        )

        pedido_itens = obter_itens(pedido_id)

        st.dataframe(
            pd.DataFrame([
                {
                    "Produto": i["produto"],
                    "Quantidade": i["quantidade"],
                    "Unidade": i["unidade"],
                    "Separado": "Sim" if i["separado"] else "Não"
                }
                for i in pedido_itens
            ]),
            use_container_width=True,
            hide_index=True
        )
    else:
        st.info("Nenhum pedido encontrado nesse período.")