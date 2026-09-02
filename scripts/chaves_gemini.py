"""Rodizio das chaves do Gemini.

A quota do free tier e por chave, nao por projeto: quando a principal se
esgota, repetir com ela nao resolve -- so passa a valer de novo no dia
seguinte. Com uma reserva configurada, o mesmo pedido segue na hora.

E diferente do reveza-modelo que ja existe para o 503: ali o servico esta
sobrecarregado e a espera resolve, aqui o limite e da credencial. Por isso
so giramos no que outra chave conserta (429/quota e chave recusada), e nao
em sobrecarga do servico.

As reservas vao no ambiente como GEMINI_API_KEY_FALLBACK_1, _2, ... A
varredura para no primeiro numero ausente.
"""
import logging
import os
import threading

# Quota estourada ou credencial recusada: a proxima chave pode dar certo.
# 503/500 nao entram -- sobrecarga do servico atinge todas por igual.
TROCA_DE_CHAVE = ('429', 'RESOURCE_EXHAUSTED', 'quota', 'API_KEY_INVALID',
                  'API key not valid', 'PERMISSION_DENIED')

_log = logging.getLogger(__name__)

# Chamado a cada troca, com a mensagem pronta. Quem estiver rodando um
# trabalho longo aponta para o proprio log -- e como o indexador mostra a
# troca na mesma lista onde ja mostra os erros de lote.
ao_girar = None

_lock = threading.Lock()
_indice = 0
_giros = 0
_cliente = None
_chave_em_uso = None


def chaves():
    """Principal e reservas, na ordem de uso e sem repetir."""
    brutas = [os.environ.get('GEMINI_API_KEY', '')]
    i = 1
    while True:
        reserva = os.environ.get(f'GEMINI_API_KEY_FALLBACK_{i}')
        if reserva is None:
            break
        brutas.append(reserva)
        i += 1

    ordenadas = []
    for chave in brutas:
        chave = (chave or '').strip()
        if chave and chave not in ordenadas:
            ordenadas.append(chave)
    return ordenadas


def configurada():
    return bool(chaves())


def total():
    return len(chaves())


def cliente():
    """Cliente da chave em uso, ou None quando nenhuma esta configurada.

    O cliente e refeito quando a chave muda; fora isso e reaproveitado.
    """
    global _cliente, _chave_em_uso
    with _lock:
        disponiveis = chaves()
        if not disponiveis:
            return None
        chave = disponiveis[_indice % len(disponiveis)]
        if _cliente is None or _chave_em_uso != chave:
            from google import genai
            _cliente = genai.Client(api_key=chave)
            _chave_em_uso = chave
        return _cliente


def vale_trocar(e):
    """O erro e do tipo que outra chave pode resolver?"""
    s = str(e)
    return any(marca in s for marca in TROCA_DE_CHAVE)


def em_uso():
    """Qual chave esta valendo agora. So para diagnostico -- da o final da
    chave, que identifica sem expor o segredo."""
    disponiveis = chaves()
    if not disponiveis:
        return {'total': 0, 'atual': 0, 'final': None, 'giros': _giros}
    i = _indice % len(disponiveis)
    return {'total': len(disponiveis), 'atual': i + 1,
            'final': disponiveis[i][-4:], 'giros': _giros}


def girar():
    """Passa a usar a proxima chave. False quando so existe uma.

    O giro e circular: a chave esgotada hoje volta a valer amanha, e quem
    chama limita quantas voltas da (ver `girar_no_erro`).
    """
    global _indice, _giros, _cliente
    with _lock:
        disponiveis = chaves()
        if len(disponiveis) < 2:
            return False
        antiga = _indice % len(disponiveis) + 1
        _indice += 1
        _giros += 1
        _cliente = None
        msg = (f'chave {antiga} sem cota; seguindo na chave '
               f'{_indice % len(disponiveis) + 1} de {len(disponiveis)}')
    # Fora do lock: o gancho e de quem chama e pode pegar outro lock.
    _log.warning(msg)
    if ao_girar:
        ao_girar(msg)
    return True


def girar_no_erro(e, giros_ja_dados):
    """Decide, no except do chamador, se vale repetir com outra chave.

    Devolve True depois de trocar. O teto de giros e o numero de chaves
    menos a que ja falhou, entao um erro que todas compartilham termina em
    excecao em vez de rodar em circulo.
    """
    if giros_ja_dados >= total() - 1:
        return False
    return vale_trocar(e) and girar()
