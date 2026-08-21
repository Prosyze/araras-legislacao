"""Quebra o ato normativo em dispositivos recuperáveis.

A divisão segue a estrutura jurídica, não um tamanho fixo: o cabeçalho
(título + ementa + preâmbulo) vira uma unidade, e cada artigo vira outra
levando junto seus parágrafos, incisos e alíneas — que só fazem sentido
colados ao caput. Capítulos e seções não viram chunk: viram metadado de
contexto dos artigos que estão sob eles.

Reaproveita a classificação de blocos de `format_lib`, que já é usada para
renderizar o ato na tela.
"""
import hashlib
import re

from format_lib import structure_text

from . import settings

# "Art. 1º", "Artigo 12-A", "Art. 3." — captura número e sufixo de letra.
ART_NUM_RE = re.compile(
    r'^Art(?:igo)?\.?\s*(\d+)\s*[º°o]?\s*(?:[-–]\s*([A-Za-z]))?', re.IGNORECASE)
PAR_NUM_RE = re.compile(r'^§\s*(\d+)', re.IGNORECASE)
PAR_UNICO_RE = re.compile(r'^Par[áa]grafo\s+[úu]nico', re.IGNORECASE)
INCISO_NUM_RE = re.compile(r'^([IVXLC]{1,7})\s*[-–—).]')
ANEXO_RE = re.compile(r'^ANEXO\s*([IVXLC0-9]*)', re.IGNORECASE)

# Blocos que se agregam ao artigo corrente.
FILHOS = ('paragrafo', 'inciso', 'alinea', 'paragrafo-comum', 'rotulo')
# Ruído de publicação: não descreve norma nenhuma.
DESCARTAR = ('assinatura', 'registro')


def hash_texto(s):
    return hashlib.sha256((s or '').encode('utf-8')).hexdigest()


def hash_ato(titulo, texto):
    """Identidade do conteúdo do ato: muda => os chunks são refeitos."""
    return hash_texto(f'{titulo}\n\n{texto}')


def _num_artigo(bloco):
    m = ART_NUM_RE.match(bloco)
    if not m:
        return None
    num, letra = m.group(1), m.group(2)
    return f'{int(num)}-{letra.upper()}' if letra else str(int(num))


def _rotulo_artigo(num):
    if num is None:
        return None
    partes = num.split('-')
    base = partes[0]
    sufixo = f'-{partes[1]}' if len(partes) > 1 else ''
    # A norma brasileira usa ordinal até o nono artigo e cardinal a partir do 10.
    marca = 'º' if base.isdigit() and int(base) <= 9 else ''
    return f'Art. {base}{marca}{sufixo}'


def _paragrafos(blocos):
    achados = []
    for b in blocos:
        if PAR_UNICO_RE.match(b):
            achados.append('unico')
        else:
            m = PAR_NUM_RE.match(b)
            if m:
                achados.append(str(int(m.group(1))))
    return ','.join(dict.fromkeys(achados)) or None


def _incisos(blocos):
    achados = []
    for b in blocos:
        m = INCISO_NUM_RE.match(b)
        if m:
            achados.append(m.group(1).upper())
    return ','.join(dict.fromkeys(achados)) or None


def _quebrar_grande(blocos, limite):
    """Divide uma sequência de blocos em partes sob o limite, sempre em
    fronteira de bloco. Um bloco isolado maior que o limite (tabela de
    dotação, anexo colado) é cortado em fronteira de frase."""
    partes, atual, tam = [], [], 0
    for b in blocos:
        if len(b) > limite:
            if atual:
                partes.append(atual)
                atual, tam = [], 0
            buf, n = [], 0
            for p in re.split(r'(?<=[.;])\s+', b):
                if n + len(p) > limite and buf:
                    partes.append(buf)
                    buf, n = [], 0
                buf.append(p)
                n += len(p) + 1
            if buf:
                partes.append(buf)
            continue
        if tam + len(b) > limite and atual:
            partes.append(atual)
            atual, tam = [], 0
        atual.append(b)
        tam += len(b) + 1
    if atual:
        partes.append(atual)
    return partes or [[]]


