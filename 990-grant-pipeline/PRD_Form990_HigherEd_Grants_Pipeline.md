# PRD — IRS Form 990 Higher-Education Grants Pipeline

**Status:** Operational · **Audience:** Engineer/analyst taking ownership · **Last updated:** 2026-07-15

This is the end-to-end ownership document: what the system produces, how each stage works, the data
model, how to run and maintain it, and where the traps are. It is written to be handed to someone
new. The companion **`README.md`** (same directory) is the technical/CLI reference — this PRD is the
map and the rationale; the README is the operating manual.

> **One-line summary:** a single self-contained Python script (`irs-990-pipeline.py`) turns raw IRS
> 990/990-PF e-file XML into three incremental, SQL-ready CSVs of grants made to U.S. colleges and
> universities — each grant resolved to a stable institution id and tagged by purpose.

---

## 1. Background & problem

U.S. tax-exempt foundations and public charities report the grants they make on IRS Form **990**
(public charities, Schedule I) and **990-PF** (private foundations, grants-paid schedule). The IRS
publishes these as machine-readable **e-file XML**. We mine them to build a clean, queryable table of
**grants made to colleges and universities**.

Two problems make this hard:

1. **Recipient strings are noisy.** One school appears as "Stanford University", "Leland Stanford
   Junior University", "Trustees of Stanford", "FBO Stanford", "Stanford Univ." — plus foundations,
   alumni associations, med centers, and typos. Grants can't be aggregated by institution until these
   collapse to one identity per school.
2. **There is no reliable join key in the source.** Recipient EIN is many-per-institution (one school
   appears under its regents', foundation's, and med-center's EINs) and **990-PF discloses no
   recipient EIN at all** (~70% of in-scope volume).

The system solves both by resolving every grant to a stable **`canonical_id`** (one row per real
institution) and enriching each grant with a topical **`tag`**. A separate double-count signal
(`funder_support_org`) lives on the processed-return log — see §7.

---

## 2. Goals & non-goals

**Goals**
- Produce a grants table (`grantsdb`) where every college/university grant carries a stable
  `canonical_id` and a topical `tag`.
- Maintain a canonical institution dimension (`dim_institution`) and a processed-return log
  (`ref_xml_processed`) carrying the funder-side double-count signal.
- Support **incremental** updates as new filings appear, without reprocessing history.
- Be reproducible and cheap: a deterministic majority, with a paid LLM only on the hard tail.
- **Minimal dependency surface:** one script, two data files, no build step, no local module imports.

**Non-goals**
- Not a general 990 parser — only grants-paid data, filtered to higher-ed recipients.
- Not real-time — it runs in batches (typically after new index snapshots).
- **Does not dedupe amended re-filings at ingestion** — that is deferred to the query layer (§10).
- **Does not exclude double-count filers** — it *flags* them (`funder_support_org`) for query-time
  filtering.
- Does not discover new IPEDS unitids at runtime — new institutions get a surrogate id; promoting one
  to a real unitid is offline curation.

---

## 3. Users & use cases

- **Analysts** querying "how much did foundation X give to university Y over time," or "total STEM
  research dollars to R1 universities," via joins on `canonical_id` and filters on `tag` and (joined
  from `ref_xml_processed`) `funder_support_org`.
- **Data engineer (owner)** running incremental updates, loading the three increments to SQL, and
  curating the review queue (grantees the pipeline left blank on purpose).

---

## 4. System overview (data flow)

The whole job is one script with four **stages**, chained through per-run intermediate files keyed by
`--stamp`. Run them together (default) or split them across machines.

