"""DDL do RAG. Idempotente: roda em toda subida e não toca em nada que já
exista. Vive no mesmo arquivo SQLite da legislação — as tabelas antigas
(`atos`, `atos_fts` e seus gatilhos) não são alteradas.
"""
import sqlite3

DDL = """
-- Um dispositivo recuperável: artigo com seus parágrafos/incisos, o
-- cabeçalho do ato (título+ementa) ou um trecho de anexo.
CREATE TABLE IF NOT EXISTS ato_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ato_id      INTEGER NOT NULL,
    ordem       INTEGER NOT NULL,
    tipo_chunk  TEXT    NOT NULL,   -- cabecalho | artigo | anexo | trecho
    rotulo      TEXT,               -- "Art. 5º", "Ementa", "Anexo I"
    artigo      TEXT,               -- "5" normalizado (NULL quando não é artigo)
    paragrafo   TEXT,               -- "1" | "unico"
    incisos     TEXT,               -- "II,III" — incisos contidos no trecho
    secao       TEXT,               -- capítulo/seção vigente no ponto do texto
    texto       TEXT    NOT NULL,
    n_chars     INTEGER NOT NULL,
    hash        TEXT    NOT NULL,   -- sha256 do texto: detecta alteração
    UNIQUE(ato_id, ordem)
);
CREATE INDEX IF NOT EXISTS idx_chunks_ato    ON ato_chunks(ato_id);
CREATE INDEX IF NOT EXISTS idx_chunks_artigo ON ato_chunks(ato_id, artigo);
CREATE INDEX IF NOT EXISTS idx_chunks_hash   ON ato_chunks(hash);

-- Vetor float16 normalizado. O hash guardado é o do texto no momento da
-- geração: se divergir do chunk, o embedding está velho e é refeito.
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id   INTEGER PRIMARY KEY,
    modelo     TEXT    NOT NULL,
    dim        INTEGER NOT NULL,
    hash       TEXT    NOT NULL,
    vetor      BLOB    NOT NULL,
    criado_em  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emb_modelo ON chunk_embeddings(modelo, dim);

-- Busca textual por dispositivo, ao lado da `atos_fts` (que é por ato).
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    texto, rotulo,
    content='ato_chunks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON ato_chunks BEGIN
    INSERT INTO chunks_fts(rowid, texto, rotulo)
    VALUES (new.id, new.texto, new.rotulo);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON ato_chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, texto, rotulo)
    VALUES ('delete', old.id, old.texto, old.rotulo);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON ato_chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, texto, rotulo)
    VALUES ('delete', old.id, old.texto, old.rotulo);
    INSERT INTO chunks_fts(rowid, texto, rotulo)
    VALUES (new.id, new.texto, new.rotulo);
END;

-- Estado da indexação por ato: evita reprocessar o que não mudou.
CREATE TABLE IF NOT EXISTS ato_index_estado (
    ato_id        INTEGER PRIMARY KEY,
    hash_texto    TEXT    NOT NULL,
    n_chunks      INTEGER NOT NULL,
    atualizado_em TEXT    NOT NULL,
    erro          TEXT
);

CREATE TABLE IF NOT EXISTS ia_conversas (
    id            TEXT PRIMARY KEY,
    titulo        TEXT,
    criada_em     TEXT NOT NULL,
    atualizada_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ia_mensagens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    conversa_id TEXT NOT NULL,
    papel       TEXT NOT NULL,      -- user | assistant
    conteudo    TEXT NOT NULL,
    fontes      TEXT,               -- JSON com os dispositivos citados
    criada_em   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_conversa ON ia_mensagens(conversa_id, id);

-- Auditoria das consultas. Guarda o que foi recuperado e quanto demorou;
-- do cliente guarda só um hash, nunca o IP.
CREATE TABLE IF NOT EXISTS ia_consultas_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    criada_em     TEXT NOT NULL,
    conversa_id   TEXT,
    pergunta      TEXT NOT NULL,
    estrategia    TEXT,
    filtros       TEXT,
    atos_ids      TEXT,
    chunk_ids     TEXT,
    n_candidatos  INTEGER,
    n_contexto    INTEGER,
    modelo        TEXT,
    ms_busca      INTEGER,
    ms_llm        INTEGER,
    ms_total      INTEGER,
    cache         INTEGER NOT NULL DEFAULT 0,
    erro          TEXT,
    cliente_hash  TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_data ON ia_consultas_log(criada_em);

CREATE TABLE IF NOT EXISTS ia_cache (
    chave     TEXT PRIMARY KEY,
    payload   TEXT NOT NULL,       -- JSON da resposta completa
    criada_em TEXT NOT NULL,
    hits      INTEGER NOT NULL DEFAULT 0
);
"""


def migrar(conn):
    """Cria o que falta. Seguro para chamar a cada subida."""
    conn.executescript(DDL)
    conn.commit()


def migrar_path(db_path):
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        migrar(conn)
    finally:
        conn.close()


def instalado(conn):
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master "
        "WHERE type='table' AND name='ato_chunks'").fetchone()
    return bool(row and row[0])
