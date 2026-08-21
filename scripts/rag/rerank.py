"""Reordenação e escolha do contexto.

A fusão devolve dezenas de candidatos plausíveis; o modelo só deve ver os
melhores. Este passo é deliberadamente sem LLM por padrão — os sinais que
importam (o dispositivo foi citado? o termo aparece literalmente? é caput ou
ementa?) são verificáveis de graça, e gastar uma chamada de modelo para
reordenar dobra o custo de cada pergunta.

`rerank_llm` existe para quem quiser ligar (RAG_RERANK_LLM=1), mas não é o
caminho padrão.
"""
import json
import re

from . import settings
from .parser import STOPWORDS, sem_acento

TOKEN_RE = re.compile(r'[a-z0-9\-]{3,}')

# Peso por tipo de dispositivo. O artigo é onde mora a regra; a ementa serve
# para identificar a norma, mas raramente responde sozinha.
PESO_TIPO = {'artigo': 1.0, 'cabecalho': 0.72, 'anexo': 0.62, 'trecho': 0.7}


def _tokens(texto):
    return {t for t in TOKEN_RE.findall(sem_acento(texto or '').lower())
            if t not in STOPWORDS}


def _cobertura(termos, texto):
    if not termos:
        return 0.0
    alvo = sem_acento(texto or '').lower()
    achados = sum(1 for t in termos if t in alvo)
    return achados / len(termos)


def _normalizar(valores):
    if not valores:
        return []
    lo, hi = min(valores), max(valores)
    if hi - lo < 1e-9:
        return [0.5] * len(valores)
    return [(v - lo) / (hi - lo) for v in valores]


def pontuar(candidatos, consulta, atos_referenciados=()):
    """Score final de cada candidato.

    A relevância estimada ocupa a faixa [0, ~1]. Acima dela existe um
    patamar reservado ao dispositivo pedido nominalmente: quem escreve
    "art. 12 da Lei 5.349" quer aquele artigo, e ele não deve disputar
    posição com um trecho que a busca semântica achou parecido.
    """
    if not candidatos:
        return []

    refs = set(atos_referenciados or ())
    arts = set(consulta.artigos or ())
    incs = {i.upper() for i in (consulta.incisos or ())}
    pars = set(consulta.paragrafos or ())

    rrf = _normalizar([c.get('score_rrf', 0.0) for c in candidatos])
    cos = _normalizar([c.get('_cos', 0.0) for c in candidatos])
    tem_cos = any('_cos' in c for c in candidatos)

    for i, c in enumerate(candidatos):
        base = 0.55 * rrf[i] + (0.25 * cos[i] if tem_cos else 0.0)
        lexical = _cobertura(consulta.termos, c.get('texto'))
        base += 0.20 * lexical

        base *= PESO_TIPO.get(c.get('tipo_chunk'), 0.7)

        # Âncoras explícitas da pergunta.
        if c['ato_id'] in refs:
            base += 0.28
        if arts and c.get('artigo') in arts:
            # Dentro da norma citada (ou sem norma citada) o artigo pedido
            # sobe de patamar; em outra norma vale só como indício.
            base += 1.0 if (not refs or c['ato_id'] in refs) else 0.35
        if incs and c.get('incisos'):
            if incs & {x.strip().upper() for x in c['incisos'].split(',')}:
                base += 0.12
        if pars and c.get('paragrafo'):
            if pars & {x.strip() for x in c['paragrafo'].split(',')}:
                base += 0.08

        # Trecho minúsculo quase nunca sustenta uma resposta.
        n = c.get('n_chars') or len(c.get('texto') or '')
        if n < 90:
            base *= 0.75

        c['score'] = round(base, 5)
        c['sinais'] = {'rrf': round(rrf[i], 3), 'cos': round(cos[i], 3),
                       'lexical': round(lexical, 3)}

    return sorted(candidatos, key=lambda d: -d['score'])


