"""Recompute data_ato in place with the corrected parser, keeping every other
column (including generated resumos) untouched."""
import sqlite3

from extract_lib import parse_data

DB_PATH = 'db/araras_legislacao.db'


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT id, titulo, texto, data_diario, data_ato FROM atos').fetchall()

    changed = 0
    for r in rows:
        nova = parse_data(r['titulo'])
        if nova is None:
            cand = parse_data(r['texto'][:300])
            if cand and cand <= r['data_diario']:
                nova = cand
        if nova != r['data_ato']:
            conn.execute('UPDATE atos SET data_ato = ? WHERE id = ?', (nova, r['id']))
            changed += 1
            print(f"  id={r['id']} {r['data_ato']} -> {nova}  |  {r['titulo'][:60]}")

    conn.commit()
    print(f'\n{changed} data(s) corrigida(s) de {len(rows)} registros.')

    restantes = conn.execute(
        'SELECT count(*) FROM atos WHERE data_ato > data_diario').fetchone()[0]
    sem_data = conn.execute(
        'SELECT count(*) FROM atos WHERE data_ato IS NULL').fetchone()[0]
    print(f'data_ato posterior ao diario (typos da fonte): {restantes}')
    print(f'sem data_ato: {sem_data}')


if __name__ == '__main__':
    main()
