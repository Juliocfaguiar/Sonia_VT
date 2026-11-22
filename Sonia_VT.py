import io
import sqlite3  # usado apenas para manter o tratamento de duplicidade na interface
from datetime import date, datetime, timedelta
from pathlib import Path

import firebase_admin
import pandas as pd
import streamlit as st
from firebase_admin import credentials, firestore

# ReportLab para geração das etiquetas em PDF
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

# --------------------------------------------------
# CONFIGURAÇÃO
# --------------------------------------------------

PASTA = Path(__file__).resolve().parent

st.set_page_config(
    page_title="Controle SoniaVT",
    page_icon="📦",
    layout="wide",
)


# --------------------------------------------------
# FIREBASE / CLOUD FIRESTORE
# --------------------------------------------------

@st.cache_resource
def obter_banco():
    """Inicializa o Firebase usando os Secrets do Streamlit Cloud."""
    if not firebase_admin._apps:
        if "firebase" not in st.secrets:
            st.error("Configure os Secrets do Firebase no Streamlit Cloud antes de usar o aplicativo.")
            st.stop()
        dados_credencial = dict(st.secrets["firebase"])
        cred = credentials.Certificate(dados_credencial)
        firebase_admin.initialize_app(cred)
    return firestore.client()


db = obter_banco()


