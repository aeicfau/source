# 990-PF Grants: Grantee Resolution and Purpose Tagging

Turn the noisy free-text of Form 990-PF foundation grant records into a clean, analyzable dataset. This repository documents two methodologies that run downstream of a raw grants extract: one resolves each recipient string to a canonical institution name, and one tags each grant with the topic it funds. Together they make it possible to aggregate giving by institution and by subject across millions of grants and many foundations.

Both methodologies are deliberately self-contained. They publish the taxonomies and decision rules a reader needs to build an equivalent system, not a turnkey script. The hard part is the iteration, and these documents tell you where the traps are so your iteration starts informed.

## What this is for

Private foundations file Form 990-PF, and the grants schedule carries two unreliable free-text fields: who received the grant and what it was for. Neither is standardized. The same university appears under dozens of recipient spellings, and the same purpose ("scholarship", "general support") recurs in countless wordings. Until those fields are normalized, you cannot answer basic questions like how much a foundation gave to a given school, or how much of total giving went to STEM versus the humanities. These two methodologies close that gap.

## Pipeline overview

```
Form 990-PF XML  ──►  raw grants CSV  ──►  Grantee Name Resolution  ──►  canonical institution name
   (downloader)        (one row/grant)  └─►  Grant Purpose Tagging   ──►  topical tag(s)
```

The downloader and extraction scripts that produce the raw grants CSV are packaged separately in this project. Both methodologies below begin where that CSV ends. They are independent of each other: name resolution reads the recipient string, tagging reads the purpose string, and neither uses the other's output.

## Component 1 — Grantee Name Resolution

**Document:** `AEI_GranteeNameResolution_Methodology_v2.md`

Resolves each free-text recipient string to a canonical institution name suitable for filtering and aggregation, while preserving the original string unchanged so any result can be traced back to exactly what the foundation wrote.

**Four-way category taxonomy.** Every recipient resolves to exactly one of:

- **Specific institution** — a U.S. degree-granting college or university, resolved to its federal IPEDS spelling and identifier. Misspellings of real schools still resolve to the school.
- **Foreign** — a non-U.S. institution. Look-alike foreign names (Technion, Complutense, Trinity College Dublin) stay foreign and are never snapped to a similarly spelled U.S. school.
- **Not applicable** — the grantee is not degree-granting. This catches the most common trap: nonprofits, hospital foundations, K-12 schools, and associations that carry "College", "University", "Institute", or "School" in their name.
- **Not clearly identified** — genuinely irrecoverable strings (truncated, OCR-garbled, no public record). Reserved as a last resort, not a default.

**Anchor vocabularies.** All matching at every stage is against two canonical lists: IPEDS (public, cited rather than redistributed) for U.S. institutions, and a curated list — built and grown across iterations — for foreign universities, system-office conventions, high-volume aliases, and edge cases IPEDS omits. Together, on the order of several thousand anchors.

**Layered resolution pipeline** (cheap before expensive, so most strings resolve before any costly method runs):

1. **Rule waterfall** — deterministic, first-hit-wins: hard "not clearly identified" patterns, exact match, punctuation stripping, noise-token stripping (FOUNDATION, INC, FBO, C/O, etc.), abbreviation expansion, hard "not applicable" triggers, foreign-keyword detection, a high-volume alias map, bounded substring match, and fuzzy match at several thresholds.
2. **Character n-gram similarity** — TF-IDF over 3–5 character n-grams for the unresolved residual; catches typos, reorderings, and abbreviations that token-level matching misses.
3. **Reconciliation** — combine the rule guess and the n-gram nearest neighbor; auto-resolve where they agree, send the rest to a review queue.
4. **Pattern review** — cluster the queue and resolve high-volume patterns by human decision; one decision typically clears tens to hundreds of rows.
5. **Language-model pass with web search** — for the hard residual (dominated by obscure and foreign institutions); the model identifies the institution and the answer is programmatically snapped to the nearest anchor, so output is always an exact anchor string.

**Expected distribution** among strings that required normalization: roughly three-quarters specific U.S. institution, about one in ten not applicable, about one in ten foreign, and under a few percent not clearly identified. A materially different split signals a problem in the anchors or pipeline.

