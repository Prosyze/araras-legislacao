"""Chamada ao modelo generativo.

Mesma estratégia já usada no resumo de atos: sobrecarga do Gemini (503) é
pico de demanda, não erro do pedido, então vale repetir com backoff e cair
para um modelo reserva antes de desistir. Quota estourada (429) é limite da
credencial, não do serviço, e sai antes por outro caminho: `chaves_gemini`
troca a chave e o pedido segue sem espera.
"""
import time

import chaves_gemini

from . import settings

TRANSITORIO = ('503', 'UNAVAILABLE', '429', 'RESOURCE_EXHAUSTED', 'overloaded',
               'high demand', '500', 'INTERNAL', 'DEADLINE_EXCEEDED')


class LLMIndisponivel(RuntimeError):
    pass


def disponivel():
    return chaves_gemini.configurada()


def cliente():
    cli = chaves_gemini.cliente()
    if cli is None:
        raise LLMIndisponivel('GEMINI_API_KEY não configurada.')
    return cli


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
    giros = 0
    for modelo in dict.fromkeys(candidatos):
        tentativa = 0
        while tentativa < tentativas:
            try:
                resp = cliente().models.generate_content(
                    model=modelo, contents=prompt, config=cfg)
                return (resp.text or '').strip()
            except Exception as e:  # noqa: BLE001
                ultimo = e
                # Quota e limite da chave: outra chave resolve na hora, e a
                # tentativa continua valendo para uma falha de verdade.
                if chaves_gemini.girar_no_erro(e, giros):
                    giros += 1
                    continue
                if not transitorio(e):
                    raise
                time.sleep(1.5 * (2 ** tentativa))
                tentativa += 1
    raise ultimo