```
 IRS e-file XML  (GivingTuesday 990 Data Lake, public S3)
        │
        ▼
 [1] download   skip anything already processed; download everything else in scope
        │        (fused with parse by default — see below)
 [2] parse      keep college/university grants; flag the filer; record the return
        │            → _parsed_<stamp>.csv  (raw grant rows)
        ▼
 [3] match      resolve grantee → canonical_id   (dim_institution.csv, then Haiku for the residual)
        │            → _matched_<stamp>.csv  + dim_institution_incremental_<stamp>.csv
        ▼
 [4] tag        classify grant purpose → tag      (Haiku)
        │            → grantsdb_incremental_<stamp>.csv
        ▼
 [D] LOAD the three increments to SQL:  grantsdb · dim_institution · ref_xml_processed
```

**Fused acquisition.** When `download` and `parse` run together (the default), each worker downloads
one return and parses it immediately — parsing overlaps downloading, and non-grant XML is deleted
inline, so disk stays tiny and there's no "download everything, then parse" phase. (The keep-only-
grant-bearing rule is why fusing matters: download-all-first would hoard terabytes to keep ~5%.)

**Parallel acquisition.** `--parallel-years N` fans stages 1–2 into N processes split by tax year
(N× connections *and* N parse cores, escaping Python's single-core GIL parse ceiling), then merges
and runs match+tag once. This is also the unit of cross-machine sharding.

Three durable tables are the product:

| Table | Grain | Role |
|---|---|---|
| `grantsdb` | one row per grant | the fact table (grant + `canonical_id` + `tag`) |
| `dim_institution` | one row per institution (`canonical_id`) | the identity dimension |
| `ref_xml_processed` | one row per XML filename | the processed-ledger / download skip-list, carries `funder_support_org` |

---

## 5. Data sources

- **GivingTuesday 990 Data Lake** (public, anonymous mirror of IRS e-file XML):
  `s3://gt990datalake-rawdata` → `https://gt990datalake-rawdata.s3.amazonaws.com`. No AWS account.
- **Master index**: one large CSV under `Indices/990xmls/` (~3.2 GB), one row per return with
  `FormType`, `ObjectId`, `TaxYear`, `URL`, `EIN`, `OrganizationName`, etc. **Auto-detected** (newest
  `all_years` index), cached locally as `_index_*.csv`, reused across runs. First 4 digits of
  `ObjectId` = filing year.
- **Each return**: `EfileData/XmlFiles/<ObjectId>_public.xml`.
- **IPEDS** (public federal enumeration of degree-granting U.S. institutions) is the source of the
  `unitid` anchor — but note (§8) the *runtime* matcher no longer reads IPEDS files directly; IPEDS is
  an **offline** input that produced `dim_institution.csv`.
- **Coverage: e-file only**, hard floor ~filing year 2011. Rising year-over-year counts are the
  e-file adoption curve (mandatory ~2020 under the Taxpayer First Act), not missing data. Paper
  returns before then don't exist in machine-readable form anywhere.

Scale (2026-06-04 index): ~7.34M total returns; **in-scope 990 + 990-PF ≈ 5.04M**.

---

## 6. The identity model — `canonical_id`

`canonical_id` is the single identity key each grant resolves to. **One row per real institution.**
All institution attributes come from the single join
`grantsdb.canonical_id → dim_institution.canonical_id`.

| Case | `canonical_id` |
|---|---|
| Institution in IPEDS | the IPEDS **`unitid`** as a string (e.g. `196866`) |
| No IPEDS unitid (foreign, or US non-IPEDS: small Bible/grad schools, etc.) | surrogate **`SG#####`** |
| Payee is not higher-ed | sentinel **`NA`** |
| A college but unidentifiable from the record | sentinel **`NCI`** |
| Uncertain / likely-in-dim under a name variant | **blank** → review queue |

**Surrogate rule:** `foreign` and `non-IPEDS-US` share **one** `SG#####` counter; a new one mints
`SG{max+1}`.

**Why not EIN as identity:** EIN is many-per-institution and absent on 990-PF. It is never stored as
identity. (In the current dim-only design it isn't even used as a resolver at runtime — see §8.)

**Precision rules (do not violate):** never collapse system campuses (Montana State ≠ Montana State
Billings); disambiguate same-named schools by state (Southwestern College CA≠NM≠FL); cross-state name
propagation only for nationally-unique single-token names (Harvard, Tufts), never common tokens
(Mercy, Union); a payee that is a foundation/fund/alumni-assoc/named-scholarship *of* a school
resolves **to the school**.

**`dim_institution` schema** (one row per `canonical_id`, 6 columns):

| Column | Meaning |
|---|---|
| `canonical_id` | identity: `unitid` / `SG#####` / `NA` / `NCI` — **is** the IPEDS unitid directly, no separate column |
| `grantee_normalized` | canonical display name; foreign names carry a `(Foreign)` suffix |
| `state` | home state (blank for foreign/NA). **US territories use the full formal name** (`Puerto Rico`, `Guam`, `U.S. Virgin Islands`, `American Samoa`, `Northern Mariana Islands`), never the postal abbreviation. |
| `country` | institution's country |
| `entity_class` | `IPEDS-US` \| `non-IPEDS-US` \| `foreign` \| `NCI` |
| `city` | home city |

There is deliberately **no `ipeds_unitid` column**: `build_dim()` derives it at load time directly
from `canonical_id` (blank only for `SG#####` surrogates and the `NA`/`NCI` sentinels), so the file
carries no redundant field.

---

## 7. Stages 1–2 — Acquire & extract

A **fused per-file pipeline** (see §4). Order of operations, per return:

1. **Resolve & cache the index** once (newest `all_years`, cached `_index_*.csv`; `--refresh-index`
   forces a clean re-fetch).
2. **Load the skip-list** `ref_xml_processed.csv` (keyed on XML filename).
3. **Stream the index once**, keeping only `FormType ∈ {990, 990PF}`; honor `--limit-*`, `--years` /
   `--years-exclude`.
4. **Skip anything already in the skip-list** — *load-bearing:* a return recorded here is never
   re-mined (see §12 risks).
5. **Download** the XML into `form990_xml/<year>/` or `form990pf_xml/<year>/` under `--data-dir`.
6. **Parse** it (both e-file schema generations), applying the higher-ed recipient filter.
7. **Record** the filename to the processed-log **before any delete** (kill-safe).
8. **Keep or delete**: keep the XML if it has ≥1 higher-ed grant, else delete (already recorded).

**Support-org flags — flag, don't drop.** A dollar can double-count when a foundation grants to a
university's support org, which then grants to the university and reports it on its own return.
Rather than exclude these filers (the old design), the pipeline **flags**, on two different grains:

| Flag | Lives on | Grain | Set when… |
|---|---|---|---|
| `funder_support_org` | `ref_xml_processed` (the upload deliverable) | per return / per filer | any of the 7 Schedule A boxes is checked on the **filer's own** return: `CollegeOrganizationInd`, `SupportingOrganization509a3Ind`, `SupportingOrganization509a3`, `HospitalInd`, `Hospital170b1Aiii`, `SchoolInd`, `School170b1Aii` |
| `grantee_support_org` | internal working data only (**not** in the `grantsdb` upload) | per grant | the **grantee** name contains `"foundation"` |

Filter `WHERE funder_support_org = 'False'` (joining `grantsdb` to `ref_xml_processed` on `xml_file`)
at query time to drop filer-side double-counts. *(This replaced the former
`public_charity_exclusions.csv` EIN exclusion list, which is no longer used.)*

**Higher-ed recipient filter** (`is_college_or_university`): three tiers — exclude-keywords →
include-keywords ("college", "university", "institute of technology", "polytechnic", …) → an
include-names allow-list (Juilliard, Cooper Union, Caltech, …).

**Schema knowledge (critical, easy to break):** two XML generations under
`http://www.irs.gov/efile` with **different tag names** (2015+ v5.x vs pre-2015 v2.x). The parser
tries every spelling for each field; adding a field means adding **all** known spellings or pre-2015
returns silently blank out. Form type comes from *which download tree the file is in*, not from
parsing `ReturnTypeCd`.

**Output — `_parsed_<stamp>.csv`, raw grant rows:** form type, xml file, filer name, filer EIN,
return year, grantee, recipient EIN, addr1/2, city, state, zip, country, address type, amount,
purpose.

---

## 8. Stage 3 — Resolve grantee → `canonical_id`

**The single reference is `dim_institution.csv`.** The runtime matcher does **not** read IPEDS,
alias, or EIN-crosswalk files — those were consumed offline to build the dimension. Resolution is
strongest-signal-first:

1. **Deterministic (dim):** exact match on `norm(name)|state`, then nationally-unique name, then a
   **guarded geo-fuzzy** within the grant's state (rapidfuzz token-sort and token-set, with a
   uniqueness margin) to catch typos. Resolves the clean majority (~60% at ~96% precision).
2. **Haiku (residual):** for what dim can't match, the LLM classifies the payee as
   `us_university` / `foreign` / `not_applicable` and returns a clean institution name, judged by the
   institution's **home campus** (not the grant's mailing address). Hard guards: never flag famous US
   schools foreign; require a real foreign address signal; distrust an LLM name that diverges from the
   input unless the input is an affiliate/subunit.