def _similaridade(a, b):
    ta, tb = a.get('_tok'), b.get('_tok')
    if ta is None:
        ta = a['_tok'] = _tokens(a.get('texto'))
    if tb is None:
        tb = b['_tok'] = _tokens(b.get('texto'))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def selecionar(candidatos, consulta, k=None, max_por_ato=None,
               max_chars=None, atos_prioritarios=()):
    """MMR com teto por norma e orçamento de caracteres.

    O teto por norma existe porque uma lei extensa domina o ranking inteiro e
    o modelo acaba sem enxergar as outras normas que respondem a pergunta.
    """
    k = k or settings.TOP_CONTEXTO
    max_por_ato = max_por_ato or settings.MAX_POR_ATO
    max_chars = max_chars or settings.MAX_CHARS_CONTEXTO

    elegiveis = [c for c in candidatos if c.get('score', 0) >= settings.SCORE_MINIMO]
    if not elegiveis:
        elegiveis = candidatos[:k]

    # Comparação entre normas: reserva vaga para cada uma antes de deixar o
    # score decidir, senão a norma mais "falante" leva todas as vagas.
    cotas = {}
    if consulta.comparativa and len(atos_prioritarios) > 1:
        por_norma = max(1, k // len(atos_prioritarios))
        cotas = {a: por_norma for a in atos_prioritarios}

    escolhidos, usados_por_ato, chars = [], {}, 0

    def cabe(c):
        if usados_por_ato.get(c['ato_id'], 0) >= max_por_ato:
            return False
        return chars + len(c['texto']) <= max_chars or not escolhidos

    # 1ª passada: cotas das normas comparadas.
    for ato_id, cota in cotas.items():
        for c in elegiveis:
            if len(escolhidos) >= k or cota <= 0:
                break
            if c['ato_id'] != ato_id or c in escolhidos or not cabe(c):
                continue
            escolhidos.append(c)
            usados_por_ato[ato_id] = usados_por_ato.get(ato_id, 0) + 1
            chars += len(c['texto'])
            cota -= 1

    # 2ª passada: MMR sobre o restante.
    while len(escolhidos) < k:
        melhor, melhor_v = None, None
        for c in elegiveis:
            if c in escolhidos or not cabe(c):
                continue
            redundancia = max((_similaridade(c, e) for e in escolhidos),
                              default=0.0)
            valor = ((1 - settings.MMR_LAMBDA) * c.get('score', 0)
                     - settings.MMR_LAMBDA * redundancia)
            if melhor_v is None or valor > melhor_v:
                melhor, melhor_v = c, valor
        if melhor is None:
            break
        escolhidos.append(melhor)
        usados_por_ato[melhor['ato_id']] = usados_por_ato.get(melhor['ato_id'], 0) + 1
        chars += len(melhor['texto'])

    # A ordem final vira a numeração das fontes ([F1], [F2]...), então o
    # dispositivo mais relevante deve ser o F1.
    escolhidos.sort(key=lambda c: -c.get('score', 0))
    for c in escolhidos:
        c.pop('_tok', None)
    return escolhidos


# ------------------------------------------------------------------ opcional
PROMPT_RERANK = """Você recebe uma PERGUNTA e uma lista de TRECHOS de legislação.
Escolha no máximo {k} trechos que realmente ajudam a responder.

Responda APENAS com um array JSON de números, do mais para o menos relevante.
Exemplo: [3, 1, 7]

PERGUNTA: {pergunta}

TRECHOS:
{trechos}"""


def rerank_llm(candidatos, consulta, k=None, gerar=None):
    """Reordenação por modelo. Desligada por padrão: custa uma chamada extra
    por pergunta e, nos testes com este acervo, mudou pouco o resultado."""
    k = k or settings.TOP_CONTEXTO
    if not candidatos or gerar is None:
        return candidatos

    trechos = '\n'.join(
        f'[{i + 1}] {c.get("titulo", "")} — {c.get("rotulo") or ""}: '
        f'{(c.get("texto") or "")[:400]}'
        for i, c in enumerate(candidatos))
    prompt = PROMPT_RERANK.format(k=k, pergunta=consulta.pergunta,
                                  trechos=trechos)
    try:
        bruto = gerar(prompt)
        nums = json.loads(re.search(r'\[.*?\]', bruto, re.S).group(0))
        ordem = [n - 1 for n in nums if isinstance(n, int)
                 and 1 <= n <= len(candidatos)]
    except Exception:  # noqa: BLE001 — resposta fora do formato: mantém a ordem
        return candidatos

    escolhidos = [candidatos[i] for i in dict.fromkeys(ordem)]
    resto = [c for c in candidatos if c not in escolhidos]
    return escolhidos + resto
