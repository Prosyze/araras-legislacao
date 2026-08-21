import re
from datetime import date

import fitz

TOC_LINE = re.compile(r'^(.+?)\.{2,}\s*(\d+)\s*$')
PAGINA_RE = re.compile(r'P[aá]gina\s+(\d+)')

MESES = {
    'JANEIRO': 1, 'FEVEREIRO': 2, 'MARÇO': 3, 'MARCO': 3, 'ABRIL': 4, 'MAIO': 5,
    'JUNHO': 6, 'JULHO': 7, 'AGOSTO': 8, 'SETEMBRO': 9, 'OUTUBRO': 10,
    'NOVEMBRO': 11, 'DEZEMBRO': 12,
}

DATE_IN_TITLE = re.compile(
    # Tolerates the real-world variations seen in the diaries:
    # "DE 1º. DE ABRIL DE 2020", ", 16 DE MAIO DE 2022", "DE 12 DE JUNHO 2.024".
    r'(\d{1,2})\s*[º°o]?\.?\s+DE\s+([A-ZÇÃÕÉ]+)\s*,?\s+(?:DE\s+)?(\d[\d\.]{3,5})',
    re.IGNORECASE
)
DATE_NUMERIC = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b')
NUMERO_IN_TITLE = re.compile(r'N[º°o]?\.?\s*[:\-]?\s*([\d\.]+(?:/\d{2,4})?)')


# O numero tem de vir logo apos o tipo do ato. Sem isso, titulos como
# "LEI FISCAL - RELATORIO DO 2o BIMESTRE" ou "LEI DE RESPONSABILIDADE FISCAL"
# (relatorios contabeis, nao normas) entrariam como lei.
# Tolera a pontuacao errada que aparece na fonte ("DECRETO Nº, 6.580",
# "DECRETO - LEGISLATIVO Nº 13"), mas nao deixa passar palavra no lugar do
# numero.
TITULO_NUMERADO = re.compile(
    r'^(LEI|DECRETO)[\s\-–]*((COMPLEMENTAR|LEGISLATIVO|MUNICIPAL)[\s\-–]*)?'
    r'N?[º°o\.,:\s\-–]*\d', re.IGNORECASE)


def classify(title):
    t = title.strip().upper()
    if t.startswith('PROJETO DE LEI'):
        return None
    if not TITULO_NUMERADO.match(t):
        return None
    if t.startswith('LEI COMPLEMENTAR'):
        return 'LEI COMPLEMENTAR'
    if t.startswith('LEI '):
        return 'LEI'
    if t.startswith('DECRETO LEGISLATIVO'):
        return 'DECRETO LEGISLATIVO'
    if t.startswith('DECRETO '):
        return 'DECRETO'
    return None


def parse_numero(title):
    m = NUMERO_IN_TITLE.search(title)
    return m.group(1).strip('.') if m else None


def _mes_num(nome):
    nome = re.sub(r'[^A-ZÇÃÕÉ]', '', nome.strip().upper())
    if nome in MESES:
        return MESES[nome]
    # The diaries contain month typos ("FEVREIRO", "JANEIREO", "FEVEREIROD");
    # the first three letters identify the month unambiguously.
    if len(nome) >= 3:
        for k, v in MESES.items():
            if k[:3] == nome[:3]:
                return v
    return None


def parse_data(title):
    """Primeira data valida do texto.

    Percorre todas as ocorrencias em vez de parar na primeira: o sumario pode
    trazer um dia impossivel ("DE 32 DE MARCO") e o corpo repetir a data certa
    logo em seguida.
    """
    for m in DATE_IN_TITLE.finditer(title):
        dia, mes_nome, ano = m.groups()
        mes = _mes_num(mes_nome)
        if not mes:
            continue
        try:
            return date(int(ano.replace('.', '')), mes, int(dia)).isoformat()
        except ValueError:
            continue

    for m in DATE_NUMERIC.finditer(title):
        dia, mes_str, ano = m.groups()
        try:
            return date(int(ano), int(mes_str), int(dia)).isoformat()
        except ValueError:
            continue

    return None


def _muito_antes(data, referencia, meses=6):
    """True se `data` esta mais de `meses` antes de `referencia` (ISO)."""
    if not data or not referencia:
        return False
    try:
        d = date.fromisoformat(data)
        r = date.fromisoformat(referencia)
    except ValueError:
        return False
    return (r.year - d.year) * 12 + (r.month - d.month) > meses


