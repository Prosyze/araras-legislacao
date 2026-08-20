import os
import re
import sqlite3

from dotenv import load_dotenv
from flask import Flask, g, jsonify, render_template, request

import config
import updater
from format_lib import plain_text, structure_text

load_dotenv()

DB_PATH = config.preparar_dados()
GEMINI_MODEL = config.GEMINI_MODEL

app = Flask(__name__)

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


def fts_query(raw_q):
    """Turn free-text user input into a safe FTS5 MATCH query: AND of terms,
    each as a prefix match, so partial words still find results."""
    terms = re.findall(r'[\w\-]+', raw_q, flags=re.UNICODE)
    if not terms:
        return None
    return ' AND '.join(f'{t}*' for t in terms)


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

    if fts:
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

    try:
        resp = gemini().models.generate_content(
            model=GEMINI_MODEL,
            contents=PROMPT.format(texto=f"{row['titulo']}\n\n{corpo}"),
        )
        texto_resumo = (resp.text or '').strip()
    except Exception as e:
        return jsonify({'error': f'falha ao gerar resumo: {e}'}), 502

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
