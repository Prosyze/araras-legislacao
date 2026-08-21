"""Busca híbrida sobre os dispositivos.

Três caminhos, combinados por Reciprocal Rank Fusion:

  exata      — a pergunta cita norma e/ou artigo: o dispositivo é buscado
               por metadado, sem depender de similaridade nenhuma;
  textual    — FTS5 sobre `chunks_fts`, que acha "art. 75", "IPTU", número
               de processo e qualquer termo literal;
  semântica  — cosseno sobre os embeddings, que acha "contratação direta"
               quando a pergunta falou em "contratar sem licitação".

A RRF é usada de propósito no lugar de somar scores: BM25 e cosseno vivem em
escalas diferentes e sem relação estável entre si; posição é comparável,
valor bruto não é.
"""
import re

from . import settings, vectorstore
from .parser import sem_acento

# `numero` na base aparece como "5.349" e às vezes como "6.275/2017".
# Normaliza para só dígitos da parte anterior à barra.
NUM_SQL = """replace(replace(
    CASE WHEN instr(a.numero, '/') > 0
         THEN substr(a.numero, 1, instr(a.numero, '/') - 1)
         ELSE a.numero END, '.', ''), ' ', '')"""

COLUNAS = """
    c.id AS chunk_id, c.ato_id, c.ordem, c.tipo_chunk, c.rotulo, c.artigo,
    c.paragrafo, c.incisos, c.secao, c.texto, c.n_chars,
    a.tipo, a.numero, a.data_ato, a.titulo, a.edicao, a.data_diario
"""


# ------------------------------------------------------------------ filtros
def atos_das_referencias(conn, consulta):
    """ids dos atos citados explicitamente na pergunta."""
    achados = {}
    for ref in consulta.referencias:
        where = [f'{NUM_SQL} = ?']
        params = [ref['numero']]
        if ref['tipo']:
            where.append('a.tipo = ?')
            params.append(ref['tipo'])
        if ref['ano']:
            where.append("substr(a.data_ato, 1, 4) = ?")
            params.append(str(ref['ano']))
        sql = f"SELECT a.id FROM atos a WHERE {' AND '.join(where)}"
        ids = [r[0] for r in conn.execute(sql, params)]

        # "Lei 5.349" quando só existe o decreto de mesmo número: sem o tipo,
        # a pergunta ainda encontra a norma em vez de não achar nada.
        if not ids and ref['tipo']:
            sql = f'SELECT a.id FROM atos a WHERE {NUM_SQL} = ?'
            ids = [r[0] for r in conn.execute(sql, [ref['numero']])]
        achados[ref['texto']] = ids
    return achados


def atos_por_metadado(conn, consulta):
    """ids dos atos que passam nos filtros soltos (tipo e/ou ano)."""
    if not consulta.tipos and not consulta.anos:
        return None
    where, params = [], []
    if consulta.tipos:
        where.append('a.tipo IN (%s)' % ','.join('?' * len(consulta.tipos)))
        params += consulta.tipos
    if consulta.anos:
        where.append('substr(a.data_ato, 1, 4) IN (%s)'
                     % ','.join('?' * len(consulta.anos)))
        params += [str(a) for a in consulta.anos]
    sql = f"SELECT a.id FROM atos a WHERE {' AND '.join(where)}"
    return [r[0] for r in conn.execute(sql, params)]


def chunks_de_atos(conn, ato_ids):
    if not ato_ids:
        return []
    marcas = ','.join('?' * len(ato_ids))
    return [r[0] for r in conn.execute(
        f'SELECT id FROM ato_chunks WHERE ato_id IN ({marcas})', list(ato_ids))]


# ------------------------------------------------------------------ exata
def busca_exata(conn, consulta, ato_ids_por_ref):
    """Dispositivo citado pelo nome. É o resultado mais confiável que existe:
    não é estimativa, é o registro pedido."""
    ato_ids = sorted({i for ids in ato_ids_por_ref.values() for i in ids})
    if not ato_ids:
        return []

    marcas = ','.join('?' * len(ato_ids))
    if consulta.artigos:
        arts = ','.join('?' * len(consulta.artigos))
        sql = (f'SELECT {COLUNAS} FROM ato_chunks c JOIN atos a ON a.id = c.ato_id '
               f'WHERE c.ato_id IN ({marcas}) AND c.artigo IN ({arts}) '
               f'ORDER BY c.ato_id, c.ordem')
        rows = conn.execute(sql, ato_ids + consulta.artigos).fetchall()
        if rows:
            return [dict(r) for r in rows]

    # Sem artigo citado (ou artigo inexistente): a própria norma é a resposta.
    sql = (f'SELECT {COLUNAS} FROM ato_chunks c JOIN atos a ON a.id = c.ato_id '
           f'WHERE c.ato_id IN ({marcas}) ORDER BY c.ato_id, c.ordem')
    return [dict(r) for r in conn.execute(sql, ato_ids).fetchall()]


