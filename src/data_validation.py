"""Data-quality checks, including detection of duplicated value sequences.

The checks never modify data. They return a ``QualityReport`` whose flags are
attached to records by ``preprocessing`` and used to build the screened view.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config import CBP_COLUMNS, LABELS, VALUE_COLUMNS, QualityConfig
from src.pipeline_analysis import gap_category, reconcile_stock_flow, summarize_residuals

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}

# Row-level status values for the duplicated-sequence check
UNIQUE = "unique"
COINCIDENTAL = "coincidental_match"
LIKELY_ORIGINAL = "likely_original"
SUSPECT_COPY = "suspect_copy"
UNRESOLVED = "unresolved"


@dataclass
class Issue:
    check: str
    severity: str
    message: str
    n_rows: int = 0
    examples: list[str] = field(default_factory=list)


@dataclass
class DuplicateAnalysis:
    runs: pd.DataFrame          # one row per matched pair of runs
    row_status: pd.Series       # status per record (index aligned with records)
    run_length: pd.Series       # longest duplicated run containing the record
    partner_dates: pd.Series    # matched dates, as text
    threshold: float
    threshold_source: str
    robust_range: tuple[float, float]   # thresholds giving identical labels


@dataclass
class QualityReport:
    issues: list[Issue]
    summary: dict
    tables: dict[str, pd.DataFrame]
    duplicates: DuplicateAnalysis
    carry_forward: pd.Series        # bool per record (HHS values repeated)
    cbp_carry_forward: pd.Series    # bool per record (CBP values repeated)

    def issues_frame(self) -> pd.DataFrame:
        rows = [{"severity": i.severity, "check": i.check, "rows_affected": i.n_rows,
                 "finding": i.message, "examples": ", ".join(i.examples[:6])}
                for i in self.issues]
        frame = pd.DataFrame(rows, columns=["severity", "check", "rows_affected",
                                            "finding", "examples"])
        return frame.sort_values("severity", key=lambda s: s.map(SEVERITY_ORDER),
                                 kind="stable").reset_index(drop=True)


def _fmt_dates(dates: pd.Series, limit: int = 8) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.to_datetime(dates).head(limit)]


# --------------------------------------------------------------------------
# Duplicated sequences
# --------------------------------------------------------------------------
def find_duplicated_runs(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """All maximal runs of consecutive records whose ``columns`` values exactly
    match another run of consecutive records (same order) elsewhere.

    Records are matched on the full tuple of values; records with any missing
    value in ``columns`` are ignored. Positions refer to ``df`` row order,
    which must be chronological.
    """
    values = df[columns]
    valid = values.notna().all(axis=1).to_numpy()
    keys = [tuple(row) for row in values.to_numpy()]
    positions: dict[tuple, list[int]] = defaultdict(list)
    for i, key in enumerate(keys):
        if valid[i]:
            positions[key].append(i)

    pairs = {(a, b) for idx in positions.values() if len(idx) > 1
             for ai, a in enumerate(idx) for b in idx[ai + 1:]}
    runs, seen = [], set()
    for a, b in sorted(pairs):
        if (a, b) in seen:
            continue
        k = 0
        # A run may not overlap its own copy (a + k < b): a constant stretch of
        # identical consecutive records is carry-forward, not a displaced copy.
        while (a + k, b + k) in pairs and a + k < b:
            seen.add((a + k, b + k))
            k += 1
        runs.append((a, b, k))
    return pd.DataFrame(runs, columns=["a_pos", "b_pos", "length"])


def analyze_duplicates(df: pd.DataFrame, qcfg: QualityConfig,
                       columns: list[str] | None = None) -> DuplicateAnalysis:
    """Detect duplicated CBP sequences and attribute original vs copy.

    Attribution uses an independent check: at its original dates a run of
    transfers should agree with the HHS stock movement, so the HHS one-day
    stock-flow residual is small; at displaced dates it is not.
    """
    columns = columns or CBP_COLUMNS
    n = len(df)
    runs = find_duplicated_runs(df, columns)
    recon = reconcile_stock_flow(df)
    resid_abs = recon["hhs_residual"].abs().where(recon["comparable"]).to_numpy()

    status = np.array([UNIQUE] * n, dtype=object)
    run_length = np.zeros(n, dtype=int)
    partners: dict[int, set[str]] = defaultdict(set)
    dates = df["date"].dt.strftime("%Y-%m-%d").to_numpy()

    long_runs = runs[runs["length"] >= qcfg.duplicate_min_run].copy()
    in_long = np.zeros(n, dtype=bool)
    for r in runs.itertuples():
        for k in range(r.length):
            a, b = r.a_pos + k, r.b_pos + k
            partners[a].add(dates[b])
            partners[b].add(dates[a])
            for p in (a, b):
                run_length[p] = max(run_length[p], r.length)
                if r.length >= qcfg.duplicate_min_run:
                    in_long[p] = True
                elif status[p] == UNIQUE:
                    status[p] = COINCIDENTAL

    baseline = resid_abs[~in_long & ~np.isnan(resid_abs)]
    if qcfg.attribution_threshold is not None:
        threshold, source = float(qcfg.attribution_threshold), "configured"
    elif baseline.size >= 20:
        threshold = float(np.quantile(baseline, qcfg.attribution_quantile))
        source = (f"P{qcfg.attribution_quantile * 100:.0f} of |HHS residual| on "
                  f"{baseline.size} non-duplicated one-day records")
    else:
        threshold, source = 50.0, "fallback default (too few non-duplicated records)"

    rank = {LIKELY_ORIGINAL: 1, UNRESOLVED: 2, SUSPECT_COPY: 3}
    side_labels_a, side_labels_b, med_a, med_b = [], [], [], []
    for r in long_runs.itertuples():
        ma = np.nanmedian(resid_abs[r.a_pos:r.a_pos + r.length]) \
            if np.isfinite(resid_abs[r.a_pos:r.a_pos + r.length]).any() else np.nan
        mb = np.nanmedian(resid_abs[r.b_pos:r.b_pos + r.length]) \
            if np.isfinite(resid_abs[r.b_pos:r.b_pos + r.length]).any() else np.nan
        la, lb = _attribute(ma, mb, threshold)
        med_a.append(ma)
        med_b.append(mb)
        side_labels_a.append(la)
        side_labels_b.append(lb)
        for start, label in ((r.a_pos, la), (r.b_pos, lb)):
            for p in range(start, start + r.length):
                current = status[p]
                if current in rank and rank[current] >= rank[label]:
                    continue
                status[p] = label

    long_runs["a_start"] = df["date"].iloc[long_runs["a_pos"]].to_numpy()
    long_runs["a_end"] = df["date"].iloc[long_runs["a_pos"] + long_runs["length"] - 1].to_numpy()
    long_runs["b_start"] = df["date"].iloc[long_runs["b_pos"]].to_numpy()
    long_runs["b_end"] = df["date"].iloc[long_runs["b_pos"] + long_runs["length"] - 1].to_numpy()
    long_runs["a_median_abs_hhs_residual"] = med_a
    long_runs["b_median_abs_hhs_residual"] = med_b
    long_runs["a_status"] = side_labels_a
    long_runs["b_status"] = side_labels_b
    long_runs = long_runs.sort_values("length", ascending=False).reset_index(drop=True)

    # Labels depend only on which run medians are <= threshold, so every
    # threshold in [largest median <= threshold, smallest median above it)
    # yields identical labels.
    finite = [m for m in med_a + med_b if np.isfinite(m)]
    below = [m for m in finite if m <= threshold]
    above = [m for m in finite if m > threshold]
    robust = (max(below) if below else np.nan, min(above) if above else np.nan)

    index = df.index
    return DuplicateAnalysis(
        runs=long_runs,
        row_status=pd.Series(status, index=index, name="cbp_duplicate_status"),
        run_length=pd.Series(run_length, index=index, name="cbp_duplicate_run_length"),
        partner_dates=pd.Series([", ".join(sorted(partners[i])) for i in range(n)],
                                index=index, name="cbp_duplicate_partner_dates"),
        threshold=threshold, threshold_source=source, robust_range=robust,
    )


def _attribute(ma: float, mb: float, threshold: float) -> tuple[str, str]:
    """Label both placements of a duplicated run from their HHS residuals."""
    fa, fb = np.isfinite(ma), np.isfinite(mb)
    if fa and fb:
        a_ok, b_ok = ma <= threshold, mb <= threshold
        if a_ok and not b_ok:
            return LIKELY_ORIGINAL, SUSPECT_COPY
        if b_ok and not a_ok:
            return SUSPECT_COPY, LIKELY_ORIGINAL
        if not a_ok and not b_ok:
            return SUSPECT_COPY, SUSPECT_COPY
        return UNRESOLVED, UNRESOLVED
    if fa:  # identical runs cannot be genuine at both dates
        return (LIKELY_ORIGINAL, SUSPECT_COPY) if ma <= threshold else (SUSPECT_COPY, UNRESOLVED)
    if fb:
        return (SUSPECT_COPY, LIKELY_ORIGINAL) if mb <= threshold else (UNRESOLVED, SUSPECT_COPY)
    return UNRESOLVED, UNRESOLVED


def duplicate_rca_facts(records: pd.DataFrame, dup: DuplicateAnalysis) -> dict:
    """Plain facts for narrating the duplicated-sequence finding."""
    copies = records.loc[dup.row_status.eq(SUSPECT_COPY), "date"]
    facts = {"copies": int(copies.size), "copy_start": None, "copy_end": None,
             "records_in_copy_range": 0, "original_spans": []}
    if copies.empty:
        return facts
    lo, hi = copies.min(), copies.max()
    facts.update(copy_start=lo, copy_end=hi,
                 records_in_copy_range=int(records["date"].between(lo, hi).sum()))
    spans = []
    for r in dup.runs.itertuples():
        if r.a_status == LIKELY_ORIGINAL:
            spans.append((r.a_start, r.a_end))
        if r.b_status == LIKELY_ORIGINAL:
            spans.append((r.b_start, r.b_end))
    merged: list[list] = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1] + pd.Timedelta(days=7):
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    facts["original_spans"] = [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in merged]
    return facts


def detect_carry_forward(df: pd.DataFrame, columns=("hhs_care", "hhs_discharges"),
                         name: str = "hhs_possible_carry_forward") -> pd.Series:
    """Later record of adjacent reports with identical values in all ``columns``."""
    same = pd.Series(True, index=df.index)
    for col in columns:
        same &= df[col].eq(df[col].shift(1))
    return same.fillna(False).astype(bool).rename(name)


def iqr_outliers(series: pd.Series, k: float = 3.0) -> pd.Series:
    q1, q3 = series.quantile([0.25, 0.75])
    iqr = q3 - q1
    return (series < q1 - k * iqr) | (series > q3 + k * iqr)


# --------------------------------------------------------------------------
# Full report
# --------------------------------------------------------------------------
def run_quality_checks(records: pd.DataFrame, parse_info: dict,
                       qcfg: QualityConfig) -> QualityReport:
    """Run all checks on chronologically sorted, parsed records."""
    issues: list[Issue] = []
    tables: dict[str, pd.DataFrame] = {}
    n = len(records)
    dates = records["date"]

    # --- ingestion-level findings (from loader / parser) ---
    if parse_info.get("footnote_headers"):
        issues.append(Issue("schema", "info",
                            "Header(s) carry footnote markers whose footnote text is not "
                            "included in the file: " + "; ".join(parse_info["footnote_headers"])
                            + ". Column definitions cannot be confirmed from the file."))
    if parse_info.get("blank_rows", 0):
        issues.append(Issue("blank rows", "info",
                            f"{parse_info['blank_rows']} fully blank rows were dropped; they carry "
                            "no observation (cross-check against the source to see whether "
                            "records were removed).", parse_info["blank_rows"]))
    if parse_info.get("invalid_dates"):
        issues.append(Issue("date parsing", "critical",
                            f"{len(parse_info['invalid_dates'])} rows have unparseable dates and "
                            "were excluded.", len(parse_info["invalid_dates"]),
                            parse_info["invalid_dates"]))
    if parse_info.get("exact_duplicate_rows"):
        issues.append(Issue("duplicate dates", "warning",
                            f"{parse_info['exact_duplicate_rows']} exact duplicate rows were "
                            "collapsed to one.", parse_info["exact_duplicate_rows"]))
    if parse_info.get("conflicting_duplicate_dates"):
        cd = parse_info["conflicting_duplicate_dates"]
        issues.append(Issue("duplicate dates", "critical",
                            f"{len(cd)} dates appear more than once with different values; the "
                            "first occurrence in file order was kept.", len(cd), cd))
    if parse_info.get("file_order"):
        issues.append(Issue("ordering", "info",
                            f"File is ordered {parse_info['file_order']}; records were sorted "
                            "chronologically."))

    for col in VALUE_COLUMNS:
        bad = parse_info.get("invalid_values", {}).get(col, [])
        if bad:
            issues.append(Issue("non-numeric values", "critical",
                                f"{LABELS[col]}: {len(bad)} non-numeric values set to missing "
                                "(not zero).", len(bad), bad))
        nonint = parse_info.get("non_integer", {}).get(col, 0)
        if nonint:
            issues.append(Issue("non-integer counts", "warning",
                                f"{LABELS[col]}: {nonint} values are not whole numbers.", nonint))

    missing = records[VALUE_COLUMNS].isna().sum()
    tables["missing_values"] = missing.rename("missing").to_frame()
    for col, m in missing.items():
        if m:
            issues.append(Issue("missing values", "warning",
                                f"{LABELS[col]}: {int(m)} missing values (kept as missing).", int(m),
                                _fmt_dates(records.loc[records[col].isna(), "date"])))

    negatives = (records[VALUE_COLUMNS] < 0).sum()
    for col, m in negatives.items():
        if m:
            issues.append(Issue("negative counts", "critical",
                                f"{LABELS[col]}: {int(m)} negative values.", int(m),
                                _fmt_dates(records.loc[records[col] < 0, "date"])))
    # A zero population is the denominator of a ratio, so that record's ratio stays missing.
    for col in ("cbp_custody", "hhs_care"):
        zeros = int((records[col] == 0).sum())
        if zeros:
            issues.append(Issue("zero population", "warning",
                                f"{LABELS[col]} is zero on {zeros} record(s); ratios using those values are left missing.", zeros,
                                _fmt_dates(records.loc[records[col] == 0, "date"])))

    # --- reporting frequency ---
    gap = dates.diff().dt.days
    span = (dates.max() - dates.min()).days + 1 if n else 0
    gap_counts = gap.value_counts().sort_index()
    tables["gap_distribution"] = gap_counts.rename("records").rename_axis("days_since_previous").to_frame()
    weekday = dates.dt.day_name().value_counts().reindex(
        ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"], fill_value=0)
    tables["weekday_distribution"] = weekday.rename("records").to_frame()
    issues.append(Issue("reporting frequency", "warning",
                        f"{n} reporting dates cover {span} calendar days ({n / span:.0%}); "
                        f"{span - n} calendar days have no record. Reports fall on "
                        + ", ".join(f"{d} ({c})" for d, c in weekday.items() if c)
                        + ". Missing days are not treated as zero activity.", span - n))
    long_gaps = records.loc[gap >= qcfg.long_gap_days, ["date"]].assign(gap_days=gap[gap >= qcfg.long_gap_days])
    long_gaps["previous_report"] = long_gaps["date"] - pd.to_timedelta(long_gaps["gap_days"], unit="D")
    tables["long_gaps"] = long_gaps.reset_index(drop=True)
    if len(long_gaps):
        issues.append(Issue("coverage gaps", "warning",
                            f"{len(long_gaps)} gaps of {qcfg.long_gap_days}+ days between reports "
                            f"(longest {int(gap.max())} days).", len(long_gaps),
                            _fmt_dates(long_gaps["date"])))

    # --- duplicated CBP sequences ---
    dup = analyze_duplicates(records, qcfg)
    tables["duplicate_runs"] = dup.runs
    counts = dup.row_status.value_counts()
    n_copy = int(counts.get(SUSPECT_COPY, 0))
    n_orig = int(counts.get(LIKELY_ORIGINAL, 0))
    n_unres = int(counts.get(UNRESOLVED, 0))
    n_coinc = int(counts.get(COINCIDENTAL, 0))
    if len(dup.runs):
        copy_dates = records.loc[dup.row_status.eq(SUSPECT_COPY), "date"]
        span_txt = (f" Suspect copies span {copy_dates.min():%Y-%m-%d} to {copy_dates.max():%Y-%m-%d}."
                    if len(copy_dates) else "")
        issues.append(Issue(
            "duplicated CBP sequences", "critical",
            f"{n_copy + n_orig + n_unres} records sit in runs of {qcfg.duplicate_min_run}+ consecutive "
            f"records whose three CBP values exactly repeat a run at other dates (longest run "
            f"{int(dup.runs['length'].max())} records). HHS stock-flow evidence labels {n_copy} as "
            f"suspected displaced copies, {n_orig} as likely originals and {n_unres} unresolved."
            + span_txt + f" Attribution threshold {dup.threshold:.1f} ({dup.threshold_source}).",
            n_copy + n_unres, _fmt_dates(copy_dates)))
    if n_coinc:
        issues.append(Issue("coincidental matches", "info",
                            f"{n_coinc} records match another date on CBP values only as isolated "
                            "short runs; treated as coincidence and kept.", n_coinc))

    carry = detect_carry_forward(records)
    if carry.any():
        issues.append(Issue("possible carry-forward", "warning",
                            f"{int(carry.sum())} records repeat the previous report's HHS care and "
                            "discharge values exactly (possible stale snapshot).", int(carry.sum()),
                            _fmt_dates(records.loc[carry, "date"])))
    carry_cbp = detect_carry_forward(records, CBP_COLUMNS, "cbp_possible_carry_forward")
    if carry_cbp.any():
        issues.append(Issue("possible carry-forward", "warning",
                            f"{int(carry_cbp.sum())} records repeat the previous report's three CBP values "
                            "exactly (possible stale feed).", int(carry_cbp.sum()),
                            _fmt_dates(records.loc[carry_cbp, "date"])))

    # --- stock-flow reconciliation ---
    recon = reconcile_stock_flow(records)
    comparable = recon["comparable"]
    status_group = dup.row_status.where(dup.row_status.isin([SUSPECT_COPY, UNRESOLVED]), "not suspect")
    tables["reconciliation_hhs_by_status"] = summarize_residuals(
        recon["hhs_residual"].where(comparable), status_group)
    tables["reconciliation_cbp_by_status"] = summarize_residuals(
        recon["cbp_residual"].where(comparable), status_group)
    tables["reconciliation_hhs_by_gap"] = summarize_residuals(
        recon["hhs_residual"].where(status_group.eq("not suspect")), gap_category(recon["gap_days"]))
    hhs_ok = tables["reconciliation_hhs_by_status"]
    cbp_ok = tables["reconciliation_cbp_by_status"]
    if "not suspect" in hhs_ok.index:
        issues.append(Issue(
            "stock-flow reconciliation", "info",
            f"On one-day gaps without suspect values, HHS care reconciles closely "
            f"(median |residual| {hhs_ok.loc['not suspect', 'median_abs']:.1f}); CBP custody does not "
            f"(median |residual| {cbp_ok.loc['not suspect', 'median_abs']:.1f}, mean "
            f"{cbp_ok.loc['not suspect', 'mean']:+.1f}), indicating undocumented CBP exits, entries "
            "or snapshot-timing differences."))

    over = (records["cbp_transfers_out"] > records["cbp_custody"].shift(1) + records["cbp_intake"]) & gap.eq(1)
    if over.any():
        issues.append(Issue("transfer plausibility", "info",
                            f"{int(over.sum())} one-day records report more transfers than the previous "
                            "custody snapshot plus same-day intake (timing differences).",
                            int(over.sum()), _fmt_dates(records.loc[over, "date"])))

    outlier_rows = []
    for col in VALUE_COLUMNS:
        mask = iqr_outliers(records[col])
        for d, v in zip(records.loc[mask, "date"], records.loc[mask, col]):
            outlier_rows.append({"date": d, "column": LABELS[col], "value": v})
    tables["outliers_iqr3"] = pd.DataFrame(outlier_rows, columns=["date", "column", "value"])
    if outlier_rows:
        issues.append(Issue("outliers", "info",
                            f"{len(outlier_rows)} values fall outside 3x IQR of their column; listed for "
                            "verification against the source (not removed).", len(outlier_rows)))

    summary = {
        "rows_in_file": parse_info.get("rows_in_file"),
        "blank_rows_dropped": parse_info.get("blank_rows", 0),
        "records": n,
        "first_date": dates.min(),
        "last_date": dates.max(),
        "calendar_days": span,
        "missing_calendar_days": span - n,
        "coverage_share": n / span if span else np.nan,
        "suspect_copy_records": n_copy,
        "likely_original_records": n_orig,
        "unresolved_records": n_unres,
        "coincidental_records": n_coinc,
        "carry_forward_records": int(carry.sum()),
        "cbp_carry_forward_records": int(carry_cbp.sum()),
        "attribution_threshold": dup.threshold,
        "attribution_threshold_source": dup.threshold_source,
        "attribution_robust_range": dup.robust_range,
        "longest_duplicate_run": int(dup.runs["length"].max()) if len(dup.runs) else 0,
    }
    return QualityReport(issues=issues, summary=summary, tables=tables,
                         duplicates=dup, carry_forward=carry, cbp_carry_forward=carry_cbp)
