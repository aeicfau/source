# IRS 990-PF Grant Extractor

A single, dependency-free Python script that mines IRS Form 990-PF e-file XML returns and extracts grants made to colleges and universities into one flat CSV.

It handles **both** the pre-2014 and the 2015-or-later IRS XML schemas in a single pass, recovers grantees that older parsers missed, and de-duplicates corrected re-filings so each foundation is counted once per tax year.

## What it does

1. **Recursively walks** every subfolder beneath the directory the script lives in and finds every `.xml` file.
2. **Parses both schemas** transparently. Grant groups, amounts, purposes, and filer names are read through tag fallbacks that cover the old (`GrantOrContriPaidDuringYear`, `Amount`, `Name/BusinessNameLine1`) and new (`GrantOrContributionPdDurYrGrp`, `Amt`/`CashGrantAmt`, `BusinessName/BusinessNameLine1Txt`) layouts.
3. **Filters to higher education** using a three-tier include/exclude keyword list (see [Filtering](#filtering)).
4. **De-duplicates corrections.** When one foundation files more than once for the same tax year, only the filing with the latest `<ReturnTs>` timestamp is kept; the earlier filing is discarded entirely.
5. **Writes one flat CSV**, `all_grants.csv`, into the directory it was run from.

## Why it exists

Two earlier scripts handled the pre-2014 and post-2014 schemas separately and had gaps:

- **Missed foundations.** Some filers tag grantees as `<RecipientPersonNm>` rather than `<RecipientBusinessName>`. The old post-2014 parser only read the latter and silently extracted **zero** grants from these filers. This version falls back to `RecipientPersonNm` and recovers them.
- **No de-duplication.** A foundation that amends a return files a second XML for the same tax year. Because the IRS organizes files by processing year, the original and the correction can land in different folders. The old per-folder scripts double-counted them. This version de-duplicates globally.
- **Blank columns.** The `RecipientEIN` field was consistently empty and has been dropped from the output.

## Requirements

- Python 3.7 or newer.
- No third-party packages. Uses only the standard library (`os`, `sys`, `csv`, `xml.etree.ElementTree`, `datetime`).

## Usage

Place the script at the top of your data tree and run it:

```bash
python irs_parse_xml_consolidated.py
```

Expected layout (folder names are arbitrary; nesting is fine):

```
your-data/
├── irs_parse_xml_consolidated.py
├── 2016/
│   └── 201633159349100978_public.xml
├── 2021/
│   └── 202103159349102615_public.xml
└── 2023/
    └── nested/
        └── 202312579349101156_public.xml
```

On completion you get `all_grants.csv` next to the script, plus a console summary:

```
Found 3 XML files. Parsing...
  Processing: 3/3 files
Kept 2 unique filings (filer + tax year) after de-duplicating corrections.
Wrote 15 grant rows to all_grants.csv
```

## Output

`all_grants.csv` has one row per qualifying grant:

| Column         | Description                                              |
| -------------- | -------------------------------------------------------- |
| XML file       | Source filename the row came from                        |
| Filer name     | Foundation name                                          |
| Filer EIN      | Foundation EIN                                           |
| Return year    | Tax year (`TaxYr`, or the year of `TaxPeriodEndDt`)      |
| Grantee        | Recipient name (business or person tag)                  |
| Grant amount   | `Amt`, `CashGrantAmt`, or `Amount`                       |
| Grant purpose  | Stated purpose of the grant                              |

## Filtering

A grantee is kept when its name passes a three-tier test:

- **Exclude** substrings are checked first and override everything (e.g. `college board`).
- **Include keywords** (substring, case-insensitive): `college`, `university`, `institute of technology`, `polytechnic`, `school of mines`.
- **Include names** for institutions that match no keyword: `juilliard`, `pratt institute`, `cooper union`, `leland stanford`, `caltech`.

Edit the `INCLUDE_KEYWORDS`, `INCLUDE_NAMES`, and `EXCLUDE_KEYWORDS` lists near the top of the script to change the scope. To extract **all** grants regardless of recipient type, make `is_college_or_university` return `True`.

## De-duplication logic

Filings are grouped by **filer EIN + tax year**. Within a group, the filing with the latest `<ReturnTs>` wins; ties break on the leading numeric submission ID in the filename.

> **Edge case:** if a filing has a blank EIN it is keyed by its file path instead of merged, so two unrelated EIN-less filers never collapse into one. The trade-off: a genuine duplicate where one copy is missing its EIN will not be caught. Add a name-based fallback if that case exists in your data.

## License

MIT. See `LICENSE` if included.
