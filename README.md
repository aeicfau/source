# Technical Appendix: How the Dashboard Was Assembled

This appendix documents how the grants dashboard was built, from raw public filings to the tagged, deduplicated dataset that the dashboard displays. The two data-engineering steps are reproducible exactly: a reader with a Python environment can rebuild the flat grants file from the public source. The two classification steps are reproducible in method but not bit-for-bit: the exact production prompts are not published, so a reader rebuilds an equivalent dataset by encoding the published taxonomy and rules into their own prompt and iterating, as described in the two methodology files. The point throughout is that every editorial decision is disclosed, so anyone can audit it, challenge it, or rebuild under their own rules.

The build has four components. Two are data-engineering steps that turn raw tax filings into a flat file. Two are classification steps that add analytical structure to that flat file. Each component is published as its own set of files so that any one of them can be inspected, rerun, or replaced independently.

1. Python scripts that filter and download Form 990-PF XML filings from GivingTuesday's public mirror.
2. Python scripts that extract grant-level detail from those XML filings into a single flat CSV.
3. A markdown file documenting the grant-purpose tagging process, taxonomy, and prompt design.
4. A markdown file documenting the grantee name resolution process, taxonomy, and prompt design.

The two classification components run in parallel and independently against the flat file produced by the first two. Neither depends on the other. A reader who wants only the tags, or only the cleaned recipient names, can run one path and ignore the other.

## Data source