def resolver_data(titulo, texto, data_diario=None):
    """Data do ato, preferindo o titulo do sumario mas conferindo com o corpo.

    O sumario as vezes erra o ano ("DE 7 DE JANEIRO DE 2014" numa edicao de
    2015, quando o proprio decreto diz 2015). Nesse caso o corpo do ato manda,
    desde que traga uma data compativel com a data de publicacao.
    """
    do_titulo = parse_data(titulo)
    do_corpo = parse_data(texto[:300]) if texto else None

    plausivel = (do_corpo and data_diario and do_corpo <= data_diario
                 and not _muito_antes(do_corpo, data_diario))

    if do_titulo is None:
        # Sem data no sumario, so aceita a do corpo se ela for compativel.
        return do_corpo if plausivel or (
            do_corpo and (not data_diario or do_corpo <= data_diario)) else None

    if _muito_antes(do_titulo, data_diario) and plausivel:
        return do_corpo

    return do_titulo


def get_toc(doc, max_scan_pages=20):
    entries = []
    started = False
    consecutive_empty = 0
    n = min(len(doc), max_scan_pages)
    for i in range(n):
        text = doc[i].get_text()
        lines = [l.strip() for l in text.split('\n')]
        page_matches = 0
        for line in lines:
            if not started:
                if 'SUM' in line.upper() and 'RIO' in line.upper():
                    started = True
                continue
            m = TOC_LINE.match(line)
            if m:
                title = m.group(1).strip().rstrip('.').strip()
                try:
                    page_num = int(m.group(2))
                except ValueError:
                    continue
                if 2 < len(title) < 300 and 1 <= page_num <= len(doc) + 200:
                    entries.append((title, page_num))
                    page_matches += 1
        if started:
            if page_matches == 0:
                consecutive_empty += 1
                if entries and consecutive_empty >= 1:
                    break
            else:
                consecutive_empty = 0
    return entries


HEADER_MARK = re.compile(r'DI[ÁA]RIO\s+OFICIAL\s+ELETR[ÔO]NICO', re.IGNORECASE)

# O cabecalho/rodape do diario se repete em toda pagina. A deteccao por
# frequencia falha quando o ato ocupa poucas paginas, entao estas linhas fixas
# sao descartadas sempre.
BOILER_LINE = re.compile(
    r'^(DI[ÁA]RIO OFICIAL ELETR[ÔO]NICO'
    r'|ANO:\s*[IVXLC]+'
    r'|EDI[ÇC][ÃA]O\s*N[º°o]?\s*:.*'
    r'|ATOS DOS PODERES EXECUTIVO E LEGISLATIVO'
    r'|P[áa]gina\s+\d+'
    r'|MUNIC[ÍI]PIO DE ARARAS\s*-\s*SP'
    r'|In[íi]cio'
    r'|Di[áa]rio Oficial Assinado Eletronicamente.*'
    r'|Brasil e Protocolado com Carimbo de Tempo.*'
    r'|Medida Provis[óo]ria 2200-2.*'
    r'|Emenda [àa] Lei Org[âa]nica do Munic[íi]pio de Araras n[º°o]?\s*30/2014'
    r'|Arquivo Assinado Digitalmente por MUNIC[ÍI]PIO DE ARARAS.*'
    r'|A Prefeitura Municipal de Araras da garantia da autenticidade.*'
    r'|documento, desde que visualizado atrav[ée]s de'
    r'|https?://www\.araras\.sp\.gov\.br.*'
    r'|(SEGUNDA|TER[ÇC]A|QUARTA|QUINTA|SEXTA|S[ÁA]BADO|DOMINGO)[- ]FEIRA,.*'
    r'|S[ÁA]BADO,.*|DOMINGO,.*'
    r')\s*$', re.IGNORECASE)


def main_pages(doc):
    """Indices of the diary's own pages, excluding attached annexes.

    Annexes are arbitrary documents appended after the diary body; they carry
    their own "Página N" numbering and would otherwise corrupt the page map.
    """
    idx = [i for i in range(len(doc))
           if HEADER_MARK.search('\n'.join(doc[i].get_text().split('\n')[:18]))]
    return idx or list(range(len(doc)))


