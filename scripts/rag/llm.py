"""Chamada ao modelo generativo.

Mesma estratégia já usada no resumo de atos: sobrecarga do Gemini (503/429)
é pico de demanda, não erro do pedido, então vale repetir com backoff e cair
para um modelo reserva antes de desistir.
"""
import os
import threading
import time

from . import settings

_client = None
_lock = threading.Lock()

TRANSITORIO = ('503', 'UNAVAILABLE', '429', 'RESOURCE_EXHAUSTED', 'overloaded',
               'high demand', '500', 'INTERNAL', 'DEADLINE_EXCEEDED')


class LLMIndisponivel(RuntimeError):
    pass


def disponivel():
    return bool(os.environ.get('GEMINI_API_KEY', '').strip())


def cliente():
    global _client
    with _lock:
        if _client is None:
            from google import genai
            chave = os.environ.get('GEMINI_API_KEY', '').strip()
            if not chave:
                raise LLMIndisponivel('GEMINI_API_KEY não configurada.')
            _client = genai.Client(api_key=chave)
    return _client


def transitorio(e):
    s = str(e)
    return any(c in s for c in TRANSITORIO)


def mensagem_amigavel(e):
    if isinstance(e, LLMIndisponivel):
        return str(e)
    if transitorio(e):
        return ('O serviço de IA está sobrecarregado no momento. '
                'Tente novamente em alguns instantes.')
    return f'Falha ao consultar o modelo: {e}'


def gerar(prompt, temperatura=None, modelos=None, tentativas=3):
    """Texto do modelo. Levanta a última exceção se todos falharem."""
    from google.genai import types

    cfg = types.GenerateContentConfig(
        temperature=settings.CHAT_TEMPERATURA if temperatura is None
        else temperatura)
    candidatos = modelos or (settings.CHAT_MODEL, *settings.CHAT_MODELOS_RESERVA)

    ultimo = None
    for modelo in dict.fromkeys(candidatos):
        for tentativa in range(tentativas):
            try:
                resp = cliente().models.generate_content(
                    model=modelo, contents=prompt, config=cfg)
                return (resp.text or '').strip()
            except Exception as e:  # noqa: BLE001
                ultimo = e
                if not transitorio(e):
                    raise
                time.sleep(1.5 * (2 ** tentativa))
    raise ultimo
