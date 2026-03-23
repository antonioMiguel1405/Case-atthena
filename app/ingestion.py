"""
Ingestão automática de PDFs financeiros em `data/pdfs/`.
Requer OPENAI_API_KEY ou GEMINI_API_KEY para extração via LLM.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pdfplumber

from app.database import DB_PATH, get_connection
from app.llm_extract import _slug_metric_key, extract_financial_json_from_text

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PDF_DIR = ROOT / "data" / "pdfs"
DEFAULT_CHUNK_PAGES = 15


def _ingest_chunk_pages() -> int:
    """Páginas por chamada à LLM (documentos longos = vários chunks)."""
    try:
        n = int(os.environ.get("PDF_INGEST_CHUNK_PAGES", str(DEFAULT_CHUNK_PAGES)))
    except ValueError:
        n = DEFAULT_CHUNK_PAGES
    return max(1, min(n, 500))


def _ingest_max_total_pages() -> int | None:
    """Limite opcional de páginas a processar (None = PDF inteiro)."""
    raw = os.environ.get("PDF_INGEST_MAX_TOTAL_PAGES", "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if n > 0 else None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _pdf_text_for_page_range(pdf: Any, start: int, end: int) -> str:
    parts: list[str] = []
    n = len(pdf.pages)
    start = max(0, min(start, n))      # start entre 0 e n
    end = max(start, min(end, n))      # end entre start e n
    for i in range(start, end):
        t = pdf.pages[i].extract_text() or ""
        parts.append(f"\n--- PÁGINA {i + 1} ---\n{t}")
    return "\n".join(parts)


def extract_pdf_text_first_pages(pdf_path: Path, max_pages: int = DEFAULT_CHUNK_PAGES) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        end = min(max_pages, len(pdf.pages))
        return _pdf_text_for_page_range(pdf, 0, end)


def _metric_dedupe_key(m: dict) -> tuple[Any, ...]:
    mk = _slug_metric_key(str(m.get("metric_key") or m.get("display_name") or "metric"))
    try:
        page = int(m.get("page_number") or 0)
    except (TypeError, ValueError):
        page = 0
    try:
        val = round(float(m.get("value_amount")), 6)
    except (TypeError, ValueError):
        val = 0.0
    row = (str(m.get("row_label") or "")[:120]).lower().strip()
    scope = (str(m.get("scope") or "consolidado")).lower().strip()
    cat = (str(m.get("statement_category") or "")).lower().strip()[:64]
    return (mk, page, val, row, scope, cat)


def _merge_financial_extractions(parts: list[dict]) -> dict[str, Any]:
    """
    Une várias respostas da LLM (uma por chunk de páginas).
    Metadados: primeiro valor não vazio vence; métricas: concatena e deduplica.
    """
    merged: dict[str, Any] = {}
    all_metrics: list[dict] = []
    note_parts: list[str] = []

    for p in parts:
        if not isinstance(p, dict):
            continue
        for fld in ("company_legal_name", "ticker", "period_label"):
            v = p.get(fld)
            if v is not None and str(v).strip():
                cur = merged.get(fld)
                if cur is None or not str(cur).strip():
                    merged[fld] = v
        fy = p.get("fiscal_year")
        if merged.get("fiscal_year") is None and fy is not None:
            try:
                iy = int(fy)
                if iy > 1900:
                    merged["fiscal_year"] = iy
            except (TypeError, ValueError):
                pass
        fq = p.get("fiscal_quarter")
        if merged.get("fiscal_quarter") is None and fq is not None:
            try:
                merged["fiscal_quarter"] = int(fq)
            except (TypeError, ValueError):
                pass
        for m in p.get("metrics") or []:
            if isinstance(m, dict):
                all_metrics.append(m)
        inn = p.get("ingestion_notes")
        if inn and str(inn).strip():
            note_parts.append(str(inn).strip())

    seen: set[tuple[Any, ...]] = set()
    deduped: list[dict] = []
    for m in all_metrics:
        if m.get("value_amount") is None:
            continue
        k = _metric_dedupe_key(m)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(m)

    merged["metrics"] = deduped
    merged["ingestion_notes"] = " | ".join(note_parts) if note_parts else ""
    return merged


def _ensure_metric(conn: sqlite3.Connection, metric_key: str, display_name: str, category: str) -> int:
    row = conn.execute(
        "SELECT id FROM metrics WHERE metric_key = ?", (metric_key,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE metrics SET display_name = ?, statement_category = ? WHERE id = ?",
            (display_name, category, row["id"]),
        )
        return int(row["id"])
    conn.execute(
        """
        INSERT INTO metrics (metric_key, display_name, statement_category, sort_order)
        VALUES (?, ?, ?, 500)
        """,
        (metric_key, display_name, category),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _upsert_company(conn: sqlite3.Connection, ticker: str, legal_name: str) -> int:
    ticker = (ticker or "UNK").upper().strip()[:32]
    legal_name = (legal_name or ticker).strip()[:500]
    row = conn.execute("SELECT id FROM companies WHERE ticker = ?", (ticker,)).fetchone()
    if row:
        conn.execute(
            "UPDATE companies SET legal_name = ? WHERE id = ?",
            (legal_name, row["id"]),
        )
        return int(row["id"])
    conn.execute(
        "INSERT INTO companies (ticker, legal_name) VALUES (?, ?)",
        (ticker, legal_name),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _upsert_reporting_period(
    conn: sqlite3.Connection,
    company_id: int,
    fiscal_year: int,
    fiscal_quarter: int | None,
    period_label: str,
) -> int:
    row = conn.execute(
        """
        SELECT id FROM reporting_periods
        WHERE company_id = ? AND fiscal_year = ?
          AND COALESCE(fiscal_quarter, -1) = COALESCE(?, -1)
        """,
        (company_id, fiscal_year, fiscal_quarter),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE reporting_periods SET period_label = ? WHERE id = ?",
            (period_label, row["id"]),
        )
        return int(row["id"])
    conn.execute(
        """
        INSERT INTO reporting_periods (company_id, fiscal_year, fiscal_quarter, period_label)
        VALUES (?, ?, ?, ?)
        """,
        (company_id, fiscal_year, fiscal_quarter, period_label),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _replace_document_and_facts(
    conn: sqlite3.Connection,
    reporting_period_id: int,
    file_name: str,
    file_hash: str,
    metrics_rows: list[dict],
) -> int:
    """Remove fatos antigos do mesmo arquivo/período e reinsere."""
    row = conn.execute(
        """
        SELECT id FROM source_documents
        WHERE reporting_period_id = ? AND file_name = ?
        """,
        (reporting_period_id, file_name),
    ).fetchone()
    if row:
        doc_id = int(row["id"])
        conn.execute("DELETE FROM financial_facts WHERE source_document_id = ?", (doc_id,))
        conn.execute(
            """
            UPDATE source_documents SET file_hash = ?, retrieved_at = ?
            WHERE id = ?
            """,
            (file_hash, datetime.now(timezone.utc).isoformat(), doc_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO source_documents (reporting_period_id, doc_type, file_name, file_hash, retrieved_at)
            VALUES (?, 'ITR', ?, ?, ?)
            """,
            (
                reporting_period_id,
                file_name,
                file_hash,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    for m in metrics_rows:
        if m.get("value_amount") is None:
            continue
        mk = _slug_metric_key(m.get("metric_key") or m.get("display_name") or "metric")
        display = (m.get("display_name") or mk).strip()[:500]
        cat = (m.get("statement_category") or "DRE").strip()[:64]
        mid = _ensure_metric(conn, mk, display, cat)
        scope = (m.get("scope") or "consolidado").strip().lower()
        if scope in ("controladora", "individual") or "controladora" in scope:
            is_cons = 0
        else:
            is_cons = 1
        conn.execute(
            """
            INSERT INTO financial_facts (
              source_document_id, metric_id, statement_type, value_amount,
              unit_scale, currency, page_number, section_title, row_label_raw, is_consolidated
            ) VALUES (?, ?, ?, ?, ?, 'BRL', ?, ?, ?, ?)
            """,
            (
                doc_id,
                mid,
                cat,
                float(m.get("value_amount") or 0),
                (m.get("unit_scale") or "thousands").lower()[:32],
                int(m.get("page_number") or 1),
                (m.get("section_title") or "")[:500],
                (m.get("row_label") or display)[:500],
                is_cons,
            ),
        )
    return doc_id


def persist_extraction(conn: sqlite3.Connection, data: dict, file_name: str, file_hash: str) -> int:
    ticker = str(data.get("ticker") or "UNK").upper()
    legal = str(data.get("company_legal_name") or ticker)
    fy = int(data.get("fiscal_year") or 2000)
    fq = data.get("fiscal_quarter")
    fq = int(fq) if fq is not None else None
    plabel = str(data.get("period_label") or f"{fq or 1}T{str(fy)[-2:]}")
    company_id = _upsert_company(conn, ticker, legal)
    rp_id = _upsert_reporting_period(conn, company_id, fy, fq, plabel)
    metrics = data.get("metrics") or []
    if not isinstance(metrics, list):
        metrics = []
    return _replace_document_and_facts(conn, rp_id, file_name, file_hash, metrics)


def process_new_pdf(pdf_path: Path) -> dict:
    """
    Extrai texto em chunks de páginas, chama a LLM por trecho, une resultados e persiste no SQLite.
    Tamanho do chunk: `PDF_INGEST_CHUNK_PAGES` (padrão 15). Limite de páginas: `PDF_INGEST_MAX_TOTAL_PAGES` (opcional).
    """
    pdf_path = Path(pdf_path).resolve()
    if not pdf_path.is_file():
        return {"status": "error", "error": "arquivo não encontrado"}

    file_hash = _sha256_file(pdf_path)
    file_name = pdf_path.name
    conn = get_connection()
    chunk_size = _ingest_chunk_pages()
    max_total = _ingest_max_total_pages()

    try:
        row = conn.execute(
            "SELECT file_hash, status FROM ingestion_jobs WHERE file_name = ?",
            (file_name,),
        ).fetchone()
        if row:
            if row["file_hash"] == file_hash and row["status"] == "ok":
                return {"status": "skipped", "reason": "hash já processado"}
            if row["file_hash"] == file_hash and row["status"] == "error":
                return {"status": "skipped", "reason": "já falhou com este arquivo, substitua o PDF"}

        chunk_results: list[dict] = []
        chunk_errors: list[str] = []

        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            limit = total_pages if max_total is None else min(total_pages, max_total)
            if limit <= 0:
                _log_job(conn, str(pdf_path), file_name, file_hash, "error", "PDF sem páginas")
                conn.commit()
                return {"status": "error", "error": "PDF sem páginas"}

            n_chunks = (limit + chunk_size - 1) // chunk_size
            for idx, start in enumerate(range(0, limit, chunk_size)):
                end = min(start + chunk_size, limit)
                text = _pdf_text_for_page_range(pdf, start, end)
                if not text.strip():
                    logger.info("ingest %s: páginas %s-%s sem texto, pulando LLM", file_name, start + 1, end)
                    continue
                ctx = (
                    f"páginas {start + 1}–{end} do PDF ({end - start} páginas), "
                    f"bloco {idx + 1} de {n_chunks}, arquivo «{file_name}»"
                )
                try:
                    data = extract_financial_json_from_text(text, chunk_context=ctx)
                    chunk_results.append(data)
                except RuntimeError as e:
                    msg = str(e)
                    logger.warning("ingest skip: %s", msg)
                    _log_job(conn, str(pdf_path), file_name, file_hash, "skipped_no_api", msg)
                    conn.commit()
                    return {"status": "skipped_no_api", "error": msg}
                except Exception as e:
                    logger.exception("LLM extraction failed chunk %s (%s-%s)", idx + 1, start + 1, end)
                    chunk_errors.append(f"bloco {idx + 1} (pág. {start + 1}-{end}): {e}")

        if not chunk_results:
            err_detail = "; ".join(chunk_errors) if chunk_errors else "nenhum trecho com texto"
            _log_job(
                conn,
                str(pdf_path),
                file_name,
                file_hash,
                "error",
                f"PDF sem extração: {err_detail}",
            )
            conn.commit()
            return {"status": "error", "error": f"nenhuma extração bem-sucedida ({err_detail})"}

        data = _merge_financial_extractions(chunk_results)
        if chunk_errors:
            extra = "Erros parciais: " + "; ".join(chunk_errors)
            prev = str(data.get("ingestion_notes") or "").strip()
            data["ingestion_notes"] = f"{prev} | {extra}" if prev else extra

        notes = str(data.get("ingestion_notes") or "")
        metrics_list = data.get("metrics") or []
        try:
            doc_id = persist_extraction(conn, data, file_name, file_hash)
        except Exception as e:
            logger.exception("persist failed")
            _log_job(conn, str(pdf_path), file_name, file_hash, "error", str(e))
            conn.commit()
            return {"status": "error", "error": str(e)}

        _log_job(conn, str(pdf_path), file_name, file_hash, "ok", notes or None, doc_id)
        conn.commit()
        return {
            "status": "ok",
            "source_document_id": doc_id,
            "metrics_count": len(metrics_list),
            "ingestion_notes": notes,
            "chunks_ok": len(chunk_results),
            "chunks_failed": len(chunk_errors),
            "pages_seen": limit,
            "chunk_pages": chunk_size,
        }
    finally:
        conn.close()


def _log_job(
    conn: sqlite3.Connection,
    file_path: str,
    file_name: str,
    file_hash: str,
    status: str,
    error_message: str | None,
    source_document_id: int | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO ingestion_jobs (file_path, file_name, file_hash, status, error_message, processed_at, source_document_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_name) DO UPDATE SET
          file_hash = excluded.file_hash,
          status = excluded.status,
          error_message = excluded.error_message,
          processed_at = excluded.processed_at,
          source_document_id = excluded.source_document_id
        """,
        (file_path, file_name, file_hash, status, error_message, now, source_document_id),
    )


def scan_and_ingest_pdfs() -> list[dict]:
    """Varre `data/pdfs` e processa cada PDF."""
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for pdf in sorted(PDF_DIR.glob("*.pdf")):
        try:
            results.append({"file": pdf.name, **process_new_pdf(pdf)})
        except Exception as e:
            logger.exception("ingest %s", pdf)
            results.append({"file": pdf.name, "status": "error", "error": str(e)})
    return results
