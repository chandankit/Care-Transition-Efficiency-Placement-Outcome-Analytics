"""Plotly figure builders shared by the dashboard and the report export.

Design rules: one y-axis per chart (stocks and flows have different scales,
so they are never overlaid); colour follows the measure, never its position;
at most three categorical colours per chart (the palette's validated
all-pairs set); raw daily values are faint, rolling means carry the signal.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from config import LABELS

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
DIVERGING = [[0.0, "#1c5cab"], [0.25, "#6da7ec"], [0.5, "#f0efec"], [0.75, "#ee8a89"], [1.0, "#b8302f"]]

# Fixed colour per measure (categorical slots 1-3 for the three flows).
COLORS = {
    "cbp_transfers_out": "#2a78d6",
    "cbp_intake": "#eb6834",
    "hhs_discharges": "#1baf7a",
    "cbp_custody": "#6250d6",
    "hhs_care": "#008300",
    "implied_hhs_admissions": "#eda100",
    "transfer_to_stock_pct": "#2a78d6",
    "discharge_to_stock_pct": "#1baf7a",
}
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"

# Short legend labels (full definitions live in config.LABELS / KPI dictionary).
SHORT = {
    "cbp_intake": "CBP intake", "cbp_custody": "CBP custody", "cbp_transfers_out": "Transfers out of CBP",
    "hhs_care": "HHS care", "hhs_discharges": "HHS discharges",
    "implied_hhs_admissions": "Implied HHS admissions (derived)",
    "transfer_to_stock_pct": "Transfer-to-stock ratio", "discharge_to_stock_pct": "Discharge-to-stock ratio",
    "hhs_change_per_day": "HHS change per day", "discharge_cv": "Discharge CV",
}


def label(col: str) -> str:
    return SHORT.get(col, LABELS.get(col, col.replace("_", " ")))


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def style(fig: go.Figure, title: str, ytitle: str = "", height: int = 400,
          hover: str = "x unified") -> go.Figure:
    fig.update_layout(
        title=dict(text=title, x=0, xanchor="left", font=dict(size=15, color=INK)),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, height=height,
        font=dict(family=FONT, size=12, color=INK_2),
        margin=dict(l=64, r=24, t=56, b=84), hovermode=hover,
        legend=dict(orientation="h", yanchor="top", y=-0.14, xanchor="left", x=0, title=None,
                    font=dict(color=INK_2)),
        hoverlabel=dict(bgcolor="white", font=dict(family=FONT, color=INK)),
    )
    fig.update_xaxes(showgrid=False, linecolor=AXIS, ticks="outside", tickcolor=AXIS, color=MUTED)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=AXIS, color=MUTED,
                     title=dict(text=ytitle, font=dict(color=INK_2)))
    return fig


def _runs(mask: pd.Series, dates: pd.Series) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Contiguous True runs of a boolean mask, as (start, end) dates."""
    m = mask.fillna(False).astype(bool).to_numpy()
    d = pd.to_datetime(dates).to_numpy()
    out, i = [], 0
    while i < len(m):
        if m[i]:
            j = i
            while j + 1 < len(m) and m[j + 1]:
                j += 1
            out.append((pd.Timestamp(d[i]), pd.Timestamp(d[j])))
            i = j + 1
        else:
            i += 1
    return out


def shade_runs(fig: go.Figure, mask: pd.Series, dates: pd.Series, color: str, label: str,
               opacity: float = 0.14) -> go.Figure:
    for k, (a, b) in enumerate(_runs(mask, dates)):
        fig.add_vrect(x0=a, x1=b + pd.Timedelta(days=1), fillcolor=color, opacity=opacity,
                      line_width=0, layer="below",
                      annotation_text=label if k == 0 else None, annotation_position="top left",
                      annotation_font=dict(size=11, color=INK_2))
    return fig


def time_series(df: pd.DataFrame, columns: list[str], title: str, ytitle: str,
                rolling: pd.DataFrame | None = None, rolling_suffix: str = "_mean",
                window_label: str = "", screened_mask: pd.Series | None = None,
                height: int = 380) -> go.Figure:
    """Raw values (faint) plus optional rolling means for measures sharing one unit."""
    fig = go.Figure()
    for col in columns:
        color = COLORS.get(col, "#2a78d6")
        name = label(col)
        fig.add_trace(go.Scatter(
            x=df["date"], y=df[col], mode="lines", name=name if rolling is None else f"{name} (daily)",
            line=dict(color=_rgba(color, 0.35 if rolling is not None else 1), width=1 if rolling is not None else 2),
            connectgaps=False, hovertemplate="%{y:,.1f}"))
        if rolling is not None and f"{col}{rolling_suffix}" in rolling:
            fig.add_trace(go.Scatter(
                x=rolling["date"], y=rolling[f"{col}{rolling_suffix}"], mode="lines",
                name=f"{name} ({window_label} mean)", line=dict(color=color, width=2),
                connectgaps=False, hovertemplate="%{y:,.1f}"))
    if screened_mask is not None and screened_mask.any():
        shade_runs(fig, screened_mask, df["date"], MUTED, "CBP values screened out")
    return style(fig, title, ytitle, height)


