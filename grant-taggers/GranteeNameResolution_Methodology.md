# Resolving Grantees to Canonical Names: A Standalone Methodology

This document is self-contained. It explains how to take the noisy free-text recipient strings in a Form 990-PF derived grants dataset and resolve each one to a clean canonical institution name suitable for filtering and aggregation. It gives the category taxonomy, the anchor vocabularies, the layered resolution pipeline, the design of the language-model step, and the audit. It does not publish the exact production system prompt. It publishes the decision rules that the prompt encodes, which is what a reader needs to build an equivalent resolver and, after their own iteration, reach comparable results.

Set expectations first. This is not turnkey. The rules below are the residue of many manual passes over real recipient strings, and getting a resolver to apply them consistently took repeated cycles of running the pipeline, reading what it got wrong, and adding rules. A reader who follows this and runs it once will resolve the easy majority and then face a long tail of genuinely hard strings that only patient, manual pattern adjudication clears. The value of this document is that it tells you the category boundaries, the pipeline order, and where the traps are, so your iteration starts informed rather than blind.

## Input and output

The input is a flat CSV with one row per grant, carrying the foundation's free-text recipient string. That file is produced by the XML download and extraction scripts packaged alongside this document; this methodology begins where that file ends.

The output attaches to every row a canonical name, a category, a confidence level, and the decision source that produced the resolution, while preserving the original recipient string unchanged. Preserving the original is non-negotiable: it is what lets any reader trace a canonical name back to exactly what the foundation wrote.

## The problem

The recipient column is unreliable. One university appears under dozens of variants across foundations and years: "Stanford University", "Leland Stanford Junior University", "Trustees of Stanford", "Stanford Univ.", "FBO Stanford University". On top of spelling variants there are abbreviations, truncations, "for the benefit of" wrappers, alumni-association and foundation suffixes, and OCR-style noise. Aggregating by institution is impossible until these collapse to one canonical name per school.

The governing design principle is to resolve the easy majority with cheap deterministic methods and spend expensive methods only on the hard residual. Most strings resolve on exact or near-exact matching. Only a small fraction ever reaches a language model.

## The category taxonomy

Every raw recipient string resolves into exactly one of four categories. The boundaries between them are where the judgment lives, so each is defined by what qualifies, what does not, and the cases that mislead.

### Specific institution

A U.S. degree-granting college or university that resolves to a federal IPEDS identifier.

Boundary rules: resolve to the canonical IPEDS spelling, not the foundation's variant. A misspelling of a real U.S. school still resolves to that school ("Fordam" to "Fordham"; "Colombia University" in a U.S. context to "Columbia University"). A campus must be identifiable; a system or multi-campus name with no campus specified may instead be "not clearly identified" if no dominant reading exists.

Example strings: "MIT" to "Massachusetts Institute of Technology"; "Univ Wisconsin Milwaukee" to "University of Wisconsin-Milwaukee".

### Foreign

A non-U.S. institution.

Boundary rules: a foreign institution whose name resembles a U.S. one stays foreign and must not be snapped to the look-alike U.S. school ("Technion" is Israel; "Complutense" is Spain; "Trinity College Dublin" is not "Trinity College" in the U.S.). Foreign-keyword and country cues take precedence over a superficially close U.S. match.

Example strings: "Hebrew University of Jerusalem"; "University of Oxford"; "Tsinghua University".

### Not applicable

The grantee is not a degree-granting institution.

Boundary rules: this catches entities that carry "College", "University", "Institute", or "School" in the name but are not degree-granting, which is the single most common misclassification trap. Examples of the class: college-access nonprofits, hospital foundations, K-12 schools, professional associations, scholarship-administering charities, and religious organizations that are not seminaries or divinity schools. A name containing "College" is not sufficient evidence of a college.

Example strings: "Boys & Girls Club"; "Children's Hospital Foundation"; "Phillips Academy" (a K-12 school).

### Not clearly identified

The string is too truncated, garbled, or ambiguous to pin down.

Boundary rules: reserve this for genuinely irrecoverable strings, not as a convenient default. If a dominant reading exists, commit to it. Use this only when no specific institution, foreign institution, or not-applicable determination can be made ("THE UNIVERSITY OF" with no campus; OCR-garbled text; an obscure name with no public record).

Example strings: "ALUMNI ASSOCIATION OF THE UNIVERSITY"; "UNIVERSITY OF"; truncated or garbled fragments.

## Anchor vocabularies

Two canonical lists are the ground truth, and all matching at every stage is against them. The first is IPEDS, the federal enumeration of every degree-granting U.S. institution, which supplies the canonical spelling and identifier for each. IPEDS is public and can be cited rather than redistributed. The second is a curated list you build across iterations, covering foreign universities, system-office naming conventions, high-volume aliases, and edge cases IPEDS does not contain. The curated list is itself an output of the manual work; expect to grow it as the pipeline surfaces new cases. Together the two provide on the order of several thousand anchor entries.

## The resolution pipeline

Each raw recipient string runs through layered stages, cheap before expensive, so most strings are resolved before any costly method runs.

### Stage 1: Rule waterfall

