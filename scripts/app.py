import os
import re
import sqlite3
import time

from dotenv import load_dotenv
from flask import Flask, g, jsonify, render_template, request

import config
import manutencao
import routes_ia
import updater
from format_lib import plain_text, structure_text
from rag import indexer as rag_indexer

load_dotenv()

DB_PATH = config.preparar_dados()
# O banco do volume persiste entre deploys, entao correcoes nas regras de
# extracao precisam ser reaplicadas aos dados ja gravados. Primeiro o texto
# reextraido que veio na semente, depois a limpeza pelas regras atuais.
manutencao.sincronizar_com_semente(DB_PATH, config.SEED_DB)
manutencao.sanitizar(DB_PATH)
GEMINI_MODEL = config.GEMINI_MODEL
# Usados quando o principal esta sobrecarregado (503). Modelos com versao fixa
# saem do ar com o tempo, entao a reserva final e um alias que acompanha a atual.
MODELOS_RESERVA = ('gemini-3.5-flash', 'gemini-flash-latest')

app = Flask(__name__)

# Consulta inteligente (RAG). Fica num blueprint proprio: a pesquisa
# convencional acima nao depende dela e continua funcionando se a IA estiver
# indisponivel.
routes_ia.registrar(app, DB_PATH)


def _indexar_apos_atualizacao(novos_atos):
    """Lei nova cadastrada entra no indice sozinha, logo apos a atualizacao."""
    if novos_atos > 0:
        rag_indexer.iniciar_em_background(DB_PATH)


updater.ao_concluir = _indexar_apos_atualizacao

_genai_client = None


