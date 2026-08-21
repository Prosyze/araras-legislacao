"""Orquestração da consulta: pergunta → resposta com fontes.

Aqui ficam as decisões de custo. Nem toda pergunta merece uma chamada de
modelo:

  * citação pura ("Art. 12 da Lei 5.349") é resolvida pela busca exata e
    devolvida direto, sem LLM;
  * pergunta sem nenhum dispositivo relevante recuperado devolve a resposta
    de ausência sem gastar chamada nenhuma;
  * pergunta repetida sai do cache.

O modelo entra quando há de fato o que sintetizar, comparar ou explicar.
"""
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

from . import (conversas, embeddings, llm, parser, prompt, rerank, retrieval,
               settings, vectorstore)

TRECHO_MAX = 900


def _agora():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _ms(t0):
    return int((time.perf_counter() - t0) * 1000)


# ------------------------------------------------------------------ fontes
def _fonte(chunk, n, usada):
    texto = (chunk.get('texto') or '').strip()
    return {
        'n': n,
        'chunk_id': chunk['chunk_id'],
        'ato_id': chunk['ato_id'],
        'norma': prompt.rotulo_norma(chunk),
        'dispositivo': (chunk.get('rotulo') if chunk.get('tipo_chunk') != 'cabecalho'
                        else 'Ementa'),
        'rotulo': prompt.rotulo_fonte(chunk),
        'tipo': chunk.get('tipo'),
        'numero': chunk.get('numero'),
        'data_ato': chunk.get('data_ato'),
        'titulo': chunk.get('titulo'),
        'secao': chunk.get('secao'),
        'artigo': chunk.get('artigo'),
        'incisos': chunk.get('incisos'),
        'trecho': texto[:TRECHO_MAX] + ('…' if len(texto) > TRECHO_MAX else ''),
        'score': chunk.get('score'),
        'usada': usada,
    }