# ------------------------------------------------------------------ textual
def _fts_expr(termos, modo='and'):
    limpos = [re.sub(r'[^\w\-]', '', sem_acento(t)) for t in termos]
    limpos = [t for t in limpos if len(t) >= 3]
    if not limpos:
        return None
    juncao = ' AND ' if modo == 'and' else ' OR '
    return juncao.join(f'{t}*' for t in limpos)


# Abaixo disto a conjunção rendeu candidatos de menos para o rerank ter o que
# escolher, e vale procurar uma conjunção mais folgada.
MIN_TEXTUAIS = 10

# Quantas palavras a busca pode abrir mão antes de desistir da conjunção. Duas
# já cobrem o "quais normas tratam de..." que abre quase toda pergunta.
MAX_DESCARTES = 3


def _maior_conjuncao(termos, contar, minimo=MIN_TEXTUAIS):
    """Maior subconjunto dos termos que o acervo ainda satisfaz junto.

    A pergunta traz palavras que falam *sobre* a consulta — "quais normas
    tratam do transporte escolar" — e que não aparecem em texto de lei.
    Exigir todas na conjunção zera o resultado; cair direto no OR entrega o
    ranking a qualquer trecho que cite uma palavra sozinha, e uma linha de
    dotação orçamentária com "escolar" passa à frente da lei que regulamenta
    o serviço. Entre os dois extremos, descarta-se uma palavra de cada vez:
    sai aquela cuja ausência mais amplia o resultado.
    """
    if len(termos) < 2:
        return termos
    atual = list(termos)
    n = contar(_fts_expr(atual, 'and') or '')
    for _ in range(MAX_DESCARTES):
        if n >= minimo or len(atual) <= 2:
            break
        melhor, melhor_n = None, n
        for t in atual:
            candidato = [x for x in atual if x != t]
            expr = _fts_expr(candidato, 'and')
            if not expr:
                continue
            c = contar(expr)
            if c > melhor_n:
                melhor, melhor_n = candidato, c
        if melhor is None:
            break
        atual, n = melhor, melhor_n
    return atual


def busca_textual(conn, consulta, limite, permitidos=None):
    termos = consulta.termos or [t for t in re.findall(
        r'[\wÀ-ÿ]{3,}', consulta.pergunta)]
    if not termos:
        return []

    filtro, params_filtro = '', []
    if permitidos:
        filtro = ' AND c.ato_id IN (%s)' % ','.join('?' * len(permitidos))
        params_filtro = list(permitidos)

    def executar(expr):
        sql = f"""
            SELECT {COLUNAS}, bm25(chunks_fts, 1.0, 2.0) AS bm25
              FROM chunks_fts
              JOIN ato_chunks c ON c.id = chunks_fts.rowid
              JOIN atos a ON a.id = c.ato_id
             WHERE chunks_fts MATCH ?{filtro}
             ORDER BY bm25 LIMIT ?
        """
        return conn.execute(sql, [expr] + params_filtro + [limite]).fetchall()

    def contar(expr):
        sql = f"""
            SELECT count(*) FROM chunks_fts
              JOIN ato_chunks c ON c.id = chunks_fts.rowid
             WHERE chunks_fts MATCH ?{filtro}
        """
        try:
            return conn.execute(sql, [expr] + params_filtro).fetchone()[0]
        except Exception:  # noqa: BLE001 — expressão inválida conta como zero
            return 0

    termos = _maior_conjuncao(termos, contar)

    rows = []
    expr = _fts_expr(termos, 'and')
    if expr:
        try:
            rows = executar(expr)
        except Exception:  # noqa: BLE001 — expressão inválida vira busca OR
            rows = []
    # Poucos resultados com AND: relaxa para OR e deixa o BM25 ordenar.
    if len(rows) < MIN_TEXTUAIS:
        expr_or = _fts_expr(termos, 'or')
        if expr_or:
            try:
                vistos = {r['chunk_id'] for r in rows}
                rows = list(rows) + [r for r in executar(expr_or)
                                     if r['chunk_id'] not in vistos]
            except Exception:  # noqa: BLE001
                pass

    saida = []
    for r in rows[:limite]:
        d = dict(r)
        d.pop('bm25', None)
        saida.append(d)
    return saida


