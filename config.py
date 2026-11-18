"""Central configuration: paths, column schema, and analytical defaults.

Every threshold used by the analysis lives here so that results are
reproducible and the dashboard can expose the same values as controls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
REPORTS_DIR = PROJECT_ROOT / "reports"
FIGURES_DIR = REPORTS_DIR / "figures"
GENERATED_DIR = REPORTS_DIR / "generated"

DEFAULT_DATA_FILE = DATA_DIR / "HHS_Unaccompanied_Alien_Children_Program.csv"
# Copies of the published dataset (healthdata.gov ehpz-xc9n) used to cross-check the input.
REFERENCE_DIR = DATA_DIR / "reference"
# Latest published copy, analysed as the extended dataset. It starts on the supplied
# file's first date so that the baseline and extended periods compare directly.
OFFICIAL_DATA_FILE = REFERENCE_DIR / "ehpz-xc9n_export_2026-10-29.csv"
OFFICIAL_ANALYSIS_START = "2023-01-12"
EXTENDED_REPORTS_DIR = REPORTS_DIR / "extended"


def resolve_data_file(path: str | os.PathLike | None = None) -> Path:
    """Return the input file: explicit argument > UAC_DATA_FILE env var > default."""
    if path:
        return Path(path)
    env = os.environ.get("UAC_DATA_FILE")
    return Path(env) if env else DEFAULT_DATA_FILE


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ColumnSpec:
    standard: str
    source: str          # expected source header (footnote markers removed)
    kind: str            # "date" | "stock" | "flow"
    stage: str           # "CBP" | "HHS" | "-"
    label: str           # short display label


COLUMN_SPECS: tuple[ColumnSpec, ...] = (
    ColumnSpec("date", "Date", "date", "-", "Reporting date"),
    ColumnSpec("cbp_intake", "Children apprehended and placed in CBP custody",
               "flow", "CBP", "CBP intake (apprehended & placed in CBP custody)"),
    ColumnSpec("cbp_custody", "Children in CBP custody",
               "stock", "CBP", "Children in CBP custody"),
    ColumnSpec("cbp_transfers_out", "Children transferred out of CBP custody",
               "flow", "CBP", "Transfers out of CBP custody"),
    ColumnSpec("hhs_care", "Children in HHS Care",
               "stock", "HHS", "Children in HHS care"),
    ColumnSpec("hhs_discharges", "Children discharged from HHS Care",
               "flow", "HHS", "Discharges from HHS care"),
)

SPEC_BY_NAME = {s.standard: s for s in COLUMN_SPECS}
VALUE_COLUMNS = [s.standard for s in COLUMN_SPECS if s.kind != "date"]
STOCK_COLUMNS = [s.standard for s in COLUMN_SPECS if s.kind == "stock"]
FLOW_COLUMNS = [s.standard for s in COLUMN_SPECS if s.kind == "flow"]
CBP_COLUMNS = [s.standard for s in COLUMN_SPECS if s.stage == "CBP"]
HHS_COLUMNS = [s.standard for s in COLUMN_SPECS if s.stage == "HHS"]
LABELS = {s.standard: s.label for s in COLUMN_SPECS}

# Formats tried in order before falling back to pandas' flexible parser.
DATE_FORMATS = ("%B %d, %Y", "%B %d %Y", "%Y-%m-%d", "%m/%d/%Y")  # "%B %d %Y": archived versions


# --------------------------------------------------------------------------
# Analytical defaults
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class QualityConfig:
    """Rules for detecting and handling suspect records."""

    # A run of >= this many consecutive records whose CBP values exactly match
    # another run elsewhere is treated as duplicated (not coincidence).
    duplicate_min_run: int = 3
    # |HHS stock-flow residual| above which a duplicated run is considered
    # inconsistent with the HHS stock movement at its reported dates.
    # None -> data-driven: this quantile of |residual| on non-duplicated rows.
    attribution_threshold: float | None = None
    attribution_quantile: float = 0.95
    # Records with an identical (HHS care, discharges) pair on consecutive
    # reporting dates are treated as possible carry-forward of stale values.
    mask_hhs_carry_forward: bool = True
    # Same for the three CBP values (a stuck feed repeating identical records).
    mask_cbp_carry_forward: bool = True
    # Duplicated runs whose original/copy status cannot be resolved are masked.
    mask_unresolved_duplicates: bool = True
    # Gap (calendar days) between reports that is reported as a coverage break.
    long_gap_days: int = 5


@dataclass(frozen=True)
class BottleneckConfig:
    """Thresholds for the transparent, rule-based pressure detection."""

    window_days: int = 28            # trailing calendar window for sustained rules
    min_records: int = 10            # minimum reported days inside a window
    baseline_days: int = 180         # look-back for "decline vs baseline" rules
    min_episode_records: int = 5     # consecutive flagged records to form an episode
    hhs_growth_pct: float = 3.0      # HHS care up >= x% vs window start
    cbp_growth_pct: float = 25.0     # CBP custody up >= x% vs window start ...
    cbp_growth_min_abs: float = 10.0  # ... and by at least this many children
    imbalance_ratio: float = 1.10    # entries / exits over window >= this
    ratio_decline_pct: float = 25.0  # pooled ratio <= (100 - x)% of baseline
    stagnation_pct_of_reference: float = 50.0  # DSR < x% of pooled DSR before the window
    anomaly_z: float = 3.5           # robust z-score for single-day anomalies
    backlog_requires_both: bool = True  # backlog = growth AND imbalance (else OR)


DEFAULT_QUALITY = QualityConfig()
DEFAULT_BOTTLENECK = BottleneckConfig()

ROLLING_WINDOW_OPTIONS = (7, 14, 28, 56, 91)
DEFAULT_ROLLING_DAYS = 28
SIGNIFICANCE_LEVEL = 0.05
