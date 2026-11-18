"""UAC Care Pipeline Efficiency Analytics - Streamlit dashboard.

Run:  streamlit run app.py
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from config import (CBP_COLUMNS, DEFAULT_BOTTLENECK, DEFAULT_QUALITY, DEFAULT_ROLLING_DAYS, LABELS,
                    OFFICIAL_ANALYSIS_START, OFFICIAL_DATA_FILE, REFERENCE_DIR, ROLLING_WINDOW_OPTIONS,
                    BottleneckConfig, QualityConfig, resolve_data_file)
from src import visualizations as viz
from src.bottleneck_detection import BASE_RULES, COMPOSITE_RULES, detect_bottlenecks, filter_episodes
from src.data_loader import SchemaError, load_raw
from src.data_validation import duplicate_rca_facts
from src.kpi_calculations import KPI_DEFINITIONS, add_daily_metrics, aggregate, period_kpis, rolling_metrics
from src.pipeline_analysis import decompose_stock_change, reconcile_stock_flow, stage_summary
from src.preprocessing import PreparedData, prepare_dataset
from src.source_check import NOT_IN_REFERENCE, SourceCheck, compare_with_reference
from src import trend_analysis as ta

st.set_page_config(page_title="UAC Care Pipeline Analytics", page_icon=":material/insights:", layout="wide")

FREQ_OPTIONS = {"Daily": "D", "Weekly": "W", "Monthly": "M", "Quarterly": "Q"}
# Period used by tables and exports; daily views summarise by month.
PERIOD_NOUN = {"D": "month", "W": "week", "M": "month", "Q": "quarter"}
PLOT = {"theme": None, "config": {"displaylogo": False}}


# --------------------------------------------------------------------------
# Cached computation
# --------------------------------------------------------------------------
@st.cache_data(show_spinner="Loading and validating data...")
def load_prepared(file_bytes: bytes | None, path: str, quality: tuple, start: str | None = None) -> PreparedData:
    import io
    qcfg = QualityConfig(*quality)
    source = io.BytesIO(file_bytes) if file_bytes is not None else path
    if file_bytes is not None:
        source.name = "uploaded file"
    return prepare_dataset(load_raw(source), qcfg, start=start)


@st.cache_data(show_spinner="Computing metrics...")
def compute(_prepared: PreparedData, data_key: str, screened: bool, denominator: str,
            window: int, bconfig: tuple):
    """Metrics on the FULL history (filtering happens afterwards)."""
    bcfg = BottleneckConfig(*bconfig)
    daily = add_daily_metrics(_prepared.view(screened), denominator)
    rolling = rolling_metrics(daily, window, min_records=max(3, round(window * 5 / 7 * 0.6)))
    # Stagnation reference = pooled DSR over all history before each window (no look-ahead).
    result = detect_bottlenecks(daily, bcfg)
    return daily, rolling, result


@st.cache_data(show_spinner="Cross-checking against the published source...")
def source_check(_prepared: PreparedData, data_key: str, reference_path: str) -> tuple[SourceCheck, dict]:
    """Compare the records with a reference copy of the published dataset."""
    ref = prepare_dataset(load_raw(reference_path)).records
    facts = {"cbp_first": ref.loc[ref[CBP_COLUMNS].notna().any(axis=1), "date"].min(),
             "first": ref["date"].min(), "last": ref["date"].max()}
    return compare_with_reference(_prepared.records, ref, reference_name=Path(reference_path).name), facts


def fmt(v, digits: int = 0, suffix: str = "") -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)) or pd.isna(v):
        return "n/a"
    return f"{v:,.{digits}f}{suffix}"


def fmt_day(ts) -> str:
    return "n/a" if ts is None or pd.isna(ts) else f"{pd.Timestamp(ts):%Y-%m-%d}"


def p_text(p) -> str:
    """A p-value for a table; very small values do not fit a cell and add no information."""
    return "n/a" if pd.isna(p) else ("< 0.0001" if p < 0.0001 else f"{p:.4f}")


def between(frame: pd.DataFrame, start, end) -> pd.DataFrame:
    return frame[(frame["date"] >= pd.Timestamp(start)) & (frame["date"] <= pd.Timestamp(end))]


def download(df: pd.DataFrame, name: str, label: str, key: str) -> None:
    st.download_button(label, df.to_csv(index=False).encode("utf-8"), file_name=name,
                       mime="text/csv", key=key, icon=":material/download:")


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------
st.sidebar.title("Controls")
# label -> (path, analysis start)
DATASETS = {"Supplied file": (str(resolve_data_file()), None)}
if OFFICIAL_DATA_FILE.exists():
    DATASETS["Official HHS data (latest copy)"] = (str(OFFICIAL_DATA_FILE), OFFICIAL_ANALYSIS_START)
upload = st.sidebar.file_uploader("Data file (optional)", type=["csv"],
                                  help="Upload a CSV with the same six columns. It replaces the dataset "
                                       "chosen below.")
dataset = st.sidebar.radio(
    "Dataset", list(DATASETS), disabled=upload is not None,
    help=f"Supplied file: the project's input (to 2025-12-21). Official: `{OFFICIAL_DATA_FILE.name}`, a copy of "
         f"the published HHS dataset, analysed from {OFFICIAL_ANALYSIS_START} so both start on the same date.")
default_path, window_start = DATASETS[dataset]
file_bytes = upload.getvalue() if upload is not None else None
if file_bytes is not None:
    window_start = None

try:
    prepared = load_prepared(file_bytes, default_path, dataclasses.astuple(DEFAULT_QUALITY), window_start)
except (SchemaError, FileNotFoundError, ValueError) as exc:
    st.error(f"Could not load data: {exc}")
    st.stop()

data_key = hashlib.md5(file_bytes or f"{default_path}|{window_start}".encode()).hexdigest()
quality = prepared.quality
records = prepared.records

view_choice = st.sidebar.radio(
    "Data view", ["Quality-screened (recommended)", "As reported"],
    help="Screened view sets to missing the CBP values that repeat other dates' values and do not "
         "reconcile with HHS stock changes (see Data quality & RCA). Nothing is imputed.")
screened = view_choice.startswith("Quality")

min_d, max_d = records["date"].min().date(), records["date"].max().date()
picked = st.sidebar.date_input("Date range", value=(min_d, max_d), min_value=min_d, max_value=max_d)
start, end = (picked if isinstance(picked, (tuple, list)) and len(picked) == 2 else (min_d, max_d))

freq_label = st.sidebar.selectbox("Aggregation", list(FREQ_OPTIONS), index=2)
freq = FREQ_OPTIONS[freq_label]
window = st.sidebar.select_slider("Rolling window (days)", options=list(ROLLING_WINDOW_OPTIONS),
                                  value=DEFAULT_ROLLING_DAYS)
denominator = st.sidebar.radio("Ratio denominator", ["same_day", "previous_snapshot"],
                               format_func=lambda s: "Same-day snapshot" if s == "same_day"
                               else "Previous-day snapshot (1-day gaps only)")

with st.sidebar.expander("Alert thresholds"):
    d = DEFAULT_BOTTLENECK
    bcfg = BottleneckConfig(
        window_days=st.number_input("Detection window (days)", 7, 120, d.window_days),
        min_records=st.number_input("Min reported days in window", 3, 60, d.min_records),
        baseline_days=st.number_input("Baseline look-back (days)", 30, 365, d.baseline_days),
        min_episode_records=st.number_input("Min consecutive records per episode", 1, 30, d.min_episode_records),
        hhs_growth_pct=st.number_input("HHS growth threshold (%)", 0.5, 50.0, d.hhs_growth_pct, 0.5),
        cbp_growth_pct=st.number_input("CBP growth threshold (%)", 1.0, 200.0, d.cbp_growth_pct, 1.0),
        cbp_growth_min_abs=st.number_input("CBP growth minimum (children)", 0.0, 500.0, d.cbp_growth_min_abs, 1.0),
        imbalance_ratio=st.number_input("Entries/exits ratio threshold", 1.0, 3.0, d.imbalance_ratio, 0.05),
        ratio_decline_pct=st.number_input("Ratio decline vs baseline (%)", 5.0, 90.0, d.ratio_decline_pct, 5.0),
        stagnation_pct_of_reference=st.number_input("Stagnation: DSR below % of prior-history DSR", 5.0, 100.0,
                                                    d.stagnation_pct_of_reference, 5.0),
        anomaly_z=st.number_input("Anomaly robust z-score", 2.0, 10.0, d.anomaly_z, 0.5),
        backlog_requires_both=st.checkbox("Backlog pressure needs growth AND imbalance", d.backlog_requires_both),
    )
    st.caption("A value outside a field's allowed range is not applied; the previous value stays in use. "
               "Charts label the thresholds actually applied.")

daily, rolling, bres = compute(prepared, data_key, screened, denominator, window, dataclasses.astuple(bcfg))
sel = between(daily, start, end)
sel_roll = between(rolling, start, end)
sel_sig = between(bres.signals, start, end)
sel_eps = filter_episodes(bres.episodes, start, end)
window_label = f"{window}-day"

if sel.empty:
    st.warning(f"No records between {start} and {end}. Reports are rarely published for Fridays and "
               "Saturdays; widen the date range.")
    st.stop()

st.title("UAC Care Pipeline Efficiency Analytics")
st.caption(f"CBP custody -> HHS care -> discharge. Aggregate daily reports, {start} to {end}. "
           f"View: **{view_choice}**. All counts are reported figures; ratios are descriptive, not "
           "individual-level probabilities or processing times.")

cbp_hidden = int(sel["cbp_values_screened_out"].sum()) if screened else 0
if cbp_hidden:
    st.warning(f"CBP values are screened out for {cbp_hidden} of {len(sel)} records in this range "
               f"(suspected copies of other dates' values). CBP-based KPIs use the remaining "
               f"{len(sel) - cbp_hidden} records.", icon=":material/report:")

# Keyed navigation instead of st.tabs: st.tabs has no key in this Streamlit version, so the
# open tab could jump back to the first one when a sidebar control reran the app. Only the
# selected module is rendered, which also keeps reruns fast.
MODULES = ["Executive overview", "Care pipeline", "Transfer & discharge efficiency",
           "Bottleneck detection", "Outcome & trend analysis", "Data quality & RCA",
           "Data explorer & KPI definitions"]
module = st.radio("Module", MODULES, horizontal=True, key="module", label_visibility="collapsed")

# --------------------------------------------------------------------------
# 1. Executive overview
# --------------------------------------------------------------------------
if module == MODULES[0]:
    k = period_kpis(sel)
    span = (pd.Timestamp(end) - pd.Timestamp(start)).days + 1
    prev = between(daily, pd.Timestamp(start) - pd.Timedelta(days=span), pd.Timestamp(start) - pd.Timedelta(days=1))
    kp = period_kpis(prev) if len(prev) else {}

    def last_value(col):
        s = sel[["date", col]].dropna()
        return (s[col].iloc[-1], s["date"].iloc[-1]) if len(s) else (np.nan, None)

    st.subheader("Latest reported populations (snapshots)")
    c1, c2, c3, c4 = st.columns(4)
    v, dt = last_value("cbp_custody")
    c1.metric("Children in CBP custody", fmt(v), delta=fmt(k.get("cbp_custody_change"), 0) + " in range",
              delta_color="off", delta_arrow="off", border=True, help=f"Latest non-missing snapshot: {dt:%Y-%m-%d}" if dt else None)
    v, dt = last_value("hhs_care")
    monthly_hhs = aggregate(sel, "M")["hhs_care_closing"].dropna().tolist()
    c2.metric("Children in HHS care", fmt(v), delta=fmt(k.get("hhs_care_change"), 0) + " in range",
              delta_color="off", delta_arrow="off", border=True, chart_data=monthly_hhs or None,
              help=f"Latest snapshot: {dt:%Y-%m-%d}" if dt else None)
    # Coverage of the selected range (not first-to-last report), matching the date filter.
    c3.metric("Reporting coverage", fmt(k["records"] / span * 100, 0, "%"),
              delta=f"{k['records']} of {span} selected days", delta_color="off", delta_arrow="off", border=True)
    c4.metric("Data-quality issues", sum(i.severity == "critical" for i in quality.issues),
              delta="critical (see Data quality & RCA)", delta_color="off", delta_arrow="off", border=True)

    st.subheader("Selected-period totals (flows on reported days)")
    c1, c2, c3 = st.columns(3)
    for c, col, name in ((c1, "cbp_intake", "CBP intake"), (c2, "cbp_transfers_out", "Transfers out of CBP"),
                         (c3, "hhs_discharges", "HHS discharges")):
        c.metric(name, fmt(k.get(f"{col}_total")),
                 delta=f"{fmt(k.get(f'{col}_per_day'), 1)} per reported day ({k.get(f'{col}_n', 0)} days)",
                 delta_color="off", delta_arrow="off", border=True)
    st.caption("Totals cover reported days only and undercount calendar-period activity; compare periods "
               "using the per-reported-day means.")

    st.subheader("Key indicators (pooled over the selected period)")
    c1, c2, c3, c4 = st.columns(4)
    for c, key, name, unit, digits in (
        (c1, "transfer_to_stock_pct", "Transfer-to-stock ratio", "%", 1),
        (c2, "discharge_to_stock_pct", "Discharge-to-stock ratio", "%", 2),
        (c3, "throughput_system_pct", "System throughput (discharges / intake)", "%", 0),
        (c4, "stock_to_discharge_days", "Stock-to-discharge ratio", " days", 0),
    ):
        prior = kp.get(key, np.nan)
        delta = f"previous {span} days: {fmt(prior, digits, unit)}" if pd.notna(prior) else "no comparable prior period"
        c.metric(name, fmt(k.get(key), digits, unit), delta=delta, delta_color="off", delta_arrow="off", border=True)
    first_cbp = sel.loc[sel["cbp_custody"].notna(), "date"].min()
    st.caption("Each ratio uses only records where both of its components are present"
               + (f"; CBP-based ratios here start on {first_cbp:%Y-%m-%d}." if pd.notna(first_cbp)
                  else "; no CBP values are available in this range."))

    left, right = st.columns(2)
    with left:
        st.plotly_chart(viz.time_series(sel, ["hhs_care"], "Children in HHS care", "children (snapshot)", height=300), **PLOT)
    with right:
        st.plotly_chart(viz.time_series(sel, ["cbp_custody"], "Children in CBP custody", "children (snapshot)",
                                        screened_mask=sel["cbp_values_screened_out"] if screened else None,
                                        height=300), **PLOT)

    st.subheader("Most significant pressure periods in range")
    if sel_eps.empty:
        st.info("No rule-based pressure episodes overlap the selected range.")
    else:
        top = sel_eps.sort_values(["max_concurrent_rules", "calendar_days"], ascending=False).head(6)
        st.dataframe(top[["rule_name", "stage", "start", "end", "calendar_days", "stock_start", "stock_end",
                          "max_concurrent_rules", "detection_rule"]], hide_index=True,
                     column_config={"start": st.column_config.DateColumn(), "end": st.column_config.DateColumn()})

    st.subheader("Data-quality warnings")
    issues = quality.issues_frame()
    st.dataframe(issues[issues["severity"].isin(["critical", "warning"])][["severity", "check", "rows_affected", "finding"]],
                 hide_index=True)

# --------------------------------------------------------------------------
# 2. Care pipeline
# --------------------------------------------------------------------------
if module == MODULES[1]:
    summary = stage_summary(sel)
    st.plotly_chart(viz.pipeline_stage_diagram(summary, "Care pipeline: stages, stocks and flows in the selected period"), **PLOT)
    st.info("Shown as a stage diagram rather than a Sankey chart. Daily counts are aggregates: the children "
            "transferred on a day are not necessarily those taken into custody that day, so drawing them as "
            "one continuous flow would imply cohort tracking the data does not contain.", icon=":material/info:")
    table = pd.DataFrame(summary).T[["opening", "closing", "net_change", "entries_total", "exits_total",
                                     "entries_per_day", "exits_per_day", "expected_closing", "unexplained_change",
                                     "records_with_flows"]]
    table.index = ["CBP custody", "HHS care"]
    st.dataframe(table.astype(float).round(1))  # built from dicts -> object dtype, so cast before rounding
    st.caption(f"Stage totals and per-day means count only records where both the stage's entry and exit are "
               f"present (CBP {summary['CBP']['records_with_flows']:.0f} and HHS "
               f"{summary['HHS']['records_with_flows']:.0f} of {len(sel)} records), so they can be lower than the "
               "Executive overview totals, which count every reported value of each flow.")
    st.caption("Expected closing = opening snapshot + entries - exits on reported days after the first record. "
               "Unexplained change includes flows on unreported days, other entry/exit routes and timing "
               "differences. HHS entries are assumed to equal transfers out of CBP (not documented).")
    st.markdown("**Where does the stock change happen? One-day report pairs vs reporting gaps**")
    dec = decompose_stock_change(sel).drop(columns=["group"]).set_index("stage")
    st.dataframe(dec.round(1))
    st.caption("Across reporting gaps (mostly Thursday -> Sunday) flows are only reported for the final day, so "
               "the gap residual holds the unreported days' net movement. When it is large, reported-day flows "
               "cannot show whether a stage is accumulating; use the stock snapshots for direction.")
    left, right = st.columns(2)
    with left:
        st.plotly_chart(viz.time_series(sel, ["cbp_custody"], "Children in CBP custody", "children (snapshot)",
                                        rolling=sel_roll, window_label=window_label,
                                        screened_mask=sel["cbp_values_screened_out"] if screened else None), **PLOT)
    with right:
        st.plotly_chart(viz.time_series(sel, ["hhs_care"], "Children in HHS care", "children (snapshot)",
                                        rolling=sel_roll, window_label=window_label), **PLOT)
    st.plotly_chart(viz.time_series(sel, ["cbp_intake", "cbp_transfers_out", "hhs_discharges"],
                                    "Daily flows through the pipeline", "children per reported day",
                                    rolling=sel_roll, window_label=window_label,
                                    screened_mask=sel["cbp_values_screened_out"] if screened else None), **PLOT)
    st.plotly_chart(viz.time_series(sel, ["implied_hhs_admissions", "cbp_transfers_out"],
                                    "HHS admissions: implied from HHS stock change vs reported CBP transfers",
                                    "children per day"), **PLOT)
    st.caption("Implied admissions = change in HHS care + discharges (one-day gaps only). It is derived from "
               "HHS columns alone and is available where CBP values are screened out.")

# --------------------------------------------------------------------------
# 3. Transfer & discharge efficiency
# --------------------------------------------------------------------------
if module == MODULES[2]:
    metric = st.radio("Ratio", ["discharge_to_stock_pct", "transfer_to_stock_pct"], horizontal=True,
                      format_func=lambda m: "Transfer-to-stock (CBP)" if m.startswith("transfer")
                      else "Discharge-to-stock (HHS)")
    name = "Transfer-to-stock ratio" if metric.startswith("transfer") else "Discharge-to-stock ratio"
    if freq == "D":
        st.plotly_chart(viz.ratio_chart(sel, metric, sel_roll, f"{name}, daily with {window_label} pooled mean",
                                        window_label=window_label), **PLOT)
    else:
        agg = aggregate(sel, freq, history=daily)
        st.plotly_chart(viz.period_bars(agg, metric, f"{name}, pooled by {freq_label.lower()} period",
                                        "% per reported day"), **PLOT)
    s = sel[metric].dropna()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Pooled (period)", fmt(period_kpis(sel)[metric], 2, "%"), border=True)
    c2.metric("Median day", fmt(s.median(), 2, "%"), border=True)
    c3.metric("Std. deviation", fmt(s.std(), 2, " pp"), border=True)
    mm_digits = 0 if s.max() >= 10 else 1  # keeps the value short enough not to be truncated
    c4.metric("Min / max", f"{fmt(s.min(), mm_digits)} / {fmt(s.max(), mm_digits)}", border=True)
    c5.metric("Days with value", f"{s.size}", border=True)

    agg = aggregate(sel, "M" if freq == "D" else freq, history=daily)
    st.markdown(f"**Throughput and ratios by {PERIOD_NOUN[freq]}**")
    cols = ["records", "cbp_intake_per_day", "cbp_transfers_out_per_day", "hhs_discharges_per_day",
            "transfer_to_stock_pct", "discharge_to_stock_pct", "throughput_cbp_stage_pct",
            "throughput_hhs_stage_pct", "throughput_system_pct", "hhs_care_period_change"]
    show = agg[cols].copy()
    show.index = show.index.astype(str)
    st.dataframe(show.round(2), column_config={
        "throughput_cbp_stage_pct": st.column_config.NumberColumn("CBP stage % (transfers/intake)", format="%.0f"),
        "throughput_hhs_stage_pct": st.column_config.NumberColumn("HHS stage % (discharges/transfers)", format="%.0f"),
        "throughput_system_pct": st.column_config.NumberColumn("System % (discharges/intake)", format="%.0f"),
    })
    st.caption("Throughput above 100% means more exits than entries on reported days (stock drawn down); "
               "below 100% means entries exceed exits.")
    left, right = st.columns(2)
    with left:
        st.plotly_chart(viz.time_series(sel, ["cbp_transfers_out", "hhs_discharges"],
                                        "Flow volumes", "children per reported day", rolling=sel_roll,
                                        window_label=window_label), **PLOT)
    with right:
        st.plotly_chart(viz.time_series(sel, ["hhs_change_per_day"], "HHS care change between reports",
                                        "children per calendar day", rolling=sel_roll,
                                        window_label=window_label), **PLOT)

# --------------------------------------------------------------------------
# 4. Bottleneck detection
# --------------------------------------------------------------------------
if module == MODULES[3]:
    stage = st.radio("Stage", ["HHS", "CBP"], horizontal=True)
    rules_all = [r for r in BASE_RULES + COMPOSITE_RULES if bres.catalog[r]["stage"] == stage]
    chosen = st.multiselect("Rules shown", rules_all, default=rules_all,
                            format_func=lambda r: bres.catalog[r]["name"])
    eps = sel_eps[sel_eps["rule"].isin(chosen)] if len(sel_eps) else sel_eps
    st.plotly_chart(viz.episode_chart(sel_sig, eps, stage, f"{stage} population with flagged pressure episodes"), **PLOT)
    key = stage.lower()
    left, right = st.columns(2)
    with left:
        st.plotly_chart(viz.entry_exit_chart(sel_roll, stage, window_label, f"{stage} entries vs exits"), **PLOT)
    with right:
        st.plotly_chart(viz.threshold_line_chart(
            sel_sig, f"{key}_entry_exit_ratio", f"{stage} entries / exits over {bcfg.window_days} days",
            "ratio", bcfg.imbalance_ratio, f"threshold {bcfg.imbalance_ratio:g}",
            flag_col="HHS_INFLOW_EXCEEDS_EXITS" if stage == "HHS" else "CBP_INTAKE_EXCEEDS_TRANSFERS"), **PLOT)
    left, right = st.columns(2)
    growth_thr = bcfg.hhs_growth_pct if stage == "HHS" else bcfg.cbp_growth_pct
    with left:
        st.plotly_chart(viz.threshold_line_chart(
            sel_sig, f"{key}_pct_change_window", f"{stage} population change vs {bcfg.window_days} days earlier",
            "%", growth_thr, f"threshold {growth_thr:g}%", flag_col=f"{stage}_STOCK_GROWTH",
            color=viz.COLORS["hhs_care" if stage == "HHS" else "cbp_custody"]), **PLOT)
    with right:
        ratio_key = "dsr" if stage == "HHS" else "tsr"
        st.plotly_chart(viz.threshold_line_chart(
            sel_sig, f"{ratio_key}_vs_baseline_pct",
            f"{ratio_key.upper()} ({bcfg.window_days}-day pooled) as % of trailing baseline", "% of baseline",
            100 - bcfg.ratio_decline_pct, f"threshold {100 - bcfg.ratio_decline_pct:g}%",
            flag_col="DSR_DECLINE" if stage == "HHS" else "TSR_DECLINE"), **PLOT)
    if stage == "HHS":
        st.plotly_chart(viz.threshold_line_chart(
            sel_sig, "dsr_vs_reference_pct", "DSR as % of prior-history pooled DSR (stagnation check)", "% of prior-history DSR",
            bcfg.stagnation_pct_of_reference, f"threshold {bcfg.stagnation_pct_of_reference:g}%",
            flag_col="LOW_DISCHARGE_ACTIVITY", color=viz.COLORS["hhs_discharges"]), **PLOT)

    st.markdown("**Detected episodes**")
    if eps.empty:
        st.info("No episodes for the chosen rules in this range.")
    else:
        st.dataframe(eps, hide_index=True, column_config={
            "start": st.column_config.DateColumn(), "end": st.column_config.DateColumn(),
            "stock_change_pct": st.column_config.NumberColumn(format="%.1f"),
            "peak_metric": st.column_config.NumberColumn(format="%.2f"),
            "entries_per_day": st.column_config.NumberColumn(format="%.1f"),
            "exits_per_day": st.column_config.NumberColumn(format="%.1f"),
            "net_flow_per_day": st.column_config.NumberColumn(format="%.1f"),
        })
        download(eps, "bottleneck_episodes.csv", "Download episodes", "dl_eps")
    st.markdown("**Single-day anomalies** (robust z-score against the preceding window)")
    anomalies = between(bres.anomalies, start, end)
    st.dataframe(anomalies, hide_index=True, column_config={"date": st.column_config.DateColumn()})
    st.caption("Anomalies are statistical outliers relative to the preceding weeks. They are prompts for "
               "verification, not evidence of operational failure.")
    with st.expander("Rule definitions (active thresholds)"):
        st.dataframe(pd.DataFrame(bres.catalog).T[["name", "stage", "rule", "interpretation", "investigate"]])

# --------------------------------------------------------------------------
# 5. Outcome & trend analysis
# --------------------------------------------------------------------------
if module == MODULES[4]:
    monthly = ta.monthly_summary(sel)
    m_agg = aggregate(sel, "M", history=daily)
    left, right = st.columns(2)
    with left:
        st.plotly_chart(viz.period_bars(m_agg, "hhs_discharges_per_day", "Reported HHS discharges, mean per reported day",
                                        "children per reported day"), **PLOT)
    with right:
        st.plotly_chart(viz.change_bars(m_agg, "hhs_discharges_per_day_pct_change",
                                        "Month-over-month change in mean daily discharges"), **PLOT)
    left, right = st.columns(2)
    with left:
        st.plotly_chart(viz.period_bars(m_agg, "cbp_transfers_out_per_day", "Transfers out of CBP, mean per reported day",
                                        "children per reported day"), **PLOT)
    with right:
        st.plotly_chart(viz.period_bars(m_agg, "hhs_care_period_change", "Monthly change in children in HHS care",
                                        "children (closing vs previous closing)", color_key="hhs_care"), **PLOT)

    st.subheader("Weekday patterns")
    wk = {m: ta.weekday_relative_index(sel, m) for m in ("cbp_transfers_out", "hhs_discharges")}
    st.plotly_chart(viz.weekday_index_chart(wk, "Weekday activity relative to the same week's Mon-Thu mean"), **PLOT)
    wk_rows = [{"metric": LABELS.get(m, m), "Sundays compared": r["n_sundays"],
                "Sunday median index": round(r["sunday_median_index"], 3) if pd.notna(r["sunday_median_index"]) else None,
                "Wilcoxon p-value": p_text(r["p_value"]),
                "difference at 5%": "yes" if r["significant"] else "no"} for m, r in wk.items()]
    st.dataframe(pd.DataFrame(wk_rows), hide_index=True)
    st.caption("Reports cover Sunday-Thursday; Friday and Saturday are essentially never reported, so a true "
               "weekday-vs-weekend comparison is not possible. Sunday follows a two-day reporting gap and may "
               "reflect carry-over. Values are compared within each reporting week to remove level shifts.")

    st.subheader("Stability of discharge activity")
    left, right = st.columns(2)
    with left:
        st.plotly_chart(viz.time_series(sel_roll.rename(columns={"hhs_discharges_cv": "discharge_cv"}),
                                        ["discharge_cv"], f"Coefficient of variation of daily discharges ({window_label})",
                                        "std / mean"), **PLOT)
    with right:
        stab = ta.stability_summary(sel, "Q")
        stab.index = stab.index.astype(str)
        st.dataframe(stab.round(3))
        st.caption("CV is unstable when mean discharges are very small; interpret values from 2025 onwards with care.")

    st.subheader("Trend tests on monthly values")
    rows = []
    for col, name in (("hhs_discharges_per_day", "Mean daily discharges"),
                      ("discharge_to_stock_pct", "Discharge-to-stock ratio"),
                      ("cbp_transfers_out_per_day", "Mean daily transfers"),
                      ("transfer_to_stock_pct", "Transfer-to-stock ratio"),
                      ("hhs_care_closing", "HHS care (month-end)")):
        if col not in monthly:
            continue
        t = ta.trend_test(monthly[col])
        shift = ta.detect_level_shift(monthly[col])
        rows.append({"measure": name, "months": t["n"], "Kendall tau": t["tau"], "p-value": p_text(t["p_value"]),
                     "Theil-Sen slope / month": t["slope"], "direction (5%)": t["direction"],
                     "level shift from": str(shift.get("index")) if shift.get("index") is not None else "n/a",
                     "mean before": shift.get("mean_before"), "mean after": shift.get("mean_after")})
    st.dataframe(pd.DataFrame(rows).round(4), hide_index=True)
    st.caption("Kendall's tau without autocorrelation correction; with serially correlated monthly data, "
               "p-values overstate certainty. A level shift is the single best split of the monthly mean, "
               "a descriptive break, not a cause.")

    st.subheader("Compare two periods")
    c1, c2 = st.columns(2)
    mid = min_d + (max_d - min_d) / 2
    pa = c1.date_input("Period A", value=(min_d, mid), min_value=min_d, max_value=max_d, key="pa")
    pb = c2.date_input("Period B", value=(mid + datetime.timedelta(days=1), max_d), min_value=min_d, max_value=max_d, key="pb")
    if isinstance(pa, (tuple, list)) and len(pa) == 2 and isinstance(pb, (tuple, list)) and len(pb) == 2:
        comp = ta.compare_periods(daily, pa, pb)
        keep = ["records", "cbp_intake_per_day", "cbp_transfers_out_per_day", "hhs_discharges_per_day",
                "hhs_care_opening", "hhs_care_closing", "transfer_to_stock_pct", "discharge_to_stock_pct",
                "throughput_hhs_stage_pct", "throughput_system_pct", "stock_to_discharge_days", "discharge_cv"]
        st.dataframe(comp["kpis"].loc[[k for k in keep if k in comp["kpis"].index]].astype(float).round(2))
        st.dataframe(comp["tests"].assign(p_value=comp["tests"]["p_value"].map(p_text)), hide_index=True)
        st.caption("Mann-Whitney U test on daily values (two-sided). Daily values are autocorrelated, so treat "
                   "p-values as indicative.")

# --------------------------------------------------------------------------
# 6. Data quality & RCA
# --------------------------------------------------------------------------
if module == MODULES[5]:
    qs = quality.summary
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Records", qs["records"], delta=f"{qs['blank_rows_dropped']} blank rows dropped", delta_color="off", delta_arrow="off", border=True)
    c2.metric("Calendar coverage", fmt(qs["coverage_share"] * 100, 0, "%"),
              delta=f"{qs['missing_calendar_days']} days unreported", delta_color="off", delta_arrow="off", border=True)
    c3.metric("Suspected displaced CBP copies", qs["suspect_copy_records"],
              delta=f"longest duplicated run {qs['longest_duplicate_run']} records", delta_color="off", delta_arrow="off", border=True)
    c4.metric("Possible HHS carry-forward", qs["carry_forward_records"], border=True)

    st.subheader("Root-cause analysis: duplicated CBP value sequences")
    lo, hi = qs["attribution_robust_range"]
    hhs_tab = quality.tables["reconciliation_hhs_by_status"]
    rca = duplicate_rca_facts(records, quality.duplicates)
    rca_from = f"{rca['copy_start']:%Y-%m-%d}" if rca["copy_start"] is not None else "n/a"
    rca_to = f"{rca['copy_end']:%Y-%m-%d}" if rca["copy_end"] is not None else "n/a"
    spans = "; ".join(f"{a:%Y-%m-%d} to {b:%Y-%m-%d}" for a, b in rca["original_spans"]) or "no consistent placement"
    in_runs = qs["suspect_copy_records"] + qs["likely_original_records"] + qs["unresolved_records"]
    if not in_runs:
        st.success(f"No duplicated CBP value sequences (runs of 3+ identical consecutive reports) were found in "
                   f"this dataset. {qs.get('coincidental_records', 0)} isolated coincidental matches were kept.")
    else:
        st.markdown(
            f"""