def annex_pages(doc):
    """Paginas anexadas depois do corpo do diario.

    A Camara Municipal publica suas leis por anexo: o corpo do diario traz so
    o titulo e a remissao "EM ANEXO", e o texto inteiro vem no documento
    apenso, que nao carrega o cabecalho do diario.
    """
    corpo = set(main_pages(doc))
    return [i for i in range(len(doc)) if i not in corpo]


def build_page_map(doc, pages=None):
    """Map printed page number -> pdf index, first occurrence wins.

    The diary body always precedes its annexes, so the first page claiming a
    printed number is the real one.
    """
    page_map = {}
    for i in (main_pages(doc) if pages is None else pages):
        head = '\n'.join(doc[i].get_text().split('\n')[:18])
        m = PAGINA_RE.search(head)
        if m:
            page_map.setdefault(int(m.group(1)), i)
    return page_map


def compute_boilerplate(doc, pages=None):
    from collections import Counter
    freq = Counter()
    pages = main_pages(doc) if pages is None else pages
    n = len(pages)
    for i in pages:
        lines = set(l.strip() for l in doc[i].get_text().split('\n') if l.strip())
        for l in lines:
            freq[l] += 1
    threshold = max(3, int(n * 0.6))
    return {l for l, c in freq.items() if c >= threshold}


# Outro tipo de ato, com numeracao propria, publicado sob um titulo de lei/
# decreto no sumario. Anexos legitimos ("TERMO DE ADMISSAO", "CONVENIO QUE
# CELEBRAM") nao trazem numero proprio e por isso nao casam aqui.
OUTRO_ATO_NUMERADO = re.compile(
    r'^(COMUNICADO|EXTRATO|EDITAL|PORTARIA|AVISO|CONVOCA[ÇC][ÃA]O'
    r'|NOTIFICA[ÇC][ÃA]O|ERRATA|OF[ÍI]CIO|DELIBERA[ÇC][ÃA]O)\b'
    r'[^\n]{0,40}?N[º°o]\.?\s*\d', re.IGNORECASE)

MARCA_DE_ATO = re.compile(
    r'(faz saber que a C[âa]mara|D\s*E\s*C\s*R\s*E\s*T\s*A|DECRETA\b'
    r'|sancionada e promulgada|no uso de suas atribui|PROMULGA'
    r'|\bArt(?:igo)?\.?\s*\d)', re.IGNORECASE)


def corpo_de_outro_ato(titulo, texto):
    """True quando o texto sob um titulo de lei/decreto e, na verdade, outro
    ato (o diario as vezes usa o titulo da norma como cabecalho de secao)."""
    if MARCA_DE_ATO.search(texto or ''):
        return False
    tnorm = _normalize(titulo)[:20]
    for linha in (l.strip() for l in (texto or '').split('\n')):
        if not linha or _normalize(linha)[:20] == tnorm:
            continue  # repeticoes do proprio titulo
        return bool(OUTRO_ATO_NUMERADO.match(linha))
    return False


def _normalize(s):
    return re.sub(r'[^A-Z0-9]', '', s.upper())


def _build_norm_index(lines):
    """Concatenate normalized lines (no separators, so wrapped headings still
    match) and record, per line, the [start, end) offset into the big string."""
    parts = []
    offsets = []
    pos = 0
    for l in lines:
        n = _normalize(l)
        offsets.append((pos, pos + len(n)))
        parts.append(n)
        pos += len(n)
    return ''.join(parts), offsets


def _line_for_offset(offsets, char_pos):
    for i, (start, end) in enumerate(offsets):
        if start <= char_pos < end or (start == end and start == char_pos):
            return i
    return len(offsets) - 1 if offsets else 0


# Distancia, em linhas, ate ainda considerar duas ocorrencias do titulo como
# a mesma abertura de ato: o diario repete o titulo como cabecalho de secao e
# logo abaixo como titulo do proprio ato, sempre a poucas linhas.
REPETICAO_MAX_LINHAS = 12