The raw material is Form 990-PF, the annual return that U.S. private foundations file with the IRS. The IRS publishes the full 990 corpus as bulk XML at the [Form 990 series downloads page](https://www.irs.gov/charities-non-profits/form-990-series-downloads). GivingTuesday mirrors and archives that corpus in a [public S3 bucket](https://990data.givingtuesday.org/), which is the source the pipeline reads.

The bucket holds the entire 990 universe, including operating charities that file Form 990 or 990-EZ. The pipeline keeps only Form 990-PF returns, so the resulting dataset is private foundations and not operating charities. Every grant in the dashboard links through to the same filing on ProPublica's Nonprofit Explorer, which renders the underlying XML in a viewer that requires no technical knowledge to read.

Two properties of the source are worth stating up front because they shape what the dashboard can and cannot show. First, the dataset covers only private foundations that file Form 990-PF. That is roughly half of total private grant dollars flowing to colleges and universities. Money from individuals, operating charities, corporations including philanthropic LLCs, and foreign sources is not captured here. Second, e-filing of Form 990 was permitted from 2008 but only required from 2020, so the corpus is sparse before 2020 and robust afterward. The apparent jump in dollar totals around 2020 reflects filing completeness, not a real surge in giving.

## Component 1: Filter and download the Form 990-PF XML

The first component reads GivingTuesday's index of the mirrored corpus, selects the filings that are Form 990-PF returns, and downloads only those. Operating charities filing Form 990 or 990-EZ are dropped at this stage so that nothing irrelevant is downloaded or parsed downstream.

The relevant design points are that the filter keys on return type rather than on filer name or size, so the selection is reproducible and does not encode any judgment about which foundations matter, and that downloads are resumable, so an interrupted run can continue without refetching what it already has. The scripts and their parameters are published alongside this appendix.

## Component 2: Extract grant-level detail into a flat CSV

The second component walks each downloaded 990-PF, isolates the grants-paid schedule (the schedule on which a foundation discloses every grant it made during the year), and writes one row per grant to a single flat CSV. Each row carries, at minimum, the funder, the recipient, the filing year, the grant amount, and the foundation's own free-text purpose string.

XML structure varies across filing years and software vendors, so the extraction reads the schedule by its standardized field tags rather than by fixed positions, and it tolerates missing or malformed elements by recording an empty value rather than failing the whole filing. The result is a single flat file, one row per grant, that serves as the common input to both classification components. The scripts are published alongside this appendix.

This flat file is the only artifact the two classification paths consume. Everything downstream is documented as a reproducible transformation of it.

## Component 3: Grant-purpose tagging

The free-text purpose field is the only signal used for thematic classification. It is highly repetitive: the more than one million grant rows collapse to roughly one hundred fifty thousand unique purpose strings, because the same wording recurs across many grants and many foundations. All classification work is therefore done in unique-string space and joined back to the full row set, which is what makes the approach inexpensive.

Each unique purpose string is classified by a large language model against a fixed ten-label schema, with one or more labels per grant. Eight labels are specific topics that can combine, so a nursing-school scholarship correctly carries science, financial-aid, and professional tags at once. Two labels are generic and exclusive, firing only when no specific topic can be read from the text. The same pass also flags whether a purpose touches contested political terrain and, if so, in which direction.

The classification is deliberately opinionated, and every judgment call is disclosed in the tagging methodology file rather than hidden in code. The model reads only the purpose string. It has no access to the funder, the recipient, the filing, or the web, so a topical grant wrapped in vague operating language is tagged on what the text actually says. The full label taxonomy, the boundary rules, the political-flag rules, the prompt-design principles, the batch and caching approach that keeps cost low, the quality-control loop, and the integrity checks are documented in the methodology file `AEI_GrantPurposeTagging_Methodology`. That file publishes the taxonomy and the rules the prompt encodes, not the exact prompt text, which is sufficient to build an equivalent classifier and, after the reader's own iteration, reach comparable results.

## Component 4: Grantee name resolution

The recipient column in the raw filings is unreliable. A single university appears under dozens of variant spellings, abbreviations, truncations, and "for the benefit of" wrappers across foundations and years. Filtering and aggregation require a clean canonical name, so each raw recipient string is resolved against a canonical anchor list and sorted into one of four categories: a specific U.S. institution with a federal identifier, a foreign institution, an entity that is not a degree-granting institution, or a string too garbled to identify.

Resolution runs as a layered pipeline that resolves the easy majority cheaply and reserves expensive methods for the residual. A deterministic rule waterfall handles exact and near-exact matches first. A character n-gram similarity pass catches typos and word reorderings. The two are reconciled, recurring unresolved patterns are adjudicated in bulk, and only the small hard residual goes to a language model with web search enabled. Each dashboard row preserves both the foundation's original recipient string, for traceability, and the resolved canonical name, for filtering. The four-category taxonomy, the anchor vocabularies, the full pipeline, the reconciliation logic, and the prompt-design principles are documented in the methodology file `AEI_GranteeNameResolution_Methodology`. As with tagging, that file publishes the decision rules the prompt encodes rather than the exact prompt text.

## Why a language model, and why this one

Two components use a language model: topical tagging and the residual of grantee resolution. The alternatives are manual classification, keyword and regex rules, and fine-tuned smaller models. The model approach produces a measurably better dataset at a small fraction of the cost and time of any of them, and, unlike manual work, it can be audited and rebuilt by anyone with an API key. Keyword rules in particular have a recall ceiling: they read "Yale Annual Fund" correctly as generic but read "general support for the Institute for Higher Education Leadership and Policy" as generic too, because the operating-language wrapper drowns out the domain content. A model given a clear rubric reads past the wrapper the way a human reader would.

The model used is Claude Haiku 4.5, chosen after direct comparison against a larger model on a sample where the larger model produced no measurably better output at roughly twenty times the cost. The two methodology files record the comparison and the run settings.

## Reproducibility and integrity

Reproducibility differs by component, and the appendix is explicit about it. The two extraction components rebuild the flat file from the public bucket exactly. The two classification components rebuild equivalent tag and canonical-name columns from that flat file, but because the exact prompts are not published, matching the original closely requires the reader to encode the published taxonomy into their own prompt and run the same manual audit-and-revise loop the methodology files describe. The files specify expected output distributions as a sanity reference and a checklist of integrity invariants that a correct rebuild must satisfy, for example that the tagged file has the same row count as its input, that every tag value is drawn only from the allowed schema, and that no row carries both a specific tag and a generic one. The dashboard does not display a row that fails these checks.

## Citation

- American Enterprise Institute for Public Policy Research, for the dashboard, code, and reproducibility instructions.
- U.S. Internal Revenue Service, for the underlying grant data (IRS Form 990-PF).
- GivingTuesday, for the consolidated and mirrored XML corpus.
- ProPublica, for the per-filing links to the XML viewer.

## Questions

Methodology and replication questions: contact AEI through the project page on aei.org. Questions about the underlying 990-PF filings and the mirrored XML bucket: GivingTuesday's project page is the authoritative source. Questions about the source documents in human-readable form: ProPublica's Nonprofit Explorer is the easiest place to start.