**Symptom.** {qs['suspect_copy_records'] + qs['likely_original_records'] + qs['unresolved_records']} records sit in runs of 3+ consecutive
reports whose three CBP values (intake, custody, transfers) are *identical, in the same order*, to a run at other
dates. The longest run is {qs['longest_duplicate_run']} consecutive records. Coincidence at that length is not plausible.

**Test.** If a run of transfers genuinely belongs to its dates, it should agree with the independently reported
HHS stock movement: HHS care(t) ~ HHS care(t-1) + transfers(t) - discharges(t). For each matched pair of runs we
compare the median |HHS residual| at both placements.

**Result.** Placements judged *likely original* have median |residual| of at most {fmt(lo, 1)}; *displaced copies*
at least {fmt(hi, 1)}. Any threshold between those values gives the same labels (the applied threshold is
{fmt(qs['attribution_threshold'], 1)}, {qs['attribution_threshold_source']}). Across all one-day records, rows not under
suspicion reconcile with median |residual| {fmt(hhs_tab.loc['not suspect', 'median_abs'], 1) if 'not suspect' in hhs_tab.index else 'n/a'};
suspected copies {fmt(hhs_tab.loc['suspect_copy', 'median_abs'], 1) if 'suspect_copy' in hhs_tab.index else 'n/a'}.

