"""
Consolidated IRS 990-PF grant extractor.

Replaces the two prior scripts (pre-2014 and 2015+). A single run:

  1. Recursively walks EVERY subfolder beneath the directory this script lives in.
  2. Parses each .xml filing, handling both the pre-2014 and 2015+ schemas.
  3. Keeps only grantees that match the college/university filter.
  4. De-duplicates corrected re-filings: when one foundation (filer EIN) files
     more than once for the same tax year, only the filing with the LATEST
     <ReturnTs> timestamp is kept; the earlier filing's rows are discarded.
  5. Writes one flat CSV, "all_grants.csv", into the run directory.

Notes:
  - Grantee names are read from <RecipientBusinessName> and, when that is
    absent, from <RecipientPersonNm>.
  - The blank Grantee EIN column has been removed from the output.

Usage:  drop this file in the top of your data tree and run:  python irs_parse_xml_consolidated.py
"""

import os
import sys
import csv
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# === USER SETTINGS ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_CSV = os.path.join(BASE_DIR, "all_grants.csv")

# === HELPER FUNCTIONS ===

def get_namespace(root):
    """Extract the XML namespace from the root tag."""
    if root.tag.startswith("{"):
        return root.tag[1 : root.tag.find("}")]
    return ""

def find_first(elem, tag_variants, ns_uri):
    """Try multiple tag names, return first matching element or None."""
    if elem is None:
        return None
    for tag in tag_variants:
        full = f".//{{{ns_uri}}}{tag}" if ns_uri else f".//{tag}"
        found = elem.find(full)
        if found is not None:
            return found
    return None

def text_of(elem, tag_variants, ns_uri):
    """Return text of the first matching tag variant, or ''."""
    found = find_first(elem, tag_variants, ns_uri)
    return (found.text or "").strip() if (found is not None and found.text) else ""

def get_text(elem, path, ns_uri):
    """Safe helper to fetch text for a namespaced slash-separated path."""
    if elem is None:
        return ""
    full_path = ".//" + "/".join(f"{{{ns_uri}}}{part}" for part in path.split("/"))
    found = elem.find(full_path)
    return (found.text or "").strip() if (found is not None and found.text) else ""

def parse_return_ts(ts):
    """Parse an IRS <ReturnTs> into an aware datetime for comparison.
    Returns datetime.min (UTC) when missing/unparseable so it always loses
    the 'latest filing' comparison."""
    if not ts:
        return datetime.min.replace(tzinfo=timezone.utc)
    s = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Fall back to the date portion only.
        try:
            dt = datetime.fromisoformat(s[:10])
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

# === GRANTEE FILTERING ===

# Tier 1: broad keyword matches (case-insensitive substring)
INCLUDE_KEYWORDS = [
    "college",
    "university",
    "institute of technology",
    "polytechnic",
    "school of mines",
]

# Tier 2: explicit institution names that don't match ANY Tier 1 keyword
INCLUDE_NAMES = [
    "juilliard",
    "pratt institute",
    "cooper union",
    "leland stanford",
    "caltech",
]

# Tier 3: exclusion substrings (override Tier 1/2 matches)
EXCLUDE_KEYWORDS = [
    "college board",
]

def is_college_or_university(name):
    """Three-tier filter: exclusions first, then broad keywords, then names."""
    nl = name.lower()
    for exc in EXCLUDE_KEYWORDS:
        if exc in nl:
            return False
    for kw in INCLUDE_KEYWORDS:
        if kw in nl:
            return True
    for inst in INCLUDE_NAMES:
        if inst in nl:
            return True
    return False

# === PER-FILE PARSING ===

