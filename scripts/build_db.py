import glob
import os
import re
import sqlite3
import sys
import time

import fitz
from extract_lib import extract_entries

DB_PATH = 'db/araras_legislacao.db'
PDF_DIR = 'diarios_oficiais'
LOG_PATH = 'db/build_log.txt'

FNAME_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})_edicao_(\d+)\.pdf$')

SCHEMA = """
CREATE TABLE IF NOT EXISTS atos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edicao INTEGER NOT NULL,
    data_diario TEXT NOT NULL,
    tipo TEXT NOT NULL,
    numero TEXT,
    data_ato TEXT,
    titulo TEXT NOT NULL,
    texto TEXT NOT NULL,
    pagina_inicio INTEGER,
    pagina_fim INTEGER,
    arquivo_pdf TEXT NOT NULL,
    UNIQUE(edicao, titulo)
);
CREATE INDEX IF NOT EXISTS idx_atos_tipo ON atos(tipo);
CREATE INDEX IF NOT EXISTS idx_atos_numero ON atos(tipo, numero);
CREATE INDEX IF NOT EXISTS idx_atos_data_ato ON atos(data_ato);

CREATE TABLE IF NOT EXISTS edicoes_processadas (
    edicao INTEGER PRIMARY KEY,
    arquivo_pdf TEXT NOT NULL,
    n_entries INTEGER NOT NULL,
    status TEXT NOT NULL,
    processed_at TEXT NOT NULL
);
"""


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None

    os.makedirs('db', exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()

    already = {row[0] for row in conn.execute('SELECT edicao FROM edicoes_processadas')}

    files = sorted(glob.glob(os.path.join(PDF_DIR, '*.pdf')))
    todo = []
    for f in files:
        base = os.path.basename(f)
        m = FNAME_RE.match(base)
        if not m:
            continue
        data_diario, edicao = m.group(1), int(m.group(2))
        if edicao in already:
            continue
        todo.append((f, data_diario, edicao))

    if limit:
        todo = todo[:limit]

    print(f"{len(todo)} edicoes a processar (de {len(files)} arquivos, {len(already)} ja no banco)")

    log = open(LOG_PATH, 'a', encoding='utf-8')
    t0 = time.time()
    n_ok, n_err, total_entries = 0, 0, 0

    for i, (f, data_diario, edicao) in enumerate(todo, 1):
        try:
            doc = fitz.open(f)
            entries = extract_entries(doc, data_diario=data_diario)
            for e in entries:
                conn.execute(
                    """INSERT OR IGNORE INTO atos
                       (edicao, data_diario, tipo, numero, data_ato, titulo, texto,
                        pagina_inicio, pagina_fim, arquivo_pdf)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (edicao, data_diario, e['tipo'], e['numero'], e['data_ato'],
                     e['titulo'], e['texto'], e['pagina_inicio'], e['pagina_fim'], f),
                )
            conn.execute(
                """INSERT OR REPLACE INTO edicoes_processadas
                   (edicao, arquivo_pdf, n_entries, status, processed_at)
                   VALUES (?,?,?,?,datetime('now'))""",
                (edicao, f, len(entries), 'ok'),
            )
            conn.commit()
            n_ok += 1
            total_entries += len(entries)
        except Exception as ex:
            n_err += 1
            msg = f"ERRO edicao={edicao} arquivo={f}: {ex}"
            log.write(msg + '\n')
            log.flush()
            conn.execute(
                """INSERT OR REPLACE INTO edicoes_processadas
                   (edicao, arquivo_pdf, n_entries, status, processed_at)
                   VALUES (?,?,?,?,datetime('now'))""",
                (edicao, f, 0, f'erro: {ex}'),
            )
            conn.commit()

        if i % 100 == 0 or i == len(todo):
            elapsed = time.time() - t0
            print(f"[{i}/{len(todo)}] ok={n_ok} err={n_err} entries={total_entries} "
                  f"elapsed={elapsed:.1f}s ({elapsed/i:.3f}s/edicao)")

    log.close()
    print(f"FIM. ok={n_ok} err={n_err} total_entries={total_entries} tempo={time.time()-t0:.1f}s")


if __name__ == '__main__':
    main()