def ratio_chart(df: pd.DataFrame, ratio_col: str, rolling: pd.DataFrame | None, title: str,
                window_label: str = "", threshold: float | None = None,
                threshold_label: str = "") -> go.Figure:
    fig = time_series(df, [ratio_col], title, "% per reported day", rolling,
                      rolling_suffix="", window_label=window_label)
    if threshold is not None and np.isfinite(threshold):
        fig.add_hline(y=threshold, line=dict(color=STATUS["critical"], width=1.5, dash="dash"),
                      annotation_text=threshold_label, annotation_position="top right",
                      annotation_font=dict(color=INK_2, size=11))
    return fig


def period_labels(agg: pd.DataFrame) -> list[str]:
    """Tooltip text naming the whole period, so weekly and quarterly bars stay distinct."""
    if not isinstance(agg.index, pd.PeriodIndex):
        return [str(i) for i in agg.index]
    kind = agg.index.freqstr[0]
    if kind == "W":
        return [f"Week of {p.start_time:%d %b %Y}" for p in agg.index]
    if kind == "Q":
        return [f"{p.year} Q{p.quarter}" for p in agg.index]
    return [f"{p.start_time:%b %Y}" for p in agg.index]


def period_bars(agg: pd.DataFrame, col: str, title: str, ytitle: str,
                color_key: str | None = None) -> go.Figure:
    color = COLORS.get(color_key or col, "#2a78d6")
    x = agg["period_start"] if "period_start" in agg else agg.index.astype(str)
    fig = go.Figure(go.Bar(x=x, y=agg[col], marker=dict(color=color, cornerradius=4), name=title,
                           customdata=period_labels(agg), hovertemplate="%{customdata}: %{y:,.2f}<extra></extra>"))
    fig.update_layout(bargap=0.15)
    return style(fig, title, ytitle, hover="closest")


def change_bars(agg: pd.DataFrame, col: str, title: str, ytitle: str = "% change") -> go.Figure:
    """Diverging bars for signed changes (blue = decrease, red = increase)."""
    vals = agg[col]
    colors = np.where(vals >= 0, "#e34948", "#2a78d6")
    x = agg["period_start"] if "period_start" in agg else agg.index.astype(str)
    fig = go.Figure(go.Bar(x=x, y=vals, marker=dict(color=colors, cornerradius=4), showlegend=False,
                           customdata=period_labels(agg), hovertemplate="%{customdata}: %{y:+.1f}<extra></extra>"))
    fig.add_hline(y=0, line=dict(color=AXIS, width=1))
    return style(fig, title, ytitle, hover="closest")


def weekday_index_chart(results: dict[str, dict], title: str) -> go.Figure:
    """Median ratio of each weekday's value to the same week's Mon-Thu mean."""
    fig = go.Figure()
    days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday"]
    for metric, res in results.items():
        s = res["summary"].reindex(days)
        fig.add_trace(go.Bar(x=days, y=s["median"], name=label(metric),
                             marker=dict(color=COLORS.get(metric, "#2a78d6"), cornerradius=4),
                             customdata=s["weeks"], hovertemplate="%{x}: %{y:.2f} (n=%{customdata} weeks)<extra></extra>"))
    fig.add_hline(y=1, line=dict(color=INK_2, width=1), annotation_text="= midweek mean",
                  annotation_position="top right", annotation_font=dict(size=11, color=INK_2))
    fig.update_layout(barmode="group", bargap=0.25, bargroupgap=0.08)
    return style(fig, title, "relative to same-week Mon-Thu mean", hover="closest")


def box_by_group(df: pd.DataFrame, col: str, group: str, title: str, ytitle: str) -> go.Figure:
    fig = go.Figure()
    color = COLORS.get(col, "#2a78d6")
    for g, part in df.groupby(group, observed=True):
        fig.add_trace(go.Box(y=part[col], name=str(g), marker_color=color, line=dict(width=1.5),
                             boxpoints="outliers", showlegend=False))
    return style(fig, title, ytitle, hover="closest")


def correlation_heatmap(corr: pd.DataFrame, title: str) -> go.Figure:
    labels = [label(c) for c in corr.columns]
    fig = go.Figure(go.Heatmap(z=corr.to_numpy(), x=labels, y=labels, zmin=-1, zmax=1,
                               colorscale=DIVERGING, text=np.round(corr.to_numpy(), 2),
                               texttemplate="%{text}", hovertemplate="%{y} vs %{x}: %{z:.2f}<extra></extra>",
                               colorbar=dict(title="rho")))
    fig.update_yaxes(autorange="reversed", gridcolor=SURFACE)
    return style(fig, title, height=460, hover="closest")


