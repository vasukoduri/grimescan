"""The scan report: scores, gating, and JSON / Markdown / HTML / fix-list outputs."""

from __future__ import annotations

import csv
import html
import io
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .model import CATEGORIES, SEVERITIES, Issue


def grade_of(score: float) -> str:
    for limit, g in ((90, "A"), (80, "B"), (65, "C"), (50, "D")):
        if score >= limit:
            return g
    return "F"


@dataclass
class Report:
    source: str
    n_rows: int
    n_columns: int
    columns: list[dict[str, Any]]
    issues: list[Issue]
    score: float
    missing_cells: int = 0
    timeline: list[list[float]] = field(default_factory=list)
    timeline_chunk: int = 0
    checks_run: list[str] = field(default_factory=list)
    seconds: float = 0.0
    profile_seconds: float = 0.0

    # ------------------------------------------------------------------ summary
    @property
    def grade(self) -> str:
        return grade_of(self.score)

    def by_severity(self) -> dict[str, int]:
        c = Counter(i.severity for i in self.issues)
        return {s: c.get(s, 0) for s in SEVERITIES}

    def by_category(self) -> dict[str, int]:
        c = Counter(i.category for i in self.issues)
        return {k: c.get(k, 0) for k in CATEGORIES}

    def summary(self) -> dict[str, Any]:
        dup = next((i.count for i in self.issues if i.title == "Exact duplicate rows"), 0)
        cells = max(1, self.n_rows * self.n_columns)
        return {
            "source": self.source,
            "rows": self.n_rows,
            "columns": self.n_columns,
            "score": self.score,
            "grade": self.grade,
            "issues": len(self.issues),
            "by_severity": self.by_severity(),
            "by_category": self.by_category(),
            "missing_cells": self.missing_cells,
            "missing_cell_rate": round(self.missing_cells / cells, 6),
            "duplicate_rows": dup,
            "seconds": self.seconds,
            "rows_per_second": int(self.n_rows / self.seconds) if self.seconds else None,
        }

    def check(self, fail_under: float | None = None, fail_on: str | None = None) -> tuple[bool, list[str]]:
        """Gate for CI: fail if the score is below ``fail_under`` or any issue is at least ``fail_on`` severity."""
        reasons = []
        if fail_under is not None and self.score < fail_under:
            reasons.append(f"quality score {self.score} < {fail_under}")
        if fail_on:
            rank = SEVERITIES.index(fail_on)
            bad = [i for i in self.issues if SEVERITIES.index(i.severity) <= rank]
            if bad:
                reasons.append(f"{len(bad)} issue(s) at severity '{fail_on}' or worse")
        return (not reasons, reasons)

    # ------------------------------------------------------------------ outputs
    def to_dict(self) -> dict[str, Any]:
        return {
            "grimescan_version": __version__,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": self.summary(),
            "columns": self.columns,
            "issues": [asdict(i) for i in self.issues],
            "checks_run": self.checks_run,
            "missing_timeline": {"rows_per_chunk": self.timeline_chunk, "rates": self.timeline},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def to_markdown(self) -> str:
        s = self.summary()
        sev = s["by_severity"]
        lines = [
            f"# grimescan report: `{self.source}`",
            "",
            f"**Quality score {self.score}/100 (grade {self.grade})** · {self.n_rows:,} rows × {self.n_columns} columns · "
            f"{sev['high']} high, {sev['medium']} medium, {sev['low']} low-severity issues",
            "",
            "| Column | Type | Missing | Distinct (approx.) | Score | Issues |",
            "|---|---|---|---|---|---|",
        ]
        for c in self.columns:
            lines.append(
                f"| `{c['name']}` | {c['type']} | {c['missing_rate']:.1%} | {c['distinct_estimate']:,} | "
                f"{c['score']} | {c['issues']} |"
            )
        for cat, label in CATEGORIES.items():
            items = [i for i in self.issues if i.category == cat]
            if not items:
                continue
            lines += ["", f"## {label}", ""]
            for i in items:
                lines.append(f"- **[{i.severity}] {i.title}**: {i.message}")
                for ex in i.examples[:3]:
                    where = f"row {ex.row:,}" if ex.row is not None else ""
                    val = f" `{ex.value}`" if ex.value else ""
                    fix = f" → `{ex.suggestion}`" if ex.suggestion else ""
                    lines.append(f"  - {where}{val}: {ex.note}{fix}")
        return "\n".join(lines) + "\n"

    def to_fix_list(self) -> str:
        """CSV of every example: row, column, check, severity, value, problem, suggestion."""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["row", "column", "check", "severity", "value", "problem", "suggestion"])
        for i in self.issues:
            for ex in i.examples:
                w.writerow(
                    [ex.row, ex.column or ", ".join(i.columns), i.check, i.severity, ex.value, ex.note, ex.suggestion]
                )
        return buf.getvalue()

    def to_html(self) -> str:
        return _render_html(self)

    def save(self, out_dir: str | Path) -> dict[str, Path]:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        files = {
            "html": ("report.html", self.to_html),
            "json": ("report.json", self.to_json),
            "md": ("report.md", self.to_markdown),
            "fixes": ("fix_list.csv", self.to_fix_list),
        }
        paths = {}
        for key, (name, fn) in files.items():
            (out / name).write_text(fn(), encoding="utf-8")
            paths[key] = out / name
        return paths


