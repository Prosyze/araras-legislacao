"""Geração e armazenamento dos vetores.

Os vetores são guardados como BLOB float16 já normalizado (L2). float16
custa metade do espaço e, para similaridade de cosseno, a perda é da ordem
do terceiro decimal — irrelevante diante do ruído do próprio ranking. Com
o vetor normalizado, o cosseno vira um produto escalar.
"""
import os
import re
import threading
import time

import numpy as np

from . import settings

_client = None
_client_lock = threading.Lock()


class EmbeddingIndisponivel(RuntimeError):
    """Falta chave ou o serviço recusou de forma definitiva."""


def cliente():
    global _client
    with _client_lock:
        if _client is None:
            from google import genai
            chave = os.environ.get('GEMINI_API_KEY', '').strip()
            if not chave:
                raise EmbeddingIndisponivel(
                    'GEMINI_API_KEY não configurada — a indexação e a busca '
                    'semântica ficam indisponíveis.')
            _client = genai.Client(api_key=chave)
    return _client


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
    for tentativa in range(tentativas):
        _limitador.aguardar(len(textos))
        try:
            resp = cliente().models.embed_content(
                model=settings.EMBED_MODEL, contents=list(textos), config=cfg)
            return [np.asarray(e.values, dtype=np.float32)
                    for e in resp.embeddings]
        except Exception as e:  # noqa: BLE001 — a origem do erro é o serviço
            ultimo = e
            if not _transitorio(e):
                raise
            espera = _espera_sugerida(e, tentativa)
            _limitador.penalizar(espera)
            time.sleep(espera)
    raise ultimo


def embed_documentos(textos):
    """Vetores para indexação. Devolve lista de np.float32 normalizados."""
    if not textos:
        return []
    return [_normalizar(v) for v in _embed(textos, 'RETRIEVAL_DOCUMENT')]


def embed_consulta(texto):
    """Vetor da pergunta. `RETRIEVAL_QUERY` é assimétrico em relação ao
    `RETRIEVAL_DOCUMENT` e melhora sensivelmente o casamento pergunta/norma."""
    return _normalizar(_embed([texto], 'RETRIEVAL_QUERY')[0])


def _normalizar(v):
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def disponivel():
    return bool(os.environ.get('GEMINI_API_KEY', '').strip())
