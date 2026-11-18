# KPI dictionary

Generated from `src/kpi_calculations.py::KPI_DEFINITIONS` and `src/bottleneck_detection.py::rule_catalog` by `scripts/run_analysis.py`; the dashboard shows the same definitions. Conventions: ratios are percentages computed as pooled sums (never averages of daily ratios); flows cover reported days only; stocks are snapshots and are never summed; zero or missing denominators give a missing value.

## Transfer-to-Stock Ratio (TSR)

- **Formula:** 100 x transfers out of CBP(t) / children in CBP custody(t)
- **Required columns:** Children transferred out of CBP custody; Children in CBP custody
- **Unit:** % of reported CBP custody per reported day
- **Aggregation frequency:** Daily; period value is pooled: 100 x sum(transfers) / sum(custody snapshots)
- **Denominator / reporting period:** Same-day reported snapshot (default). Option: previous snapshot, only when it is exactly one day earlier. Whether the snapshot is opening or closing is undocumented.
- **Missing or zero denominators:** NaN when custody is 0 or missing, or transfers missing. Never 0 or infinity.
- **Interpretation:** Descriptive: transfer activity relative to the reported custody population. Higher values mean more transfers per child reported in custody.
- **Known limitations:** Not a transfer probability and not a processing time. Values above 100% are possible because custody is a point-in-time snapshot while transfers accumulate over a day. Unavailable where CBP values are screened out.

## Discharge-to-Stock Ratio (DSR)

- **Formula:** 100 x discharges from HHS care(t) / children in HHS care(t)
- **Required columns:** Children discharged from HHS Care; Children in HHS Care
- **Unit:** % of reported HHS care population per reported day
- **Aggregation frequency:** Daily; period value pooled as for TSR
- **Denominator / reporting period:** Same-day reported snapshot (default) or one-day-earlier snapshot.
- **Missing or zero denominators:** NaN when HHS care is 0 or missing, or discharges missing.
- **Interpretation:** Descriptive: discharge activity relative to the reported HHS population.
- **Known limitations:** Not a placement success rate. HHS describes discharges as children 'united to a vetted sponsor' but publishes no categories or counting rules, and the data do not track cohorts. The children discharged are not necessarily those in the denominator.

## Stock-to-Discharge Ratio (days)