3. **Map back / mint conservatively:** the LLM's name is re-matched to dim. Found → use it. Only
   *resembles* an existing dim entry (variant / campus-ambiguous) → **review** (blank `canonical_id`),
   never a duplicate. A genuinely-new institution mints a new `SG#####`.

**Design tradeoff (brief the successor):** `dim_institution.csv` holds one canonical name per
institution (no alias table), so a larger share of grantees fall through to the LLM than a rich
alias-based matcher would, and some variant/campus-ambiguous names land in **review** for offline
curation. This is intentional — it keeps the runtime dependency surface at **two files**. The natural
evolution, if LLM volume or review load grows, is to reintroduce an offline-curated
`alias(match_key → canonical_id)` table so more of the tail resolves deterministically.

Outputs: `_matched_<stamp>.csv` (internal working file, wider column set for debugging) and
`dim_institution_incremental_<stamp>.csv` (new institutions discovered this run, upload-ready
6-column format — see §6).

---

## 9. Stage 4 — Tag grant purpose

Add a `tag` from the free-text purpose **only** (not funder/recipient/amount/year). Tag is a
semicolon-joined subset of exactly ten labels; the eight specific labels combine freely, the two
generic ones are mutually exclusive with everything:

`stem · hass · athletics · finaid · research · professional · studentlife · capital · general · other`

