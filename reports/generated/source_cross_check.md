# Source cross-check

Input: `HHS_Unaccompanied_Alien_Children_Program.csv`: 720 records, 2023-01-12 to 2025-12-21, 450 fully blank rows dropped on load.

Each value cell of the input is compared with the reference on the same date. Statuses: *match*; *both missing*; *value not in reference* (reference cell blank, input has a value); *missing in file*; *different value*.

## Reference `ehpz-xc9n_2025-12-22T12-38-59.csv`

- 1170 rows in file; 1160 records after de-duplication, 2021-01-11 to 2025-12-21; earliest date with any CBP value: 2024-01-31.
- Reference rows dated before the input's first date: 450 (440 distinct dates).
- Input dates missing from the reference: 0; reference dates (within the input's range) missing from the input: 0.
- Reference's own quality note: 10 dates appear more than once with different values; the first occurrence in file order was kept.

**Cells by column and status**

| column | match | both missing | value not in reference | missing in file | different value |
|---|---|---|---|---|---|
| cbp_intake | 469 | 0 | 251 | 0 | 0 |
| cbp_custody | 469 | 0 | 251 | 0 | 0 |
| cbp_transfers_out | 469 | 0 | 251 | 0 | 0 |
| hhs_care | 720 | 0 | 0 | 0 | 0 |
| hhs_discharges | 720 | 0 | 0 | 0 | 0 |

**CBP cells (three per record) by the input's duplicated-sequence status**

| cbp_duplicate_status | match | value not in reference |
|---|---|---|
| coincidental_match | 6 | 0 |
| likely_original | 363 | 0 |
| suspect_copy | 0 | 753 |
| unique | 1038 | 0 |

**Where the input departs from the reference**

| column | status | cells | first_date | last_date |
|---|---|---|---|---|
| cbp_intake | value not in reference | 251 | 2023-01-12 | 2024-01-30 |
| cbp_custody | value not in reference | 251 | 2023-01-12 | 2024-01-30 |
| cbp_transfers_out | value not in reference | 251 | 2023-01-12 | 2024-01-30 |

Verdict: 753 cells depart from the reference; 753 input values have no counterpart in the reference.

## Reference `ehpz-xc9n_export_2026-10-29.csv`

- 1379 rows in file; 1369 records after de-duplication, 2021-01-11 to 2026-10-27; earliest date with any CBP value: 2024-01-31.
- Reference rows dated before the input's first date: 450 (440 distinct dates).
- Input dates missing from the reference: 0; reference dates (within the input's range) missing from the input: 0.
- Reference's own quality note: 10 dates appear more than once with different values; the first occurrence in file order was kept.

**Cells by column and status**

| column | match | both missing | value not in reference | missing in file | different value |
|---|---|---|---|---|---|
| cbp_intake | 469 | 0 | 251 | 0 | 0 |
| cbp_custody | 469 | 0 | 251 | 0 | 0 |
| cbp_transfers_out | 469 | 0 | 251 | 0 | 0 |
| hhs_care | 720 | 0 | 0 | 0 | 0 |
| hhs_discharges | 720 | 0 | 0 | 0 | 0 |

**CBP cells (three per record) by the input's duplicated-sequence status**

| cbp_duplicate_status | match | value not in reference |
|---|---|---|
| coincidental_match | 6 | 0 |
| likely_original | 363 | 0 |
| suspect_copy | 0 | 753 |
| unique | 1038 | 0 |

**Where the input departs from the reference**

| column | status | cells | first_date | last_date |
|---|---|---|---|---|
| cbp_intake | value not in reference | 251 | 2023-01-12 | 2024-01-30 |
| cbp_custody | value not in reference | 251 | 2023-01-12 | 2024-01-30 |
| cbp_transfers_out | value not in reference | 251 | 2023-01-12 | 2024-01-30 |

Verdict: 753 cells depart from the reference; 753 input values have no counterpart in the reference.
