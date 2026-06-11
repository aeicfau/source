# Tagging Grant Purposes by Topic: A Standalone Methodology

This document is self-contained. It explains how to assign a topical tag to every grant in a Form 990-PF derived dataset, using the foundation's free-text purpose string as the only signal. It gives the full taxonomy, the decision rules, the process for running it at scale, and the quality-control loop. It does not publish the exact production system prompt. It publishes the taxonomy and rules that the prompt encodes, which is what a reader needs to build an equivalent classifier and, after their own iteration, reach comparable results.

A word on expectations before anything else. This is not a turnkey recipe. The taxonomy below is the distillation of many manual passes over real disagreements, and getting a classifier to apply it consistently took repeated rounds of auditing the model against hand-labeled samples and rewriting rules to close each failure pattern. A reader who encodes this taxonomy into their own prompt and runs it once will get close but not identical results. Reaching comparable quality requires doing the same painstaking iteration described in the quality-control section. The value of this document is that it tells you what the rules are and where the hard cases hide, so your iteration starts from the right place rather than from scratch.

## Input and output

The input is a flat CSV with one row per grant, carrying at minimum a `grant_purpose` free-text field. That file is produced by the XML download and extraction scripts packaged alongside this document; this methodology begins where that file ends.

The output adds one column to every row: `tag`, the topical classification. Classification uses the purpose text only. It does not use the funder, the recipient, the amount, the year, or the surrounding filing. Every tag is a read of the purpose string as written.

## Work in unique-string space

The purpose field is highly repetitive. A dataset of more than a million grants typically collapses to on the order of one hundred fifty thousand unique purpose strings, because the same wording recurs across many grants and many foundations. A handful of strings ("SCHOLARSHIP", "GENERAL SUPPORT", "EDUCATION") cover a large share of all rows, and the genuine difficulty lives in the long tail of low-frequency strings.

Classify once per unique string, then join the result back onto the full dataset by exact string match, so identical purpose text always receives an identical tag. Cost scales with the number of distinct strings, not the number of grants, which is what makes the whole approach inexpensive.

## The taxonomy

The `tag` column is a semicolon-joined string of one or more labels drawn from exactly ten values. Eight are specific topics that combine freely. Two are generic and mutually exclusive with everything else. The point of a written taxonomy is that each label is defined by what it covers, what it excludes, and the boundary cases that trip up a naive reader. Encode all three, not just the inclusions.

### stem

Covers science, technology, engineering, mathematics, and medicine: medical and nursing schools, hospitals, clinics, public-health and pandemic and vaccine programs, climate and sustainability work, conservation and environmental science, computer science, AI and AI-policy research, data science, and agricultural, food, and animal science.

Boundary rules: AI and sustainability default to `stem` even when wrapped in policy or governance framing. Health professional schools always also carry `professional` (see below). A medical-history or bioethics grant that studies medicine as a humanistic subject is `hass`, not `stem`.

Example strings: "Endowment for the Department of Chemistry"; "Research on coral reef resilience"; "COVID-19 vaccine distribution program".

### hass

Covers humanities, arts, and social sciences: music (orchestra, band, choral, ensemble, percussion), theatre, dance, opera, museums, galleries, history, literature, philosophy, religious studies and theology as academic disciplines, political science, economics, psychology, sociology, anthropology, area studies, gender studies, and education research.

Boundary rules: this is the tag most prone to over- and under-firing, so apply a study-versus-run test. A grant that *studies* a humanistic or social subject is `hass`. A grant that *runs* a program, service, campaign, or bare education-delivery effort on a similar topic is not. "Research on the history of the civil rights movement" is `hass`; "K-12 education program" and "empower women and girls" and "workforce development" and "financial literacy" are not, even though they sound social. Those route to `general` when generic, to `stem` when the subject is science or health, or to a specific tag where one applies. Music terms are always `hass`, never `athletics`.

Example strings: "Support for the Department of Philosophy"; "Symphony orchestra endowment"; "Study of voting behavior in rural counties".

### athletics

Covers sports, teams, coaching, stadiums, intramural and intercollegiate competition, and scholarships explicitly named for a sport.