def gemini():
    global _genai_client
    if _genai_client is None:
        from google import genai
        key = os.environ.get('GEMINI_API_KEY')
        if not key:
            raise RuntimeError('GEMINI_API_KEY nao configurada no .env')
        _genai_client = genai.Client(api_key=key)
    return _genai_client


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        # WAL deixa a leitura da busca conviver com a escrita da atualizacao.
        g.db.execute('PRAGMA journal_mode=WAL')
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def clean_snippet(s):
    """Collapse PDF line wrapping and escape HTML, keeping only our own
    highlight markers as real tags."""
    s = re.sub(r'\s+', ' ', s or '').strip()
    s = (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
    return s.replace('@@M@@', '<mark>').replace('@@/M@@', '</mark>')


# Numero com separador de milhar precisa ser lido inteiro: caindo no padrao
# generico, o ponto vira fronteira e "1.620" ja chega quebrado em dois termos.
TERMO_RE = re.compile(r'\d{1,3}(?:[.,]\d{3})+|[\w\-]+', re.UNICODE)


def fts_query(raw_q):
    """Turn free-text user input into a safe FTS5 MATCH query: AND of terms.

    Palavra vai como prefixo, para achar com digitacao parcial. Numero vai
    exato: como prefixo, "1.620" viraria `1* AND 620*` e casaria com todo ato
    que tivesse um numero comecando em 1 e outro em 620 -- valor, CNPJ, o que
    fosse. O tokenizador quebra no ponto, entao a grafia com separador so e
    alcancada como frase, e a sem separador entra junto num OR. Cada termo vai
    entre aspas porque o hifen nao e valido em termo solto do FTS5.
    """
    termos = []
    for bruto in TERMO_RE.findall(raw_q):
        if '.' in bruto or ',' in bruto:
            termos.append(f'("{bruto}" OR {re.sub(r"[^0-9]", "", bruto)})')
        elif bruto.isdigit():
            termos.append(bruto)
        else:
            termos.append(f'"{bruto}"*')
    if not termos:
        return None
    return ' AND '.join(termos)


def numeros_de_ato(raw_q):
    """Digitos da consulta que podem ser o numero de um ato, sem o separador.

    So conta termo que e numero por inteiro: em "covid-19" o 19 faz parte da
    palavra e nao deve trazer o Decreto Legislativo 19 para o topo.
    """
    achados = []
    for bruto in TERMO_RE.findall(raw_q):
        d = re.sub(r'[^0-9]', '', bruto)
        if d != re.sub(r'[.,]', '', bruto):
            continue
        if 0 < len(d) <= 6 and d not in achados:
            achados.append(d)
    return achados


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/facetas')
def facetas():
    db = get_db()
    anos = [r[0] for r in db.execute(
        "SELECT DISTINCT substr(data_ato,1,4) a FROM atos WHERE a IS NOT NULL ORDER BY a DESC"
    )]
    return jsonify({'anos': anos})


@app.route('/api/search')
def search():
    db = get_db()
    q = request.args.get('q', '').strip()
    tipo = request.args.get('tipo', '').strip()
    numero = request.args.get('numero', '').strip()
    ano = request.args.get('ano', '').strip()
    ordem = request.args.get('ordem', 'recentes').strip()
    page = max(1, int(request.args.get('page', 1)))
    page_size = 20
    offset = (page - 1) * page_size

    # Records missing data_ato still sort sensibly by the diary's own date.
    DATE_DESC = 'COALESCE(a.data_ato, a.data_diario) DESC, a.edicao DESC, a.id DESC'
    DATE_ASC = 'COALESCE(a.data_ato, a.data_diario) ASC, a.edicao ASC, a.id ASC'

    where = []
    params = []

    if tipo:
        where.append('a.tipo = ?')
        params.append(tipo)
    if numero:
        where.append("REPLACE(a.numero, '.', '') LIKE ?")
        params.append(f"%{numero.replace('.', '')}%")
    if ano:
        where.append('substr(a.data_ato,1,4) = ?')
        params.append(ano)

    fts = fts_query(q) if q else None
    # Quem digita "1.620" na caixa quer a Lei 1.620, e nao os atos cujo texto
    # por acaso cita esse numero -- entao o numero do ato entra na busca junto
    # com o texto, e vem antes na ordenacao.
    alvos = numeros_de_ato(q) if fts else []

    if fts and alvos:
        cond_num = "REPLACE(a.numero, '.', '') IN (%s)" % ','.join('?' * len(alvos))
        where_sql = (' AND ' + ' AND '.join(where)) if where else ''
        base = f"""
            WITH achados AS (
                SELECT rowid AS id,
                       snippet(atos_fts, 1, '@@M@@', '@@/M@@', '…', 20) AS trecho
                FROM atos_fts WHERE atos_fts MATCH ?
            )
            SELECT %s
            FROM atos a LEFT JOIN achados h ON h.id = a.id
            WHERE (h.id IS NOT NULL OR {cond_num}) {where_sql}
        """
        colunas = """a.id, a.edicao, a.tipo, a.numero, a.data_ato, a.titulo,
                     COALESCE(h.trecho, substr(a.texto, 1, 220)) AS trecho,
                     (a.resumo IS NOT NULL) AS tem_resumo"""
        sql = (base % colunas) + f"""
            ORDER BY {cond_num} DESC,
                     {DATE_ASC if ordem == 'antigos' else DATE_DESC}
            LIMIT ? OFFSET ?
        """
        args = [fts] + alvos + params
        rows = db.execute(sql, args + alvos + [page_size, offset]).fetchall()
        total = db.execute(base % 'count(*)', args).fetchone()[0]
    elif fts:
        where_sql = (' AND ' + ' AND '.join(where)) if where else ''
        sql = f"""
            SELECT a.id, a.edicao, a.tipo, a.numero, a.data_ato, a.titulo,
                   snippet(atos_fts, 1, '@@M@@', '@@/M@@', '…', 20) AS trecho,
                   (a.resumo IS NOT NULL) AS tem_resumo
            FROM atos_fts
            JOIN atos a ON a.id = atos_fts.rowid
            WHERE atos_fts MATCH ? {where_sql}
            ORDER BY {DATE_ASC if ordem == 'antigos' else DATE_DESC}
            LIMIT ? OFFSET ?
        """
        count_sql = f"""
            SELECT count(*) FROM atos_fts JOIN atos a ON a.id = atos_fts.rowid
            WHERE atos_fts MATCH ? {where_sql}
        """
        rows = db.execute(sql, [fts] + params + [page_size, offset]).fetchall()
        total = db.execute(count_sql, [fts] + params).fetchone()[0]
    else:
        where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
        sql = f"""
            SELECT a.id, a.edicao, a.tipo, a.numero, a.data_ato, a.titulo,
                   substr(a.texto, 1, 220) AS trecho,
                   (a.resumo IS NOT NULL) AS tem_resumo
            FROM atos a
            {where_sql}
            ORDER BY {DATE_ASC if ordem == 'antigos' else DATE_DESC}
            LIMIT ? OFFSET ?
        """
        count_sql = f"SELECT count(*) FROM atos a {where_sql}"
        rows = db.execute(sql, params + [page_size, offset]).fetchall()
        total = db.execute(count_sql, params).fetchone()[0]

    results = []
    for r in rows:
        d = dict(r)
        d['trecho'] = clean_snippet(d.get('trecho', ''))
        results.append(d)
    return jsonify({
        'results': results,
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': max(1, (total + page_size - 1) // page_size),
    })


@app.route('/api/ato/<int:ato_id>')
def ato_detail(ato_id):
    db = get_db()
    row = db.execute('SELECT * FROM atos WHERE id = ?', (ato_id,)).fetchone()
    if not row:
        return jsonify({'error': 'nao encontrado'}), 404
    data = dict(row)
    data['blocos'] = structure_text(data['texto'])
    return jsonify(data)


PROMPT = """Você é um analista legislativo. Resuma o ato normativo municipal abaixo
para um leitor leigo, em português do Brasil.

Responda EXATAMENTE neste formato, sem markdown, sem asteriscos:

OBJETO: <uma frase dizendo o que o ato faz>
PONTOS:
- <ponto principal 1>
- <ponto principal 2>
- <ponto principal 3, se houver>
VIGENCIA: <quando entra em vigor, ou "não especificado">

Regras: seja objetivo e factual, use no máximo 4 pontos, não invente informação
que não esteja no texto, ignore listas de dotação orçamentária repetitivas
(nesse caso apenas indique o valor total e a finalidade).

ATO:
{texto}"""


def _transitorio(e):
    """503/429/500 do Gemini sao picos de demanda, nao erro do pedido."""
    s = str(e)
    return any(c in s for c in ('503', 'UNAVAILABLE', '429', 'RESOURCE_EXHAUSTED',
                                'overloaded', 'high demand', '500', 'INTERNAL'))


def _mensagem_amigavel(e):
    if _transitorio(e):
        return ('O serviço de IA está sobrecarregado no momento. '
                'Tente novamente em alguns instantes.')
    return f'Falha ao gerar resumo: {e}'


def _gerar_com_retry(prompt):
    """Tenta o modelo principal com backoff e, se persistir, o de reserva."""
    ultimo = None
    for modelo in (GEMINI_MODEL, *MODELOS_RESERVA):
        for tentativa in range(3):
            try:
                resp = gemini().models.generate_content(
                    model=modelo, contents=prompt)
                return (resp.text or '').strip()
            except Exception as e:
                ultimo = e
                if not _transitorio(e):
                    raise
                time.sleep(1.5 * (2 ** tentativa))  # 1.5s, 3s, 6s
    raise ultimo


@app.route('/api/resumo/<int:ato_id>', methods=['POST'])
def resumo(ato_id):
    db = get_db()
    row = db.execute('SELECT titulo, texto, resumo FROM atos WHERE id = ?',
                     (ato_id,)).fetchone()
    if not row:
        return jsonify({'error': 'nao encontrado'}), 404

    if row['resumo'] and not request.args.get('regerar'):
        return jsonify({'resumo': row['resumo'], 'cache': True})

    corpo = plain_text(row['texto'])
    if len(corpo) > 120000:
        corpo = corpo[:120000] + '\n[...texto truncado...]'

    prompt = PROMPT.format(texto=f"{row['titulo']}\n\n{corpo}")
    try:
        texto_resumo = _gerar_com_retry(prompt)
    except Exception as e:
        return jsonify({'error': _mensagem_amigavel(e)}), 502

    if not texto_resumo:
        return jsonify({'error': 'o modelo nao retornou resumo'}), 502

    db.execute('UPDATE atos SET resumo = ? WHERE id = ?', (texto_resumo, ato_id))
    db.commit()
    return jsonify({'resumo': texto_resumo, 'cache': False})


@app.route('/api/atualizar', methods=['POST'])
def atualizar():
    completo = request.args.get('completo') == '1'
    iniciado = updater.iniciar_em_background(
        anos=updater.anos_padrao(completo=completo))
    if not iniciado:
        return jsonify({'error': 'Já existe uma atualização em andamento.',
                        'status': updater.get_state()}), 409
    return jsonify({'ok': True, 'status': updater.get_state()})


@app.route('/api/atualizar/status')
def atualizar_status():
    db = get_db()
    st = updater.get_state()
    st['ultima_edicao'] = db.execute(
        'SELECT max(edicao) FROM edicoes_processadas').fetchone()[0]
    st['ultima_data'] = db.execute(
        'SELECT max(data_diario) FROM atos').fetchone()[0]
    st['total_atos'] = db.execute('SELECT count(*) FROM atos').fetchone()[0]
    return jsonify(st)


@app.route('/api/stats')
def stats():
    db = get_db()
    por_tipo = {r['tipo']: r['n'] for r in db.execute(
        'SELECT tipo, count(*) n FROM atos GROUP BY tipo')}
    total = db.execute('SELECT count(*) FROM atos').fetchone()[0]
    edicoes = db.execute('SELECT count(*) FROM edicoes_processadas').fetchone()[0]
    com_resumo = db.execute(
        'SELECT count(*) FROM atos WHERE resumo IS NOT NULL').fetchone()[0]
    periodo = db.execute(
        'SELECT min(data_ato), max(data_ato) FROM atos WHERE data_ato IS NOT NULL'
    ).fetchone()
    return jsonify({
        'total': total, 'por_tipo': por_tipo, 'edicoes': edicoes,
        'com_resumo': com_resumo,
        'periodo': {'inicio': periodo[0], 'fim': periodo[1]},
    })


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5050, debug=False)