**Cross-cutting rules:** exclusivity is absolute (`general`/`other` never co-occur); multi-tag
liberally (nursing scholarship = `stem;finaid;professional`); read **topic not audience**; own the
opinionated calls (journalism always `professional`; health professional schools `stem;professional`;
bare "research" → `research`); a lone weak word doesn't pull a row out of `general`.

**Implementation:** classify **once per unique purpose string** (a ~1M-grant dataset collapses to
~150K unique strings), join back by exact string. Haiku 4.5 at temperature 0 with a **byte-identical
cached system prompt** (inlined into the script). Post-processing (`canonize`) maps hallucinated
labels via a fixups table, enforces exclusivity and canonical order; empty purpose → `other` with no
API call. Keep the system prompt ≥4096 tokens so prompt caching engages.

**API efficiency:** both LLM stages default to the async **Message Batches API (≈50% cheaper)** plus
prompt caching. `--sync` switches to synchronous calls (seconds vs minutes latency, full price) for
small/interactive runs.

---

## 10. Data model (the three tables)

**`grantsdb`** (fact, upload deliverable) — **14 columns**, one per grant:
`xml_file, grantee, grantee_addr1, grantee_addr2, grantee_city, grantee_state, grantee_zip,
grantee_country, grantee_addr_type, grant_amount, grant_purpose, canonical_id, tag, uid`
- `uid` — emitted **blank**; the DB assigns it (IDENTITY / SERIAL / AUTO_INCREMENT).
- No `filer_name` / `filer_ein` / `return_year` / `form_type` here — join to `ref_xml_processed` on
  `xml_file` for those.
