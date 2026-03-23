import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "atthena_case.db"
ROOT = Path(__file__).resolve().parent.parent


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema_only() -> None:
    """Cria todas as tabelas a partir de `sql/schema.sql` (sem seed)."""
    schema = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
    conn = get_connection()
    try:
        conn.executescript(schema.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def ensure_migrations() -> None:
    """Para bases antigas: garante tabelas novas sem reexecutar o schema completo."""
    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ingestion_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              file_path TEXT NOT NULL UNIQUE,
              file_name TEXT NOT NULL,
              file_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              error_message TEXT,
              processed_at TEXT,
              source_document_id INTEGER
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

#não vamos usar!!!
def ensure_demo_seed_if_empty() -> None:
    """
    Se `companies` estiver vazia, aplica `sql/seed.sql` para o `/ask` funcionar sem PDF/LLM.
    O seed usa IDs fixos; só roda quando não há linhas (evita duplicar PK).
    """
    conn = get_connection()
    try:
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()
        except sqlite3.OperationalError:
            return
        if row is None or int(row["n"]) > 0:
            return
        seed_path = ROOT / "sql" / "seed.sql"
        if not seed_path.is_file():
            logger.warning("Banco sem empresas e %s ausente; /ask ficará sem dados.", seed_path)
            return
        conn.executescript(seed_path.read_text(encoding="utf-8"))
        conn.commit()
        logger.info("Carregado seed de demonstração (tabela companies estava vazia).")
    finally:
        conn.close()


def init_db() -> None:
    """Compatibilidade: apenas schema (sem seed.sql)."""
    init_schema_only()