Boundary rules: music ensembles ("marching band", "drum line") are `hass`, not `athletics`, despite the overlap in vocabulary.

Example strings: "Athletic scholarship fund"; "Renovation of the football stadium" (also `capital`).

### finaid

Covers scholarships and student aid: undergraduate and graduate scholarships, named scholarships, tuition assistance, and student-level fellowships.

Boundary rules: a fellowship is `finaid` only when the recipient class is explicitly a student (graduate, doctoral, predoctoral, PhD, master's, undergraduate). Faculty and postdoctoral fellowships are `research`, not `finaid`. A named award is `finaid` if it funds a student and `research` if it funds investigation; the word "scholar" alone does not make it `research`.

Example strings: "Annual scholarship for first-year students"; "The Jane Doe Memorial Scholarship".

### research

Covers direct scholarly investigation: faculty research awards, postdoctoral fellowships, investigator grants, endowed chairs, named research fellowships, policy laboratories, and research centers. Phrasing like "to investigate", "to study", or "to examine" fires this tag.

Boundary rules: a bare scholarship or bare training program is not `research`, even when "scholar" appears. `research` frequently combines with a domain tag: a chemistry investigator award is `stem;research`; an economics policy lab is `hass;research`.

Example strings: "Endowed chair in molecular biology" (`stem;research`); "Postdoctoral fellowship in art history" (`hass;research`).

### professional

Covers the professional schools: law, business and MBA, journalism (always), public policy, divinity, architecture, hospitality, library and information science, and social work.

Boundary rules: health professional schools (medicine, nursing, dentistry, pharmacy, public health, optometry, veterinary) always double-tag as `stem;professional`. Journalism is always `professional`, never `hass`. A liberal-arts public-policy *research* center is `hass;research`; a public-policy *school* is `professional`.

Example strings: "Support for the law school" (`professional`); "Nursing school scholarship" (`stem;finaid;professional`).

### studentlife

Covers the non-academic side of campus: dormitories and residence halls, student unions, fraternities and sororities, orientation, career services, counseling and disability services, campus religious life (chaplaincy, Hillel, Newman, interfaith), student radio and newspapers, service-learning, and community engagement.

Boundary rules: campus religious *life* is `studentlife`; religious studies as an academic *discipline* is `hass`. A student newspaper is `studentlife`; a journalism school is `professional`.

Example strings: "New residence hall" (`studentlife;capital`); "Campus counseling center".

### capital

Covers physical construction and equipment: buildings, facilities, laboratories, renovation, infrastructure, parking, and deferred maintenance.

Boundary rules: artwork donations do not count. A metaphorical "capital campaign" does not count when it funds programs rather than a building. A grant to a new biology building is `stem;capital`; a grant to "the capital campaign" with no further detail is `general`.

Example strings: "Construction of the new engineering building" (`stem;capital`); "Library renovation".

### general

Covers generic operating language: unrestricted gifts, annual fund, corporate matching, IRS boilerplate, "for the donee's charitable purposes."

Boundary rules: `general` fires only when no specific topic can be extracted. It never co-occurs with any other tag.

Example strings: "General operating support"; "Unrestricted gift"; "For the donee's exempt purpose".

### other

Covers the residual: opaque codes, internal references, truncated strings, or rows with no purpose text at all.

Boundary rules: `other` fires only when no specific tag and no generic operating language apply. It never co-occurs with any other tag.

Example strings: "FBO RECIPIENT"; "SEE STATEMENT A"; "MIP PAYOUT 04/2022".

## Cross-cutting rules

These govern how the labels combine and are as important as the definitions.

Exclusivity is absolute. `general` and `other` never co-occur with each other or with any of the eight specific tags. A row is either generic or specific, never both. An output like `general;stem` is invalid and indicates a parsing or model error.

Multi-tag liberally when several domains are genuinely present. The eight specific tags combine in any number. A nursing-school scholarship is correctly `stem;finaid;professional`. Do not force a single label on a grant that truly spans domains.

Read topic, not audience. Tag a grant on the subject it funds, not on the population it serves. Read past operating-language wrappers ("general support for the Institute for X") to the domain of the named object. This is precisely where keyword approaches fail and where a careful reader earns the difference.

Own the opinionated calls. The taxonomy makes deliberate decisions that a reasonable person could make differently, and consistency requires stating them rather than leaving them to chance: journalism is always `professional`; AI and sustainability policy default to `stem`; a named scholarship is `finaid` unless it clearly funds investigation; health professional schools are `stem;professional`. Disclose these so a reader who disagrees can change them and rebuild.

Restrain promotion. Do not escalate generic language to a specific tag on a single weak word. A lone ambiguous term does not pull a row out of `general`. This keeps precision high and stops the specific tags from absorbing boilerplate.

## Running it at scale

Two choices keep cost roughly twenty times below a naive run. Use an asynchronous batch API, which discounts every call by about half in exchange for queue latency that is immaterial for a one-shot job; submit the unique strings in chunks of around ten thousand requests, one request per unique string. And cache the system prompt, since it is identical across every request and only the user message changes; caching charges the repeated prompt at a small fraction of the normal rate, which is what makes a long, example-rich prompt affordable. Keep the prompt byte-for-byte identical across all batches or the cache hit is lost.

Run at temperature zero with a frozen prompt so the same input yields the same output. A fast, inexpensive model is sufficient: on this task a frontier-class small model matched a model twenty times its cost on a head-to-head sample, so model size is not the lever. Prompt quality is the lever.

Retrieval should be idempotent and resumable: poll the batches, download each as it ends, and remember the queue is not first-in-first-out, so the slowest batch governs total time regardless of submission order.

## The quality-control loop, which is the actual work

The taxonomy above is the output of this loop, not the input to it. Reproducing comparable quality means running the loop yourself.

Draw a sample of a few hundred unique strings and classify them by hand. Run your current prompt against the same strings. Log every disagreement with a verdict: model right, human right, or both wrong. Read the disagreements for systematic patterns rather than one-off errors. Turn each recurring pattern into a new rule or example in your prompt. Repeat until the model and the auditor agree on the large majority of cases and the residual is genuine judgment calls rather than category errors. In practice this converges around the high-eighties as a percentage of agreement, with the rest being defensible differences.

The failure patterns this taxonomy already encodes are the ones the loop surfaced: tagging anything containing "scholar" as `research`; landing journalism in `hass`; reading K-12 program delivery as education research; firing `capital` on metaphorical capital campaigns; and missing the `professional` co-tag on health schools. You will rediscover these immediately if you skip the boundary rules. You will discover new ones specific to your data that no document can anticipate. Budget real human attention for this. Anyone who claims the classification is hands-off is mistaken.

After the full run, draw a fresh sample, ideally from rows where your model disagrees with a prior or alternative classifier, and audit it the same way. Disagreements that concentrate in one direction (most commonly, specific tags collapsing to `general` under an operating-language wrapper) indicate a systematic gap that a cheap, targeted second pass over just the affected slice can close without disturbing the rest.

## Integrity checks

A correct run satisfies these invariants; treat any failure as a defect.

Row-count match: the tagged file has exactly the same number of rows as its input.

Schema validity: every `tag` value is a semicolon-joined string drawn only from the ten labels. Fast models occasionally invent a label, so scan for any out-of-schema token and map known hallucinations to the correct label.

Exclusivity: no row carries `general` or `other` alongside any specific tag, and not both generic tags together.

Empty handling: an empty or unclassifiable `tag` defaults to `other`.

Distribution sanity: as an order-of-magnitude reference, the great majority of rows fall into `general` and `finaid`; `stem`, `research`, and `hass` each take a single-digit percentage; `athletics`, `capital`, and `studentlife` are each around or below one percent. Multi-tag rows make shares sum above one hundred percent. A materially different distribution points to a model, prompt, or parsing problem rather than a real finding.

## Limitations

Classification rests on self-reported purpose text only. A real program described in vague operating language tags `general`, because the model is given nothing else.

Genuinely cryptic strings cannot be recovered from text and default to `other`.