# ====================================================================== HTML
_SEV_COLOR = {"high": "#d1242f", "medium": "#bf8700", "low": "#0969da", "info": "#6e7781"}
_GRADE_COLOR = {"A": "#1a7f37", "B": "#4d8b31", "C": "#bf8700", "D": "#c2410c", "F": "#d1242f"}
_TYPE_LABEL = {
    "us_zip": "ZIP",
    "person_name": "name",
    "card_number": "card",
    "currency_amount": "money",
    "us_state": "state",
    "integer": "integer",
    "decimal": "number",
}


def _sparkline(hist: dict | None) -> str:
    if not hist or not hist.get("counts"):
        return ""
    counts = hist["counts"]
    top = max(counts) or 1
    w, h = 120, 26
    bw = w / len(counts)
    bars = "".join(
        f"<rect x='{k * bw:.1f}' y='{h - c / top * h:.1f}' width='{max(bw - 1, 1):.1f}' height='{c / top * h:.1f}'/>"
        for k, c in enumerate(counts)
        if c
    )
    title = f"{hist['lo']:,.4g} to {hist['hi']:,.4g}"
    return f"<svg class='spark' viewBox='0 0 {w} {h}' width='{w}' height='{h}'><title>{title}</title>{bars}</svg>"


def _heatmap(rep: Report) -> str:
    rates = rep.timeline
    if not rates or not rep.columns:
        return "<p class='muted'>No data.</p>"
    n_chunks, n_cols = len(rates), len(rep.columns)
    cell_w = max(3.0, 760 / n_chunks)  # the grid keeps the same width whatever the number of chunks
    cell_h = 16
    label_w = 150
    w = label_w + cell_w * n_chunks + 10
    h = cell_h * n_cols + 26
    parts = []
    for j, col in enumerate(rep.columns):
        y = j * cell_h
        name = html.escape(col["name"][:22])
        parts.append(f"<text x='{label_w - 8}' y='{y + 12}' text-anchor='end' class='hm-label'>{name}</text>")
        for k in range(n_chunks):
            r = rates[k][j]
            if r <= 0:
                continue
            x = label_w + k * cell_w
            start = k * rep.timeline_chunk + 1
            parts.append(
                f"<rect x='{x:.1f}' y='{y + 1}' width='{cell_w:.1f}' height='{cell_h - 2}' "
                f"fill='var(--hm)' fill-opacity='{0.15 + 0.85 * r:.2f}'><title>{html.escape(col['name'])}, "
                f"rows {start:,}–{start + rep.timeline_chunk - 1:,}: {r:.0%} missing</title></rect>"
            )
    axis_y = n_cols * cell_h + 16
    parts.append(f"<text x='{label_w}' y='{axis_y}' class='hm-axis'>row 1</text>")
    parts.append(
        f"<text x='{label_w + cell_w * n_chunks}' y='{axis_y}' text-anchor='end' class='hm-axis'>"
        f"row {rep.n_rows:,}</text>"
    )
    grid = f"<rect x='{label_w}' y='0' width='{cell_w * n_chunks:.1f}' height='{n_cols * cell_h}' class='hm-bg'/>"
    return f"<svg class='heatmap' viewBox='0 0 {w:.0f} {h}' width='{w:.0f}' height='{h}'>{grid}{''.join(parts)}</svg>"


