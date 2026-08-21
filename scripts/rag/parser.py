"""Interpretação da pergunta antes de qualquer busca.

Boa parte das perguntas jurídicas carrega uma âncora estruturada — "Lei
5.349", "art. 12", "decretos de 2021". Extrair isso antes vale mais do que
qualquer ajuste de similaridade: transforma uma busca aproximada num filtro
exato, e é de graça.
"""
import re
import unicodedata

TIPOS = {
    'lei complementar': 'LEI COMPLEMENTAR',
    'leis complementares': 'LEI COMPLEMENTAR',
    'decreto legislativo': 'DECRETO LEGISLATIVO',
    'decretos legislativos': 'DECRETO LEGISLATIVO',
    'lei': 'LEI',
    'leis': 'LEI',
    'decreto': 'DECRETO',
    'decretos': 'DECRETO',
}

REF_RE = re.compile(
    r'\b(leis?\s+complementar(?:es)?|decretos?\s+legislativos?|leis?|decretos?)\b'
    r'(?:\s+(?:municipal|municipais|federal|federais|estadual|estaduais))?'
    r'\s*(?:n[º°o]?\.?)?\s*[:\-]?\s*'
    r'(\d{1,3}(?:[.\s]\d{3})*|\d{1,6})'
    r'(?:\s*[/\-]\s*(\d{2,4}))?',
    re.IGNORECASE)

TIPO_SOLTO_RE = re.compile(
    r'\b(leis?\s+complementar(?:es)?|decretos?\s+legislativos?|leis?|decretos?)\b',
    re.IGNORECASE)

ARTIGO_RE = re.compile(
    r'\bart(?:igos?)?\.?\s*(\d{1,4})\s*[º°o]?(?:\s*[-–]\s*([A-Za-z])\b)?',
    re.IGNORECASE)
INCISO_RE = re.compile(r'\bincisos?\s+([IVXLC]{1,7})\b', re.IGNORECASE)
PARAGRAFO_RE = re.compile(
    r'(?:§\s*(\d{1,3})|par[áa]grafos?\s+(?:(\d{1,3})[º°o]?|([úu]nico)))',
    re.IGNORECASE)
ANO_RE = re.compile(r'\b(?:de|em|do ano de|ano)\s+((?:19|20)\d{2})\b', re.IGNORECASE)
ANO_SOLTO_RE = re.compile(r'\b((?:19|20)\d{2})\b')

# Sinais de que a pergunta não se sustenta sozinha e precisa do turno anterior.
CONECTIVO_RE = re.compile(r'^\s*(e|mas|ent[ãa]o|ok|certo|t[áa])[\s,]', re.IGNORECASE)
ANAFORA_RE = re.compile(
    r'(isso|isto|aquilo|ele|ela|eles|elas|dele|dela|deles|delas|nele|nela|'
    r'nesse caso|neste caso|o mesmo|a mesma|essa norma|esse artigo|essa lei|'
    r'esse decreto|a[ií])', re.IGNORECASE)

COMPARATIVA_RE = re.compile(
    r'\b(compar\w*|diferen[çc]a?s?|distin[çc]\w*|confront\w*|versus|vs\.?|'
    r'em rela[çc][ãa]o a|frente a)\b', re.IGNORECASE)

# Pergunta que é só uma citação: merece resposta direta, sem LLM.
SO_CITACAO_RE = re.compile(
    r'^\s*(?:me\s+)?(?:mostr\w+|ver|leia|le|abrir?|qual\s+(?:é|e)?\s*(?:o|a)?\s*'
    r'(?:texto|teor|conte[úu]do|reda[çc][ãa]o)\s*(?:d[oae]s?)?|texto\s+d[oae]|'
    r'teor\s+d[oae]|conte[úu]do\s+d[oae])?\s*',
    re.IGNORECASE)

