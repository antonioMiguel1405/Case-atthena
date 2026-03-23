# Case Atthena — Infraestrutura de Dados Financeiros

Projeto para o case técnico (estágio full-stack): modelagem de demonstrações financeiras (ITR), queries de exemplo, documentação das Partes A e B e API **FastAPI** + **SQLite** com **ingestão automática de PDFs**.

## Estrutura

| Caminho | Conteúdo |
|---------|----------|
| `sql/schema.sql` | DDL do banco (+ `ingestion_jobs`) |
| `sql/queries_example.sql` | Queries ilustrativas |
| `sql/seed.sql` | Dados de demonstração — aplicado **automaticamente no startup** se `companies` estiver vazia |
| `data/pdfs/` | Coloque aqui os PDFs de ITR/DFP; o servidor ingere no startup |
| `app/ingestion.py` | Extração `pdfplumber` em **chunks** de páginas + LLM por trecho → fusão → SQLite |
| `app/llm_extract.py` | Chamadas OpenAI / Gemini (JSON + embeddings) |
| `app/semantic_match.py` | Ranking de métricas por embedding ou LLM |
| `app/qa.py` | Perguntas / comparações / `raciocinio` |
| `docs/` | Entrega, fluxograma, tabela de testes |

## Variáveis de ambiente (ingestão + QA semântico)

Defina **pelo menos uma** chave:

- `OPENAI_API_KEY` — extração JSON (`gpt-4o-mini` por padrão) e embeddings `text-embedding-3-small` para casar métricas com a pergunta.
- `GEMINI_API_KEY` — alternativa para extração e desambiguação (se não houver chave OpenAI).

Opcionais: `OPENAI_EXTRACT_MODEL`, `GEMINI_EXTRACT_MODEL`, `OPENAI_EMBED_MODEL`.

**PDF longos (chunking):**

- `PDF_INGEST_CHUNK_PAGES` — quantas páginas por chamada à LLM (padrão **15**). Reduza se o trecho ultrapassar o contexto do modelo.
- `PDF_INGEST_MAX_TOTAL_PAGES` — teto de páginas a processar por arquivo (omitir = **todas** as páginas).

Sem chave: PDFs em `data/pdfs/` são ignorados na extração (status `skipped_no_api` em `ingestion_jobs`); o `/ask` usa **fallback lexical** (`SequenceMatcher`).

## API

```powershell
cd "c:\Users\Eleva\Desktop\case atthena"
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Na **primeira** subida, se `data/atthena_case.db` não existir, o **schema** é criado. Em seguida o startup:

1. Garante a tabela `ingestion_jobs` (`ensure_migrations`).
2. Se não houver linhas em `companies`, aplica `sql/seed.sql` (demo sem PDF/chave).
3. Varre `data/pdfs/*.pdf` e chama `process_new_pdf` (vários blocos de páginas + uma persistência por arquivo).

**Exemplo**:

```http
POST http://127.0.0.1:8000/ask
Content-Type: application/json

{"question": "Qual a receita líquida da Magazine Luiza no 1T22?"}
```

Documentação: `http://127.0.0.1:8000/docs`.

### Demo offline sem LLM

O seed é carregado sozinho quando `companies` está vazia. Para recarregar do zero, apague `data/atthena_case.db` e suba a API de novo (ou execute `sql/seed.sql` manualmente num banco já com schema, **somente** se a tabela estiver vazia — o script usa IDs fixos).

## Entrega oficial

Conforme o PDF do case: e-mail, documentos Etapa 1 e 2 e link do GitHub se aplicável.