def _render_html(rep: Report) -> str:
    e = html.escape
    s = rep.summary()
    sev = s["by_severity"]
    gcolor = _GRADE_COLOR[rep.grade]
    # category bars
    cat_counts = s["by_category"]
    cmax = max(cat_counts.values()) or 1
    cats = "".join(
        f"<div class='cat'><span>{e(CATEGORIES[k])}</span><div class='cbar'><i style='width:{v / cmax * 100:.0f}%'>"
        f"</i></div><b>{v}</b></div>"
        for k, v in cat_counts.items()
    )
    # columns table
    rows = []
    for c in rep.columns:
        score = c["score"]
        color = _GRADE_COLOR[grade_of(score)]
        tops = ""
        if not c.get("histogram") and c.get("top_values"):
            tops = (
                "<span class='tops'>"
                + ", ".join(f"{e(str(v))[:18]} ({n:,})" for v, n in c["top_values"][:3])
                + "</span>"
            )
        rows.append(
            f"<tr><td><code>{e(c['name'])}</code></td><td><span class='type'>{e(_TYPE_LABEL.get(c['type'], c['type']))}"
            f"</span></td><td><div class='mbar'><i style='width:{c['missing_rate'] * 100:.1f}%'></i></div>"
            f"{c['missing_rate']:.1%}</td><td>{c['distinct_estimate']:,}</td>"
            f"<td>{_sparkline(c.get('histogram'))}{tops}</td>"
            f"<td><div class='sbar'><i style='width:{score:.0f}%;background:{color}'></i></div>{score:.0f}</td>"
            f"<td>{c['issues'] or ''}</td></tr>"
        )
    # issues
    blocks = []
    for cat, label in CATEGORIES.items():
        items = [i for i in rep.issues if i.category == cat]
        if not items:
            continue
        cards = []
        for i in items:
            ex_rows = "".join(
                f"<tr><td>{'' if x.row is None else f'{x.row:,}'}</td><td>{e(x.column or '')}</td>"
                f"<td><code>{e(str(x.value)) if x.value is not None else ''}</code></td><td>{e(x.note)}</td>"
                f"<td>{'<code>' + e(str(x.suggestion)) + '</code>' if x.suggestion else ''}</td></tr>"
                for x in i.examples
            )
            table = (
                (
                    f"<table class='ex'><tr><th>Row</th><th>Column</th><th>Value</th><th>Problem</th>"
                    f"<th>Suggested fix</th></tr>{ex_rows}</table>"
                )
                if ex_rows
                else ""
            )
            pct = f" · {i.rate:.2%}" if i.count and i.rate else ""
            masked = " · <span class='muted'>values masked</span>" if i.sensitive else ""
            cards.append(
                f"<details class='issue' {'open' if i.severity == 'high' and len(cards) < 3 else ''}>"
                f"<summary><span class='pill' style='background:{_SEV_COLOR[i.severity]}'>{i.severity}</span> "
                f"<b>{_md(i.title)}</b><span class='muted'>{f' · {i.count:,} affected' if i.count else ''}{pct}"
                f"</span>{masked}</summary><p>{_md(i.message)}</p>{table}"
                f"<p class='muted small'>check <code>{e(i.check)}</code></p></details>"
            )
        blocks.append(f"<h3>{e(label)} <span class='muted'>({len(items)})</span></h3>{''.join(cards)}")
    issues_html = "".join(blocks) or "<p class='clean'>No problems found. This dataset looks clean.</p>"
    speed = f"{s['rows_per_second']:,} rows/s" if s["rows_per_second"] else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>grimescan: {e(rep.source)}</title>
