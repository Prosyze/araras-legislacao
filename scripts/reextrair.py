"""Reaplica o extrator ao acervo sem apagar o banco.

O `rebuild_db.py` recria a tabela do zero, o que serve para uma carga limpa
mas invalida tudo o que aponta para o id do ato (resumos, chunks e vetores do
RAG). Quando o que mudou foi so a regra de recorte, o certo e reprocessar os
PDFs e regravar o texto no registro que ja existe: o id fica de pe e o
indexador reindexa sozinho os atos cujo texto mudou, porque ele compara o
hash do texto.

    python scripts/reextrair.py --dry-run    # so relata o que mudaria
    python scripts/reextrair.py              # aplica
"""
import argparse
import glob
import os
import re
import sqlite3
import sys
import time

import fitz

from extract_lib import extract_entries

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, 'db', 'araras_legislacao.db')
PDF_DIR = os.path.join(BASE_DIR, 'diarios_oficiais')
FNAME_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})_edicao_(\d+)\.pdf$')


def atos_por_edicao(conn):
    mapa = {}
    for r in conn.execute(
            'SELECT id, edicao, titulo, texto FROM atos'):
        mapa.setdefault(r[1], {})[r[2]] = (r[0], r[3])
    return mapa


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limite', type=int, default=0,
                    help='processa so as N primeiras edicoes (para testes)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute('PRAGMA journal_mode=WAL')
    existentes = atos_por_edicao(conn)

    arquivos = sorted(glob.glob(os.path.join(PDF_DIR, '*.pdf')))
    if args.limite:
        arquivos = arquivos[:args.limite]

    t0 = time.time()
    alterados = inseridos = iguais = erros = 0
    ganho = 0
    exemplos = []

    for i, f in enumerate(arquivos, 1):
        m = FNAME_RE.match(os.path.basename(f))
        if not m:
            continue
        data_diario, edicao = m.group(1), int(m.group(2))
        try:
            doc = fitz.open(f)
            try:
                entradas = extract_entries(doc, data_diario=data_diario)
            finally:
                doc.close()
        except Exception as ex:
            erros += 1
            print(f'  ERRO {os.path.basename(f)}: {ex}', flush=True)
            continue

        no_banco = existentes.get(edicao, {})
        # O sumario as vezes repete o mesmo titulo numa pagina adiante, onde na
        # verdade esta outro ato. A carga original ja resolvia isso com
        # INSERT OR IGNORE (vale a primeira), e aqui tem de valer o mesmo,
        # senao a segunda ocorrencia sobrescreveria o texto certo.
        vistos = set()
        for e in entradas:
            if e['titulo'] in vistos:
                continue
            vistos.add(e['titulo'])
            atual = no_banco.get(e['titulo'])
            if atual is None:
                inseridos += 1
                if not args.dry_run:
                    conn.execute(
                        """INSERT OR IGNORE INTO atos
                           (edicao, data_diario, tipo, numero, data_ato, titulo,
                            texto, pagina_inicio, pagina_fim, arquivo_pdf)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (edicao, data_diario, e['tipo'], e['numero'],
                         e['data_ato'], e['titulo'], e['texto'],
                         e['pagina_inicio'], e['pagina_fim'],
                         os.path.relpath(f, BASE_DIR)))
                continue

            ato_id, texto_antigo = atual
            if e['texto'] == texto_antigo:
                iguais += 1
                continue

            alterados += 1
            ganho += len(e['texto']) - len(texto_antigo)
            if len(exemplos) < 40:
                exemplos.append((edicao, e['titulo'], len(texto_antigo),
                                 len(e['texto'])))
            if not args.dry_run:
                conn.execute(
                    """UPDATE atos SET texto = ?, data_ato = ?,
                           pagina_inicio = ?, pagina_fim = ? WHERE id = ?""",
                    (e['texto'], e['data_ato'], e['pagina_inicio'],
                     e['pagina_fim'], ato_id))

        if i % 300 == 0:
            if not args.dry_run:
                conn.commit()
            print(f'  [{i}/{len(arquivos)}] alterados={alterados} '
                  f'inseridos={inseridos} ({time.time()-t0:.0f}s)', flush=True)

    if not args.dry_run:
        conn.commit()
        # O indice espelha `atos`; sem isso a busca continua achando o texto
        # antigo.
        conn.execute("INSERT INTO atos_fts(atos_fts) VALUES('rebuild')")
        conn.commit()
    conn.close()

    print()
    print(f'iguais    : {iguais}')
    print(f'alterados : {alterados}')
    print(f'inseridos : {inseridos}')
    print(f'erros     : {erros}')
    print(f'saldo     : {ganho:+,} caracteres')
    print(f'tempo     : {time.time()-t0:.0f}s')
    if exemplos:
        print('\nprimeiros alterados:')
        for ed, tit, a, b in exemplos:
            print(f'  ed {ed:<5} {tit[:58]:<58} {a:>8} -> {b:>8}')
    if args.dry_run:
        print('\n(dry-run: nada foi gravado)')


if __name__ == '__main__':
    sys.exit(main())
