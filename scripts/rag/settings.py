"""Parâmetros do RAG. Tudo ajustável por variável de ambiente, com padrões
que funcionam no free tier do Gemini sem estourar quota.
"""
import os


def _int(nome, padrao):
    try:
        return int(os.environ.get(nome, '').strip() or padrao)
    except ValueError:
        return padrao


def _float(nome, padrao):
    try:
        return float(os.environ.get(nome, '').strip() or padrao)
    except ValueError:
        return padrao


def _bool(nome, padrao=False):
    v = os.environ.get(nome, '').strip().lower()
    if not v:
        return padrao
    return v in ('1', 'true', 'sim', 'yes', 'on')


# ---------------------------------------------------------------- embeddings
EMBED_MODEL = os.environ.get('RAG_EMBED_MODEL', 'gemini-embedding-001')
# 768 é o ponto de equilíbrio do gemini-embedding-001 (MRL): custa 1/4 do
# vetor completo e mantém a qualidade de recuperação.
EMBED_DIM = _int('RAG_EMBED_DIM', 768)
# Cada item do lote conta como uma requisição na quota, então o lote não
# adianta nada contra o limite — serve só para reduzir round-trips.
#
# O lote precisa dividir o RPM: como a janela só libera um lote inteiro,
# 32 num teto de 90 deixa 2 lotes por minuto (64/min) e desperdiça 29% da
# cota. 24 em 96 fecha exatamente 4 lotes por minuto.
EMBED_BATCH = _int('RAG_EMBED_BATCH', 24)
# Free tier: 100 requisições/minuto por modelo, contando item a item.
EMBED_RPM = _int('RAG_EMBED_RPM', 96)
EMBED_MAX_CHARS = _int('RAG_EMBED_MAX_CHARS', 8000)

# --------------------------------------------------------------- chunking
CHUNK_MAX_CHARS = _int('RAG_CHUNK_MAX_CHARS', 3000)
CHUNK_MIN_CHARS = _int('RAG_CHUNK_MIN_CHARS', 25)

# --------------------------------------------------------------- recuperação
TOP_FTS = _int('RAG_TOP_FTS', 40)
TOP_VETORIAL = _int('RAG_TOP_VETORIAL', 40)
TOP_CANDIDATOS = _int('RAG_TOP_CANDIDATOS', 30)
TOP_CONTEXTO = _int('RAG_TOP_CONTEXTO', 8)
# Teto de trechos por ato, para uma lei extensa não ocupar todo o contexto.
MAX_POR_ATO = _int('RAG_MAX_POR_ATO', 4)
MAX_CHARS_CONTEXTO = _int('RAG_MAX_CHARS_CONTEXTO', 24000)
# Peso da diversidade no MMR: 0 = só relevância, 1 = só diversidade.
MMR_LAMBDA = _float('RAG_MMR_LAMBDA', 0.25)
# Abaixo disso o trecho é ruído e não entra no contexto.
SCORE_MINIMO = _float('RAG_SCORE_MINIMO', 0.02)
# O cosseno do gemini-embedding-001 é anisotrópico: num acervo homogêneo como
# este, trechos irrelevantes ficam em ~0.60 e os bons em ~0.69. Um piso
# absoluto não separa nada. O corte é relativo ao melhor da própria consulta.
SEMANTICA_MARGEM = _float('RAG_SEMANTICA_MARGEM', 0.08)
# k da fusão Reciprocal Rank Fusion.
RRF_K = _int('RAG_RRF_K', 60)

RERANK_LLM = _bool('RAG_RERANK_LLM', False)

# --------------------------------------------------------------- geração
CHAT_MODEL = os.environ.get('RAG_CHAT_MODEL') or os.environ.get(
    'GEMINI_MODEL', 'gemini-flash-latest')
CHAT_MODELOS_RESERVA = ('gemini-3.5-flash', 'gemini-flash-latest')
CHAT_TEMPERATURA = _float('RAG_CHAT_TEMPERATURA', 0.15)

# --------------------------------------------------------------- conversa
HISTORICO_MAX_TURNOS = _int('RAG_HISTORICO_MAX_TURNOS', 4)
HISTORICO_MAX_CHARS = _int('RAG_HISTORICO_MAX_CHARS', 2500)

# --------------------------------------------------------------- custo
CACHE_ATIVO = _bool('RAG_CACHE_ATIVO', True)
CACHE_TTL_HORAS = _int('RAG_CACHE_TTL_HORAS', 168)
# Pergunta que é só uma citação ("Art. 12 da Lei 5.349") é respondida pela
# busca direta, sem gastar chamada de LLM.
ATALHO_CITACAO = _bool('RAG_ATALHO_CITACAO', True)

# --------------------------------------------------------------- segurança
PERGUNTA_MAX_CHARS = _int('RAG_PERGUNTA_MAX_CHARS', 1000)
RATE_LIMITE = _int('RAG_RATE_LIMITE', 20)        # perguntas
RATE_JANELA_S = _int('RAG_RATE_JANELA_S', 300)   # por janela, por cliente
ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', '').strip()


def resumo():
    """Snapshot dos parâmetros, para o painel administrativo."""
    return {
        'embed_model': EMBED_MODEL, 'embed_dim': EMBED_DIM,
        'embed_batch': EMBED_BATCH, 'embed_rpm': EMBED_RPM,
        'top_candidatos': TOP_CANDIDATOS, 'top_contexto': TOP_CONTEXTO,
        'chat_model': CHAT_MODEL, 'rerank_llm': RERANK_LLM,
        'cache_ativo': CACHE_ATIVO, 'atalho_citacao': ATALHO_CITACAO,
        'admin_protegido': bool(ADMIN_TOKEN),
    }
