"""
Chamadas a provedores de LLM para extração estruturada e embeddings.
Usa OPENAI_API_KEY ou GEMINI_API_KEY (variáveis de ambiente).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

# JSON esperado pela ingestão
EXTRACTION_SCHEMA_HINT = """
Retorne APENAS um objeto JSON válido com a estrutura:
{
  "company_legal_name": string,
  "ticker": string (ex.: MGLU3),
  "period_label": string (ex.: 1T22),
  "fiscal_year": number,
  "fiscal_quarter": number | null (1-4),
  "metrics": [
    {
      "metric_key": string (snake_case, ex.: receita_liquida),
      "display_name": string (rótulo do PDF),
      "statement_category": string (DRE, BP_ATIVO, BP_PASSIVO_PL, DFC, NOTA_19),
      "value_amount": number,
      "unit_scale": "thousands" | "units",
      "page_number": number,
      "section_title": string,
      "row_label": string,
      "scope": "consolidado" | "controladora"
    }
  ],
  "ingestion_notes": string (opcional, avisos)
}
Se não encontrar métricas confiáveis, use "metrics": []. Não invente números.
"""


def extract_financial_json_from_text(
    pdf_text: str,
    *,
    chunk_context: str | None = None,
) -> dict[str, Any]:
    """
    Envia o texto do PDF (ou um trecho) ao LLM e devolve dict parseado.
    `chunk_context` descreve o trecho (ex.: páginas 16–30); o modelo deve extrair só o que aparece nesse trecho.
    """
    chunk_instr = ""
    if chunk_context:
        chunk_instr = (
            f"\n\n**Trecho do documento:** {chunk_context}\n"
            "Extraia **apenas** linhas com valores numéricos presentes **neste trecho**. "
            "Repita `company_legal_name`, `ticker` e período fiscal se estiverem visíveis aqui; "
            "se não estiverem, use os mesmos valores que no restante do relatório se puder inferir, "
            "senão deixe métricas com os números encontrados e campos de cabeçalho vazios ou null.\n"
        )
    prompt = (
        "Você extrai dados de demonstrações financeiras de relatórios brasileiros (ITR/DFP).\n"
        + EXTRACTION_SCHEMA_HINT
        + chunk_instr
        + "\n\nTexto extraído do PDF:\n---\n"
        + pdf_text[:120000]
        + "\n---\n"
    )

    key_openai = os.environ.get("OPENAI_API_KEY", "").strip()
    key_gemini = os.environ.get("GEMINI_API_KEY", "").strip()

    if key_openai:
        return _extract_openai(prompt)
    if key_gemini:
        return _extract_gemini(prompt)
    raise RuntimeError(
        "Nenhuma chave de API configurada. Defina OPENAI_API_KEY ou GEMINI_API_KEY."
    )


def _extract_openai(prompt: str) -> dict[str, Any]:
    from openai import OpenAI

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY vazia após strip().")
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=os.environ.get("OPENAI_EXTRACT_MODEL", "gpt-4o-mini"),
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": "Você responde somente com JSON válido, sem markdown.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )
    raw = resp.choices[0].message.content or "{}"
    return json.loads(raw)


def _extract_gemini(prompt: str) -> dict[str, Any]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    model_name = os.environ.get("GEMINI_EXTRACT_MODEL", "gemini-2.0-flash")

    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )
    raw = response.text or "{}"
    return json.loads(raw)


def embed_texts_openai(texts: list[str]) -> list[list[float]] | None:
    """Embeddings em lote; retorna None se indisponível."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    from openai import OpenAI

    model = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    client = OpenAI(api_key=key)
    resp = client.embeddings.create(model=model, input=texts)
    return [item.embedding for item in resp.data]


def llm_pick_metric_key(question_focus: str, candidates: list[dict[str, str]]) -> tuple[str | None, str]:
    """
    candidates: [{"metric_key": "...", "display_name": "...", "row_label_raw": "..."}, ...]
    Retorna (metric_key escolhida ou None, nota curta).
    """
    key_openai = os.environ.get("OPENAI_API_KEY", "").strip()
    key_gemini = os.environ.get("GEMINI_API_KEY", "").strip()
    payload = json.dumps(candidates, ensure_ascii=False, indent=2)
    prompt = (
        "Dada a intenção do usuário sobre qual linha contábil consultar, escolha no máximo UMA "
        "entrada da lista pelo metric_key. Se nenhuma for adequada, retorne null.\n"
        "Regras contábeis: distinga valor total em moeda (ex.: lucro líquido em R$ mil) de métricas "
        "**por ação** (LPA/EPS). Se a pergunta pedir lucro ou resultado **por ação** e os candidatos "
        "forem apenas totais, retorne metric_key null. O mesmo para **margem** ou **EBITDA** se nenhum "
        "candidato mencionar explicitamente esse conceito.\n"
        f'Intenção (métrica): "{question_focus}"\n'
        f"Candidatos:\n{payload}\n"
        'Responda só JSON: {"metric_key": "<key ou null>", "reason": "<uma frase>"}'
    )

    try:
        if key_openai:
            from openai import OpenAI

            client = OpenAI(api_key=key_openai)
            resp = client.chat.completions.create(
                model=os.environ.get("OPENAI_EXTRACT_MODEL", "gpt-4o-mini"),
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": "Responda apenas JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
            )
            data = json.loads(resp.choices[0].message.content or "{}")
        # Substituir o bloco elif key_gemini: dentro de llm_pick_metric_key
        elif key_gemini:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=key_gemini)
            model_name = os.environ.get("GEMINI_EXTRACT_MODEL", "gemini-2.0-flash")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                ),
            )
            data = json.loads(response.text or "{}")
        else:
            return None, "sem API de LLM para desambiguação"
    except Exception as e:
        return None, f"erro LLM: {e}"

    mk = data.get("metric_key")
    reason = str(data.get("reason", ""))
    if mk is None or mk == "null" or mk == "":
        return None, reason or "LLM não escolheu métrica"
    return str(mk).strip(), reason


def _slug_metric_key(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "metrica_extraida"
