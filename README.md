# Legislação Municipal de Araras

Busca no texto integral das **leis e decretos** do Município de Araras (SP),
extraídos automaticamente do Diário Oficial Eletrônico, com resumo por IA.

- **3.706 atos normativos** — leis, leis complementares, decretos e decretos legislativos
- **2.957 edições** do Diário Oficial processadas (2014 → 2026)
- Busca full-text (SQLite FTS5), filtros por tipo/número/ano
- Resumo automático de cada ato via **Google Gemini**, salvo no banco
- Atualização automática direto da API oficial do Diário

## Como funciona

1. `updater.py` consulta a API pública do Diário Oficial de Araras e descobre
   edições novas.
2. Baixa o PDF da edição e lê o **SUMÁRIO** para localizar cada publicação.
3. Isola apenas leis e decretos (descarta portarias, editais, extratos etc.),
   recorta o texto das páginas certas e limpa cabeçalho/rodapé do diário.
4. Grava no SQLite e reindexa a busca.

> As edições trazem anexos com numeração de página própria. O extrator
> distingue o corpo do diário dos anexos — sem isso o recorte pega a página
> errada (ver `scripts/auditoria.py`).

## Rodando localmente

```bash
pip install -r requirements.txt
cp .env.example .env        # coloque sua GEMINI_API_KEY
python scripts/app.py
```

Abra <http://127.0.0.1:5050>.

## Deploy (Railway)

O banco (`db/araras_legislacao.db`) vai versionado como **semente**, então o
app sobe já com todo o acervo.

Variáveis de ambiente:

| Variável | Obrigatória | Descrição |
|---|---|---|
| `GEMINI_API_KEY` | sim (para resumos) | Chave do Google AI Studio |
| `DATA_DIR` | recomendada | Caminho do volume persistente (ex.: `/data`) |
| `GEMINI_MODEL` | não | Padrão: `gemini-2.5-flash` |

**Sobre o `DATA_DIR`:** o disco do container é efêmero. Sem um volume, os
resumos gerados e as edições novas se perdem no próximo deploy. Com um volume
montado em `/data` e `DATA_DIR=/data`, o banco é copiado para lá na primeira
subida e passa a persistir.

O `Procfile` usa `--workers 1 --threads 8` de propósito: o progresso da
atualização vive na memória do processo, e vários workers dariam status
inconsistente.

## Estrutura

```
scripts/
  app.py           servidor Flask + API
  config.py        caminhos (local x volume do Railway)
  extract_lib.py   extração de leis/decretos do PDF
  format_lib.py    remonta o texto do PDF em blocos legíveis
  updater.py       busca e processa edições novas
  auditoria.py     mede a qualidade da extração
  rebuild_db.py    reprocessa tudo preservando resumos
  build_db.py      carga inicial
  templates/       interface web
db/
  araras_legislacao.db
```

## Fonte dos dados

API pública do Diário Oficial (Assistech Publicações):

```
https://api.assistechpublicacoes.com.br/v1/public/tenants/pmararassp/official-gazettes?year=2026&page=1
```

Dados públicos da Prefeitura Municipal de Araras. Este projeto é uma ferramenta
de consulta não oficial — **para qualquer uso legal, confira o texto no Diário
Oficial**. Os resumos são gerados por IA e podem conter erros.
