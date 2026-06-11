#!/usr/bin/env python3
"""
download_990pf.py
=================

Download IRS **Form 990-PF** (private foundation) e-file XML returns from the
GivingTuesday 990 Data Lake and sort each file into a folder named for the
year it was filed.

Public bucket (anonymous, no credentials):
    s3://gt990datalake-rawdata   (region us-east-1)

Each kept return lands in  <output>/<year>/<ObjectId>_public.xml .

How it works
------------
1. **Fetch the index once, to disk, with resume.** The master index is ~3 GB.
   It is downloaded to a local cache file using HTTP Range requests, so a
   dropped connection or read timeout resumes from the last byte instead of
   killing the whole run. The cache is reused on later runs.
2. **Stream the local index and download matches.** 990-PF rows download in
   parallel as the index is parsed; files start appearing immediately. Each XML
   download retries on transient errors.

Both phases are resumable: re-running the same command finishes an interrupted
job (already-downloaded XMLs are skipped, a partial index continues).

Redistributable: standard library only. Python 3.8+. No pip install, no AWS
account. License: Public domain / CC0.

"Year filed"
------------
Default folders use the IRS submission date (`SubmittedOn`) -- the year the
return was *filed*, usually 1-2 years after the tax year it covers. Use
`--year-field tax` to organize by tax year instead. Corrupt dates in the source
(e.g. "7202", "2222") fall into an `unknown` folder.

Usage
-----
    python3 download_990pf.py --output-dir ./990pf
    python3 download_990pf.py -o ./990pf --years 2022 2023 --workers 16
    python3 download_990pf.py -o ./990pf --dry-run --limit 50
    python3 download_990pf.py -o ./990pf --year-field tax
"""

from __future__ import annotations

import argparse
import csv
import gzip
import http.client
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
from xml.etree import ElementTree as ET

# --------------------------------------------------------------------------- #
# Constants -- the public GivingTuesday 990 Data Lake
# --------------------------------------------------------------------------- #
BUCKET = "gt990datalake-rawdata"
BASE_URL = f"https://{BUCKET}.s3.amazonaws.com"
INDEX_PREFIX = "Indices/990xmls/"
XML_PREFIX = "EfileData/XmlFiles/"
S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"

USER_AGENT = "download_990pf/2.0 (+https://990data.givingtuesday.org)"
CHUNK = 1 << 16  # 64 KiB

# Plausible filing/tax year range. The source index contains a few corrupt
# dates (e.g. "7202", "2222"); anything outside this window -> "unknown" folder.
MIN_YEAR = 2000
MAX_YEAR = datetime.now().year + 1

_lock = threading.Lock()
_stats = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}

# Exceptions that mean "connection hiccup, retry / resume".
_TRANSIENT = (urllib.error.URLError, http.client.HTTPException, OSError)


# --------------------------------------------------------------------------- #
# Low-level HTTP
# --------------------------------------------------------------------------- #
def _request(url: str, headers=None, method="GET", timeout=120):
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=h, method=method), timeout=timeout
    )


def _head_length(url: str, timeout=60):
    """Return Content-Length for a URL, or 0 if unknown."""
    try:
        with _request(url, method="HEAD", timeout=timeout) as r:
            return int(r.headers.get("Content-Length") or 0)
    except _TRANSIENT:
        return 0


