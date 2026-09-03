"""Catalogo das normas estruturantes do Municipio (codigos, estatutos, planos).

Sao poucas leis, mas respondem pela maior parte das consultas do dia a dia --
e ficavam escondidas no meio de 3.700 atos, alcancaveis so por quem ja sabia o
numero. O catalogo e escrito a mao de proposito: "ser um codigo" nao esta em
lugar nenhum no texto do Diario, e adivinhar pela ementa erraria justamente
nos casos que importam.

Cada entrada aponta para o ato pelo par tipo+numero, nunca pelo id: o id muda
quando a base e reconstruida do zero.

`na_base=False` marca a norma anterior a 2014, fora do alcance do acervo. O
card existe mesmo assim para dizer o que consta -- as alteracoes posteriores --
em vez de deixar o leitor procurando um texto que nunca esteve aqui.
"""
import re
import threading
import time
import unicodedata

from format_lib import structure_text

CATALOGO = [
    {
        'slug': 'posturas',
        'nome': 'Código de Posturas',
        'tema': 'Cidade e convivência',
        'descricao': 'Licenciamento e funcionamento de estabelecimentos, uso do '
                     'espaço público, higiene, silêncio, publicidade e as multas '
                     'de cada infração.',
        'tipo': 'LEI COMPLEMENTAR', 'numero': '167',
    },
    {
        'slug': 'obras',
        'nome': 'Código de Obras e Edificações',
        'tema': 'Construir e reformar',
        'descricao': 'Regras para projetar, aprovar, executar e regularizar '
                     'construções: recuos, alvarás, habite-se e responsabilidades '
                     'do proprietário e do profissional.',
        'tipo': 'LEI COMPLEMENTAR', 'numero': '162',
    },
    {
        'slug': 'arborizacao',
        'nome': 'Código de Arborização Urbana',
        'tema': 'Meio ambiente',
        'descricao': 'Plantio, poda, supressão e compensação de árvores em vias, '
                     'praças e calçadas.',
        'tipo': 'LEI COMPLEMENTAR', 'numero': '228',
    },
    {
        'slug': 'residuos-solidos',
        'nome': 'Política Municipal de Resíduos Sólidos',
        'tema': 'Meio ambiente',
        'descricao': 'Marco regulatório da coleta, destinação e responsabilidade '
                     'pelos resíduos gerados no Município.',
        'tipo': 'LEI', 'numero': '4.927',
    },
    {
        'slug': 'plano-diretor-turismo',
        'nome': 'Plano Diretor de Turismo',
        'tema': 'Desenvolvimento',
        'descricao': 'Diretrizes, inventário e programas do turismo municipal, '
                     'na revisão aprovada em 2024.',
        'tipo': 'LEI', 'numero': '5.769',
    },
    {
        'slug': 'plano-diretor',
        'nome': 'Plano Diretor e Uso do Solo',
        'tema': 'Uso e ocupação do solo',
        'descricao': 'Zoneamento, parcelamento e diretrizes de crescimento da '
                     'cidade.',
        'tipo': 'LEI COMPLEMENTAR', 'numero': '3.901',
        'na_base': False, 'ano_origem': 2006,
    },
    {
        'slug': 'tributario',
        'nome': 'Código Tributário Municipal',
        'tema': 'Tributos',
        'descricao': 'IPTU, ISS, taxas, lançamento e cobrança dos tributos do '
                     'Município.',
        'tipo': 'LEI', 'numero': '3.362', 'na_base': False, 'ano_origem': 2001,
    },
]

_CACHE = {'em': 0.0, 'dados': None}
_CACHE_TTL = 300  # a base so muda quando o updater roda; 5 min ja acompanha
_LOCK = threading.Lock()


def _sem_acento(s):
    return unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode()


def _digitos(numero):
    return re.sub(r'\D', '', numero or '')


def _localizar(db, entrada):
    """Ato que instituiu a norma. Numero repetido acontece (a mesma lei sai
    duas vezes no diario): fica a versao com o texto mais completo."""
    return db.execute(
        """SELECT id, tipo, numero, data_ato, titulo, texto
             FROM atos
            WHERE tipo = ? AND REPLACE(numero, '.', '') = ?
            ORDER BY length(texto) DESC LIMIT 1""",
        (entrada['tipo'], _digitos(entrada['numero']))).fetchone()