def parse_filing(xml_path):
    """Parse one XML filing.

    Returns a dict describing the filing, or None if it can't be used:
        {
          "filename":  basename,
          "filer_ein": str,
          "tax_year":  str,
          "return_ts": datetime (aware),
          "submission_id": str,   # leading digits of filename, secondary tiebreak
          "rows":      [ [filename, filer_name, filer_ein, tax_year,
                          grantee, amount, purpose], ... ]
        }
    """
    filename = os.path.basename(xml_path)

    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        print(f"\n  Skipping (parse error): {xml_path}")
        return None

    root = tree.getroot()
    ns_uri = get_namespace(root)
    ns = f"{{{ns_uri}}}" if ns_uri else ""

    # Tax year: prefer TaxYr/TaxYear; fall back to the year of TaxPeriodEndDt.
    tax_year = text_of(root, ["TaxYr", "TaxYear"], ns_uri)
    if not tax_year:
        period_end = text_of(root, ["TaxPeriodEndDt", "TaxPeriodEndDate"], ns_uri)
        if len(period_end) >= 4:
            tax_year = period_end[:4]

    # Filing timestamp (used to pick the latest of duplicate filings).
    return_ts = parse_return_ts(text_of(root, ["ReturnTs"], ns_uri))

    # Filer information
    filer = root.find(f".//{ns}Filer")
    if filer is None:
        print(f"\n  No <Filer> found in {filename}")
        return None

    filer_ein = text_of(filer, ["EIN"], ns_uri)

    # Filer name: 2015+ uses BusinessName/BusinessNameLine1Txt;
    # pre-2015 uses BusinessName/BusinessNameLine1 or Name/BusinessNameLine1.
    filer_name = get_text(filer, "BusinessName/BusinessNameLine1Txt", ns_uri)
    if not filer_name:
        filer_name = get_text(filer, "BusinessName/BusinessNameLine1", ns_uri)
    if not filer_name:
        filer_name = get_text(filer, "Name/BusinessNameLine1", ns_uri)

    # Grant groups under both schemas
    grant_tags = [
        f"{ns}GrantOrContributionPdDurYrGrp",   # 2015+
        f"{ns}GrantOrContriPaidDuringYear",      # pre-2015
    ]
    grants = []
    for tag in grant_tags:
        grants.extend(root.findall(f".//{tag}"))

    rows = []
    for g in grants:
        # Grantee name: RecipientBusinessName first, then RecipientPersonNm.
        grantee = get_text(g, "RecipientBusinessName/BusinessNameLine1Txt", ns_uri)
        if not grantee:
            grantee = get_text(g, "RecipientBusinessName/BusinessNameLine1", ns_uri)
        if not grantee:
            grantee = text_of(g, ["RecipientPersonNm", "RecipientPersonName"], ns_uri)

        if not is_college_or_university(grantee):
            continue

        # Amount: 2015+ uses Amt or CashGrantAmt; pre-2015 uses Amount.
        amount = text_of(g, ["Amt", "CashGrantAmt", "Amount"], ns_uri)

        # Purpose across schema variants.
        purpose = text_of(g, [
            "GrantOrContributionPurposeTxt",
            "PurposeOfGrantTxt",
            "PurposeOfGrantOrContribution",
        ], ns_uri)

        rows.append([
            filename,
            filer_name,
            filer_ein,
            tax_year,
            grantee,
            amount,
            purpose,
        ])

    # Leading digits of the filename serve as a secondary tiebreak.
    submission_id = ""
    for ch in filename:
        if ch.isdigit():
            submission_id += ch
        else:
            break

    return {
        "filename": filename,
        "filer_ein": filer_ein,
        "tax_year": tax_year,
        "return_ts": return_ts,
        "submission_id": submission_id,
        "rows": rows,
    }

# === MAIN SCRIPT ===

def main():
    # Collect every .xml file beneath BASE_DIR, recursively.
    xml_paths = []
    for dirpath, _dirnames, filenames in os.walk(BASE_DIR):
        for fn in filenames:
            if fn.lower().endswith(".xml"):
                xml_paths.append(os.path.join(dirpath, fn))

    if not xml_paths:
        print("No XML files found beneath this directory.")
        return

    total = len(xml_paths)
    print(f"Found {total} XML files. Parsing...")

    # Group filings by (filer EIN, tax year); keep the latest by ReturnTs.
    # Key uses the file path when EIN is blank so distinct filers don't merge.
    best = {}  # key -> filing dict
    for i, xml_path in enumerate(xml_paths, 1):
        filing = parse_filing(xml_path)
        if filing is not None:
            ein = filing["filer_ein"] or f"__noein__:{xml_path}"
            key = (ein, filing["tax_year"])
            cur = best.get(key)
            if cur is None:
                best[key] = filing
            else:
                # Keep the later filing; tiebreak on submission id.
                newer = (filing["return_ts"], filing["submission_id"]) > \
                        (cur["return_ts"], cur["submission_id"])
                if newer:
                    best[key] = filing
        sys.stdout.write(f"\r  Processing: {i}/{total} files")
        sys.stdout.flush()
    print()

    # Flatten rows from the kept filings.
    all_rows = []
    for filing in best.values():
        all_rows.extend(filing["rows"])

    headers = [
        "XML file",
        "Filer name",
        "Filer EIN",
        "Return year",
        "Grantee",
        "Grant amount",
        "Grant purpose",
    ]

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(all_rows)

    print(f"Kept {len(best)} unique filings (filer + tax year) "
          f"after de-duplicating corrections.")
    print(f"Wrote {len(all_rows)} grant rows to {os.path.basename(OUTPUT_CSV)}")

if __name__ == "__main__":
    main()