- No `ipeds_unitid` here — join to `dim_institution` on `canonical_id`.
- `canonical_id` — §6. `tag` — §9.
- The internal working file from the `match` stage (`_matched_<stamp>.csv`) carries the parsed grant
  columns (`filer_name`, `filer_ein`, `return_year`, `form_type`, etc.) plus a per-grant
  `ipeds_unitid` and `canonical_id` — it does **not** yet have `tag`, `uid`, or `grantee_support_org`.
  Those are computed in-memory during the `tag` stage; only the 14 columns above make it into the
  actual upload deliverable.

**`dim_institution`** — schema in §6 (6 columns, no `ipeds_unitid`).

**`ref_xml_processed`** — **7 columns**:
`xml_file, timestamp, form_type, filer_name, filer_ein, return_year, funder_support_org`
- `funder_support_org` is `"True"`/`"False"` — see §7.

**Loading (append; DB assigns uid; skip the blank uid column on grants):**

```sql
COPY grantsdb (xml_file, grantee, grantee_addr1, grantee_addr2, grantee_city, grantee_state,
  grantee_zip, grantee_country, grantee_addr_type, grant_amount, grant_purpose, canonical_id, tag)
FROM 'grantsdb_incremental_<stamp>.csv' CSV HEADER;
COPY dim_institution     FROM 'dim_institution_incremental_<stamp>.csv'   CSV HEADER;  -- upsert on canonical_id
COPY ref_xml_processed   FROM 'ref_xml_processed_incremental_<stamp>.csv' CSV HEADER;  -- append
```

**Query-time dedupe** (ingestion no longer dedupes amended re-filings): keep the latest filing per
`(filer_ein, return_year)` — the `ObjectId` in `xml_file` encodes recency; join to `ref_xml_processed`
for `filer_ein`/`return_year` since they're no longer on `grantsdb` directly:

```sql
SELECT * FROM (
  SELECT g.*, r.filer_ein, r.return_year,
         ROW_NUMBER() OVER (PARTITION BY r.filer_ein, r.return_year ORDER BY g.xml_file DESC) rn
  FROM grantsdb g JOIN ref_xml_processed r ON r.xml_file = g.xml_file
) t WHERE rn = 1;
```

---

## 11. Operations / runbook

**Environment:** Python 3.8+; `pip install pandas rapidfuzz anthropic`; `ANTHROPIC_API_KEY` for the
`match`/`tag` stages only. Both data files (`dim_institution.csv`, `ref_xml_processed.csv`) live
beside the script. Reference machine: 6c/12t, ~400 Mbps.

| Task | Command |
|---|---|
| Incremental (normal cadence) | `python irs-990-pipeline.py --stamp <YYYYMMDD>` |
| Acquire only (no key) | `python irs-990-pipeline.py --steps download,parse --stamp <s>` |
| Resolve + tag (needs key) | `python irs-990-pipeline.py --steps match,tag --stamp <s>` |
| **Faster full run** | `python irs-990-pipeline.py --parallel-years 4 --workers 32 --stamp <s>` |
| Bounded smoke test | `python irs-990-pipeline.py --limit-990 500 --limit-990pf 500 --stamp test` |
| Force a fresh index | add `--refresh-index` |

Everything is **resumable** — re-running skips anything already in the skip-list or on disk; killing
mid-run is safe (the return is recorded before any delete). Always pass an explicit `--stamp` if you
split stages, so `match,tag` can find the `download,parse` intermediates. Load the three increments to
SQL after each run so the next run skips what's already loaded.

