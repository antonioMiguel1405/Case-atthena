"""
Ranking semântico de métricas: embeddings OpenAI ou LLM de desambiguação, com fallback local.
"""
from __future__ import annotations

import math
import sqlite3
from app.llm_extract import embed_texts_openai, llm_pick_metric_key


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def rank_facts_semantic(
    question_focus: str,
    question_full_norm: str,
    rows: list[sqlite3.Row],
) -> tuple[list[tuple[float, sqlite3.Row]], str] | None:
    """
    Retorna lista (score, row) ordenada por score descendente, ou None para usar fallback lexical.
    Segundo valor: nota técnica para raciocinio.
    """
    if not rows:
        return None
    try:
        labels = [
            f"{r['metric_key']} | {r['display_name']} | {r['row_label_raw'] or ''}" for r in rows
        ]
        texts = [question_focus or question_full_norm] + labels
        emb = embed_texts_openai(texts)
        if emb and len(emb) == len(texts):
            qv = emb[0]
            scored: list[tuple[float, sqlite3.Row]] = []
            for i, r in enumerate(rows):
                scored.append((_cosine(qv, emb[i + 1]), r))
            scored.sort(key=lambda x: x[0], reverse=True)
            return (
                scored,
                "• **Ranking métrica:** similaridade de cosseno entre *embedding* da intenção e rótulos "
                "(`metric_key` + `display_name` + `row_label_raw`) via OpenAI.",
            )

        candidates = [
            {
                "metric_key": r["metric_key"],
                "display_name": r["display_name"],
                "row_label_raw": r["row_label_raw"] or "",
            }
            for r in rows
        ]
        mk, reason = llm_pick_metric_key(question_focus or question_full_norm, candidates)
        if mk:
            for r in rows:
                if r["metric_key"] == mk:
                    return [(1.0, r)], (
                        "• **Ranking métrica:** classificação via LLM (embeddings indisponível ou falhou). "
                        f"{reason}"
                    )
    except Exception:
        return None
    return None
