# Technical Appendix: How the Dashboard Was Assembled

This appendix documents how AEI SOURCE was built, from raw public filings to the tagged, deduplicated dataset that the dashboard displays. The two data-engineering steps are reproducible exactly: a reader with a Python environment can rebuild the flat grants file from the public source. The two classification steps are reproducible in method as they require individual judgment and iteration, as described in the two methodology files. Anyone user can audit, challenge, or rebuild it under their own rules.

The build has four components. Two are data-engineering steps that turn raw tax filings into a flat file. Two are classification steps that add analytical structure to that flat file. Each component is published as its own set of files so that any one of them can be inspected, rerun, or replaced independently.

1. Python scripts that filter and download Form 990-PF XML filings from GivingTuesday’s public mirror.
2. Python scripts that extract grant-level detail from those XML filings into a single flat CSV.
3. A markdown file documenting the grant-purpose tagging process, taxonomy, and prompt design.
4. A markdown file documenting the grantee name resolution process, taxonomy, and prompt design.

The two classification components run in parallel and independently against the flat file produced by the first two.

## Data source

The raw material is Form 990-PF, the annual return that U.S. private foundations file with the IRS. The IRS publishes the full 990 corpus as bulk XML at the [Form 990 series downloads page](https://www.irs.gov/charities-non-profits/form-990-series-downloads). GivingTuesday mirrors and archives that corpus in a [public S3 bucket](https://990data.givingtuesday.org/), which is the source the pipeline reads.

The bucket holds the entire 990 universe, including operating charities that file Form 990 or 990-EZ. The pipeline keeps only Form 990-PF returns, so the resulting dataset is private foundations and not operating charities.

Two properties of the source limit what the dashboard can and cannot show. First, the dataset covers only private foundations that file Form 990-PF. That is roughly half of total private grant dollars flowing to colleges and universities. Money from individuals, operating charities, corporations including philanthropic LLCs, and foreign sources is not captured here. Second, e-filing of Form 990 was permitted from 2008 but only required from 2020, so the corpus is sparse before 2020 and robust afterward. The apparent jump in dollar totals around 2020 reflects filing completeness, not a real surge in giving.

## Component 1: Filter and download the Form 990-PF XML

The first component reads GivingTuesday’s index of the mirrored corpus, selects the filings that are Form 990-PF returns, and downloads only those. Operating charities filing Form 990 or 990-EZ are dropped at this stage so that nothing irrelevant is downloaded or parsed downstream.

The relevant design points are that the filter keys on return type rather than on filer name or size, so the selection is reproducible and does not encode any judgment about which foundations matter, and that downloads are resumable, so an interrupted run can continue without refetching what it already has. The scripts and their parameters are published alongside this appendix.

## Component 2: Extract grant-level detail into a flat CSV

The second component walks each downloaded 990-PF, isolates the grants-paid schedule (the schedule on which a foundation discloses every grant it made during the year), and writes one row per grant to a single flat CSV. Each row carries the funder, the recipient, the filing year, the grant amount, and the foundation’s own free-text purpose string.

The XML extraction reads the schedule by its standardized field tags. The result is a single flat file, one row per grant, that serves as the common input to both classification components. The scripts are published alongside this appendix.

## Component 3: Grant-purpose tagging

The free-text purpose field is the only mechanism used for thematic classification. It is highly repetitive: the more than one million grant rows collapse to roughly 150,000 unique purpose strings, because the same wording recurs (e.g., “GENERAL”). All classification work is therefore done in unique-string space and joined back to the full row set, which makes this approach efficient..

Each unique purpose string is classified by a large language model against a fixed ten-label schema, with one or more labels per grant. Eight labels are specific topics that can combine, so a nursing-school scholarship correctly carries science, financial-aid, and professional tags at once. Two labels are generic and exclusive, firing only when no specific topic can be read from the text. The details of these tags follow:

1. stem covers science, technology, engineering, mathematics, and medicine.
2. hass covers humanities, arts, and social sciences.
3. athletics covers sports, teams, intramural and intercollegiate competition, and scholarships explicitly named for a sport.
4. finaid covers scholarships and student aid such as undergraduate and graduate scholarships, named scholarships, tuition assistance, student-level fellowships. Here we took a specific call: a fellowship counts as finaid only when the recipient class is explicitly a student (graduate, doctoral, predoctoral, PhD, master’s, or undergraduate). Faculty fellowships and postdoctoral fellowships tag research instead.
5. research covers direct scholarly investigation, such as faculty research awards, postdoctoral fellowships, investigator grants, and research centers.
6. professional covers the professional schools, such as law, business, journalism, and public policy. Health professional schools (medicine, etc.) double-tag as both stem and professional because they are simultaneously scientific and professional.
7. studentlife covers the non-academic side of campus, including student clubs, fraternities and sororities, religious life, and community engagement programs.
8. capital covers physical construction and equipment such as buildings, facilities, laboratories, and renovation. “Capital campaigns” do not count when the campaign funds programs rather than buildings, e.g. a grant to a new biology building tags both stem and capital, while a grant to “the capital campaign” with no further specification tags general.
9. general covers generic operating language: unrestricted gifts, annual fund contributions, corporate matching, IRS boilerplate, “for the donee’s charitable purposes.” It fires only when no specific topic can be extracted from the purpose text.
10. other covers the residual: codes, internal references, truncated strings, or rows with no purpose text at all.

The model reads only the purpose string. It has no access to the funder, the recipient, the filing, or the web, so a topical grant wrapped in vague operating language is tagged on what the text actually says. The full label taxonomy, the boundary rules, the prompt-design principles, the batch and caching approach that keeps cost low, the quality-control loop, and the integrity checks are documented in the attached methodology markdown file.

## Component 4: Grantee name resolution

The recipient column in the raw filings is unreliable. A single university appears under dozens of variant spellings, abbreviations, truncations, and wrappers (e.g., “for benefit of”) across foundations and years. Filtering and aggregation require a clean canonical name, so each raw recipient string is resolved against a canonical anchor list and sorted into one of four categories, as follows:

1. Specific institution: resolves to a U.S. college or university with an IPEDS identifier.
2. Foreign: a non-U.S. institution (universities in Canada, UK, Israel, China, and elsewhere).
3. Not applicable: the grantee is not a degree-granting institution. Examples include college-access nonprofits, K-12 schools with “college” in the name, and professional associations.
4. Not clearly identified: the string is too truncated, garbled, or ambiguous to pin down, e.g. “THE UNIVERSITY OF” without specifying which university.

Name resolution runs as a layered pipeline that resolves the easy majority cheaply and reserves expensive methods for the residual entries. A deterministic rule waterfall handles exact and near-exact matches first. A character n-gram similarity pass catches typos and word reorderings. The residual goes to a lightweight large language model (Claude Haiku 4.5) that matches them against a web search. Human spot checks Each dashboard row preserves both the foundation’s original recipient string, for traceability, and the resolved canonical name, for filtering. The four-category taxonomy, the anchor vocabularies, the full pipeline, the reconciliation logic, and the prompt-design principles are documented in the attached methodology markdown.

## Why a language model, and why this one

The latter two components (topical tagging and the residual of grantee resolution) use a large language model. The alternatives are manual classification, keyword and regex rules, and fine-tuned smaller models. The model approach produces a measurably better dataset at a small fraction of the cost and time of any of them, and, unlike manual work, it can be audited and rebuilt by anyone with an API key.

Keyword rules in particular have limitations: they read “Yale Annual Fund” correctly as generic but read “general support for the Institute for Higher Education Leadership and Policy” as generic too, because the operating-language wrapper drowns out the domain content. A model given a clear rubric reads past the wrapper the way a human reader would.

The model used is Claude Haiku 4.5, chosen after direct comparison against a larger model on a sample where the larger model produced no measurably better output at order of magnitude higher costs. The two methodology files record the comparison and the run settings.

## Reproducibility and integrity

Reproducibility differs by component, and the appendix is explicit about it. The two extraction components rebuild the flat file from the public bucket exactly. The two classification components rebuild equivalent tag and canonical-name columns from that flat file, but require the reader to execute a manual audit-and-revise loop as described in the methodology files.

Code, markdowns, and data sets can be downloaded at this GitHub repository.

## Citation

- American Enterprise Institute for Public Policy Research, for the dashboard, code, and reproducibility instructions.
- U.S. Internal Revenue Service, for the underlying grant data (IRS Form 990-PF).
- GivingTuesday, for the consolidated and mirrored XML corpus.
- ProPublica, for the per-filing links to the XML viewer.

## Questions

We welcome your feedback on methodology and replication. Please contact AEI CFAU [here](https://cfau.aei.org/contact/).