---

## 12. Performance, quality, audit & risks

**Throughput (acquisition).** Files are small (~150 KB); a single process at the default 24 workers
is usually **latency/concurrency-bound, not bandwidth-bound** (≈ `workers ÷ per-request-latency`).
Levers: raise `--workers` while the heartbeat's **`failed`** stays ~0 (rising `failed` = S3
throttling → back off); `--parallel-years N` multiplies connections *and* parse cores. On the
reference box, `--parallel-years 4 --workers 32` targets the bandwidth ceiling. Beyond ~4 processes on
one machine, shard `--years` across **separate machines** for real bandwidth scaling. Rough full-run
wall-clock is bandwidth-bound and tracks the **total** download count (~5M), not survivors.

**Cost (LLM).** Only `match` (institution residual) and `tag` (unique purposes) hit the API, on Haiku,
scaling with **unique** unresolved strings — not grant count. Batch API + caching keep this to cents
for incremental runs.

**Audit (both LLM stages).** Draw a stratified sample across decision sources, judge by hand, report
accuracy **by source** so a weak stage is visible. Resolution sanity: among strings needing
normalization, expect ≈ ¾ specific US, ~1/10 NA, ~1/10 foreign, few % NCI. Tagging converges around
high-80s% human-model agreement; most rows are `general`/`finaid`, `stem`/`research`/`hass` single
digits, `athletics`/`capital`/`studentlife` ≤ ~1%.

