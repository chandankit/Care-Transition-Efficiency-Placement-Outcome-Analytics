# Data

## Default input

`HHS_Unaccompanied_Alien_Children_Program.csv`. This is a copy of the file supplied for the project; the original is in the parent folder.

- **Publisher:** U.S. Department of Health & Human Services, dataset "HHS Unaccompanied Alien Children Program" (data.gov catalog; identifier `healthdata.gov/api/views/ehpz-xc9n`): https://catalog.data.gov/dataset/hhs-unaccompanied-alien-children-program
- **Content:** aggregate daily counts only, with no personal information.
- **Coverage of this copy:** 720 reports, 2023-01-12 to 2025-12-21. The file also contains 450 blank trailing rows, which are dropped on load.
- **Relation to the published data:** the file has the same 1,170 rows as the version HHS published on 2025-12-22, and identical values on all 720 dated rows, with two exceptions: the CBP cells for 2023-01-12 to 2024-01-30 (blank at source) hold copied values, and the 450 rows for 2021-01-11 to 2022-12-29 are blank.

| File | What it is | Retrieved from | Retrieved |
|---|---|---|---|
| `ehpz-xc9n_2025-12-22T12-38-59.csv` | Archived version published 2025-12-22 12:38:59 (1,170 rows, 2021-01-11 → 2025-12-21); the version the supplied file corresponds to | https://us-dhhs-aa.s3.us-east-2.amazonaws.com/ehpz-xc9n_2025-12-22T12-38-59.csv, linked from the version archive https://healthdata.gov/d/y83i-qwwd | 2026-10-29 |
| `ehpz-xc9n_export_2026-10-29.csv` | Current export (1,379 rows, 2021-01-11 → 2026-10-27; rows last updated 2026-10-28) | https://healthdata.gov/api/v3/views/ehpz-xc9n/export.csv?accessType=DOWNLOAD | 2026-10-29 |

Facts from the publisher's metadata (https://healthdata.gov/api/views/ehpz-xc9n and its column list):

- The three CBP columns were added on 2024-03-01, and the first CBP value is dated 2024-01-31. Earlier rows contain HHS values only.
- The intake column was first published as "Children placed in CBP custody\*" and renamed the same day. The text of its `*` footnote is not published.
- The discharge column is described as "Unaccompanied children united to a vetted sponsor". That description has been unchanged since 2021.
- In both copies, 10 dates in January 2022 appear twice with different values. The loader keeps the first occurrence and flags it.

## Using a different file

The file must contain these six columns. Header matching ignores case, extra spaces and footnote markers such as `*`:

| Header | Kind |
|---|---|
| Date | e.g. `December 21, 2025`, `December 21 2025`, `2025-12-21` or `12/21/2025` |
| Children apprehended and placed in CBP custody | daily flow |
| Children in CBP custody | snapshot |
| Children transferred out of CBP custody | daily flow |
| Children in HHS Care | snapshot |
| Children discharged from HHS Care | daily flow |

Thousands separators are accepted. Blank or non-numeric cells become missing (never zero), and they are reported in the data-quality report.

## Known issues in the supplied file

- The CBP columns of all reports from 2023-01-12 to 2024-01-30 duplicate later periods and are blank in the published source, so they are screened out.
- Fridays and Saturdays are not reported, in the source as well.
- The footnote behind the `*` in the intake header is not included, and it is not published either.

## Terms

The data are public U.S. government aggregates. Check the publisher's terms before redistributing modified copies.