**Conclusion.** {rca['copies']} of the {rca['records_in_copy_range']} reports between {rca_from} and {rca_to}
appear to carry CBP values copied from other periods; the matching values sit consistently at {spans}.
HHS columns show no such repetition. In the screened view these CBP values are set to missing; HHS values are kept.
Whether the duplication is in the published source is checked below.
"""
        )
    st.subheader("Source cross-check")
    # Compare with a published copy other than the selected file itself (the archived version sorts first).
    references = [r for r in sorted(REFERENCE_DIR.glob("*.csv")) if r.resolve() != Path(default_path).resolve()]
    if file_bytes is None and references:
        chk, ref = source_check(prepared, data_key, str(references[0]))
        added = chk.cells[chk.cells["status"].eq(NOT_IN_REFERENCE)]
        added_dates = added["date"].drop_duplicates()
        flagged = added["cbp_duplicate_status"].eq("suspect_copy").sum() if "cbp_duplicate_status" in added else 0
        other = chk.cells[~chk.cells.index.isin(added.index)]
        st.markdown(
            f"Compared with the published version `{chk.reference_name}` (healthdata.gov dataset ehpz-xc9n, "
            f"{fmt_day(ref['first'])} to {fmt_day(ref['last'])}; see `data/README.md`): **{len(added)} cells on "
            f"{len(added_dates)} dates have values in this file but are blank in the source**"
            + (f" ({added_dates.min():%Y-%m-%d} to {added_dates.max():%Y-%m-%d}); {flagged} of them are flagged "
               "as displaced copies by the check above" if len(added) else "")
            + f". The source's first CBP value is dated {fmt_day(ref['cbp_first'])}. Of the remaining {len(other)} "
              f"cells on common dates, {other['status'].eq('match').sum()} match the source exactly and "
              f"{other['status'].eq('both missing').sum()} are blank in both. Dates in this file but not in the "
              f"reference: {len(chk.dates_only_in_file)}; dates in the reference (within this file's range) but not "
              f"in this file: {len(chk.dates_only_in_reference)}.")
        st.dataframe(chk.summary.rename(index=LABELS))
        st.caption("Each value cell is compared with the source on the same date. 'Value not in reference': "
                   "the source cell is blank but this file has a value. Reproduce with "
                   "`python scripts/cross_check_source.py`.")
    else:
        st.info("No reference copy of the published dataset is available for this file, so whether any "
                "duplication is in the source cannot be determined from the file alone.")
    if in_runs:
        st.plotly_chart(viz.duplicate_map(quality.tables["duplicate_runs"]), **PLOT)
    recon = reconcile_stock_flow(records)
    st.plotly_chart(viz.reconciliation_scatter(recon, records["cbp_duplicate_status"], "hhs"), **PLOT)
    left, right = st.columns(2)
    with left:
        st.markdown("**HHS residual by duplicate status (one-day gaps)**")
        st.dataframe(quality.tables["reconciliation_hhs_by_status"].round(2))
        st.markdown("**HHS residual by gap length (non-suspect rows)**")
        st.dataframe(quality.tables["reconciliation_hhs_by_gap"].round(2))
        st.caption("Residuals across 2+ day gaps are systematically negative: unreported days had net "
                   "outflow, so missing days are not zero-activity days.")
    with right:
        st.markdown("**CBP residual by duplicate status (one-day gaps)**")
        st.dataframe(quality.tables["reconciliation_cbp_by_status"].round(2))
        st.caption("CBP custody does not reconcile with intake minus transfers even on clean consecutive days "
                   "(positive mean residual): other CBP entries/exits or snapshot timing are not documented.")
    st.markdown("**Matched runs**")
    st.dataframe(quality.tables["duplicate_runs"].drop(columns=["a_pos", "b_pos"]), hide_index=True)
    st.subheader("All checks")
    st.dataframe(quality.issues_frame(), hide_index=True)
    left, right = st.columns(2)
    with left:
        st.markdown("**Days since previous report**")
        st.dataframe(quality.tables["gap_distribution"])
    with right:
        st.markdown("**Reports by weekday**")
        st.dataframe(quality.tables["weekday_distribution"])
    st.markdown("**Coverage gaps**")
    st.dataframe(quality.tables["long_gaps"], hide_index=True)
    st.markdown("**Cleaning and transformation log**")
    st.dataframe(prepared.audit_frame(), hide_index=True)

# --------------------------------------------------------------------------
# 7. Data explorer & KPI definitions
# --------------------------------------------------------------------------
if module == MODULES[6]:
    st.subheader("Records")
    only_flagged = st.checkbox("Show only flagged records")
    base_cols = ["date", "weekday", "gap_days", "cbp_intake", "cbp_custody", "cbp_transfers_out", "hhs_care",
                 "hhs_discharges", "transfer_to_stock_pct", "discharge_to_stock_pct", "hhs_change",
                 "implied_hhs_admissions", "cbp_duplicate_status", "cbp_duplicate_run_length",
                 "cbp_duplicate_partner_dates", "hhs_possible_carry_forward", "cbp_values_screened_out",
                 "source_row"]
    table = sel[base_cols].copy()
    table["weekday"] = table["weekday"].astype(str)
    # Counts are whole numbers; nullable integers keep missing values missing in the export.
    for col in ("gap_days", "cbp_intake", "cbp_custody", "cbp_transfers_out", "hhs_care", "hhs_discharges",
                "hhs_change", "implied_hhs_admissions"):
        table[col] = table[col].round().astype("Int64")
    if only_flagged:
        table = table[(table["cbp_duplicate_status"] != "unique") | table["hhs_possible_carry_forward"]]
    st.dataframe(table, hide_index=True, column_config={"date": st.column_config.DateColumn()})
    c1, c2, c3 = st.columns(3)
    with c1:
        download(table, "uac_records_filtered.csv", "Filtered records + daily metrics", "dl_rec")
    with c2:
        agg_out = aggregate(sel, "M" if freq == "D" else freq, history=daily).reset_index()
        agg_out["period"] = agg_out["period"].astype(str)
        period_label = "Monthly" if freq == "D" else freq_label  # daily view exports monthly KPIs
        download(agg_out, f"uac_kpis_{period_label.lower()}.csv", f"{period_label} KPIs", "dl_agg")
    with c3:
        download(quality.issues_frame(), "uac_data_quality_report.csv", "Data-quality report", "dl_dq")
    st.caption("Source: HHS 'Unaccompanied Alien Children Program' public aggregate dataset. Check the "
               "publisher's terms before redistribution.")

    st.subheader("Missing values in the selected range")
    miss = pd.DataFrame({"reported view": records.pipe(between, start, end)[list(LABELS)[1:]].isna().sum(),
                         "current view": sel[list(LABELS)[1:]].isna().sum()})
    miss.index = [LABELS[c] for c in miss.index]
    st.dataframe(miss)

    st.subheader("KPI definitions")
    st.dataframe(pd.DataFrame(KPI_DEFINITIONS), hide_index=True)
