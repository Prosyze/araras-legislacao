"""Reprocessa todas as edicoes com o extrator corrigido, preservando os
resumos ja gerados (reassociados por edicao+titulo)."""
import glob
import os
import re
import sqlite3
import time

import fitz

from extract_lib import extract_entries

DB_PATH = 'db/araras_legislacao.db'
PDF_DIR = 'diarios_oficiais'
FNAME_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})_edicao_(\d+)\.pdf$')


def main():
    conn = sqlite3.connect(DB_PATH)

    resumos = conn.execute(
        'SELECT edicao, titulo, resumo FROM atos WHERE resumo IS NOT NULL'
    ).fetchall()
    print(f'{len(resumos)} resumo(s) preservado(s)')

    conn.execute('DELETE FROM atos')
    conn.execute('DELETE FROM edicoes_processadas')
    conn.commit()

    arquivos = sorted(glob.glob(os.path.join(PDF_DIR, '*.pdf')))
    t0 = time.time()
    total_atos = n_err = 0

    for i, f in enumerate(arquivos, 1):
        base = os.path.basename(f)
        m = FNAME_RE.match(base)
        if not m:
            continue
        data_diario, edicao = m.group(1), int(m.group(2))
        try:
            doc = fitz.open(f)
            entries = extract_entries(doc, data_diario=data_diario)
            doc.close()
            for e in entries:
                conn.execute(
                    """INSERT OR IGNORE INTO atos
                       (edicao, data_diario, tipo, numero, data_ato, titulo,
                        texto, pagina_inicio, pagina_fim, arquivo_pdf)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (edicao, data_diario, e['tipo'], e['numero'], e['data_ato'],
                     e['titulo'], e['texto'], e['pagina_inicio'],
                     e['pagina_fim'], f))
            conn.execute(
                """INSERT OR REPLACE INTO edicoes_processadas
                   (edicao, arquivo_pdf, n_entries, status, processed_at)
                   VALUES (?,?,?,?,datetime('now'))""",
                (edicao, f, len(entries), 'ok'))
            total_atos += len(entries)
        except Exception as ex:
            n_err += 1
            print(f'  ERRO {base}: {ex}')

        if i % 500 == 0:
            conn.commit()
            print(f'  [{i}/{len(arquivos)}] atos={total_atos} '
                  f'({time.time()-t0:.0f}s)')

    conn.commit()

    restaurados = 0
    for edicao, titulo, resumo in resumos:
        cur = conn.execute(
            'UPDATE atos SET resumo = ? WHERE edicao = ? AND titulo = ?',
            (resumo, edicao, titulo))
        restaurados += cur.rowcount
    conn.commit()

    conn.execute("INSERT INTO atos_fts(atos_fts) VALUES('rebuild')")
    conn.commit()

    print(f'\nFIM: {total_atos} atos, {n_err} erro(s), '
          f'{restaurados}/{len(resumos)} resumo(s) restaurado(s), '
          f'{time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
