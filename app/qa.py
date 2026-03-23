"""
Respostas por consulta ao banco: identifica empresa e período a partir dos dados cadastrados
e escolhe a métrica pelo melhor casamento de texto com o catálogo e com o rótulo extraído do PDF.
"""
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.semantic_match import rank_facts_semantic


@dataclass
class QAResult:
    answer: str
    raciocinio: str
    sql_executed: str
    references: list[dict]


_META_QUESTION = re.compile(
    r"""
    \b(?:racioc[ií]nio|l[óo]gica|fluxo|arquitetura|funcionamento)\b
    |\bcomo\s+(?:voc[eê]|tu|a\s+aplica[cç][aã]o|o\s+sistema|esta\s+api)\s+funciona
    |\bcomo\s+funciona\b
    |\bexplica(?:r)?\s+(?:o\s+)?(?:racioc[ií]nio|fluxo|funcionamento|l[óo]gica)
    |\bqual\s+[eé]\s+o\s+(?:racioc[ií]nio|fluxo|funcionamento)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_META_ANSWER = """Esta API (demo do case Atthena) combina ingestão de PDFs em `data/pdfs/` (extração com pdfplumber + LLM) com consultas em SQLite. No `/ask`: (1) identifica empresa e período; (2) filtra fatos por consolidado/controladora (`is_consolidated`); (3) ranqueia a métrica com **embeddings** ou **LLM** quando há chave de API, senão usa pontuação lexical (`SequenceMatcher` + tokens). Valores e páginas vêm do banco. Detalhes: docs/FLUXOGRAMA.md."""


def _is_meta_question(question: str) -> bool:
    qn = _normalize(question)
    return bool(_META_QUESTION.search(qn))


# Termos alternativos comuns em perguntas (não amarrados a uma metric_key específica)
_TOKEN_ALIASES: dict[str, tuple[str, ...]] = {
    "endividamento": ("emprestimo", "emprestimos", "financiamento", "financiamentos", "divida"),
    "divida": ("emprestimo", "financiamento", "endividamento"),
    "faturamento": ("receita", "vendas"),
    "lucro": ("resultado",),
    "prejuizo": ("resultado", "lucro"),
}


_STOP = frozenset(
    """
    a as ao aos aquela aquele aquilo ate com como da das de dela dele depois do dos e ela elas ele eles em
    esse essa esses essas esta estao eu foi for foram ha isso ja la lhe lhes lo mas me mesmo meu meus minha
    minhas na nas no nos nós o os ou para pela pelas pelo pelos por qual quais quanto quantos que se sem
    seu sua seus suas sao somos sou tam tem temos tenho te tu tua vos vos ja
    primeiro segundo terceiro quarto trimestre ano mes
    sobre sob tanto todos todo toda todas foi ser era eram está estão
    numero nº empresa companhia cnpj acoes bolsa
    """.split()
)


def _normalize(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFD", text.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tokens(text: str) -> list[str]:
    return [t for t in _normalize(text).split() if t]


def _meaningful_question_tokens(q: str) -> list[str]:
    return [t for t in _tokens(q) if len(t) >= 2 and t not in _STOP]


def _strip_company_from_question(qn: str, ticker: str, legal_name: str) -> str:
    out = qn
    for part in (ticker.lower(), re.sub(r"\d+$", "", ticker.lower())):
        if len(part) >= 2:
            out = out.replace(part, " ")
    for w in _tokens(legal_name):
        if len(w) >= 3 and w not in _STOP:
            out = out.replace(w, " ")
    return re.sub(r"\s+", " ", out).strip()


# --- Modificadores contábeis obrigatórios (pergunta vs. métrica mapeada) ---
_MOD_PER_SHARE = "per_share"
_MOD_MARGEM = "margem"
_MOD_EBITDA = "ebitda"
_MOD_BASICO = "basico"
_MOD_DILUIDO = "diluido"


def _modifiers_from_question(question: str) -> frozenset[str]:
    """Sinais na pergunta que exigem presença explícita no rótulo/métrica do banco."""
    q = _normalize(question)
    out: set[str] = set()
    if (
        re.search(r"\bpor\s+a[cç]a[oô]\b", q)
        or re.search(r"\bp\s*/\s*a[cç]a[oô]\b", q)
        or re.search(r"\beps\b", q)
        or re.search(r"\blpa\b", q)
    ):
        out.add(_MOD_PER_SHARE)
    if "margem" in q or re.search(r"\bmargin\b", q):
        out.add(_MOD_MARGEM)
    if "ebitda" in q:
        out.add(_MOD_EBITDA)
    if "basico" in q:
        out.add(_MOD_BASICO)
    if "diluido" in q:
        out.add(_MOD_DILUIDO)
    return frozenset(out)


def _metric_row_blob(row: sqlite3.Row) -> str:
    parts = [
        str(row["metric_key"] or ""),
        str(row["display_name"] or ""),
        str(row["row_label_raw"] or ""),
    ]
    return _normalize(" ".join(parts))


def _blob_indicates_per_share(blob: str, metric_key_norm: str) -> bool:
    if any(
        k in metric_key_norm
        for k in ("por_acao", "poracao", "lucro_por", "resultado_por", "eps", "_lpa", "lpa_")
    ):
        return True
    if "por acao" in blob or "p acao" in blob:
        return True
    tb = f" {blob} "
    if " eps " in tb:
        return True
    if " lpa " in tb:
        return True
    return False


def _row_satisfies_modifiers(row: sqlite3.Row, mods: frozenset[str]) -> bool:
    if not mods:
        return True
    b = _metric_row_blob(row)
    mk = _normalize(str(row["metric_key"] or ""))
    for m in mods:
        if m == _MOD_PER_SHARE:
            if not _blob_indicates_per_share(b, mk):
                return False
        elif m == _MOD_MARGEM:
            if "margem" not in b and "margin" not in b:
                return False
        elif m == _MOD_EBITDA:
            if "ebitda" not in b:
                return False
        elif m == _MOD_BASICO:
            if "basico" not in b:
                return False
        elif m == _MOD_DILUIDO:
            if "diluido" not in b:
                return False
    return True


def _modifier_labels_pt(mods: frozenset[str]) -> list[str]:
    labels: list[str] = []
    order = (_MOD_PER_SHARE, _MOD_MARGEM, _MOD_EBITDA, _MOD_BASICO, _MOD_DILUIDO)
    names = {
        _MOD_PER_SHARE: "por ação (LPA/EPS)",
        _MOD_MARGEM: "margem",
        _MOD_EBITDA: "EBITDA",
        _MOD_BASICO: "básico",
        _MOD_DILUIDO: "diluído",
    }
    for k in order:
        if k in mods:
            labels.append(names[k])
    return labels


def _human_intent_label(mods: frozenset[str], question: str) -> str:
    qn = _normalize(question)
    if _MOD_PER_SHARE in mods:
        if "lucro" in qn or "prejuizo" in qn or "resultado" in qn:
            return "Lucro (ou resultado) por ação"
        return "Métrica por ação"
    if _MOD_EBITDA in mods:
        return "EBITDA"
    if _MOD_MARGEM in mods:
        return "Margem (contábil)"
    if mods:
        return "Métrica com modificadores: " + ", ".join(_modifier_labels_pt(mods))
    return "Métrica solicitada"


def _sql_verification_modifiers_reject(
    *,
    metric_key: str | None = None,
    display_name: str | None = None,
    required_mods: frozenset[str],
) -> str:
    req_txt = ", ".join(_modifier_labels_pt(required_mods)) or "(nenhum)"
    lines = [
        "-- Verificação: conceito contábil da pergunta não possui métrica compatível neste documento.",
        f"-- Modificadores exigidos na pergunta: {req_txt}.",
        "-- Não se retorna valor de métrica distinta (ex.: total em moeda) como proxy.",
    ]
    if metric_key and display_name:
        mods_lit = "', '".join(_modifier_labels_pt(required_mods))
        lines.append(
            f"-- A pergunta solicita '{mods_lit}'. A métrica '{metric_key}' ({display_name}) NÃO atende a este critério."
        )
    lines.append("-- Consulta bloqueada (sem SELECT de fato financeiro).")
    return "\n".join(lines)


def _sql_ok_verification_line(question: str, row: sqlite3.Row) -> str:
    mods = _modifiers_from_question(question)
    mk = row["metric_key"]
    dn = row["display_name"]
    if mods:
        joined = ", ".join(_modifier_labels_pt(mods))
        return (
            f"-- Verificação: a métrica `{mk}` ({dn}) é **compatível** com os modificadores detectados na pergunta: {joined}."
        )
    return (
        f"-- Verificação: nenhum modificador restritivo (por ação, margem, EBITDA, básico/diluído) na pergunta; "
        f"métrica `{mk}` ({dn}) escolhida pelo ranking."
    )


def _suggest_related_metrics(
    question: str,
    rows: list[sqlite3.Row],
    *,
    limit: int = 5,
) -> list[str]:
    """Ordena por sobreposição lexical com a pergunta (sem chutar métrica incompatível por modificador)."""
    if not rows:
        return []
    q_full = _normalize(question)
    q_stripped = q_full  # período/empresa ainda ajudam no overlap
    scored: list[tuple[float, sqlite3.Row]] = []
    for r in rows:
        blob = _blob_for_fact(r["display_name"], r["metric_key"], r["row_label_raw"])
        s = _score_match(
            q_stripped,
            blob,
            r["display_name"],
            r["metric_key"],
            q_full,
        )
        scored.append((s, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[str] = []
    seen: set[str] = set()
    for s, r in scored[:limit]:
        mk = r["metric_key"]
        if mk in seen:
            continue
        seen.add(mk)
        out.append(f"«{r['display_name']}» (`{mk}`)")
    return out


def _period_label_from_quarter_year(quarter: int, year: int) -> str:
    return f"{quarter}T{str(year)[-2:]}"


def _find_periods_in_question(question: str) -> list[tuple[int, int | None]]:
    """
    Extrai todas as âncoras temporais na ordem em que aparecem na pergunta.
    Cada item é (ano, trimestre) com trimestre em 1–4, ou (ano, None) quando só o ano é citado
    (ex.: «entre 2021 e 2022») — None será resolvido para o último trimestre disponível daquele ano.
    """
    q_raw = question
    qn = _normalize(question)
    seen: set[tuple[int, int | None]] = set()
    ordered: list[tuple[int, int | None]] = []

    def add(year: int, quarter: int | None) -> None:
        key = (year, quarter)
        if key in seen:
            return
        seen.add(key)
        ordered.append(key)

    compact = re.sub(r"\s+", "", q_raw.lower())
    # (?![0-9]) evita exigir \b após o ano (ex.: «1t21e1t22» sem espaço entre períodos)
    for m in re.finditer(r"(?<![0-9])([1-4])t(\d{2}|\d{4})(?![0-9])", compact):
        qt = int(m.group(1))
        yy = m.group(2)
        year = int(yy) if len(yy) == 4 else 2000 + int(yy)
        add(year, qt)

    for m in re.finditer(
        r"\b(primeiro|segundo|terceiro|quarto|[1-4])\s*[ºo]?\s*trimestre\s+(?:de\s+)?(\d{4})\b",
        qn,
    ):
        ord_ = m.group(1)
        year = int(m.group(2))
        qmap = {"primeiro": 1, "segundo": 2, "terceiro": 3, "quarto": 4}
        qt = qmap.get(ord_, int(ord_) if ord_.isdigit() else 0)
        if 1 <= qt <= 4:
            add(year, qt)

    for m in re.finditer(r"\btrimestre\s+(\d)\s+(?:de\s+)?(\d{4})\b", qn):
        qt = int(m.group(1))
        year = int(m.group(2))
        if 1 <= qt <= 4:
            add(year, qt)

    years_with_quarter = {y for y, qtr in seen if qtr is not None}
    for m in re.finditer(r"\b(20\d{2}|19\d{2})\b", qn):
        year = int(m.group(1))
        if year in years_with_quarter:
            continue
        add(year, None)

    return ordered


def _period_sort_key(item: tuple[int, int | None]) -> tuple[int, int]:
    y, q = item
    return (y, q if q is not None else 0)


def _want_consolidated(question: str) -> bool:
    """
    Escopo contábil: sem menção explícita → consolidado (is_consolidated = 1).
    «controladora» / «individual» → demonstrações da controladora (0).
    «consolidado» / «consolidacao» → força consolidado.
    """
    n = _normalize(question)
    ind = "controladora" in n or "individual" in n
    cons = "consolidado" in n or "consolidada" in n or "consolidacao" in n
    if ind and not cons:
        return False
    return True


def _is_consolidated_sql_flag(want_consolidated: bool) -> int:
    return 1 if want_consolidated else 0


def _explain_scope_technical(want_consolidated: bool) -> str:
    if want_consolidated:
        return (
            "• **Escopo contábil:** filtro `financial_facts.is_consolidated = 1` (demonstrações **consolidadas**); "
            "padrão quando a pergunta não cita controladora/individual."
        )
    return (
        "• **Escopo contábil:** filtro `financial_facts.is_consolidated = 0` — demonstrações da **controladora** "
        "(individual), pedido explícito na pergunta."
    )


def _is_comparison_intent(question: str) -> bool:
    if "%" in question:
        return True
    n = _normalize(question)
    if "em relacao a" in n or "em relacao ao" in n or "em relacao aos" in n:
        return True
    tokens = frozenset(
        """
        aumento queda variacao diferenca porcentagem crescimento declinio
        comparacao comparar entre versus vs
        """.split()
    )
    words = set(n.replace(",", " ").split())
    if words & tokens:
        return True
    if "por cento" in n:
        return True
    return False


def _select_two_periods_for_comparison(
    hints: list[tuple[int, int | None]],
) -> tuple[tuple[int, int | None], tuple[int, int | None]] | None:
    if len(hints) < 2:
        return None
    unique = list(dict.fromkeys(hints))
    if len(unique) < 2:
        return None
    sorted_refs = sorted(unique, key=_period_sort_key)
    return sorted_refs[0], sorted_refs[-1]


def _resolve_period_row_for_anchor(
    conn: sqlite3.Connection, company_id: int, year: int, quarter: int | None
) -> sqlite3.Row | None:
    if quarter is not None:
        label = _period_label_from_quarter_year(quarter, year)
        row = conn.execute(
            """
            SELECT rp.id, rp.period_label, rp.fiscal_year, rp.fiscal_quarter,
                   sd.id AS source_document_id
            FROM reporting_periods rp
            JOIN source_documents sd ON sd.reporting_period_id = rp.id
            WHERE rp.company_id = ? AND rp.period_label = ?
            LIMIT 1
            """,
            (company_id, label),
        ).fetchone()
        if row:
            return row
        return conn.execute(
            """
            SELECT rp.id, rp.period_label, rp.fiscal_year, rp.fiscal_quarter,
                   sd.id AS source_document_id
            FROM reporting_periods rp
            JOIN source_documents sd ON sd.reporting_period_id = rp.id
            WHERE rp.company_id = ? AND rp.fiscal_year = ? AND rp.fiscal_quarter = ?
            LIMIT 1
            """,
            (company_id, year, quarter),
        ).fetchone()

    return conn.execute(
        """
        SELECT rp.id, rp.period_label, rp.fiscal_year, rp.fiscal_quarter,
               sd.id AS source_document_id
        FROM reporting_periods rp
        JOIN source_documents sd ON sd.reporting_period_id = rp.id
        WHERE rp.company_id = ? AND rp.fiscal_year = ?
        ORDER BY COALESCE(rp.fiscal_quarter, 0) DESC
        LIMIT 1
        """,
        (company_id, year),
    ).fetchone()


def _resolve_company(conn: sqlite3.Connection, q: str) -> sqlite3.Row | None:
    qn = _normalize(q)
    rows = conn.execute("SELECT id, ticker, legal_name FROM companies").fetchall()
    best: sqlite3.Row | None = None
    best_score = 0.0
    for r in rows:
        score = 0.0
        tick = r["ticker"].lower()
        base = re.sub(r"\d+$", "", tick)
        if tick in qn.replace(" ", ""):
            score += 12
        if len(base) >= 3 and base in qn:
            score += 10
        for w in _tokens(r["legal_name"]):
            if len(w) < 4:
                continue
            if w in qn:
                score += 4
        if score > best_score:
            best_score = score
            best = r
    return best if best_score >= 4 else None


def _resolve_period_row(
    conn: sqlite3.Connection, company_id: int, q: str
) -> sqlite3.Row | None:
    hints = _find_periods_in_question(q)
    if hints:
        y, qt = hints[0]
        return _resolve_period_row_for_anchor(conn, company_id, y, qt)

    return conn.execute(
        """
        SELECT rp.id, rp.period_label, rp.fiscal_year, rp.fiscal_quarter,
               sd.id AS source_document_id
        FROM reporting_periods rp
        JOIN source_documents sd ON sd.reporting_period_id = rp.id
        WHERE rp.company_id = ?
        ORDER BY rp.fiscal_year DESC, COALESCE(rp.fiscal_quarter, 0) DESC
        LIMIT 1
        """,
        (company_id,),
    ).fetchone()


def _blob_for_fact(display_name: str, metric_key: str, row_label: str | None) -> str:
    parts = [
        display_name,
        metric_key.replace("_", " "),
        row_label or "",
    ]
    return _normalize(" ".join(parts))


def _expanded_tokens(question_raw: str) -> list[str]:
    out: list[str] = []
    for t in _meaningful_question_tokens(question_raw):
        out.append(t)
        for extra in _TOKEN_ALIASES.get(t, ()):
            out.append(extra)
    return out


def _display_name_keyword_bonus(question_full_norm: str, display_name: str) -> float:
    """Usa a pergunta completa (antes de remover empresa) para premiar palavras do rótulo oficial."""
    bonus = 0.0
    for w in _tokens(display_name):
        if len(w) < 4 or w in _STOP:
            continue
        if w in question_full_norm:
            bonus += 1.35
    return bonus


def _metric_key_token_bonus(question_raw: str, metric_key: str) -> float:
    qn = _normalize(question_raw)
    bonus = 0.0
    for part in metric_key.replace("_", " ").split():
        if len(part) < 4:
            continue
        if part in qn:
            bonus += 1.5
    return bonus


def _score_match(
    question_raw: str,
    blob: str,
    display_name: str,
    metric_key: str,
    question_full_norm: str,
) -> float:
    qn = _normalize(question_raw)
    if not qn or not blob:
        base = 0.0
    else:
        base = 0.0
        for t in _expanded_tokens(question_raw):
            if len(t) < 3:
                continue
            if t in blob:
                base += 1.2
            elif any(t in w or w in t for w in blob.split() if len(w) >= 4):
                base += 0.6

        dn = _normalize(display_name)
        if len(dn) >= 4:
            base += SequenceMatcher(None, qn, dn).ratio() * 2.5

        base += SequenceMatcher(None, qn, blob).ratio() * 1.0

    base += _display_name_keyword_bonus(question_full_norm, display_name)
    base += _metric_key_token_bonus(question_raw, metric_key)
    base += _metric_key_token_bonus(question_full_norm, metric_key)
    return base


def _explain_company_choice(company_row: sqlite3.Row) -> str:
    return (
        f"• Empresa: «{company_row['legal_name']}» ({company_row['ticker']}) — "
        "correspondência entre a pergunta e ticker/nome cadastrados no banco."
    )


def _explain_period_choice(question: str, period_row: sqlite3.Row) -> str:
    hints = _find_periods_in_question(question)
    if hints:
        fq = period_row["fiscal_quarter"]
        qtxt = f"Q{fq}" if fq is not None else "consolidado"
        return (
            f"• Período: âncora(s) extraída(s) da pergunta → documento **{period_row['period_label']}** "
            f"({period_row['fiscal_year']} {qtxt})."
        )
    return (
        "• Período: sem âncora explícita; usei o **mais recente** cadastrado "
        f"→ {period_row['period_label']}."
    )


def _describe_anchor(anchor: tuple[int, int | None]) -> str:
    y, q = anchor
    if q is None:
        return f"ano {y} (último trimestre disponível no cadastro)"
    return f"{q}T{str(y)[-2:]}"


def _fetch_fact_by_metric_key(
    conn: sqlite3.Connection,
    source_document_id: int,
    metric_key: str,
    *,
    want_consolidated: bool,
) -> sqlite3.Row | None:
    ic = _is_consolidated_sql_flag(want_consolidated)
    return conn.execute(
        """
        SELECT ff.id, ff.value_amount, ff.unit_scale, ff.currency, ff.page_number,
               ff.section_title, ff.row_label_raw, ff.is_consolidated, m.metric_key, m.display_name,
               sd.file_name, rp.period_label, c.legal_name
        FROM financial_facts ff
        JOIN metrics m ON m.id = ff.metric_id AND m.metric_key = ?
        JOIN source_documents sd ON sd.id = ff.source_document_id
        JOIN reporting_periods rp ON rp.id = sd.reporting_period_id
        JOIN companies c ON c.id = rp.company_id
        WHERE ff.source_document_id = ? AND ff.is_consolidated = ?
        LIMIT 1
        """,
        (metric_key, source_document_id, ic),
    ).fetchone()


def _reference_dict_from_row(row: sqlite3.Row) -> dict:
    cons = bool(row["is_consolidated"]) if row["is_consolidated"] is not None else True
    return {
        "documento": row["file_name"],
        "pagina": row["page_number"],
        "secao": row["section_title"],
        "metrica": row["metric_key"],
        "periodo": row["period_label"],
        "escopo": "Consolidado" if cons else "Controladora (individual)",
    }


def _sql_string_literal(value: str) -> str:
    return value.replace("'", "''")


def _build_sql_comparison(
    metric_key: str,
    period_old: str,
    period_new: str,
    ticker: str,
    *,
    want_consolidated: bool,
    verification_preamble: str = "",
) -> str:
    mk = _sql_string_literal(metric_key)
    po = _sql_string_literal(period_old)
    pn = _sql_string_literal(period_new)
    tk = _sql_string_literal(ticker)
    ic = _is_consolidated_sql_flag(want_consolidated)
    body = (
        "SELECT ff_ant.value_amount AS valor_periodo_antigo,\n"
        "       ff_novo.value_amount AS valor_periodo_novo,\n"
        "       rp_ant.period_label AS periodo_antigo, rp_novo.period_label AS periodo_novo,\n"
        "       sd_ant.file_name AS documento_antigo, sd_novo.file_name AS documento_novo\n"
        "FROM financial_facts ff_ant\n"
        "JOIN metrics m ON m.id = ff_ant.metric_id AND m.metric_key = "
        f"'{mk}'\n"
        "JOIN source_documents sd_ant ON sd_ant.id = ff_ant.source_document_id\n"
        "JOIN reporting_periods rp_ant ON rp_ant.id = sd_ant.reporting_period_id\n"
        "JOIN companies c ON c.id = rp_ant.company_id AND c.ticker = "
        f"'{tk}'\n"
        "JOIN financial_facts ff_novo ON ff_novo.metric_id = m.id\n"
        "  AND ff_novo.is_consolidated = "
        f"{ic}\n"
        "JOIN source_documents sd_novo ON sd_novo.id = ff_novo.source_document_id\n"
        "JOIN reporting_periods rp_novo ON rp_novo.id = sd_novo.reporting_period_id\n"
        "  AND rp_novo.company_id = c.id\n"
        f"WHERE ff_ant.is_consolidated = {ic}\n"
        f"  AND rp_ant.period_label = '{po}'\n"
        f"  AND rp_novo.period_label = '{pn}';"
    )
    if verification_preamble:
        return verification_preamble.rstrip() + "\n" + body
    return body


def _build_sql_fact_lookup(
    metric_key: str,
    period_label: str,
    ticker: str,
    *,
    want_consolidated: bool,
    verification_preamble: str = "",
) -> str:
    mk = _sql_string_literal(metric_key)
    pl = _sql_string_literal(period_label)
    tk = _sql_string_literal(ticker)
    ic = _is_consolidated_sql_flag(want_consolidated)
    body = (
        "SELECT ff.value_amount, ff.page_number, ff.is_consolidated, sd.file_name, rp.period_label\n"
        "FROM financial_facts ff\n"
        "JOIN metrics m ON m.id = ff.metric_id\n"
        "JOIN source_documents sd ON sd.id = ff.source_document_id\n"
        "JOIN reporting_periods rp ON rp.id = sd.reporting_period_id\n"
        "JOIN companies c ON c.id = rp.company_id\n"
        f"WHERE m.metric_key = '{mk}'\n"
        f"  AND ff.is_consolidated = {ic}\n"
        f"  AND rp.period_label = '{pl}'\n"
        f"  AND c.ticker = '{tk}';"
    )
    if verification_preamble:
        return verification_preamble.rstrip() + "\n" + body
    return body


def _format_comparison_answer(
    row_old: sqlite3.Row,
    row_new: sqlite3.Row,
    pct: float | None,
    *,
    want_consolidated: bool,
) -> str:
    label = row_old["display_name"]
    scale = row_old["unit_scale"]
    v_old = row_old["value_amount"]
    v_new = row_new["value_amount"]
    p_old = row_old["period_label"]
    p_new = row_new["period_label"]
    esc = "nas demonstrações **consolidadas**" if want_consolidated else "nas demonstrações da **controladora (individual)**"
    base = (
        f"Comparação {esc} para **{row_old['legal_name']}**: a «{label}» foi **R$ {v_old:,.0f}** no **{p_old}** "
        f"e **R$ {v_new:,.0f}** no **{p_new}** (valores em {scale} de reais, mesma métrica e escopo)."
    )
    if pct is None:
        return base + " Não é possível calcular variação percentual porque o valor base é zero."
    if abs(pct) < 1e-9:
        return base + " Variação percentual: **0%**."
    if pct > 0:
        return base + f" Isso representa um **aumento de {pct:.2f}%** em relação ao período mais antigo."
    return base + f" Isso representa uma **queda de {abs(pct):.2f}%** em relação ao período mais antigo."


def _pick_best_fact(
    conn: sqlite3.Connection,
    source_document_id: int,
    question: str,
    ticker: str,
    legal_name: str,
    *,
    want_consolidated: bool,
) -> tuple[sqlite3.Row | None, float | None, str, str, str | None]:
    """
    Retorna (fato, score, raciocínio, sql_verificação_ou_vazio, resposta_pronta_se_fora_escopo).
    Quando a pergunta exige modificadores (ex.: por ação) e nenhuma métrica do documento atende,
    não retorna valor por proximidade lexical/embedding.
    """
    ic = _is_consolidated_sql_flag(want_consolidated)
    rows = conn.execute(
        """
        SELECT ff.id, ff.value_amount, ff.unit_scale, ff.currency, ff.page_number,
               ff.section_title, ff.row_label_raw, ff.is_consolidated, m.metric_key, m.display_name,
               sd.file_name, rp.period_label, c.legal_name
        FROM financial_facts ff
        JOIN metrics m ON m.id = ff.metric_id
        JOIN source_documents sd ON sd.id = ff.source_document_id
        JOIN reporting_periods rp ON rp.id = sd.reporting_period_id
        JOIN companies c ON c.id = rp.company_id
        WHERE ff.source_document_id = ? AND ff.is_consolidated = ?
        """,
        (source_document_id, ic),
    ).fetchall()

    if not rows:
        esc = "consolidadas" if want_consolidated else "controladora (is_consolidated = 0)"
        return (
            None,
            None,
            "• Métrica: **nenhum fato** neste documento para o escopo solicitado.\n"
            f"  – Filtro: `is_consolidated = {ic}`.\n"
            "  – Se o PDF foi ingerido mas a LLM **não extraiu** linhas para este demonstrativo, o relatório "
            "fica apenas cadastrado em `source_documents` (sem `financial_facts`). "
            "A métrica pedida pode **não constar** naquele recorte ou ter ficado fora das 15 primeiras páginas.",
            "",
            None,
        )

    q_full = _normalize(question)
    q_stripped = _strip_company_from_question(q_full, ticker, legal_name)
    q_for_tokens = q_stripped if len(q_stripped) >= 3 else q_full
    mods = _modifiers_from_question(question)
    rows_f = [r for r in rows if _row_satisfies_modifiers(r, mods)]

    if mods and not rows_f:
        illus: sqlite3.Row | None = None
        illus_score = -1.0
        for r in rows:
            if _row_satisfies_modifiers(r, mods):
                continue
            blob = _blob_for_fact(r["display_name"], r["metric_key"], r["row_label_raw"])
            s = _score_match(
                q_for_tokens,
                blob,
                r["display_name"],
                r["metric_key"],
                q_full,
            )
            if s > illus_score:
                illus_score = s
                illus = r
        req_txt = ", ".join(_modifier_labels_pt(mods))
        rac_lines = [
            "• **Validação contábil (RAG):** a pergunta exige modificadores que **não** aparecem em nenhuma "
            f"métrica mapeada neste documento: **{req_txt}**.",
            "• **Regra:** não usar métrica com alto score lexical/semântico se o **conceito contábil** for distinto "
            "(ex.: total em moeda ≠ valor por ação).",
            f"  – Fatos no documento após `is_consolidated = {ic}`: **{len(rows)}**; compatíveis com o modificador: **0**.",
        ]
        if illus is not None and illus_score >= 0.85:
            rac_lines.append(
                f"  – O melhor candidato **incompatível** seria `{illus['metric_key']}` (score lexical **{illus_score:.2f}**) — "
                "**rejeitado** por divergência de conceito."
            )
        sql_ex = _sql_verification_modifiers_reject(
            metric_key=str(illus["metric_key"]) if illus else None,
            display_name=str(illus["display_name"]) if illus else None,
            required_mods=mods,
        )
        intent = _human_intent_label(mods, question)
        related = _suggest_related_metrics(question, rows)
        rel_txt = ", ".join(related) if related else "nenhuma com proximidade relevante"
        answer = (
            f"A métrica «{intent}» **não está mapeada** neste documento (nenhuma linha atende aos modificadores exigidos). "
            f"Métricas disponíveis com **maior proximidade lexical** (apenas sugestão, **não** substituem o conceito pedido): {rel_txt}."
        )
        return None, None, "\n".join(rac_lines), sql_ex, answer

    work_rows = rows_f if mods else rows
    filter_note = ""
    if mods:
        filter_note = (
            f"• **Filtro de modificadores:** {len(work_rows)} candidato(s) alinhados ao conceito pedido "
            f"(de {len(rows)} no documento).\n"
        )

    sem_pack = rank_facts_semantic(q_for_tokens, q_full, list(work_rows))
    if sem_pack is not None:
        scored, sem_note = sem_pack
        best_s, best_r = scored[0]
        second_s = scored[1][0] if len(scored) > 1 else -1.0
        lines = [
            filter_note,
            sem_note,
            f"  Texto focal: «{q_for_tokens or q_full}».",
            f"  Candidatos após filtro (id={source_document_id}): {len(work_rows)}.",
        ]
        for s, r in scored[:5]:
            rot = (r["row_label_raw"] or "—")[:80]
            lines.append(f"  – score {s:.4f} → `{r['metric_key']}` — {r['display_name']} | rótulo: {rot}")

        if best_s >= 1.0 - 1e-9:
            lines.append(f"• Decisão: LLM fixou `{best_r['metric_key']}` para a intenção do usuário.")
            if not _row_satisfies_modifiers(best_r, mods):
                lines.append("• **Inconsistência:** candidato fora do filtro — abortando escolha.")
                return None, None, "\n".join(lines), "", None
            return best_r, best_s, "\n".join(lines), "", None

        _E_MIN, _E_GAP = 0.18, 0.035
        if best_s >= _E_MIN and (best_s - second_s) >= _E_GAP:
            lines.append(
                f"• Decisão: melhor cosseno **{best_s:.4f}** (2º **{second_s:.4f}**) → `{best_r['metric_key']}`."
            )
            if not _row_satisfies_modifiers(best_r, mods):
                lines.append("• **Inconsistência:** candidato fora do filtro — abortando escolha.")
                return None, None, "\n".join(lines), "", None
            return best_r, best_s, "\n".join(lines), "", None

        lines.append(
            "• Embeddings não separaram candidatos com confiança → **fallback lexical** (`SequenceMatcher` + tokens)."
        )
        prefix_fallback = "\n".join(lines) + "\n"
    else:
        prefix_fallback = (
            filter_note
            + "• **Ranking métrica:** sem API OpenAI/Gemini para embeddings/classificador — uso **fallback lexical**.\n"
        )

    scored_lex: list[tuple[float, sqlite3.Row]] = []
    for r in work_rows:
        blob = _blob_for_fact(r["display_name"], r["metric_key"], r["row_label_raw"])
        s = _score_match(
            q_for_tokens,
            blob,
            r["display_name"],
            r["metric_key"],
            q_full,
        )
        scored_lex.append((s, r))

    scored_lex.sort(key=lambda x: x[0], reverse=True)
    best_s, best_r = scored_lex[0]
    second_s = scored_lex[1][0] if len(scored_lex) > 1 else 0.0

    lines = [
        prefix_fallback,
        "• **Fallback lexical:** candidatos filtrados por `is_consolidated = "
        f"{ic}`; `SequenceMatcher` + tokens sobre `display_name`, `metric_key`, `row_label_raw`.",
        f"  Texto focal: «{q_for_tokens or q_full}».",
        f"  Candidatos após filtro (id={source_document_id}): {len(work_rows)}.",
    ]
    for s, r in scored_lex[:5]:
        rot = (r["row_label_raw"] or "—")[:80]
        lines.append(f"  – score {s:.2f} → `{r['metric_key']}` — {r['display_name']} | rótulo: {rot}")

    min_score = 0.85
    if best_s < min_score:
        lines.append(
            f"• Decisão: **nenhum** candidato atingiu o limiar mínimo ({min_score}); melhor score = {best_s:.2f}."
        )
        return None, None, "\n".join(lines), "", None

    if len(scored_lex) > 1 and second_s >= min_score:
        gap = best_s - second_s
        if gap < 0.18 and second_s >= 0.92 * best_s and best_s < 2.2:
            lines.append(
                "• Decisão: **empate ambíguo** entre os dois melhores (scores muito próximos e abaixo do "
                f"limiar de separação); 1º={best_s:.2f}, 2º={second_s:.2f}."
            )
            return None, None, "\n".join(lines), "", None

    if mods and not _row_satisfies_modifiers(best_r, mods):
        lines.append(
            "• **Validação:** melhor score lexical entre candidatos filtrados falhou verificação de modificadores — **não retornar**."
        )
        sql_ex = _sql_verification_modifiers_reject(
            metric_key=str(best_r["metric_key"]),
            display_name=str(best_r["display_name"]),
            required_mods=mods,
        )
        return None, None, "\n".join(lines), sql_ex, None

    lines.append(
        f"• Decisão: escolhido `{best_r['metric_key']}` com score **{best_s:.2f}** "
        f"(2º lugar {second_s:.2f}); valor e página vêm desta linha no SQLite."
    )
    return best_r, best_s, "\n".join(lines), "", None


def _format_answer(row: sqlite3.Row, *, want_consolidated: bool) -> str:
    amount = row["value_amount"]
    scale = row["unit_scale"]
    label = row["display_name"]
    esc = "consolidadas" if want_consolidated else "da controladora (individual)"
    valor_txt = f"R$ {amount:,.0f}"
    if amount < 0 and "lucro" in row["metric_key"]:
        valor_txt = f"prejuízo de R$ {abs(amount):,.0f} (valor contábil negativo)"
    return (
        f"«{label}» (**demonstrações {esc}**) em **{row['legal_name']}**, período **{row['period_label']}**: "
        f"{valor_txt} ({scale} de reais), conforme **{row['file_name']}**, página **{row['page_number']}**."
    )


def _available_metrics(
    conn: sqlite3.Connection, source_document_id: int, *, want_consolidated: bool
) -> str:
    ic = _is_consolidated_sql_flag(want_consolidated)
    rows = conn.execute(
        """
        SELECT DISTINCT m.display_name, m.metric_key
        FROM financial_facts ff
        JOIN metrics m ON m.id = ff.metric_id
        WHERE ff.source_document_id = ? AND ff.is_consolidated = ?
        ORDER BY m.display_name
        """,
        (source_document_id, ic),
    ).fetchall()
    if not rows:
        return ""
    parts = [f"{r['display_name']} ({r['metric_key']})" for r in rows]
    return "; ".join(parts)


def answer_question(conn: sqlite3.Connection, question: str) -> QAResult:
    if _is_meta_question(question):
        rac = (
            "• Tipo: pergunta **meta** (regex sobre raciocínio/funcionamento).\n"
            "• Ação: retorno texto fixo; **não** consultei `financial_facts`.\n"
            "• Ver também: `docs/FLUXOGRAMA.md` no repositório."
        )
        return QAResult(
            answer=_META_ANSWER,
            raciocinio=rac,
            sql_executed="-- Pergunta meta: sem consulta a financial_facts.",
            references=[
                {
                    "documento": "docs/FLUXOGRAMA.md",
                    "pagina": None,
                    "secao": "Documentação do projeto",
                    "metrica": None,
                    "periodo": None,
                    "escopo": None,
                }
            ],
        )

    n_companies = conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"]
    company = _resolve_company(conn, question)
    if not company:
        if n_companies == 0:
            rac = (
                "• **Diagnóstico:** a tabela `companies` está **vazia** — a API não chegou a usar OpenAI/Gemini "
                "para esta pergunta; o problema é **dados ausentes**, não a chave em si.\n"
                "• **Correção:** defina `OPENAI_API_KEY` (ou `GEMINI_API_KEY`), coloque o PDF em `data/pdfs/` e "
                "reinicie o servidor para ingerir; **ou** execute manualmente `sql/seed.sql` no SQLite."
            )
            return QAResult(
                answer=(
                    "Não há dados no banco. Coloque o PDF em `data/pdfs/`, configure OPENAI_API_KEY ou "
                    "GEMINI_API_KEY no `.env` e reinicie a API."
                ),
                raciocinio=rac,
                sql_executed="",
                references=[],
            )
        rac = (
            "• Empresa: há cadastro(s) em `companies`, mas **nenhum** atingiu pontuação mínima (≥4) com a pergunta "
            "(ticker, raiz do ticker ou palavras do razão social com ≥4 letras, ex.: «magazine», «luiza»).\n"
            "• Sua pergunta já cita «Magazine luiza» — se o banco tiver **Magazine Luiza S.A.**, o match deveria "
            "ocorrer; confira se o `legal_name` ingerido difere muito do texto falado."
        )
        return QAResult(
            answer="Não identifiquei a empresa na pergunta. Cite o nome ou o ticker cadastrado (ex.: Magazine Luiza, MGLU3).",
            raciocinio=rac,
            sql_executed="",
            references=[],
        )

    want_consolidated = _want_consolidated(question)
    scope_line = _explain_scope_technical(want_consolidated)

    hints = _find_periods_in_question(question)
    if _is_comparison_intent(question):
        pair = _select_two_periods_for_comparison(hints)
        if not pair:
            rac = "\n".join(
                [
                    _explain_company_choice(company),
                    scope_line,
                    "• Modo **comparação** ativado (aumento, queda, variação, %, «em relação a», entre, versus…).",
                    f"• `_find_periods_in_question` (regex nTyy, trimestre+ano, anos isolados): **{len(hints)}** âncora(s); "
                    "são necessárias **pelo menos duas** distintas (ex.: «1T22 e 1T21», «entre 2021 e 2022»).",
                ]
            )
            return QAResult(
                answer=(
                    "Para comparar, indique **dois períodos** na pergunta "
                    "(ex.: «Qual o aumento da receita entre 2021 e 2022?» ou «4T21 versus 1T22»)."
                ),
                raciocinio=rac,
                sql_executed="",
                references=[],
            )

        anchor_old, anchor_new = pair
        row_old_p = _resolve_period_row_for_anchor(
            conn, company["id"], anchor_old[0], anchor_old[1]
        )
        row_new_p = _resolve_period_row_for_anchor(
            conn, company["id"], anchor_new[0], anchor_new[1]
        )

        miss_parts: list[str] = []
        if not row_old_p:
            miss_parts.append(
                f"período mais antigo ({_describe_anchor(anchor_old)} / documento não cadastrado)"
            )
        if not row_new_p:
            miss_parts.append(
                f"período mais recente ({_describe_anchor(anchor_new)} / documento não cadastrado)"
            )
        if miss_parts:
            rac = "\n".join(
                [
                    _explain_company_choice(company),
                    scope_line,
                    "• Comparação: ordenação cronológica das âncoras → "
                    f"antigo = {_describe_anchor(anchor_old)}, novo = {_describe_anchor(anchor_new)}.",
                    "• Resolução no banco: " + "; ".join(miss_parts) + ".",
                ]
            )
            return QAResult(
                answer=(
                    "Não consigo concluir a comparação: faltam dados no banco para — "
                    + ", ".join(miss_parts)
                    + "."
                ),
                raciocinio=rac,
                sql_executed="",
                references=[],
            )

        doc_old = row_old_p["source_document_id"]
        doc_new = row_new_p["source_document_id"]
        picked_old, _sc_old, rac_old, sql_oos_cmp, ans_oos_cmp = _pick_best_fact(
            conn,
            doc_old,
            question,
            company["ticker"],
            company["legal_name"],
            want_consolidated=want_consolidated,
        )
        head_cmp = "\n".join(
            [
                _explain_company_choice(company),
                scope_line,
                "• Modo **comparação** (motor RAG estruturado): intenção de variação + **duas** âncoras temporais (regex).",
                f"• Períodos ordenados: **{row_old_p['period_label']}** (V1, mais antigo) e **{row_new_p['period_label']}** (V2, mais recente).",
            ]
        )

        if ans_oos_cmp:
            return QAResult(
                answer=ans_oos_cmp,
                raciocinio=head_cmp + "\n" + (rac_old or ""),
                sql_executed=sql_oos_cmp,
                references=[],
            )

        if not picked_old:
            if sql_oos_cmp:
                return QAResult(
                    answer=(
                        "Não identifiquei uma métrica **compatível com o conceito contábil** da pergunta "
                        "(validação de modificadores / ranking)."
                    ),
                    raciocinio=head_cmp + "\n" + (rac_old or ""),
                    sql_executed=sql_oos_cmp,
                    references=[],
                )
            catalog = _available_metrics(conn, doc_old, want_consolidated=want_consolidated)
            extra = f" Métricas no documento antigo: {catalog}" if catalog else ""
            return QAResult(
                answer=(
                    "Não identifiquei a métrica no primeiro período da comparação." + extra
                ),
                raciocinio=head_cmp + "\n" + (rac_old or ""),
                sql_executed="",
                references=[],
            )

        mk = picked_old["metric_key"]
        picked_new = _fetch_fact_by_metric_key(
            conn, doc_new, mk, want_consolidated=want_consolidated
        )
        if not picked_new:
            rac = "\n".join(
                [
                    head_cmp,
                    rac_old,
                    f"• Segundo período ({row_new_p['period_label']}): **não há** fato com `metric_key` = `{mk}` "
                    f"e `is_consolidated = {_is_consolidated_sql_flag(want_consolidated)}`.",
                ]
            )
            return QAResult(
                answer=(
                    f"Encontrei a métrica «{picked_old['display_name']}» (`{mk}`) em **{row_old_p['period_label']}**, "
                    f"mas **não** há essa métrica cadastrada no documento de **{row_new_p['period_label']}**."
                ),
                raciocinio=rac,
                sql_executed="",
                references=[_reference_dict_from_row(picked_old)],
            )

        v_old = float(picked_old["value_amount"])
        v_new = float(picked_new["value_amount"])
        pct: float | None
        if v_old == 0:
            pct = None
        else:
            pct = (v_new - v_old) / v_old * 100.0

        answer = _format_comparison_answer(
            picked_old, picked_new, pct, want_consolidated=want_consolidated
        )
        sql_cmp = _build_sql_comparison(
            mk,
            row_old_p["period_label"],
            row_new_p["period_label"],
            company["ticker"],
            want_consolidated=want_consolidated,
            verification_preamble=_sql_ok_verification_line(question, picked_old),
        )
        refs_cmp = [
            _reference_dict_from_row(picked_old),
            _reference_dict_from_row(picked_new),
        ]
        pct_line = (
            "• Variação percentual: ((valor_novo − valor_antigo) / valor_antigo) × 100"
            + (f" = **{pct:.2f}%**." if pct is not None else " — **indefinida** (base zero).")
        )
        rac_cmp = "\n".join(
            [
                head_cmp,
                rac_old,
                f"• Segundo período: `SELECT` com `metric_key = '{mk}'`, mesmo `is_consolidated`, documento **{row_new_p['period_label']}** "
                "(sem novo `SequenceMatcher` — métrica fixada pela primeira janela).",
                pct_line,
            ]
        )
        return QAResult(
            answer=answer,
            raciocinio=rac_cmp,
            sql_executed=sql_cmp,
            references=refs_cmp,
        )

    period = _resolve_period_row(conn, company["id"], question)
    if not period:
        rac = "\n".join(
            [
                _explain_company_choice(company),
                scope_line,
                "• Período: **sem** registro em `reporting_periods` / `source_documents` para essa empresa.",
            ]
        )
        return QAResult(
            answer=f"Não há período/documento cadastrado para {company['legal_name']}.",
            raciocinio=rac,
            sql_executed="",
            references=[],
        )

    doc_id = period["source_document_id"]
    picked, _score, pick_rac, sql_oos, ans_oos = _pick_best_fact(
        conn,
        doc_id,
        question,
        company["ticker"],
        company["legal_name"],
        want_consolidated=want_consolidated,
    )

    head = "\n".join(
        [
            _explain_company_choice(company),
            scope_line,
            _explain_period_choice(question, period),
        ]
    )

    if ans_oos:
        return QAResult(
            answer=ans_oos,
            raciocinio=head + "\n" + pick_rac,
            sql_executed=sql_oos,
            references=[],
        )

    if not picked:
        if sql_oos:
            return QAResult(
                answer=(
                    "Não consegui associar a pergunta a uma métrica **compatível** após validação contábil "
                    "(conceito distinto do pedido, apesar do score lexical/semântico)."
                ),
                raciocinio=head + "\n" + pick_rac,
                sql_executed=sql_oos,
                references=[],
            )
        catalog = _available_metrics(conn, doc_id, want_consolidated=want_consolidated)
        extra = f" Métricas disponíveis neste documento: {catalog}" if catalog else ""
        return QAResult(
            answer=(
                "Não consegui associar a pergunta a uma única métrica com confiança suficiente."
                + extra
            ),
            raciocinio=head + "\n" + pick_rac,
            sql_executed="",
            references=[],
        )

    refs = [_reference_dict_from_row(picked)]
    sql_dyn = _build_sql_fact_lookup(
        picked["metric_key"],
        period["period_label"],
        company["ticker"],
        want_consolidated=want_consolidated,
        verification_preamble=_sql_ok_verification_line(question, picked),
    )
    return QAResult(
        answer=_format_answer(picked, want_consolidated=want_consolidated),
        raciocinio=head + "\n" + pick_rac,
        sql_executed=sql_dyn,
        references=refs,
    )
