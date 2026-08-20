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


def classify(title):
    t = title.strip().upper()
    if t.startswith('PROJETO DE LEI'):
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
    m = DATE_IN_TITLE.search(title)
    if not m:
        m = DATE_NUMERIC.search(title)
        if not m:
            return None
        dia, mes_str, ano = m.groups()
        try:
            return date(int(ano), int(mes_str), int(dia)).isoformat()
        except ValueError:
            return None

    dia, mes_nome, ano = m.groups()
    ano = ano.replace('.', '')
    mes = _mes_num(mes_nome)
    if not mes:
        return None
    try:
        # date() rejects impossible days ("DE 32 DE MARÇO"), which do occur
        # as typos in the source diaries.
        return date(int(ano), mes, int(dia)).isoformat()
    except ValueError:
        return None


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


def _find_last_heading(lines, title_norm):
    if not title_norm:
        return None
    big, offsets = _build_norm_index(lines)
    last_pos = big.rfind(title_norm)
    if last_pos == -1:
        return None
    return _line_for_offset(offsets, last_pos)


def _find_first_heading_after(lines, title_norm, after_idx):
    if not title_norm:
        return None
    sub_lines = lines[after_idx + 1:]
    big, offsets = _build_norm_index(sub_lines)
    pos = big.find(title_norm)
    if pos == -1:
        return None
    return after_idx + 1 + _line_for_offset(offsets, pos)


def extract_entries(doc, data_diario=None):
    """Returns list of dicts: title, tipo, numero, data_ato, texto, pagina_inicio, pagina_fim"""
    toc = get_toc(doc)
    if not toc:
        return []
    body = main_pages(doc)
    page_map = build_page_map(doc, pages=body)
    boilerplate = compute_boilerplate(doc, pages=body)
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
                lines.append(s)

        title_norm = _normalize(title)
        start_i = _find_last_heading(lines, title_norm)
        if start_i is not None:
            lines = lines[start_i:]
            next_title = toc[idx + 1][0] if idx + 1 < len(toc) else None
            if next_title and toc[idx + 1][1] <= printed_pages[-1]:
                next_norm = _normalize(next_title)
                cut_i = _find_first_heading_after(lines, next_norm, 0)
                if cut_i is not None:
                    lines = lines[:cut_i]

        texto = '\n'.join(lines).strip()
        texto = re.sub(r'\n{3,}', '\n\n', texto)

        data_ato = parse_data(title)
        if data_ato is None:
            # Falling back to the body can pick up a date belonging to some
            # other act cited there, so only trust one that could plausibly
            # be this act: signed on or before the day it was published.
            cand = parse_data(texto[:300])
            if cand and (data_diario is None or cand <= data_diario):
                data_ato = cand

        results.append({
            'titulo': title,
            'tipo': tipo,
            'numero': parse_numero(title),
            'data_ato': data_ato,
            'texto': texto,
            'pagina_inicio': printed_pages[0],
            'pagina_fim': printed_pages[-1],
        })
    return results
