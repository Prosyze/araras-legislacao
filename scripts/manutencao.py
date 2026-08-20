"""Alinha os dados ja gravados com as regras atuais de classificacao.

O banco de producao vive num volume e sobrevive aos deploys, entao correcoes
no extrator nao chegam la sozinhas. Esta rotina roda na subida e remove o que
as regras atuais nao aceitariam mais. E idempotente: sem nada a corrigir, nao
toca no banco.
"""
import sqlite3

from extract_lib import classify, corpo_de_outro_ato


def sanitizar(db_path, verbose=True):
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        remover = []
        for rid, titulo, texto in conn.execute(
                'SELECT id, titulo, texto FROM atos'):
            if classify(titulo) is None:
                remover.append((rid, titulo, 'nao e lei/decreto numerado'))
            elif corpo_de_outro_ato(titulo, texto):
                remover.append((rid, titulo, 'corpo e outro ato'))

        if not remover:
            return 0

        for rid, titulo, motivo in remover:
            conn.execute('DELETE FROM atos WHERE id = ?', (rid,))
            if verbose:
                print(f'[manutencao] removido: {titulo[:60]} ({motivo})',
                      flush=True)

        conn.execute("INSERT INTO atos_fts(atos_fts) VALUES('rebuild')")
        conn.commit()
        if verbose:
            print(f'[manutencao] {len(remover)} registro(s) removido(s)',
                  flush=True)
        return len(remover)
    finally:
        conn.close()