A deterministic, first-hit-wins waterfall, ordered so the most reliable signals fire first: hard "not clearly identified" patterns (truncated strings with no campus); exact match against the anchors; match after stripping punctuation; match after stripping noise tokens ("FOUNDATION", "INC", "FBO", "C/O", "ALUMNI ASSOCIATION", "HILLEL"); match after expanding common abbreviations ("UNIV" to "UNIVERSITY", state abbreviations to full names); hard "not applicable" triggers (K-12 schools, hospital foundations, professional associations); a foreign-keyword detector; an alias map for the highest-volume cases (UCLA, NYU, MIT, Cal Poly); a bounded substring match where any anchor appearing whole inside the raw string resolves it, longest match winning; and a fuzzy match at several confidence thresholds. The waterfall resolves the majority of strings on the first pass.

### Stage 2: Character n-gram similarity

For strings the waterfall does not resolve, build a TF-IDF representation over character n-grams (three to five characters) of every anchor, and encode each unresolved recipient the same way. Retrieve the top few cosine-similarity matches per string. Character n-grams catch typos, word reorderings, and abbreviations that token-level fuzzy matching misses.

### Stage 3: Reconciliation

Each still-unresolved string now has two independent guesses, one from the rules and one from the n-gram nearest neighbor. Where they agree, auto-resolve. Where they agree on the school but disagree on phrasing, the curated canonical wins. Where the n-gram match is highly confident and the rules are weak, the n-gram guess wins. Everything else goes to a review queue.

### Stage 4: Pattern review

Do not adjudicate the review queue row by row. Cluster it into recurring patterns and resolve the highest-volume patterns by human decision. One pattern decision typically resolves tens to hundreds of rows, compressing what would be person-hours of row-by-row review into a short adjudication session. This stage is where most of the remaining manual effort goes and where most of the residual is actually cleared.

### Stage 5: Language-model pass with web search

For the hard residual that no rule, no n-gram match, and no pattern decision can pin down, run a language model with the web-search tool enabled, because this residual is dominated by obscure and foreign institutions a model would not reliably recognize from training knowledge alone. The model returns a name in a strict format, and that name is then fuzzy-snapped to the nearest anchor above a high similarity threshold, so the output is always an exact anchor string rather than free text. This pass closes the residual to a small fraction of unique recipient strings.

## Design of the language-model step

The exact prompt is not published, but its design rules are, and they are what matter for reproduction. Each rule exists to prevent a failure seen in development.

Resolve, do not invent. Ask the model to identify which real institution a string refers to, then snap that answer to the anchor list programmatically. Do not ask the model to produce a canonical string directly, or it will return plausible but non-anchor phrasings.

Prefer a specific answer over uncertainty when a dominant reading exists. Left alone, the model over-uses "not clearly identified". Instruct it to commit to the most likely institution, or to "foreign" when that clearly dominates, and to reserve the uncertain category for genuinely irrecoverable strings.

Carry an explicit spelling and disambiguation list. Name the recurring traps directly: common misspellings that collide with real institutions, and look-alike foreign names that must not map to a similarly spelled U.S. school. This is the same boundary logic stated in the category taxonomy above, given to the model as named cases.

Carry a generic-canonical warning list. Some anchors are dangerously generic ("Institute of Technology", "National University", "City University", "Park University") and attract false matches. Flag them so the model does not snap an unrelated string onto a generic-sounding anchor.

Use training knowledge first, web search only when needed, to keep the pass fast and cheap while still resolving the obscure tail. End the response with the answer on a single labeled line so the downstream snapper can parse it deterministically.

When run as a large batch, two efficiencies mirror good practice generally: process at the cluster level where many raw strings share one guess, classifying the cluster once and splitting to row-level resolution only for clusters that genuinely contain multiple institutions; and run through an asynchronous batch API with the prompt cached.

## Audit

Check resolution quality on a stratified sample drawn across all decision sources (rules, n-gram, pattern, model) and across all four categories. Judge each sampled row correct or incorrect by hand, and report accuracy by decision source so any weak stage is visible rather than buried in an aggregate. A correct build clears a high overall accuracy bar with no single source dragging materially below it. As with tagging, this audit is not a formality; it is how you find the systematic errors that a single pass leaves in.

## Expected distribution

Among the unique recipient strings that required normalization (those that did not exact-match on the first pass), the categories break down approximately as: specific U.S. institution around three-quarters, not applicable around one in ten, foreign around one in ten, and not clearly identified under a few percent. Use this as a sanity reference; a materially different split points to a problem in the anchors or the pipeline rather than a real difference in the data. The "not clearly identified" residual is the genuine ceiling, consisting of truncated strings, OCR-garbled text, and obscure schools with no public web record. Past that point only source-document review would help.

## Integrity

Each output row carries the original recipient string, the resolved canonical name, the four-way category, a confidence level, and the decision source. The resolved file must have the same row count as its input, and every canonical name must be an exact member of the anchor lists rather than free text.

## Limitations

Resolution is only as good as the anchor vocabularies. An institution absent from both IPEDS and the curated list cannot resolve to a canonical name and will land in "foreign" or "not clearly identified" depending on its text. Growing the curated list is part of the work, not a one-time setup.

The "not clearly identified" residual cannot be closed from text alone. It is the honest floor of the method.

The four-way schema encodes judgment at its boundaries, most visibly around entities that carry "College" or "University" in their name but are not degree-granting. These resolve to "not applicable" by rule, and the rules are disclosed so a reader who draws the boundary differently can adjust them and rebuild.
