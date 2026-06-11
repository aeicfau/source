# 990-PF Downloader

`download_990pf.py` downloads IRS **Form 990-PF** e-file XML returns from the
public [GivingTuesday 990 Data Lake](https://990data.givingtuesday.org/access-via-aws-account-2/)
(`s3://gt990datalake-rawdata`, us-east-1) and sorts each file into a folder
named for the year it was filed:

```
990pf_xml/
  2024/202412...._public.xml
  2025/202541...._public.xml
  manifest.csv
```

## Run it

```bash
python3 download_990pf.py --output-dir ./990pf_xml
```

No `pip install`, no AWS account, no credentials. Standard library only,
Python 3.8+. The bucket is read anonymously over HTTPS.

**What to expect when it runs.** It works in two phases:

1. **Fetch the index (one-time).** The master index is ~3 GB. It downloads to a
   local cache file (`<output>/_index_*.csv`) with a byte-by-byte progress
   readout. This download is *resumable*: a dropped connection or read timeout
   reconnects and continues from where it stopped instead of failing the run.
   The cache is reused on later runs, so you pay this cost once.
2. **Download the returns.** The local index is parsed and matching 990-PF XMLs
   download in parallel, appearing immediately in `<output>/<year>/`, with a
   heartbeat every ~10 seconds.

A `--years`-filtered run still reads the whole index (matching rows are
scattered through it), but parsing a local file is fast; the time cost is the
one-time index download.

## Common options

| Flag | Effect |
|------|--------|
| `--years 2023 2024` | Only those filing years |
| `--year-field tax` | Folder by tax year covered instead of year filed |
| `--workers 16` | Parallel downloads (default 8) |
| `--limit 50` | Stop after N matches (testing) |
| `--dry-run` | List what would download, write nothing |
| `--overwrite` | Re-fetch existing XMLs |
| `--refresh-index` | Re-download the index instead of using the cache |
| `--index-file PATH` | Use a local index CSV you already have |
| `--form-type 990` | Grab a different form instead of 990-PF |

## Notes

- **Resumable on both phases.** A partial index continues; already-downloaded
  XMLs are skipped. If a run dies, just run the same command again.
- **"Year filed" vs "tax year."** Default folders use the IRS submission date
  (`SubmittedOn`) -- the year the return was *filed*, typically 1-2 years after
  the tax year it reports on. Use `--year-field tax` for the latter.
- The newest master index is auto-detected; override with `--index-key`.
- Scale: the full 990-PF universe is large (hundreds of thousands of returns,
  tens of GB of XML, plus the ~3 GB index). Use `--years` to scope it down.

Public domain / CC0.