# ------------------------------------------------------------------ cache
def _chave_cache(pergunta_busca, versao_indice):
    crua = json.dumps({
        'q': parser.normalizar(pergunta_busca),
        'v': list(versao_indice),
        'm': settings.CHAT_MODEL,
        'k': settings.TOP_CONTEXTO,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(crua.encode('utf-8')).hexdigest()


def _ler_cache(conn, chave):
    if not settings.CACHE_ATIVO:
        return None
    row = conn.execute(
        'SELECT payload, criada_em FROM ia_cache WHERE chave = ?',
        (chave,)).fetchone()
    if not row:
        return None
    try:
        criada = datetime.fromisoformat(row['criada_em'])
        if datetime.now(timezone.utc) - criada > timedelta(
                hours=settings.CACHE_TTL_HORAS):
            return None
        payload = json.loads(row['payload'])
    except Exception:  # noqa: BLE001 — cache corrompido é cache inexistente
        return None
    conn.execute('UPDATE ia_cache SET hits = hits + 1 WHERE chave = ?', (chave,))
    conn.commit()
    return payload


def _gravar_cache(conn, chave, payload):
    if not settings.CACHE_ATIVO:
        return
    conn.execute(
        'INSERT INTO ia_cache (chave, payload, criada_em, hits) VALUES (?,?,?,0) '
        'ON CONFLICT(chave) DO UPDATE SET payload=excluded.payload, '
        'criada_em=excluded.criada_em',
        (chave, json.dumps(payload, ensure_ascii=False), _agora()))
    conn.commit()


# ------------------------------------------------------------------ log
def _logar(conn, **campos):
    try:
        conn.execute(
            """INSERT INTO ia_consultas_log
               (criada_em, conversa_id, pergunta, estrategia, filtros, atos_ids,
                chunk_ids, n_candidatos, n_contexto, modelo, ms_busca, ms_llm,
                ms_total, cache, erro, cliente_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_agora(), campos.get('conversa_id'), campos.get('pergunta', '')[:2000],
             campos.get('estrategia'),
             json.dumps(campos.get('filtros') or {}, ensure_ascii=False),
             json.dumps(campos.get('atos_ids') or []),
             json.dumps(campos.get('chunk_ids') or []),
             campos.get('n_candidatos'), campos.get('n_contexto'),
             campos.get('modelo'), campos.get('ms_busca'), campos.get('ms_llm'),
             campos.get('ms_total'), 1 if campos.get('cache') else 0,
             (campos.get('erro') or None), campos.get('cliente_hash')))
        conn.commit()
    except Exception:  # noqa: BLE001 — log nunca derruba a consulta
        pass


# ------------------------------------------------------------------ fluxo
class PerguntaInvalida(ValueError):
    pass


def perguntar(conn, pergunta_bruta, conversa_id=None, cliente_hash=None,
              usar_cache=True):
    t_inicio = time.perf_counter()
    ms_busca = ms_llm = 0

    pergunta = prompt.sanitizar_pergunta(pergunta_bruta,
                                         settings.PERGUNTA_MAX_CHARS)
    if len(pergunta) < 3:
        raise PerguntaInvalida('Escreva uma pergunta com pelo menos 3 caracteres.')

    conversa_id = conversas.garantir(conn, conversa_id)
    historico = conversas.historico(conn, conversa_id)
    pergunta_busca = parser.reescrever_com_historico(pergunta, historico)
    consulta = parser.interpretar(pergunta_busca)

    chave = _chave_cache(pergunta_busca, vectorstore.versao_indice(conn))
    if usar_cache and not historico:
        cacheado = _ler_cache(conn, chave)
        if cacheado:
            cacheado = dict(cacheado, conversa_id=conversa_id, cache=True)
            conversas.adicionar(conn, conversa_id, 'user', pergunta)
            conversas.adicionar(conn, conversa_id, 'assistant',
                                cacheado['resposta'], cacheado.get('fontes'))
            _logar(conn, conversa_id=conversa_id, pergunta=pergunta,
                   estrategia='cache', cache=True, ms_total=_ms(t_inicio),
                   n_contexto=len(cacheado.get('fontes') or []),
                   cliente_hash=cliente_hash,
                   filtros=consulta.filtros_visiveis())
            return cacheado

    # ---------------------------------------------------------- recuperação
    t_busca = time.perf_counter()
    vetor = None
    aviso_semantica = None
    if embeddings.disponivel() and vectorstore.versao_indice(conn)[0]:
        try:
            vetor = embeddings.embed_consulta(pergunta_busca)
        except Exception as e:  # noqa: BLE001 — sem vetor a busca segue textual
            aviso_semantica = ('A busca semântica não respondeu; a consulta usou '
                               'apenas a busca textual.')
            _logar(conn, conversa_id=conversa_id, pergunta=pergunta,
                   estrategia='embedding_falhou', erro=str(e)[:400],
                   cliente_hash=cliente_hash)
    elif not vectorstore.versao_indice(conn)[0]:
        aviso_semantica = ('O índice semântico ainda não foi gerado; a consulta '
                           'usou apenas a busca textual.')

    candidatos, diag = retrieval.recuperar(conn, consulta, vetor)
    pontuados = rerank.pontuar(candidatos, consulta, diag['atos_referenciados'])

    if settings.RERANK_LLM and pontuados:
        pontuados = rerank.rerank_llm(
            pontuados[:settings.TOP_CANDIDATOS], consulta,
            gerar=lambda p: llm.gerar(p, temperatura=0.0))

    contexto = rerank.selecionar(
        pontuados, consulta,
        atos_prioritarios=diag['atos_referenciados'])
    ms_busca = _ms(t_busca)

    ausentes = [texto for texto, ids in
                retrieval.atos_das_referencias(conn, consulta).items() if not ids]

    base = {
        'ok': True,
        'conversa_id': conversa_id,
        'pergunta': pergunta,
        'cache': False,
        'aviso': aviso_semantica,
        'normas_nao_encontradas': ausentes,
        'interpretacao': consulta.filtros_visiveis(),
        'diagnostico': dict(diag, n_candidatos=len(candidatos),
                            n_contexto=len(contexto)),
    }

    # ------------------------------------------------- sem base: não chama LLM
    if not contexto:
        resposta = prompt.SEM_BASE
        if ausentes:
            resposta += (' As normas citadas na pergunta não constam do acervo: '
                         + '; '.join(ausentes) + '.')
        resultado = dict(base, resposta=resposta, fontes=[], modo='sem_base',
                         llm=False,
                         ms={'busca': ms_busca, 'llm': 0, 'total': _ms(t_inicio)})
        conversas.adicionar(conn, conversa_id, 'user', pergunta)
        conversas.adicionar(conn, conversa_id, 'assistant', resposta, [])
        _logar(conn, conversa_id=conversa_id, pergunta=pergunta,
               estrategia='sem_base', filtros=consulta.filtros_visiveis(),
               n_candidatos=len(candidatos), n_contexto=0, ms_busca=ms_busca,
               ms_total=_ms(t_inicio), cliente_hash=cliente_hash)
        if usar_cache and not historico:
            _gravar_cache(conn, chave, resultado)
        return resultado

    # ------------------------------------------------- atalho: citação pura
    if (settings.ATALHO_CITACAO and consulta.so_citacao and diag['n_exatos']
            and not historico):
        fontes = [_fonte(c, i + 1, True) for i, c in enumerate(contexto)]
        alvo = fontes[0]['rotulo']
        resposta = (f'Segue o texto do dispositivo solicitado, tal como consta '
                    f'no acervo — {alvo}. Reproduzido na íntegra, sem análise.')
        resultado = dict(base, resposta=resposta, fontes=fontes, modo='direta',
                         llm=False,
                         ms={'busca': ms_busca, 'llm': 0, 'total': _ms(t_inicio)})
        conversas.adicionar(conn, conversa_id, 'user', pergunta)
        conversas.adicionar(conn, conversa_id, 'assistant', resposta, fontes)
        _logar(conn, conversa_id=conversa_id, pergunta=pergunta,
               estrategia='direta', filtros=consulta.filtros_visiveis(),
               atos_ids=[c['ato_id'] for c in contexto],
               chunk_ids=[c['chunk_id'] for c in contexto],
               n_candidatos=len(candidatos), n_contexto=len(contexto),
               ms_busca=ms_busca, ms_total=_ms(t_inicio),
               cliente_hash=cliente_hash)
        if usar_cache:
            _gravar_cache(conn, chave, resultado)
        return resultado

    # ------------------------------------------------- geração
    texto_prompt = prompt.montar(
        pergunta, contexto, historico=historico,
        comparativa=consulta.comparativa, normas_ausentes=ausentes)

    t_llm = time.perf_counter()
    try:
        bruto = llm.gerar(texto_prompt)
    except Exception as e:  # noqa: BLE001
        _logar(conn, conversa_id=conversa_id, pergunta=pergunta,
               estrategia='erro_llm', erro=str(e)[:400], ms_busca=ms_busca,
               ms_total=_ms(t_inicio), cliente_hash=cliente_hash,
               n_candidatos=len(candidatos), n_contexto=len(contexto))
        raise
    ms_llm = _ms(t_llm)

    resposta, usadas = prompt.extrair_citacoes(bruto, len(contexto))
    if not resposta:
        resposta = prompt.SEM_BASE

    # A fonte que o modelo não citou continua visível, mas marcada como não
    # utilizada: o usuário vê o que foi consultado e o que sustentou a resposta.
    fontes = [_fonte(c, i + 1, (i + 1) in usadas)
              for i, c in enumerate(contexto)]
    if not usadas and not prompt.sem_base(resposta):
        # Resposta sem nenhuma citação não é auditável; devolve as fontes
        # consultadas e avisa.
        base['aviso'] = ((base.get('aviso') or '') +
                         ' A resposta não indicou fontes explicitamente; '
                         'confira os dispositivos abaixo.').strip()

    modo = 'sem_base' if prompt.sem_base(resposta) else 'rag'
    resultado = dict(base, resposta=resposta, fontes=fontes, modo=modo,
                     llm=True, modelo=settings.CHAT_MODEL,
                     ms={'busca': ms_busca, 'llm': ms_llm,
                         'total': _ms(t_inicio)})

    conversas.adicionar(conn, conversa_id, 'user', pergunta)
    conversas.adicionar(conn, conversa_id, 'assistant', resposta,
                        [f for f in fontes if f['usada']] or fontes)
    _logar(conn, conversa_id=conversa_id, pergunta=pergunta, estrategia=modo,
           filtros=consulta.filtros_visiveis(),
           atos_ids=sorted({c['ato_id'] for c in contexto}),
           chunk_ids=[c['chunk_id'] for c in contexto],
           n_candidatos=len(candidatos), n_contexto=len(contexto),
           modelo=settings.CHAT_MODEL, ms_busca=ms_busca, ms_llm=ms_llm,
           ms_total=_ms(t_inicio), cliente_hash=cliente_hash)

    if usar_cache and not historico:
        _gravar_cache(conn, chave, resultado)
    return resultado


def buscar_dispositivos(conn, texto, limite=10):
    """Busca híbrida crua, sem passar pelo modelo. Serve para o modo de
    pré-visualização e para os testes de recuperação."""
    consulta = parser.interpretar(texto)
    vetor = None
    if embeddings.disponivel() and vectorstore.versao_indice(conn)[0]:
        try:
            vetor = embeddings.embed_consulta(texto)
        except Exception:  # noqa: BLE001
            vetor = None
    candidatos, diag = retrieval.recuperar(conn, consulta, vetor)
    pontuados = rerank.pontuar(candidatos, consulta, diag['atos_referenciados'])
    escolhidos = rerank.selecionar(pontuados, consulta, k=limite,
                                   atos_prioritarios=diag['atos_referenciados'])
    return [_fonte(c, i + 1, True) for i, c in enumerate(escolhidos)], diag
