"""Histórico de conversa.

Guarda tudo, mas devolve pouco: só os últimos turnos entram no prompt. O
histórico serve para desambiguar a pergunta atual ("e quais documentos são
necessários?"), não para ser reenviado inteiro a cada consulta — isso
encareceria a chamada e diluiria o contexto jurídico recuperado.
"""
import json
import uuid
from datetime import datetime, timezone

from . import settings


def _agora():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def criar(conn, titulo=None):
    cid = uuid.uuid4().hex
    agora = _agora()
    conn.execute(
        'INSERT INTO ia_conversas (id, titulo, criada_em, atualizada_em) '
        'VALUES (?,?,?,?)', (cid, titulo, agora, agora))
    conn.commit()
    return cid


def garantir(conn, conversa_id, titulo=None):
    if conversa_id:
        row = conn.execute('SELECT id FROM ia_conversas WHERE id = ?',
                           (conversa_id,)).fetchone()
        if row:
            return conversa_id
    return criar(conn, titulo=titulo)


def adicionar(conn, conversa_id, papel, conteudo, fontes=None):
    conn.execute(
        'INSERT INTO ia_mensagens (conversa_id, papel, conteudo, fontes, criada_em) '
        'VALUES (?,?,?,?,?)',
        (conversa_id, papel, conteudo,
         json.dumps(fontes, ensure_ascii=False) if fontes else None, _agora()))
    conn.execute('UPDATE ia_conversas SET atualizada_em = ? WHERE id = ?',
                 (_agora(), conversa_id))
    # O título da conversa é a primeira pergunta — suficiente para listar.
    conn.execute(
        'UPDATE ia_conversas SET titulo = ? WHERE id = ? AND titulo IS NULL',
        (conteudo[:120], conversa_id))
    conn.commit()


def historico(conn, conversa_id, max_turnos=None):
    """Últimas mensagens, em ordem cronológica, dentro do orçamento."""
    if not conversa_id:
        return []
    max_turnos = max_turnos or settings.HISTORICO_MAX_TURNOS
    rows = conn.execute(
        'SELECT papel, conteudo FROM ia_mensagens WHERE conversa_id = ? '
        'ORDER BY id DESC LIMIT ?', (conversa_id, max_turnos * 2)).fetchall()
    msgs = [{'papel': r['papel'], 'conteudo': r['conteudo']}
            for r in reversed(rows)]

    total = 0
    recortado = []
    for m in reversed(msgs):
        total += len(m['conteudo'])
        if total > settings.HISTORICO_MAX_CHARS and recortado:
            break
        recortado.append(m)
    return list(reversed(recortado))


def mensagens(conn, conversa_id):
    rows = conn.execute(
        'SELECT papel, conteudo, fontes, criada_em FROM ia_mensagens '
        'WHERE conversa_id = ? ORDER BY id', (conversa_id,)).fetchall()
    saida = []
    for r in rows:
        saida.append({
            'papel': r['papel'], 'conteudo': r['conteudo'],
            'criada_em': r['criada_em'],
            'fontes': json.loads(r['fontes']) if r['fontes'] else [],
        })
    return saida


def apagar(conn, conversa_id):
    conn.execute('DELETE FROM ia_mensagens WHERE conversa_id = ?', (conversa_id,))
    conn.execute('DELETE FROM ia_conversas WHERE id = ?', (conversa_id,))
    conn.commit()
