"""
Robô de busca de licitações - Grupo Concrevia
=================================================
Busca diariamente, na API pública do PNCP (Portal Nacional de Contratações
Públicas), licitações publicadas nos municípios de interesse que contenham
as palavras-chave do ramo de atuação da empresa, e envia um e-mail com o
resumo formatado.

Roda de graça via GitHub Actions (agendado) ou manualmente (python buscar_licitacoes.py).
"""

import os
import re
import smtplib
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Sessão HTTP com nova tentativa automática (o servidor do PNCP às vezes demora
# ou falha momentaneamente; tentamos até 4 vezes antes de desistir)
SESSAO = requests.Session()
_retry = Retry(
    total=2,
    backoff_factor=3,  # espera 3s, depois 6s entre tentativas
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
SESSAO.mount("https://", HTTPAdapter(max_retries=_retry))

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO — ajuste aqui sem precisar mexer no resto do código
# ---------------------------------------------------------------------------

# Municípios de interesse (nome de exibição -> código IBGE de 7 dígitos)
MUNICIPIOS = {
    "Batayporã": "5002001",
    "Taquarussu": "5007976",
    "Angélica": "5000856",
    "Ivinhema": "5004700",
    "Anaurilândia": "5000807",
    "Novo Horizonte do Sul": "5006259",
    "Deodápolis": "5003454",
    "Bataguassu": "5001904",
    "Santa Rita do Pardo": "5007554",
    "Naviraí": "5005707",
    "Juti": "5005152",
    "Itaquiraí": "5004601",
    "Brasilândia": "5002308",
}

# Órgãos monitorados diretamente por CNPJ, independente do município onde a
# licitação aparece registrada no PNCP — importante para órgãos estaduais como
# a AGESUL, que é sediada em Campo Grande mas executa obras em todo o estado
# (por filtro de município, essas licitações passariam batido).
ORGAOS = {
    "AGESUL (Agência Estadual de Gestão de Empreendimentos)": "15457856000156",
}

# Modalidades de contratação a consultar (códigos oficiais do PNCP)
# 4 = Concorrência Eletrônica | 5 = Concorrência Presencial
# 6 = Pregão Eletrônico       | 8 = Dispensa de Licitação
MODALIDADES = [4, 5, 6, 8]

# Palavras-chave (o filtro ignora maiúsculas/minúsculas e acentos)
PALAVRAS_CHAVE = [
    "pavimentacao asfaltica",
    "recapeamento asfaltico",
    "cbuq",
    "concreto betuminoso usinado a quente",
    "tapa buraco",
    "recomposicao de pavimento",
    "pavimentacao rigida",
    "fresagem",
    "pintura de ligacao",
    "imprimacao",
    "duplicacao de rodovia",
    "restauracao rodoviaria",
    "terraplanagem",
    "obras de arte especiais",
    "sinalizacao viaria",
    "acostamento",
    "rede de drenagem",
    "drenagem pluvial",
    "galeria de aguas pluviais",
    "microdrenagem",
    "macrodrenagem",
    "bueiro",
    "boca de lobo",
    "rede de esgoto",
    "contencao de erosao",
    "tubo de concreto",
    "tubos de concreto",
    "paver",
    "bloquete intertravado",
    "concreto usinado",
    "meio fio",
    "guias e sarjetas",
    "estrutura pre moldada",
    "concreto armado",
    "execucao de obras",
    "servicos de engenharia",
    "infraestrutura urbana",
    "mobilidade urbana",
]

# Quantos dias para trás buscar (2 dá uma margem de segurança para feriados/atrasos).
# Pode ser sobrescrito pontualmente (ex: busca retroativa) via variável de ambiente
# DIAS_RETROATIVOS_OVERRIDE, sem precisar mudar esse valor padrão.
DIAS_RETROATIVOS = int(os.environ.get("DIAS_RETROATIVOS_OVERRIDE") or 2)

# E-mail
REMETENTE = os.environ["EMAIL_REMETENTE"]          # grupoconcrevia@gmail.com
SENHA_APP = os.environ["EMAIL_SENHA_APP"]           # senha de app (via GitHub Secret)
DESTINATARIOS = os.environ["EMAIL_DESTINATARIOS"]   # pode ter vários separados por vírgula

# ---------------------------------------------------------------------------
# FUNÇÕES
# ---------------------------------------------------------------------------

BASE_URL = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"


def normalizar(texto: str) -> str:
    """Remove acentos e baixa a caixa, para comparação de texto mais robusta."""
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", texto).encode("ASCII", "ignore").decode()
    return texto.lower()


PALAVRAS_NORMALIZADAS = [normalizar(p) for p in PALAVRAS_CHAVE]

# Nomes dos municípios normalizados (sem acento), usados para filtrar resultados
# de órgãos estaduais como a AGESUL, que licitam obras no estado inteiro — sem
# esse filtro extra, uma obra em Dourados (fora da nossa lista) bateria a mesma
# palavra-chave que uma em Ivinhema (dentro da lista).
MUNICIPIOS_NORMALIZADOS = [normalizar(nome) for nome in MUNICIPIOS]


def bate_palavra_chave(objeto: str) -> list:
    """Retorna a lista de palavras-chave encontradas no texto do objeto da licitação."""
    objeto_norm = normalizar(objeto)
    return [PALAVRAS_CHAVE[i] for i, p in enumerate(PALAVRAS_NORMALIZADAS) if p in objeto_norm]


def menciona_municipio_monitorado(objeto: str) -> bool:
    """Verifica se o texto do objeto menciona algum dos 13 municípios monitorados.
    Usado para filtrar resultados de órgãos estaduais (ex: AGESUL), que cobrem o
    estado inteiro e não só os municípios de interesse."""
    objeto_norm = normalizar(objeto)
    return any(nome in objeto_norm for nome in MUNICIPIOS_NORMALIZADOS)


def buscar_licitacoes_municipio(codigo_ibge: str, data_inicial: str, data_final: str) -> list:
    """Consulta a API do PNCP para um município e todas as modalidades configuradas."""
    encontradas = []
    for modalidade in MODALIDADES:
        pagina = 1
        while True:
            params = {
                "dataInicial": data_inicial,
                "dataFinal": data_final,
                "codigoModalidadeContratacao": modalidade,
                "codigoMunicipioIbge": codigo_ibge,
                "pagina": pagina,
                "tamanhoPagina": 50,
            }
            try:
                resp = SESSAO.get(BASE_URL, params=params, timeout=30)
            except requests.exceptions.RequestException as e:
                print(f"  aviso: falha ao consultar município {codigo_ibge}, modalidade {modalidade}, página {pagina}: {e}")
                break
            if resp.status_code != 200:
                break
            dados = resp.json()
            registros = dados.get("data", [])
            if not registros:
                break
            encontradas.extend(registros)
            total_paginas = dados.get("totalPaginas", 1)
            if pagina >= total_paginas:
                break
            pagina += 1
    return encontradas


def buscar_licitacoes_orgao(cnpj: str, data_inicial: str, data_final: str) -> list:
    """Consulta a API do PNCP para um órgão específico (por CNPJ), em todas as modalidades.
    Usado para órgãos estaduais como a AGESUL, cujas licitações aparecem no PNCP
    associadas ao município-sede do órgão, não ao município da obra."""
    encontradas = []
    for modalidade in MODALIDADES:
        pagina = 1
        while True:
            params = {
                "dataInicial": data_inicial,
                "dataFinal": data_final,
                "codigoModalidadeContratacao": modalidade,
                "cnpj": cnpj,
                "pagina": pagina,
                "tamanhoPagina": 50,
            }
            try:
                resp = SESSAO.get(BASE_URL, params=params, timeout=30)
            except requests.exceptions.RequestException as e:
                print(f"  aviso: falha ao consultar órgão {cnpj}, modalidade {modalidade}, página {pagina}: {e}")
                break
            if resp.status_code != 200:
                break
            dados = resp.json()
            registros = dados.get("data", [])
            if not registros:
                break
            encontradas.extend(registros)
            total_paginas = dados.get("totalPaginas", 1)
            if pagina >= total_paginas:
                break
            pagina += 1
    return encontradas


def montar_link_edital(item: dict) -> str:
    cnpj = item.get("orgaoEntidade", {}).get("cnpj", "")
    ano = item.get("anoCompra", "")
    seq = item.get("sequencialCompra", "")
    if cnpj and ano and seq:
        return f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}"
    return item.get("linkSistemaOrigem", "") or ""


def formatar_data_iso_br(data_iso: str) -> str:
    """Converte datas ISO da API (ex: 2026-09-22 ou 2026-09-22T00:00:00) para DD/MM/AAAA."""
    if not data_iso:
        return "não informado"
    try:
        return datetime.fromisoformat(data_iso.split("T")[0]).strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return data_iso


def formatar_data_br(data_yyyymmdd: str) -> str:
    """Converte AAAAMMDD (formato da API) para DD/MM/AAAA (formato brasileiro)."""
    try:
        return datetime.strptime(data_yyyymmdd, "%Y%m%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return data_yyyymmdd


def montar_bloco_item(item: dict, palavras: list) -> str:
    """Monta o HTML de um único card de licitação (usado tanto para municípios quanto órgãos)."""
    objeto = item.get("objetoCompra", "sem descrição")
    orgao = item.get("orgaoEntidade", {}).get("razaoSocial", "órgão não informado")
    valor = item.get("valorTotalEstimado")
    valor_fmt = f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if valor else "não informado"
    encerramento = formatar_data_iso_br(item.get("dataEncerramentoProposta"))
    link = montar_link_edital(item)
    tags = " ".join(f'<span style="background:#eef4ff;color:#2952a3;padding:2px 8px;border-radius:10px;font-size:12px;margin-right:4px;">{p}</span>' for p in palavras)

    return f"""
    <div style="border:1px solid #e2e2e2;border-radius:8px;padding:14px;margin-bottom:10px;">
      <div style="font-weight:600;font-size:15px;color:#1a1a1a;margin-bottom:4px;">{orgao}</div>
      <div style="color:#444;margin-bottom:8px;">{objeto}</div>
      <div style="margin-bottom:8px;">{tags}</div>
      <div style="font-size:13px;color:#666;">
        Valor estimado: <b>{valor_fmt}</b> &nbsp;|&nbsp;
        Encerramento das propostas: <b>{encerramento}</b>
      </div>
      {f'<div style="margin-top:8px;"><a href="{link}" style="color:#2952a3;">Abrir edital &rarr;</a></div>' if link else ''}
    </div>
    """


def montar_html(resultados_por_municipio: dict, resultados_por_orgao: dict, data_inicial: str, data_final: str) -> str:
    data_inicial_br = formatar_data_br(data_inicial)
    data_final_br = formatar_data_br(data_final)
    hoje_fmt = datetime.now().strftime("%d/%m/%Y")
    total = sum(len(v) for v in resultados_por_municipio.values()) + sum(len(v) for v in resultados_por_orgao.values())

    if total == 0:
        corpo = "<p>Nenhuma licitação nova encontrada com os critérios configurados nesse período.</p>"
    else:
        blocos = []

        # Seção de órgãos monitorados por CNPJ (ex: AGESUL) — aparece primeiro
        for nome_orgao, itens in resultados_por_orgao.items():
            if not itens:
                continue
            linhas = "".join(montar_bloco_item(item, palavras) for item, palavras in itens)
            blocos.append(f"""
            <h3 style="margin-top:24px;margin-bottom:8px;color:#1a1a1a;">🏛️ {nome_orgao} ({len(itens)})</h3>
            {linhas}
            """)

        # Seção por município
        for municipio, itens in resultados_por_municipio.items():
            if not itens:
                continue
            linhas = "".join(montar_bloco_item(item, palavras) for item, palavras in itens)
            blocos.append(f"""
            <h3 style="margin-top:24px;margin-bottom:8px;color:#1a1a1a;">📍 {municipio} ({len(itens)})</h3>
            {linhas}
            """)
        corpo = "".join(blocos)

    return f"""
    <html>
    <body style="font-family:Arial,Helvetica,sans-serif;background:#f7f7f8;padding:0;margin:0;">
      <div style="max-width:640px;margin:0 auto;padding:24px;">
        <div style="background:#1a1a1a;color:#fff;padding:20px 24px;border-radius:10px 10px 0 0;">
          <h2 style="margin:0;">Resumo de Licitações — Grupo Concrevia</h2>
          <div style="opacity:0.8;font-size:13px;">Publicações de {data_inicial_br} a {data_final_br} · Enviado em {hoje_fmt}</div>
        </div>
        <div style="background:#fff;padding:24px;border-radius:0 0 10px 10px;border:1px solid #eee;border-top:none;">
          <p style="color:#333;">Bom dia! Segue o resumo automático de licitações encontradas no PNCP para os municípios monitorados.</p>
          {corpo}
          <p style="color:#999;font-size:12px;margin-top:24px;">
            Robô automático · Fonte: Portal Nacional de Contratações Públicas (PNCP)
          </p>
        </div>
      </div>
    </body>
    </html>
    """


def montar_assunto(total: int, data_final_br: str) -> str:
    if total == 0:
        texto = "Nenhuma oportunidade encontrada"
    elif total == 1:
        texto = "1 oportunidade encontrada"
    else:
        texto = f"{total} oportunidades encontradas"
    return f"[Licitações] {texto} — {data_final_br}"


def enviar_email(html: str, total: int, data_final: str):
    data_final_br = formatar_data_br(data_final)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = montar_assunto(total, data_final_br)
    msg["From"] = REMETENTE
    msg["To"] = DESTINATARIOS

    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as servidor:
        servidor.login(REMETENTE, SENHA_APP)
        servidor.sendmail(REMETENTE, DESTINATARIOS.split(","), msg.as_string())


def processar_municipio(nome_municipio: str, codigo_ibge: str, data_inicial: str, data_final: str):
    """Busca e filtra as licitações de um único município. Roda em paralelo, uma thread por município."""
    registros = buscar_licitacoes_municipio(codigo_ibge, data_inicial, data_final)
    encontrados = []
    for item in registros:
        palavras = bate_palavra_chave(item.get("objetoCompra", ""))
        if palavras:
            encontrados.append((item, palavras))
    print(f"{nome_municipio}: {len(registros)} publicações analisadas, {len(encontrados)} relevantes")
    return nome_municipio, encontrados


def processar_orgao(nome_orgao: str, cnpj: str, data_inicial: str, data_final: str):
    """Busca e filtra as licitações de um órgão específico (por CNPJ). Roda em paralelo.
    Como esses órgãos cobrem o estado inteiro, exige também que o objeto mencione
    um dos 13 municípios monitorados — senão pegaríamos obras de qualquer lugar de MS."""
    registros = buscar_licitacoes_orgao(cnpj, data_inicial, data_final)
    encontrados = []
    for item in registros:
        objeto = item.get("objetoCompra", "")
        palavras = bate_palavra_chave(objeto)
        if palavras and menciona_municipio_monitorado(objeto):
            encontrados.append((item, palavras))
    print(f"{nome_orgao}: {len(registros)} publicações analisadas, {len(encontrados)} relevantes (após filtro de município)")
    return nome_orgao, encontrados


def main():
    hoje = datetime.now()
    data_inicial = (hoje - timedelta(days=DIAS_RETROATIVOS)).strftime("%Y%m%d")
    data_final = hoje.strftime("%Y%m%d")

    resultados_por_municipio = {}
    resultados_por_orgao = {}
    total = 0

    somente_orgaos = os.environ.get("SOMENTE_ORGAOS", "").lower() == "true"

    # Consulta todos os municípios E os órgãos por CNPJ em paralelo (até 8 ao mesmo
    # tempo), em vez de um por um — isso reduz bastante o tempo total quando o PNCP
    # está lento. SOMENTE_ORGAOS=true pula os municípios (útil para uma busca
    # retroativa pontual de um órgão específico, sem reenviar municípios já cobertos).
    with ThreadPoolExecutor(max_workers=8) as executor:
        futuros = {}
        if not somente_orgaos:
            futuros.update({
                executor.submit(processar_municipio, nome, codigo, data_inicial, data_final): ("municipio", nome)
                for nome, codigo in MUNICIPIOS.items()
            })
        futuros.update({
            executor.submit(processar_orgao, nome, cnpj, data_inicial, data_final): ("orgao", nome)
            for nome, cnpj in ORGAOS.items()
        })
        for futuro in as_completed(futuros):
            tipo, nome_chave = futuros[futuro]
            try:
                nome, encontrados = futuro.result()
                if tipo == "municipio":
                    resultados_por_municipio[nome] = encontrados
                else:
                    resultados_por_orgao[nome] = encontrados
                total += len(encontrados)
            except Exception as e:
                print(f"  aviso: falha ao processar {nome_chave}: {e}")
                if tipo == "municipio":
                    resultados_por_municipio[nome_chave] = []
                else:
                    resultados_por_orgao[nome_chave] = []

    # Reordena os dicionários na ordem original (o paralelismo embaralha a ordem de chegada)
    resultados_por_municipio = {nome: resultados_por_municipio.get(nome, []) for nome in MUNICIPIOS}
    resultados_por_orgao = {nome: resultados_por_orgao.get(nome, []) for nome in ORGAOS}

    html = montar_html(resultados_por_municipio, resultados_por_orgao, data_inicial, data_final)
    enviar_email(html, total, data_final)
    print(f"E-mail enviado. Total de oportunidades relevantes: {total}")


if __name__ == "__main__":
    main()
