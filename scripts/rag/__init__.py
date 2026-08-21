"""Consulta inteligente da legislação (RAG).

Camadas, de baixo para cima:

    settings   parâmetros configuráveis por ambiente
    schema     DDL idempotente das tabelas do RAG
    chunker    quebra o ato em dispositivos (artigo, §, inciso)
    embeddings cliente de embeddings + (de)serialização dos vetores
    indexer    job de indexação em background (resumível)
    parser     interpreta a pergunta (norma citada, artigo, comparação)
    retrieval  busca híbrida: FTS5 + vetorial + filtros
    rerank     fusão, reordenação e seleção do contexto
    llm        chamada ao modelo generativo
    prompt     montagem do prompt e validação das citações
    service    orquestra o fluxo completo da consulta
"""