def pipeline_stage_diagram(summary: dict[str, dict], title: str) -> go.Figure:
    """Stage diagram: stock boxes (opening -> closing) and flow arrows (period
    totals and mean per reported day). Deliberately not a Sankey: the counts are
    aggregate stocks and flows, not tracked cohorts of the same children."""
    fig = go.Figure()

    def fmt(v, digits=0):
        return "n/a" if v is None or not np.isfinite(v) else f"{v:,.{digits}f}"

    cbp, hhs = summary.get("CBP", {}), summary.get("HHS", {})
    boxes = [
        (0.0, "CBP intake", f"{fmt(cbp.get('entries_total'))} total<br>{fmt(cbp.get('entries_per_day'), 1)} per<br>reported day", COLORS["cbp_intake"]),
        (1.0, "CBP custody<br>(snapshot)", f"{fmt(cbp.get('opening'))} -> {fmt(cbp.get('closing'))}", COLORS["cbp_custody"]),
        (2.0, "Transfers out<br>of CBP", f"{fmt(cbp.get('exits_total'))} total<br>{fmt(cbp.get('exits_per_day'), 1)} per<br>reported day", COLORS["cbp_transfers_out"]),
        (3.0, "HHS care<br>(snapshot)", f"{fmt(hhs.get('opening'))} -> {fmt(hhs.get('closing'))}", COLORS["hhs_care"]),
        (4.0, "HHS<br>discharges", f"{fmt(hhs.get('exits_total'))} total<br>{fmt(hhs.get('exits_per_day'), 1)} per<br>reported day", COLORS["hhs_discharges"]),
    ]
    # Two-line titles keep neighbouring boxes from overlapping on narrow screens.
    for x, head, body, color in boxes:
        is_stock = "snapshot" in head
        fig.add_shape(type="rect", x0=x - 0.42, x1=x + 0.42, y0=0.2, y1=0.8,
                      line=dict(color=color, width=2), fillcolor=_rgba(color, 0.10 if is_stock else 0.04),
                      layer="below")
        fig.add_annotation(x=x, y=0.66, text=f"<b>{head}</b>", showarrow=False, font=dict(size=12, color=INK))
        fig.add_annotation(x=x, y=0.38, text=body, showarrow=False, font=dict(size=12, color=INK_2))
    for x in (0.0, 1.0, 2.0, 3.0):
        fig.add_annotation(x=x + 0.58, y=0.5, ax=x + 0.42, ay=0.5, xref="x", yref="y", axref="x", ayref="y",
                           showarrow=True, arrowhead=3, arrowwidth=1.5, arrowcolor=AXIS, text="")
    notes = []
    for stage in ("CBP", "HHS"):
        s = summary.get(stage, {})
        if not s:
            continue
        if np.isfinite(s.get("unexplained_change", np.nan)):
            notes.append(f"{stage}: reported change {s['net_change']:+,.0f}; flows on reported days imply "
                         f"{s['net_change'] - s['unexplained_change']:+,.0f}; unexplained {s['unexplained_change']:+,.0f}")
        else:
            notes.append(f"{stage}: reconciliation not computed ({s.get('records_incomplete', 0)} records with "
                         "missing or screened values)")
    fig.add_annotation(x=2.0, y=0.05, text="<br>".join(notes), showarrow=False, font=dict(size=11, color=MUTED))
    fig.update_xaxes(visible=False, range=[-0.5, 4.5])
    fig.update_yaxes(visible=False, range=[0, 0.9])
    return style(fig, title, height=300, hover=False)


def episode_chart(signals: pd.DataFrame, episodes: pd.DataFrame, stage: str, title: str) -> go.Figure:
    """Stage stock with detected pressure episodes shaded."""
    col = "cbp_stock" if stage == "CBP" else "hhs_stock"
    key = "cbp_custody" if stage == "CBP" else "hhs_care"
    fig = go.Figure(go.Scatter(x=signals["date"], y=signals[col], mode="lines",
                               name=label(key), line=dict(color=COLORS[key], width=2),
                               hovertemplate="%{y:,.0f}"))
    eps = episodes[episodes["stage"].eq(stage)] if len(episodes) else episodes
    for k, e in enumerate(eps.itertuples()):
        fig.add_vrect(x0=e.start, x1=e.end + pd.Timedelta(days=1), fillcolor=STATUS["serious"],
                      opacity=0.16, line_width=0, layer="below")
    if len(eps):
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name="Flagged episode (any rule)",
                                 marker=dict(symbol="square", size=12, color=_rgba(STATUS["serious"], 0.4))))
    return style(fig, title, "children (snapshot)")