- **Formula:** mean HHS care / mean daily discharges over the period (= 100 / pooled DSR)
- **Required columns:** Children in HHS Care; Children discharged from HHS Care
- **Unit:** days
- **Aggregation frequency:** Weekly, monthly, quarterly, or selected period
- **Denominator / reporting period:** Mean discharges per reported day
- **Missing or zero denominators:** NaN when there are no discharges in the period.
- **Interpretation:** How many days of discharges at the current pace the reported population represents. Under a steady state (Little's law) it approximates mean time in care.
- **Known limitations:** Not a measured length of care. Biased whenever the population is growing or shrinking, which is most of this dataset.

## Pipeline Throughput

- **Formula:** CBP stage: sum(transfers)/sum(intake). HHS stage: sum(discharges)/sum(transfers). System: sum(HHS discharges)/sum(CBP intake). All x100.
- **Required columns:** Intake, transfers out of CBP, HHS discharges
- **Unit:** %
- **Aggregation frequency:** Weekly, monthly, quarterly, or selected period
- **Denominator / reporting period:** Entries to the stated boundary over the same reported days as the exits
- **Missing or zero denominators:** Only records where both flows are present are used.
- **Interpretation:** Above 100%: more exits than entries on reported days (stock drawn down). Below 100%: entries exceed exits (stock builds). The system ratio counts the internal CBP->HHS transfer only once.
- **Known limitations:** System ratio ignores CBP exits other than transfers (none are reported). HHS stage assumes transfers out of CBP are the HHS admissions. Unreported days are excluded from both numerator and denominator.

## CBP / HHS Population Change

- **Formula:** stock(t) - stock(previous report); per-day version divides by the gap in days. Period change: closing snapshot - previous period's closing snapshot.
- **Required columns:** Children in CBP custody; Children in HHS Care
- **Unit:** children (or children per calendar day)
- **Aggregation frequency:** Between consecutive reports; weekly/monthly/quarterly
- **Denominator / reporting period:** Gap in calendar days (per-day version)
- **Missing or zero denominators:** NaN if either snapshot is missing or screened out.
- **Interpretation:** Growth or reduction in the reported population.
- **Known limitations:** Snapshots only; the change mixes entries and exits. The combined CBP+HHS population is not a count of unique children.

## Net Flow Imbalance

- **Formula:** CBP: intake - transfers. HHS: transfers - discharges. Window ratio: sum(entries)/sum(exits) over a trailing window.
- **Required columns:** Intake, transfers, discharges
- **Unit:** children per reported day; ratio
- **Aggregation frequency:** Daily; trailing window (default 28 days)
- **Denominator / reporting period:** Exits over the same records (ratio form)
- **Missing or zero denominators:** Records with any missing component are excluded from the window sums.
- **Interpretation:** Positive / ratio above 1: entries exceed exits on reported days.
- **Known limitations:** Flows on unreported days are unknown. CBP stock-flow does not reconcile even on consecutive days, so CBP imbalance is indicative only.

## Backlog Pressure Indicator

- **Formula:** Per stage: sustained stock growth rule AND (configurable: OR) entries-exceed-exits rule, both evaluated on the trailing window.
- **Required columns:** Stage stock and flows
- **Unit:** flag (true/false) per record; episodes of 5+ consecutive flagged records
- **Aggregation frequency:** Daily evaluation over a trailing window
- **Denominator / reporting period:** -
- **Missing or zero denominators:** Not evaluated (NA) when the window has too few reported days.
- **Interpretation:** Flags potential operational pressure for review.
- **Known limitations:** Not a verified queue length or waiting time; does not identify causes.

## Discharge Stability Indicator

- **Formula:** Coefficient of variation = std / mean of daily discharges, over a trailing window or a calendar period; plus month-over-month % change of mean daily discharges.
- **Required columns:** Children discharged from HHS Care
- **Unit:** ratio (unitless); %
- **Aggregation frequency:** Rolling window; monthly
- **Denominator / reporting period:** Mean daily discharges (NaN when 0)
- **Missing or zero denominators:** Needs at least the configured number of reported days.
- **Interpretation:** Lower CV = more consistent discharge activity.
- **Known limitations:** Day-of-week reporting patterns inflate daily variability; CV is unstable when mean discharges are very small (e.g. late 2025).

## Bottleneck Alert

- **Formula:** Any configured rule (see Bottleneck Detection) firing for 5+ consecutive records; alert tier = number of rules active at once (equal weights).
- **Required columns:** All
- **Unit:** episode (start, end, duration, magnitude)
- **Aggregation frequency:** Daily evaluation
- **Denominator / reporting period:** -
- **Missing or zero denominators:** Rules are not evaluated on insufficient data.
- **Interpretation:** Periods that merit operational review.
- **Known limitations:** Rule-based screening, not proof of a delay; causes require other evidence.

## Implied HHS Admissions (derived)

- **Formula:** HHS care(t) - HHS care(t-1) + discharges(t), one-day gaps only
- **Required columns:** Children in HHS Care; Children discharged from HHS Care
- **Unit:** children per day
- **Aggregation frequency:** Daily (consecutive days only)
- **Denominator / reporting period:** -
- **Missing or zero denominators:** NaN across reporting gaps or missing values.
- **Interpretation:** HHS-side estimate of admissions that does not rely on the CBP columns.
- **Known limitations:** Derived, not reported. Equals admissions only if the stock-flow identity holds with no other adjustments.

## Bottleneck detection rules (default thresholds)

| Rule | Stage | Definition | Interpretation | Suggested investigation |
|---|---|---|---|---|
| `HHS_STOCK_GROWTH` | HHS | Children in HHS care >= 3% above the snapshot 28 days earlier | The reported HHS population is building up. | Compare with admissions, discharge processing and capacity records for the period. |
| `HHS_INFLOW_EXCEEDS_EXITS` | HHS | Over 28 days, sum(transfers out of CBP) / sum(HHS discharges) >= 1.1 | On reported days, more children entered HHS than were discharged. Flows on unreported days are missing, and HHS care can fall across reporting gaps, so read this together with HHS_STOCK_GROWTH (see HHS_BACKLOG_PRESSURE). | Check discharge pipeline stages (sponsor assessment, approvals) for delays. |
| `CBP_STOCK_GROWTH` | CBP | Children in CBP custody >= 25% and >= 10 children above the snapshot 28 days earlier | The reported CBP custody population is building up. | Review intake surges and HHS placement availability for transfers. |
| `CBP_INTAKE_EXCEEDS_TRANSFERS` | CBP | Over 28 days, sum(CBP intake) / sum(transfers out of CBP) >= 1.1 | On reported days, more children entered CBP custody than were transferred out. CBP stock-flow does not reconcile even on consecutive days, so treat as indicative. | Check transfer referral and placement-designation timing. |
| `DSR_DECLINE` | HHS | 28-day pooled DSR <= 75% of the pooled DSR over the 180 days before the window | Discharge activity relative to the population is weaker than its recent norm. | Check sponsor vetting, case-management workload and discharge definitions. |
| `TSR_DECLINE` | CBP | 28-day pooled TSR <= 75% of the pooled TSR over the 180 days before the window | Transfers relative to custody are weaker than their recent norm. | Check HHS bed availability and transfer logistics. |
| `LOW_DISCHARGE_ACTIVITY` | HHS | 28-day pooled DSR < 50% of the long-run pooled DSR (all reports before the window; no look-ahead) | Few discharges relative to the population compared with the long-run norm. | Establish whether discharge policy, definitions or processing changed. |
| `HHS_BACKLOG_PRESSURE` | HHS | HHS_STOCK_GROWTH AND HHS_INFLOW_EXCEEDS_EXITS | Population growth coinciding with entries outpacing discharges. | Prioritise for operational review. |
| `CBP_BACKLOG_PRESSURE` | CBP | CBP_STOCK_GROWTH AND CBP_INTAKE_EXCEEDS_TRANSFERS | Custody growth coinciding with intake outpacing transfers. | Prioritise for operational review. |

Episodes: 5+ consecutive flagged records. Rules are NA (not evaluated) when the 28-day window holds fewer than 10 reported days or the baseline is unavailable. Alert tier = number of base rules active at once (equal weights); no weighted composite score is used.