## Component 2 — Grant Purpose Tagging

**Document:** `AEI_GrantPurposeTagging_Methodology_v3.md`

Assigns a topical `tag` to every grant from the purpose text alone — not the funder, recipient, amount, or year. Work is done in unique-string space: a dataset of millions of grants collapses to roughly 150,000 unique purpose strings, each classified once and joined back by exact match, so cost scales with distinct strings rather than total rows.

**Ten-label taxonomy.** The `tag` is a semicolon-joined string of one or more labels. Eight specific topics combine freely; two generic labels are mutually exclusive with everything:

| Label | Covers |
|---|---|
| `stem` | Science, technology, engineering, math, medicine; AI and sustainability default here even in policy framing |
| `hass` | Humanities, arts, social sciences — applies the *study-versus-run* test (studying a subject qualifies; running a program does not) |
| `athletics` | Sports, teams, stadiums, sport-named scholarships (music ensembles are `hass`, not `athletics`) |
| `finaid` | Student scholarships and aid (student recipients only; faculty/postdoc awards are `research`) |
| `research` | Direct scholarly investigation — faculty awards, postdocs, endowed chairs, research centers |
| `professional` | Professional schools: law, business, journalism (always), public policy, divinity, etc. |
| `studentlife` | Non-academic campus life: dorms, unions, chaplaincy, student media, career services |
| `capital` | Physical construction and equipment (not artwork; not metaphorical "capital campaigns") |
| `general` | Generic operating language; unrestricted gifts, annual fund, boilerplate — never co-occurs with any other tag |
| `other` | Residual: opaque codes, truncated or empty strings — never co-occurs with any other tag |

**Cross-cutting rules.** Exclusivity is absolute (`general`/`other` stand alone). Multi-tag liberally when domains genuinely overlap (a nursing-school scholarship is `stem;finaid;professional`). Read topic, not audience. Own the opinionated calls (journalism is always `professional`; health professional schools are `stem;professional`). Restrain promotion — a single weak word does not pull a row out of `general`.

**Running at scale.** Classify unique strings via an asynchronous batch API in chunks of ~10,000, one request per string, with the system prompt cached byte-for-byte and temperature at zero for determinism. A fast, inexpensive model matched a model 20× its cost head-to-head; prompt quality, not model size, is the lever. These choices keep cost roughly 20× below a naive run.

## The quality-control loop (the actual work)

Both methodologies stress the same point: the taxonomy is the *output* of an audit loop, not the input to it. Reproducing comparable quality means running the loop yourself — sample a few hundred strings, hand-label them, run the classifier against the same set, log every disagreement, read for systematic patterns, turn each pattern into a new rule, and repeat. For tagging, agreement converges around the high-eighties as a percentage, with the remainder being defensible judgment calls. Anyone claiming this classification is hands-off is mistaken.

## Integrity invariants

Treat any failure of these as a defect:

- **Row-count match** — every output file has exactly the same number of rows as its input.
- **Canonical/schema validity** — every resolved name is an exact member of the anchor lists; every `tag` is drawn only from the ten labels (scan for and remap model-hallucinated tokens).
- **Exclusivity** — no row carries `general` or `other` alongside a specific tag.
- **Original preserved** — the raw recipient and purpose strings are never overwritten.
- **Provenance** — each resolved row records the canonical name, category, confidence level, and the decision source that produced it.

## Limitations

Resolution and tagging are only as good as their inputs. An institution absent from both IPEDS and the curated list cannot resolve to a canonical name. A real program described in vague operating language tags `general`, because the model is given nothing else. Genuinely cryptic strings cannot be recovered from text and land in "not clearly identified" or `other`. These residuals are the honest floor of the method; past that point only source-document review would help.

## Files

- `GranteeNameResolution_Methodology.md` — full grantee resolution methodology
- `GrantPurposeTagging_Methodology.md` — full purpose-tagging methodology

The production system prompts are intentionally not published. What is published is the taxonomies and decision rules those prompts encode — which is what you need to build an equivalent system and, after your own iteration, reach comparable results.
