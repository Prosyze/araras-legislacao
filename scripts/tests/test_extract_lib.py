"""Testes do recorte dos atos no PDF do Diário Oficial.

    python scripts/tests/test_extract_lib.py

Os testes de unidade não tocam em disco. Os de integração usam os PDFs reais
em `diarios_oficiais/` e são pulados quando a edição não está baixada.
"""
import os
import re
import sys
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(AQUI)
BASE = os.path.dirname(SCRIPTS)
sys.path.insert(0, SCRIPTS)

import fitz  # noqa: E402

import extract_lib as E  # noqa: E402

PDF_DIR = os.path.join(BASE, 'diarios_oficiais')


def abrir(nome):
    caminho = os.path.join(PDF_DIR, nome)
    if not os.path.exists(caminho):
        raise unittest.SkipTest(f'edição não baixada: {nome}')
    return fitz.open(caminho)


def por_titulo(entradas):
    """Primeira ocorrência de cada título, como faz a carga no banco: o
    sumário às vezes repete um título numa página onde está outro ato."""
    mapa = {}
    for e in entradas:
        mapa.setdefault(e['titulo'], e)
    return mapa


# ============================================================ início do recorte
class TestInicioDoRecorte(unittest.TestCase):
    """O ato começa na repetição vizinha do título, nunca numa menção adiante."""

    TITULO = E._normalize('DECRETO Nº 1, DE 2 DE MARÇO DE 2020')

    def test_pega_a_segunda_repeticao_vizinha(self):
        # O diário imprime o título como cabeçalho de seção e logo abaixo como
        # título do próprio ato; o corpo é o que vem depois do segundo.
        linhas = ['DECRETO N. 1, DE 2 DE MARÇO DE 2020',
                  'DECRETO N. 1, DE 2 DE MARÇO DE 2020.',
                  'DECRETA:', 'Art. 1) - Fica criado o que segue.']
        self.assertEqual(E._find_start_heading(linhas, self.TITULO), 1)

    def test_ignora_mencao_distante(self):
        # A menção lá no fim é outro ato citando este. Antes, o recorte
        # começava nela e o registro ficava só com a linha do título.
        linhas = (['DECRETO N. 1, DE 2 DE MARÇO DE 2020', 'DECRETA:']
                  + [f'Art. {i}) - Dispositivo.' for i in range(1, 40)]
                  + ['DECRETO N. 1, DE 2 DE MARÇO DE 2020 fica revogado.'])
        self.assertEqual(E._find_start_heading(linhas, self.TITULO), 0)

    def test_titulo_ausente(self):
        self.assertIsNone(E._find_start_heading(['Art. 1) - nada'], self.TITULO))


# ================================================================== chaves/anexo
class TestChavesDoAto(unittest.TestCase):

    def test_tolera_pontuacao_do_numero(self):
        chaves = E._chaves_do_ato('LEI', '5.761')
        self.assertIn(E._normalize('LEI Nº 5.761'), chaves)

    def test_variante_sem_zero_a_esquerda(self):
        chaves = E._chaves_do_ato('DECRETO LEGISLATIVO', '05')
        self.assertIn(E._normalize('DECRETO LEGISLATIVO Nº 5'), chaves)

    def test_sem_numero_nao_gera_chave(self):
        self.assertEqual(E._chaves_do_ato('LEI', None), [])


class TestAbreOutroAto(unittest.TestCase):

    CHAVES = E._chaves_do_ato('LEI', '5.761')

    def test_outro_ato_encerra(self):
        self.assertTrue(E._abre_outro_ato('LEI Nº 5.762, DE 12 DE JUNHO 2.024.',
                                          self.CHAVES))

    def test_o_proprio_ato_nao_encerra(self):
        self.assertFalse(E._abre_outro_ato('LEI Nº 5.761, DE 12 DE JUNHO 2.024.',
                                           self.CHAVES))

    def test_citacao_no_meio_do_texto_nao_encerra(self):
        # Título é linha em caixa alta; citação corrida não é cabeçalho.
        self.assertFalse(E._abre_outro_ato(
            'Lei nº 1.768, de 7 de agosto de 1987, que dispõe sobre',
            self.CHAVES))


# ============================================================== integração real
class TestEdicoesReais(unittest.TestCase):

    def test_lei_publicada_so_como_em_anexo(self):
        """Ed. 2383: o corpo do diário traz só o título e "EM ANEXO"; o texto
        está no PDF apenso da Câmara."""
        doc = abrir('2024-06-21_edicao_2383.pdf')
        try:
            atos = por_titulo(E.extract_entries(doc, data_diario='2024-06-21'))
        finally:
            doc.close()
        lei = next(a for t, a in atos.items() if '5.761' in t)
        self.assertIn('Dia', lei['texto'])
        self.assertRegex(lei['texto'], r'Art\.\s*1')
        self.assertGreater(len(lei['texto']), 800)

    def test_ato_extenso_nao_fica_so_no_titulo(self):
        """Ed. 1647: o título reaparece páginas adiante e o recorte antigo
        começava lá, gravando 43 caracteres."""
        doc = abrir('2021-08-13_edicao_1647.pdf')
        try:
            atos = por_titulo(E.extract_entries(doc, data_diario='2021-08-13'))
        finally:
            doc.close()
        dec = next(a for t, a in atos.items() if '6.915' in t)
        self.assertGreater(len(dec['texto']), 10000)
        self.assertIn('DECRETA', dec['texto'].upper())

    def test_lei_complementar_160_integral(self):
        """Ed. 1427: recorte que já estava certo continua idêntico — 51
        artigos, do Art. 1 ao fecho assinado."""
        doc = abrir('2020-10-02_edicao_1427.pdf')
        try:
            atos = por_titulo(E.extract_entries(doc, data_diario='2020-10-02'))
        finally:
            doc.close()
        lc = next(a for t, a in atos.items()
                  if t.startswith('LEI COMPLEMENTAR') and '160' in t)
        artigos = [l for l in lc['texto'].split('\n')
                   if re.match(r'Art\.\s*\d', l)]
        self.assertEqual(len(artigos), 51)
        self.assertIn('entra em vigor', lc['texto'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
