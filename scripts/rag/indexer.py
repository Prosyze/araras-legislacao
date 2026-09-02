"""Indexação: transforma os atos em dispositivos e gera os vetores.

Duas etapas independentes e idempotentes:

  1. sincronizar_chunks — reparte os atos em dispositivos. Só mexe no que
     mudou: o hash do texto do ato decide. Também limpa órfãos deixados por
     `manutencao.sanitizar()`, que apaga atos direto na tabela.

  2. gerar_embeddings — vetoriza os dispositivos cuja hash não bate com a do
     embedding gravado (novos ou alterados). Resumível: pode parar a qualquer
     momento e continuar depois de onde estava.

Um documento problemático nunca derruba a rodada — o erro é registrado e a
indexação segue.
"""
import sqlite3
import threading
from datetime import datetime, timezone

import chaves_gemini

from . import embeddings, schema, settings, vectorstore
from .chunker import chunk_ato, hash_ato, texto_para_embedding

MAX_ERROS_GUARDADOS = 50

_state = {
    'running': False,
    'fase': 'idle',
    'mensagem': 'Nenhuma indexação em andamento.',
    'atos_feitos': 0,
    'atos_total': 0,
    'chunks_feitos': 0,
    'chunks_total': 0,
    'novos_chunks': 0,
    'embeddings_gerados': 0,
    'erros': [],
    'started_at': None,
    'finished_at': None,
}
_lock = threading.Lock()
_cancelar = threading.Event()


def _agora():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def get_state():
    with _lock:
        st = dict(_state)
        st['erros'] = list(_state['erros'])[-10:]
        st['cancelando'] = _cancelar.is_set()
        return st


def _set(**kw):
    with _lock:
        _state.update(kw)


def _erro(msg):
    with _lock:
        _state['erros'].append(f'{_agora()} · {msg}')
        del _state['erros'][:-MAX_ERROS_GUARDADOS]


# --------------------------------------------------------------- etapa 1
def _apagar_chunks(conn, ato_ids):
    """Remove dispositivos e vetores de um conjunto de atos."""
    for ato_id in ato_ids:
        ids = [r[0] for r in conn.execute(
            'SELECT id FROM ato_chunks WHERE ato_id = ?', (ato_id,))]
        if ids:
            marcas = ','.join('?' * len(ids))
            conn.execute(
                f'DELETE FROM chunk_embeddings WHERE chunk_id IN ({marcas})', ids)
            conn.execute('DELETE FROM ato_chunks WHERE ato_id = ?', (ato_id,))


def _limpar_orfaos(conn):
    """`manutencao.sanitizar()` apaga atos direto na tabela; os dispositivos
    ficariam para trás e apareceriam na busca como fonte inexistente."""
    orfaos = [r[0] for r in conn.execute(
        'SELECT DISTINCT c.ato_id FROM ato_chunks c '
        'LEFT JOIN atos a ON a.id = c.ato_id WHERE a.id IS NULL')]
    if orfaos:
        _apagar_chunks(conn, orfaos)
        conn.execute('DELETE FROM ato_index_estado WHERE ato_id NOT IN '
                     '(SELECT id FROM atos)')
        conn.commit()
    # Vetores sem dispositivo (interrupção no meio de uma reindexação).
    conn.execute('DELETE FROM chunk_embeddings WHERE chunk_id NOT IN '
                 '(SELECT id FROM ato_chunks)')
    conn.commit()
    return len(orfaos)