def download_resumable(url: str, dest: str, label: str,
                       retries: int = 30, timeout: int = 120) -> None:
    """Download `url` to `dest`, resuming with HTTP Range on any interruption.

    Robust to read timeouts and dropped connections on very large files: on
    failure it reconnects with `Range: bytes=<pos>-` and continues appending.
    """
    total = _head_length(url)
    pos = os.path.getsize(dest) if os.path.exists(dest) else 0
    if total and pos > total:        # stale/corrupt partial -> start over
        pos = 0
    if total and pos == total:       # already complete
        print(f"  {label}: cached ({total / 1e6:,.0f} MB)", flush=True)
        return

    attempt = 0
    last_print = 0.0
    while True:
        try:
            headers = {"Range": f"bytes={pos}-"} if pos else {}
            resp = _request(url, headers=headers, timeout=timeout)
            # If the server ignored Range and sent the whole file, restart clean.
            if pos and getattr(resp, "status", 206) == 200:
                pos = 0
            mode = "ab" if pos else "wb"
            with open(dest, mode) as fh:
                while True:
                    buf = resp.read(CHUNK)
                    if not buf:
                        break
                    fh.write(buf)
                    pos += len(buf)
                    now = time.time()
                    if total and now - last_print >= 5:
                        pct = pos * 100 // total
                        print(f"  {label}: {pos / 1e6:,.0f}/{total / 1e6:,.0f} MB "
                              f"({pct}%)", flush=True)
                        last_print = now
            resp.close()
            if not total or pos >= total:
                if total:
                    print(f"  {label}: {total / 1e6:,.0f} MB complete", flush=True)
                return
            # Stream ended early with no error -> loop and resume via Range.
        except _TRANSIENT as exc:
            attempt += 1
            if attempt > retries:
                raise
            wait_s = min(2 ** attempt, 30)
            sys.stderr.write(f"  {label}: {type(exc).__name__} at "
                             f"{pos / 1e6:,.0f} MB; resuming in {wait_s}s "
                             f"(retry {attempt}/{retries})\n")
            time.sleep(wait_s)


# --------------------------------------------------------------------------- #
# Step 1: find the newest master index file in the bucket
# --------------------------------------------------------------------------- #
def list_bucket(prefix: str):
    """Yield (key, last_modified) for every object under `prefix` (paginated)."""
    token = None
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        url = f"{BASE_URL}/?{urllib.parse.urlencode(params)}"
        with _request(url, timeout=60) as resp:
            root = ET.parse(resp).getroot()
        for contents in root.findall(f"{{{S3_NS}}}Contents"):
            key = contents.findtext(f"{{{S3_NS}}}Key", default="")
            lm = contents.findtext(f"{{{S3_NS}}}LastModified", default="")
            if key:
                yield key, lm
        if root.findtext(f"{{{S3_NS}}}IsTruncated", default="false").lower() != "true":
            break
        token = root.findtext(f"{{{S3_NS}}}NextContinuationToken")
        if not token:
            break


_DATE_IN_NAME = re.compile(r"(\d{4})[-_](\d{2})[-_](\d{2})")


def find_latest_index() -> str:
    """Return the S3 key of the newest 'all years' index CSV."""
    candidates = []
    for key, lm in list_bucket(INDEX_PREFIX):
        name = key.rsplit("/", 1)[-1].lower()
        if not name.endswith((".csv", ".csv.gz")):
            continue
        m = _DATE_IN_NAME.search(name)
        date_key = "".join(m.groups()) if m else ""
        priority = 1 if "all_years" in name else 0
        candidates.append(((priority, date_key, lm), key))
    if not candidates:
        raise RuntimeError(
            f"No CSV index found under {BASE_URL}/{INDEX_PREFIX} . "
            "Pass --index-key or --index-file explicitly."
        )
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


# --------------------------------------------------------------------------- #
# Step 2: parse the (local) index
# --------------------------------------------------------------------------- #
def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _get(row: dict, *candidates: str):
    for c in candidates:
        if c in row and row[c] not in (None, "", "null", "NULL"):
            return row[c]
    return None


def iter_index_rows(path: str):
    """Stream a local index CSV (optionally .gz) -> dicts with normalized keys."""
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header:
            return
        cols = [_norm(h) for h in header]
        for values in reader:
            if values:
                yield dict(zip(cols, values))


def _plausible(value):
    if value and len(value) >= 4 and value[:4].isdigit():
        y = int(value[:4])
        if MIN_YEAR <= y <= MAX_YEAR:
            return value[:4]
    return None


def filing_year(row: dict, year_field: str) -> str:
    """Folder year for a row; corrupt/out-of-range dates -> 'unknown'."""
    if year_field == "tax":
        fields = ("taxyear", "taxperiodenddate", "taxperiod")
    else:
        fields = ("submittedon", "returnts", "datesigned", "indexedon")
    for field in fields:
        y = _plausible(_get(row, field))
        if y:
            return y
    return "unknown"


