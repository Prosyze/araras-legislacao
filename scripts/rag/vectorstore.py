"""Índice vetorial em memória sobre a tabela `chunk_embeddings`.

Com ~28 mil dispositivos, força bruta é a escolha certa: o produto
matriz-vetor custa poucos milissegundos e não traz nenhuma dependência
nativa nova (nada de pgvector, FAISS ou extensão de SQLite para carregar).

A matriz fica em float16 na memória e é convertida por blocos na hora da
conta — segura ~43 MB em vez de 86 MB, o que importa num container pequeno.
"""
import threading

import numpy as np

from . import settings

_BLOCO = 4096

_cache = {'chave': None, 'ids': None, 'mat': None, 'versao': None}
_lock = threading.Lock()


def versao_indice(conn):
    """Assinatura barata do índice; muda quando algo é (re)indexado."""
    row = conn.execute(
        'SELECT count(*), coalesce(max(chunk_id), 0), coalesce(max(criado_em), "") '
        'FROM chunk_embeddings WHERE modelo = ? AND dim = ?',
        (settings.EMBED_MODEL, settings.EMBED_DIM)).fetchone()
    return tuple(row)


def invalidar():
    with _lock:
        _cache.update(chave=None, ids=None, mat=None, versao=None)


def _carregar(conn):
    chave = (settings.EMBED_MODEL, settings.EMBED_DIM)
    versao = versao_indice(conn)
    if _cache['chave'] == chave and _cache['versao'] == versao:
        return _cache['ids'], _cache['mat']

    n = versao[0]
    if not n:
        ids = np.zeros(0, dtype=np.int64)
        mat = np.zeros((0, settings.EMBED_DIM), dtype=np.float16)
    else:
        ids = np.empty(n, dtype=np.int64)
        mat = np.empty((n, settings.EMBED_DIM), dtype=np.float16)
        i = 0
        for chunk_id, blob in conn.execute(
                'SELECT chunk_id, vetor FROM chunk_embeddings '
                'WHERE modelo = ? AND dim = ? ORDER BY chunk_id',
                (settings.EMBED_MODEL, settings.EMBED_DIM)):
            v = np.frombuffer(blob, dtype=np.float16)
            if v.shape[0] != settings.EMBED_DIM:
                continue  # vetor de outra dimensão: ignorado até ser refeito
            ids[i] = chunk_id
            mat[i] = v
            i += 1
        ids, mat = ids[:i], mat[:i]

    _cache.update(chave=chave, ids=ids, mat=mat, versao=versao)
    return ids, mat


def carregado():
    return _cache['mat'] is not None


def tamanho():
    return 0 if _cache['mat'] is None else int(_cache['mat'].shape[0])


def buscar(conn, vetor, k, permitidos=None):
    """Top-k por cosseno. `permitidos` restringe a busca a um conjunto de
    chunk_ids (usado quando a pergunta cita uma norma específica).

    Devolve [(chunk_id, score)] em ordem decrescente.
    """
    with _lock:
        ids, mat = _carregar(conn)

    if ids.shape[0] == 0 or k <= 0:
        return []

    q = np.asarray(vetor, dtype=np.float32)
    n = float(np.linalg.norm(q))
    if n > 0:
        q = q / n

    if permitidos is not None:
        permitidos = np.fromiter(permitidos, dtype=np.int64)
        if permitidos.size == 0:
            return []
        mask = np.isin(ids, permitidos)
        if not mask.any():
            return []
        ids_busca, mat_busca = ids[mask], mat[mask]
    else:
        ids_busca, mat_busca = ids, mat

    total = mat_busca.shape[0]
    scores = np.empty(total, dtype=np.float32)
    for ini in range(0, total, _BLOCO):
        fim = min(ini + _BLOCO, total)
        scores[ini:fim] = mat_busca[ini:fim].astype(np.float32) @ q

    k = min(k, total)
    topo = np.argpartition(-scores, k - 1)[:k]
    topo = topo[np.argsort(-scores[topo])]
    return [(int(ids_busca[i]), float(scores[i])) for i in topo]
