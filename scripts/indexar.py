"""Indexação pela linha de comando.

É por aqui que se faz a carga inicial do acervo já cadastrado — milhares de
atos não cabem numa requisição HTTP, e o free tier do Gemini limita a 100
embeddings por minuto. O processo é resumível: pode ser interrompido e
retomado, que ele continua de onde parou.

    python scripts/indexar.py --status
    python scripts/indexar.py --so-chunks        # divide, sem custo de API
    python scripts/indexar.py                    # divide e vetoriza tudo
    python scripts/indexar.py --limite 500       # vetoriza um pedaço
    python scripts/indexar.py --forcar           # refaz a divisão dos atos
"""
import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv  # noqa: E402

import config  # noqa: E402
from rag import indexer, schema, settings  # noqa: E402

load_dotenv()


def _conn(db_path):
    conn = sqlite3.connect(db_path, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    schema.migrar(conn)
    return conn


def mostrar_status(conn):
    st = indexer.estatisticas(conn)
    print(f"Legislações .............. {st['legislacoes']:>8,}".replace(',', '.'))
    print(f"Dispositivos ............. {st['dispositivos']:>8,}".replace(',', '.'))
    print(f"  dos quais artigos ...... {st['artigos']:>8,}".replace(',', '.'))
    print(f"Indexados (com vetor) .... {st['indexados']:>8,}".replace(',', '.'))
    print(f"Pendentes ................ {st['pendentes']:>8,}".replace(',', '.'))
    print(f"Cobertura ................ {st['cobertura']:>7}%")
    print(f"Modelo ................... {st['modelo']} ({st['dim']}d)")
    print(f"Última indexação ......... {st['ultima_indexacao'] or '—'}")
    if st['n_atos_com_erro']:
        print(f"Atos com erro ............ {st['n_atos_com_erro']}")
        for e in st['atos_com_erro'][:5]:
            print(f"   · ato {e['ato_id']}: {e['erro'][:110]}")


def main():
    ap = argparse.ArgumentParser(description='Indexa a legislação para o RAG.')
    ap.add_argument('--status', action='store_true',
                    help='só mostra a situação do índice')
    ap.add_argument('--so-chunks', action='store_true',
                    help='divide os atos em dispositivos, sem chamar a API')
    ap.add_argument('--forcar', action='store_true',
                    help='refaz a divisão mesmo sem mudança no texto')
    ap.add_argument('--limite', type=int, default=None,
                    help='máximo de embeddings a gerar nesta rodada')
    ap.add_argument('--db', default=None, help='caminho do banco')
    args = ap.parse_args()

    db_path = args.db or config.preparar_dados()
    conn = _conn(db_path)
    print(f'Banco: {db_path}\n')

    if args.status:
        mostrar_status(conn)
        return

    t0 = time.time()
    if args.so_chunks or args.forcar:
        print('Dividindo os atos em dispositivos…')
        novos = indexer.sincronizar_chunks(
            conn, forcar=args.forcar,
            progresso=lambda i, t: print(f'\r  {i}/{t}', end='', flush=True))
        print(f'\r  {novos} dispositivo(s) gravado(s).            ')

    if not args.so_chunks:
        pendentes = indexer.contar_pendentes(conn)
        if not pendentes:
            print('Nenhum vetor pendente.')
        else:
            alvo = min(pendentes, args.limite) if args.limite else pendentes
            estimativa = alvo / max(1, settings.EMBED_RPM)
            print(f'{pendentes} pendente(s); gerando {alvo} '
                  f'(~{estimativa:.0f} min no ritmo de {settings.EMBED_RPM}/min).')
            print('Pode interromper com Ctrl+C — o progresso já gravado fica.')
            try:
                indexer.gerar_embeddings(
                    conn, limite=args.limite,
                    progresso=lambda i, t: print(
                        f'\r  {i}/{t} ({100 * i // max(1, t)}%)', end='',
                        flush=True))
            except KeyboardInterrupt:
                print('\nInterrompido.')
            print()

    for erro in indexer.get_state()['erros']:
        print(f'  ! {erro}')
    print(f'\nTempo: {time.time() - t0:.0f}s\n')
    mostrar_status(conn)


if __name__ == '__main__':
    main()
