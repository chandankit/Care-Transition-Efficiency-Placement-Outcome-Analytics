# UAC Care Pipeline Efficiency Analytics

A process-efficiency analysis of the unaccompanied children care pipeline: **CBP custody → HHS care → discharge**. It is built from HHS's public aggregate daily reports. It includes:

- a reproducible analytics library;
- an interactive Streamlit dashboard;
- a final report include research paper, an executive summary and supporting reports.

## Quick start

```bash
python -m pip install -r requirements.txt
```

```bash
python -m streamlit run app.py
```

Tested with Python 3.13, pandas 2.3, Streamlit 1.52, Plotly 7.1, SciPy 1.16 and kaleido 1.4 on Windows 11.

## What is in the project

```text
uac-care-pipeline-analytics/
├── app.py                     Streamlit dashboard (7 modules)
├── config.py                  paths, schema, every threshold and default
├── requirements.txt
├── data/                      input CSV + data README (provenance, schema)
│   └── reference/             copies of the published HHS dataset, for the source cross-check
├── src/
│   ├── data_loader.py         read CSV, map headers (tolerates footnote markers)
│   ├── data_validation.py     quality checks, duplicated-sequence detection + attribution
│   ├── source_check.py        cell-by-cell comparison with a reference copy of the source
│   ├── preprocessing.py       parsing, audit log, as-reported and screened views
│   ├── pipeline_analysis.py   stage model, stock-flow reconciliation, gap decomposition
│   ├── kpi_calculations.py    KPI formulas, pooled ratios, aggregation, rolling, KPI dictionary
│   ├── bottleneck_detection.py rule-based pressure episodes, anomalies
│   ├── trend_analysis.py      weekday index, trend tests, level shifts, stability
│   └── visualizations.py      Plotly figures (shared by app and report export)
└── Ankit_Chand_Final_Internship_Project_Report.pdf           Final report of full output tables
```

## Dashboard

The **sidebar controls** are:

- dataset: the supplied file (to 2025-12-21, default) or the official HHS data (latest copy, to 2026-10-27);
- data file upload;
- data view (*quality-screened*, recommended, or *as reported*);
- date range;
- aggregation (daily, weekly, monthly or quarterly);
- rolling window;
- ratio denominator (same-day or previous-day snapshot);
- every alert threshold.

All modules use the same filtered slice. Pick a module with the selector at the top of the page; the choice is kept when sidebar settings change.

| Module | Contents |
|---|---|
| Executive overview | Latest snapshots, period totals vs per-day means, pooled KPIs vs the previous period, top pressure episodes, quality warnings |
| Care pipeline | Stage diagram (deliberately not a Sankey), stage reconciliation, one-day vs gap decomposition, stocks, flows, implied HHS admissions |
| Transfer & discharge efficiency | TSR and DSR with rolling pooled means or period bars, summary statistics, throughput by period |
| Bottleneck detection | Episodes shaded on the stage population, entries vs exits, every rule metric against its threshold, anomalies, rule definitions |
| Outcome & trend analysis | Monthly discharges and MoM change, weekday index with tests, rolling CV, trend tests and level shifts, two-period comparison |
| Data quality & RCA | Duplicated-sequence evidence, source cross-check, reconciliation residuals, all checks, gaps, cleaning log |
| Data explorer & KPI definitions | Filterable records with flags, missing-value summary, CSV downloads, KPI dictionary |

## Analytical conventions

These are enforced in code and covered by tests:

- **Stocks vs flows.** Custody and care are snapshots and are never summed. Intake, transfers and discharges are flows.
- **Pooled ratios.** A period ratio is Σ numerator / Σ denominator over records where both are present, never the mean of daily ratios.
- **Per reported day.** Periods are compared on means per reported day. Totals depend on how many days were reported.
- **No zero-fill, no imputation.** Missing or screened values stay missing; ratios with zero or missing denominators are missing.
- **Full-history rules.** Rolling metrics and bottleneck rules are computed on the full history, then filtered, so windows never start at the edge of a selection.
- **Stocks for direction.** Backlog pressure requires stock growth because reported-day flows miss unreported days.
- **No composite score.** The alert tier is the count of concurrent rules, with equal weights.

## Configuration

Every default is in [config.py](config.py):

- `QualityConfig`: duplicate run length, attribution threshold (data-driven by default) and masking options.
- `BottleneckConfig`: window, minimum records, baseline, growth and imbalance thresholds, decline percentage, stagnation level and anomaly z-score.

Dashboard threshold controls override `BottleneckConfig` for the session. The input file can be set with `UAC_DATA_FILE`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Input schema is invalid: missing column ...` | The CSV needs the six columns listed in [data/README.md](data/README.md); header matching ignores case and `*` |
| `Data file not found` | Put the CSV in `data/`, pass `--data`, set `UAC_DATA_FILE`, or upload it in the sidebar |
| Dashboard reachable from other machines | Streamlit listens on all interfaces by default; add `address = "localhost"` under `[server]` in `.streamlit/config.toml` to restrict it |
| Charts show "CBP values screened out" | Expected for 2023 – Jan 2024; switch the data view to *As reported* to compare |

## Deliverables map

| Requested deliverable | Location |
|---|---|
| Python project, dashboard | `src/`, `app.py` |
| Final Report | [Ankit_Chand_Final_Internship_Project_Report.pdf](Ankit_Chand_Final_Internship_Project_Report.pdf) |