**Integrity invariants.** Row count preserved through match/tag; every `canonical_id` is an exact
member of `dim_institution`; every `tag` is a valid label combination (exclusivity holds);
`canonical_id == ipeds unitid` on `IPEDS-US` rows (there's no separate column to drift from it).

**Risks / traps to brief the successor on:**
- **Skip-list is load-bearing.** A return recorded in `ref_xml_processed` is never re-mined. If a
  filename lands there without its grants reaching `grantsdb`, that grant-maker is silently absent.
  *(This exact failure hit Howard Hughes Medical Institute — all years missing though its parser
  output was fine. Fix was to remove its filenames from the skip-list, re-run, verify.)* Wire a
  periodic **coverage audit**: are known large university funders present as filers?
- **Review bucket is intentional, not a bug.** Blank `canonical_id` = "likely in dim under a variant,
  don't duplicate." Curate these offline and (optionally) feed an alias table (§8).
- **`dim_institution.csv` must stay in the 6-column format** (no `ipeds_unitid`). If a downstream
  tool (e.g. a SQL export) ever re-syncs a 7-column copy back over the local file, `build_dim()`
  handles it fine either way — but don't let the two formats diverge in what they *mean*.
- **Zip/EIN leading zeros** vanish if any CSV is re-read without forcing string dtype — always read as
  string.
- **Parser tag variants.** When adding a parsed field, add **all** schema spellings or pre-2015
  returns silently blank out. Re-run a pre-2015 + a 2024 sample and confirm row counts don't drop.
- **LLM non-determinism** at the margin — keep the deterministic guards; the `NA`/blank/review buckets
  are the honest floor.
- **`--append-file` / parallel shards:** never point two writers at the same record file. Under
  `--parallel-years` each shard writes its own `_yearshard_rec_*.csv`, merged into the skip-list at
  the end — this is handled automatically; don't hand-run overlapping shards at the same file.

**Limitations (honest floor):** `NCI` can't be closed from text alone; institutions absent from both
IPEDS and the curated dimension mint an `SG` and need offline promotion to a real unitid; tags rest on
self-reported purpose text, so vague operating language tags `general`; coverage is e-file only.

---

## 13. Repository & artifact inventory

**GitHub** (`aeicfau/source`): `990-grant-pipeline/irs-990-pipeline.py` is the current, maintained
script. Its two ancestors (`990pf-downloader/download_990pf.py`,
`990pf-parser/irs_parse_xml_consolidated.py`) are fully superseded and have been removed from the
repo. `grant-taggers/` holds prior tagging-methodology docs.

**Local machine (`C:\form990\`)**, what the script actually needs beside it:

| Path | Role |
|---|---|
| **`irs-990-pipeline.py`** | THE script — self-contained: download + parse + match + tag, all stages |
| **`dim_institution.csv`** | the canonical institution dimension — the **only** matching reference (§6), 6-column format |
| **`ref_xml_processed.csv`** | processed-ledger / download skip-list, 7-column format |
| `CLAUDE.md` | dataset facts + schema knowledge (measured counts, XML tag variants) |
| `incremental/` | per-run intermediate + output files (`_parsed_`, `_matched_`, the 3 `*_incremental_` CSVs) |
| `_index_*.csv` | cached ~3.2 GB master index (machine-local; do not commit) |
| `form990_xml/`, `form990pf_xml/` | downloaded grant-bearing XML (only survivors kept) |

The script has **no local module imports** — the matcher, the LLM prompts, and the download/parse
engine are all inlined. Its runtime data dependencies are exactly the two files in bold.

---

## 14. What changed from the previous architecture (for anyone who saw the old version)

| Area | Old (multi-file) | Prior consolidation (irs-990-pipeline.py, July 2026) | Current (2026-07-15) |
|---|---|---|---|
| Code | `irs_990_pipeline.py` + `matcher.py` + `incremental_990.py` + `tagger.py` | one file, all inlined | same |
| Matching reference | `ipeds_canonical.csv` + `vwMatchIPEDS.csv` + `grantee_key.csv` | **`dim_institution.csv` only** | same |
| `dim_institution.csv` schema | n/a | 7 columns incl. `ipeds_unitid` | **6 columns, `ipeds_unitid` dropped** — `canonical_id` derives it directly |
| US territory names | n/a | postal abbreviation (`PR`, `GU`, …) | **full formal name** (`Puerto Rico`, `Guam`, …) |
| Double-count flag | excluded via `public_charity_exclusions.csv` | one `support_org` flag on `grantsdb` | **split**: `funder_support_org` (filer-side) moved to `ref_xml_processed`; `grantee_support_org` (name-based) is internal-only, no longer on the upload |
| `ref_xml_processed.csv` schema | n/a | 5 columns | **7 columns** — added `return_year`, `funder_support_org` |
| `grantsdb` upload columns | n/a | 21 (full internal set) | **14** — trimmed to grant-level fields only; `filer_name`/`filer_ein`/`return_year`/`form_type`/`ipeds_unitid` moved off, join instead |
| Dedupe | at ingestion (`dedupe.csv`, `ReturnTs`) | deferred to the query layer | same |
| `uid` | assigned by the script | left blank, DB assigns | same |
| LLM transport | sync (resolution) / batch (tagging) | Batch API + caching by default, `--sync` to opt out | same |

---

## 15. Handoff checklist / open items

- [ ] Provision `ANTHROPIC_API_KEY` for the new owner (**rotate** any key used in development).
- [ ] Confirm the SQL loaders for the three increments (append `grantsdb`/`ref_xml_processed`, upsert
      `dim_institution` on `canonical_id`; skip the blank `uid` column on grants).
- [ ] Stand up the **review queue** (blank-`canonical_id` grantees grouped with sample name, count,
      total $) as the standing curation surface.
- [ ] Wire a periodic **coverage audit** (known large university funders present as filers?) to catch
      skip-list gaps like the HHMI case.
- [ ] Decide whether to reintroduce an offline **alias table** (§8) if LLM volume / review load grows.
- [ ] Keep the inlined tagging prompt and matcher guards under version control; re-validate against
      the held-out hand-fixed set before shipping a change to either.

*End of PRD.*
