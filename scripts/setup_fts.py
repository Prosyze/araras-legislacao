import sqlite3

DB_PATH = 'db/araras_legislacao.db'


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
    CREATE VIRTUAL TABLE IF NOT EXISTS atos_fts USING fts5(
        titulo, texto, content='atos', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
    );

    CREATE TRIGGER IF NOT EXISTS atos_ai AFTER INSERT ON atos BEGIN
        INSERT INTO atos_fts(rowid, titulo, texto) VALUES (new.id, new.titulo, new.texto);
    END;
    CREATE TRIGGER IF NOT EXISTS atos_ad AFTER DELETE ON atos BEGIN
        INSERT INTO atos_fts(atos_fts, rowid, titulo, texto) VALUES ('delete', old.id, old.titulo, old.texto);
    END;
    CREATE TRIGGER IF NOT EXISTS atos_au AFTER UPDATE ON atos BEGIN
        INSERT INTO atos_fts(atos_fts, rowid, titulo, texto) VALUES ('delete', old.id, old.titulo, old.texto);
        INSERT INTO atos_fts(rowid, titulo, texto) VALUES (new.id, new.titulo, new.texto);
    END;
    """)
    conn.commit()

    atos_count = conn.execute('SELECT count(*) FROM atos').fetchone()[0]
    hits = conn.execute("SELECT count(*) FROM atos_fts WHERE atos_fts MATCH 'lei'").fetchone()[0]
    if hits == 0 and atos_count > 0:
        conn.execute("INSERT INTO atos_fts(atos_fts) VALUES('rebuild')")
        conn.commit()
        print(f'FTS reconstruido (rebuild): {atos_count} registros')
    else:
        print(f'FTS ja sincronizado: {atos_count} registros ({hits} contem "lei")')


if __name__ == '__main__':
    main()
