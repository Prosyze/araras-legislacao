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

O repositório já traz o banco pronto em `db/araras_legislacao.db` (acervo,
índice de busca, chunks e vetores da IA), versionado com **Git LFS**.

```bash
git lfs install                 # uma vez por máquina, antes do clone
git clone git@github.com:Prosyze/araras-legislacao.git
cd araras-legislacao
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # coloque sua GEMINI_API_KEY
python scripts/app.py
```

Abra <http://127.0.0.1:5050>. Com várias threads no Windows:
`cd scripts && waitress-serve --listen=127.0.0.1:5050 app:app`.

Se `db/araras_legislacao.db` tiver ~130 bytes, o LFS não baixou o arquivo:
rode `git lfs pull`.

## Deploy (Railway)

O que vai versionado é `db/semente.db` — só o acervo (`atos`,
`edicoes_processadas` e o índice de busca), gerado por
`python scripts/gerar_semente.py`. Rode e commite sempre que o acervo mudar.
O banco de trabalho `db/araras_legislacao.db` fica fora do git: ele carrega
também os chunks e vetores da busca inteligente, que o servidor refaz sozinho.

Na primeira subida a semente é copiada para o volume. Nos deploys seguintes o
volume já existe e não é sobrescrito — então `manutencao.sincronizar_com_semente`
regrava, ato a ato, só os textos que a semente corrigiu, sem tocar nas edições
que o `updater` capturou depois do último deploy. Reextrair em produção não é
opção: os PDFs do Diário não vão para o container.

Variáveis de ambiente:

| Variável | Obrigatória | Descrição |
|---|---|---|
| `GEMINI_API_KEY` | sim (para resumos) | Chave do Google AI Studio |
| `GEMINI_API_KEY_FALLBACK_1`, `_2`, ... | não | Chaves de reserva. Quota estourada (429) troca de chave em vez de esperar |
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
  manutencao.py    alinha o banco de produção com as regras atuais
  reextrair.py     reaplica o extrator ao acervo sem recriar o banco
  rebuild_db.py    reprocessa tudo do zero preservando resumos
  build_db.py      carga inicial
  gerar_semente.py gera db/semente.db para o deploy
  tests/           testes do extrator e da busca inteligente
  templates/       interface web
db/
  semente.db            versionado — acervo + índice de busca
  araras_legislacao.db  banco de trabalho (fora do git)
```

> Corrigiu regra de extração? `python scripts/reextrair.py --dry-run` mostra o
> que mudaria; sem a flag, aplica e preserva os ids (e com eles os resumos e o
> índice da busca inteligente). Depois, `gerar_semente.py` para levar ao deploy.

## Fonte dos dados

API pública do Diário Oficial (Assistech Publicações):

```
https://api.assistechpublicacoes.com.br/v1/public/tenants/pmararassp/official-gazettes?year=2026&page=1
```

Dados públicos da Prefeitura Municipal de Araras. Este projeto é uma ferramenta
de consulta não oficial — **para qualquer uso legal, confira o texto no Diário
Oficial**. Os resumos são gerados por IA e podem conter erros.