def _gravar_chunks(conn, ato, chunks):
    conn.executemany(
        """INSERT INTO ato_chunks
           (ato_id, ordem, tipo_chunk, rotulo, artigo, paragrafo, incisos,
            secao, texto, n_chars, hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [(ato['id'], c['ordem'], c['tipo_chunk'], c['rotulo'], c['artigo'],
          c['paragrafo'], c['incisos'], c['secao'], c['texto'], c['n_chars'],
          c['hash']) for c in chunks])


def sincronizar_chunks(conn, forcar=False, ato_ids=None, progresso=None):
    """Reparte em dispositivos os atos novos ou alterados.

    `forcar` reprocessa mesmo sem mudança de texto (usado depois de mexer nas
    regras do chunker). `ato_ids` limita a um subconjunto.
    """
    removidos = _limpar_orfaos(conn)
    if removidos:
        _erro(f'{removidos} ato(s) removido(s) da base tiveram seus '
              f'dispositivos descartados do índice')

    estado = {r[0]: r[1] for r in conn.execute(
        'SELECT ato_id, hash_texto FROM ato_index_estado')}

    if ato_ids:
        marcas = ','.join('?' * len(ato_ids))
        sql = f'SELECT id, titulo, texto FROM atos WHERE id IN ({marcas}) ORDER BY id'
        cursor = conn.execute(sql, list(ato_ids))
    else:
        cursor = conn.execute('SELECT id, titulo, texto FROM atos ORDER BY id')

    pendentes = []
    for r in cursor:
        h = hash_ato(r['titulo'], r['texto'])
        if forcar or estado.get(r['id']) != h:
            pendentes.append((dict(r), h))

    total = len(pendentes)
    _set(atos_total=total, atos_feitos=0)
    novos = 0

    for i, (ato, h) in enumerate(pendentes, 1):
        if _cancelar.is_set():
            break
        try:
            chunks = chunk_ato(ato)
            _apagar_chunks(conn, [ato['id']])
            _gravar_chunks(conn, ato, chunks)
            conn.execute(
                """INSERT INTO ato_index_estado
                   (ato_id, hash_texto, n_chunks, atualizado_em, erro)
                   VALUES (?,?,?,?,NULL)
                   ON CONFLICT(ato_id) DO UPDATE SET
                     hash_texto=excluded.hash_texto, n_chunks=excluded.n_chunks,
                     atualizado_em=excluded.atualizado_em, erro=NULL""",
                (ato['id'], h, len(chunks), _agora()))
            novos += len(chunks)
            if not chunks:
                _erro(f'ato {ato["id"]}: texto sem conteúdo aproveitável')
        except Exception as e:  # noqa: BLE001 — um ato ruim não para a carga
            conn.rollback()
            _erro(f'ato {ato["id"]}: {e}')
            try:
                conn.execute(
                    """INSERT INTO ato_index_estado
                       (ato_id, hash_texto, n_chunks, atualizado_em, erro)
                       VALUES (?,?,0,?,?)
                       ON CONFLICT(ato_id) DO UPDATE SET
                         atualizado_em=excluded.atualizado_em, erro=excluded.erro""",
                    (ato['id'], h, _agora(), str(e)[:400]))
            except Exception:
                pass

        if i % 50 == 0 or i == total:
            conn.commit()
            _set(atos_feitos=i, novos_chunks=novos)
            if progresso:
                progresso(i, total)

    conn.commit()
    _set(atos_feitos=total, novos_chunks=novos)
    return novos


# --------------------------------------------------------------- etapa 2
SQL_PENDENTES = """
    SELECT c.id, c.ato_id, c.tipo_chunk, c.rotulo, c.secao, c.texto, c.hash,
           a.titulo, a.tipo, a.numero, a.data_ato
      FROM ato_chunks c
      JOIN atos a ON a.id = c.ato_id
      LEFT JOIN chunk_embeddings e
             ON e.chunk_id = c.id AND e.modelo = ? AND e.dim = ?
     WHERE e.chunk_id IS NULL OR e.hash <> c.hash
     ORDER BY c.ato_id DESC, c.ordem
"""


def contar_pendentes(conn):
    return conn.execute(
        f'SELECT count(*) FROM ({SQL_PENDENTES})',
        (settings.EMBED_MODEL, settings.EMBED_DIM)).fetchone()[0]


def gerar_embeddings(conn, limite=None, progresso=None):
    """Vetoriza os dispositivos pendentes. Grava a cada lote, então uma
    interrupção só perde o lote em andamento.

    Enquanto roda, a troca de chave por falta de cota aparece na mesma lista
    dos erros de lote: numa rodada longa é o que explica por que ela parou —
    ou por que seguiu.
    """
    if not embeddings.disponivel():
        raise embeddings.EmbeddingIndisponivel(
            'GEMINI_API_KEY não configurada — não é possível gerar embeddings.')

    pendentes = conn.execute(
        SQL_PENDENTES, (settings.EMBED_MODEL, settings.EMBED_DIM)).fetchall()
    if limite:
        pendentes = pendentes[:limite]

    total = len(pendentes)
    _set(chunks_total=total, chunks_feitos=0)
    lote = settings.EMBED_BATCH
    chaves_gemini.ao_girar = _erro
    try:
        feitos = _vetorizar(conn, pendentes, total, lote, progresso)
    finally:
        chaves_gemini.ao_girar = None
    vectorstore.invalidar()
    return feitos


def _vetorizar(conn, pendentes, total, lote, progresso):
    feitos = 0
    for ini in range(0, total, lote):
        if _cancelar.is_set():
            break
        fatia = pendentes[ini:ini + lote]
        textos = [
            texto_para_embedding(
                {'texto': r['texto'], 'rotulo': r['rotulo'],
                 'secao': r['secao'], 'tipo_chunk': r['tipo_chunk']},
                {'titulo': r['titulo']})
            for r in fatia
        ]
        try:
            vetores = embeddings.embed_documentos(textos)
        except embeddings.EmbeddingIndisponivel:
            raise
        except Exception as e:  # noqa: BLE001 — lote ruim não para a rodada
            _erro(f'lote {ini}-{ini + len(fatia)}: {e}')
            _set(chunks_feitos=min(ini + len(fatia), total))
            # Lote ruim não para a rodada, mas cota do dia esgotada sim: se
            # nem a última chave tem saldo, os lotes seguintes vão falhar
            # igual, e o que já foi gerado fica gravado de qualquer forma.
            if chaves_gemini.cota_diaria(e):
                _erro(f'cota diária esgotada nas {chaves_gemini.total()} '
                      f'chave(s); encerrando a rodada — retome amanhã, o que '
                      f'já foi gerado está gravado')
                break
            continue

        agora = _agora()
        conn.executemany(
            """INSERT INTO chunk_embeddings
               (chunk_id, modelo, dim, hash, vetor, criado_em)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(chunk_id) DO UPDATE SET
                 modelo=excluded.modelo, dim=excluded.dim, hash=excluded.hash,
                 vetor=excluded.vetor, criado_em=excluded.criado_em""",
            [(r['id'], settings.EMBED_MODEL, settings.EMBED_DIM, r['hash'],
              embeddings.para_blob(v), agora)
             for r, v in zip(fatia, vetores)])
        conn.commit()

        feitos += len(fatia)
        _set(chunks_feitos=min(ini + len(fatia), total),
             embeddings_gerados=feitos)
        if progresso:
            progresso(feitos, total)

    return feitos


# --------------------------------------------------------------- orquestração
def rodar(db_path, forcar_chunks=False, so_chunks=False, ato_ids=None,
          limite_embeddings=None):
    """Ciclo completo. Deve rodar em thread própria."""
    conn = sqlite3.connect(db_path, timeout=120)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        schema.migrar(conn)

        _set(running=True, fase='dividindo', atos_feitos=0, atos_total=0,
             chunks_feitos=0, chunks_total=0, novos_chunks=0,
             embeddings_gerados=0, erros=[], finished_at=None,
             started_at=_agora(),
             mensagem='Dividindo os atos em dispositivos…')

        def prog_ato(i, total):
            _set(mensagem=f'Dividindo os atos em dispositivos… {i}/{total}')

        sincronizar_chunks(conn, forcar=forcar_chunks, ato_ids=ato_ids,
                           progresso=prog_ato)

        if _cancelar.is_set():
            _set(fase='cancelado', mensagem='Indexação interrompida.')
            return

        if so_chunks:
            _set(fase='concluido',
                 mensagem='Dispositivos atualizados (sem gerar vetores).')
            return

        pendentes = contar_pendentes(conn)
        _set(fase='vetorizando', chunks_total=pendentes,
             mensagem=(f'Gerando vetores de {pendentes} dispositivo(s)…'
                       if pendentes else 'Nenhum vetor pendente.'))

        def prog_emb(i, total):
            _set(mensagem=f'Gerando vetores… {i}/{total}')

        feitos = gerar_embeddings(conn, limite=limite_embeddings,
                                  progresso=prog_emb)

        restantes = contar_pendentes(conn)
        if _cancelar.is_set():
            _set(fase='cancelado',
                 mensagem=f'Interrompida: {feitos} vetor(es) gerado(s), '
                          f'{restantes} pendente(s).')
        else:
            _set(fase='concluido',
                 mensagem=(f'Concluído: {feitos} vetor(es) gerado(s).'
                           if feitos else 'Índice já estava atualizado.')
                          + (f' Ainda pendentes: {restantes}.' if restantes else ''))
    except Exception as e:  # noqa: BLE001
        _erro(str(e))
        _set(fase='erro', mensagem=f'Falha na indexação: {e}')
    finally:
        _set(running=False, finished_at=_agora())
        _cancelar.clear()
        conn.close()


def iniciar_em_background(db_path, **kw):
    with _lock:
        if _state['running']:
            return False
    _cancelar.clear()
    threading.Thread(target=rodar, args=(db_path,), kwargs=kw,
                     daemon=True).start()
    return True


def cancelar():
    if _state['running']:
        _cancelar.set()
        return True
    return False


# --------------------------------------------------------------- painel
def estatisticas(conn):
    if not schema.instalado(conn):
        return {'instalado': False}

    modelo, dim = settings.EMBED_MODEL, settings.EMBED_DIM
    um = lambda sql, p=(): conn.execute(sql, p).fetchone()[0]  # noqa: E731

    total_atos = um('SELECT count(*) FROM atos')
    total_chunks = um('SELECT count(*) FROM ato_chunks')
    artigos = um("SELECT count(*) FROM ato_chunks WHERE tipo_chunk = 'artigo'")
    atos_divididos = um('SELECT count(*) FROM ato_index_estado WHERE erro IS NULL')
    indexados = um(
        'SELECT count(*) FROM chunk_embeddings e JOIN ato_chunks c '
        'ON c.id = e.chunk_id AND c.hash = e.hash WHERE e.modelo=? AND e.dim=?',
        (modelo, dim))
    pendentes = max(0, total_chunks - indexados)
    ultima = um('SELECT coalesce(max(criado_em), "") FROM chunk_embeddings')
    atos_com_erro = [
        {'ato_id': r[0], 'erro': r[1]} for r in conn.execute(
            'SELECT ato_id, erro FROM ato_index_estado WHERE erro IS NOT NULL '
            'ORDER BY atualizado_em DESC LIMIT 20')]
    atos_indexaveis = um(
        'SELECT count(DISTINCT ato_id) FROM ato_chunks')

    return {
        'instalado': True,
        'legislacoes': total_atos,
        'legislacoes_divididas': atos_divididos,
        'legislacoes_com_dispositivo': atos_indexaveis,
        'dispositivos': total_chunks,
        'artigos': artigos,
        'indexados': indexados,
        'pendentes': pendentes,
        'cobertura': round(100.0 * indexados / total_chunks, 1) if total_chunks else 0.0,
        'ultima_indexacao': ultima or None,
        'atos_com_erro': atos_com_erro,
        'n_atos_com_erro': len(atos_com_erro),
        'modelo': modelo,
        'dim': dim,
        'busca_semantica_pronta': indexados > 0,
        'chave_configurada': embeddings.disponivel(),
        'chaves': chaves_gemini.em_uso(),
    }
