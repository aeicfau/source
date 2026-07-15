# Changelog

## 2026-07-15

Consolidated `990pf-downloader/download_990pf.py` and `990pf-parser/irs_parse_xml_consolidated.py` into a single self-contained pipeline, `990-grant-pipeline/irs-990-pipeline.py`. The two prior scripts are now fully superseded and have been removed from the repo.

- Fixed a systemic matching bug (an admin/system-office entry was falsely tying with a real flagship campus on city), recovering a large share of previously-unresolved institutions.
- Corrected roughly 150 specific institution name-resolution errors surfaced during manual audit, and fixed a data bug where two different real institutions (Queens College and City College of New York) had been incorrectly merged onto one placeholder ID.
- US territories (Puerto Rico, Guam, U.S. Virgin Islands, American Samoa, Northern Mariana Islands) now get their full formal name in the state field, not the postal abbreviation.
- Simplified the canonical institution table's schema: `canonical_id` now serves as the IPEDS unitid directly for real institutions, so a separate `ipeds_unitid` column is no longer needed.
- Standardized the three SQL-upload file formats (institutions, grants, processed-returns) with explicit, documented column contracts.
- Added tracking of whether a filer is itself a college/school/hospital/supporting-organization (based on the 7 IRS Schedule A flags) to the processed-returns table.
- Rewrote `990-grant-pipeline/README.md` and `PRD_Form990_HigherEd_Grants_Pipeline.md` to match the schema and design changes above.

## 2026-06-25

In response to external feedback:
- Corrected disambiguation of similarly named institutions; Trinity Washington University (Washiginton, DC) and Trinity University (San Antonio, TX) are now correctly resolved as distinct entities.
- Added the inferred IPEDS ID to institution profiles.
