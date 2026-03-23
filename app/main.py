from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.database import (
    DB_PATH,
    ensure_demo_seed_if_empty,
    ensure_migrations,
    get_connection,
    init_schema_only,
)
from app.ingestion import PDF_DIR, scan_and_ingest_pdfs
from app.qa import answer_question

import os
from dotenv import load_dotenv

import logging
logger = logging.getLogger("uvicorn.error")

# Carrega as variáveis do arquivo .env para o sistema
load_dotenv() 

app = FastAPI(
    title="Case Atthena — API demo",
    version="0.1.0",
    description=(
        "API de demonstração: perguntas em linguagem natural sobre fatos financeiros no SQLite. "
        "A resposta inclui o campo **raciocinio** (rastro legível) para auditoria e para o Swagger UI."
    ),
)


class QuestionIn(BaseModel):
    question: str = Field(
        ...,
        min_length=3,
        description="Pergunta em português (empresa, período e ideia da métrica).",
        examples=[
            "Qual a receita líquida da Magazine Luiza no 1T22?",
            "Qual o aumento da receita da Magazine Luiza entre 2021 e 2022?",
        ],
    )


class ReferenceOut(BaseModel):
    documento: str | None = None
    pagina: int | None = None
    secao: str | None = None
    metrica: str | None = None
    periodo: str | None = Field(
        default=None,
        description="Rótulo do período (ex.: 1T22), útil em respostas comparativas.",
    )
    escopo: str | None = Field(
        default=None,
        description="Consolidado vs controladora (individual), alinhado a `financial_facts.is_consolidated`.",
    )


class QuestionOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "answer": "O valor de «Receita líquida de vendas» em Magazine Luiza S.A. no período 1T22 foi R$ 8,824,567 (thousands de reais), conforme MGLU_ITR_1T22.pdf.",
                    "raciocinio": "• Empresa: «Magazine Luiza S.A.» (MGLU3) — correspondência entre a pergunta e ticker/nome cadastrados no banco.\n• Período: inferido da pergunta (trimestre 1 de 2022) → rótulo 1T22, documento-fonte em uso.\n• Métrica: ...",
                    "sql_executed": "SELECT ff.value_amount ... WHERE ff.id = 1;",
                    "references": [
                        {
                            "documento": "MGLU_ITR_1T22.pdf",
                            "pagina": 7,
                            "secao": "Demonstração do Resultado",
                            "metrica": "receita_liquida",
                            "periodo": "1T22",
                            "escopo": "Consolidado",
                        }
                    ],
                }
            ]
        }
    )

    answer: str = Field(..., description="Resposta em linguagem natural com o valor e a fonte citada.")
    raciocinio: str = Field(
        ...,
        description=(
            "**Raciocínio por trás da resposta** (para demonstração no `/docs`): "
            "(1) como a empresa foi identificada; "
            "(2) como o período/documento foi escolhido; "
            "(3) pontuação de cada métrica candidata e decisão final (ou motivo de rejeição). "
            "Texto em Markdown leve com marcadores «•»."
        ),
        examples=[
            "• Empresa: …\n• Período: …\n• Métrica: score 4.20 → `receita_liquida` …"
        ],
    )
    sql_executed: str = Field(
        ...,
        description="SQL ilustrativo usado para recuperar o fato escolhido (ou vazio se não houve consulta).",
    )
    references: list[ReferenceOut] = Field(
        ...,
        description="Rastreabilidade: arquivo, página, seção e chave da métrica.",
    )


@app.on_event("startup")
def startup() -> None:
    if not DB_PATH.exists():
        init_schema_only()
    ensure_migrations()
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    results = scan_and_ingest_pdfs()

    skipped = [r for r in results if r.get("status") == "skipped_no_api"]
    if skipped:
        logger.warning(
            "%d PDF(s) ignorados por falta de chave de API. "
            "Configure OPENAI_API_KEY ou GEMINI_API_KEY no .env.",
            len(skipped)
        )

    conn = get_connection()
    try:
        n = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    finally:
        conn.close()

    if n == 0:
        logger.warning(
            "Banco vazio — nenhum PDF foi ingerido. O /ask não funcionará."
        )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/ask",
    response_model=QuestionOut,
    summary="Pergunta sobre demonstrações financeiras",
    response_description="Inclui `answer`, `raciocinio` (passo a passo), `sql_executed` e `references`.",
)
def ask(body: QuestionIn) -> QuestionOut:
    conn = get_connection()
    try:
        result = answer_question(conn, body.question)
    finally:
        conn.close()
    if not result.answer:
        raise HTTPException(status_code=500, detail="Resposta vazia")
    return QuestionOut(
        answer=result.answer,
        raciocinio=result.raciocinio,
        sql_executed=result.sql_executed,
        references=[ReferenceOut(**r) for r in result.references],
    )
