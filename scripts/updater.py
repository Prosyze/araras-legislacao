"""Busca novas edicoes do Diario Oficial na API oficial, baixa os PDFs e
extrai leis/decretos para o banco. Pensado para rodar em background a partir
do frontend, reportando progresso.
"""
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime

import fitz

from extract_lib import extract_entries

import config

API = ('https://api.assistechpublicacoes.com.br/v1/public/tenants/'
       'pmararassp/official-gazettes')
DB_PATH = config.DB_PATH
PDF_DIR = config.PDF_DIR
UA = {'User-Agent': 'Mozilla/5.0'}


# --------------------------------------------------------------------------
# Estado compartilhado do job (uma atualizacao por vez)
# --------------------------------------------------------------------------
_state = {
    'running': False,
    'phase': 'idle',
    'message': 'Nenhuma atualização em andamento.',
    'done': 0,
    'total': 0,
    'novas_edicoes': 0,
    'novos_atos': 0,
    'erros': [],
    'started_at': None,
    'finished_at': None,
}
_lock = threading.Lock()


def get_state():
    with _lock:
        return dict(_state, erros=list(_state['erros'])[-8:])


def _set(**kw):
    with _lock:
        _state.update(kw)


def _fetch_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode('utf-8'))


def anos_padrao(completo=False):
    """Conferir o ano corrente (e o anterior, por causa da virada) cobre o uso
    do dia a dia; a varredura completa so faz sentido para reconstruir a base."""
    ano = datetime.now().year
    return list(range(ano, 2013, -1)) if completo else [ano, ano - 1]


def listar_edicoes_remotas(anos=None, progress=None):
    """Percorre a API (paginada) e devolve {numero: {...}} de todas as edicoes."""
    if anos is None:
        anos = anos_padrao()

    achadas = {}
    for ano in anos:
        page = 1
        while True:
            url = f'{API}?year={ano}&page={page}'
            try:
                d = _fetch_json(url)
            except Exception as e:
                _add_erro(f'ano {ano} pagina {page}: {e}')
                break

            for item in d.get('data', []):
                num = item.get('number')
                if num is not None and num not in achadas:
                    achadas[num] = {
                        'edicao': int(num),
                        'data': item.get('date'),
                        'descricao': item.get('description') or '',
                        'arquivo': item.get('file_published'),
                    }

            if progress:
                progress(ano, page, len(achadas))

            pag = (d.get('meta') or {}).get('pagination') or {}
            if page >= int(pag.get('last_page') or 1):
                break
            page += 1
            time.sleep(0.05)
    return achadas


def _add_erro(msg):
    with _lock:
        _state['erros'].append(msg)


def edicoes_locais(conn):
    return {r[0] for r in conn.execute('SELECT edicao FROM edicoes_processadas')}


def _nome_pdf(edicao, data_iso):
    return os.path.join(PDF_DIR, f'{data_iso}_edicao_{int(edicao):04d}.pdf')


def _baixar(url, destino):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as r:
        conteudo = r.read()
    if not conteudo.startswith(b'%PDF'):
        raise ValueError('resposta nao e um PDF')
    tmp = destino + '.part'
    with open(tmp, 'wb') as f:
        f.write(conteudo)
    os.replace(tmp, destino)
    return len(conteudo)


def processar_edicao(conn, info):
    """Baixa (se preciso) e extrai uma edicao. Devolve n. de atos inseridos."""
    edicao = info['edicao']
    data_iso = info['data']
    caminho = _nome_pdf(edicao, data_iso)

    if not os.path.exists(caminho):
        os.makedirs(PDF_DIR, exist_ok=True)
        _baixar(info['arquivo'], caminho)

    doc = fitz.open(caminho)
    entries = extract_entries(doc, data_diario=data_iso)
    doc.close()

    inseridos = 0
    for e in entries:
        cur = conn.execute(
            """INSERT OR IGNORE INTO atos
               (edicao, data_diario, tipo, numero, data_ato, titulo, texto,
                pagina_inicio, pagina_fim, arquivo_pdf)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (edicao, data_iso, e['tipo'], e['numero'], e['data_ato'],
             e['titulo'], e['texto'], e['pagina_inicio'], e['pagina_fim'],
             caminho))
        inseridos += cur.rowcount

    conn.execute(
        """INSERT OR REPLACE INTO edicoes_processadas
           (edicao, arquivo_pdf, n_entries, status, processed_at)
           VALUES (?,?,?,?,datetime('now'))""",
        (edicao, caminho, len(entries), 'ok'))
    conn.commit()
    return inseridos


def rodar_atualizacao(anos=None):
    """Executa o ciclo completo. Deve rodar em uma thread propria."""
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        _set(running=True, phase='listando', done=0, total=0, novas_edicoes=0,
             novos_atos=0, erros=[], finished_at=None,
             started_at=datetime.now().isoformat(timespec='seconds'),
             message='Consultando a API do Diário Oficial…')

        def prog(ano, page, achadas):
            _set(message=f'Consultando {ano} (página {page}) — '
                         f'{achadas} edições encontradas…')

        remotas = listar_edicoes_remotas(anos=anos, progress=prog)
        locais = edicoes_locais(conn)
        novas = sorted(n for n in remotas if n not in locais)

        _set(phase='processando', total=len(novas), novas_edicoes=len(novas),
             message=(f'{len(novas)} nova(s) edição(ões) para processar.'
                      if novas else 'Base já está atualizada.'))

        total_atos = 0
        for i, num in enumerate(novas, 1):
            info = remotas[num]
            _set(done=i - 1,
                 message=f'Processando edição {num} ({i} de {len(novas)})…')
            try:
                total_atos += processar_edicao(conn, info)
                _set(novos_atos=total_atos)
            except Exception as e:
                _add_erro(f'edição {num}: {e}')
            _set(done=i)

        if total_atos:
            _set(message='Atualizando o índice de busca…')
            _sync_fts(conn)

        resumo = (f'Concluído: {len(novas)} edição(ões) processada(s), '
                  f'{total_atos} novo(s) ato(s) adicionado(s).'
                  if novas else 'Nada a fazer — a base já estava atualizada.')
        _set(phase='concluido', message=resumo,
             finished_at=datetime.now().isoformat(timespec='seconds'))

        if ao_concluir:
            try:
                ao_concluir(total_atos)
            except Exception as e:
                _add_erro(f'pos-atualizacao: {e}')
    except Exception as e:
        _add_erro(str(e))
        _set(phase='erro', message=f'Falha na atualização: {e}',
             finished_at=datetime.now().isoformat(timespec='seconds'))
    finally:
        _set(running=False)
        conn.close()


# Gancho opcional: app.py usa para disparar a indexacao da IA logo apos
# entrarem atos novos, sem que o updater precise conhecer o RAG.
ao_concluir = None


def _sync_fts(conn):
    """Reconstroi o indice de busca apos novas insercoes."""
    try:
        conn.execute("INSERT INTO atos_fts(atos_fts) VALUES('rebuild')")
        conn.commit()
    except Exception as e:
        _add_erro(f'indice de busca: {e}')


def iniciar_em_background(anos=None):
    with _lock:
        if _state['running']:
            return False
    t = threading.Thread(target=rodar_atualizacao, kwargs={'anos': anos},
                         daemon=True)
    t.start()
    return True
