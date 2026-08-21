"""Gera `db/semente.db`, o banco que vai versionado e sobe no deploy.

A semente leva so o acervo: `atos`, `edicoes_processadas` e o indice de busca,
reconstruido do zero. Fica de fora tudo o que o servidor sabe refazer sozinho
— os chunks e os vetores do RAG, que juntos pesam mais que o acervo inteiro e
mudariam o arquivo a cada reindexacao, inchando o historico do repositorio.

Rode depois de qualquer mudanca no acervo (reextracao, carga de edicoes
novas) e commite o resultado:

    python scripts/gerar_semente.py
"""
import os
import sqlite3
import sys

import config

TABELAS = ('atos', 'edicoes_processadas')

FTS_DDL = """CREATE VIRTUAL TABLE atos_fts USING fts5(
    titulo, texto, content='atos', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
)"""


def gerar(origem=None, destino=None, verbose=True):
    origem = origem or config.DB_PATH
    destino = destino or config.SEED_DB
    if os.path.abspath(origem) == os.path.abspath(destino):
        raise ValueError('a semente nao pode ser o proprio banco de trabalho')
    if not os.path.exists(origem):
        raise FileNotFoundError(origem)

    temporario = destino + '.novo'
    for f in (temporario, temporario + '-wal', temporario + '-shm'):
        if os.path.exists(f):
            os.remove(f)

    conn = sqlite3.connect(temporario)
    try:
        conn.execute('ATTACH DATABASE ? AS origem', (origem,))
        for nome in TABELAS:
            ddl = conn.execute(
                "SELECT sql FROM origem.sqlite_master WHERE type='table' "
                "AND name=?", (nome,)).fetchone()
            if not ddl:
                raise RuntimeError(f'tabela {nome} nao existe em {origem}')
            conn.execute(ddl[0])
            conn.execute(f'INSERT INTO {nome} SELECT * FROM origem.{nome}')
        conn.execute(FTS_DDL)
        conn.execute("INSERT INTO atos_fts(atos_fts) VALUES('rebuild')")
        conn.commit()
        conn.execute('DETACH DATABASE origem')
        # Sem o VACUUM o arquivo guarda as paginas livres do INSERT e chega ao
        # git bem maior do que precisa.
        conn.execute('VACUUM')
        n_atos = conn.execute('SELECT count(*) FROM atos').fetchone()[0]
    finally:
        conn.close()

    os.replace(temporario, destino)
    if verbose:
        print(f'[semente] {n_atos} atos -> {destino} '
              f'({os.path.getsize(destino) / 1048576:.1f} MB)', flush=True)
    return destino


if __name__ == '__main__':
    sys.exit(0 if gerar() else 1)
