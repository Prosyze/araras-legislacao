"""Alinha os dados ja gravados com as regras atuais de extracao.

O banco de producao vive num volume e sobrevive aos deploys, entao correcoes
no extrator nao chegam la sozinhas: o `config.preparar_dados` so copia a
semente quando o volume ainda esta vazio. Estas rotinas rodam na subida e
fecham essa lacuna. Sao idempotentes: sem nada a corrigir, nao tocam no banco.

    sincronizar_com_semente  traz o texto reextraido que veio no deploy
    sanitizar                remove o que as regras atuais nao aceitariam mais
"""
import os
import sqlite3

from extract_lib import classify, corpo_de_outro_ato


def sincronizar_com_semente(db_path, seed_path, verbose=True):
    """Regrava no volume o texto dos atos que a semente do deploy corrigiu.

    Reextrair em producao nao e opcao: os PDFs do Diario (quase 7 GB) nao vao
    para o container. O que viaja e o resultado, dentro da semente versionada
    — daqui ela e copiada por cima, ato a ato, so onde o texto difere.

    Mexe apenas em atos que existem nos dois lados: edicoes novas, captadas
    pelo `updater` depois do ultimo deploy, ficam intactas.
    """
    if not seed_path or os.path.abspath(seed_path) == os.path.abspath(db_path):
        return 0                      # rodando direto na semente, nada a fazer
    if not os.path.exists(seed_path):
        return 0

    conn = sqlite3.connect(db_path, timeout=60)
    try:
        conn.execute('ATTACH DATABASE ? AS semente', (seed_path,))
        cur = conn.execute("""
            UPDATE atos SET texto = s.texto,
                            data_ato = s.data_ato,
                            pagina_inicio = s.pagina_inicio,
                            pagina_fim = s.pagina_fim
              FROM semente.atos s
             WHERE atos.edicao = s.edicao
               AND atos.titulo = s.titulo
               AND atos.texto <> s.texto
        """)
        n = cur.rowcount
        if n > 0:
            # O indice espelha `atos`; sem isso a busca acha o texto antigo.
            conn.execute("INSERT INTO atos_fts(atos_fts) VALUES('rebuild')")
        conn.commit()
        conn.execute('DETACH DATABASE semente')
        if verbose and n > 0:
            print(f'[manutencao] {n} ato(s) com texto atualizado pela semente',
                  flush=True)
        return n
    finally:
        conn.close()


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