def xml_key_for(row: dict):
    url = _get(row, "url")
    if url and ".amazonaws.com/" in url:
        return url.split(".amazonaws.com/", 1)[1]
    object_id = _get(row, "objectid")
    if object_id:
        return f"{XML_PREFIX}{object_id}_public.xml"
    return None


# --------------------------------------------------------------------------- #
# Step 3: download one return (with retries)
# --------------------------------------------------------------------------- #
def download_one(key: str, dest_path: str, overwrite: bool,
                 retries: int = 4, timeout: int = 60) -> str:
    """Download a single XML. Returns 'downloaded' | 'skipped' | 'failed'."""
    if not overwrite and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        with _lock:
            _stats["skipped"] += 1
        return "skipped"

    url = f"{BASE_URL}/{urllib.parse.quote(key)}"
    tmp = dest_path + ".part"
    for attempt in range(retries):
        try:
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            n = 0
            with _request(url, timeout=timeout) as resp, open(tmp, "wb") as fh:
                while True:
                    buf = resp.read(CHUNK)
                    if not buf:
                        break
                    fh.write(buf)
                    n += len(buf)
            os.replace(tmp, dest_path)  # atomic; partials never look complete
            with _lock:
                _stats["downloaded"] += 1
                _stats["bytes"] += n
            return "downloaded"
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # permanent
                break
            time.sleep(1.5 ** attempt)
        except _TRANSIENT:
            time.sleep(1.5 ** attempt)
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    with _lock:
        _stats["failed"] += 1
    sys.stderr.write(f"  ! failed {key}\n")
    return "failed"


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def resolve_index(args) -> str:
    """Return a path to a local index file, downloading/caching it if needed."""
    if args.index_file:
        if not os.path.exists(args.index_file):
            raise FileNotFoundError(args.index_file)
        print(f"Index:   {args.index_file} (local)", flush=True)
        return args.index_file

    index_key = args.index_key or find_latest_index()
    url = f"{BASE_URL}/{urllib.parse.quote(index_key)}"
    os.makedirs(args.output_dir, exist_ok=True)
    local = os.path.join(args.output_dir, "_index_" + index_key.rsplit("/", 1)[-1])
    if args.refresh_index and os.path.exists(local):
        os.remove(local)
    print(f"Index:   {url}", flush=True)
    print(f"Caching: {local}", flush=True)
    print("Fetching index (one-time, resumable)...", flush=True)
    download_resumable(url, local, "index")
    return local


