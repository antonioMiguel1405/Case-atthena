"""NAO ESTAMOS USANDO ESSE ARQUIVO!!!!!!!!"""


-- Seed alinhado ao ITR MGLU 1T22 (valores em milhares de R$, exceto onde indicado).
-- Receita 1T21: valor do enunciado para teste YoY (rótulo 1T21 — homólogo 1T22).

INSERT INTO companies (id, ticker, legal_name) VALUES
  (1, 'MGLU3', 'Magazine Luiza S.A.'),
  (2, 'LREN3', 'Lojas Renner S.A.');

INSERT INTO reporting_periods (id, company_id, fiscal_year, fiscal_quarter, period_label) VALUES
  (1, 1, 2022, 1, '1T22'),
  (2, 1, 2021, 4, '4T21'),
  (3, 2, 2022, 1, '1T22'),
  (4, 1, 2021, 1, '1T21');

INSERT INTO source_documents (id, reporting_period_id, doc_type, file_name) VALUES
  (1, 1, 'ITR', 'MGLU_ITR_1T22.pdf'),
  (2, 2, 'ITR', 'MGLU_ITR_4T21.pdf'),
  (3, 3, 'ITR', 'LREN_ITR_1T22.pdf'),
  (4, 4, 'ITR', 'MGLU_ITR_1T21.pdf');

INSERT INTO metrics (id, metric_key, display_name, statement_category, parent_metric_id, sort_order) VALUES
  (1, 'receita_liquida', 'Receita líquida de vendas', 'DRE', NULL, 10),
  (2, 'emprestimo_saldo', 'Saldo de empréstimos e financiamentos', 'NOTA_19', NULL, 100),
  (3, 'estoque_mercadorias', 'Estoques de mercadorias', 'BP_ATIVO', NULL, 20),
  (4, 'lucro_liquido', 'Lucro líquido', 'DRE', NULL, 50);

-- MGLU 1T22 (documento id=1): consolidado conforme PDF
INSERT INTO financial_facts (
  source_document_id, metric_id, statement_type, value_amount, unit_scale, currency,
  page_number, section_title, row_label_raw, is_consolidated
) VALUES
  (1, 1, 'DRE', 8824567.0, 'thousands', 'BRL', 7, 'Demonstração do Resultado', 'Receita líquida de vendas', 1),
  (1, 4, 'DRE', -161158.0, 'thousands', 'BRL', 7, 'Demonstração do Resultado', 'Lucro líquido', 1),
  (1, 3, 'BP_ATIVO', 8077255.0, 'thousands', 'BRL', 5, 'Balanço Patrimonial — Ativo', 'Estoques de mercadorias', 1);

-- MGLU 1T21: receita consolidada para comparação YoY (1T22 vs 1T21)
INSERT INTO financial_facts (
  source_document_id, metric_id, statement_type, value_amount, unit_scale, currency,
  page_number, section_title, row_label_raw, is_consolidated
) VALUES
  (4, 1, 'DRE', 8222488.0, 'thousands', 'BRL', 7, 'Demonstração do Resultado', 'Receita líquida de vendas', 1);

INSERT INTO financial_facts (
  source_document_id, metric_id, statement_type, value_amount, unit_scale, currency,
  page_number, section_title, row_label_raw, is_consolidated
) VALUES
  (2, 1, 'DRE', 7650000.0, 'thousands', 'BRL', 7, 'Demonstração do Resultado', 'Receita líquida de vendas', 1),
  (3, 1, 'DRE', 2100000.0, 'thousands', 'BRL', 7, 'Demonstração dos Resultados', 'Receita líquida', 1);

INSERT INTO financial_facts (
  source_document_id, metric_id, statement_type, value_amount, unit_scale, currency,
  page_number, section_title, row_label_raw, is_consolidated
) VALUES
  (1, 2, 'NOTA_19', 1250000.0, 'thousands', 'BRL', 30, 'Nota 19 — Empréstimos e Financiamentos', 'Linha sintética exemplo', 1);

INSERT INTO fact_dimensions (fact_id, dimension_type, dimension_value) VALUES
  ((SELECT MAX(id) FROM financial_facts), 'modalidade', 'Debêntures'),
  ((SELECT MAX(id) FROM financial_facts), 'vencimento', '2025-2027');