STOPWORDS = {
    'a', 'as', 'o', 'os', 'um', 'uma', 'uns', 'umas', 'de', 'do', 'da', 'dos',
    'das', 'em', 'no', 'na', 'nos', 'nas', 'por', 'para', 'pra', 'com', 'sem',
    'sobre', 'que', 'qual', 'quais', 'quando', 'quanto', 'quantos', 'quantas',
    'como', 'onde', 'porque', 'e', 'ou', 'se', 'ao', 'aos', 'à', 'às', 'pelo',
    'pela', 'pelos', 'pelas', 'ser', 'sao', 'são', 'é', 'e', 'foi', 'ha', 'há',
    'existe', 'existem', 'tem', 'têm', 'ter', 'diz', 'fala', 'trata', 'trata-se',
    'me', 'meu', 'minha', 'eu', 'voce', 'você', 'isso', 'esse', 'essa', 'este',
    'esta', 'aquele', 'aquela', 'seu', 'sua', 'seus', 'suas', 'mais', 'menos',
    'muito', 'pode', 'posso', 'podem', 'deve', 'devem', 'preciso', 'algum',
    'alguma', 'alguns', 'algumas', 'nenhum', 'nenhuma', 'todo', 'toda', 'todos',
    'todas', 'entre', 'ate', 'até', 'apenas', 'so', 'só', 'tambem', 'também',
    'nao', 'não', 'sim', 'favor', 'por favor', 'quero', 'saber', 'gostaria',
}


def sem_acento(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s or '')
                   if unicodedata.category(c) != 'Mn')


def normalizar(s):
    return re.sub(r'\s+', ' ', sem_acento(s or '').lower()).strip()


def _numero_limpo(bruto):
    return re.sub(r'[.\s]', '', bruto or '')


def _ano4(bruto):
    if not bruto:
        return None
    n = int(bruto)
    if n < 100:
        n += 2000 if n < 50 else 1900
    return n if 1900 <= n <= 2100 else None


class Consulta:
    """Resultado da interpretação. É só um saco de fatos sobre a pergunta."""

    def __init__(self, pergunta):
        self.pergunta = pergunta
        self.normalizada = normalizar(pergunta)
        self.referencias = []   # [{'tipo','numero','ano','texto'}]
        self.artigos = []       # ['75', '12-A']
        self.incisos = []       # ['II']
        self.paragrafos = []    # ['1', 'unico']
        self.tipos = []         # ['DECRETO'] quando citado sem número
        self.anos = []          # [2021]
        self.comparativa = False
        self.so_citacao = False
        self.termos = []

    @property
    def tem_ancora(self):
        return bool(self.referencias or self.artigos or self.tipos or self.anos)

    def filtros_visiveis(self):
        """Versão enxuta para exibir na interface e gravar no log."""
        d = {}
        if self.referencias:
            d['normas'] = [
                ' '.join(filter(None, [r['tipo'] or '', f'nº {r["numero"]}',
                                       f'/{r["ano"]}' if r['ano'] else '']))
                for r in self.referencias]
        if self.artigos:
            d['artigos'] = self.artigos
        if self.incisos:
            d['incisos'] = self.incisos
        if self.paragrafos:
            d['paragrafos'] = self.paragrafos
        if self.tipos:
            d['tipos'] = self.tipos
        if self.anos:
            d['anos'] = self.anos
        if self.comparativa:
            d['comparativa'] = True
        return d

    def to_dict(self):
        return {
            'referencias': self.referencias, 'artigos': self.artigos,
            'incisos': self.incisos, 'paragrafos': self.paragrafos,
            'tipos': self.tipos, 'anos': self.anos,
            'comparativa': self.comparativa, 'so_citacao': self.so_citacao,
            'termos': self.termos,
        }


def _tipo_de(rotulo):
    return TIPOS.get(normalizar(rotulo), None)


