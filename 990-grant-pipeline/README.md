# irs-990-pipeline

A single, self-contained Python script that turns raw IRS **Form 990 / 990-PF** e-file XML into a
clean, incremental table of **grants made to U.S. colleges and universities** — each grant resolved
to a canonical institution (`canonical_id`) and tagged by purpose.

It downloads new filings, extracts college/university grants, resolves each grantee against a
canonical institution dimension (with an LLM for the hard tail), tags the grant's purpose, and emits
three **incremental CSVs** ready to load into a SQL database.

> One file, two data dependencies, no build step. `python irs-990-pipeline.py`.

---

## Table of contents

- [What it produces](#what-it-produces)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Dependencies (only two data files)](#dependencies-only-two-data-files)
- [Quick start](#quick-start)
- [The four stages](#the-four-stages)
- [The `canonical_id` identity model](#the-canonical_id-identity-model)
- [Matching methodology](#matching-methodology)
- [Support-org flags (double-count signal)](#support-org-flags-double-count-signal)
- [Grant-purpose tagging](#grant-purpose-tagging)
- [Data schemas](#data-schemas)
- [Output & loading into SQL](#output--loading-into-sql)
- [Index handling](#index-handling)
- [CLI reference](#cli-reference)
- [Cost & performance](#cost--performance)
- [Design notes & limitations](#design-notes--limitations)
- [Troubleshooting](#troubleshooting)

---

## What it produces

Three append-only increments per run (in `--work-dir`, default `./incremental/`):

| File | Loads into | Grain |
|---|---|---|
| `grantsdb_incremental_<stamp>.csv` | your **grants** table | one row per college/university grant |
| `dim_institution_incremental_<stamp>.csv` | your **institution** dimension | new institutions discovered this run |
| `ref_xml_processed_incremental_<stamp>.csv` | your **processed-log** table | one row per XML processed this run |

Every grant row carries a stable `canonical_id` and a topical `tag`. The **funder**-level double-count
signal (is the filer itself a college/school/hospital/support-org?) lives on `ref_xml_processed`,
joined by `xml_file` — see [Support-org flags](#support-org-flags-double-count-signal).

---

## How it works

```
 IRS e-file XML (GivingTuesday 990 Data Lake, public S3)
        │
        ▼
 [1] download   skip anything already processed; fetch everything else
        ▼
 [2] parse      keep college/university grants; flag the filer; record the return
        ▼
 [3] match      resolve grantee → canonical_id  (dim_institution.csv, then Haiku for the residual)
        ▼
 [4] tag        classify grant purpose → tag     (Haiku)
        ▼
 grantsdb / dim_institution / ref_xml_processed  increments  →  SQL
```

The four stages are **independent** and chained through per-run intermediate files keyed by
`--stamp`, so you can run them together or split them across machines (e.g. acquire on one box,
run the paid LLM stages on another).

**Fused acquisition.** When `download` and `parse` are requested **together** (the default), they
run **fused**: each worker downloads one return and parses it immediately, concurrently — parsing
overlaps downloading, and non-grant XML is deleted inline, so disk stays small and there's no
"download everything, then parse everything" phase. Run `--steps download` alone to fetch to a
manifest without parsing (e.g. to parse later on another machine); run `--steps parse` alone to
parse from that manifest. All long-running stages print a **live heartbeat** (rows scanned/sec,
dispatched, downloaded, kept, deleted, **failed**; or files parsed).

**Parallel acquisition (`--parallel-years N`).** A single process is throughput-limited: at low
worker counts you're **latency/concurrency-bound** (each small file costs a network round-trip), and
Python's GIL serializes XML parsing on one core. `--parallel-years N` fans `download`+`parse` out
into **N separate processes split by tax year** — N× the concurrent connections *and* N cores of
parsing — then merges their outputs and runs `match`+`tag` **once**. Years are balanced by volume;
one shard is a gap-free catch-all so coverage has no holes. Each shard writes its **own** record file
(no write races) and prefixes its heartbeat with `[y0]`…`[yN]`. See
[Cost & performance](#cost--performance) for tuning. This is also the unit of **cross-machine**
sharding: run disjoint `--years` on separate boxes (separate connections) for real bandwidth scaling.

---

## Requirements

- **Python 3.8+**
- `pip install pandas rapidfuzz anthropic`
- **`ANTHROPIC_API_KEY`** environment variable — required only for the `match` and `tag` stages
  (the `download` and `parse` stages need no key).
- Network access to the public S3 bucket `gt990datalake-rawdata` (no AWS account needed).

No other setup: the script is a single file with no local module imports.

---

## Dependencies (only two data files)

Both live **beside the script** by default (`SCRIPT_DIR`), overridable by flag:

| File | Flag | Purpose |
|---|---|---|
| **`dim_institution.csv`** | `--dim` | the **only** matching reference — the canonical institution dimension |
| **`ref_xml_processed.csv`** | `--existing` | the skip-list / processed-log |

Everything else is either **inlined** into the script (the matcher, the LLM prompts) or
**auto-fetched** (`_index_*.csv`, the ~3.2 GB master index, cached in `--data-dir`). `uid` is **not**
assigned by the script — it is left blank for the database to auto-assign on insert.

> **`dim_institution.csv` has no `ipeds_unitid` column.** `canonical_id` *is* the IPEDS unitid for
> every real institution (see [identity model](#the-canonical_id-identity-model)) — a separate column
> would just duplicate it. If you ever see a `KeyError: 'ipeds_unitid'`, you have an old-format file;
> `build_dim()` no longer reads or requires that column.

---

## Quick start

```bash
# 1) acquire (no API key needed) — downloads everything not already in ref_xml_processed.csv
python irs-990-pipeline.py --steps download,parse --stamp 20260707

# 2) resolve + tag (needs the key) — on the same batch/stamp
export ANTHROPIC_API_KEY=sk-ant-...
python irs-990-pipeline.py --steps match,tag --stamp 20260707

# ...or all four at once
python irs-990-pipeline.py --stamp 20260707
```

Faster full run — fan acquisition out across cores by tax year, then match+tag once:

```bash
python irs-990-pipeline.py --parallel-years 4 --workers 32 --stamp 20260707
```

Bounded test run (caps new downloads per form type):

```bash
python irs-990-pipeline.py --limit-990 500 --limit-990pf 500 --stamp test
```

Always pass an explicit `--stamp` if you split stages, so `match,tag` can find the `download,parse`
intermediates.

---

## The four stages

> By default `download` and `parse` run **fused** (per file, concurrently — see
> [Fused acquisition](#how-it-works)). The two are described separately below for when you split
> them with `--steps download` / `--steps parse`.

### 1. `download` — acquire (network only)
- Loads the skip-list `ref_xml_processed.csv` (keyed on XML filename).
- Streams the master index, keeping only `FormType ∈ {990, 990PF}`.
- **Skips** anything already in the skip-list; **downloads everything else** — no caps by default,
  and **no dedupe** (deduping amended re-filings is deferred to the query layer).
- In fused mode, parses each file immediately and keeps only grant-bearing XML. In `download`-only
  mode, writes each XML into `form990_xml/<year>/` or `form990pf_xml/<year>/` under `--data-dir`
  plus a `_manifest_<stamp>.csv` for a later `parse`.

### 2. `parse` — extract + flag (CPU only)
- Parses each XML (both e-file schema generations), keeping only grants whose recipient passes a
  college/university filter.
- Sets the return-level `filer_support_org` flag (see
  [Support-org flags](#support-org-flags-double-count-signal)). Nothing is dropped for double-count
  reasons — it is **tagged**.
- Records each return to `ref_xml_processed.csv` and writes `_parsed_<stamp>.csv`.
- Deletes downloaded XML that yielded no college grant (keep with `--keep-xml`).

### 3. `match` — resolve to `canonical_id` (Haiku)
- Loads `dim_institution.csv` (the only reference).
- Resolves each unique grantee **deterministically** against dim (exact normalized-name + state,
  then a guarded geo-fuzzy); the residual goes to **Haiku**, which classifies it and returns a clean
  name that is mapped back to dim or minted as a new institution.
- Writes `_matched_<stamp>.csv` and `dim_institution_incremental_<stamp>.csv` (new institutions only).

### 4. `tag` — classify purpose (Haiku)
- Classifies each **unique** `grant_purpose` string into a topic taxonomy (see
  [tagging](#grant-purpose-tagging)) and joins back by exact string.
- Emits the final `grantsdb_incremental_<stamp>.csv`. `uid` is left blank (the DB assigns it).

---

## The `canonical_id` identity model

`canonical_id` is the single identity key each grant resolves to — **one row per real institution** —
and it *is* the IPEDS unitid directly for every real institution; there is no separate ID column.

| Case | `canonical_id` |
|---|---|
| Institution in IPEDS | the IPEDS **`unitid`** (string, e.g. `171100`) |
| No IPEDS unitid (foreign, or US non-IPEDS) | a surrogate **`SG#####`** |
| Payee is not a higher-ed institution | sentinel **`NA`** |
| A college but unidentifiable | sentinel **`NCI`** |
| Uncertain / likely-in-dim under a variant | **blank** → review queue |

`foreign` and `non-IPEDS-US` share a single `SG#####` counter (a new one mints `max+1`). All
institution attributes come from a single join `grant.canonical_id → dim_institution.canonical_id`.

**Why not EIN as identity:** recipient EIN is many-per-institution (a school appears under its
regents', foundation's, and med-center's EINs) and **990-PF discloses no recipient EIN at all**
(~70% of volume). EIN is never the identity.

---

## Matching methodology

The script resolves against **`dim_institution.csv` only** (it does not read IPEDS/alias/crosswalk
files). Resolution is strongest-signal-first:

1. **Deterministic (dim):** exact match on `norm(name)|state`, then national-unique name, then a
   **guarded geo-fuzzy** within the grant's state (token-sort and token-set, with a uniqueness
   margin) to catch typos. This resolves the clean majority.
2. **Haiku (residual):** for what dim can't match, an LLM classifies the payee as
   `us_university` / `foreign` / `not_applicable` and returns a clean institution name, judged by the
   institution's **home campus** (not the grant's mailing address). Guards prevent flagging famous US
   schools as foreign and prevent trusting a name the model diverged toward.
3. **Map back / mint conservatively:** the LLM's name is re-matched to dim. If found → use it. If it
   only *resembles* an existing dim entry (a name variant / campus-ambiguous case) → **review**
   (blank `canonical_id`), never a duplicate. Only a genuinely-new institution mints a new `SG#####`.

> **Tradeoff of the two-file design:** `dim_institution.csv` holds one canonical name per
> institution (no aliases), so a larger share of grantees fall through to the LLM than a rich
> alias-based matcher would, and some variant/campus-ambiguous names land in **review** for offline
> curation. This is intentional — it keeps the runtime dependency surface at two files and matches
> the "deterministic dim match + offline curation" design.

New IPEDS unitids are **not** discovered at runtime (that requires the offline IPEDS crosswalk);
genuinely new institutions therefore mint an `SG#####` surrogate, which offline curation can later
promote to a real unitid.

---

## Support-org flags (double-count signal)

Grants can double-count a dollar (e.g. a foundation grants to a university's support org, which then
grants to the university and reports it on its own return). Rather than drop these, the script
**flags** them — but the two flags live on **different tables** now:

| Flag | Table | Grain | Set when… |
|---|---|---|---|
| `funder_support_org` | `ref_xml_processed` | per return (per filer) | any of 7 Schedule A boxes is checked on the **filer's own** return: `CollegeOrganizationInd`, `SupportingOrganization509a3Ind`, `SupportingOrganization509a3`, `HospitalInd`, `Hospital170b1Aiii`, `SchoolInd`, `School170b1Aii` |
| `grantee_support_org` *(internal only — not in the upload file)* | working data during a run | per grant | the **grantee** name contains `"foundation"` |

To exclude filer-side double-counts in reporting, join `grantsdb.xml_file → ref_xml_processed.xml_file`
and filter `WHERE funder_support_org = 'False'`.

---

## Grant-purpose tagging

Each grant gets a `tag`: a semicolon-joined subset of ten labels, classified from the purpose text
only (not funder/recipient/amount). Eight specific labels combine freely; two generic labels are
mutually exclusive with everything.

```
stem · hass · athletics · finaid · research · professional · studentlife · capital · general · other
```

Key rules: exclusivity is absolute (`general`/`other` never co-occur with anything); multi-tag when
several domains are present (a nursing scholarship is `stem;finaid;professional`); read the **topic
funded, not the audience served**; journalism is always `professional`; health professional schools
are `stem;professional`; a lone weak word does not pull a row out of `general`. Classification runs
once per **unique** purpose string (Haiku, temperature 0, cached prompt) and is joined back by exact
string. Output is post-processed to enforce the integrity rules: only the ten labels, exclusivity,
known hallucinations remapped, empty/unclassifiable → `other`.

> **Scale note.** This script classifies unique purposes with **synchronous** API calls, which is
> ideal for incremental batches (a handful to a few thousand unique strings per run). For a
> one-shot **full backfill** (~150K unique strings), use the async **Message Batches API** instead
> (~20× cheaper, same prompt) — the taxonomy/prompt is identical; only the transport differs.

---

## Data schemas

**`dim_institution.csv`** (the matching reference, 6 columns)

| Column | Meaning |
|---|---|
| `canonical_id` | identity: `unitid` / `SG#####` / `NA` / `NCI` — **is** the IPEDS unitid directly |
| `grantee_normalized` | canonical display name (foreign names carry a `(Foreign)` suffix) |
| `state` | home state (blank for foreign/NA). **US territories spell out the full formal name** — `Puerto Rico`, `Guam`, `U.S. Virgin Islands`, `American Samoa`, `Northern Mariana Islands` — never the postal abbreviation. |
| `country` | institution's country |
| `entity_class` | `IPEDS-US` \| `non-IPEDS-US` \| `foreign` \| `NCI` |
| `city` | home city |

**`ref_xml_processed.csv`** (skip-list / processed-log, 7 columns)

```
xml_file, timestamp, form_type, filer_name, filer_ein, return_year, funder_support_org
```

`funder_support_org` is `"True"`/`"False"` — see [Support-org flags](#support-org-flags-double-count-signal).

**`grantsdb_incremental_<stamp>.csv`** (grants deliverable, 14 columns)

```
xml_file, grantee, grantee_addr1, grantee_addr2, grantee_city, grantee_state, grantee_zip,
grantee_country, grantee_addr_type, grant_amount, grant_purpose, canonical_id, tag, uid
```

- `uid` is emitted **blank** — the DB assigns it (IDENTITY / SERIAL / AUTO_INCREMENT).
- `filer_name`, `filer_ein`, `return_year`, and `form_type` are **not** on this file — join to
  `ref_xml_processed` on `xml_file` for those.
- There is no `ipeds_unitid` column here either — join to `dim_institution` on `canonical_id`.

The internal working file for a run (`_matched_<stamp>.csv`, produced by the `match` stage before
`tag` runs) carries the parsed grant columns plus a per-grant `ipeds_unitid` and `canonical_id` — it
does **not** yet have `tag`, `uid`, or `grantee_support_org`; those are computed in-memory during the
`tag` stage and only the 14 columns above are written to the upload deliverable.

---

## Output & loading into SQL

```sql
-- grants: append; DB assigns uid
COPY grantsdb (xml_file, grantee, grantee_addr1, grantee_addr2, grantee_city, grantee_state,
  grantee_zip, grantee_country, grantee_addr_type, grant_amount, grant_purpose, canonical_id, tag)
FROM 'grantsdb_incremental_<stamp>.csv' CSV HEADER;   -- note: skip the blank uid column

-- new institutions: insert (or upsert on canonical_id)
COPY dim_institution FROM 'dim_institution_incremental_<stamp>.csv' CSV HEADER;

-- processed log / skip-list: append
COPY ref_xml_processed FROM 'ref_xml_processed_incremental_<stamp>.csv' CSV HEADER;
```

**Query-time dedupe** (since ingestion no longer dedupes amended re-filings): pick one return per
filer-year — join to `ref_xml_processed` for `filer_ein`/`return_year`, e.g.

```sql
-- keep the latest filing per (filer_ein, return_year); ObjectId in xml_file encodes recency
SELECT * FROM (
  SELECT g.*, r.filer_ein, r.return_year,
         ROW_NUMBER() OVER (PARTITION BY r.filer_ein, r.return_year ORDER BY g.xml_file DESC) rn
  FROM grantsdb g JOIN ref_xml_processed r ON r.xml_file = g.xml_file
) t WHERE rn = 1;
```

Keep both `ref_xml_processed` (skip-list) and any dedupe view current between runs so the next run
skips what's already loaded.

---

## Index handling

**Every run checks for the latest index by default.** `resolve_index()` calls `find_latest_index()`,
which lists the bucket and returns the newest `all_years` index (by filename date, then last-modified).
The cache path is derived from that key:

- If a **newer** index was published → it downloads and uses it automatically.
- If the newest **equals** the cached copy → it reuses the cache (resumable download no-ops).

Overrides: `--index-key <key>` pins a specific index; `--index-file <path>` uses a local file
directly (both skip the latest-check). `--refresh-index` deletes the cached copy for the resolved key
and forces a clean full re-download.

---

## CLI reference

| Flag | Default | Meaning |
|---|---|---|
| `--steps` | `download,parse,match,tag` | which stages to run (comma-separated) |
| `--stamp` | today (`YYYYMMDD`) | run id; keys intermediate + output files |
| `--data-dir` | script dir | index cache + XML download trees |
| `--work-dir` | `<script dir>/incremental` | intermediate + output files |
| `--dim` | `<script dir>/dim_institution.csv` | matching reference |
| `--existing` | `<script dir>/ref_xml_processed.csv` | skip-list |
| `--year-field` | `tax` | folder by `tax` or `submitted` year |
| `--years Y…` | (all) | restrict to specific tax years (cross-machine sharding) |
| `--years-exclude Y…` | (none) | download every in-scope year **except** these (catch-all shard) |
| `--parallel-years N` | `1` | fan `download`+`parse` into N processes split by tax year, then merge and `match`+`tag` once |
| `--append-file` | = `--existing` | record processed filenames here instead of `--existing` (per-shard, no write races) |
| `--limit-990` / `--limit-990pf` | `0` (unlimited) | cap new downloads per form type (per shard under `--parallel-years`) |
| `--workers` | `24` | parallel download threads **per process** |
| `--overwrite` | off | re-download XML even if present |
| `--keep-xml` | off | keep non-grant XML instead of deleting |
| `--sync` | off | use synchronous API calls instead of the cheaper-but-slower Batch API |
| `--index-file` / `--index-key` / `--refresh-index` | auto | override / refresh the index |

---

## Cost & performance

- **Acquisition throughput.** Files are small (~150 KB), so a single process at the default 24
  workers is usually **latency/concurrency-bound, not bandwidth-bound** — throughput ≈
  `workers ÷ per-request-latency` (e.g. 24 ÷ ~0.25 s ≈ ~95 files/s), which can sit well under your
  pipe's ceiling. Two levers raise it:
  - **`--workers N`** — more concurrent connections. Raise it while the heartbeat's **`failed`**
    stays ~0; if `failed` climbs, S3 is throttling the anonymous connections — back off.
  - **`--parallel-years N`** — N processes escape the single-core parse ceiling (the GIL) and
    multiply connections. On a 6-core/400 Mbps box, `--parallel-years 4 --workers 32` (≈128
    connections, 4 parse cores) pushes toward the bandwidth ceiling. Going past ~4 processes on one
    machine mostly just re-scans the index without adding bandwidth — to go faster, shard `--years`
    across **separate machines** (separate connections). Incremental runs (new filings since last
    run) are small and rarely need this.
- **LLM cost** is on `match` (the institution residual) and `tag` (unique purposes only), on Haiku.
  Cost scales with the number of **unique** unresolved grantees and purposes — not the number of
  grants. Roughly **~60%** of grantees resolve deterministically against dim (~96% precision) and
  never reach the LLM; unique purposes are classified once and joined back by exact string.
- **Maximum API efficiency (default):** both LLM stages use the **async Message Batches API (≈50%
  cheaper)** *and* **prompt caching** (the long system prompts carry `cache_control`, so the repeated
  prefix is billed at a fraction across the batch). The tradeoff is **latency**: a batch takes a few
  minutes to process regardless of size.
- **`--sync`** switches to synchronous calls — **seconds instead of minutes**, at full (non-batch)
  price. Use it for small/interactive runs where you don't want to wait on the batch queue. (Tiny
  batches, e.g. a single request, auto-fall back to synchronous even without `--sync`.)

> Rule of thumb: **default (batch)** for scheduled / large runs where cost matters and latency
> doesn't; **`--sync`** for quick interactive runs.

---

## Design notes & limitations

- **Two-file dependency by design.** Matching uses only `dim_institution.csv`; the rich IPEDS/alias
  crosswalks are treated as offline curation inputs, not runtime dependencies. Consequence: more
  grantees reach the LLM, and some variant/campus-ambiguous names land in **review** (blank
  `canonical_id`) for offline resolution.
- **No runtime unitid discovery.** New institutions mint an `SG#####` surrogate; promoting one to a
  real IPEDS unitid is an offline curation step.
- **`dim_institution.csv` has no `ipeds_unitid` column.** `build_dim()` derives it directly from
  `canonical_id` (blank only for `SG#####` surrogates and the `NA`/`NCI` sentinels).
- **Skip-list is load-bearing.** A filename recorded in `ref_xml_processed.csv` is never re-mined.
  If a return is recorded without its grants reaching the grants table, that filer goes silently
  missing — periodically audit that known large funders are present.
- **Dedupe is deferred to the query layer** (see the SQL above).
- **Coverage is e-file only** (hard floor ~filing year 2011); paper returns before mandatory e-filing
  do not exist in machine-readable form.
- **LLM stages are non-deterministic** at the margin; the guards keep precision high but a small
  residual of judgment calls remains — the `NA`/blank/review buckets are the honest floor.

---

## Troubleshooting

- **`ModuleNotFoundError` for pandas/rapidfuzz/anthropic** → `pip install pandas rapidfuzz anthropic`.
- **`ERROR: match/tag stages need ANTHROPIC_API_KEY`** → export the key (only needed for match/tag).
- **`FileNotFoundError: dim_institution.csv` / `ref_xml_processed.csv`** → place them beside the
  script or pass `--dim` / `--existing`.
- **`KeyError: 'ipeds_unitid'`** → an old-format `dim_institution.csv` snuck back in with the
  7-column schema. The current script doesn't need or use that column at all; re-export from the
  6-column format (`canonical_id, grantee_normalized, state, country, entity_class, city`) and it'll
  load fine either way.
- **`[match]`/`[tag] ... file missing`** → run `download,parse` first with the same `--stamp`, or pass
  the stamp of the batch you want to resolve.
- **Partial/corrupt index** → `--refresh-index` forces a clean re-download.
- **Nothing downloads** → everything in range is already in the skip-list; that's expected for a
  caught-up database.
