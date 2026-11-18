"""Transparent, rule-based detection of operational pressure periods.

Every rule is evaluated per record on a trailing calendar window, so a rule
only uses information available on that date. Rules return True / False, or
NA when the window has too few reported days. Consecutive True records form an
*episode*. Rules flag periods for review; they do not establish causes.

Run detection on the full history and filter afterwards: windows and
baselines then never start artificially at the edge of a user selection.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import DEFAULT_BOTTLENECK, BottleneckConfig
from src.kpi_calculations import add_daily_metrics, ratio_denominator, rolling_metrics, safe_divide

STAGE_COLUMNS = {
    "CBP": {"stock": "cbp_custody", "entry": "cbp_intake", "exit": "cbp_transfers_out"},
    "HHS": {"stock": "hhs_care", "entry": "cbp_transfers_out", "exit": "hhs_discharges"},
}


def rule_catalog(cfg: BottleneckConfig = DEFAULT_BOTTLENECK) -> dict[str, dict[str, str]]:
    """Human-readable rule definitions using the active thresholds."""
    w = cfg.window_days
    combine = "AND" if cfg.backlog_requires_both else "OR"
    return {
        "HHS_STOCK_GROWTH": {
            "name": "Sustained HHS care growth", "stage": "HHS", "metric": "hhs_pct_change_window",
            "rule": f"Children in HHS care >= {cfg.hhs_growth_pct:g}% above the snapshot {w} days earlier",
            "interpretation": "The reported HHS population is building up.",
            "investigate": "Compare with admissions, discharge processing and capacity records for the period.",
        },
        "HHS_INFLOW_EXCEEDS_EXITS": {
            "name": "HHS entries exceed discharges", "stage": "HHS", "metric": "hhs_entry_exit_ratio",
            "rule": f"Over {w} days, sum(transfers out of CBP) / sum(HHS discharges) >= {cfg.imbalance_ratio:g}",
            "interpretation": "On reported days, more children entered HHS than were discharged. Flows on "
                              "unreported days are missing, and HHS care can fall across reporting gaps, "
                              "so read this together with HHS_STOCK_GROWTH (see HHS_BACKLOG_PRESSURE).",
            "investigate": "Check discharge pipeline stages (sponsor assessment, approvals) for delays.",
        },
        "CBP_STOCK_GROWTH": {
            "name": "Sustained CBP custody growth", "stage": "CBP", "metric": "cbp_pct_change_window",
            "rule": f"Children in CBP custody >= {cfg.cbp_growth_pct:g}% and >= {cfg.cbp_growth_min_abs:g} "
                    f"children above the snapshot {w} days earlier",
            "interpretation": "The reported CBP custody population is building up.",
            "investigate": "Review intake surges and HHS placement availability for transfers.",
        },
        "CBP_INTAKE_EXCEEDS_TRANSFERS": {
            "name": "CBP intake exceeds transfers", "stage": "CBP", "metric": "cbp_entry_exit_ratio",
            "rule": f"Over {w} days, sum(CBP intake) / sum(transfers out of CBP) >= {cfg.imbalance_ratio:g}",
            "interpretation": "On reported days, more children entered CBP custody than were transferred out. "
                              "CBP stock-flow does not reconcile even on consecutive days, so treat as indicative.",
            "investigate": "Check transfer referral and placement-designation timing.",
        },
        "DSR_DECLINE": {
            "name": "Discharge-to-stock ratio decline", "stage": "HHS", "metric": "dsr_vs_baseline_pct",
            "rule": f"{w}-day pooled DSR <= {100 - cfg.ratio_decline_pct:g}% of the pooled DSR over "
                    f"the {cfg.baseline_days} days before the window",
            "interpretation": "Discharge activity relative to the population is weaker than its recent norm.",
            "investigate": "Check sponsor vetting, case-management workload and discharge definitions.",
        },
        "TSR_DECLINE": {
            "name": "Transfer-to-stock ratio decline", "stage": "CBP", "metric": "tsr_vs_baseline_pct",
            "rule": f"{w}-day pooled TSR <= {100 - cfg.ratio_decline_pct:g}% of the pooled TSR over "
                    f"the {cfg.baseline_days} days before the window",
            "interpretation": "Transfers relative to custody are weaker than their recent norm.",
            "investigate": "Check HHS bed availability and transfer logistics.",
        },
        "LOW_DISCHARGE_ACTIVITY": {
            "name": "Low discharge activity (stagnation)", "stage": "HHS", "metric": "dsr_vs_reference_pct",
            "rule": f"{w}-day pooled DSR < {cfg.stagnation_pct_of_reference:g}% of the long-run pooled DSR "
                    "(all reports before the window; no look-ahead)",
            "interpretation": "Few discharges relative to the population compared with the long-run norm.",
            "investigate": "Establish whether discharge policy, definitions or processing changed.",
        },
        "HHS_BACKLOG_PRESSURE": {
            "name": "HHS backlog pressure", "stage": "HHS", "metric": "hhs_pct_change_window",
            "rule": f"HHS_STOCK_GROWTH {combine} HHS_INFLOW_EXCEEDS_EXITS",
            "interpretation": "Population growth coinciding with entries outpacing discharges.",
            "investigate": "Prioritise for operational review.",
        },
        "CBP_BACKLOG_PRESSURE": {
            "name": "CBP backlog pressure", "stage": "CBP", "metric": "cbp_pct_change_window",
            "rule": f"CBP_STOCK_GROWTH {combine} CBP_INTAKE_EXCEEDS_TRANSFERS",
            "interpretation": "Custody growth coinciding with intake outpacing transfers.",
            "investigate": "Prioritise for operational review.",
        },
    }


BASE_RULES = ["HHS_STOCK_GROWTH", "HHS_INFLOW_EXCEEDS_EXITS", "CBP_STOCK_GROWTH",
              "CBP_INTAKE_EXCEEDS_TRANSFERS", "DSR_DECLINE", "TSR_DECLINE", "LOW_DISCHARGE_ACTIVITY"]
COMPOSITE_RULES = ["HHS_BACKLOG_PRESSURE", "CBP_BACKLOG_PRESSURE"]

EPISODE_COLUMNS = ["rule", "rule_name", "stage", "start", "end", "records", "calendar_days",
                   "stock_start", "stock_end", "stock_change", "stock_change_pct",
                   "entries_per_day", "exits_per_day", "net_flow_per_day", "peak_metric",
                   "max_concurrent_rules", "detection_rule", "interpretation", "investigate",
                   "limitations"]


@dataclass
class BottleneckResult:
    signals: pd.DataFrame      # per-record metrics and rule flags
    episodes: pd.DataFrame     # one row per episode
    anomalies: pd.DataFrame    # single-day robust z-score anomalies
    catalog: dict[str, dict[str, str]]


def value_days_earlier(dates: pd.Series, values: pd.Series, days: int,
                       tolerance_days: int = 7) -> pd.Series:
    """Value of ``values`` on the last report at or before date - ``days``.

    NaN if that report is more than ``tolerance_days`` before the target date.
    """
    left = pd.DataFrame({"target": dates - pd.Timedelta(days=days), "_order": np.arange(len(dates))})
    right = pd.DataFrame({"target": dates, "value": values}).dropna()
    merged = pd.merge_asof(left.sort_values("target"), right.sort_values("target"), on="target",
                           direction="backward", tolerance=pd.Timedelta(days=tolerance_days))
    return pd.Series(merged.sort_values("_order")["value"].to_numpy(), index=dates.index)


def _pooled_before_window(dates: pd.Series, num: pd.Series, den: pd.Series, cfg: BottleneckConfig,
                          lookback_days: int | None) -> pd.Series:
    """Pooled ratio (x100) over the period that ends where the trailing window
    starts: the last ``lookback_days`` before it, or all earlier history if None.
    Uses only information available before each date (no look-ahead)."""
    both = num.notna() & den.notna()
    n = pd.Series(num.where(both).to_numpy(dtype=float), index=pd.DatetimeIndex(dates))
    d = pd.Series(den.where(both).to_numpy(dtype=float), index=pd.DatetimeIndex(dates))
    if lookback_days is None:
        sums_n, sums_d, count = n.expanding().sum(), d.expanding().sum(), n.expanding().count()
        need = cfg.min_records * 3
    else:
        w = f"{lookback_days}D"
        sums_n, sums_d, count = n.rolling(w).sum(), d.rolling(w).sum(), n.rolling(w).count()
        need = cfg.min_records * 2
    ratio = (safe_divide(sums_n, sums_d) * 100).where(count >= need)
    return value_days_earlier(dates, pd.Series(ratio.to_numpy(), index=dates.index),
                              cfg.window_days, tolerance_days=7)


def _flag(condition: pd.Series, available: pd.Series) -> pd.Series:
    """Boolean flag with NA where inputs were insufficient."""
    out = condition.astype("boolean")
    out[~available.fillna(False).astype(bool)] = pd.NA
    return out


def robust_zscores(dates: pd.Series, values: pd.Series, window_days: int,
                   min_records: int) -> pd.DataFrame:
    """Robust z-score of each value against the *preceding* window (median/MAD)."""
    series = pd.Series(values.to_numpy(dtype=float), index=pd.DatetimeIndex(dates))
    roll = series.rolling(f"{window_days}D", closed="left")
    median = roll.median()
    mad = roll.apply(lambda a: np.nanmedian(np.abs(a - np.nanmedian(a))) if np.isfinite(a).any() else np.nan,
                     raw=True)
    count = roll.count()
    scale = 1.4826 * mad
    z = (series - median) / scale.where(scale > 0)
    z = z.where(count >= min_records)
    return pd.DataFrame({"median": median.to_numpy(), "z": z.to_numpy()}, index=values.index)


def compute_signals(df: pd.DataFrame, cfg: BottleneckConfig = DEFAULT_BOTTLENECK,
                    reference_dsr: float | None = None) -> pd.DataFrame:
    """Per-record window metrics and rule flags."""
    data = df if "transfer_to_stock_pct" in df else add_daily_metrics(df)
    roll = rolling_metrics(data, cfg.window_days, cfg.min_records)
    dates = data["date"]
    s = pd.DataFrame({"date": dates}, index=data.index)

    for stage, cols in STAGE_COLUMNS.items():
        key = stage.lower()
        earlier = value_days_earlier(dates, data[cols["stock"]], cfg.window_days)
        s[f"{key}_stock"] = data[cols["stock"]]
        s[f"{key}_stock_window_start"] = earlier
        s[f"{key}_abs_change_window"] = data[cols["stock"]] - earlier
        s[f"{key}_pct_change_window"] = safe_divide(s[f"{key}_abs_change_window"], earlier) * 100
        s[f"{key}_entry_exit_ratio"] = roll[f"{key}_entry_exit_ratio"]

    s["dsr_window"] = roll["discharge_to_stock_pct"]
    s["tsr_window"] = roll["transfer_to_stock_pct"]
    hhs_num, hhs_den = data["hhs_discharges"], data[ratio_denominator(data, "hhs")]
    cbp_num, cbp_den = data["cbp_transfers_out"], data[ratio_denominator(data, "cbp")]
    s["dsr_baseline"] = _pooled_before_window(dates, hhs_num, hhs_den, cfg, cfg.baseline_days)
    s["tsr_baseline"] = _pooled_before_window(dates, cbp_num, cbp_den, cfg, cfg.baseline_days)
    s["dsr_vs_baseline_pct"] = safe_divide(s["dsr_window"], s["dsr_baseline"]) * 100
    s["tsr_vs_baseline_pct"] = safe_divide(s["tsr_window"], s["tsr_baseline"]) * 100
    # Long-run reference: pooled DSR over all history before the window (or a
    # fixed value if supplied, e.g. for scenario checks).
    s["dsr_reference"] = (_pooled_before_window(dates, hhs_num, hhs_den, cfg, None)
                          if reference_dsr is None else float(reference_dsr))
    s["dsr_vs_reference_pct"] = safe_divide(s["dsr_window"], s["dsr_reference"]) * 100
    s["discharge_cv_window"] = roll["hhs_discharges_cv"]

    keep = 1 - cfg.ratio_decline_pct / 100
    s["HHS_STOCK_GROWTH"] = _flag(s["hhs_pct_change_window"] >= cfg.hhs_growth_pct,
                                  s["hhs_pct_change_window"].notna())
    s["HHS_INFLOW_EXCEEDS_EXITS"] = _flag(s["hhs_entry_exit_ratio"] >= cfg.imbalance_ratio,
                                          s["hhs_entry_exit_ratio"].notna())
    s["CBP_STOCK_GROWTH"] = _flag((s["cbp_pct_change_window"] >= cfg.cbp_growth_pct)
                                  & (s["cbp_abs_change_window"] >= cfg.cbp_growth_min_abs),
                                  s["cbp_pct_change_window"].notna())
    s["CBP_INTAKE_EXCEEDS_TRANSFERS"] = _flag(s["cbp_entry_exit_ratio"] >= cfg.imbalance_ratio,
                                              s["cbp_entry_exit_ratio"].notna())
    s["DSR_DECLINE"] = _flag(s["dsr_window"] <= keep * s["dsr_baseline"],
                             s["dsr_window"].notna() & s["dsr_baseline"].notna())
    s["TSR_DECLINE"] = _flag(s["tsr_window"] <= keep * s["tsr_baseline"],
                             s["tsr_window"].notna() & s["tsr_baseline"].notna())
    s["LOW_DISCHARGE_ACTIVITY"] = _flag(s["dsr_vs_reference_pct"] < cfg.stagnation_pct_of_reference,
                                        s["dsr_vs_reference_pct"].notna())
    for stage, (growth, imbalance) in {"HHS": ("HHS_STOCK_GROWTH", "HHS_INFLOW_EXCEEDS_EXITS"),
                                       "CBP": ("CBP_STOCK_GROWTH", "CBP_INTAKE_EXCEEDS_TRANSFERS")}.items():
        # Kleene logic: NA & False = False, NA | True = True
        combined = (s[growth] & s[imbalance]) if cfg.backlog_requires_both else (s[growth] | s[imbalance])
        s[f"{stage}_BACKLOG_PRESSURE"] = combined
    s["active_rules"] = s[BASE_RULES].fillna(False).astype(int).sum(axis=1)
    return s


def extract_episodes(signals: pd.DataFrame, data: pd.DataFrame, rule: str,
                     cfg: BottleneckConfig, catalog: dict) -> list[dict]:
    """Group consecutive flagged records (NA breaks a run) into episodes."""
    flag = signals[rule].fillna(False).astype(bool).to_numpy()
    meta = catalog[rule]
    cols = STAGE_COLUMNS[meta["stage"]]
    episodes, i, n = [], 0, len(flag)
    while i < n:
        if not flag[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and flag[j + 1]:
            j += 1
        if j - i + 1 >= cfg.min_episode_records:
            part = data.iloc[i:j + 1]
            sig = signals.iloc[i:j + 1]
            stock = part[cols["stock"]].dropna()
            start_val = stock.iloc[0] if len(stock) else np.nan
            end_val = stock.iloc[-1] if len(stock) else np.nan
            aligned = part[[cols["entry"], cols["exit"]]].dropna()
            entries = aligned[cols["entry"]].mean() if len(aligned) else np.nan
            exits = aligned[cols["exit"]].mean() if len(aligned) else np.nan
            metric = sig[meta["metric"]]
            peak = metric.min() if "vs_" in meta["metric"] else metric.max()
            episodes.append({
                "rule": rule, "rule_name": meta["name"], "stage": meta["stage"],
                "start": part["date"].iloc[0], "end": part["date"].iloc[-1], "records": j - i + 1,
                "calendar_days": (part["date"].iloc[-1] - part["date"].iloc[0]).days + 1,
                "stock_start": start_val, "stock_end": end_val, "stock_change": end_val - start_val,
                "stock_change_pct": safe_divide(end_val - start_val, start_val) * 100,
                "entries_per_day": entries, "exits_per_day": exits, "net_flow_per_day": entries - exits,
                "peak_metric": peak, "max_concurrent_rules": int(sig["active_rules"].max()),
                "detection_rule": meta["rule"], "interpretation": meta["interpretation"],
                "investigate": meta["investigate"],
                "limitations": "Aggregate counts on reported days only; flags pressure, not cause or "
                               "individual waiting time.",
            })
        i = j + 1
    return episodes


def detect_bottlenecks(df: pd.DataFrame, cfg: BottleneckConfig = DEFAULT_BOTTLENECK,
                       reference_dsr: float | None = None) -> BottleneckResult:
    """Signals, episodes and anomalies for the supplied records."""
    data = df if "transfer_to_stock_pct" in df else add_daily_metrics(df)
    catalog = rule_catalog(cfg)
    signals = compute_signals(data, cfg, reference_dsr)
    rows: list[dict] = []
    for rule in BASE_RULES + COMPOSITE_RULES:
        rows.extend(extract_episodes(signals, data, rule, cfg, catalog))
    episodes = pd.DataFrame(rows, columns=EPISODE_COLUMNS)
    if len(episodes):
        episodes = episodes.sort_values(["start", "rule"]).reset_index(drop=True)

    anomaly_rows = []
    for col, label in (("hhs_discharges", "HHS discharges"), ("cbp_transfers_out", "CBP transfers"),
                       ("cbp_intake", "CBP intake")):
        z = robust_zscores(data["date"], data[col], cfg.window_days, cfg.min_records)
        hit = z["z"].abs() >= cfg.anomaly_z
        for idx in z.index[hit.fillna(False)]:
            anomaly_rows.append({"date": data.at[idx, "date"], "metric": label,
                                 "value": data.at[idx, col], "trailing_median": z.at[idx, "median"],
                                 "robust_z": z.at[idx, "z"],
                                 "direction": "high" if z.at[idx, "z"] > 0 else "low"})
    anomalies = pd.DataFrame(anomaly_rows, columns=["date", "metric", "value", "trailing_median",
                                                    "robust_z", "direction"])
    return BottleneckResult(signals=signals, episodes=episodes, anomalies=anomalies, catalog=catalog)


def filter_episodes(episodes: pd.DataFrame, start, end) -> pd.DataFrame:
    """Episodes overlapping [start, end]."""
    if episodes.empty:
        return episodes
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    return episodes[(episodes["end"] >= start) & (episodes["start"] <= end)].reset_index(drop=True)