<style>
:root{{--fg:#1f2328;--mut:#59636e;--bd:#d1d9e0;--bg:#ffffff;--card:#f6f8fa;--hm:#d1242f;--accent:#8250df}}
@media (prefers-color-scheme:dark){{:root{{--fg:#e6edf3;--mut:#9198a1;--bd:#3d444d;--bg:#0d1117;--card:#151b23;--hm:#ff7b72}}}}
*{{box-sizing:border-box}}
body{{font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--fg);background:var(--bg);max-width:1180px;margin:0 auto;padding:2rem 1.2rem 4rem}}
h1{{font-size:1.6rem;margin:0 0 .2rem}} h2{{font-size:1.15rem;margin:2.2rem 0 .8rem}} h3{{font-size:1rem;margin:1.6rem 0 .6rem}}
.muted{{color:var(--mut)}} .small{{font-size:.8rem;margin:.4rem 0 0}}
code{{font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}}
.hero{{display:grid;grid-template-columns:200px 1fr;gap:1.2rem;align-items:center;margin:1.4rem 0}}
.score{{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:1.1rem;text-align:center}}
.score b{{display:block;font-size:3rem;line-height:1;color:{gcolor}}} .score span{{font-size:1.1rem;font-weight:600;color:{gcolor}}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.7rem}}
.kpi{{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:.7rem .9rem}}
.kpi b{{display:block;font-size:1.35rem}} .kpi small{{color:var(--mut)}}
.cats{{display:grid;gap:.35rem;max-width:720px}}
.cat{{display:grid;grid-template-columns:300px 1fr 30px;gap:.6rem;align-items:center;font-size:.9rem}}
.cbar,.mbar,.sbar{{height:8px;background:var(--bd);border-radius:5px;overflow:hidden}}
.cbar i{{display:block;height:100%;background:var(--accent)}} .mbar i{{display:block;height:100%;background:var(--hm)}}
.sbar i{{display:block;height:100%}}
.mbar,.sbar{{width:70px;display:inline-block;vertical-align:middle;margin-right:.4rem}}
table{{border-collapse:collapse;width:100%}} th,td{{text-align:left;padding:.42rem .5rem;border-bottom:1px solid var(--bd);vertical-align:middle}}
th{{font-size:.8rem;color:var(--mut);font-weight:600}}
.type{{font-size:.75rem;background:var(--card);border:1px solid var(--bd);border-radius:999px;padding:.05rem .5rem}}
.spark rect{{fill:var(--accent);opacity:.75}} .tops{{font-size:.78rem;color:var(--mut)}}
.heatmap{{max-width:100%;height:auto;background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:.6rem}}
.hm-label{{font-size:10px;fill:var(--fg)}} .hm-axis{{font-size:10px;fill:var(--mut)}} .hm-bg{{fill:var(--bg)}}
details.issue{{border:1px solid var(--bd);border-radius:10px;margin:.5rem 0;padding:.6rem .85rem;background:var(--card)}}
details.issue summary{{cursor:pointer}} details.issue p{{margin:.5rem 0}}
.pill{{color:#fff;font-size:.72rem;padding:.1rem .5rem;border-radius:999px;text-transform:uppercase;letter-spacing:.03em;margin-right:.3rem}}
table.ex{{font-size:.85rem;background:var(--bg);border-radius:6px}}
.clean{{font-size:1.1rem;color:#1a7f37}}
footer{{margin-top:3rem;color:var(--mut);font-size:.8rem}}
@media (max-width:700px){{.hero{{grid-template-columns:1fr}} .cat{{grid-template-columns:1fr 1fr 30px}}}}
</style></head><body>
<h1>grimescan report</h1>
<div class="muted"><code>{e(rep.source)}</code> · {rep.n_rows:,} rows × {rep.n_columns} columns · scanned in {rep.seconds:,}s {("· " + speed) if speed else ""}</div>
<div class="hero">
  <div class="score"><small class="muted">Data quality</small><b>{rep.score:.0f}</b><span>grade {rep.grade}</span></div>
  <div class="kpis">
    <div class="kpi"><small>High severity</small><b style="color:{_SEV_COLOR["high"]}">{sev["high"]}</b></div>
    <div class="kpi"><small>Medium</small><b style="color:{_SEV_COLOR["medium"]}">{sev["medium"]}</b></div>
    <div class="kpi"><small>Low</small><b style="color:{_SEV_COLOR["low"]}">{sev["low"]}</b></div>
    <div class="kpi"><small>Missing cells</small><b>{s["missing_cell_rate"]:.1%}</b><small>{s["missing_cells"]:,}</small></div>
    <div class="kpi"><small>Duplicate rows</small><b>{s["duplicate_rows"]:,}</b></div>
  </div>
</div>
<h2>Issues by category</h2><div class="cats">{cats}</div>
<h2>Columns</h2>
<table><tr><th>Column</th><th>Detected type</th><th>Missing</th><th>Distinct (approx.)</th><th>Distribution / top values</th><th>Quality</th><th>Issues</th></tr>{"".join(rows)}</table>
<h2>Missing values across the file</h2>
<p class="muted small">Each row of cells is a column; left to right runs through the file. Darker means more missing. Hover for details.</p>
{_heatmap(rep)}
<h2>Issues ({len(rep.issues)})</h2>{issues_html}
<footer>grimescan {e(__version__)} · {len(rep.checks_run)} checks run · examples are a sample; counts cover every row ·
sensitive values are masked · {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</footer>
</body></html>"""


def _md(text: str) -> str:
    """Render `code` spans from check messages."""
    parts = html.escape(text).split("`")
    return "".join(f"<code>{p}</code>" if k % 2 else p for k, p in enumerate(parts))