# ------------------------------------------------------------------ semântica
def busca_semantica(conn, vetor, limite, permitidos_chunks=None):
    if vetor is None:
        return []
    pares = vectorstore.buscar(conn, vetor, limite, permitidos=permitidos_chunks)
    if not pares:
        return []

    # Corte relativo: o que ficou muito abaixo do melhor desta consulta é
    # cauda. Sem isso, a RRF premia pela posição um trecho que só entrou por
    # falta de concorrente, e ele sobe ao topo do ranking final.
    teto = pares[0][1]
    pares = [p for p in pares if p[1] >= teto - settings.SEMANTICA_MARGEM]

    ids = [p[0] for p in pares]
    scores = dict(pares)
    marcas = ','.join('?' * len(ids))
    sql = (f'SELECT {COLUNAS} FROM ato_chunks c JOIN atos a ON a.id = c.ato_id '
           f'WHERE c.id IN ({marcas})')
    rows = {r['chunk_id']: dict(r) for r in conn.execute(sql, ids)}
    saida = []
    for cid in ids:
        d = rows.get(cid)
        if d:
            d['_cos'] = scores[cid]
            saida.append(d)
    return saida


# ------------------------------------------------------------------ fusão
def fundir(listas, k=None, peso_por_lista=None):
    """Reciprocal Rank Fusion. `listas` é [(nome, [candidatos])]."""
    k = k or settings.RRF_K
    pesos = peso_por_lista or {}
    acumulado = {}
    for nome, lista in listas:
        peso = pesos.get(nome, 1.0)
        for pos, cand in enumerate(lista):
            cid = cand['chunk_id']
            alvo = acumulado.get(cid)
            if alvo is None:
                alvo = dict(cand)
                alvo['score_rrf'] = 0.0
                alvo['origens'] = {}
                acumulado[cid] = alvo
            alvo['score_rrf'] += peso / (k + pos + 1)
            alvo['origens'][nome] = pos + 1
            if '_cos' in cand:
                alvo['_cos'] = cand['_cos']
    return sorted(acumulado.values(), key=lambda d: -d['score_rrf'])


# ------------------------------------------------------------------ fachada
def recuperar(conn, consulta, vetor=None):
    """Devolve (candidatos, diagnostico). Não decide o contexto final — isso
    é do `rerank`."""
    refs = atos_das_referencias(conn, consulta)
    ato_ids_ref = sorted({i for ids in refs.values() for i in ids})
    ato_ids_meta = atos_por_metadado(conn, consulta)

    # A norma citada manda: quando ela existe, a busca acontece dentro dela.
    escopo = ato_ids_ref or ato_ids_meta

    exatos = busca_exata(conn, consulta, refs) if ato_ids_ref else []
    textuais = busca_textual(conn, consulta, settings.TOP_FTS,
                             permitidos=escopo)
    permitidos_chunks = chunks_de_atos(conn, escopo) if escopo else None
    semanticos = busca_semantica(conn, vetor, settings.TOP_VETORIAL,
                                 permitidos_chunks=permitidos_chunks)

    # Sem nenhum acerto dentro do escopo, vale reabrir a busca para a base
    # inteira — o usuário pode ter citado a norma errada.
    if escopo and not textuais and not semanticos and not exatos:
        textuais = busca_textual(conn, consulta, settings.TOP_FTS)
        semanticos = busca_semantica(conn, vetor, settings.TOP_VETORIAL)

    fundidos = fundir(
        [('exata', exatos[:settings.TOP_CANDIDATOS]),
         ('textual', textuais),
         ('semantica', semanticos)],
        peso_por_lista={'exata': 3.0, 'textual': 1.0, 'semantica': 1.2})

    diagnostico = {
        'referencias_resolvidas': {k: len(v) for k, v in refs.items()},
        'atos_referenciados': ato_ids_ref,
        'escopo_atos': len(escopo) if escopo else 0,
        'n_exatos': len(exatos), 'n_textuais': len(textuais),
        'n_semanticos': len(semanticos),
        'semantica_ativa': bool(semanticos),
    }
    return fundidos[:settings.TOP_CANDIDATOS * 2], diagnostico


def cabecalho_do_ato(conn, ato_id):
    row = conn.execute(
        f"SELECT {COLUNAS} FROM ato_chunks c JOIN atos a ON a.id = c.ato_id "
        f"WHERE c.ato_id = ? AND c.tipo_chunk = 'cabecalho' "
        f"ORDER BY c.ordem LIMIT 1", (ato_id,)).fetchone()
    return dict(row) if row else None
