-- Case Atthena — schema relacional (SQLite; compatível conceitualmente com PostgreSQL)
-- Objetivo: múltiplas empresas/períodos, métricas flexíveis, rastreabilidade (doc, página, seção),
-- séries temporais e comparação entre empresas.

PRAGMA foreign_keys = ON;

CREATE TABLE companies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL UNIQUE,
  legal_name TEXT NOT NULL
);

CREATE TABLE reporting_periods (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
  fiscal_year INTEGER NOT NULL,
  fiscal_quarter INTEGER, -- NULL = consolidado anual
  period_label TEXT NOT NULL, -- ex.: '1T22', '4T21'
  UNIQUE (company_id, fiscal_year, fiscal_quarter)
);

CREATE TABLE source_documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reporting_period_id INTEGER NOT NULL REFERENCES reporting_periods (id) ON DELETE CASCADE,
  doc_type TEXT NOT NULL DEFAULT 'ITR',
  file_name TEXT,
  file_hash TEXT,
  retrieved_at TEXT
);

-- Catálogo de métricas: uma linha por conceito contábil (sem coluna por métrica nos fatos)
CREATE TABLE metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  metric_key TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  statement_category TEXT NOT NULL, -- BP_ATIVO, BP_PASSIVO_PL, DRE, DFC, NOTA_19
  parent_metric_id INTEGER REFERENCES metrics (id),
  sort_order INTEGER NOT NULL DEFAULT 0
);

-- Fato principal: valor numérico + vínculo ao documento e à métrica
CREATE TABLE financial_facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_document_id INTEGER NOT NULL REFERENCES source_documents (id) ON DELETE CASCADE,
  metric_id INTEGER NOT NULL REFERENCES metrics (id),
  statement_type TEXT NOT NULL, -- mesmo recorte do PDF: BP_ATIVO, DRE, etc.
  value_amount REAL NOT NULL,
  unit_scale TEXT NOT NULL DEFAULT 'thousands', -- ITR frequentemente em milhares de R$
  currency TEXT NOT NULL DEFAULT 'BRL',
  page_number INTEGER NOT NULL,
  section_title TEXT,
  row_label_raw TEXT,
  parent_fact_id INTEGER REFERENCES financial_facts (id),
  -- 1 = consolidado (padrão ITR); 0 = controladora / individual
  is_consolidated INTEGER NOT NULL DEFAULT 1
);

-- Nota 19 e outros casos multi-dimensão: EAV por fato (modalidade, encargo, garantia, vencimento)
CREATE TABLE fact_dimensions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fact_id INTEGER NOT NULL REFERENCES financial_facts (id) ON DELETE CASCADE,
  dimension_type TEXT NOT NULL,
  dimension_value TEXT NOT NULL,
  UNIQUE (fact_id, dimension_type, dimension_value)
);

CREATE INDEX idx_financial_facts_doc_metric ON financial_facts (source_document_id, metric_id);
CREATE INDEX idx_financial_facts_consolidated ON financial_facts (is_consolidated);
CREATE INDEX idx_financial_facts_statement ON financial_facts (statement_type);
CREATE INDEX idx_reporting_periods_company_time ON reporting_periods (company_id, fiscal_year, fiscal_quarter);
CREATE INDEX idx_fact_dimensions_fact ON fact_dimensions (fact_id);
CREATE INDEX idx_fact_dimensions_type ON fact_dimensions (dimension_type, dimension_value);

-- Controle de ingestão automática de PDFs (app/ingestion.py)
CREATE TABLE IF NOT EXISTS ingestion_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file_path TEXT NOT NULL,
  file_name TEXT NOT NULL UNIQUE,
  file_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  error_message TEXT,
  processed_at TEXT,
  source_document_id INTEGER
);
