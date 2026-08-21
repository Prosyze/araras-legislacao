"""Rotas da consulta inteligente.

Blueprint separado de propósito: a busca convencional continua exatamente
como está em `app.py`, e a camada de IA pode ser desligada (ou falhar) sem
levar a pesquisa junto.
"""
import hashlib
import os
import sqlite3
import threading
import time

from flask import Blueprint, current_app, jsonify, request

from rag import conversas, embeddings, indexer, llm, schema, service, settings

bp = Blueprint('ia', __name__, url_prefix='/api/ia')

_SALT = os.environ.get('LOG_SALT') or 'araras-legislacao'


def _db():
    # Importação tardia: `app` importa este módulo, não o contrário.
    from app import get_db
    return get_db()


def _cliente_hash():
    """Identificador estável do cliente sem guardar o IP."""
    ip = (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
          or request.remote_addr or '-')
    return hashlib.sha256(f'{_SALT}:{ip}'.encode('utf-8')).hexdigest()[:16]


# --------------------------------------------------------------- rate limit
_janelas = {}
_rl_lock = threading.Lock()


def _rate_ok(chave):
    agora = time.time()
    with _rl_lock:
        marcas = [t for t in _janelas.get(chave, [])
                  if agora - t < settings.RATE_JANELA_S]
        if len(marcas) >= settings.RATE_LIMITE:
            _janelas[chave] = marcas
            return False, int(settings.RATE_JANELA_S - (agora - marcas[0]))
        marcas.append(agora)
        _janelas[chave] = marcas
        if len(_janelas) > 5000:  # poda barata contra crescimento sem fim
            for k in [k for k, v in _janelas.items()
                      if not v or agora - v[-1] > settings.RATE_JANELA_S]:
                _janelas.pop(k, None)
        return True, 0


# --------------------------------------------------------------- admin
def _local():
    return (request.remote_addr or '') in ('127.0.0.1', '::1', 'localhost')


def _admin_ok():
    """Sem ADMIN_TOKEN configurado, a administração só responde localmente —
    falha fechada em produção em vez de expor a indexação a qualquer um."""
    if not settings.ADMIN_TOKEN:
        return _local()
    enviado = (request.headers.get('X-Admin-Token')
               or request.args.get('token') or '')
    return bool(enviado) and enviado == settings.ADMIN_TOKEN


def _nega_admin():
    if settings.ADMIN_TOKEN:
        msg = 'Token administrativo inválido ou ausente.'
    else:
        msg = ('Área administrativa restrita ao acesso local. Defina '
               'ADMIN_TOKEN no ambiente para liberar o acesso remoto.')
    return jsonify({'error': msg}), 403


# --------------------------------------------------------------- consulta
@bp.route('/status')
def status():
    db = _db()
    if not schema.instalado(db):
        return jsonify({'pronto': False, 'motivo': 'índice não instalado'})
    st = indexer.estatisticas(db)
    return jsonify({
        'pronto': bool(st.get('indexados')),
        'busca_semantica': st.get('busca_semantica_pronta', False),
        'chave_configurada': st.get('chave_configurada', False),
        'dispositivos': st.get('dispositivos', 0),
        'indexados': st.get('indexados', 0),
        'pendentes': st.get('pendentes', 0),
        'cobertura': st.get('cobertura', 0.0),
        'indexando': indexer.get_state()['running'],
    })


@bp.route('/perguntar', methods=['POST'])
def perguntar():
    dados = request.get_json(silent=True) or {}
    pergunta = (dados.get('pergunta') or '').strip()
    conversa_id = (dados.get('conversa_id') or '').strip() or None

    if not pergunta:
        return jsonify({'error': 'Envie uma pergunta.'}), 400
    if len(pergunta) > settings.PERGUNTA_MAX_CHARS * 2:
        return jsonify({'error': 'Pergunta longa demais.'}), 413

    cliente = _cliente_hash()
    ok, espera = _rate_ok(cliente)
    if not ok:
        return jsonify({'error': 'Muitas consultas em pouco tempo. '
                                 f'Tente novamente em {espera}s.'}), 429

    db = _db()
    if not schema.instalado(db):
        return jsonify({'error': 'O índice de IA ainda não foi criado.'}), 503
    if not llm.disponivel():
        return jsonify({'error': 'GEMINI_API_KEY não configurada no servidor.'}), 503

    try:
        resultado = service.perguntar(db, pergunta, conversa_id=conversa_id,
                                      cliente_hash=cliente)
    except service.PerguntaInvalida as e:
        return jsonify({'error': str(e)}), 400
    except (llm.LLMIndisponivel, embeddings.EmbeddingIndisponivel) as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:  # noqa: BLE001
        current_app.logger.exception('falha na consulta com IA')
        return jsonify({'error': llm.mensagem_amigavel(e)}), 502

    return jsonify(resultado)


