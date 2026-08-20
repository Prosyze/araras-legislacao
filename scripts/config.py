"""Configuracao de caminhos, valida tanto local quanto no Railway.

Em producao o disco do container e efemero: monta-se um volume (DATA_DIR) e o
banco vive nele. Na primeira subida, o banco versionado no repositorio e
copiado para o volume como semente.
"""
import os
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Banco versionado no repositorio (semente somente-leitura).
SEED_DB = os.path.join(BASE_DIR, 'db', 'araras_legislacao.db')

# Volume persistente, quando houver. Sem ele, tudo roda direto no repositorio.
DATA_DIR = os.environ.get('DATA_DIR', '').strip()

if DATA_DIR:
    DB_PATH = os.path.join(DATA_DIR, 'araras_legislacao.db')
    PDF_DIR = os.environ.get('PDF_DIR') or os.path.join(DATA_DIR, 'diarios_oficiais')
else:
    DB_PATH = os.environ.get('DB_PATH') or SEED_DB
    PDF_DIR = os.environ.get('PDF_DIR') or os.path.join(BASE_DIR, 'diarios_oficiais')

GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')


def preparar_dados():
    """Garante que o banco exista no destino antes do primeiro acesso."""
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    os.makedirs(PDF_DIR, exist_ok=True)

    if not os.path.exists(DB_PATH):
        if not os.path.exists(SEED_DB):
            raise FileNotFoundError(
                f'Banco nao encontrado em {DB_PATH} nem semente em {SEED_DB}')
        shutil.copy2(SEED_DB, DB_PATH)
        print(f'[config] banco semeado: {SEED_DB} -> {DB_PATH}', flush=True)

    return DB_PATH