def interpretar(pergunta):
    c = Consulta(pergunta)
    texto = pergunta or ''

    consumido = []
    for m in REF_RE.finditer(texto):
        numero = _numero_limpo(m.group(2))
        if not numero:
            continue
        ano = _ano4(m.group(3))
        c.referencias.append({
            'tipo': _tipo_de(m.group(1)),
            'numero': numero,
            'ano': ano,
            'texto': m.group(0).strip(),
        })
        consumido.append((m.start(), m.end()))

    # Tipo citado sem número ("quais decretos municipais tratam de…").
    if not c.referencias:
        for m in TIPO_SOLTO_RE.finditer(texto):
            t = _tipo_de(m.group(1))
            if t and t not in c.tipos:
                c.tipos.append(t)

    for m in ARTIGO_RE.finditer(texto):
        if any(ini <= m.start() < fim for ini, fim in consumido):
            continue
        num = str(int(m.group(1)))
        if m.group(2):
            num = f'{num}-{m.group(2).upper()}'
        if num not in c.artigos:
            c.artigos.append(num)

    for m in INCISO_RE.finditer(texto):
        v = m.group(1).upper()
        if v not in c.incisos:
            c.incisos.append(v)

    for m in PARAGRAFO_RE.finditer(texto):
        v = m.group(1) or m.group(2) or ('unico' if m.group(3) else None)
        if v and str(v) not in c.paragrafos:
            c.paragrafos.append(sem_acento(str(v)).lower())

    anos = {a for a in (_ano4(m.group(1)) for m in ANO_RE.finditer(texto)) if a}
    # Ano já capturado como parte de "Lei 5.349/2020" não vira filtro solto.
    anos_ref = {r['ano'] for r in c.referencias if r['ano']}
    c.anos = sorted(anos - anos_ref)

    c.comparativa = bool(COMPARATIVA_RE.search(texto)) or len(c.referencias) > 1

    c.termos = termos_uteis(texto)
    c.so_citacao = _e_so_citacao(texto, c)
    return c


def termos_uteis(texto):
    """Palavras que valem busca textual: sem acento, sem stopword, sem o
    vocabulário de citação que aparece em toda pergunta."""
    limpo = normalizar(texto)
    limpo = re.sub(r'\b(art|artigo|artigos|inciso|incisos|paragrafo|'
                   r'paragrafos|lei|leis|decreto|decretos|complementar|'
                   r'complementares|legislativo|legislativos|municipal|'
                   r'municipais|n|no|numero)\b', ' ', limpo)
    limpo = re.sub(r'\b\d+\b', ' ', limpo)
    palavras = re.findall(r'[a-z0-9\-]{3,}', limpo)
    return [p for p in dict.fromkeys(palavras) if p not in STOPWORDS]


def _e_so_citacao(texto, c):
    """True quando a pergunta não pede análise: quer só o dispositivo."""
    if not c.referencias and not c.artigos:
        return False
    if c.comparativa or c.termos:
        return False
    resto = REF_RE.sub(' ', texto)
    resto = ARTIGO_RE.sub(' ', resto)
    resto = INCISO_RE.sub(' ', resto)
    resto = PARAGRAFO_RE.sub(' ', resto)
    resto = SO_CITACAO_RE.sub(' ', resto, count=1)
    resto = re.sub(r'[^\w]+', ' ', normalizar(resto)).strip()
    sobra = [p for p in resto.split() if p not in STOPWORDS]
    return not sobra


def reescrever_com_historico(pergunta, historico):
    """Pergunta curta e dependente ("e quais documentos?") só faz sentido
    junto da anterior. Em vez de gastar uma chamada de LLM para reescrever,
    concatena as âncoras do último turno do usuário — barato e suficiente
    para a recuperação, que é onde o contexto faz falta."""
    if not historico:
        return pergunta
    c = interpretar(pergunta)

    # Contar palavras não serve: "Quais normas tratam do transporte escolar?"
    # é curta e completa. O que denuncia dependência é abrir com conectivo,
    # usar anáfora ("e quanto a ela?") ou não ter conteúdo próprio nenhum.
    dependente = bool(CONECTIVO_RE.match(pergunta or '')
                      or ANAFORA_RE.search(pergunta or '')
                      or len(c.termos) <= 1)
    if c.tem_ancora or not dependente:
        return pergunta

    anterior = next((m['conteudo'] for m in reversed(historico)
                     if m.get('papel') == 'user'), None)
    if not anterior:
        return pergunta
    ca = interpretar(anterior)
    ancoras = [r['texto'] for r in ca.referencias] + ca.termos[:6]
    if not ancoras:
        return pergunta
    return f'{pergunta} (no contexto de: {" ".join(ancoras)})'
