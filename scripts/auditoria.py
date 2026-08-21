"""Audita a qualidade dos textos extraidos, sem alterar nada."""
import re
import sqlite3
import sys

DB_PATH = 'db/araras_legislacao.db'

BOILER = re.compile(
    r'Di[áa]rio Oficial Assinado Eletronicamente|Arquivo Assinado Digitalmente|'
    r'DI[ÁA]RIO OFICIAL ELETR[ÔO]NICO', re.IGNORECASE)


# Todo ato tem parte dispositiva. Sem nenhuma destas marcas o registro nao
# tem corpo: o recorte pegou so o cabecalho.
CORPO = re.compile(
    r'\bArt(?:igo)?\.?\s*\d|D\s*E\s*C\s*R\s*E\s*T\s*A\b|'
    r'faz saber que a C[\u00e2a]mara|sancionada e promulgada', re.IGNORECASE)


def norm(s):
    return re.sub(r'[^A-Z0-9]', '', (s or '').upper())


def auditar(conn):
    problemas = {
        'boilerplate': [],      # cabecalho/rodape do diario vazou para o texto
        'sem_titulo': [],       # texto nao comeca pelo proprio ato
        'muito_curto': [],      # provavelmente "EM ANEXO" ou recorte falho
        'sem_artigo': [],       # nenhum "Art." no corpo
        'sem_corpo': [],        # so o cabecalho: recorte falhou por completo
    }

    for r in conn.execute(
            'SELECT id, edicao, tipo, numero, titulo, texto FROM atos'):
        rid, ed, tipo, num, titulo, texto = r
        t = texto or ''

        if BOILER.search(t):
            problemas['boilerplate'].append((rid, ed, titulo))

        # o texto deve comecar pelo cabecalho do proprio ato
        head = norm(t[:400])
        if norm(titulo)[:24] not in head:
            problemas['sem_titulo'].append((rid, ed, titulo))

        if len(t) < 200:
            problemas['muito_curto'].append((rid, ed, titulo))

        if not re.search(r'\bArt\.?\s*\d', t, re.IGNORECASE):
            problemas['sem_artigo'].append((rid, ed, titulo))

        if not CORPO.search(t):
            problemas['sem_corpo'].append((rid, ed, titulo))

    return problemas


def main():
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute('SELECT count(*) FROM atos').fetchone()[0]
    p = auditar(conn)

    print(f'Total de atos: {total}\n')
    for k, v in p.items():
        print(f'{k:14} {len(v):5}  ({100*len(v)/total:.1f}%)')

    suspeitos = {i for k in ('boilerplate', 'sem_titulo', 'sem_corpo')
                 for i, _, _ in p[k]}
    print(f'\nRegistros com recorte provavelmente errado: {len(suspeitos)}')

    if '-v' in sys.argv:
        for k in ('boilerplate', 'sem_titulo', 'muito_curto', 'sem_artigo',
                  'sem_corpo'):
            print(f'\n=== {k} (ate 15) ===')
            for rid, ed, tit in p[k][:15]:
                print(f'  id={rid} ed={ed} {tit[:70]}')

    return suspeitos


if __name__ == '__main__':
    main()