class ResultadoFirestore:
    def __init__(self, rows=None, lastrowid=None):
        self.rows = rows or []
        self.lastrowid = lastrowid

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class ConexaoFirestore:
    """Adaptador para manter compatíveis as operações CRUD da interface existente."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self):
        return None

    def executescript(self, script):
        return None

    def execute(self, sql, params=()):
        q = " ".join(sql.lower().split())
        p = tuple(params or ())

        if q.startswith("insert into clientes"):
            numero = obter_proximo_numero_cliente()
            ref = db.collection("clientes").document()
            ref.set({
                "numero": numero,
                "nome": p[0],
                "telefone": p[1],
                "observacoes": p[2],
            })
            return ResultadoFirestore(lastrowid=ref.id)

        if q.startswith("update clientes"):
            nome, telefone, obs, doc_id = p
            db.collection("clientes").document(str(doc_id)).update({
                "nome": nome,
                "telefone": telefone,
                "observacoes": obs,
            })
            return ResultadoFirestore()

        if q.startswith("delete from clientes"):
            db.collection("clientes").document(str(p[0])).delete()
            return ResultadoFirestore()

        if q.startswith("insert into estoque"):
            nome, qtd = p
            existentes = list(db.collection("estoque").where("produto", "==", nome).limit(1).stream())
            if existentes:
                raise sqlite3.IntegrityError("Produto já cadastrado")
            ref = db.collection("estoque").document()
            ref.set({"produto": nome, "quantidade": float(qtd)})
            return ResultadoFirestore(lastrowid=ref.id)

        if q.startswith("update estoque"):
            if "set produto = ?, quantidade = ?" in q:
                nome, qtd, doc_id = p
                existentes = list(db.collection("estoque").where("produto", "==", nome).stream())
                if any(x.id != str(doc_id) for x in existentes):
                    raise sqlite3.IntegrityError("Produto já cadastrado")
                db.collection("estoque").document(str(doc_id)).update({
                    "produto": nome,
                    "quantidade": float(qtd),
                })
            elif "set quantidade = quantidade - ?" in q:
                qtd, nome = p
                docs = list(db.collection("estoque").where("produto", "==", nome).limit(1).stream())
                if not docs:
                    raise ValueError(f"Produto não encontrado no estoque: {nome}")
                ref = docs[0].reference

                @firestore.transactional
                def baixar(transaction):
                    snap = ref.get(transaction=transaction)
                    atual = float(snap.get("quantidade") or 0)
                    # O saldo pode ficar negativo: representa produto vendido sob encomenda.
                    transaction.update(ref, {"quantidade": atual - float(qtd)})

                baixar(db.transaction())
            return ResultadoFirestore()

        if q.startswith("delete from estoque"):
            db.collection("estoque").document(str(p[0])).delete()
            return ResultadoFirestore()

        if q.startswith("insert into pedidos"):
            cliente_id, data_pedido, data_entrega, tipo, local, obs = p
            ref = db.collection("pedidos").document()
            ref.set({
                "cliente_id": str(cliente_id),
                "data_pedido": data_pedido,
                "data_entrega": data_entrega,
                "tipo_entrega": tipo,
                "local_entrega": local,
                "observacoes": obs,
                "status": "Pendente",
                "data_conclusao": None,
            })
            return ResultadoFirestore(lastrowid=ref.id)

        if q.startswith("insert into itens_pedido"):
            pedido_id, produto, quantidade = p
            ref = db.collection("pedidos").document(str(pedido_id)).collection("itens").document()
            ref.set({
                "produto": produto,
                "quantidade": float(quantidade),
                "unidade": "un",
                "separado": False,
            })
            return ResultadoFirestore(lastrowid=ref.id)

        if q.startswith("update itens_pedido"):
            separado, item_id, pedido_id = p
            ref = (
                db.collection("pedidos")
                .document(str(pedido_id))
                .collection("itens")
                .document(str(item_id))
            )
            ref.update({"separado": bool(separado)})
            return ResultadoFirestore()

        if q.startswith("delete from pedidos"):
            ref = db.collection("pedidos").document(str(p[0]))
            for item in ref.collection("itens").stream():
                item.reference.delete()
            ref.delete()
            return ResultadoFirestore()

        if q.startswith("update pedidos"):
            if "set status = 'entregue'" in q:
                data_conclusao, pedido_id = p
                ref = db.collection("pedidos").document(str(pedido_id))
                snap = ref.get()
                if snap.exists and snap.get("status") == "Pronto para entrega":
                    ref.update({"status": "Entregue", "data_conclusao": data_conclusao})
            else:
                status, data_conclusao, pedido_id = p
                db.collection("pedidos").document(str(pedido_id)).update({
                    "status": status,
                    "data_conclusao": data_conclusao,
                })
            return ResultadoFirestore()

        if q.startswith("insert into historico_etiquetas"):
            data_geracao, data_filtro, total, detalhes = p
            ref = db.collection("historico_etiquetas").document()
            ref.set({
                "data_geracao": data_geracao,
                "data_filtro_entrega": data_filtro,
                "total_pedidos": int(total),
                "detalhes_pedidos": detalhes,
            })
            return ResultadoFirestore(lastrowid=ref.id)

        if q.startswith("insert into retiradas"):
            nome, itens, obs, data_registro = p
            ref = db.collection("retiradas").document()
            ref.set({
                "nome_cliente": nome,
                "itens": itens,
                "observacoes": obs,
                "data_registro": data_registro,
                "status": "Pendente",
                "data_conclusao": None,
            })
            return ResultadoFirestore(lastrowid=ref.id)

        if q.startswith("update retiradas set nome_cliente"):
            nome, itens, obs, retirada_id = p
            db.collection("retiradas").document(str(retirada_id)).update({
                "nome_cliente": nome,
                "itens": itens,
                "observacoes": obs,
            })
            return ResultadoFirestore()

        if q.startswith("update retiradas set status"):
            data_conclusao, retirada_id = p
            db.collection("retiradas").document(str(retirada_id)).update({
                "status": "Concluída",
                "data_conclusao": data_conclusao,
            })
            return ResultadoFirestore()

        if q.startswith("delete from retiradas"):
            db.collection("retiradas").document(str(p[0])).delete()
            return ResultadoFirestore()

        raise NotImplementedError(f"Operação SQL ainda não convertida para Firestore: {sql}")


def conectar():
    return ConexaoFirestore()


def inicializar_banco():
    return None


inicializar_banco()


# --------------------------------------------------
# FUNÇÕES AUXILIARES - FIRESTORE & DATAS
# --------------------------------------------------

def proximo_sabado():
    hoje = date.today()
    dias = (5 - hoje.weekday()) % 7
    return hoje + timedelta(days=dias)


def formatar_data(valor):
    """Formata datas no padrão dd/mm/aaaa."""
    if not valor:
        return "-"
    texto = str(valor).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(texto, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return texto


def formatar_data_hora(valor):
    """Formata data e hora no padrão dd/mm/aaaa HH:MM:SS."""
    if not valor:
        return "-"
    texto = str(valor).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d-%m-%y %H:%M:%S",
        "%d/%m/%y %H:%M:%S",
    ):
        try:
            return datetime.strptime(texto, fmt).strftime("%d/%m/%Y %H:%M:%S")
        except ValueError:
            continue
    return texto


def _docs(colecao):
    return [{"id": d.id, **d.to_dict()} for d in db.collection(colecao).stream()]


def listar_clientes():
    return sorted(_docs("clientes"), key=lambda x: x.get("nome", "").lower())


def cliente_tem_pedidos(cliente_id):
    return bool(
        list(db.collection("pedidos").where("cliente_id", "==", str(cliente_id)).limit(1).stream())
    )


def listar_estoque():
    return sorted(_docs("estoque"), key=lambda x: x.get("produto", "").lower())


def cadastrar_item_estoque(produto, quantidade):
    nome = produto.strip()
    if list(db.collection("estoque").where("produto", "==", nome).limit(1).stream()):
        raise sqlite3.IntegrityError("Produto já cadastrado")
    db.collection("estoque").document().set({"produto": nome, "quantidade": float(quantidade)})


def atualizar_item_estoque(item_id, produto, quantidade):
    nome = produto.strip()
    existentes = list(db.collection("estoque").where("produto", "==", nome).stream())
    if any(d.id != str(item_id) for d in existentes):
        raise sqlite3.IntegrityError("Produto já cadastrado")
    db.collection("estoque").document(str(item_id)).update({
        "produto": nome,
        "quantidade": float(quantidade),
    })


def excluir_item_estoque(item_id):
    db.collection("estoque").document(str(item_id)).delete()


def baixar_estoque(produto, quantidade, con=None):
    ConexaoFirestore().execute(
        "UPDATE estoque SET quantidade = quantidade - ? WHERE produto = ?",
        (float(quantidade), produto),
    )


def obter_proximo_numero_pedido():
    """Gera o próximo número sequencial de pedido de forma atômica."""
    counter_ref = db.collection("counters").document("pedidos")

    @firestore.transactional
    def incrementar(transaction):
        snap = counter_ref.get(transaction=transaction)
        if snap.exists:
            atual = int(snap.get("ultimo") or 0)
        else:
            # Se o contador ainda não existe, descobre o maior número já usado
            atual = 0
            for p in db.collection("pedidos").stream():
                n = p.to_dict().get("numero")
                if n is not None:
                    try:
                        atual = max(atual, int(n))
                    except (TypeError, ValueError):
                        pass
        novo = atual + 1
        transaction.set(counter_ref, {"ultimo": novo}, merge=True)
        return novo

    return incrementar(db.transaction())


def numero_pedido(pedido):
    """Retorna o número amigável do pedido (sequencial). Fallback para ID antigo."""
    if isinstance(pedido, dict):
        n = pedido.get("numero")
        if n is not None:
            return n
        return pedido.get("id", "?")
    return pedido


def obter_proximo_numero_cliente():
    """Gera o próximo número sequencial de cliente de forma atômica."""
    counter_ref = db.collection("counters").document("clientes")

    @firestore.transactional
    def incrementar(transaction):
        snap = counter_ref.get(transaction=transaction)
        if snap.exists:
            atual = int(snap.get("ultimo") or 0)
        else:
            atual = 0
            for c in db.collection("clientes").stream():
                n = c.to_dict().get("numero")
                if n is not None:
                    try:
                        atual = max(atual, int(n))
                    except (TypeError, ValueError):
                        pass
        novo = atual + 1
        transaction.set(counter_ref, {"ultimo": novo}, merge=True)
        return novo

    return incrementar(db.transaction())


def numero_cliente(cliente):
    """Retorna o número amigável do cliente (sequencial). Fallback para ID antigo."""
    if isinstance(cliente, dict):
        n = cliente.get("numero")
        if n is not None:
            return n
        return cliente.get("id", "?")
    return cliente


def criar_pedido_com_estoque(cliente_id, data_pedido, data_entrega, local_entrega, observacoes, itens):
    """Cria o pedido e baixa todo o estoque em uma única transação Firestore."""
    produtos = {}
    for item in itens:
        nome = item["produto"].strip()
        encontrados = list(db.collection("estoque").where("produto", "==", nome).limit(1).stream())
        if not encontrados:
            raise ValueError(f"Produto não encontrado no estoque: {nome}")
        produtos[nome] = encontrados[0].reference

    # Gera o número sequencial ANTES da transação principal
    numero = obter_proximo_numero_pedido()

    pedido_ref = db.collection("pedidos").document()
    itens_refs = [(pedido_ref.collection("itens").document(), item) for item in itens]
    transaction = db.transaction()

    @firestore.transactional
    def executar(transaction):
        snapshots = {
            nome: next(transaction.get(ref))
            for nome, ref in produtos.items()
        }
        for item in itens:
            nome = item["produto"].strip()
            qtd = float(item["quantidade"])
            snap = snapshots[nome]
            if not snap.exists:
                raise ValueError(f"Produto não encontrado no estoque: {nome}")
            disponivel = float(snap.get("quantidade") or 0)
            if qtd <= 0:
                raise ValueError(f"A quantidade de {nome} deve ser maior que zero.")

        transaction.set(
            pedido_ref,
            {
                "numero": numero,  # número sequencial amigável
                "cliente_id": str(cliente_id),
                "data_pedido": str(data_pedido),
                "data_entrega": str(data_entrega),
                "tipo_entrega": "Entrega",
                "local_entrega": local_entrega.strip(),
                "observacoes": observacoes.strip(),
                "status": "Pendente",
                "data_conclusao": None,
            },
        )
        for item_ref, item in itens_refs:
            transaction.set(
                item_ref,
                {
                    "produto": item["produto"].strip(),
                    "quantidade": float(item["quantidade"]),
                    "unidade": "un",
                    "separado": False,
                },
            )
        for nome, ref in produtos.items():
            total = sum(float(i["quantidade"]) for i in itens if i["produto"].strip() == nome)
            atual = float(snapshots[nome].get("quantidade") or 0)
            transaction.update(ref, {"quantidade": atual - total})

    executar(transaction)
    return pedido_ref.id, numero  # retorna também o número sequencial


def listar_pedidos(data_filtro=None, status=None):
    clientes = {c["id"]: c for c in listar_clientes()}
    resultado = []
    for pedido in _docs("pedidos"):
        cliente = clientes.get(str(pedido.get("cliente_id")))
        if not cliente:
            continue
        if data_filtro and pedido.get("data_entrega") != str(data_filtro):
            continue
        if status and pedido.get("status") != status:
            continue
        itens = obter_itens(pedido["id"])
        pedido.update({
            "cliente": cliente.get("nome", ""),
            "telefone": cliente.get("telefone", ""),
            "total_itens": len(itens),
            "itens_separados": sum(bool(i.get("separado")) for i in itens),
        })
        resultado.append(pedido)
    return sorted(
        resultado,
        key=lambda x: (x.get("data_entrega", ""), x.get("cliente", "")),
        reverse=True,
    )


def obter_pedido(pedido_id):
    snap = db.collection("pedidos").document(str(pedido_id)).get()
    if not snap.exists:
        return None
    p = {"id": snap.id, **snap.to_dict()}
    cliente = db.collection("clientes").document(str(p.get("cliente_id"))).get()
    if cliente.exists:
        p["cliente"] = cliente.get("nome")
        p["telefone"] = cliente.get("telefone", "")
    return p


def obter_itens(pedido_id):
    return [
        {"id": d.id, **d.to_dict()}
        for d in db.collection("pedidos").document(str(pedido_id)).collection("itens").stream()
    ]


def atualizar_status_pedido(pedido_id):
    itens = obter_itens(pedido_id)
    if not itens:
        return
    pedido_ref = db.collection("pedidos").document(str(pedido_id))
    pedido = pedido_ref.get()
    if pedido.exists and pedido.get("status") != "Entregue":
        pedido_ref.update({
            "status": "Pronto para entrega" if all(i.get("separado") for i in itens) else "Pendente"
        })


def cancelar_pedido_e_devolver_estoque(pedido_id):
    """Cancela um pedido e devolve suas quantidades ao estoque uma única vez."""
    pedido_ref = db.collection("pedidos").document(str(pedido_id))
    itens = obter_itens(pedido_id)

    @firestore.transactional
    def executar(transaction):
        pedido_snap = pedido_ref.get(transaction=transaction)
        if not pedido_snap.exists:
            raise ValueError("Pedido não encontrado.")
        dados_pedido = pedido_snap.to_dict() or {}

        if dados_pedido.get("status") == "Entregue":
            raise ValueError("Um pedido já entregue não pode ser cancelado por esta opção.")

        if dados_pedido.get("status") == "Cancelado" or dados_pedido.get("estoque_devolvido", False):
            return False

        refs_por_produto = {}
        for item in itens:
            nome = str(item.get("produto", "")).strip()
            encontrados = list(db.collection("estoque").where("produto", "==", nome).limit(1).stream())
            if not encontrados:
                raise ValueError(f"O produto '{nome}' não existe mais no estoque. Recoloque-o no cadastro antes de cancelar.")
            refs_por_produto[nome] = encontrados[0].reference

        snapshots = {nome: next(transaction.get(ref)) for nome, ref in refs_por_produto.items()}
        totais = {}
        for item in itens:
            nome = str(item.get("produto", "")).strip()
            totais[nome] = totais.get(nome, 0.0) + float(item.get("quantidade") or 0)

        for nome, ref in refs_por_produto.items():
            snap = snapshots[nome]
            if not snap.exists:
                raise ValueError(f"Produto '{nome}' não encontrado no estoque.")
            atual = float(snap.get("quantidade") or 0)
            transaction.update(ref, {"quantidade": atual + totais[nome]})

        transaction.update(pedido_ref, {
            "status": "Cancelado",
            "estoque_devolvido": True,
            "data_cancelamento": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        return True

    return executar(db.transaction())


def salvar_historico_etiquetas(data_entrega, pedidos):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    resumo = []
    for p in pedidos:
        itens_txt = " + ".join(i["produto"] for i in obter_itens(p["id"]))
        resumo.append(f"Pedido #{numero_pedido(p)} - Para: {p['cliente']} | Itens: {itens_txt}")
    db.collection("historico_etiquetas").document().set({
        "data_geracao": agora,
        "data_filtro_entrega": str(data_entrega),
        "total_pedidos": len(pedidos),
        "detalhes_pedidos": "\n".join(resumo),
    })


def listar_historico_etiquetas():
    return sorted(_docs("historico_etiquetas"), key=lambda x: x.get("data_geracao", ""), reverse=True)


def excluir_historico_etiqueta(historico_id):
    db.collection("historico_etiquetas").document(str(historico_id)).delete()


def criar_retirada(nome_cliente, itens, observacoes):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.collection("retiradas").document().set({
        "nome_cliente": nome_cliente.strip(),
        "itens": itens.strip(),
        "observacoes": observacoes.strip(),
        "data_registro": agora,
        "status": "Pendente",
        "data_conclusao": None,
    })


def listar_retiradas(status=None):
    dados = _docs("retiradas")
    if status:
        dados = [r for r in dados if r.get("status") == status]
    return sorted(dados, key=lambda x: x.get("data_registro", ""), reverse=True)


def atualizar_retirada(retirada_id, nome_cliente, itens, observacoes):
    db.collection("retiradas").document(str(retirada_id)).update({
        "nome_cliente": nome_cliente.strip(),
        "itens": itens.strip(),
        "observacoes": observacoes.strip(),
    })


def concluir_retirada(retirada_id):
    db.collection("retiradas").document(str(retirada_id)).update({
        "status": "Concluída",
        "data_conclusao": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


def excluir_retirada(retirada_id):
    db.collection("retiradas").document(str(retirada_id)).delete()


# --------------------------------------------------
# GERADOR DE ETIQUETAS EM PDF
# --------------------------------------------------

def expandir_etiquetas_pedido(pedido):
    """Retorna uma lista de etiquetas (1 por unidade de item) com contador por item."""
    itens = obter_itens(pedido["id"])
    info_obs = pedido["local_entrega"]
    if pedido.get("observacoes"):
        info_obs += f" ({pedido['observacoes']})"

    unidades = []
    for item in itens:
        nome_produto = str(item.get("produto", "")).strip()
        qtd_bruta = float(item.get("quantidade") or 0)
        qtd_etiquetas = max(1, int(round(qtd_bruta))) if qtd_bruta > 0 else 0

        for idx_item in range(1, qtd_etiquetas + 1):
            unidades.append({
                "de": "Sônia Vende Tudo",
                "para": pedido["cliente"],
                "produto": nome_produto,
                "item_indice": idx_item,
                "item_total": qtd_etiquetas,
                "obs": info_obs,
            })

    return unidades


def gerar_pdf_etiquetas(pedidos):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=1 * cm,
        rightMargin=1 * cm,
        topMargin=1 * cm,
        bottomMargin=1 * cm,
    )

    styles = getSampleStyleSheet()

    style_de = ParagraphStyle(
        "EtiquetaDe",
        parent=styles["Normal"],
        fontSize=13,
        leading=16,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#222222"),
    )
    style_para = ParagraphStyle(
        "EtiquetaPara",
        parent=styles["Normal"],
        fontSize=15,
        leading=18,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#000000"),
    )
    style_itens = ParagraphStyle(
        "EtiquetaItens",
        parent=styles["Normal"],
        fontSize=10.5,
        leading=13,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#111111"),
    )
    style_obs = ParagraphStyle(
        "EtiquetaObs",
        parent=styles["Normal"],
        fontSize=9,
        leading=11,
        fontName="Helvetica-Oblique",
        textColor=colors.HexColor("#444444"),
    )

    cards = []

    for p in pedidos:
        etiquetas = expandir_etiquetas_pedido(p)
        for etq in etiquetas:
            texto_item = f"{etq['produto']} ({etq['item_indice']}/{etq['item_total']})"

            conteudo = [
                Paragraph(f"<b>De: {etq['de']}</b>", style_de),
                Paragraph(f"<b>Para: {etq['para']}</b>", style_para),
                Paragraph(texto_item, style_itens),
                Paragraph(f"<b>Obs:</b> {etq['obs']}", style_obs),
            ]

            t_card = Table([[c] for c in conteudo], colWidths=[9.2 * cm])
            t_card.setStyle(
                TableStyle([
                    ("BOX", (0, 0), (-1, -1), 1.2, colors.HexColor("#333333")),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFFFFF")),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ])
            )
            cards.append(t_card)

    rows = []
    for i in range(0, len(cards), 2):
        if i + 1 < len(cards):
            rows.append([cards[i], cards[i + 1]])
        else:
            rows.append([cards[i], ""])

    grid = Table(rows, colWidths=[9.5 * cm, 9.5 * cm])
    grid.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ])
    )

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
        "Histórico Geral",
    ],
)

st.sidebar.divider()
st.sidebar.caption("Sistema online com Firebase Firestore")


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
        if p.get("data_conclusao"):
            data_str = formatar_data(p["data_conclusao"])
            itens_ped = obter_itens(p["id"])
            qtd_itens = sum([i["quantidade"] for i in itens_ped])
            dados_grafico.append({
                "Data": data_str,
                "Itens Entregues": qtd_itens,
                "_ordem": str(p["data_conclusao"])[:10],
            })

    if dados_grafico:
        df_graf = pd.DataFrame(dados_grafico)
        df_agrupado = (
            df_graf.groupby(["_ordem", "Data"], as_index=False)["Itens Entregues"]
            .sum()
            .sort_values("_ordem")
        )
        df_agrupado.set_index("Data", inplace=True)
        st.line_chart(df_agrupado[["Itens Entregues"]])
    else:
        st.info("Ainda não há registros de pedidos entregues com data de conclusão para exibir no gráfico.")

    st.divider()
    st.subheader("Próximo sábado")
    data_sabado = proximo_sabado()
    st.write(formatar_data(data_sabado))

    pedidos_sabado = listar_pedidos(data_filtro=data_sabado)

    if pedidos_sabado:
        dados = []
        for p in pedidos_sabado:
            dados.append({
                "Pedido": numero_pedido(p),
                "Cliente": p["cliente"],
                "Local": p["local_entrega"],
                "Status": p["status"],
                "Itens separados": f'{p["itens_separados"]}/{p["total_itens"]}',
            })
        st.dataframe(pd.DataFrame(dados), width="stretch", hide_index=True)
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
                    resultado = con.execute(
                        """
                        INSERT INTO clientes (nome, telefone, observacoes)
                        VALUES (?, ?, ?)
                        """,
                        (nome.strip(), telefone.strip(), observacoes.strip()),
                    )
                # Busca o número gerado para mostrar na mensagem
                clientes_atual = listar_clientes()
                novo = next((c for c in clientes_atual if c["id"] == resultado.lastrowid), None)
                n = numero_cliente(novo) if novo else "?"
                st.success(f"Cliente #{n} cadastrado com sucesso!")
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
                    st.markdown(f"**#{numero_cliente(c)} — {c['nome']}**")
                    if c.get("telefone"):
                        st.caption(f"📞 {c['telefone']}")
                    if c.get("observacoes"):
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
                        novo_telefone = st.text_input("Telefone", value=c.get("telefone") or "")
                        novas_obs = st.text_area("Observações", value=c.get("observacoes") or "")

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
                                    con.execute(
                                        """
                                        UPDATE clientes
                                        SET nome = ?, telefone = ?, observacoes = ?
                                        WHERE id = ?
                                        """,
                                        (novo_nome.strip(), novo_telefone.strip(), novas_obs.strip(), c["id"]),
                                    )
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

    aba_cad, aba_ger, aba_encomendar = st.tabs([
        "➕ Cadastrar Item", "📋 Gerenciar Estoque", "🛒 Itens a encomendar"
    ])

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

    with aba_encomendar:
        st.subheader("🛒 Produtos que precisam ser encomendados")
        itens_encomenda = [e for e in listar_estoque() if float(e.get("quantidade") or 0) < 0]
        if not itens_encomenda:
            st.success("Nenhum produto com saldo negativo. Não há encomendas pendentes pelo estoque.")
        else:
            st.caption("O saldo negativo indica quantas unidades foram vendidas além do estoque disponível.")
            for item in itens_encomenda:
                falta = abs(float(item.get("quantidade") or 0))
                with st.container(border=True):
                    st.markdown(f"**{item['produto']}**")
                    st.markdown(f"Quantidade a encomendar: **{falta:g}**")


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
                f'#{numero_cliente(c)} — {c["nome"]}': c["id"]
                for c in clientes
            }

            with st.form("form_pedido"):
                cliente_escolhido = st.selectbox("Cliente", list(opcoes_clientes.keys()))

                data_pedido = st.date_input("Data do pedido", value=date.today(), format="DD/MM/YYYY")

                data_entrega = st.date_input(
                    "Data prevista da entrega",
                    value=proximo_sabado(),
                    format="DD/MM/YYYY",
                )

                local_entrega = st.text_input(
                    "Local de entrega / Estação / Endereço",
                    placeholder="Ex.: Estação Vila das Belezas ou Endereço",
                )

                observacoes = st.text_area("Observações (ex.: Entregar para fulano)")

                st.markdown("### Selecione os produtos do estoque:")

                opcoes_produtos_estoque = [
                    f"{e['produto']} (Disp: {e['quantidade']:g})" for e in estoque_atual
                ]

                itens_df = pd.DataFrame([
                    {"Produto": opcoes_produtos_estoque[0], "Quantidade": 1.0}
                ])

                itens_editados = st.data_editor(
                    itens_df,
                    num_rows="dynamic",
                    hide_index=True,
                    column_config={
                        "Produto": st.column_config.SelectboxColumn(
                            "Produto do Estoque",
                            options=opcoes_produtos_estoque,
                            required=True,
                        ),
                        "Quantidade": st.column_config.NumberColumn(
                            "Quantidade", min_value=0.01, step=1.0, required=True
                        ),
                    },
                    key="tabela_itens_novo_pedido",
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
                        itens_para_salvar = []
                        for _, item in itens_validos.iterrows():
                            prod_str = str(item["Produto"])
                            nome_prod_limpo = prod_str.split(" (Disp:")[0].strip()
                            qtd_pedida = float(item["Quantidade"])
                            itens_para_salvar.append({
                                "produto": nome_prod_limpo,
                                "quantidade": qtd_pedida,
                            })

                        try:
                            pedido_id, numero = criar_pedido_com_estoque(
                                cliente_id=cliente_id,
                                data_pedido=data_pedido,
                                data_entrega=data_entrega,
                                local_entrega=local_entrega,
                                observacoes=observacoes,
                                itens=itens_para_salvar,
                            )
                            st.success(f"Pedido #{numero} cadastrado e estoque atualizado com sucesso!")
                        except Exception as e:
                            st.error(f"Erro ao salvar pedido: {e}")

    with aba_gerenciar:
        pedidos = [p for p in listar_pedidos() if p.get("status") != "Cancelado"]

        if not pedidos:
            st.info("Nenhum pedido cadastrado.")
        else:
            col_filtro1, col_filtro2 = st.columns(2)
            with col_filtro1:
                filtro_status = st.selectbox(
                    "Filtrar por status",
                    ["Todos", "Pendente", "Pronto para entrega", "Entregue"],
                    key="filtro_status_gerenciar",
                )
            with col_filtro2:
                busca = st.text_input("Buscar por cliente ou nº do pedido", key="busca_gerenciar_pedido")

            pedidos_filtrados = pedidos
            if filtro_status != "Todos":
                pedidos_filtrados = [p for p in pedidos_filtrados if p["status"] == filtro_status]

            if busca.strip():
                busca_lower = busca.strip().lower()
                pedidos_filtrados = [
                    p
                    for p in pedidos_filtrados
                    if busca_lower in p["cliente"].lower()
                    or busca_lower in str(numero_pedido(p))
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
                                f"**Pedido #{numero_pedido(p)}** — {p['cliente']}  \n"
                                f"📅 Pedido: {formatar_data(p.get('data_pedido'))} | "
                                f"Entrega: {formatar_data(p['data_entrega'])} | "
                                f"Status: **{p['status']}**  \n"
                                f"📍 Local: {p['local_entrega']}  \n"
                                f"Itens: {p['itens_separados']}/{p['total_itens']} separados"
                            )

                        # Permite consultar os produtos e quantidades de cada pedido.
                        with st.expander(f"🛍️ Visualizar itens do pedido #{numero_pedido(p)}"):
                            itens_pedido = obter_itens(p["id"])
                            if not itens_pedido:
                                st.info("Este pedido não possui itens cadastrados.")
                            else:
                                for item in itens_pedido:
                                    nome_item = item.get("produto", "Produto sem nome")
                                    quantidade = float(item.get("quantidade") or 0)
                                    separado = bool(item.get("separado"))
                                    status_item = "✅ Separado" if separado else "⏳ Pendente"
                                    st.markdown(
                                        f"- **{nome_item}** — Quantidade: **{quantidade:g}** "
                                        f"— {status_item}"
                                    )

                        with col_acoes:
                            if p.get("status") != "Entregue" and p.get("status") != "Cancelado":
                                if st.button("🚫 Cancelar pedido", key=f"cancelar_ped_{p['id']}"):
                                    st.session_state[f"confirmar_cancelamento_ped_{p['id']}"] = True

                        if st.session_state.get(f"confirmar_cancelamento_ped_{p['id']}", False):
                            st.warning(
                                f"Cancelar o **Pedido #{numero_pedido(p)}** de **{p['cliente']}**? "
                                "As quantidades dos produtos serão devolvidas ao estoque."
                            )
                            col_sim, col_nao = st.columns(2)
                            with col_sim:
                                if st.button("Sim, cancelar pedido", key=f"confirma_cancel_ped_{p['id']}", type="primary"):
                                    try:
                                        devolvido = cancelar_pedido_e_devolver_estoque(p["id"])
                                        st.session_state[f"confirmar_cancelamento_ped_{p['id']}"] = False
                                        if devolvido:
                                            st.success(f"Pedido #{numero_pedido(p)} cancelado e estoque devolvido!")
                                        else:
                                            st.info("Este pedido já estava cancelado; o estoque não foi alterado novamente.")
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Não foi possível cancelar o pedido: {e}")
                            with col_nao:
                                if st.button("Manter pedido", key=f"manter_ped_{p['id']}"):
                                    st.session_state[f"confirmar_cancelamento_ped_{p['id']}"] = False
                                    st.rerun()


# --------------------------------------------------
# CHECKLIST (SEPARAÇÃO & ENTREGAS DE SÁBADO)
# --------------------------------------------------

elif pagina == "Checklist":
    st.title("📋 Central de Checklists")

    aba_separacao, aba_entregas = st.tabs(["✅ Separação", "🚚 Entregas de sábado"])

    with aba_separacao:
        st.subheader("Checklist de separação")
        st.write(
            "Escolha a data de entrega, confira todos os pedidos e marque os produtos "
            "conforme forem colocados nas sacolas."
        )

        data_separacao = st.date_input(
            "Filtrar pela data de entrega",
            value=proximo_sabado(),
            key="data_checklist_separacao",
            format="DD/MM/YYYY",
        )

        pedidos_separacao = [
            p for p in listar_pedidos(data_filtro=data_separacao)
            if p["status"] not in ("Entregue", "Cancelado")
        ]

        if not pedidos_separacao:
            st.info("Não há pedidos pendentes de separação para esta data.")
        else:
            st.success(
                f"{len(pedidos_separacao)} pedido(s) encontrado(s) para "
                f"{formatar_data(data_separacao)}."
            )

            marcacoes = {}
            pedidos_ids = []

            with st.form("form_separacao_todos"):
                for p in pedidos_separacao:
                    pedido_id = p["id"]
                    pedidos_ids.append(pedido_id)

                    st.markdown("---")
                    st.subheader(
                        f'Pedido #{numero_pedido(p)} — {p["cliente"]}'
                    )
                    st.write(f'**Entrega:** {formatar_data(p["data_entrega"])}')
                    st.write(f'**Local:** {p["local_entrega"]}')
                    st.write(
                        f'**Separação atual:** '
                        f'{p["itens_separados"]}/{p["total_itens"]} itens'
                    )

                    itens = obter_itens(pedido_id)
                    if not itens:
                        st.caption("Este pedido não possui itens cadastrados.")

                    for item in itens:
                        marcacoes[item["id"]] = st.checkbox(
                            f'{item["produto"]} — {item["quantidade"]:g}',
                            value=bool(item.get("separado")),
                            key=f'sep_todos_{pedido_id}_{item["id"]}',
                        )

                st.markdown("---")
                salvar_todos = st.form_submit_button(
                    "💾 Salvar todos os checklists",
                    type="primary",
                    use_container_width=True,
                )

                if salvar_todos:
                    try:
                        with conectar() as con:
                            for pedido in pedidos_separacao:
                                pedido_id = pedido["id"]
                                for item in obter_itens(pedido_id):
                                    separado = marcacoes.get(item["id"], False)
                                    con.execute(
                                        """
                                        UPDATE itens_pedido
                                        SET separado = ?
                                        WHERE id = ? AND pedido_id = ?
                                        """,
                                        (int(separado), item["id"], pedido_id),
                                    )

                        for pedido_id in pedidos_ids:
                            atualizar_status_pedido(pedido_id)

                        st.success("Todos os checklists foram salvos com sucesso!")
                        st.rerun()

                    except Exception as e:
                        st.error(f"Erro ao salvar os checklists: {e}")

    with aba_entregas:
        st.subheader("Checklist de entregas")

        data_entrega = st.date_input(
            "Data das entregas",
            value=proximo_sabado(),
            key="data_checklist_entregas",
            format="DD/MM/YYYY",
        )

        pedidos_sab = listar_pedidos(data_filtro=data_entrega)

        if not pedidos_sab:
            st.info("Nenhum pedido encontrado para essa data.")
        else:
            for p in pedidos_sab:
                with st.container(border=True):
                    st.subheader(f'Pedido #{numero_pedido(p)} — {p["cliente"]}')
                    st.write(f'**Local:** {p["local_entrega"]}')
                    st.write(f'**Separação:** {p["itens_separados"]}/{p["total_itens"]} itens')

                    if p["status"] == "Entregue":
                        st.success(f'Entregue em {formatar_data(p.get("data_conclusao"))}')
                    elif p["status"] != "Pronto para entrega":
                        st.warning("Pedido ainda não está totalmente separado.")
                    else:
                        st.info("Pedido pronto para entrega.")

                    with st.expander("Conferir produtos"):
                        for item in obter_itens(p["id"]):
                            marca = "✅" if item.get("separado") else "⬜"
                            st.write(f'{marca} {item["produto"]} — {item["quantidade"]:g}')

                    if p["status"] == "Pronto para entrega":
                        if st.button("Confirmar pedido entregue", key=f'entregue_{p["id"]}', type="primary"):
                            agora = date.today().isoformat()
                            with conectar() as con:
                                con.execute(
                                    """
                                    UPDATE pedidos
                                    SET status = 'Entregue', data_conclusao = ?
                                    WHERE id = ? AND status = 'Pronto para entrega'
                                    """,
                                    (agora, p["id"]),
                                )
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
        "📚 Histórico de Retiradas Concluídas",
    ])

    with aba_nova:
        st.subheader("Nova Retirada Presencial")
        with st.form("form_nova_retirada", clear_on_submit=True):
            nome_cli = st.text_input("Nome do Cliente *", placeholder="Ex.: Maria Souza")
            itens_ret = st.text_area(
                "Item / Itens a retirar *",
                placeholder="Ex.: Panela de Pressão Electrolux + Conjunto de Copos",
            )
            obs_ret = st.text_input(
                "Observações (OBS)",
                placeholder="Ex.: Já pago via PIX / Retira no sábado à tarde",
            )

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
                r
                for r in retiradas_pendentes
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
                        if r.get("observacoes"):
                            st.caption(f"📝 **OBS:** {r['observacoes']}")
                        st.caption(f"📅 Cadastrado em: {formatar_data_hora(r.get('data_registro'))}")

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
                            novas_obs_r = st.text_input("OBS", value=r.get("observacoes") or "")

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
                    "Observações": r.get("observacoes") or "-",
                    "Data do Cadastro": formatar_data_hora(r.get("data_registro")),
                    "Data da Retirada": formatar_data_hora(r.get("data_conclusao")),
                })

            df_hist = pd.DataFrame(dados_hist)
            st.dataframe(df_hist, width="stretch", hide_index=True)

            csv_ret = df_hist.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 Exportar Histórico de Retiradas (CSV)",
                data=csv_ret,
                file_name="historico_retiradas.csv",
                mime="text/csv",
            )


# --------------------------------------------------
# ETIQUETAS E HISTÓRICO
# --------------------------------------------------

elif pagina == "Etiquetas e Histórico":
    st.title("🏷️ Etiquetas de Entrega")

    aba_gerar, aba_historico = st.tabs(["📄 Gerar Etiquetas Atuais", "📚 Histórico de Etiquetas"])

    with aba_gerar:
        data_entrega = st.date_input(
            "Selecione a data de entrega",
            value=proximo_sabado(),
            key="dt_etiq",
            format="DD/MM/YYYY",
        )
        pedidos = listar_pedidos(data_filtro=data_entrega)

        if not pedidos:
            st.info("Nenhum pedido encontrado para esta data.")
        else:
            todas_etiquetas = []
            for p in pedidos:
                todas_etiquetas.extend(expandir_etiquetas_pedido(p))

            st.success(
                f"Encontrados {len(pedidos)} pedido(s) -> **{len(todas_etiquetas)} etiqueta(s)** para a entrega de {formatar_data(data_entrega)}."
            )

            st.subheader("Pré-visualização do formato das etiquetas:")
            for etq in todas_etiquetas:
                texto_item = f"{etq['produto']} ({etq['item_indice']}/{etq['item_total']})"

                st.info(
                    f"**De: {etq['de']}**  \n"
                    f"**Para: {etq['para']}**  \n"
                    f"**{texto_item}**  \n"
                    f"*Obs:* {etq['obs']}"
                )

            pdf_bytes = gerar_pdf_etiquetas(pedidos)

            col1, col2 = st.columns(2)
            with col1:
                if st.download_button(
                    label="📄 Baixar PDF com Todas as Etiquetas (Folha A4)",
                    data=pdf_bytes,
                    file_name=f"etiquetas_{data_entrega.strftime('%d-%m-%Y')}.pdf",
                    mime="application/pdf",
                    type="primary",
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
                titulo = (
                    f"Lote de {formatar_data(h.get('data_filtro_entrega'))} - "
                    f"Gerado em: {formatar_data_hora(h.get('data_geracao'))} "
                    f"({h.get('total_pedidos', 0)} pedidos)"
                )
                with st.expander(titulo):
                    st.text(h.get("detalhes_pedidos", ""))

                    if st.button("🗑️ Excluir este registro", key=f"del_hist_etiq_{h['id']}"):
                        st.session_state[f"confirm_del_hist_etiq_{h['id']}"] = True

                    if st.session_state.get(f"confirm_del_hist_etiq_{h['id']}", False):
                        st.warning("Tem certeza que deseja apagar este registro do histórico de etiquetas?")
                        col_sim, col_nao = st.columns(2)
                        with col_sim:
                            if st.button("Sim, apagar", key=f"conf_del_hist_etiq_{h['id']}", type="primary"):
                                excluir_historico_etiqueta(h["id"])
                                st.session_state[f"confirm_del_hist_etiq_{h['id']}"] = False
                                st.success("Registro removido do histórico!")
                                st.rerun()
                        with col_nao:
                            if st.button("Cancelar", key=f"canc_del_hist_etiq_{h['id']}"):
                                st.session_state[f"confirm_del_hist_etiq_{h['id']}"] = False
                                st.rerun()


# --------------------------------------------------
# HISTÓRICO GERAL
# --------------------------------------------------

elif pagina == "Histórico Geral":
    st.title("📚 Histórico geral de pedidos")

    col1, col2 = st.columns(2)
    with col1:
        data_inicio = st.date_input(
            "Data inicial",
            value=date.today() - timedelta(days=30),
            key="historico_inicio",
            format="DD/MM/YYYY",
        )
    with col2:
        data_fim = st.date_input(
            "Data final",
            value=date.today(),
            key="historico_fim",
            format="DD/MM/YYYY",
        )

    status_filtro = st.selectbox(
        "Status do pedido",
        ["Todos", "Pendente", "Pronto para entrega", "Entregue"],
    )

    todos = listar_pedidos()
    filtrados = [
        p
        for p in todos
        if data_inicio.isoformat() <= str(p.get("data_entrega", "")) <= data_fim.isoformat()
        and (status_filtro == "Todos" or p.get("status") == status_filtro)
    ]

    if filtrados:
        dados = []
        for p in filtrados:
            dados.append({
                "Pedido": numero_pedido(p),
                "Cliente": p["cliente"],
                "Telefone": p.get("telefone", ""),
                "Data do pedido": formatar_data(p.get("data_pedido")),
                "Data prevista": formatar_data(p.get("data_entrega")),
                "Local": p["local_entrega"],
                "Status": p["status"],
                "Data de conclusão": formatar_data(p.get("data_conclusao")),
            })

        historico_df = pd.DataFrame(dados)
        st.dataframe(historico_df, width="stretch", hide_index=True)
    else:
        st.info("Nenhum pedido encontrado nesse período.")