def entry_exit_chart(rolling: pd.DataFrame, stage: str, window_label: str, title: str) -> go.Figure:
    """Mean entries vs mean exits per reported day over the trailing window."""
    entry, exit_ = ("cbp_intake", "cbp_transfers_out") if stage == "CBP" else ("cbp_transfers_out", "hhs_discharges")
    names = {"CBP": ("CBP intake", "Transfers out of CBP"), "HHS": ("Transfers into HHS (assumed)", "HHS discharges")}[stage]
    fig = go.Figure()
    for col, name in zip((entry, exit_), names):
        fig.add_trace(go.Scatter(x=rolling["date"], y=rolling[f"{col}_mean"], mode="lines", name=name,
                                 line=dict(color=COLORS[col], width=2), hovertemplate="%{y:,.1f}"))
    return style(fig, f"{title} ({window_label} mean per reported day)", "children per reported day")


def threshold_line_chart(signals: pd.DataFrame, col: str, title: str, ytitle: str,
                         threshold: float, threshold_label: str, flag_col: str | None = None,
                         color: str = "#2a78d6") -> go.Figure:
    fig = go.Figure(go.Scatter(x=signals["date"], y=signals[col], mode="lines", name=title,
                               line=dict(color=color, width=2), hovertemplate="%{y:,.2f}", showlegend=False))
    fig.add_hline(y=threshold, line=dict(color=STATUS["critical"], width=1.5, dash="dash"),
                  annotation_text=threshold_label, annotation_position="top left",
                  annotation_font=dict(color=INK_2, size=11))
    if flag_col and flag_col in signals:
        hit = signals[flag_col].fillna(False).astype(bool)
        fig.add_trace(go.Scatter(x=signals.loc[hit, "date"], y=signals.loc[hit, col], mode="markers",
                                 name="Rule met", marker=dict(color=STATUS["critical"], size=6, symbol="diamond"),
                                 hovertemplate="rule met: %{y:,.2f}<extra></extra>"))
        fig.update_layout(showlegend=True)
    return style(fig, title, ytitle)


def reconciliation_scatter(recon: pd.DataFrame, status: pd.Series, stage: str = "hhs") -> go.Figure:
    """One-day stock-flow residuals by date, split by duplicate status."""
    frame = recon.assign(status=status.to_numpy())
    frame = frame[frame["comparable"]]
    groups = [("not suspect", "Not suspect", "#2a78d6", "circle"),
              ("suspect_copy", "Suspected displaced CBP copy", STATUS["critical"], "x")]
    fig = go.Figure()
    for key, name, color, symbol in groups:
        part = frame[frame["status"].eq(key)] if key != "not suspect" else frame[~frame["status"].isin(["suspect_copy", "unresolved"])]
        fig.add_trace(go.Scatter(x=part["date"], y=part[f"{stage}_residual"], mode="markers", name=name,
                                 marker=dict(color=color, size=8, symbol=symbol, line=dict(width=1, color=SURFACE)),
                                 hovertemplate="%{x|%Y-%m-%d}: %{y:+,.0f}<extra>" + name + "</extra>"))
    fig.add_hline(y=0, line=dict(color=AXIS, width=1))
    label = "HHS care" if stage == "hhs" else "CBP custody"
    return style(fig, f"{label}: reported minus expected snapshot (one-day gaps)", "children", height=400,
                 hover="closest")


def duplicate_map(runs: pd.DataFrame) -> go.Figure:
    """Each duplicated run drawn from its copy dates (x) to its matched dates (y)."""
    fig = go.Figure()
    shown = set()
    for r in runs.itertuples():
        for (x0, x1, y0, y1, st) in ((r.a_start, r.a_end, r.b_start, r.b_end, r.a_status),
                                     (r.b_start, r.b_end, r.a_start, r.a_end, r.b_status)):
            color = STATUS["critical"] if st == "suspect_copy" else "#2a78d6"
            name = "Placement judged a displaced copy" if st == "suspect_copy" else "Placement judged likely original"
            fig.add_trace(go.Scatter(x=[x0, x1], y=[y0, y1], mode="lines+markers", name=name,
                                     line=dict(color=color, width=3), marker=dict(size=6),
                                     legendgroup=name, showlegend=name not in shown,
                                     hovertemplate=f"{r.length} records<br>%{{x|%Y-%m-%d}} matches %{{y|%Y-%m-%d}}<extra></extra>"))
            shown.add(name)
    return style(fig, "Duplicated CBP value runs: reported dates vs matching dates", "dates with identical CBP values",
                 height=420, hover="closest")