@bp.route('/buscar')
def buscar():
    """Recuperação sem geração — útil para conferir o que o RAG enxerga."""
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'error': 'Informe q.'}), 400
    limite = max(1, min(30, int(request.args.get('limite', 10))))
    db = _db()
    if not schema.instalado(db):
        return jsonify({'error': 'O índice de IA ainda não foi criado.'}), 503
    fontes, diag = service.buscar_dispositivos(db, q[:500], limite=limite)
    return jsonify({'resultados': fontes, 'diagnostico': diag})


@bp.route('/conversa/<conversa_id>')
def conversa(conversa_id):
    db = _db()
    if not schema.instalado(db):
        return jsonify({'error': 'indisponível'}), 503
    return jsonify({'conversa_id': conversa_id,
                    'mensagens': conversas.mensagens(db, conversa_id)})


@bp.route('/conversa/<conversa_id>', methods=['DELETE'])
def apagar_conversa(conversa_id):
    db = _db()
    if schema.instalado(db):
        conversas.apagar(db, conversa_id)
    return jsonify({'ok': True})


# --------------------------------------------------------------- administração
@bp.route('/admin/indice')
def admin_indice():
    if not _admin_ok():
        return _nega_admin()
    db = _db()
    schema.migrar(db)
    return jsonify({'indice': indexer.estatisticas(db),
                    'job': indexer.get_state(),
                    'config': settings.resumo()})


@bp.route('/admin/indexar', methods=['POST'])
def admin_indexar():
    if not _admin_ok():
        return _nega_admin()
    dados = request.get_json(silent=True) or {}
    kw = {
        'forcar_chunks': bool(dados.get('forcar')),
        'so_chunks': bool(dados.get('so_chunks')),
        'limite_embeddings': dados.get('limite') or None,
    }
    ato_ids = dados.get('ato_ids')
    if ato_ids:
        kw['ato_ids'] = [int(i) for i in ato_ids][:500]

    if not kw['so_chunks'] and not embeddings.disponivel():
        return jsonify({'error': 'GEMINI_API_KEY não configurada — não é '
                                 'possível gerar embeddings.'}), 503

    db_path = current_app.config['DB_PATH']
    if not indexer.iniciar_em_background(db_path, **kw):
        return jsonify({'error': 'Já existe uma indexação em andamento.',
                        'job': indexer.get_state()}), 409
    return jsonify({'ok': True, 'job': indexer.get_state()})


@bp.route('/admin/indexar/status')
def admin_indexar_status():
    if not _admin_ok():
        return _nega_admin()
    db = _db()
    return jsonify({'job': indexer.get_state(),
                    'indice': indexer.estatisticas(db)})


@bp.route('/admin/indexar/cancelar', methods=['POST'])
def admin_cancelar():
    if not _admin_ok():
        return _nega_admin()
    return jsonify({'ok': indexer.cancelar(), 'job': indexer.get_state()})


@bp.route('/admin/logs')
def admin_logs():
    if not _admin_ok():
        return _nega_admin()
    limite = max(1, min(200, int(request.args.get('limite', 50))))
    db = _db()
    if not schema.instalado(db):
        return jsonify({'logs': []})
    rows = db.execute(
        'SELECT criada_em, pergunta, estrategia, n_candidatos, n_contexto, '
        '       modelo, ms_busca, ms_llm, ms_total, cache, erro '
        '  FROM ia_consultas_log ORDER BY id DESC LIMIT ?', (limite,)).fetchall()
    return jsonify({'logs': [dict(r) for r in rows]})


def registrar(app, db_path):
    """Chamado por `app.py` na subida."""
    app.config['DB_PATH'] = db_path
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        schema.migrar(conn)
    finally:
        conn.close()
    app.register_blueprint(bp)
