import re

ART_RE = re.compile(r'^Art(?:igo)?\.?\s*\d+', re.IGNORECASE)
PARAG_RE = re.compile(r'^(§\s*\d+|Par[áa]grafo\s+[úu]nico|Par[áa]grafo\s+\d+)', re.IGNORECASE)
INCISO_RE = re.compile(r'^[IVXLC]{1,7}\s*[-–—).]')
ALINEA_RE = re.compile(r'^[a-z]\s*[)\.]\s')
HEADING_RE = re.compile(
    r'^(ANEXO|T[ÍI]TULO|CAP[ÍI]TULO|SE[ÇC][ÃA]O|SUBSE[ÇC][ÃA]O|DISPOSI[ÇC][ÕO]ES)\b',
    re.IGNORECASE)
REGISTRO_RE = re.compile(
    r'^(Registrad|Publicad|Protocolo\s*n|MAK/|Prefeitura do Munic[íi]pio|'
    r'Gabinete do Prefeito)', re.IGNORECASE)
ROTULO_RE = re.compile(
    r'^(Ó?rg[ãa]o|Unidade|Fun[çc][ãa]o|Subfun[çc][ãa]o|Programa|Projeto/Atividade|'
    r'Dota[çc][ãa]o|Ficha|Elemento|Fonte)\s*:', re.IGNORECASE)
ACT_TITLE_RE = re.compile(
    r'^(LEI|DECRETO|EMENDA|RESOLU[ÇC][ÃA]O)\b.{0,120}?N[º°o\.]', re.IGNORECASE)
PREAMBULO_RE = re.compile(
    r'(Prefeit[oa]\s+d[oe]\s+Munic[íi]pio|faz saber que a C[âa]mara|'
    r'no uso de suas atribui[çc][õo]es|RESOLVE|D E C R E T A|DECRETA)',
    re.IGNORECASE)

BLOCK_STARTERS = (ART_RE, PARAG_RE, INCISO_RE, ALINEA_RE, HEADING_RE, REGISTRO_RE,
                  ROTULO_RE)


def _is_upperish(s):
    """True/False for case run detection; None when the line is too short to
    judge, so a stray justified word like 'DE' never splits a block."""
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 8:
        return None
    uppers = sum(1 for c in letters if c.isupper())
    return uppers / len(letters) > 0.85


def _starts_block(line):
    return any(p.match(line) for p in BLOCK_STARTERS)


def _merge_lines(lines):
    """Rejoin PDF hard-wrapped lines into logical blocks."""
    blocks = []
    cur = []

    def flush():
        if cur:
            blocks.append(' '.join(cur).strip())
            cur.clear()

    prev_upper = None
    first = True
    for raw in lines:
        line = raw.strip()
        if not line:
            flush()
            prev_upper = None
            continue

        # The act's own title line always stands alone, so the ementa that
        # follows it (also uppercase) does not get glued onto it.
        if first and ACT_TITLE_RE.match(line):
            flush()
            blocks.append(line)
            first = False
            prev_upper = None
            continue
        first = False

        up = _is_upperish(line)
        # A switch between an uppercase (ementa/heading) run and normal text
        # is a real boundary even without a starter pattern.
        toggled = up is not None and prev_upper is not None and up != prev_upper
        if _starts_block(line) or toggled:
            flush()
        cur.append(line)
        if up is not None:
            prev_upper = up

    flush()
    return [b for b in blocks if b]


def _classify(block, index, total, seen_article):
    if index == 0:
        return 'titulo'
    if ART_RE.match(block):
        return 'artigo'
    if PARAG_RE.match(block):
        return 'paragrafo'
    if INCISO_RE.match(block):
        return 'inciso'
    if ALINEA_RE.match(block):
        return 'alinea'
    if ROTULO_RE.match(block):
        return 'rotulo'
    if REGISTRO_RE.match(block):
        return 'registro'
    # A heading is a structural marker only when it is actually set as one.
    if HEADING_RE.match(block) and block == block.upper():
        return 'heading'
    if not seen_article and _is_upperish(block):
        return 'ementa'
    if not seen_article and PREAMBULO_RE.search(block):
        return 'preambulo'
    if seen_article and len(block) < 90 and not block.endswith('.'):
        return 'assinatura'
    return 'paragrafo-comum'


def structure_text(texto):
    """Return a list of {tipo, texto} blocks for readable rendering."""
    lines = texto.split('\n')
    blocks = _merge_lines(lines)

    out = []
    seen_article = False
    for i, b in enumerate(blocks):
        kind = _classify(b, i, len(blocks), seen_article)
        if kind == 'artigo':
            seen_article = True
        b = re.sub(r'\s{2,}', ' ', b)
        out.append({'tipo': kind, 'texto': b})
    return out


def plain_text(texto):
    """Readable plain text (used for the Gemini prompt)."""
    return '\n\n'.join(b['texto'] for b in structure_text(texto))