def _find_start_heading(lines, title_norm):
    """Linha em que o ato realmente comeca.

    O titulo aparece duas vezes seguidas: como cabecalho de secao e como
    titulo do ato, e o recorte tem de comecar na segunda. So que ele tambem
    reaparece paginas adiante, citado por outro ato ou repetido no fim da
    edicao; ir ate a ultima ocorrencia da faixa (o que se fazia antes) fazia
    o recorte pular todo o corpo e gravar apenas a linha do titulo. Por isso
    a busca so avanca enquanto as repeticoes forem vizinhas.
    """
    if not title_norm:
        return None
    big, offsets = _build_norm_index(lines)
    pos = big.find(title_norm)
    if pos == -1:
        return None
    linha = _line_for_offset(offsets, pos)
    while True:
        seguinte = big.find(title_norm, pos + 1)
        if seguinte == -1:
            break
        linha_seguinte = _line_for_offset(offsets, seguinte)
        if linha_seguinte - linha > REPETICAO_MAX_LINHAS:
            break
        pos, linha = seguinte, linha_seguinte
    return linha


def _find_first_heading_after(lines, title_norm, after_idx):
    if not title_norm:
        return None
    sub_lines = lines[after_idx + 1:]
    big, offsets = _build_norm_index(sub_lines)
    pos = big.find(title_norm)
    if pos == -1:
        return None
    return after_idx + 1 + _line_for_offset(offsets, pos)


# Fecho dos documentos que a Camara anexa; depois dele vem so a folha de
# assinaturas digitais, que nao faz parte do ato.
ANEXO_FECHO = re.compile(
    r'^(DOCUMENTO ASSINADO DIGITALMENTE|Assinaturas Digitais)\b', re.IGNORECASE)

# Quantos caracteres do cabecalho da pagina identificam o documento apenso.
MARCA_ANEXO_CHARS = 16


def _chaves_do_ato(tipo, numero):
    """Formas normalizadas pelas quais o titulo do ato pode abrir uma linha.

    Serve para reencontrar o ato dentro do anexo, onde a pontuacao do titulo
    quase nunca bate com a do sumario ("LEI N. 5.761," x "LEI Nº 5761 -").
    """
    if not tipo or not numero:
        return []
    limpo = numero.strip().lstrip('0') or numero.strip()
    chaves = {_normalize('%s N %s' % (tipo, n)) for n in (numero, limpo)}
    return [c for c in chaves if len(c) >= 6]


def _abre_outro_ato(linha, chaves):
    """True quando a linha e o titulo de um ato diferente do que se recorta."""
    if not TITULO_NUMERADO.match(linha) or linha != linha.upper():
        return False
    n = _normalize(linha)
    return not any(n.startswith(c) for c in chaves)


def texto_do_anexo(doc, tipo, numero, anexos=None, boilerplate=None):
    """Corpo do ato quando o diario publicou so o titulo e remeteu ao anexo.

    As leis promulgadas pela Camara entram no diario como "EM ANEXO": o corpo
    traz apenas a linha do titulo e o texto vem no PDF apenso, que
    `main_pages` exclui de proposito. Sem esta busca o ato fica gravado sem
    uma linha de conteudo.
    """
    chaves = _chaves_do_ato(tipo, numero)
    if not chaves:
        return None
    anexos = annex_pages(doc) if anexos is None else anexos
    if not anexos:
        return None
    if boilerplate is None:
        boilerplate = compute_boilerplate(doc, pages=anexos)

    def limpar(pi):
        saida = []
        for linha in doc[pi].get_text().split(chr(10)):
            t = linha.strip()
            if t and t not in boilerplate and not BOILER_LINE.match(t):
                saida.append(t)
        return saida

    def abre_ato(l):
        return any(_normalize(l).startswith(c) for c in chaves)

    def cabecalho(pi):
        """Marca do documento apenso: os anexos vem concatenados num PDF so,
        cada um com seu proprio cabecalho de pagina ("GABINETE DO PREFEITO",
        "CAMARA MUNICIPAL DE ARARAS"). Quando a marca muda, comecou outro
        documento e o ato acabou."""
        for linha in doc[pi].get_text().split(chr(10)):
            n = _normalize(linha)
            if n:
                return n[:MARCA_ANEXO_CHARS] if len(n) >= MARCA_ANEXO_CHARS \
                    else None
        return None

    for pos, pi in enumerate(anexos):
        linhas = limpar(pi)
        inicio = next((i for i, l in enumerate(linhas) if abre_ato(l)), None)
        if inicio is None:
            continue

        marca = cabecalho(pi)
        colhido = []
        for pj in [pi] + anexos[pos + 1:]:
            atual = linhas[inicio:] if pj == pi else limpar(pj)
            if pj != pi:
                # A pagina seguinte que ja abre outro ato encerra este.
                if any(_abre_outro_ato(l, chaves) for l in atual[:6]):
                    break
                if marca and cabecalho(pj) != marca:
                    break
            parou = False
            for i, l in enumerate(atual):
                if (i or pj != pi) and _abre_outro_ato(l, chaves):
                    parou = True
                    break
                if ANEXO_FECHO.match(l):
                    parou = True
                    break
                colhido.append(l)
            if parou:
                break

        texto = chr(10).join(colhido).strip()
        if MARCA_DE_ATO.search(texto):
            return texto
    return None


