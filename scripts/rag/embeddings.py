"""Geração e armazenamento dos vetores.

Os vetores são guardados como BLOB float16 já normalizado (L2). float16
custa metade do espaço e, para similaridade de cosseno, a perda é da ordem
do terceiro decimal — irrelevante diante do ruído do próprio ranking. Com
o vetor normalizado, o cosseno vira um produto escalar.

Dois backends, escolhidos por `settings.EMBED_BACKEND`:
  gemini — API do Google, usado em produção (free tier: 100 req/min).
  local  — sentence-transformers rodando na própria CPU, sem rede nem
           quota; pensado para a carga inicial de um acervo grande.
"""
import os
import re
import threading
import time

import numpy as np

import chaves_gemini

from . import settings

_modelo_local = None
_modelo_local_lock = threading.Lock()


class EmbeddingIndisponivel(RuntimeError):
    """Falta chave/modelo ou o serviço recusou de forma definitiva."""


def cliente():
    cli = chaves_gemini.cliente()
    if cli is None:
        raise EmbeddingIndisponivel(
            'GEMINI_API_KEY não configurada — a indexação e a busca '
            'semântica ficam indisponíveis.')
    return cli


# ----------------------------------------------------------------- local
def _modelo():
    global _modelo_local
    with _modelo_local_lock:
        if _modelo_local is None:
            import torch
            from sentence_transformers import SentenceTransformer
            torch.set_num_threads(os.cpu_count() or 4)
            _modelo_local = SentenceTransformer(settings.EMBED_MODEL)
    return _modelo_local


def _embed_local(textos, prefixo):
    """multilingual-e5 é assimétrico por convenção de prefixo, não de
    task_type como o Gemini: 'passage: ' para o que entra no índice e
    'query: ' para a pergunta."""
    vetores = _modelo().encode(
        [f'{prefixo}{t}' for t in textos], batch_size=settings.EMBED_BATCH,
        normalize_embeddings=True, show_progress_bar=False)
    return [np.asarray(v, dtype=np.float32) for v in vetores]


# ----------------------------------------------------------------- serialização
def para_blob(vetor):
    """np.float32 normalizado -> bytes float16."""
    v = np.asarray(vetor, dtype=np.float32)
    n = float(np.linalg.norm(v))
    if n > 0:
        v = v / n
    return v.astype(np.float16).tobytes()


def de_blob(blob):
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32)


# ----------------------------------------------------------------- rate limit
class _Limitador:
    """Janela deslizante de 60 s. A quota do Gemini conta cada item do lote
    como uma requisição, então quem entra na conta é o número de textos."""

    def __init__(self, rpm):
        self.rpm = max(1, rpm)
        self.marcas = []
        self.lock = threading.Lock()
        self.bloqueado_ate = 0.0

    def aguardar(self, n):
        while True:
            with self.lock:
                agora = time.time()
                if agora < self.bloqueado_ate:
                    espera = self.bloqueado_ate - agora
                else:
                    self.marcas = [t for t in self.marcas if agora - t < 60.0]
                    if len(self.marcas) + n <= self.rpm:
                        self.marcas.extend([agora] * n)
                        return
                    espera = 60.0 - (agora - self.marcas[0]) + 0.05
            time.sleep(min(max(espera, 0.05), 65.0))

    def penalizar(self, segundos):
        with self.lock:
            self.bloqueado_ate = max(self.bloqueado_ate, time.time() + segundos)
            self.marcas.clear()

    def reiniciar(self):
        """Zera a janela. A quota por minuto e contada por chave, entao ao
        trocar de chave a conta antiga nao vale mais."""
        with self.lock:
            self.marcas.clear()
            self.bloqueado_ate = 0.0


_limitador = _Limitador(settings.EMBED_RPM)


RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s")
TRANSITORIO = ('429', 'RESOURCE_EXHAUSTED', '503', 'UNAVAILABLE', '500',
               'INTERNAL', 'overloaded', 'high demand', 'DEADLINE_EXCEEDED')


def _transitorio(e):
    s = str(e)
    return any(c in s for c in TRANSITORIO)


def _espera_sugerida(e, tentativa):
    m = RETRY_DELAY_RE.search(str(e))
    if m:
        return min(float(m.group(1)) + 1.0, 120.0)
    return min(2.0 * (2 ** tentativa), 60.0)


# ----------------------------------------------------------------- chamadas
def _embed(textos, task_type, tentativas=5):
    from google.genai import types

    cfg = types.EmbedContentConfig(
        task_type=task_type, output_dimensionality=settings.EMBED_DIM)

    ultimo = None
    giros = 0
    tentativa = 0
    while tentativa < tentativas:
        _limitador.aguardar(len(textos))
        try:
            resp = cliente().models.embed_content(
                model=settings.EMBED_MODEL, contents=list(textos), config=cfg)
            return [np.asarray(e.values, dtype=np.float32)
                    for e in resp.embeddings]
        except Exception as e:  # noqa: BLE001 — a origem do erro é o serviço
            ultimo = e
            # Quota é da chave: com outra, o lote segue na hora. Nem espera
            # nem gasta tentativa, e a janela do limitador recomeça.
            if chaves_gemini.girar_no_erro(e, giros):
                giros += 1
                _limitador.reiniciar()
                continue
            if not _transitorio(e):
                raise
            espera = _espera_sugerida(e, tentativa)
            _limitador.penalizar(espera)
            time.sleep(espera)
            tentativa += 1
    raise ultimo


def embed_documentos(textos):
    """Vetores para indexação. Devolve lista de np.float32 normalizados."""
    if not textos:
        return []
    if settings.EMBED_BACKEND == 'local':
        return _embed_local(textos, 'passage: ')
    return [_normalizar(v) for v in _embed(textos, 'RETRIEVAL_DOCUMENT')]


def embed_consulta(texto):
    """Vetor da pergunta. Assimétrico em relação ao vetor de documento
    (task_type no Gemini, prefixo no e5) — melhora o casamento pergunta/norma."""
    if settings.EMBED_BACKEND == 'local':
        return _embed_local([texto], 'query: ')[0]
    return _normalizar(_embed([texto], 'RETRIEVAL_QUERY')[0])


def _normalizar(v):
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def disponivel():
    if settings.EMBED_BACKEND == 'local':
        return True
    return chaves_gemini.configurada()
