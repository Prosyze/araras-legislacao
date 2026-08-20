"""Procura entradas classificadas como lei/decreto cujo corpo e, na verdade,
outro tipo de ato (comunicado, edital, portaria...) que apenas cita a norma."""
import re
import sqlite3

DB_PATH = 'db/araras_legislacao.db'

OUTRO_ATO = re.compile(
    r'^(COMUNICADO|EDITAL|PORTARIA|AVISO|ATA\b|EXTRATO|CONVOCA[ÇC][ÃA]O'
    r'|NOTIFICA[ÇC][ÃA]O|RESOLU[ÇC][ÃA]O|INSTRU[ÇC][ÃA]O NORMATIVA|TERMO\b'
    r'|CONV[ÊE]NIO|ERRATA|ORDEM DE SERVI[ÇC]O|DELIBERA[ÇC][ÃA]O|OF[ÍI]CIO)\b',
    re.IGNORECASE)

# Marcas de um ato normativo de verdade.
PREAMBULO = re.compile(
    r'(faz saber que a C[âa]mara|D\s*E\s*C\s*R\s*E\s*T\s*A|DECRETA\b'
    r'|sancionada e promulgada|no uso de suas atribui|PROMULGA)', re.IGNORECASE)
ARTIGO = re.compile(r'\bArt(?:igo)?\.?\s*\d', re.IGNORECASE)


def suspeito(titulo, texto):
    linhas = [l.strip() for l in (texto or '').split('\n') if l.strip()]
    # pula as repeticoes do proprio titulo no inicio do corpo
    tnorm = re.sub(r'[^A-Z0-9]', '', titulo.upper())[:20]
    corpo = [l for l in linhas
             if re.sub(r'[^A-Z0-9]', '', l.upper())[:20] != tnorm]
    if not corpo:
        return None
    for l in corpo[:3]:
        if OUTRO_ATO.match(l):
            if PREAMBULO.search(texto or '') or ARTIGO.search(texto or ''):
                return None  # tem corpo de lei/decreto de verdade
            return l[:70]
    return None


def main():
    conn = sqlite3.connect(DB_PATH)
    achados = []
    for rid, ed, tipo, num, titulo, texto in conn.execute(
            'SELECT id, edicao, tipo, numero, titulo, texto FROM atos'):
        m = suspeito(titulo, texto)
        if m:
            achados.append((rid, ed, tipo, num, titulo, m))

    print(f'{len(achados)} entrada(s) suspeita(s):\n')
    for rid, ed, tipo, num, titulo, m in achados:
        print(f'  id={rid} ed={ed} [{tipo} {num}] {titulo[:52]}')
        print(f'       corpo comeca com: {m}')
    return achados


if __name__ == '__main__':
    main()