def _citacao_re(entrada):
    """Como um ato posterior se refere a esta norma no proprio texto."""
    d = _digitos(entrada['numero'])
    if len(d) > 3:  # 4.927 aparece com e sem o ponto de milhar
        alvo = r'%s\.?%s' % (d[:-3], d[-3:])
    else:
        alvo = d
    # "Lei Municipal no. 3.362" e "Lei Complementar Municipal no 3.901" sao
    # tao comuns quanto a forma seca. Ja "complementar" nunca e opcional: Lei
    # 90 e Lei Complementar 90 sao normas diferentes.
    prefixo = (r'lei\s+complementar(?:\s+municipal)?'
               if entrada['tipo'] == 'LEI COMPLEMENTAR' else r'lei(?:\s+municipal)?')
    return re.compile(r'%s\s*n?[o°º.\s]{0,4}%s(?!\d)' % (prefixo, alvo), re.I)


def alteracoes(db, entrada, limite=80):
    """Atos posteriores que citam a norma -- alteracoes e regulamentos.

    O LIKE so pre-seleciona candidatos; quem decide e a regex, porque
    '167' sozinho casa com valor, numero de processo e artigo de outra lei.
    """
    origem = _localizar(db, entrada)
    d = _digitos(entrada['numero'])
    com_ponto = f'{d[:-3]}.{d[-3:]}' if len(d) > 3 else d
    desde = origem['data_ato'] if origem else None
    ato_id = origem['id'] if origem else -1

    sql = """SELECT id, edicao, tipo, numero, data_ato, titulo, texto,
                    (resumo IS NOT NULL) AS tem_resumo
               FROM atos
              WHERE id <> ? AND (texto LIKE ? OR texto LIKE ?)"""
    args = [ato_id, f'%{com_ponto}%', f'%{d}%']
    if desde:
        sql += ' AND COALESCE(data_ato, data_diario) >= ?'
        args.append(desde)
    sql += ' ORDER BY COALESCE(data_ato, data_diario) DESC'

    padrao = _citacao_re(entrada)
    achados = []
    for r in db.execute(sql, args):
        corpo = re.sub(r'\s+', ' ', _sem_acento(r['texto']))
        m = padrao.search(corpo)
        if not m:
            continue
        achados.append({
            'id': r['id'], 'edicao': r['edicao'], 'tipo': r['tipo'],
            'numero': r['numero'], 'tem_resumo': r['tem_resumo'],
            'data_ato': r['data_ato'], 'titulo': r['titulo'],
            'trecho': corpo[max(0, m.start() - 90):m.start() + 150].strip(),
        })
        if len(achados) >= limite:
            break
    return achados


def _resumo_estrutura(texto):
    """Quantos artigos e quantas divisoes o texto tem, pela mesma leitura que
    a tela usa para montar o sumario."""
    blocos = structure_text(texto)
    return (sum(1 for b in blocos if b['tipo'] == 'artigo'),
            sum(1 for b in blocos if b['tipo'] == 'heading'))


def _montar(db):
    saida = []
    for e in CATALOGO:
        item = {k: e[k] for k in ('slug', 'nome', 'tema', 'descricao', 'tipo', 'numero')}
        item['na_base'] = e.get('na_base', True)
        item['ano_origem'] = e.get('ano_origem')
        row = _localizar(db, e) if item['na_base'] else None
        if row:
            artigos, divisoes = _resumo_estrutura(row['texto'])
            item.update(ato_id=row['id'], data_ato=row['data_ato'],
                        titulo=row['titulo'], artigos=artigos, divisoes=divisoes)
        else:
            item['na_base'] = False
            item['ato_id'] = None
        posteriores = alteracoes(db, e)
        item['alteracoes'] = len(posteriores)
        item['ultima_alteracao'] = posteriores[0]['data_ato'] if posteriores else None
        saida.append(item)
    return saida


def listar(db, forcar=False):
    """Catalogo pronto para a tela. Remontar custa a leitura dos textos
    inteiros, entao o resultado fica em cache curto."""
    with _LOCK:
        agora = time.time()
        if forcar or _CACHE['dados'] is None or agora - _CACHE['em'] > _CACHE_TTL:
            _CACHE['dados'] = _montar(db)
            _CACHE['em'] = agora
        return _CACHE['dados']


def por_slug(slug):
    for e in CATALOGO:
        if e['slug'] == slug:
            return e
    return None