def extract_entries(doc, data_diario=None):
    """Returns list of dicts: title, tipo, numero, data_ato, texto, pagina_inicio, pagina_fim"""
    toc = get_toc(doc)
    if not toc:
        return []
    body = main_pages(doc)
    page_map = build_page_map(doc, pages=body)
    boilerplate = compute_boilerplate(doc, pages=body)
    anexos = annex_pages(doc)
    boiler_anexo = compute_boilerplate(doc, pages=anexos) if anexos else set()
    # The last entry must stop at the end of the diary body, not run into the
    # annexes appended after it.
    total_pages = len(body)

    results = []
    for idx, (title, start_page) in enumerate(toc):
        tipo = classify(title)
        if tipo is None:
            continue
        next_page = toc[idx + 1][1] if idx + 1 < len(toc) else total_pages + 1
        # Include the next entry's start page too: content can legitimately spill
        # onto the page where the next entry begins; heading-based trimming below
        # (via _find_first_heading_after) separates the two once both are present.
        end_page_excl = max(next_page + 1, start_page + 1)
        printed_pages = list(range(start_page, end_page_excl))

        pdf_indices = []
        for p in printed_pages:
            if p in page_map:
                pdf_indices.append(page_map[p])
            elif 1 <= p <= len(body):
                # No header marker on that page: fall back to position within
                # the body, never to a raw index that could land in an annex.
                pdf_indices.append(body[p - 1])
        pdf_indices = sorted(set(pdf_indices))
        if not pdf_indices:
            continue

        lines = []
        for pi in pdf_indices:
            text = doc[pi].get_text()
            for line in text.split('\n'):
                s = line.strip()
                if not s:
                    continue
                if s in boilerplate or BOILER_LINE.match(s):
                    continue
                if re.match(r'^P[aá]gina\s+\d+$', s):
                    continue
                # Linha do sumario ("Titulo ....... 7"): quando o ato comeca na
                # propria pagina do sumario, ela casaria com o titulo e o
                # recorte abriria no indice em vez de abrir no ato.
                if TOC_LINE.match(s):
                    continue
                lines.append(s)

        title_norm = _normalize(title)
        start_i = _find_start_heading(lines, title_norm)
        if start_i is not None:
            lines = lines[start_i:]
            # O corte e o proximo titulo do sumario que aparecer na faixa. Se
            # ele nao for encontrado (o sumario nem sempre reproduz o titulo
            # ao pe da letra), tenta os seguintes, senao o recorte engoliria
            # todas as paginas ate o proximo ato numerado.
            for prox_titulo, prox_pagina in toc[idx + 1:]:
                if prox_pagina > printed_pages[-1]:
                    break
                cut_i = _find_first_heading_after(
                    lines, _normalize(prox_titulo), 0)
                if cut_i:
                    lines = lines[:cut_i]
                    break

        texto = '\n'.join(lines).strip()
        texto = re.sub(r'\n{3,}', '\n\n', texto)

        numero = parse_numero(title)
        # Diario que so publica o titulo e remete ao anexo: o texto vem de la.
        if not MARCA_DE_ATO.search(texto):
            do_anexo = texto_do_anexo(doc, tipo, numero, anexos=anexos,
                                      boilerplate=boiler_anexo)
            if do_anexo:
                texto = do_anexo

        if corpo_de_outro_ato(title, texto):
            continue

        data_ato = resolver_data(title, texto, data_diario)

        results.append({
            'titulo': title,
            'tipo': tipo,
            'numero': numero,
            'data_ato': data_ato,
            'texto': texto,
            'pagina_inicio': printed_pages[0],
            'pagina_fim': printed_pages[-1],
        })
    return results