def run(args) -> int:
    local_index = resolve_index(args)
    print(f"Output:  {os.path.abspath(args.output_dir)}", flush=True)
    print(f"Filter:  FormType == {args.form_type} | folder by {args.year_field} year",
          flush=True)
    if args.years:
        print(f"Years:   {', '.join(sorted(args.years))}", flush=True)
    if args.dry_run:
        print("Mode:    DRY RUN (no files will be written)", flush=True)
    print("\nDownloading returns as the index is parsed...\n", flush=True)

    want_form = _norm(args.form_type)
    year_set = set(args.years) if args.years else None

    manifest = mw = None
    if args.manifest and not args.dry_run:
        os.makedirs(args.output_dir, exist_ok=True)
        manifest = open(os.path.join(args.output_dir, "manifest.csv"), "w", newline="")
        mw = csv.writer(manifest)
        mw.writerow(["object_id", "year", "s3_key", "dest", "status"])

    seen = set()
    scanned = matched = done = 0
    dry_examples = []
    t0 = last = time.time()
    max_inflight = max(args.workers * 8, 16)
    pool = None if args.dry_run else ThreadPoolExecutor(max_workers=args.workers)
    inflight = {}

    def heartbeat(force=False):
        nonlocal last
        now = time.time()
        if force or now - last >= 10:
            rate = scanned / max(now - t0, 1e-6)
            print(f"  scanned {scanned:,} rows ({rate:,.0f}/s) | matched {matched:,} | "
                  f"downloaded {_stats['downloaded']:,} | skipped {_stats['skipped']:,} | "
                  f"failed {_stats['failed']:,}", flush=True)
            last = now

    def harvest(block):
        nonlocal done
        if not inflight:
            return
        ready = (wait(list(inflight), return_when=FIRST_COMPLETED)[0]
                 if block else [f for f in list(inflight) if f.done()])
        for fut in ready:
            key, dest = inflight.pop(fut)
            status = fut.result()
            done += 1
            if mw:
                year = os.path.basename(os.path.dirname(dest))
                mw.writerow([os.path.basename(dest).split("_")[0], year, key, dest, status])

    try:
        for row in iter_index_rows(local_index):
            scanned += 1
            heartbeat()
            if _norm(_get(row, "formtype") or "") != want_form:
                continue
            year = filing_year(row, args.year_field)
            if year_set and year not in year_set:
                continue
            key = xml_key_for(row)
            if not key:
                continue
            object_id = _get(row, "objectid") or key.rsplit("/", 1)[-1]
            if object_id in seen:
                continue
            seen.add(object_id)
            matched += 1
            dest = os.path.join(args.output_dir, year, os.path.basename(key))

            if args.dry_run:
                if len(dry_examples) < 20:
                    dry_examples.append(f"  would download {key} -> {dest}")
            else:
                inflight[pool.submit(download_one, key, dest, args.overwrite)] = (key, dest)
                harvest(block=False)
                while len(inflight) >= max_inflight:
                    harvest(block=True)

            if args.limit and matched >= args.limit:
                print(f"  reached --limit {args.limit}, stopping index scan", flush=True)
                break

        while inflight:
            harvest(block=True)
    finally:
        if pool:
            pool.shutdown(wait=True)
        if manifest:
            manifest.close()

    heartbeat(force=True)
    print(f"\nScanned {scanned:,} rows; {matched:,} {args.form_type} returns matched.",
          flush=True)

    if args.dry_run:
        for line in dry_examples:
            print(line)
        if matched > len(dry_examples):
            print(f"  ... and {matched - len(dry_examples):,} more")
        return 0

    print(f"Done. downloaded={_stats['downloaded']:,} skipped={_stats['skipped']:,} "
          f"failed={_stats['failed']:,} ({_stats['bytes'] / 1e6:,.1f} MB)", flush=True)
    return 1 if _stats["failed"] else 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Download IRS Form 990-PF XML returns from the public "
                    "GivingTuesday 990 Data Lake, sorted into per-year folders.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-o", "--output-dir", default="990pf_xml",
                   help="Root folder; XMLs go in <output>/<year>/.")
    p.add_argument("--form-type", default="990PF",
                   help="FormType to keep (990PF, 990, 990EZ, ...).")
    p.add_argument("--year-field", choices=("submitted", "tax"), default="submitted",
                   help="'submitted' = year filed with IRS; 'tax' = tax year covered.")
    p.add_argument("--years", nargs="+", metavar="YYYY",
                   help="Only download these years (space separated).")
    p.add_argument("--workers", type=int, default=8, help="Parallel download threads.")
    p.add_argument("--limit", type=int, default=0,
                   help="Stop after N matched returns (0 = no limit). Good for testing.")
    p.add_argument("--index-file", default=None,
                   help="Use this local index CSV instead of downloading one.")
    p.add_argument("--index-key", default=None,
                   help="Override the index S3 key instead of auto-detecting the newest.")
    p.add_argument("--refresh-index", action="store_true",
                   help="Re-download the index even if a cached copy exists.")
    p.add_argument("--overwrite", action="store_true",
                   help="Re-download XMLs even if they already exist.")
    p.add_argument("--no-manifest", dest="manifest", action="store_false",
                   help="Do not write manifest.csv.")
    p.add_argument("--dry-run", action="store_true",
                   help="List what would be downloaded; write nothing.")
    p.set_defaults(manifest=True)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run the same command to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
