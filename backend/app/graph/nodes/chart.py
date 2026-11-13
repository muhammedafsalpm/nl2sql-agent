"""Chart builder (deterministic) — turns the retrieved rows into a render-ready chart spec.

This node makes NO LLM call. The Orchestrator already decided *whether* the user wants a chart
and *which* type; everything else follows from the shape of the result set. That is deliberate:

  the chart carries the SAME numbers the Executor returned, never numbers an LLM re-typed,

so a chart can never disagree with the answer text. It also costs ~1ms, which matters in a
pipeline where the LLM agents dominate the latency.

Emits {} (no chart) rather than a misleading one whenever the data cannot honestly be plotted.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from app.graph.state import AgentState
from app.observability import get_logger

log = get_logger()

# Readability caps. A chart with hundreds of categories communicates nothing.
MAX_DONUT_SLICES = 8
MAX_BAR_CATEGORIES = 25
MAX_LINE_POINTS = 200
# A column counts as numeric when most of its non-null values parse as numbers. Below this it is
# treated as a label, so an ID-like text column never becomes a value axis by accident.
NUMERIC_RATIO = 0.7


def _to_number(value: Any) -> float | None:
    """Best-effort numeric coercion. Returns None when the value is not a number.

    Text columns matter here: some length/progress columns in this database are varchar, so a
    real number can arrive as '1,200' or '450 m'.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return float(value)
        except (TypeError, ValueError, ArithmeticError):
            return None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("%", "")
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            # Trailing unit, e.g. '450 m'
            head = cleaned.split()[0] if cleaned.split() else ""
            try:
                return float(head)
            except ValueError:
                return None
    return None


def _is_temporal(value: Any) -> bool:
    return isinstance(value, (dt.date, dt.datetime))


def _column_kinds(columns: list[str], rows: list[list[Any]]) -> list[str]:
    """Classify every column as 'temporal', 'numeric' or 'categorical'."""
    kinds: list[str] = []
    for i in range(len(columns)):
        values = [r[i] for r in rows if i < len(r)]
        present = [v for v in values if v is not None]
        if not present:
            kinds.append("categorical")
            continue
        if all(_is_temporal(v) for v in present):
            kinds.append("temporal")
            continue
        parsed = sum(1 for v in present if _to_number(v) is not None)
        kinds.append("numeric" if parsed / len(present) >= NUMERIC_RATIO else "categorical")
    return kinds


def _label_text(value: Any) -> str:
    if value is None:
        return "(blank)"
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def _pick_columns(kinds: list[str]) -> tuple[int, list[int]]:
    """Return (label_index, value_indexes). value_indexes is empty when nothing is plottable."""
    label_idx = next((i for i, k in enumerate(kinds) if k == "temporal"), None)
    if label_idx is None:
        label_idx = next((i for i, k in enumerate(kinds) if k == "categorical"), None)
    if label_idx is None:
        label_idx = 0  # all numeric: use the first column as the axis
    values = [i for i, k in enumerate(kinds) if k == "numeric" and i != label_idx]
    return label_idx, values


def _choose_type(requested: str, is_temporal: bool, n_rows: int, n_values: int) -> str:
    if n_rows == 1 and n_values == 1:
        return "kpi"  # a single number is a stat card, not a chart
    if is_temporal:
        return "line"
    if requested == "donut":
        return "donut" if n_values == 1 else "bar"
    if requested in ("bar", "line"):
        return requested
    return "bar"


