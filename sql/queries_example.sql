-- =============================================================================
-- Query 1 — Série temporal: receita líquida da Magazine Luiza por trimestre
-- Ilustra consulta eficiente ao longo do tempo com join em período e métrica.
-- =============================================================================
SELECT
  c.ticker,
  rp.period_label,
  rp.fiscal_year,
  rp.fiscal_quarter,
  ff.value_amount AS receita_liquida_milhares_brl,
  ff.unit_scale,
  ff.page_number,
  ff.section_title,
  sd.file_name AS documento_origem
FROM financial_facts ff
JOIN metrics m ON m.id = ff.metric_id AND m.metric_key = 'receita_liquida'
JOIN source_documents sd ON sd.id = ff.source_document_id
JOIN reporting_periods rp ON rp.id = sd.reporting_period_id
JOIN companies c ON c.id = rp.company_id
WHERE c.ticker = 'MGLU3'
ORDER BY rp.fiscal_year, COALESCE(rp.fiscal_quarter, 99);

-- =============================================================================
-- Query 2 — Comparação entre empresas: mesma métrica e mesmo período (1T22)
-- Ilustra painel tipo “peer comparison” com rastreabilidade por documento/página.
-- =============================================================================
SELECT
  c.ticker,
  c.legal_name,
  rp.period_label,
  m.display_name AS metrica,
  ff.value_amount,
  ff.unit_scale,
  ff.currency,
  ff.page_number,
  ff.section_title,
  sd.file_name
FROM financial_facts ff
JOIN metrics m ON m.id = ff.metric_id
JOIN source_documents sd ON sd.id = ff.source_document_id
JOIN reporting_periods rp ON rp.id = sd.reporting_period_id
JOIN companies c ON c.id = rp.company_id
WHERE m.metric_key = 'receita_liquida'
  AND rp.period_label = '1T22'
ORDER BY ff.value_amount DESC;

-- =============================================================================
-- Query 3 — Nota 19: saldo de empréstimos com dimensões (modalidade/vencimento)
-- Ilustra o modelo EAV para dados multi-dimensionais da Nota Explicativa 19.
-- =============================================================================
SELECT
  c.ticker,
  rp.period_label,
  m.display_name AS metrica,
  ff.value_amount AS saldo_milhares_brl,
  MAX(CASE WHEN fd.dimension_type = 'modalidade' THEN fd.dimension_value END) AS modalidade,
  MAX(CASE WHEN fd.dimension_type = 'encargo'    THEN fd.dimension_value END) AS encargo,
  MAX(CASE WHEN fd.dimension_type = 'garantia'   THEN fd.dimension_value END) AS garantia,
  MAX(CASE WHEN fd.dimension_type = 'vencimento' THEN fd.dimension_value END) AS vencimento,
  ff.page_number,
  sd.file_name AS documento_origem
FROM financial_facts ff
JOIN metrics m ON m.id = ff.metric_id AND m.metric_key = 'emprestimo_saldo'
JOIN fact_dimensions fd ON fd.fact_id = ff.id
JOIN source_documents sd ON sd.id = ff.source_document_id
JOIN reporting_periods rp ON rp.id = sd.reporting_period_id
JOIN companies c ON c.id = rp.company_id
WHERE c.ticker = 'MGLU3'
  AND ff.is_consolidated = 1
GROUP BY ff.id
ORDER BY ff.value_amount DESC;