class _Acumulador:
    """Junta blocos até que um novo dispositivo comece."""

    def __init__(self):
        self.reset()

    def reset(self, tipo=None, rotulo=None, artigo=None, secao=None):
        self.tipo = tipo
        self.rotulo = rotulo
        self.artigo = artigo
        self.secao = secao
        self.blocos = []

    @property
    def vazio(self):
        return not self.blocos


def chunk_ato(ato):
    """`ato` é um mapeamento com id, titulo, texto (e opcionalmente tipo/numero).

    Devolve a lista de chunks na ordem do documento, cada um pronto para
    gravar em `ato_chunks`.
    """
    blocos = structure_text(ato['texto'] or '')
    limite = settings.CHUNK_MAX_CHARS

    chunks = []
    acc = _Acumulador()
    secao = None
    em_anexo = None
    viu_artigo = False

    def emitir():
        if acc.vazio:
            return
        for i, parte in enumerate(_quebrar_grande(acc.blocos, limite)):
            texto = '\n'.join(parte).strip()
            if len(texto) < settings.CHUNK_MIN_CHARS:
                continue
            rotulo = acc.rotulo
            if rotulo and i:
                rotulo = f'{rotulo} (parte {i + 1})'
            chunks.append({
                'ordem': len(chunks),
                'tipo_chunk': acc.tipo,
                'rotulo': rotulo,
                'artigo': acc.artigo,
                'paragrafo': _paragrafos(parte),
                'incisos': _incisos(parte),
                'secao': acc.secao,
                'texto': texto,
                'n_chars': len(texto),
                'hash': hash_texto(texto),
            })
        acc.reset()

    for b in blocos:
        tipo, txt = b['tipo'], b['texto']

        if tipo in DESCARTAR:
            emitir()
            continue

        if tipo == 'heading':
            emitir()
            if ANEXO_RE.match(txt):
                em_anexo = txt.strip()
                secao = em_anexo
            else:
                secao = txt.strip()
            continue

        if tipo == 'artigo':
            emitir()
            viu_artigo = True
            num = _num_artigo(txt)
            acc.reset(tipo='artigo', rotulo=_rotulo_artigo(num) or 'Artigo',
                      artigo=num, secao=secao)
            acc.blocos.append(txt)
            continue

        if tipo in ('titulo', 'ementa', 'preambulo') and not viu_artigo:
            if acc.tipo != 'cabecalho':
                emitir()
                acc.reset(tipo='cabecalho', rotulo='Ementa', secao=None)
            acc.blocos.append(txt)
            continue

        if tipo in FILHOS and acc.tipo == 'artigo':
            acc.blocos.append(txt)
            continue

        # Sobrou: texto solto antes do primeiro artigo, corpo de anexo, ou
        # filho órfão (parágrafo sem artigo, por falha de extração do PDF).
        alvo = 'anexo' if em_anexo else 'trecho'
        if acc.tipo != alvo:
            emitir()
            acc.reset(tipo=alvo, rotulo=em_anexo or secao, secao=secao)
        acc.blocos.append(txt)

    emitir()

    # Um ato sem estrutura reconhecível (texto curto, extração ruim) ainda
    # precisa ser recuperável: vira um chunk único.
    if not chunks:
        texto = (ato['texto'] or '').strip()
        if len(texto) >= settings.CHUNK_MIN_CHARS:
            chunks.append({
                'ordem': 0, 'tipo_chunk': 'trecho', 'rotulo': 'Texto integral',
                'artigo': None, 'paragrafo': None, 'incisos': None,
                'secao': None, 'texto': texto[:limite],
                'n_chars': min(len(texto), limite),
                'hash': hash_texto(texto[:limite]),
            })

    return chunks


def texto_para_embedding(chunk, ato):
    """O vetor é do dispositivo, mas com o cabeçalho da norma junto: sem isso
    o "Art. 3º" de dois mil decretos vira o mesmo ponto no espaço vetorial."""
    partes = []
    titulo = (ato.get('titulo') or '').strip()
    if titulo:
        partes.append(titulo)
    if chunk.get('secao'):
        partes.append(chunk['secao'])
    if chunk.get('rotulo') and chunk['tipo_chunk'] == 'artigo':
        partes.append(chunk['rotulo'])
    partes.append(chunk['texto'])
    return '\n'.join(partes)[:settings.EMBED_MAX_CHARS]