def chart_node(state: AgentState) -> dict:
    # Routing only ever sends control here when wants_chart is already True, so this is a
    # defensive fallback, not a normal path — but it still logs if it's ever reached.
    if not state.get("wants_chart"):
        log.info("chart: skipped - not requested")
        return {"chart": {}}

    # Include steps the Verifier set aside: on an unverified turn the answer is written from them,
    # so the chart must plot the same rows or it would contradict the text beside it.
    available = (state.get("steps") or []) + (state.get("rejected_steps") or [])
    step = next((s for s in reversed(available) if s.get("rows")), None)
    if step is None:
        log.info("chart: skipped - no rows to plot")
        return {"chart": {}}

    # A capped result would render as a confident but partial picture. Refuse instead.
    if step.get("truncated"):
        log.info("chart: skipped - result was truncated, plotting it would mislead")
        return {"chart": {}}

    columns: list[str] = list(step.get("columns") or [])
    rows: list[list[Any]] = [r for r in (step.get("rows") or [])]
    if not columns or not rows:
        log.info("chart: skipped - empty result")
        return {"chart": {}}

    kinds = _column_kinds(columns, rows)

    # A single all-numeric row is a summary, not a series: plot the COLUMNS as the categories.
    # 'SELECT COUNT(*) AS well_count' becomes a KPI card; 'SELECT wells, rigs' becomes two bars.
    pivoted = len(rows) == 1 and all(k == "numeric" for k in kinds)
    if pivoted:
        rows = [[name, value] for name, value in zip(columns, rows[0])]
        columns = ["metric", "value"]
        kinds = _column_kinds(columns, rows)

    label_idx, value_idxs = _pick_columns(kinds)
    if not value_idxs:
        log.info("chart: skipped - no numeric column in %s", columns)
        return {"chart": {}}

    is_temporal = kinds[label_idx] == "temporal"
    chart_type = _choose_type(state.get("chart_type", "auto"), is_temporal, len(rows), len(value_idxs))
    notes: list[str] = []

    # Order: chronological for a time axis, largest-first otherwise.
    primary = value_idxs[0]
    if is_temporal:
        rows = sorted(rows, key=lambda r: (r[label_idx] is None, r[label_idx]))
    elif chart_type in ("bar", "donut"):
        rows = sorted(rows, key=lambda r: (_to_number(r[primary]) is None, -(_to_number(r[primary]) or 0.0)))

    total_rows = len(rows)
    if chart_type == "donut" and total_rows > MAX_DONUT_SLICES:
        keep = rows[: MAX_DONUT_SLICES - 1]
        others = sum(_to_number(r[primary]) or 0.0 for r in rows[MAX_DONUT_SLICES - 1 :])
        rows = keep
        notes.append(f"top {len(keep)} of {total_rows} shown, the rest grouped as 'Others'")
    else:
        others = None
        cap = MAX_LINE_POINTS if chart_type == "line" else MAX_BAR_CATEGORIES
        if chart_type != "kpi" and total_rows > cap:
            rows = rows[:cap]
            notes.append(f"showing {cap} of {total_rows}")

    labels = [_label_text(r[label_idx]) for r in rows]
    if chart_type == "donut" and others is not None:
        labels.append("Others")

    missing = 0
    unparsed = 0
    series: list[dict[str, Any]] = []
    for idx in value_idxs:
        data: list[float] = []
        for r in rows:
            raw = r[idx] if idx < len(r) else None
            number = _to_number(raw)
            if number is None:
                if raw is None:
                    missing += 1
                else:
                    unparsed += 1
                number = 0.0
            data.append(round(number, 6))
        if chart_type == "donut" and others is not None and idx == primary:
            data.append(round(others, 6))
        series.append({"name": columns[idx], "data": data})

    if missing:
        notes.append(f"{missing} empty value(s) plotted as 0")
    if unparsed:
        notes.append(f"{unparsed} non-numeric value(s) plotted as 0")

    spec = {
        "type": chart_type,
        "title": (step.get("sub_question") or state.get("question", "")).strip()[:120],
        "labels": labels,
        "series": series,
        # A pivoted summary has synthetic column names ('metric'/'value') - no axis titles.
        "x_axis": "" if pivoted or chart_type in ("donut", "kpi") else columns[label_idx],
        "y_axis": (
            columns[value_idxs[0]]
            if not pivoted and len(value_idxs) == 1 and chart_type != "kpi"
            else ""
        ),
        "note": "; ".join(notes),
    }
    log.info(
        "chart: %s built - %d point(s), %d series%s",
        chart_type,
        len(labels),
        len(series),
        f" ({spec['note']})" if spec["note"] else "",
    )
    return {"chart": spec}
