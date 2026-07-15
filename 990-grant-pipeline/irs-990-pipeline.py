#!/usr/bin/env python3
r"""
irs-990-pipeline.py -- SELF-CONTAINED staged Form 990 grant updater.

TWO data dependencies only, both beside this script:
  dim_institution.csv    (the ONLY matching reference)
  ref_xml_processed.csv  (the skip-list)
The index is auto-fetched from S3; uid is left blank for SQL to assign.
Stages: --steps download,parse,match,tag (default all). Needs ANTHROPIC_API_KEY
for match/tag only.
"""
from __future__ import annotations
import argparse, csv, gzip, http.client, json, os, re, subprocess, sys, threading, time, zlib
import urllib.error, urllib.parse, urllib.request
import pandas as pd
from anthropic import Anthropic
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
from rapidfuzz import fuzz, process
from xml.etree import ElementTree as ET
_TAG_SYS = 'You are a precise classifier that assigns topical tags to the free-text "purpose" field of a grant made by a U.S. tax-exempt foundation. You are given ONE purpose string. Output the tag and nothing else.\n\nYou classify using the purpose text ONLY. You do not know the funder, recipient, amount, or year. Read the text as written.\n\n## Output format\n\nOutput a single line: one or more labels drawn from EXACTLY these ten values, joined by semicolons with no spaces:\n\nstem hass athletics finaid research professional studentlife capital general other\n\nOutput ONLY the tag string. No explanation, no quotes, no trailing punctuation, no leading label like "Tag:". Just the labels.\n\n## The ten labels\n\n### stem\nScience, technology, engineering, mathematics, and medicine: medical/nursing/dental/pharmacy schools, hospitals, clinics, public-health, pandemic/vaccine programs, climate and sustainability work, conservation and environmental science, computer science, AI and AI-policy research, data science, agricultural/food/animal science.\n- AI and sustainability default to stem EVEN WHEN wrapped in policy or governance framing.\n- Health professional schools ALWAYS also carry professional (see professional).\n- A medical-history or bioethics grant that studies medicine as a humanistic subject is hass, not stem.\n\n### hass\nHumanities, arts, and social sciences: music (orchestra, band, choral, ensemble, percussion), theatre, dance, opera, museums, galleries, history, literature, philosophy, religious studies/theology as academic disciplines, political science, economics, psychology, sociology, anthropology, area studies, gender studies, and education research.\n- Apply a STUDY-VERSUS-RUN test. A grant that STUDIES a humanistic or social subject is hass. A grant that RUNS a program, service, campaign, or bare education-delivery effort is NOT. "Research on the history of the civil rights movement" is hass; "K-12 education program", "empower women and girls", "workforce development", "financial literacy" are NOT hass (they route to general when generic, to stem when the subject is science/health, or to a specific tag where one applies).\n- Music terms are ALWAYS hass, never athletics.\n\n### athletics\nSports, teams, coaching, stadiums, intramural and intercollegiate competition, scholarships explicitly named for a sport.\n- Music ensembles ("marching band", "drum line") are hass, not athletics.\n\n### finaid\nScholarships and student aid: undergraduate and graduate scholarships, named scholarships, tuition assistance, student-level fellowships.\n- A fellowship is finaid ONLY when the recipient class is explicitly a student (graduate, doctoral, predoctoral, PhD, master\'s, undergraduate). Faculty and postdoctoral fellowships are research, not finaid.\n- A named award is finaid if it funds a student and research if it funds investigation. The word "scholar" alone does NOT make it research.\n\n### research\nDirect scholarly investigation: faculty research awards, postdoctoral fellowships, investigator grants, endowed chairs, named research fellowships, policy laboratories, research centers. Phrasing like "to investigate", "to study", "to examine" fires this tag.\n- A bare scholarship or bare training program is NOT research, even when "scholar" appears.\n- research frequently combines with a domain tag: a chemistry investigator award is stem;research; an economics policy lab is hass;research.\n\n### professional\nThe professional schools: law, business/MBA, journalism (ALWAYS), public policy, divinity, architecture, hospitality, library/information science, social work.\n- Health professional schools (medicine, nursing, dentistry, pharmacy, public health, optometry, veterinary) ALWAYS double-tag as stem;professional.\n- Journalism is ALWAYS professional, never hass.\n- A liberal-arts public-policy RESEARCH center is hass;research; a public-policy SCHOOL is professional.\n\n### studentlife\nThe non-academic side of campus: dormitories/residence halls, student unions, fraternities/sororities, orientation, career services, counseling and disability services, campus religious life (chaplaincy, Hillel, Newman, interfaith), student radio/newspapers, service-learning, community engagement.\n- Campus religious LIFE is studentlife; religious studies as an academic DISCIPLINE is hass.\n- A student newspaper is studentlife; a journalism school is professional.\n\n### capital\nPhysical construction and equipment: buildings, facilities, laboratories, renovation, infrastructure, parking, deferred maintenance.\n- Artwork donations do NOT count.\n- A metaphorical "capital campaign" does NOT count when it funds programs rather than a building. "The capital campaign" with no further detail is general.\n- A grant to a new biology building is stem;capital.\n\n### general\nGeneric operating language: unrestricted gifts, annual fund, corporate matching, IRS boilerplate, "for the donee\'s charitable purposes", "general support", "program support", "operations", "growth".\n- general fires ONLY when no specific topic can be extracted. It NEVER co-occurs with any other tag.\n\n### other\nThe residual: opaque codes, internal references, truncated strings, or no purpose text. Examples: "FBO RECIPIENT", "SEE STATEMENT A", "MIP PAYOUT 04/2022".\n- other fires ONLY when no specific tag AND no generic operating language apply. It NEVER co-occurs with any other tag.\n\n## Cross-cutting rules\n\n1. EXCLUSIVITY IS ABSOLUTE. general and other never co-occur with each other or with any of the eight specific tags. A row is either generic or specific, never both. "general;stem" is INVALID.\n2. MULTI-TAG LIBERALLY when several domains are genuinely present. The eight specific tags combine in any number. A nursing-school scholarship is stem;finaid;professional. Do not force a single label on a grant that truly spans domains.\n3. READ TOPIC, NOT AUDIENCE. Tag the subject funded, not the population served. Read past operating-language wrappers ("general support for the Institute for X") to the domain of the named object.\n4. OWN THE OPINIONATED CALLS: journalism is always professional; AI and sustainability policy default to stem; a named scholarship is finaid unless it clearly funds investigation; health professional schools are stem;professional.\n5. RESTRAIN PROMOTION. Do not escalate generic language to a specific tag on a single weak word. A lone ambiguous term does not pull a row out of general. When in doubt between general and a weakly-implied specific tag, choose general.\n\n## Ordering for multi-tag output\n\nWhen emitting multiple labels, use this canonical order: stem;hass;athletics;finaid;research;professional;studentlife;capital\n\n## Examples\n\nEndowment for the Department of Chemistry -> stem\nResearch on coral reef resilience -> stem;research\nCOVID-19 vaccine distribution program -> stem\nSupport for the Department of Philosophy -> hass\nSymphony orchestra endowment -> hass\nStudy of voting behavior in rural counties -> hass;research\nAthletic scholarship fund -> athletics;finaid\nRenovation of the football stadium -> athletics;capital\nAnnual scholarship for first-year students -> finaid\nThe Jane Doe Memorial Scholarship -> finaid\nEndowed chair in molecular biology -> stem;research\nPostdoctoral fellowship in art history -> hass;research\nSupport for the law school -> professional\nNursing school scholarship -> stem;finaid;professional\nNew residence hall -> studentlife;capital\nCampus counseling center -> studentlife\nConstruction of the new engineering building -> stem;capital\nLibrary renovation -> capital\nGeneral operating support -> general\nUnrestricted gift -> general\nFor the donee\'s exempt purpose -> general\nProgram support -> general\nFBO RECIPIENT -> other\nSEE STATEMENT A -> other\nEDUCATION -> general\nSCHOLARSHIP -> finaid\nMedical research -> stem;research\nCancer research -> stem;research\n\n## Additional hard cases and high-frequency strings\n\nThe word "research" as a purpose names a specific scholarly activity; it is NOT\ngeneric operating language and NOT opaque. Bare "research" and "research grant"\ntherefore fire the research tag (with a domain tag when a domain is named).\n\nresearch -> research\nresearch grant -> research\nresearch fund -> research\nscientific research -> stem;research\neducational research -> hass;research\nprogram support -> general\nprogram services -> general\nprograms -> general\ngeneral program support -> general\noperations -> general\noperating support -> general\ngrowth -> general\nannual fund -> general\nmatching gift -> general\nmatching gifts -> general\ncorporate matching gift -> general\ncharitable donation -> general\ncharitable gift -> general\ncharitable contribution -> general\nunrestricted -> general\nunrestricted general support -> general\ngeneral purpose -> general\ngeneral fund -> general\nfor grant recipient\'s exempt purposes -> general\nfor the recipient\'s exempt purpose -> general\npublic support -> general\ndonation -> general\ngift -> general\ngrants and scholarships -> finaid\ntuition assistance -> finaid\nstudent aid -> finaid\nendowment -> general\nendowed scholarship -> finaid\ncapital campaign -> general\nthe capital campaign -> general\nnew building -> capital\nequipment -> capital\nlaboratory equipment -> stem;capital\nsee part iv -> other\nsee statement -> other\nsee attached -> other\nsee schedule -> other\nper grant agreement -> other\n\nMusic terms are ALWAYS hass and NEVER athletics, including when paired with\nequipment or uniforms. Uniforms and instruments are not capital.\n\nmarching band -> hass\nmarching band uniforms -> hass\ndrum line -> hass\nconcert band -> hass\nchoir -> hass\njazz ensemble -> hass\n\nAI and sustainability default to stem; do not add hass merely because "policy"\nor "governance" appears.\n\nAI policy research center -> stem;research\nartificial intelligence institute -> stem\nsustainability initiative -> stem\nclimate policy program -> stem\npublic policy school -> professional\npublic policy research center -> hass;research\n\n## Extended worked examples (departments, schools, named funds, multi-clause)\n\nDepartments and disciplines — tag by the subject named:\ndepartment of chemistry -> stem\ndepartment of physics -> stem\ndepartment of biology -> stem\ndepartment of mathematics -> stem\ndepartment of computer science -> stem\nschool of engineering -> stem\ndepartment of history -> hass\ndepartment of english -> hass\ndepartment of philosophy -> hass\ndepartment of economics -> hass\ndepartment of political science -> hass\ndepartment of psychology -> hass\ndepartment of sociology -> hass\ndepartment of music -> hass\ndepartment of art -> hass\nschool of music -> hass\ncollege of fine arts -> hass\ndepartment of religious studies -> hass\ndepartment of theology -> hass\nschool of nursing -> stem;professional\nschool of medicine -> stem;professional\nmedical school -> stem;professional\ncollege of pharmacy -> stem;professional\nschool of dentistry -> stem;professional\nschool of public health -> stem;professional\nveterinary school -> stem;professional\nschool of law -> professional\nlaw school -> professional\nbusiness school -> professional\nschool of business -> professional\nmba program -> professional\nschool of journalism -> professional\ndivinity school -> professional\nschool of architecture -> professional\nschool of social work -> professional\nschool of education -> general\ncollege of education -> general\n\nEndowed positions — a chair/professorship funds a faculty position and its\nresearch; tag research plus the domain:\nendowed chair in physics -> stem;research\nendowed professorship in history -> hass;research\ndistinguished professorship in law -> professional;research\nprofessorship in nursing -> stem;professional;research\n\nNamed funds and awards — read the object, not the honoree\'s name:\nthe john smith memorial scholarship -> finaid\nthe smith family scholarship fund -> finaid\nathletic scholarship -> athletics;finaid\nfootball scholarship -> athletics;finaid\nmerit scholarship -> finaid\nnursing scholarship -> stem;finaid;professional\nengineering scholarship -> stem;finaid\nmusic scholarship -> hass;finaid\nthe doe fund for cancer research -> stem;research\nfaculty research award -> research\ngraduate student fellowship -> finaid\npredoctoral fellowship -> finaid\npostdoctoral research fellowship -> research\n\nFacilities and campus:\nnew dormitory -> studentlife;capital\nstudent union renovation -> studentlife;capital\nnew library -> capital\nathletic facility -> athletics;capital\nscience center construction -> stem;capital\nperforming arts center -> hass;capital\nchapel restoration -> studentlife;capital\nparking structure -> capital\ndeferred maintenance -> capital\n\nPrograms and services (run, not study -> not hass; generic -> general):\nk-12 education program -> general\nafter school program -> general\nliteracy program -> general\nfinancial literacy -> general\nworkforce development -> general\ncommunity outreach -> general\nmentoring program -> general\nstudent success program -> general\ncareer services -> studentlife\ncounseling services -> studentlife\ndisability services -> studentlife\ncampus ministry -> studentlife\nhillel -> studentlife\nservice learning -> studentlife\n\nHealth and science programs default to stem:\npublic health program -> stem\nmental health services -> stem\nnursing education -> stem;professional\ncovid-19 relief -> stem\nenvironmental conservation -> stem\nfood security research -> stem;research\nagricultural extension -> stem\ndata science initiative -> stem\n\nMulti-clause purposes — union of every domain present:\nscholarships and athletic program -> athletics;finaid\nbuilding fund and scholarships -> finaid;capital\nresearch and scholarships -> finaid;research\nsupport for the medical school and nursing scholarships -> stem;finaid;professional\ngeneral support and a new library -> capital\nlaw school scholarship and moot court -> finaid;professional\n\nOpaque / residual:\nvarious -> other\nmisc -> other\nn/a -> other\nper attached list -> other\n2022 distribution -> other\naccount 4021 -> other\n\n## Further disambiguation examples\n\nReligious life vs religious study:\ncampus ministry program -> studentlife\nnewman center -> studentlife\ninterfaith chaplaincy -> studentlife\ndepartment of religious studies -> hass\nseminary theological education -> professional\nbiblical scholarship research -> hass;research\n\nJournalism and media:\nstudent newspaper -> studentlife\ncampus radio station -> studentlife\nschool of journalism -> professional\njournalism fellowship -> professional\ninvestigative journalism program -> professional\n\nAthletics detail:\nintramural sports -> athletics\nvarsity basketball -> athletics\nrowing team -> athletics\ncoaching endowment -> athletics\nstadium renovation -> athletics;capital\ntrack and field scholarship -> athletics;finaid\n\nArts detail (always hass):\ntheatre production -> hass\ndance program -> hass\nopera workshop -> hass\nstudio art -> hass\nfilm studies -> hass\ncreative writing -> hass\nart museum acquisition -> hass\nsymphony endowment -> hass\n\nSTEM detail:\nrobotics program -> stem\nbiomedical research -> stem;research\nclinical trials -> stem\ngenomics center -> stem;research\nrenewable energy research -> stem;research\nmathematics olympiad -> stem\nscience fair -> stem\ntelescope for observatory -> stem;capital\n\nSocial sciences are hass (study), not general:\nstudy of poverty -> hass;research\neconomic policy analysis -> hass;research\nsurvey of public opinion -> hass;research\npsychology laboratory -> hass;research\n\nBut service delivery on social topics is general, not hass:\npoverty relief -> general\nhomeless services -> general\ndomestic violence shelter -> general\njob training -> general\n\nAmbiguous single words lean general (restrain promotion):\nsupport -> general\ncontribution -> general\ngrant -> general\nfund -> general\nendowment fund -> general\ncapital -> general\nproject -> general\ninitiative -> general\nprogram -> general\n\nForeign / area studies are hass:\nlatin american studies -> hass\neast asian studies -> hass\nmiddle east studies center -> hass\nafrican studies research -> hass;research\n\nLibraries and collections:\nlibrary acquisitions -> general\nlibrary building fund -> capital\nrare books collection -> hass\ndigital library initiative -> general\n\n## Additional patterns observed in real 990-PF purpose text\n\nEndowment and giving vehicles (generic -> general):\nannual giving -> general\ncapital campaign pledge -> general\npresident\'s fund -> general\ndean\'s discretionary fund -> general\narea of greatest need -> general\nwhere needed most -> general\nsustaining membership -> general\nchallenge grant -> general\nmatching challenge -> general\n\nSpecific endowments carry their domain:\nchemistry department endowment -> stem\nhistory department endowment -> hass\nathletic department endowment -> athletics\nscholarship endowment -> finaid\nlibrary endowment -> general\nlecture series in physics -> stem\nvisiting scholar in economics -> hass;research\n\nHealth professions always stem;professional:\nnursing program -> stem;professional\nphysician assistant program -> stem;professional\nphysical therapy program -> stem;professional\noccupational therapy -> stem;professional\npharmacy scholarship -> stem;finaid;professional\ndental clinic -> stem;professional\n\nBusiness, law, policy:\nentrepreneurship center -> professional\nexecutive education -> professional\nlegal clinic -> professional\nmoot court -> professional\npublic administration program -> professional\nthink tank policy research -> hass;research\n\nStudent support and life:\nfirst generation student program -> studentlife\nemergency student fund -> finaid\nfood pantry for students -> studentlife\nmental health counseling for students -> studentlife\norientation program -> studentlife\ndiversity and inclusion office -> studentlife\n\nResearch phrasing fires research:\nto study the effects of -> research\nto investigate -> research\nto examine -> research\nresearch into -> research\nlaboratory research -> research\nfaculty research fund -> research\n\nCapital phrasing fires capital:\nbuilding renovation -> capital\nnew facility -> capital\nconstruction project -> capital\nhvac replacement -> capital\nroof repair -> capital\ncampus infrastructure -> capital\ntechnology upgrade -> capital\n\nGeneric education delivery is general, not hass and not a specific tag:\neducational programs -> general\neducational support -> general\neducational purposes -> general\ntuition -> finaid\nstudent scholarships -> finaid\nscholarship program -> finaid\n\n## Final reminders and additional residual/generic examples\n\nRemember: output ONLY the semicolon-joined labels, canonical order\nstem;hass;athletics;finaid;research;professional;studentlife;capital, and never\nmix general or other with anything else. When a purpose only restates that the\nrecipient is a charity or that the funds are unrestricted, it is general. When a\npurpose is a bare code, reference, date, or is empty, it is other.\n\nfor the general purposes of the organization -> general\nfor its exempt purpose -> general\nto further the mission -> general\nin support of operations -> general\noperating grant -> general\nsustaining support -> general\ngeneral contribution -> general\ndonor advised distribution -> general\nqualified charitable distribution -> general\npass through grant -> general\nfiscal sponsorship -> general\nreference number 5567 -> other\nstatement 12 -> other\nschedule attached -> other\nform continued -> other\nsee supplemental -> other\ngrant -> general\ngift in kind -> general\nprogram related investment -> general\nendowment support -> general\ncurrent use gift -> general\n\n'

# ====================================================================
# ---- dim_institution matcher + Haiku resolution/tagging ----
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TAG_SYS = _TAG_SYS

INST_SYS = """You identify the institution behind a messy grant-recipient name on an IRS Form 990, from its name and mailing address.

For each item, output: {"id":<int>,"type":"us_university"|"foreign"|"not_applicable","name":"<clean standard institution name, NO 'Foreign' suffix>","country":"<country, ONLY when type=foreign>"}.

- "us_university": a degree-granting U.S. college/university/community college/seminary/professional school -- OR a foundation/fund/alumni/"friends of" tied to ONE such U.S. institution (name the PARENT). Judge by the institution's HOME campus, not this grant's address (a US school abroad is still us_university).
- "foreign": home campus OUTSIDE the U.S. (PR/Guam/USVI are DOMESTIC -> us_university). Use the address to pick which same-named school this is. Oxford and Cambridge are each a federation of individually-named constituent colleges (e.g. "Balliol College", "Trinity College Cambridge", "St Hilda's College") -- for ANY of these, name the PARENT: "University of Oxford" or "University of Cambridge", never the individual college.
- "not_applicable": NOT a degree-granting college (K-12, hospital, church, charity, scholarship-access nonprofit, business, association) -- even if "college"/"university" appears.

NAME = the institution's OFFICIAL, CURRENT name as in the U.S. Dept. of Education IPEDS directory, WHENEVER you are confident which institution it is:
- Correct informal/outdated/abbreviated forms to the official name: "Harvard College" -> "Harvard University"; "UCLA" -> "University of California-Los Angeles"; "UC Berkeley"/"Cal" -> "University of California-Berkeley"; "Va Tech" -> "Virginia Polytechnic Institute and State University"; "Penn" -> "University of Pennsylvania"; "SUNY Buffalo" -> "University at Buffalo"; "Ole Miss" -> "University of Mississippi".
- Use the IPEDS campus form "Institution-Campus" (hyphen) when the specific campus is identifiable from the name or address ("University of Michigan-Ann Arbor").
- For a multi-campus SYSTEM named only generically, with NO campus identifiable from the address, return the FLAGSHIP / main campus in IPEDS form: "University of California" -> "University of California-Berkeley"; "University of Michigan" -> "University of Michigan-Ann Arbor"; "Penn State" -> "Pennsylvania State University-Main Campus"; "University of Wisconsin" -> "University of Wisconsin-Madison"; "University of Missouri" -> "University of Missouri-Columbia".
- If you are NOT confident which institution it is, return the input cleaned of foundation/abbreviation noise but otherwise UNCHANGED. NEVER switch to a DIFFERENT institution.
Return ONLY a JSON array, same order/ids, no prose, no markdown fences."""

# ---------------------------------------------------------------- string helpers
_AMP = re.compile(r"\s*&\s*"); _PUNCT = re.compile(r"[^\w\s]")
_NOISE = re.compile(r"\b("
                    r"FOUNDATION|INC|INCORPORATED|CORP|CORPORATION|LLC|THE|"
                    r"BOARD OF TRUSTEES(?:\s+OF)?|TRUSTEES OF|"
                    r"BOARD OF REGENTS(?:\s+OF)?|REGENTS OF|"
                    r"PRESIDENT AND FELLOWS OF|FRIENDS OF|"
                    r"ASSOCIATION OF FORMER STUDENTS(?:\s+OF)?|"
                    r"ALUMNI ASSOCIATION(?:\s+OF)?|ALUMNI|FBO|C O"
                    r")\b")
_ABBR = [(re.compile(r"\bUNIV\b"), "UNIVERSITY"), (re.compile(r"\bCOMM COLLEGE\b"), "COMMUNITY COLLEGE"),
         (re.compile(r"\bINST\b"), "INSTITUTE"), (re.compile(r"\bST\b"), "SAINT")]

def norm(s):
    s = _AMP.sub(" AND ", str(s or "").upper())
    s = _PUNCT.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()

def strip_wrappers(s):
    n = norm(s)
    for rx, rep in _ABBR:
        n = rx.sub(rep, n)
    n = _NOISE.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()

# ---------------------------------------------------------------- dim_institution matcher
# very common higher-ed tokens -- too generic to BLOCK on (nearly every name has them)
_STOP_TOK = {"UNIVERSITY","COLLEGE","OF","THE","AND","STATE","INSTITUTE","SCHOOL","COMMUNITY",
             "TECHNICAL","CENTER","AT","IN","FOR","A","AN","SEMINARY","ACADEMY","INSTITUTION",
             "EDUCATION","STUDIES","SYSTEM","NORTH","SOUTH","EAST","WEST","NEW","SAINT"}

# Administrative entries, not real campuses -- dim carries these (e.g. "Texas A&M
# University-System Office") alongside the real flagship campus, often in the SAME
# city, which falsely defeats city-based flagship disambiguation in match_dim's pick().
_NON_CAMPUS = re.compile(r"SYSTEM OFFICE|CENTRAL OFFICE|SYSTEM ADMINISTRATION", re.I)

# Human-verified corrections for names the deterministic/LLM matcher could not resolve safely
# (bare/generic system names, or historical bad SG mints that pre-date and block the real IPEDS
# row). Keyed by norm(strip_wrappers(grantee)) only (no state) -- verified to be unambiguous
# across every occurrence seen. Checked FIRST in match_dim, ahead of any fuzzy/national tier,
# since these are 100% confirmed, not guesses. Maps to a canonical_id already in dim_institution.csv,
# or to "NA" for names confirmed not to be a degree-granting institution.
_KNOWN_CORRECTIONS = {
    'CUNY CENTRAL OFFICE OFFICE OF UNIVERSITY CONTROLLER': '190035',
    'GIRARD COLLEGE': 'NA',
    'NORTH PLATTE COMMUNITY COLLEGE': '181312',
    'ORANGE COUNTY COMMUNITY COLLEGE DBA SUNY ORANGE': '194240',
    'STATE UNIVERSITY OF NEW YORK CORTLAND COLLEGE': '196149',
    'STATE UNIVERSITY OF NY AT FREDONIA': '196158',
    'UNIVERSITY OF AKRON': '200800',
    'UNIVERSITY OF CO SCHOOL OF MEDICINE': '126571',
    'UNIVERSITY OF COLORADO': '128300',
    'UNIVERSITY OF COLORADO AT DENVER': '126562',
    'UNIVERSITY OF COLORADO DENVER': '126562',
    'UNIVERSITY OF COLORADO HOSPITAL': '126571',
    'UNIVERSITY OF COLORADO MEDICAL SCHOOL': '126571',
    'UNIVERSITY OF COLORADO SCHOOL OF MEDICINE': '126571',
    'UNIVERSITY OF ILLINOIS AT CHICAGO': '145600',
    'UNIVERSITY OF MONTANA': '180489',
    'UNIVERSITY OF NEBRASKA': '181464',
    'UNIVERSITY OF NEBRASKA COLLEGE OF MEDICINE': '181464',
    'UNIVERSITY OF NEBRASKA MED CTR': '181428',
    'UNIVERSITY OF NORTH CAROLINA': '199175',
    'UNIVERSITY OF NORTH CAROLINA CHAPEL HILL': '199120',
    'UNIVERSITY OF NORTH CAROLINA HEALTH CARE SYSTEM': '199175',
    'UNIVERSITY OF OKLAHOMA': '207500',
    'UNIVERSITY OF PITTSBURGH': '215293',
    'UNIVERSITY OF SOUTH CAROLINA SCHOOL OF MEDICINE GREENVILLE': '218663',
    'UNIVERSITY OF TEXAS': '229090',
    'UNIVERSITY OF TEXAS AUSTIN': '228778',
    'UNIVERSITY OF TEXAS HEALTH SCIENCE': '229300',
    'UNIVERSITY OF TEXAS SAN ANTONIO': '228644',
    'UNIVERSITY OF WISCONSIN': '240444',
    'UNIVERSITY OF WISCONSIN CARBONE CANCER CENTER': '240435',
    'VIRGINIA POLYTECHNIC INSTITUTE AND STATE UNIVERSITY': '233921',
    'TOURO COLLEGE': '196592',
    'MARIST COLLEGE': '192819',
    'BUFFALO STATE COLLEGE': '196130',
    'SUNY ALFRED STATE COLLEGE': '196006',
    'PAN ATLANTIC UNIVERSITY': 'SG01137',
    'SAINT EDMUND S COLLEGE': 'SG01687',
    'OPEN UNIVERSITY': 'SG01114',
    'HEINRICH HEINE UNIVERSITY DUESSELDORF': 'SG00582',
    'ANATOLIA COLLEGE': 'NA',
    'UTICA COLLEGE': '197045',
    'RESEARCH STATE UNIVERSITY OF NEW YORK': '195827',
    'RESEARCH FOR STATE UNIVERSITY OF NEW YO': '196060',
    'AMERICAN SAINT HILDAS COLLEGE': 'SG01879',
    'UNIVERSITY OF QUEENSLAND': 'SG01911',
    'JOAN I AND SANFORD WEILL MEDICAL COLLEGE': '190424',
    'HERBERT H LEHMAN COLLEGE': '190637',
    'UNIVERSITY OF TORONTO': 'SG01969',
    'ITASCA COMMUNITY COLLEGE': '173805',
    'VOORHEES COLLEGE': '218919',
    'GRANITE STATE COLLEGE': '183071',
    'ALAMO COLLEGES': '222497',
    'AMERICAN BAR ILAN UNIVERSITY': 'SG00162',
    'BAPTIST STUDENT AT PURDUE UNIVERSITY': '243780',
    'BAR ILAN UNIVERSITY': 'SG00162',
    'BAR ILAN UNIVERSITY IN ISRAEL': 'SG00162',
    'BAR ILAN UNIVERSITY OF ISRAEL': 'SG00162',
    'BARUCH COLLEGE FUND': '190512',
    'CANISUS COLLEGE': '189705',
    'CANISUS COLLEGE ATHLETICS': '189705',
    'CAPITAL COMMUNITY COLLEGE': '129367',
    'CHAMPLAIN UNIVERSITY': '230852',
    'CUMBERLAND COUNTY COLLEGE': '184205',
    'GLENVILLE STATE COLLEGE': '237385',
    'GRANITE STATE UNIVERSITY': '183071',
    'HEINRICH HEINE UNIVERSITY': 'SG00582',
    'HILLEL AT BARUCH COLLEGE': '190512',
    'JESUS COLLEGE': 'SG01879',
    'KING EDWARD COLLEGE': 'SG00762',
    'LE MOYNE UNIVERSITY': '192323',
    'LIVINGSTONE UNIVERSITY PARTNERS': 'NA',
    'MANHATTANVILLE COLLEGE': '192749',
    'MANHATTANVILLE COLLEGE GLOBAL STUDENT LEADERSHIP': '192749',
    'MANNES COLLEGE OF MUSIC': '193654',
    'NEWYORKINSTITUTE OF TECHNOLOGY': '194091',
    'NYACK COLLEGE': '194161',
    'OF UNIVERSITY OF VALLEY OF GUAT': 'SG02036',
    'OLIVET COLLEGE': '171599',
    'RANDOLPH MACON UNIVERSITY': '233295',
    'SAGE COLLEGES': '195128',
    'SUNY PLATTSBURGH STATE COLLEGE': '196246',
    'UNIVERSITY OF DUBLIN FUND': 'SG01567',
    'UNIVERSITY OF HAWAII AT WCC': '141990',
    'VERMONT STATE COLLEGES SYSTEM VERMONT TECHNICAL COLLEGE': '231156',
    'ALBANY MEDICAL CENTERCOLLEGE': '188580',
    'BLACKHAWK TECH COLLEGE FDN': '238397',
    'BOWLING GREEN TECHNICAL COLLEGE': '201441',
    'CITY COLLEGES OF CHICAGO': '144500',
    'CITY COLLEGES OF CHICAGO FOUNATION': '144500',
    'DREXEL COLLEGE OF MEDICINE': '212054',
    'FOOTHILL DE ANZA COMMUNITY COLLEGES FDDN': '114831',
    'HUMBOLDT STATE UNIVERSITY REAL ETATE HOLDINGS': '115755',
    'INDIANA UNIVERSITY': '151351',
    'INDIANA UNIVERSITY AUDITORIUM': '151351',
    'INDIANA UNIVERSITY CHRISTIAN STUDENT FELLOWSHIP': '151351',
    'INDIANA UNIVERSITY CINEMA': '151351',
    'JOHN HOPKINS UNIVERSITY APPLIED PHYSCIS LABORATOR': '162928',
    'JOHN HOPKINS UNIVERSITY APPLIED PHYSIC LABORATORY': '162928',
    'LOYOLA UNIVERSITY OF CHICAGO': '146719',
    'MAGDALENE COLLEGE': 'SG01687',
    'MARICOPA COMMUNITY COLLEGE KJZZ': '105136',
    'MARICOPA COUNTY COMMUNITY COLLEGE DISTRICT': '105136',
    'MARICOPA COUNTY COMMUNITY COLLEGE FDTN': '105136',
    'RESEARCH OF STATE UNIVERSITY OF NEW': '195827',
    'TEXAS A AND M UNIVERSITY PRESS': '228732',
    'TEXAS A AND M UNIVERSITY SYSTEM HEALTH SCI': '228732',
    'U OF D MERCY DENTAL COLLEGE': '169716',
    'UNIVERSITY OF ALABAMA': '100751',
    'UNIVERSITY OF ALABAMA ADAPTED ATHLETICS': '100751',
    'UNIVERSITY OF ALABAMA HILLEL': '100751',
    'UNIVERSITY OF ALABAMA SAFE CENTER': '100751',
    'UNIVERSITY OF ALASKA': '103529',
    'UNIVERSITY OF CENTRAL FLORIDA STADIUM': '132903',
    'UNIVERSITY OF CINCINNATI ASSOC': '201885',
    'UNIVERSITY OF CINCINNATI EMERGENCY': '201885',
    'UNIVERSITY OF CINCINNATI MEDICAL CENTER': '201885',
    'UNIVERSITY OF CINCINNATI PHYSICIANS COMPANY': '201885',
    'UNIVERSITY OF COLORADO CANCER CENTER': '126562',
    'UNIVERSITY OF COLORADO HOSPITAL AUTHORITY': '126562',
    'UNIVERSITY OF COLORADO MEDICINE': '126562',
    'UNIVERSITY OF COLORADO TRANSPLANT CENTER': '126562',
    'UNIVERSITY OF CONN HEALTH CENTER': '129020',
    'UNIVERSITY OF FLORIDA JACKSONVILLE HEALTHCARE': '134130',
    'UNIVERSITY OF FLORIDA JACKSONVILLE PHYSICIANS': '134130',
    'UNIVERSITY OF ILLINOIS EXTENSION': '149587',
    'UNIVERSITY OF ILLINOIS HEALTH': '149587',
    'UNIVERSITY OF ILLINOIS LIBRARY': '149587',
    'UNIVERSITY OF ILLINOIS MEDICAL CENTER': '149587',
    'UNIVERSITY OF INDIANA': '151351',
    'UNIVERSITY OF MARYLAND CAP REGION HEALTH': '163259',
    'UNIVERSITY OF MARYLAND DERMATOLOGISTS PA': '163259',
    'UNIVERSITY OF MARYLAND EYE ASSOCIATES PA': '163259',
    'UNIVERSITY OF MARYLAND FACULTY PHYSICIANS': '163259',
    'UNIVERSITY OF MARYLAND FAMILY MEDICINE PA': '163259',
    'UNIVERSITY OF MARYLAND MEDICAL CENTER': '163259',
    'UNIVERSITY OF MARYLAND MEDICAL CENTER NA YIN PHD': '163259',
    'UNIVERSITY OF MARYLAND MEDICAL CENTER UMMC': '163259',
    'UNIVERSITY OF MARYLAND MEDICAL CTR UMMC': '163259',
    'UNIVERSITY OF MARYLAND MEDICAL R ADAMS COWLEY SHOCK TRAUMA CENTER': '163259',
    'UNIVERSITY OF MARYLAND MEDICAL SYS UMMS': '163259',
    'UNIVERSITY OF MARYLAND MEDICAL SYSTEMS': '163259',
    'UNIVERSITY OF MARYLAND MEDICAN CENTER UMMC': '163259',
    'UNIVERSITY OF MARYLAND NEUROLOGY ASSOCIATES PA': '163259',
    'UNIVERSITY OF MARYLAND OBGYN ASSOCIATES': '163259',
    'UNIVERSITY OF MARYLAND ONCOLOGY ASSOCIATES': '163259',
    'UNIVERSITY OF MARYLAND ONCOLOGY ASSOCIATES PA': '163259',
    'UNIVERSITY OF MARYLAND ORTHOPAEDIC ASSOC': '163259',
    'UNIVERSITY OF MARYLAND ORTHOPAEDIC ASSOCIATES PA': '163259',
    'UNIVERSITY OF MARYLAND ORTHOPAEDIC ASSOCS': '163259',
    'UNIVERSITY OF MARYLAND PEDIATRIC ASSOCIATES': '163259',
    'UNIVERSITY OF MARYLAND PEDIATRIC ASSOCIATES PA': '163259',
    'UNIVERSITY OF MARYLAND PHYSICIANS PA': '163259',
    'UNIVERSITY OF MARYLAND PHYSICIANS PA UMPPA': '163259',
    'UNIVERSITY OF MARYLAND SHORE HEALTH': '163259',
    'UNIVERSITY OF MARYLAND SURGICAL ASSOCIATES': '163259',
    'UNIVERSITY OF MARYLAND SURGICAL ASSOCIATES PA': '163259',
    'UNIVERSITY OF MARYLAND SURGICAL ASSOCIATES PA UM': '163259',
    'UNIVERSITY OF MARYLAND SURGICAL ASSOCIATES PA UMSA': '163259',
    'UNIVERSITY OF MASSACHUSETTS': '166665',
    'UNIVERSITY OF MASSACHUSETTS MEDICAL CENTER': '166708',
    'UNIVERSITY OF MD EMERGENCY MEDICINE ASSOCIATES PA': '163259',
    'UNIVERSITY OF MD MEMORIAL HOSPITAL': '163259',
    'UNIVERSITY OF MD PATHOLOGY ASSOCIATES PA': '163259',
    'UNIVERSITY OF MD RADIATION ONCOLOGY ASSOCIATE PA': '163259',
    'UNIVERSITY OF MD SHORE MEDICAL CENTER AT EASTON': '163259',
    'UNIVERSITY OF MD SURGICAL ASSOCIATES PA': '163259',
    'UNIVERSITY OF MEXICO KNME': '187985',
    'UNIVERSITY OF MI': '174066',
    'UNIVERSITY OF MICHIGAN': '170976',
    'UNIVERSITY OF MICHIGAN CANCER CENTER': '170976',
    'UNIVERSITY OF MICHIGAN HEALTH SYSTEM': '170976',
    'UNIVERSITY OF MICHIGAN HEALTH WEST': '170976',
    'UNIVERSITY OF MICHIGAN HOSPITALS AND HEALTH CENTERS': '170976',
    'UNIVERSITY OF MICHIGAN HOSPITALS AND HEALTH SYSTEMS': '170976',
    'UNIVERSITY OF MINNESOTA MASONIC CHILDREN S HOSPITAL FAIRVIEW': '174066',
    'UNIVERSITY OF MINNESOTA MEDICAL CEN': '174066',
    'UNIVERSITY OF MINNESOTA MEDICAL CENTER': '174066',
    'UNIVERSITY OF MISS MEDICAL CENTER': '176026',
    'UNIVERSITY OF MN MEDICAL CENTER': '174066',
    'UNIVERSITY OF MS MEDICAL CENTER': '176026',
    'UNIVERSITY OF NCAROLINA CHAPEL HILL': '199120',
    'UNIVERSITY OF NEBRASKA STATE MUSEUM': '181464',
    'UNIVERSITY OF NEW MEXICO CHILDREN S HOSPITAL': '187985',
    'UNIVERSITY OF NORTH CAROLINA AT CHA': '199120',
    'UNIVERSITY OF NORTH TEXAS HEALTH': '228909',
    'UNIVERSITY OF PGH CANCER INSTITUTE CANCER SVCS': '418418',
    'UNIVERSITY OF PGH PHYSICIANS': '418418',
    'UNIVERSITY OF PITTSBURGH CANCER INSTITUTE': '418418',
    'UNIVERSITY OF PITTSBURGH CANCER INSTITUTE CANCER SERVICE': '418418',
    'UNIVERSITY OF PITTSBURGH HEALTH POLICY INSTITUTE': '418418',
    'UNIVERSITY OF PITTSBURGH PHYSICIANS': '418418',
    'UNIVERSITY OF TENNESSEE MEDICAL CENTER': '487010',
    'UNIVERSITY OF TEXAS CHABAD HOUSE': '228778',
    'UNIVERSITY OF TEXAS HEALTH CENTER AT HOUSTON': '229300',
    'UNIVERSITY OF TEXAS HEALTH SCIENCE AT HOUSTON': '229300',
    'UNIVERSITY OF TEXAS HEALTH SCIENCE CENTER AT HOUSTON': '229300',
    'UNIVERSITY OF TEXAS HEALTH SCIENCE CENTER AT HOUSTON UTHEALTH': '229300',
    'UNIVERSITY OF TEXAS HEALTH SCIENCE CENTER AT SAN ANTONIO': '229027',
    'UNIVERSITY OF TEXAS HEALTH SCIENCE CENTER TYLER': '228802',
    'UNIVERSITY OF TEXAS HEALTH SCIENCE CTR HOUSTON': '229300',
    'UNIVERSITY OF TEXAS HEALTH SCIENCES': '229300',
    'UNIVERSITY OF TEXAS HEALTH SYSTEM': '229027',
    'UNIVERSITY OF TEXAS LACROSSE': '228778',
    'UNIVERSITY OF TEXAS M D ANDERSON CANCER': '416801',
    'UNIVERSITY OF TEXAS M D ANDERSON CANCER CENTER': '416801',
    'UNIVERSITY OF TEXAS MD ANDERSON CAN': '416801',
    'UNIVERSITY OF TEXAS MD ANDERSON CANCER': '416801',
    'UNIVERSITY OF TEXAS MED BRANCH': '229300',
    'UNIVERSITY OF TEXAS MEDICAL BRA': '229300',
    'UNIVERSITY OF TEXAS MEDICAL SCHOOL': '229300',
    'UNIVERSITY OF TEXAS PRESS': '228778',
    'UNIVERSITY OF TEXAS PRESS SYSTEM': '228778',
    'UNIVERSITY OF TEXAS SCHOOL OF NURSING': '228778',
    'UNIVERSITY OF TEXAS SOUTHWESTERN': '228787',
    'UNIVERSITY OF TEXAS SOUTHWESTERN MEDICAL CENTER AT DALLAS': '228787',
    'UNIVERSITY OF TEXAS SYSTEM MEDICAL': '229300',
    'UNIVERSITY OF VIRGINIA CANCER CENTER': '234076',
    'UNIVERSITY OF VIRGINIA FILM FESTIVAL': '234076',
    'UNIVERSITY OF VIRGINIA HEALT': '234076',
    'UNIVERSITY OF VIRGINIA HEALTH': '234076',
    'UNIVERSITY OF VIRGINIA HEALTH FDN': '234076',
    'UNIVERSITY OF VIRGINIA HEALTH SYSTEM': '234076',
    'UNIVERSITY OF VIRGINIA HEALTH SYSTEM UVA CHILDREN S HOSPITAL': '234076',
    'UNIVERSITY OF VIRGINIA JEFFERSONIAN GROUNDS INITIATIVE': '234076',
    'UNIVERSITY OF VIRGINIA PHYSICIANS': '234076',
    'UNIVERSITY OF VIRIGINIA ART MUSEUM': '234076',
    'UNIVERSITY OF VT MEDICAL': '231174',
    'UNIVERSITY OF WASHINGTON': '236948',
    'UNIVERSITY OF WASHINGTON IR': '236948',
    'UNIVERSITY OF WASHINGTON LAW SCHOOL': '236948',
    'UNIVERSITY OF WASHINGTON MEDICAL CENTER': '236948',
    'UNIVERSITY OF WASHINGTON PHYSICIANS': '236948',
    'UNIVERSITY OF WASHINGTON PRESS': '236948',
    'UNIVERSITY OF WASHINGTON WOMEN S CENTER': '236948',
    'UNIVERSITY OF WEST AL': '101587',
    'UNIVERSITY OF WI HOSPITAL AND CLINICS AUTH': '240444',
    'UNIVERSITY OF WI HOSPITALS AND CLINICS AUTHORITY': '240444',
    'UNIVERSITY OF WISCONSIN HILLEL': '240444',
    'UNIVERSITY OF WISCONSIN HOSPITAL': '240444',
    'UNIVERSITY OF WISCONSIN HOSPITAL AND CLINIC': '240444',
    'UNIVERSITY OF WISCONSIN HOSPITAL AND CLINICS AUTHORITY': '240444',
    'UNIVERSITY OF WISCONSIN HOSPITALS AND CLINICS': '240444',
    'UNIVERSITY OF WISCONSIN HOSPITALS AND CLINICS AUTH': '240444',
    'UNIVERSITY OF WISCONSIN HOSPITALS AND CLINICS AUTHORITY': '240444',
    'UNIVERSITY OF WISCONSIN HOUSE STAFF': '240444',
    'UNIVERSITY OF WISCONSIN PRESCHOOL LAB': '240444',
    'WASHINGTON UNIVERSITY MEDICAL CENTER': '179867',
    'WASHINGTON UNIVERSITY STUDENT MEDIA': '179867',
    'WEILL MEDICAL COLLEGE OF CORNELL': '190424',
    'WEILL MEDICAL COLLEGE OF RESEARCH': '190424',
    'WILLIAMSBURG TECH COLLEGE': '218955',
    'ARIZONA STATE UNIVERSITY': '104151',
    'TEXAS A AND M UNIVERSITY': '228723',
    'COLUMBIA UNIVERSITY': '190150',
    'GEORGIA REGENTS UNIVERSITY': '482149',
    'KENT STATE UNIVERSITY': '203517',
    'UNIVERSITY OF ILLINOIS': '145637',
    'FLORIDA A AND M UNIVERSITY': '133650',
    'CITY COLLEGE OF NEW YORK': '190567',
    'TEXAS A AND M UNIVERSITY COMMERCE': '224554',
    'IONA COLLEGE': '191931',
    'NEVADA STATE COLLEGE': '441900',
    'AUSTIN COMMUNITY COLLEGE': '222992',
    'INDIANA WESLEYAN UNIVERSITY': '151801',
    'HOUSTON BAPTIST UNIVERSITY': '225399',
    'MANHATTAN COLLEGE': '192703',
    'SCHOOLCRAFT COLLEGE': '172200',
    'TALLAHASSEE COMMUNITY COLLEGE': '137759',
    'PACIFIC NORTHWEST UNIVERSITY': '455406',
    'WEST VALLEY MISSION COMMUNITY COLLEGE': '125222',
    'BAYLOR COLLEGE OF DENTISTRY OF TAMU': '223214',
    'DELAWARE TECHNICAL AND COMMUNITY COLLEGE A DIVISION OF STATE OF DELAWARE': '130882',
    'RES FOUND OF STATE UNIVERSITY OF NY': '195827',
    'INDIANA UNIVERSITY SCHOOL OF MED': '151111',
    'UNIVERSITY OF ALABAMA SCHOOL OF ENGINEERING': '100751',
    'UNIVERSITY OF ALABAMA LAW SCHOOL': '100751',
    'MEDICAL COLLEGE OF GEORGIA': '482149',
    'TEXAS AM UNIVERSITY': '228723',
    'UNIVERSITY OF WASHINGTON STUDENT FISCAL SERVICES': '236948',
    'UNIVERSITY OF TEXAS AT AUSTIN': '228778',
    'SCHOOL OF DENTISTRY OF TEXAS A AND M UNIVERSITY': '223214',
    'IOWA STATE UNIVERSITY OF SCIENCE AND TECHNOLOGY': '153603',
    'LOUISIANA COLLEGE': '159568',
    'CALTECH Y': '110404',
    'PHILADELPHIA UNIVERSITY': '215099',
    'CT STATE COMMUNITY COLLEGE': '129367',
    'UNIVERSITY OF ALABAMA AT TUSCALOOSA': '100751',
    'ARIZONA STATE UNIVERSITY ASU': '104151',
    'COLORADO STATE UNIVERSITY RESEARCH': '126818',
    'RESEARCH FOR STATE UNIVERSITY OF NY': '195827',
    'ROSALIND FRANKLIN UNIVERSITY OF MEDICINE AND SCIENCE': '145558',
    'INDIANA UNIVERSITY SCHOOL OF MEDICINE CO INDIANA UNIVERSITY': '151111',
    'UNIVERSITY OF PITTSBURGH OFFICE OF RESEARCHCOST': '215293',
    'UNIVERSITY OF SOUTH FLORIDA': '137351',
    'UNIVERSITY OF SOUTH FLORIDA COLLEGE OF MEDICINE': '137351',
    'ROSALIND FRANKLIN UNIVERSITY': '145558',
    'ROSALIND FRANKLIN UNIVERSITY OF MEDICINE AND SCIEN': '145558',
    'UNIVERSITY OF MAINE SYSTEM': '161280',
    'PENNSYLVANIA COLLEGE OF OPTOMETRY': '214564',
    'UNIVERSITY OF MASSACHUSETTS WORCESTER': '166708',
    'FAIRLEIGH DICKINSON UNIVERSITY TEANECK': '184603',
    'KUFM TV UNIVERSITY OF MONTANA': '180489',
    'RADCLIFFE COLLEGE': '166027',
    'LOUISIANA STATE UNIVERSITY': '159391',
    'VERMONT COLLEGE CAMPUS': '455992',
    'NORTH CAROLINA AGRICULTURAL AND TECHNICAL STATE UNIVERSITY': '199102',
    'DIXIE STATE COLLEGE': '230171',
    'SOUTHERN ARKANSAS UNIVERSITY': '107983',
    'EMORY AND HENRY COLLEGE': '232025',
    'ADVENTIST UNIVERSITY OF HEALTH SCIENCES': '133872',
    'JOHN TYLER COMMUNITY COLLEGE': '232450',
    'MASS COLLEGE OF PHARMACYHEALTH SCIENCES': '166656',
    'UNIVERSITY OF PITTSBURGH DEPARTMENT OF MEDICINE': '215293',
    'INDIANA UNIVERSITY IU RESEARCH ADMIN DEPT': '151351',
    'UNIVERSITY OF WASHINGTON DEPARTMENT OF LABORATORY MEDICINE': '236948',
    'UH HONORS COLLEGE': '225511',
    'INDIANA UNIVERSITY SCHOOL OF DENTISTRY': '151111',
    'MACAULEY HONORS COLLEGE': 'SG00863',
    'PIKES PEAK COMMUNITY COLLEGE': '127820',
    'ARIZONA STATE UNIVERSITY FOR A NEW AMER': '104151',
    'LOUISIANA STATE UNIVERSITY HEALTH SCIENCES CENTER': '159373',
    'UNIVERSITY OF ALABAMA COLLEGE OF': '100751',
    'UNIVERSITY OF MASSACHUSETTS SCHOOL OF MEDICINE': '166708',
    'UNIVERSITY OF MONTANA MISSOULA': '180489',
    'ESF COLLEGE': '196103',
    'UNIVERSITY OF WA GRANTS': '236948',
    'UNIVERSITY OF WASHINGTON DEPARTMENT OF': '236948',
    'UNIVERSITY OF WASHINGTON SCHOOL OF MEDICINE': '236948',
    'ASSOCIATE ALUMNAE OF DOUGLAS COLLEGE': '186380',
    'ASSOCIATE ALUMNAE OF DOUGLASS COLLEGE': '186380',
    'TEXAS A AND M UNIVERSITY STSTEM HEALTH SCI': '223214',
    'UNIVERSITY OF MASSACHUSETTS DBA UNIVERSITY': '166708',
    'UNIVERSITY OF WASHINGTON AIMS CENTER': '236948',
    'UNIVERSITY OF SOUTH FLORIDA TAMPA': '137351',
    'UNIVERSITY OF SOUTH CAROLINA': '218663',
    'BOARD OF UNIVERSITY OF ALABAMA': '100751',
    'MASSACHUSETTS COLLEGE OF PHARMACY AND HEALTH SCIENCES': '166656',
    'PITTSBURGH UNIVERSITY OF': '215293',
    'CITY COLLEGE OF NY': '190567',
    'FOR CITY COLLEGE': '190567',
    'QUEENS COLLEGE': '190664',
    'STATE UNIVERSITY OF NY MARITIME COLLEGE': '196291',
    'RESEARCH OF STATE UNIVERSITY': '195827',
    'UNIVERSITY OF WA': '236948',
    'JOAN AND SANFORD I WEILL MEDICAL COLLEGE': '190424',
    'INDIANA UNIVERSITY DEPT 78867': '151351',
    'UNIVERSITY OF TEXAS DALLAS': '228787',
    'INDIANA UNIVERSITY RESEARCH': '151111',
    'UNIVERSITY OF ALABMA': '100751',
    'UNIVERSITY OF ALABAMA CRIMSON TIDE': '100751',
    'NORTH CAROLINA A AND T UNIVERSITY': '199102',
    'UNIVERSITY OF ILLONOI': '148654',
    'MASSACHUSETTS COLLEGE OF PHARMACY AND ALLIED HEALTH SCIENCES': '166656',
    'COLORADO STATE UNIVERSITY FUND': '126818',
    'MASSACHUSETTS UNIVERSITY OF': '166708',
    'WICHITA AREA TECHNICAL COLLEGE': '156107',
    'MERCY COLLEGE': '193016',
    'CITY COLLEGE FUND': '190567',
    'COLLEGE OF MOUNT SAINT JOSEPH': '204200',
    'WALLACE COMMUNITY COLLEGE': '101286',
    'TEXAS A AND M UNIVERSITY 12TH MAN': '228723',
    'N CAROLINA A T STATE UNIVERSITY': '199102',
    'CONTRA COSTA COMMUNITY COLLEGE': '112826',
    'LAKE SUMTER COMMUNITY COLLEGE': '135188',
    'UNIVERSITY OF ALABAMA HOSPITAL': '100663',
    'COLORADO STATE UNIVERSITY HISTONE': '126818',
    'LOUISIANA STATE UNIVERSITY HEALTH SCIENCES CE': '159373',
    'COLLEGE OF STATEN ISLAND HILLEL': '190558',
    'TEXAS A AND M UNIVERSITY AT COMMERCE': '224554',
    'UNIVERSITY OF WASHINGTON SCHOOL OF PUBLIC HEALTH': '236948',
    'JOHNSON AND WALES UNIVERSITY': '217235',
    'TEXAS A AND M UNIVERSITYTX AGRILIFE RESEARCH': '228723',
    'BRANDMAN UNIVERSITY': '262086',
    'LOUISIANA STATE UNIVERSITY HEALTH': '159373',
    'UNIVERSITY OF PITTSBURGH OF COMMON': '215293',
    'UNIVERSITY OF TEXAS AT DALLAS': '228787',
    'FIU HONORS COLLEGE': '133951',
    'INDIANA UNIVERSITY PURDUE UNIVERSITY AT INDIANAPOLIS': '151111',
    'INDIANA UNIVERSITY PURDUE UNIVERSITY INDIANAPOLIS': '151111',
    'LOYOLA UNIVERSITY OF CHICAGO': '146719',
    'LOYOLA UNIVERSITY CHICAGO': '146719',
    'LOYOLA UNIVERSITY AT CHICAGO': '146719',
    'LOYOLA UNIVERSITY MAYWOOD': '146719',
    'LOYOLA UNIVERSITY MEDICAL CENTER': '146719',
    'LOYOLA UNIVERSITY HEALTH SYSTEM': '146719',
    'LOYOLA UNIVERSITY OF NEW ORLEANS': '159656',
    'LOYOLA UNIVERSITY MARYLAND': '163046',
    'LOYOLA UNIVERSITY OF MARYLAND MESSINA PROGRAM': '163046',
    'CONCORDIA UNIVERSITY IRVINE': '112075',
    'CONCORDIA UNIVERSITY IRVINE FOUNDATION': '112075',
    'CONCORDIA UNIVERSITY ST PAUL': '173328',
    'CONCORDIA UNIVERSITY SAINT PAUL': '173328',
    'CONCORDIA UNIVERSITY WISCONSIN': '238616',
    'CONCORDIA UNIVERSITY WISCONSIN FOUNDATION': '238616',
    'CONCORDIA UNIVERSITY WISCONSIN SCHOOL OF PHARMACY': '238616',
    'CONCORDIA UNIVERSITY SCHOOL OF PHARMACY': '238616',
    'CONCORDIA UNIVERSITY TEXAS': '224004',
    'CONCORDIA UNIVERSITY AT AUSTIN': '224004',
    'CONCORDIA UNIVERSITY CHICAGO': '144351',
    'CONCORDIA UNIVERSITY NEBRASKA': '180984',
    'CONCORDIA UNIVERSITY PORTLAND': '208488',
    'CITY COLLEGE OF CUNY': '190567',
    'TEXAS A AND M UNIVERSITY BAYLOR COLLEGE OF DENTISTRY': '223214',
    'VERMONT STATE COLLEGES SYSTEM': '231156',
    'RSRCH FNDTN OF STATE UNIVERSITY OF NY': '195827',
    'RIVERSIDE COMMUNITY COLLEGE': '121901',
    'STATE UNIVERSITY COLLEGE AT ONEONTA': '196185',
    'NORTH CENTRAL TECHNICAL COLLEGE': '155593',
    'EMPIRE STATE COLLEGE': '196264',
    'WALLACE STATE COMMUNITY COLLEGE': '101295',
    'RESEARCH OF STATE UNIVERSITY OF NY': '195827',
    'UNIVERSITY OF WASHINGTON ITECH': '236948',
    'POLYTECHNIC UNIVERSITY OF PUERTO RICO': '243577',
    'UNIVERSITY OF CONNECTICUT HARTFORD': '463056',
    'UNIVERSITY OF WASHINGTON SPH S3951': '236948',
    'AZ STATE UNIVERSITY': '104151',
    'INDIANA UNIVERSITY SCHOOL OF MEDICINE': '151111',
    'FREDONIA COLLEGE': '196158',
    'PERALTA COMMUNITY COLLEGE DISTRICT': '121178',
    'UNIVERSITY OF NORTH TEXAS HEALTH SCIENCE CENTER AT FORT WORTH': '228909',
    'MANSFIELD CENTER AT UNIVERSITY OF MONTANA': '180489',
    'CONCORDIA COLLEGE NEW YORK': '190248',
    'CENTRAL METHODIST UNIVERSITY': '176947',
    'GREAT FALLS COLLEGE MSU': '180249',
    'MSU GREAT FALLS COLLEGE': '180249',
    'DAVIDSON COMMUNITY COLLEGE': '198376',
    'MACAULAY HONOR COLLEGE': 'SG00863',
    'FREDONIA COLLEGE OF STATE UNIVERSITY OF': '196158',
    'TALLAHASSEE COMMUNITY COLLEGE FOUNDATIO': '137759',
    'MASSACHUSETTS COLLEGE OF PHARMACY AND HEALTH SCIENCE': '166656',
    'MONTANA STATE UNIVERSITY GREAT FALLS COLLEGE OF TECH': '180249',
    'CITY COLLEGE STUDENT SERVICE': '190567',
    'LOCK HAVEN UNIVERSITY OF PENNSYLVANIA': '213613',
    'CONCORDIA COLLEGE AT BRONXVILLE': '190248',
    'CONCORDIA COLLEGE NY': '190248',
    'UNIVERSITY OF TENNESSEE': '221759',
    'STATE UNIVERSITY COLLEGE AT ONEONTA BIOLOGICAL FIELD STATION': '196185',
    'UNIVERSITY OF WASHINGTON SEATTLE': '236948',
    'SIDNEY KIMMEL MEDICAL COLLEGE': '216366',
    'RESEARCH OF STATE UNIVERSITY OF NY SUNY': '195827',
    'ROSALIND FRANKLIN UNIVERSITY OR MEDICINE AND SCIENCE': '145558',
    'DIXIE STATE UNIVERSITY': '230171',
    'SAN JOSE EVERGREEN COMMUNITY COLLEGE': '122737',
    'MASSACHUSETTSUNIVERSITY OF': '166708',
    'ADIRONDACK COMMUNITY COLLEGE': '188438',
    'RUTGERS UNIVERSITY': '186380',
    'UPMC UNIVERSITY OF PITTSBURGH PHYSICIANS': '215293',
    'COLLEGE OF IDAHO': '142294',
    'DALLAS COUNTY COMMUNITY COLLEGE': '224253',
    'UNIVERSITY OF WI MARINETTE': '240277',
}

# Names that route to a DIFFERENT target depending on the grant's own city (not state --
# both cities below are in TX). Keyed by (norm(strip_wrappers(name)), city.upper()).
_CITY_DISAMBIGUATED = {
    ('BUTLER COUNTY COMMUNITY COLLEGE', 'BUTLER'): '211343',
    ('BUTLER COUNTY COMMUNITY COLLEGE', 'EL DORADO'): '154800',
    ('UNIVERSITY', 'TUSCALOOSA'): '100751',
    ('YORK COLLEGE', 'JAMAICA'): '190691',
    ('DELAWARE TECHNICAL AND COMMUNITY COLLEGE', 'WILMINGTON'): '130916',
    ('UNIVERSITY OF TEXAS HEALTH', 'HOUSTON'): '229300',
    ('UNIVERSITY OF TEXAS HEALTH', 'SAN ANTONIO'): '229027',
    ('UNIVERSITY OF TEXAS HEALTH', 'DALLAS'): '229300',
    ('UNIVERSITY OF TEXAS HEALTH SCI', 'HOUSTON'): '229300',
    ('UNIVERSITY OF TEXAS HEALTH SCI', 'SAN ANTONIO'): '229027',
    ('CURATORS OF UNIVERSITY OF MISSOURI', 'ROLLA'): '178411',
    ('CURATORS OF UNIVERSITY OF MISSOURI', 'COLUMBIA'): '178396',
    ('CURATORS OF UNIVERSITY OF MISSOURI', 'ST LOUIS'): '178420',
    ('CURATORS OF UNIVERSITY OF MISSOURI', 'SAINT LOUIS'): '178420',
    ('CURATORS OF UNIVERSITY OF MISSOURI', 'KANSAS CITY'): '178402',
    ('LOUISIANA STATE UNIVERSITY', 'BATON ROUGE'): '159391',
    ('LOUISIANA STATE UNIVERSITY', 'NEW ORLEANS'): '159373',
    ('LOUISIANA STATE UNIVERSITY', 'SHREVEPORT'): '159416',
    ('LOUISIANA STATE UNIVERSITY', 'ALEXANDRIA'): '159382',
    ('UNIVERSITY OF ILLINOIS COLLEGE OF MEDICINE', 'ROCKFORD'): 'SG01774',
    ('UNIVERSITY OF ILLINOIS COLLEGE OF MEDICINE', 'PEORIA'): '145600',
    ('UNIVERSITY OF ILLINOIS COLLEGE OF MEDICINE', 'URBANA'): '145600',
    ('UNIVERSITY OF IL COLLEGE OF MEDICINE', 'ROCKFORD'): 'SG01774',
    ('UNIVERSITY OF IL COLLEGE OF MEDICINE', 'PEORIA'): '145600',
    ('UNIVERSITY OF IL COLLEGE OF MEDICINE', 'URBANA'): '145600',
}

# Broad substring rules for institutions whose grantee text carries dozens of
# department/school/institute suffixes (e.g. "Columbia University Mailman School of
# Public Health") that would otherwise need one _KNOWN_CORRECTIONS entry each. Each
# rule is (must_contain_substring, exclude_substrings, canonical_id) -- checked only
# when nothing more specific matched. Only add a rule once you've confirmed no OTHER
# real institution's name also contains must_contain (verified against dim first).
_CONTAINS_RULES = [
    # "COLUMBIA UNIVERSITY" as an adjacent phrase is unique to Columbia NYC in dim,
    # EXCEPT "Teachers College at Columbia University" -- a separate real institution
    # with its own IPEDS id -- which must not be swept in.
    ("COLUMBIA UNIVERSITY", ["TEACHERS COLLEGE"], "190150"),
    # "STATE UNIVERSITY OF NEW YORK" as a full phrase excludes both "Colorado State
    # University..." and "...of CUNY..." (different system) by construction.
    ("STATE UNIVERSITY OF NEW YORK", [], "195827"),
    # "University of Washington" + any department/office/foundation suffix -> Seattle,
    # EXCEPT the two real separate branch campuses.
    ("UNIVERSITY OF WASHINGTON", ["BOTHELL", "TACOMA"], "236948"),
    # "University of Alabama" + any department/society/foundation suffix -> Tuscaloosa,
    # EXCEPT Birmingham (UAB) and Huntsville (UAH) -- legally separate universities.
    ("UNIVERSITY OF ALABAMA", ["BIRMINGHAM", "HUNTSVILLE"], "100751"),
    # "Texas A&M University" + any department/office/foundation suffix -> College Station,
    # EXCEPT the real separate branch campuses.
    ("TEXAS A AND M UNIVERSITY", ["CORPUS CHRISTI", "GALVESTON", "KINGSVILLE", "SAN ANTONIO",
                                  "TEXARKANA", "CENTRAL TEXAS", "COMMERCE", "SYSTEM OFFICE"], "228723"),
]

def _contains_rule_match(gq):
    for must, excl, cid in _CONTAINS_RULES:
        if must in gq and not any(x in gq for x in excl):
            return cid
    return None

_OXFORD_ID = 'SG01879'    # University of Oxford (Foreign)
_CAMBRIDGE_ID = 'SG01687' # University of Cambridge (Foreign)

# Oxford/Cambridge are federations of individually-named constituent colleges; per policy,
# every constituent college rolls up to its parent university's existing entry rather than
# minting its own. Names UNIQUE to one university map bare; names that collide -- with the
# other university, or with real US/other institutions (Trinity, King's, St John's all have
# unrelated real namesakes already in dim) -- require an explicit "OXFORD"/"CAMBRIDGE"
# qualifier token to match. (Emmanuel College's Boston/Georgia collision is handled via
# _STATE_DISAMBIGUATED below, not here -- it isn't an Oxbridge case.)
# Keys are the EXACT norm(strip_wrappers(...)) output -- note "St" -> "SAINT" (existing
# abbreviation expansion) and apostrophes -> a literal space, not deletion (e.g.
# "St Anne's College" -> "SAINT ANNE S COLLEGE").
_OXFORD_COLLEGES_UNIQUE = [
    "ALL SOULS COLLEGE", "BALLIOL COLLEGE", "BLACKFRIARS", "BRASENOSE COLLEGE",
    "CHRIST CHURCH", "GREEN TEMPLETON COLLEGE", "HARRIS MANCHESTER COLLEGE",
    "HERTFORD COLLEGE", "KEBLE COLLEGE", "KELLOGG COLLEGE", "LADY MARGARET HALL",
    "LINACRE COLLEGE", "MANSFIELD COLLEGE", "MERTON COLLEGE",
    "NUFFIELD COLLEGE", "ORIEL COLLEGE", "REGENT S PARK COLLEGE", "REUBEN COLLEGE",
    "SOMERVILLE COLLEGE", "SAINT ANNE S COLLEGE", "SAINT ANTONY S COLLEGE",
    "SAINT CATHERINE S COLLEGE", "SAINT CROSS COLLEGE", "SAINT EDMUND HALL",
    "SAINT HILDA S COLLEGE", "SAINT HUGH S COLLEGE", "SAINT PETER S COLLEGE",
    "WADHAM COLLEGE", "WORCESTER COLLEGE", "WYCLIFFE HALL",
]
# "Magdalen College" ALONE is ambiguous with a real, unrelated "Magdalen College" in Warner,
# NH (canonical_id 182917) -- requires an explicit "Oxford" qualifier, unlike the rest above.
_OXBRIDGE_QUALIFIER_ONLY = {"MAGDALEN COLLEGE OXFORD": _OXFORD_ID}
_CAMBRIDGE_COLLEGES_UNIQUE = [
    "CHRIST S COLLEGE", "CHURCHILL COLLEGE", "CLARE COLLEGE", "CLARE HALL",
    "DARWIN COLLEGE", "DOWNING COLLEGE", "FITZWILLIAM COLLEGE", "GIRTON COLLEGE",
    "GONVILLE AND CAIUS COLLEGE", "HOMERTON COLLEGE", "HUGHES HALL",
    "LUCY CAVENDISH COLLEGE", "MAGDALENE COLLEGE", "MURRAY EDWARDS COLLEGE",
    "NEWNHAM COLLEGE", "PETERHOUSE", "ROBINSON COLLEGE", "SELWYN COLLEGE",
    "SIDNEY SUSSEX COLLEGE", "SAINT CATHARINE S COLLEGE", "SAINT EDMUND S COLLEGE",
    "TRINITY HALL",
]
# ambiguous with the OTHER university, or with unrelated real institutions of the same name
# -- only match when the query text ALSO names the university explicitly.
_AMBIGUOUS_COLLEGES = ["CORPUS CHRISTI COLLEGE", "EXETER COLLEGE", "JESUS COLLEGE",
    "KING S COLLEGE", "PEMBROKE COLLEGE", "QUEEN S COLLEGE", "QUEENS COLLEGE",
    "SAINT JOHN S COLLEGE", "TRINITY COLLEGE", "UNIVERSITY COLLEGE", "WOLFSON COLLEGE"]

_OXBRIDGE_COLLEGES = {c: _OXFORD_ID for c in _OXFORD_COLLEGES_UNIQUE}
_OXBRIDGE_COLLEGES.update({c: _CAMBRIDGE_ID for c in _CAMBRIDGE_COLLEGES_UNIQUE})
_OXBRIDGE_COLLEGES.update(_OXBRIDGE_QUALIFIER_ONLY)
for c in _AMBIGUOUS_COLLEGES:
    _OXBRIDGE_COLLEGES[f"{c} OXFORD"] = _OXFORD_ID
    _OXBRIDGE_COLLEGES[f"{c} CAMBRIDGE"] = _CAMBRIDGE_ID

# Same bare name, two unrelated real institutions -- dim_institution.csv is one-row-per-id,
# so this cannot be encoded as two dim rows sharing an id; disambiguate by the grant's own
# state instead. Keyed by (norm(strip_wrappers(name)), state).
_STATE_DISAMBIGUATED = {
    ("EMMANUEL COLLEGE", "MA"): "165671",   # Emmanuel College, Boston MA
    ("EMMANUEL COLLEGE", "GA"): "139630",   # Emmanuel University, Franklin Springs GA (former name)
    ("TEACHERS COLLEGE", "NY"): "196468",    # Teachers College at Columbia University
    ("LOYOLA UNIVERSITY", "IL"): "146719",   # Loyola University Chicago
    ("LOYOLA UNIVERSITY", "MD"): "163046",   # Loyola University Maryland
    ("LOYOLA UNIVERSITY", "LA"): "159656",   # Loyola University New Orleans
    ("CONCORDIA UNIVERSITY", "NE"): "180984",  # Concordia University-Nebraska (Seward)
    ("CONCORDIA UNIVERSITY", "IL"): "144351",  # Concordia University-Chicago (River Forest)
    ("CONCORDIA UNIVERSITY", "WI"): "238616",  # Concordia University-Wisconsin (Mequon)
    ("CONCORDIA UNIVERSITY", "CA"): "112075",  # Concordia University-Irvine
    ("CONCORDIA UNIVERSITY", "MN"): "173328",  # Concordia University-Saint Paul
    ("CONCORDIA UNIVERSITY", "OR"): "208488",  # Concordia University-Portland (closed 2020)
    ("CONCORDIA UNIVERSITY", "TX"): "224004",  # Concordia University Texas (Austin)
    ("CONCORDIA UNIVERSITY", "MI"): "169363",  # Concordia University Ann Arbor (closed 2013)
}

def build_dim(dim_path):
    """Load dim_institution.csv -> the ONLY reference the matcher uses."""
    # keep_default_na=False: canonical_id legitimately holds the literal strings
    # "NA"/"NCI" -- pandas' default missing-value sniffing would silently blank them.
    d = pd.read_csv(dim_path, dtype=str, keep_default_na=False)
    has_city = "city" in d.columns
    by_ns = {}; by_n = {}; by_state = {}; prefix_index = {}; info = {}; name_canon = {}; max_sg = 0
    us_cid = []; us_name = []                                 # parallel arrays for NATIONAL match
    for _, r in d.iterrows():
        cid = r["canonical_id"]; nm = r["grantee_normalized"]; st = r["state"].upper()
        cls = r["entity_class"]; cty = r["city"].upper() if has_city else ""
        # setdefault: if this canonical_id already has info (its TRUE canonical row,
        # which precedes any ALIAS rows appended later for the same id), never let an
        # alias row's own name/city overwrite the real display info.
        # ipeds_unitid is no longer a column in dim_institution.csv -- canonical_id
        # itself IS the IPEDS unitid for every real (non-"SG#####", non-sentinel) row.
        ipeds = cid if not cid.startswith("SG") and cid not in ("NA", "NCI") else ""
        info.setdefault(cid, {"name": nm, "ipeds": ipeds, "state": st,
                              "country": r["country"], "class": cls, "city": cty})
        n = norm(nm)
        if n:
            by_ns.setdefault((n, st), set()).add(cid)
            by_n.setdefault(n, set()).add(cid)
            by_state.setdefault(st, []).append((cid, n))
            name_canon.setdefault(n, cid)
            if cls in ("IPEDS-US", "non-IPEDS-US"):          # US institutions, for NATIONAL name match
                us_cid.append(cid); us_name.append(n)
        if "-" in nm:                                        # IPEDS "System-Campus" -> index the base name
            base = norm(nm.split("-", 1)[0])
            if base and base != n:
                prefix_index.setdefault(base, set()).add(cid)
        m = re.match(r"^SG(\d+)$", cid)
        if m:
            max_sg = max(max_sg, int(m.group(1)))
    tok_index = {}                                            # distinctive token -> [us-array indices]
    for idx, n in enumerate(us_name):
        for t in set(n.split()) - _STOP_TOK:
            tok_index.setdefault(t, []).append(idx)
    return {"by_ns": by_ns, "by_n": by_n, "by_state": by_state, "prefix_index": prefix_index,
            "info": info, "name_canon": name_canon, "next_sg": max_sg + 1,
            "us_cid": us_cid, "us_name": us_name, "tok_index": tok_index}

def _national_name_match(q, dim, floor, margin):
    """Best US dim canonical_id whose name token-set-matches `q` across ALL states,
    but only if that winner is CONFIDENT (>= floor) and UNAMBIGUOUS (beats the best
    OTHER institution by >= margin). Returns (cid, top_score) or (None, top_score).
    Uses token_set so a clean 'University of Michigan' matches the IPEDS long-form
    'University of Michigan-Ann Arbor' -- but bare names shared by multiple campuses
    tie and are rejected (-> review, not a wrong match). BLOCKED on distinctive tokens
    so each query scores only against the handful of names sharing a rare token."""
    us_name = dim.get("us_name") or []
    if len(q) < 10 or " " not in q or not us_name:
        return None, 0
    qtoks = set(q.split()) - _STOP_TOK
    if not qtoks:
        return None, 0
    tok_index = dim["tok_index"]; cand = set()
    for t in qtoks:
        cand.update(tok_index.get(t, ()))
    if not cand:
        return None, 0
    cand = list(cand); choices = [us_name[i] for i in cand]
    got = process.extract(q, choices, scorer=fuzz.token_set_ratio, score_cutoff=floor, limit=20)
    if not got:
        return None, 0
    best = {}
    for _, sc, j in got:
        c = dim["us_cid"][cand[j]]
        if sc > best.get(c, 0): best[c] = sc
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    s1 = ranked[0][1]; s2 = ranked[1][1] if len(ranked) > 1 else 0
    if s1 >= floor and s1 - s2 >= margin:
        return ranked[0][0], s1
    return None, s1

def match_dim(grantee, city, state, dim):
    """Deterministic canonical_id from dim via a name -> name+city+state -> name+state
    -> flagship(HQ) cascade, then a national name match and a state-scoped geo-fuzzy
    for typos. Returns canonical_id or None (-> Haiku, which supplies the flagship for
    bare systems it can't pin)."""
    st = (state or "").upper(); cty = norm(city or ""); info = dim["info"]
    gq = norm(strip_wrappers(grantee))
    kc = (_STATE_DISAMBIGUATED.get((gq, st)) or _CITY_DISAMBIGUATED.get((gq, cty))
          or _KNOWN_CORRECTIONS.get(gq) or _OXBRIDGE_COLLEGES.get(gq) or _contains_rule_match(gq))
    if kc:
        return kc
    def pick(cids):
        """Choose ONE institution from a set sharing a base name: unique -> it;
        else name+city+state; else name+state; else the flagship '-Main Campus'
        (system HQ); else None (ambiguous -> Haiku). Administrative "System
        Office"/"Central Office"/"System Administration" entries are dropped
        first when there's a genuine campus alternative -- they aren't real
        campuses and otherwise falsely tie with the flagship on city (e.g.
        Texas A&M University-System Office sits in the same city, College
        Station, as the actual flagship campus)."""
        cids = list(cids)
        real = [c for c in cids if not _NON_CAMPUS.search(info[c]["name"])]
        if real:
            cids = real
        if len(cids) == 1:
            return cids[0]
        st_c = [c for c in cids if info[c]["state"] == st] if st else []
        if cty and st_c:                                          # name + city + state
            cc = [c for c in st_c if norm(info[c].get("city", "")) == cty]
            if len(cc) == 1:
                return cc[0]
        if len(st_c) == 1:                                        # name + state
            return st_c[0]
        pool = st_c or cids                                       # flagship / system HQ
        main = [c for c in pool if "MAIN CAMPUS" in norm(info[c]["name"])]
        if len(main) == 1:
            return main[0]
        return None
    for q in (norm(grantee), strip_wrappers(grantee)):
        if not q:
            continue
        hit = dim["by_ns"].get((q, st))                           # exact name+state (may be >1: aliases)
        if hit:
            if len(hit) == 1:
                return next(iter(hit))
            p = pick(hit)
            if p:
                return p
        cs = dim["by_n"].get(q)
        if cs:
            p = pick(cs)
            if p:
                return p
        pre = dim["prefix_index"].get(q)                          # bare system name -> its campuses
        if pre:
            p = pick(pre)
            if p:
                return p
    # NATIONAL name match (state-independent): dim stores IPEDS long-form names and the
    # grant's state may be a fundraising-arm mailing address in the WRONG state, so match
    # on NAME across all states -- accepting only a confident, unambiguous winner.
    cid, _ = _national_name_match(strip_wrappers(grantee), dim, floor=93, margin=10)
    if cid:
        return cid
    if st and st in dim["by_state"]:
        q = strip_wrappers(grantee)
        if len(q) >= 6:
            pool = dim["by_state"][st]; names = [n for _, n in pool]
            for scorer, floor, margin in ((fuzz.token_sort_ratio, 92, 6),
                                          (fuzz.token_set_ratio, 95, 8)):
                got = process.extract(q, names, scorer=scorer, score_cutoff=floor, limit=8)
                if got:
                    ranked = sorted(((sc, pool[i][0]) for _, sc, i in got), reverse=True)
                    ts, tc = ranked[0]; sec = next((s for s, c in ranked if c != tc), 0)
                    if ts >= floor and ts - sec >= margin:
                        return tc
    return None

def dim_has_similar(nm, state, dim, floor=85):
    """True if a dim entry in this state loosely resembles `nm` -- i.e. the school
    is probably already in dim under a name variant, so we must NOT mint a
    duplicate SG for it (send it to review instead)."""
    st = (state or "").upper(); q = norm(nm)
    for _, n in dim["by_state"].get(st, []):
        if fuzz.token_set_ratio(q, n) >= floor:
            return True
    return False

# ---------------------------------------------------------------- Haiku helpers
LABELS = {"stem","hass","athletics","finaid","research","professional","studentlife","capital","general","other"}
_SPEC = ["stem","hass","athletics","finaid","research","professional","studentlife","capital"]; _ORD = {l:i for i,l in enumerate(_SPEC)}
FIX = {"science":"stem","medicine":"stem","medical":"stem","health":"stem","environment":"stem","arts":"hass","humanities":"hass","music":"hass","socialscience":"hass","social":"hass","scholarship":"finaid","scholarships":"finaid","financialaid":"finaid","aid":"finaid","sports":"athletics","athletic":"athletics","professionalschool":"professional","law":"professional","business":"professional","facilities":"capital","building":"capital","construction":"capital","operating":"general","generalsupport":"general","unrestricted":"general","none":"other","":"other"}
_US_FLAGSHIP = re.compile(r"\bHARVARD\b|\bYALE\b|\bPRINCETON\b|\bSTANFORD\b|\bMIT\b|MASSACHUSETTS INSTITUTE|\bCOLUMBIA UNIVERSITY\b|\bGEORGETOWN\b|\bVANDERBILT\b|\bCORNELL\b|\bDARTMOUTH\b|\bJOHNS HOPKINS\b|\bSPELMAN\b|\bWAYNE STATE\b|UNIVERSITY OF MICHIGAN|UNIVERSITY OF CHICAGO|UNIVERSITY OF PENNSYLVANIA")
_SUBUNIT = re.compile(r"\bCOLLEGE OF\b|\bSCHOOL OF\b|\bINSTITUTE OF\b|\bDEPARTMENT\b|\bFOUNDATION\b|\bALUMNI\b|\bFRIENDS OF\b|\bSCHOLARSHIP\b|\bATHLETIC\b|\bDEVELOPMENT\b|\bENDOWMENT\b|\bFUND\b|\bCENTER\b|\bPROGRAM\b|\bASSOCIATION\b")
_US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR VI GU AS MP".split())
# US territories (not DC -- that's a federal district, not a territory) get their
# full formal name in dim_institution's state column, never the postal abbreviation.
_TERRITORY_NAMES = {
    "PR": "Puerto Rico",
    "VI": "U.S. Virgin Islands",
    "GU": "Guam",
    "AS": "American Samoa",
    "MP": "Northern Mariana Islands",
}

def qnorm(s): return re.sub(r'\s+', ' ', str(s or '').strip()).lower()

def canonize(raw):
    if raw is None: return "other"
    t = raw.strip().lower()
    if t.startswith("__err__"): return "other"
    t = re.sub(r'^(tag|tags|label|labels)\s*[:=]\s*', '', t).strip().strip('"').strip("'").strip('.')
    out = []
    for p in re.split(r'[;,/]| and ', t):
        p = re.sub(r'[^a-z]', '', p)
        if not p: continue
        if p not in LABELS:
            p = FIX.get(p)
            if p is None: continue
        if p not in out: out.append(p)
    if not out: return "other"
    sp = [l for l in out if l in _ORD]
    return ";".join(sorted(set(sp), key=lambda l: _ORD[l])) if sp else ("general" if "general" in out else "other")

def parse_json(txt):
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.split("```", 2)[1]
        if txt.startswith("json"): txt = txt[4:]
    a = txt.find("["); b = txt.rfind("]")
    return json.loads(txt[a:b + 1])

MODEL = "claude-haiku-4-5"

def haiku_batch(cl, sysblock, payload, attempt=0):
    try:
        m = cl.messages.create(model=MODEL, max_tokens=4096, temperature=0,
                               system=sysblock, messages=[{"role":"user","content":json.dumps(payload)}])
        return {int(o["id"]): o for o in parse_json(m.content[0].text)}
    except Exception:
        if attempt < 2:
            time.sleep(1.5); return haiku_batch(cl, sysblock, payload, attempt + 1)
        return {}

def _req(cid, sysblock, user, max_tokens):
    """One Message-Batches request. cache_control on the system block -> the
    (long) prompt is cached across all requests in the batch."""
    return {"custom_id": cid, "params": {"model": MODEL, "max_tokens": max_tokens,
            "temperature": 0, "system": sysblock,
            "messages": [{"role": "user", "content": user}]}}

def _retry(fn, tries=6, base=3, label=""):
    """Call fn(); on ANY exception retry with exponential backoff (capped 60s),
    then re-raise on the final attempt. Survives transient network drops
    (WinError 10053, read timeouts, 5xx) instead of crashing the whole run."""
    for t in range(tries):
        try:
            return fn()
        except Exception as e:
            if t == tries - 1:
                raise
            wait = min(base * (2 ** t), 60)
            print(f"  [batch {label}] network error ({type(e).__name__}); "
                  f"retry {t + 1}/{tries - 1} in {wait}s...", flush=True)
            time.sleep(wait)

def _size_chunks(reqs, max_bytes=96_000_000, max_count=90000):
    """Split requests so each Batches.create POST stays well under the API's
    256 MB / 100k-request caps. Chunk by BYTES, not count: every request repeats
    the (multi-KB) system prompt, so tens of thousands of tag requests balloon to
    hundreds of MB and the oversized upload is rejected (-> APIConnectionError)."""
    groups, cur, cur_bytes = [], [], 0
    for r in reqs:
        rb = len(json.dumps(r)) + 2
        if cur and (cur_bytes + rb > max_bytes or len(cur) >= max_count):
            groups.append(cur); cur, cur_bytes = [], 0
        cur.append(r); cur_bytes += rb
    if cur:
        groups.append(cur)
    return groups

def batch_run(cl, reqs, poll=20, chunk=90000, label=""):
    """Submit requests via the async Message Batches API (50% cheaper than
    synchronous) with prompt caching, poll until ended, return {custom_id: text}.
    reqs: list from _req(). Falls back to synchronous on 1 request. Every network
    call is retried (see _retry), so a transient drop never kills the run."""
    if len(reqs) == 1:
        p = reqs[0]["params"]
        try:
            m = _retry(lambda: cl.messages.create(**p), label=label)
            return {reqs[0]["custom_id"]: m.content[0].text}
        except Exception:
            return {reqs[0]["custom_id"]: None}
    ids = []
    for grp in _size_chunks(reqs, max_count=chunk):
        b = _retry(lambda grp=grp: cl.messages.batches.create(requests=grp), label=label)
        ids.append(b.id)
    print(f"  [batch {label}] {len(reqs)} requests in {len(ids)} batch(es); polling every {poll}s "
          f"(50% cheaper, async)...", flush=True)
    res = {}; pending = set(ids)
    while pending:
        time.sleep(poll)
        for bid in list(pending):
            try:
                info = _retry(lambda bid=bid: cl.messages.batches.retrieve(bid), tries=4, label=label)
            except Exception:
                continue                      # transient; re-check on the next poll cycle
            if info.processing_status == "ended":
                try:
                    entries = _retry(lambda bid=bid: list(cl.messages.batches.results(bid)), tries=4, label=label)
                except Exception:
                    continue                  # results fetch failed; retry next cycle
                for e in entries:
                    res[e.custom_id] = e.result.message.content[0].text if e.result.type == "succeeded" else None
                pending.discard(bid)
        print(f"  [batch {label}] {len(ids)-len(pending)}/{len(ids)} ended | {len(res):,} results", flush=True)
    return res

# ---------------------------------------------------------------- resolution (dim + Haiku)
def resolve_institutions(grantees, dim, cl, use_batch=True):
    """Return {key -> canonical_id} and a list of NEW dim rows. Matcher (dim) first;
    Haiku for the residual, mapping to an existing dim name or minting a new SG#####.
    canonical_id values: a real cid | 'NA' (not a college) | '' (uncertain -> review)."""
    res = {}; need = []
    for g in grantees:
        cid = match_dim(g["grantee"], g["city"], g["state"], dim)
        if cid: res[g["key"]] = cid
        else: need.append(g)
    verdict = {}
    sb = [{"type":"text","text":INST_SYS,"cache_control":{"type":"ephemeral"}}]
    groups = [need[i:i+25] for i in range(0, len(need), 25)]
    if use_batch and len(groups) > 1:
        reqs = []
        for gi, grp in enumerate(groups):
            payload = [{"id":j,"name":it["grantee"],"city":it["city"],"state":it["state"],
                        "zip":it["zip"],"country":it["country"]} for j,it in enumerate(grp)]
            reqs.append(_req(f"g{gi}", sb, json.dumps(payload), 4096))
        out = batch_run(cl, reqs, label="match")
        for gi, grp in enumerate(groups):
            txt = out.get(f"g{gi}"); by_id = {}
            if txt:
                try: by_id = {int(o["id"]): o for o in parse_json(txt)}
                except Exception: by_id = {}
            for j, it in enumerate(grp): verdict[it["key"]] = by_id.get(j)
    else:
        for grp in groups:
            payload = [{"id":j,"name":it["grantee"],"city":it["city"],"state":it["state"],
                        "zip":it["zip"],"country":it["country"]} for j,it in enumerate(grp)]
            out = haiku_batch(cl, sb, payload)
            for j, it in enumerate(grp): verdict[it["key"]] = out.get(j)
    new_dim = {}
    def new_sg():
        c = f"SG{dim['next_sg']:05d}"; dim["next_sg"] += 1; return c
    for g in need:
        o = verdict.get(g["key"]); orig = norm(g["grantee"])
        typ = str((o or {}).get("type","")).strip(); nm = str((o or {}).get("name","")).strip()
        ctry = str((o or {}).get("country","")).strip()
        if not o or typ == "not_applicable" or not nm:
            res[g["key"]] = "NA"; continue
        if typ == "foreign":
            st = str(g["state"]).strip().upper(); zp = str(g["zip"]).strip()
            sig = (g["country"] and g["country"] not in ("United States","Puerto Rico","Guam","")) \
                  or (st and st not in _US_STATES and len(st) >= 2) \
                  or (zp and not re.match(r"^\d{5}(-\d{4})?$", zp))
            if not sig or _US_FLAGSHIP.search(orig):
                res[g["key"]] = ""; continue
            fn = f"{nm} (Foreign)"; nk = norm(fn)
            nk2 = norm(f"{strip_wrappers(nm)} (Foreign)")   # drops a leading "The"/noise the LLM may have kept
            ex = dim["name_canon"].get(nk) or dim["name_canon"].get(nk2)
            if ex: res[g["key"]] = ex
            else:
                c = new_sg(); dim["name_canon"][nk] = c
                dim["info"][c] = {"name":fn,"ipeds":"","state":"","country":ctry,"class":"foreign","city":g["city"]}
                new_dim.setdefault(c, [c, fn, "", "", ctry, "foreign", g["city"]]); res[g["key"]] = c
        else:  # us_university
            cid = match_dim(nm, g["city"], g["state"], dim)   # cascade: name -> city+state -> state -> flagship
            raw = strip_wrappers(g["grantee"])
            heavy_rename = fuzz.ratio(norm(raw), norm(nm)) < 55 and not _SUBUNIT.search(orig)
            if cid:
                itoks = set(raw.split())
                mtoks = set(norm(dim["info"].get(cid, {}).get("name", "")).split())
                # Trust the matcher (the LLM named a real institution dim confirms) UNLESS a
                # MULTI-word input was heavily renamed to an institution sharing NO token with
                # it -> likely a school-swap, not an official-name expansion -> review.
                if heavy_rename and len(itoks) >= 2 and not (itoks & mtoks):
                    res[g["key"]] = ""
                else:
                    res[g["key"]] = cid
            # resembles a dim entry nationally (or in-state) but not a confident UNIQUE
            # match -> campus-ambiguous / name variant: REVIEW, never mint a duplicate.
            elif _national_name_match(strip_wrappers(nm), dim, floor=88, margin=0)[1] >= 88 \
                 or dim_has_similar(nm, g["state"], dim) or heavy_rename:
                res[g["key"]] = ""
            else:                                             # genuinely new -> mint + register
                c = new_sg(); nk = norm(nm)
                st_upper = str(g["state"]).upper()
                st = _TERRITORY_NAMES.get(st_upper, g["state"]) if st_upper in _US_STATES else ""
                dim["name_canon"][nk] = c
                dim["by_n"].setdefault(nk, set()).add(c)      # register mint so identical/similar
                nidx = len(dim["us_name"]); dim["us_cid"].append(c); dim["us_name"].append(nk)
                for t in set(nk.split()) - _STOP_TOK:         # index it so later grantees dedup to it
                    dim["tok_index"].setdefault(t, []).append(nidx)
                dim["info"][c] = {"name":nm,"ipeds":"","state":st,"country":"United States","class":"non-IPEDS-US","city":g["city"]}
                new_dim.setdefault(c, [c, nm, "", st, "United States", "non-IPEDS-US", g["city"]]); res[g["key"]] = c
    return res, list(new_dim.values())

def tag_purposes(purposes, cl, use_batch=True):
    """Classify each UNIQUE purpose string. TAG_SYS is long, so cache_control caches
    it across every request; the Batch API halves the price again."""
    uniq = sorted(set(qnorm(p) for p in purposes)); reps = {}
    for p in purposes:
        k = qnorm(p)
        if k and k not in reps: reps[k] = str(p).strip()
    sb = [{"type":"text","text":TAG_SYS,"cache_control":{"type":"ephemeral"}}]
    todo = [k for k in uniq if k]
    tag = {"": "general"}                         # blank/no stated purpose -> general (operating) support
    if use_batch and len(todo) > 1:
        reqs = [_req(f"t{i}", sb, reps[k].upper(), 24) for i, k in enumerate(todo)]
        out = batch_run(cl, reqs, label="tag")
        for i, k in enumerate(todo):
            tag[k] = canonize(out.get(f"t{i}"))
    else:
        for k in todo:
            m = cl.messages.create(model=MODEL, max_tokens=24, temperature=0,
                                   system=sb, messages=[{"role":"user","content":reps[k].upper()}])
            tag[k] = canonize(m.content[0].text)
    return tag

# ---------------------------------------------------------------- schemas
PARSED_COLS = ["form_type","xml_file","filer_name","filer_ein","return_year","grantee","recipient_ein",
    "grantee_addr1","grantee_addr2","grantee_city","grantee_state","grantee_zip","grantee_country",
    "grantee_addr_type","grant_amount","grant_purpose"]
GRANTSDB_COLS = ["xml_file","filer_name","filer_ein","return_year","grantee","grantee_addr1",
    "grantee_addr2","grantee_city","grantee_state","grantee_zip","grantee_country","grantee_addr_type",
    "grant_amount","grant_purpose","ipeds_unitid","uid","grantee_match","form","canonical_id","tag","grantee_support_org"]
# on-disk master skip-list / shard-record format (internal bookkeeping only --
# load_existing() reads solely column 0, so this can carry extra fields safely).
REF_COLS = ["xml_file","timestamp","form_type","filer_name","filer_ein","filer_support_org","return_year"]

# ---- upload-ready formats: what the three incremental_{stamp}.csv deliverables
# actually get written in, trimmed/reordered from the internal formats above. ----
DIM_UPLOAD_COLS = ["canonical_id","grantee_normalized","state","country","entity_class","city"]
GRANTSDB_UPLOAD_COLS = ["xml_file","grantee","grantee_addr1","grantee_addr2","grantee_city","grantee_state",
    "grantee_zip","grantee_country","grantee_addr_type","grant_amount","grant_purpose","canonical_id",
    "tag","uid"]
REF_UPLOAD_COLS = ["xml_file","timestamp","form_type","filer_name","filer_ein","return_year","funder_support_org"]
# per-grantee resolution audit (emitted after match; fill correct_ipeds_unitid to write back)
AUDIT_COLS = ["grantee","grantee_city","grantee_state","grantee_zip","grantee_country",
    "disposition","is_new_mint","canonical_id","canonical_name","canonical_city","ipeds_unitid",
    "entity_class","n_grants","total_amount","correct_ipeds_unitid","notes"]

# ====================================================================
# ---- inlined download + parse pipeline ----
"""
irs_990_pipeline.py
===================

One script that combines the IRS e-file **downloader** and the grant **parser**
into a single run. It:

  1. Downloads Form **990** (public charity) returns  -> form990_xml/<tax_year>/
  2. Downloads Form **990-PF** (private foundation) returns -> form990pf_xml/<tax_year>/
  3. Runs up to 24 simultaneous downloads.
  4. Ignores any return already listed in existing.csv (matched by XML filename).
  5. Parses every downloaded XML (both schema generations, both form types),
     keeping the original parser's college/university grantee filter and
     de-duplication of corrected re-filings, PLUS two new columns:
        - "Form type"     : "990" or "990pf"
        - "Recipient EIN" : the grantee EIN (present on 990 Schedule I; usually
                            blank on 990-PF, which historically omits it)
  6. Appends EVERY downloaded filename to existing.csv (the record file), so no
     return is ever downloaded twice -- across runs or resumes.
  7. DELETES any downloaded XML with no college/university grant (AFTER it is
     recorded in step 6), so only grant-bearing returns stay on disk. The final
     CSV is rebuilt from whatever survives, cumulatively across runs.

Design: download, parse, record, and delete are fused per file (24+ in
parallel), so disk never fills with returns that will be discarded. On one box,
--parallel N fans out into N hash-sharded download processes from a single
command, then rebuilds the CSV once (use ~one per core, but bandwidth usually
caps the useful count). Across machines, shard by tax year with --years and give
each machine its own --append-file, then concatenate into existing.csv.

By default it processes EVERYTHING: no download cap on either form type. Use
--limit-990 / --limit-990pf to cap NEW downloads (e.g. for a small test run).
The ~3 GB master index is cached on first run and reused (not re-downloaded)
on later runs.

Data source: the public, anonymous GivingTuesday 990 Data Lake
    s3://gt990datalake-rawdata  (region us-east-1)

Standard library only. Python 3.8+. No pip install, no AWS account.

Usage
-----
    python3 irs_990_pipeline.py                        # process EVERYTHING
    python3 irs_990_pipeline.py --data-dir C:/form990
    python3 irs_990_pipeline.py --limit-990 250 --limit-990pf 250   # small test
    python3 irs_990_pipeline.py --parallel 3            # 3 processes, one box
    python3 irs_990_pipeline.py --years 2019 2020 --append-file existing_2019_20.csv
    python3 irs_990_pipeline.py --parse-only           # rebuild CSV from disk
"""



# --------------------------------------------------------------------------- #
# Constants -- the public GivingTuesday 990 Data Lake
# --------------------------------------------------------------------------- #
BUCKET = "gt990datalake-rawdata"
BASE_URL = f"https://{BUCKET}.s3.amazonaws.com"
INDEX_PREFIX = "Indices/990xmls/"
XML_PREFIX = "EfileData/XmlFiles/"
S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"

USER_AGENT = "irs_990_pipeline/1.0 (+https://990data.givingtuesday.org)"
CHUNK = 1 << 16  # 64 KiB

# Plausible filing/tax year range. The source index contains a few corrupt
# dates (e.g. "7202", "2222"); anything outside this window -> "unknown" folder.
MIN_YEAR = 2000
MAX_YEAR = datetime.now().year + 1

# Two form types this pipeline handles, and where each lands.
FORM_TYPES = {
    "990": {"norm": "990", "dir": "form990_xml"},
    "990pf": {"norm": "990pf", "dir": "form990pf_xml"},
}

_lock = threading.Lock()
_stats = {"downloaded": 0, "skipped": 0, "ignored": 0, "failed": 0, "bytes": 0,
          "kept": 0, "deleted": 0, "recorded": 0, "supporting_org": 0,
          "public_charity_exclusion": 0}

# Filer EINs from public_charity_exclusions.csv -- curated 509(a)(3)/college
# support organizations whose grants would double-count money already tied to
# a single named university (see public_charity_exclusions.csv). Filtered on
# filer_ein only. Populated once at startup.
_PUBLIC_CHARITY_EXCLUSION_EINS = set()

# --- Fused-pipeline shared state: download -> parse -> record -> maybe delete.
_append_lock = threading.Lock()
_append_fh = None       # open handle to the record file (existing.csv)
_append_writer = None   # csv.writer bound to _append_fh

# Exceptions that mean "connection hiccup, retry / resume".
_TRANSIENT = (urllib.error.URLError, http.client.HTTPException, OSError)


# =========================================================================== #
#  PART 1 -- DOWNLOADER
# =========================================================================== #

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
    """Download `url` to `dest`, resuming with HTTP Range on any interruption."""
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
# Find the newest master index file in the bucket
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
# Parse the (local) index
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
# Load existing.csv -> set of XML filenames to ignore
# --------------------------------------------------------------------------- #
def load_existing(path: str) -> set:
    """Return the set of XML basenames already processed (from existing.csv).

    existing.csv has a single quoted column of filenames like
    '202630949349100103_public.xml'. We key on the basename so a match ignores
    the return regardless of which year folder it would land in.
    """
    seen = set()
    if not path or not os.path.exists(path):
        print(f"existing.csv: not found at {path} (nothing will be ignored)",
              flush=True)
        return seen
    with open(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        for i, row in enumerate(reader):
            if not row:
                continue
            val = row[0].strip()
            if i == 0 and val.lower() in ("xml_file", "xmlfile", "filename", "file"):
                continue  # header
            if val:
                seen.add(os.path.basename(val))
    print(f"existing.csv: {len(seen):,} filenames loaded (will be ignored)",
          flush=True)
    return seen


# --------------------------------------------------------------------------- #
# Load dedupe.csv -> set of (filer_ein, return_year) already emitted
# --------------------------------------------------------------------------- #
def load_dedupe(path) -> set:
    """Return the set of (filer_ein, return_year) pairs already written to the
    output CSV in a prior run. Rows matching a pair in this set are skipped
    during parsing so a filer/year already captured is never emitted twice."""
    seen = set()
    if not path or not os.path.exists(path):
        print(f"dedupe.csv: not found at {path} (nothing will be deduped)",
              flush=True)
        return seen
    with open(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        for i, row in enumerate(reader):
            if not row or len(row) < 2:
                continue
            if i == 0 and row[0].strip().lower() in ("filer_ein", "ein"):
                continue  # header
            seen.add((row[0].strip(), row[1].strip()))
    print(f"dedupe.csv: {len(seen):,} (filer_ein, return_year) pairs loaded "
          f"(will be skipped)", flush=True)
    return seen


# --------------------------------------------------------------------------- #
# Load public_charity_exclusions.csv -> set of filer EINs to exclude entirely
# --------------------------------------------------------------------------- #
def load_public_charity_exclusions(path) -> set:
    """Return the set of filer EINs from public_charity_exclusions.csv --
    curated 509(a)(3)/college support organizations tied to a single named
    university. Any return filed by one of these EINs is skipped entirely:
    its grants would double-count money already reflected in the university's
    own finances. Filtered on the filer_ein column only -- other columns
    (e.g. filer_name) are informational and ignored."""
    seen = set()
    if not path or not os.path.exists(path):
        print(f"public_charity_exclusions.csv: not found at {path} "
              f"(nothing will be excluded)", flush=True)
        return seen
    with open(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    if not rows:
        return seen
    header = [h.strip().lower() for h in rows[0]]
    ein_col = header.index("filer_ein") if "filer_ein" in header else None
    data_rows = rows[1:] if ein_col is not None else rows
    if ein_col is None:
        ein_col = 1  # legacy file with no recognized header
    for row in data_rows:
        if len(row) > ein_col:
            ein = row[ein_col].strip()
            if ein:
                seen.add(ein)
    print(f"public_charity_exclusions.csv: {len(seen):,} filer EINs loaded "
          f"(will be excluded)", flush=True)
    return seen


# --------------------------------------------------------------------------- #
# Download one return (with retries)
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


def resolve_index(args) -> str:
    """Return a path to a local index file, downloading/caching it if needed."""
    if args.index_file:
        if not os.path.exists(args.index_file):
            raise FileNotFoundError(args.index_file)
        print(f"Index:   {args.index_file} (local)", flush=True)
        return args.index_file

    index_key = args.index_key or find_latest_index()
    url = f"{BASE_URL}/{urllib.parse.quote(index_key)}"
    os.makedirs(args.data_dir, exist_ok=True)
    local = os.path.join(args.data_dir, "_index_" + index_key.rsplit("/", 1)[-1])
    if args.refresh_index and os.path.exists(local):
        os.remove(local)
    print(f"Index:   {url}", flush=True)
    print(f"Caching: {local}", flush=True)
    print("Fetching index (one-time, resumable)...", flush=True)
    download_resumable(url, local, "index")
    return local


def open_append(path):
    """Open the record file (existing.csv) for appending downloaded filenames.
    Creates it with an 'xml_file' header when it does not exist yet."""
    global _append_fh, _append_writer
    fresh = (not os.path.exists(path)) or os.path.getsize(path) == 0
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    _append_fh = open(path, "a", newline="", encoding="utf-8")
    _append_writer = csv.writer(_append_fh, quoting=csv.QUOTE_ALL)
    if fresh:
        _append_writer.writerow(["xml_file"])
        _append_fh.flush()
    print(f"Recording: appending downloaded filenames to {path}", flush=True)


def close_append():
    """Flush and close the record file."""
    global _append_fh, _append_writer
    if _append_fh is not None:
        try:
            _append_fh.flush()
            _append_fh.close()
        except OSError:
            pass
    _append_fh = _append_writer = None


def record_existing(basename, existing_set):
    """Append one downloaded XML's basename to the record file (thread-safe).
    ALWAYS runs before any deletion, so an interrupted run never refetches the
    file -- returns known to have no college grant stay recorded but off disk."""
    with _append_lock:
        existing_set.add(basename)
        if _append_writer is not None:
            _append_writer.writerow([basename])
            _append_fh.flush()
        _stats["recorded"] += 1


def process_one(key, dest, form_type, overwrite, existing_set):
    """Fused unit of work: download one return, parse it, record it, and delete
    it when it holds no college/university grant. Grant-bearing files are kept
    on disk; the final CSV is rebuilt from whatever survives (run_parse). The
    record-then-delete order guarantees a killed run never redownloads a file
    already known to be empty."""
    status = download_one(key, dest, overwrite)
    if status == "failed":
        return
    filing = parse_filing(dest, form_type)          # None on parse error
    basename = os.path.basename(dest)
    record_existing(basename, existing_set)          # record BEFORE any delete
    if filing is not None and filing["rows"]:
        with _lock:
            _stats["kept"] += 1
    else:
        try:
            os.remove(dest)
        except OSError:
            pass
        with _lock:
            _stats["deleted"] += 1


def run_download(args, existing: set) -> None:
    """Single index scan that serves BOTH form types, each with its own cap."""
    local_index = resolve_index(args)
    print(f"Output:  form990 -> {os.path.join(args.data_dir, FORM_TYPES['990']['dir'])}",
          flush=True)
    print(f"         990pf   -> {os.path.join(args.data_dir, FORM_TYPES['990pf']['dir'])}",
          flush=True)
    print(f"Filter:  FormType in (990, 990PF) | folder by {args.year_field} year",
          flush=True)
    print(f"Caps:    990 -> {args.limit_990} new | 990pf -> {args.limit_990pf} new "
          f"| workers {args.workers}", flush=True)
    print("\nDownloading returns as the index is parsed...\n", flush=True)

    # Map normalized index FormType -> our form key.
    want = {FORM_TYPES[k]["norm"]: k for k in FORM_TYPES}
    caps = {"990": args.limit_990, "990pf": args.limit_990pf}
    dispatched = {"990": 0, "990pf": 0}
    year_set = set(args.years) if args.years else None

    seen_obj = set()
    scanned = 0
    t0 = last = time.time()
    max_inflight = max(args.workers * 8, 24)
    pool = ThreadPoolExecutor(max_workers=args.workers)
    inflight = {}

    def heartbeat(force=False):
        nonlocal last
        now = time.time()
        if force or now - last >= 10:
            rate = scanned / max(now - t0, 1e-6)
            print(f"  scanned {scanned:,} rows ({rate:,.0f}/s) | "
                  f"990 disp {dispatched['990']}/{caps['990']} | "
                  f"990pf disp {dispatched['990pf']}/{caps['990pf']} | "
                  f"dl {_stats['downloaded']:,} kept {_stats['kept']:,} "
                  f"del {_stats['deleted']:,} ignored {_stats['ignored']:,} "
                  f"fail {_stats['failed']:,}",
                  flush=True)
            last = now

    def harvest(block):
        if not inflight:
            return
        ready = (wait(list(inflight), return_when=FIRST_COMPLETED)[0]
                 if block else [f for f in list(inflight) if f.done()])
        for fut in ready:
            inflight.pop(fut)
            fut.result()

    def caps_reached():
        return all(dispatched[k] >= caps[k] for k in caps)

    try:
        for row in iter_index_rows(local_index):
            scanned += 1
            heartbeat()

            form_key = want.get(_norm(_get(row, "formtype") or ""))
            if not form_key:
                continue
            if dispatched[form_key] >= caps[form_key]:
                if caps_reached():
                    print("  all caps reached, stopping index scan", flush=True)
                    break
                continue

            key = xml_key_for(row)
            if not key:
                continue
            object_id = _get(row, "objectid") or key.rsplit("/", 1)[-1]
            if object_id in seen_obj:
                continue
            seen_obj.add(object_id)

            # Hash-shard: in a --parallel run each child process handles a
            # disjoint slice of returns (covers every year, incl. "unknown",
            # with no gaps or overlaps). crc32 is stable across processes;
            # the builtin hash() is NOT (randomized per process).
            if args.shard_count > 1 and \
                    zlib.crc32(object_id.encode()) % args.shard_count != args.shard_index:
                continue

            basename = os.path.basename(key)
            if basename in existing:            # already processed previously
                with _lock:
                    _stats["ignored"] += 1
                continue

            year = filing_year(row, args.year_field)
            if year_set and year not in year_set:
                continue
            dest = os.path.join(args.data_dir, FORM_TYPES[form_key]["dir"],
                                year, basename)

            dispatched[form_key] += 1
            inflight[pool.submit(process_one, key, dest, form_key, args.overwrite, existing)] = True
            harvest(block=False)
            while len(inflight) >= max_inflight:
                harvest(block=True)

            if caps_reached():
                print("  all caps reached, stopping index scan", flush=True)
                break

        while inflight:
            harvest(block=True)
    finally:
        pool.shutdown(wait=True)

    heartbeat(force=True)
    print(f"\nDownload+parse done. downloaded={_stats['downloaded']:,} "
          f"kept(has grant)={_stats['kept']:,} "
          f"deleted(no grant)={_stats['deleted']:,} "
          f"recorded={_stats['recorded']:,} "
          f"ignored(existing.csv)={_stats['ignored']:,} "
          f"supporting_org_skipped={_stats['supporting_org']:,} "
          f"public_charity_exclusion_skipped={_stats['public_charity_exclusion']:,} "
          f"failed={_stats['failed']:,} ({_stats['bytes'] / 1e6:,.1f} MB)",
          flush=True)


# =========================================================================== #
#  PART 2 -- PARSER
# =========================================================================== #

def get_namespace(root):
    """Extract the XML namespace from the root tag."""
    if root.tag.startswith("{"):
        return root.tag[1: root.tag.find("}")]
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
    """Parse an IRS <ReturnTs> into an aware datetime for comparison."""
    if not ts:
        return datetime.min.replace(tzinfo=timezone.utc)
    s = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:10])
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# === ADDRESS EXTRACTION ===

EMPTY_ADDRESS = {
    "line1": "", "line2": "", "city": "",
    "state": "", "zip": "", "country": "", "type": "",
}


def extract_address(grant_elem, ns_uri):
    """Pull the recipient address from one grant group into discrete fields.

    Handles both schema generations and both address kinds. Tag variants also
    cover Form 990 Schedule I, whose recipient blocks are <USAddress> /
    <ForeignAddress> (no 'Recipient' prefix).
    """
    us = find_first(grant_elem, ["RecipientUSAddress", "USAddress", "AddressUS"], ns_uri)
    if us is not None:
        return {
            "line1": text_of(us, ["AddressLine1Txt", "AddressLine1"], ns_uri),
            "line2": text_of(us, ["AddressLine2Txt", "AddressLine2"], ns_uri),
            "city": text_of(us, ["CityNm", "City"], ns_uri),
            "state": text_of(us, ["StateAbbreviationCd", "State"], ns_uri),
            "zip": text_of(us, ["ZIPCd", "ZIPCode"], ns_uri),
            "country": "US",
            "type": "US",
        }

    foreign = find_first(grant_elem, ["RecipientForeignAddress", "ForeignAddress", "AddressForeign"], ns_uri)
    if foreign is not None:
        return {
            "line1": text_of(foreign, ["AddressLine1Txt", "AddressLine1"], ns_uri),
            "line2": text_of(foreign, ["AddressLine2Txt", "AddressLine2"], ns_uri),
            "city": text_of(foreign, ["CityNm", "City"], ns_uri),
            "state": text_of(foreign, ["ProvinceOrStateNm", "ProvinceOrState"], ns_uri),
            "zip": text_of(foreign, ["ForeignPostalCd", "PostalCode"], ns_uri),
            "country": text_of(foreign, ["CountryCd", "Country"], ns_uri),
            "type": "Foreign",
        }

    return dict(EMPTY_ADDRESS)


# === GRANTEE FILTERING ===

INCLUDE_KEYWORDS = [
    "college",
    "university",
    "institute of technology",
    "polytechnic",
    "school of mines",
]

INCLUDE_NAMES = [
    "juilliard",
    "pratt institute",
    "cooper union",
    "leland stanford",
    "caltech",
]

EXCLUDE_KEYWORDS = [
    "college board",
    "college prep",         # K-12 charter/prep schools branded "___ College Prep(aratory) Academy"
    "college preparatory",
]


# A return checking any of these Schedule A boxes identifies the FILER itself
# as a college/university, a school, a 509(a)(3) supporting organization of
# one, or a hospital. Grants reported by such a filer would double-count money
# already reflected in the college's/school's/hospital's own finances, so the
# whole return is skipped.
EXCLUDED_FILER_FLAG_TAGS = [
    "CollegeOrganizationInd",
    "SupportingOrganization509a3Ind",
    "SupportingOrganization509a3",
    "HospitalInd",
    "Hospital170b1Aiii",
    "SchoolInd",
    "School170b1Aii",
]


def is_excluded_filer(root, ns_uri):
    """True if the filer flagged itself as a college, a school, a 509(a)(3)
    supporting organization, or a hospital (IRS990ScheduleA checkboxes) --
    these must be excluded."""
    for tag in EXCLUDED_FILER_FLAG_TAGS:
        if text_of(root, [tag], ns_uri).strip().upper() == "X":
            return True
    return False


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

def parse_filing(xml_path, form_type, flag_support_org=False):
    """Parse one XML filing (990 or 990-PF).

    `form_type` is "990" or "990pf" and is derived from which download tree the
    file came from. Returns a dict (see fields below) or None if unusable.

    `flag_support_org` toggles behaviour for the incremental updater:
      - False (default, legacy): a support-org / college / school / hospital
        filer (Schedule-A flags) causes the whole return to be dropped (None).
      - True: NOTHING is dropped for double-count reasons. The return dict carries
        `filer_support_org` (bool = the filer matches the 7 Schedule-A support-org
        flags) for the ref_xml_processed record. The per-grant `grantee_support_org`
        flag ("foundation" in the grantee name) is computed downstream in stage_tag.

    Grant groups searched (any that appear):
        990-PF 2015+ : GrantOrContributionPdDurYrGrp
        990-PF pre15 : GrantOrContriPaidDuringYear
        990 Sched I  : RecipientTable   (carries RecipientEIN)
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

    filer_support = is_excluded_filer(root, ns_uri)     # 990 grantor support-org flag
    if filer_support and not flag_support_org:
        with _lock:
            _stats["supporting_org"] += 1
        return None

    # Tax year: prefer TaxYr/TaxYear; fall back to the year of TaxPeriodEndDt.
    tax_year = text_of(root, ["TaxYr", "TaxYear"], ns_uri)
    if not tax_year:
        period_end = text_of(root, ["TaxPeriodEndDt", "TaxPeriodEndDate"], ns_uri)
        if len(period_end) >= 4:
            tax_year = period_end[:4]

    return_ts = parse_return_ts(text_of(root, ["ReturnTs"], ns_uri))

    # Filer information
    filer = root.find(f".//{ns}Filer")
    if filer is None:
        print(f"\n  No <Filer> found in {filename}")
        return None

    filer_ein = text_of(filer, ["EIN"], ns_uri)

    # NOTE: public_charity_exclusions.csv is intentionally NOT applied here --
    # double-count handling is deferred to the query layer (support_org flag +
    # query-side filtering). The legacy EIN-exclusion path has been removed.

    filer_name = get_text(filer, "BusinessName/BusinessNameLine1Txt", ns_uri)
    if not filer_name:
        filer_name = get_text(filer, "BusinessName/BusinessNameLine1", ns_uri)
    if not filer_name:
        filer_name = get_text(filer, "Name/BusinessNameLine1", ns_uri)

    # Grant groups under all handled schemas / form types.
    grant_tags = [
        f"{ns}GrantOrContributionPdDurYrGrp",   # 990-PF 2015+
        f"{ns}GrantOrContriPaidDuringYear",       # 990-PF pre-2015
        f"{ns}RecipientTable",                    # 990 Schedule I
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
            # Pre-2015 Form 990 Schedule I uses <RecipientNameBusiness>.
            grantee = get_text(g, "RecipientNameBusiness/BusinessNameLine1", ns_uri)
        if not grantee:
            grantee = text_of(g, ["RecipientPersonNm", "RecipientPersonName"], ns_uri)

        if not is_college_or_university(grantee):
            continue

        # Recipient EIN -- present on 990 Schedule I (2015+: RecipientEIN;
        # pre-2015: EINOfRecipient). Usually blank on 990-PF.
        recipient_ein = text_of(g, ["RecipientEIN", "EINOfRecipient"], ns_uri)

        # Recipient address -> discrete fields (US or foreign, both schemas).
        addr = extract_address(g, ns_uri)

        # Amount across schema/form variants (990-PF: Amt/CashGrantAmt/Amount;
        # 990 Sched I 2015+: CashGrantAmt; pre-2015: AmountOfCashGrant).
        amount = text_of(g, ["Amt", "CashGrantAmt", "Amount", "AmountOfCashGrant"], ns_uri)

        # Purpose across schema/form variants.
        purpose = text_of(g, [
            "GrantOrContributionPurposeTxt",
            "PurposeOfGrantTxt",
            "PurposeOfGrantOrContribution",
            "PurposeOfGrant",
        ], ns_uri)

        row = [
            form_type,          # NEW: "990" or "990pf"
            filename,
            filer_name,
            filer_ein,
            tax_year,
            grantee,
            recipient_ein,      # NEW: grantee EIN (from the 990)
            addr["line1"],
            addr["line2"],
            addr["city"],
            addr["state"],
            addr["zip"],
            addr["country"],
            addr["type"],
            amount,
            purpose,
        ]
        rows.append(row)

    submission_id = ""
    for ch in filename:
        if ch.isdigit():
            submission_id += ch
        else:
            break

    return {
        "form_type": form_type,
        "filename": filename,
        "filer_name": filer_name,
        "filer_ein": filer_ein,
        "tax_year": tax_year,
        "return_ts": return_ts,
        "submission_id": submission_id,
        "rows": rows,
        "filer_support_org": bool(filer_support),   # return-level: filer matches the 7 Schedule-A support-org flags
    }


def collect_xml(root_dir, form_type):
    """Return list of (xml_path, form_type) for every .xml beneath root_dir."""
    out = []
    if not os.path.isdir(root_dir):
        return out
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for fn in filenames:
            if fn.lower().endswith(".xml"):
                out.append((os.path.join(dirpath, fn), form_type))
    return out


def run_parse(args) -> None:
    """Walk both download trees, parse, de-duplicate, write one flat CSV."""
    targets = []
    for key, meta in FORM_TYPES.items():
        targets.extend(collect_xml(os.path.join(args.data_dir, meta["dir"]), key))

    if not targets:
        print("No XML files found to parse under the download trees.")
        return

    total = len(targets)
    print(f"\nParsing {total} XML files (990 + 990pf)...", flush=True)

    dedupe_path = args.dedupe_file
    dedupe_seen = load_dedupe(dedupe_path)
    dedupe_skipped = 0

    # De-duplicate corrected re-filings: keep the latest ReturnTs per
    # (form type, filer EIN, tax year). Include form type in the key so the two
    # trees never collide.
    best = {}
    for i, (xml_path, form_type) in enumerate(targets, 1):
        filing = parse_filing(xml_path, form_type)
        if filing is not None:
            dedupe_key = (filing["filer_ein"], filing["tax_year"])
            if dedupe_key in dedupe_seen:
                dedupe_skipped += 1
            else:
                ein = filing["filer_ein"] or f"__noein__:{xml_path}"
                key = (filing["form_type"], ein, filing["tax_year"])
                cur = best.get(key)
                if cur is None:
                    best[key] = filing
                else:
                    newer = (filing["return_ts"], filing["submission_id"]) > \
                            (cur["return_ts"], cur["submission_id"])
                    if newer:
                        best[key] = filing
        if i % 25 == 0 or i == total:
            sys.stdout.write(f"\r  Processing: {i}/{total} files")
            sys.stdout.flush()
    print()
    if dedupe_skipped:
        print(f"  Skipped {dedupe_skipped:,} filings already in dedupe.csv "
              f"(filer_ein + return_year already emitted)", flush=True)
    if _stats["supporting_org"]:
        print(f"  Skipped {_stats['supporting_org']:,} filings flagged as "
              f"college/school/509(a)(3) supporting organizations/hospitals",
              flush=True)
    if _stats["public_charity_exclusion"]:
        print(f"  Skipped {_stats['public_charity_exclusion']:,} filings from "
              f"filers listed in public_charity_exclusions.csv", flush=True)

    all_rows = []
    for filing in best.values():
        all_rows.extend(filing["rows"])

    headers = [
        "Form type",                # NEW
        "XML file",
        "Filer name",
        "Filer EIN",
        "Return year",
        "Grantee",
        "Recipient EIN",            # NEW
        "Grantee address line 1",
        "Grantee address line 2",
        "Grantee city",
        "Grantee state/province",
        "Grantee ZIP/postal",
        "Grantee country",
        "Grantee address type",
        "Grant amount",
        "Grant purpose",
    ]

    out_csv = os.path.join(args.data_dir, args.output_csv)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(all_rows)

    print(f"Kept {len(best)} unique filings (form + filer + tax year) "
          f"after de-duplicating corrections.")
    print(f"Wrote {len(all_rows)} grant rows to {out_csv}")


# =========================================================================== #
#  ORCHESTRATION
# =========================================================================== #

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Download IRS Form 990 and 990-PF XML returns and parse "
                    "college/university grants into one flat CSV.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--data-dir", default=r"C:\form990",
                   help="Root dir holding existing.csv and the output trees.")
    p.add_argument("--year-field", choices=("submitted", "tax"), default="tax",
                   help="'tax' = tax year covered; 'submitted' = year filed.")
    p.add_argument("--limit-990", type=int, default=0,
                   help="Max NEW Form 990 downloads (0 = no limit, the default).")
    p.add_argument("--limit-990pf", type=int, default=0,
                   help="Max NEW Form 990-PF downloads (0 = no limit, the default).")
    p.add_argument("--workers", type=int, default=24,
                   help="Parallel download threads.")
    p.add_argument("--existing", default=None,
                   help="Path to existing.csv (default: <data-dir>/existing.csv).")
    p.add_argument("--dedupe-file", default=None,
                   help="Path to dedupe.csv, tracking (filer_ein, return_year) "
                        "pairs already emitted to the output CSV -- matching "
                        "filings are skipped on future parses (default: "
                        "<data-dir>/dedupe.csv).")
    p.add_argument("--exclusions-file", default=None,
                   help="Path to public_charity_exclusions.csv, listing filer "
                        "EINs for 509(a)(3)/college support organizations "
                        "tied to a single university -- their returns are "
                        "skipped entirely to avoid double-counting. Filtered "
                        "on the filer_ein column only (default: "
                        "<data-dir>/public_charity_exclusions.csv).")
    p.add_argument("--output-csv", default="all_grants_combined.csv",
                   help="Output CSV filename (written into --data-dir).")
    p.add_argument("--index-file", default=None,
                   help="Use this local index CSV instead of downloading one.")
    p.add_argument("--index-key", default=None,
                   help="Override the index S3 key instead of auto-detecting.")
    p.add_argument("--refresh-index", action="store_true",
                   help="Re-download the index even if a cached copy exists.")
    p.add_argument("--overwrite", action="store_true",
                   help="Re-download XMLs even if they already exist on disk.")
    p.add_argument("--download-only", action="store_true",
                   help="Download/parse/record/delete but skip final CSV rebuild.")
    p.add_argument("--parse-only", action="store_true",
                   help="Rebuild the CSV from XML already on disk; no download.")
    p.add_argument("--years", nargs="+", metavar="YYYY", default=None,
                   help="Only process these tax years (e.g. --years 2019 2020). "
                        "Use to shard the job across machines/processes.")
    p.add_argument("--append-file", default=None,
                   help="File to append downloaded filenames to (default: the "
                        "--existing file). For sharded runs give each shard its "
                        "own file, then concatenate afterward.")
    p.add_argument("--parallel", type=int, default=1, metavar="N",
                   help="Run N download processes in parallel from this one "
                        "command, then rebuild the CSV once. Good = ~one per "
                        "core, but bandwidth usually caps the useful count. "
                        "N=1 (default) runs single-process.")
    # Internal: set by the --parallel orchestrator on each child. Not for
    # manual use, but harmless if set.
    p.add_argument("--shard-count", type=int, default=1, help=argparse.SUPPRESS)
    p.add_argument("--shard-index", type=int, default=0, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    # 0 means "no limit" -> treat as effectively unbounded for cap logic.
    if args.limit_990 == 0:
        args.limit_990 = sys.maxsize
    if args.limit_990pf == 0:
        args.limit_990pf = sys.maxsize
    if args.existing is None:
        args.existing = os.path.join(args.data_dir, "existing.csv")
    if args.dedupe_file is None:
        args.dedupe_file = os.path.join(args.data_dir, "dedupe.csv")
    if args.exclusions_file is None:
        args.exclusions_file = os.path.join(args.data_dir,
                                             "public_charity_exclusions.csv")
    if args.append_file is None:
        args.append_file = args.existing
    return args


def merge_shard_records(existing_path, shard_files):
    """Concatenate each shard's per-shard record file into existing.csv so the
    master skip-list is complete for the next run. Shards are disjoint (hashed),
    so no de-duplication is needed."""
    total = 0
    with open(existing_path, "a", newline="", encoding="utf-8") as out:
        w = csv.writer(out, quoting=csv.QUOTE_ALL)
        for sf in shard_files:
            if not os.path.exists(sf):
                continue
            with open(sf, newline="", encoding="utf-8") as fh:
                r = csv.reader(fh)
                next(r, None)  # skip the shard file's "xml_file" header
                for row in r:
                    if row and row[0]:
                        w.writerow([row[0]])
                        total += 1
    print(f"Merged {total:,} recorded filenames from {len(shard_files)} shard "
          f"file(s) into {existing_path}", flush=True)


def run_parallel(args) -> int:
    """Launch args.parallel download-only child processes (hash-sharded), wait
    for them, merge their record files into existing.csv, then let the caller
    rebuild the CSV once. Resolves the index a single time and hands children a
    fixed --index-file so they never each re-detect/download it."""
    n = args.parallel
    local_index = resolve_index(args)          # cache/verify the index once
    script = os.path.abspath(__file__)
    lim990 = "0" if args.limit_990 == sys.maxsize else str(args.limit_990)
    lim990pf = "0" if args.limit_990pf == sys.maxsize else str(args.limit_990pf)

    shard_files, procs = [], []
    print(f"\nOrchestrating {n} parallel download shards...\n", flush=True)
    for k in range(n):
        sf = os.path.join(args.data_dir, f"_shard_{k}.csv")
        shard_files.append(sf)
        cmd = [sys.executable, script,
               "--data-dir", args.data_dir,
               "--year-field", args.year_field,
               "--workers", str(args.workers),
               "--existing", args.existing,
               "--append-file", sf,
               "--index-file", local_index,
               "--exclusions-file", args.exclusions_file,
               "--download-only",
               "--shard-count", str(n),
               "--shard-index", str(k),
               "--limit-990", lim990,
               "--limit-990pf", lim990pf]
        if args.overwrite:
            cmd.append("--overwrite")
        if args.years:
            cmd += ["--years"] + list(args.years)
        print(f"  launching shard {k + 1}/{n} -> {sf}", flush=True)
        procs.append(subprocess.Popen(cmd))

    rc = 0
    for pr in procs:
        rc |= pr.wait()
    print("\nAll shards finished.", flush=True)
    merge_shard_records(args.existing, shard_files)
    return rc


def main(argv=None) -> int:
    args = parse_args(argv)
    global _PUBLIC_CHARITY_EXCLUSION_EINS
    _PUBLIC_CHARITY_EXCLUSION_EINS = load_public_charity_exclusions(args.exclusions_file)
    try:
        if args.parse_only:
            run_parse(args)
            return 0
        # Orchestrator path: one command fans out into N shard processes.
        # Children carry shard_count > 1, so they never re-enter this branch.
        if args.parallel > 1 and args.shard_count == 1:
            rc = run_parallel(args)
            if not args.download_only:
                run_parse(args)
            return rc
        existing = load_existing(args.existing)
        open_append(args.append_file)
        try:
            run_download(args, existing)
        finally:
            close_append()
        if not args.download_only:
            run_parse(args)
        return 1 if _stats["failed"] else 0
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run the same command to resume.", file=sys.stderr)
        return 130



# ================================================================ orchestration
def _p(work, name, stamp): return os.path.join(work, f"{name}_{stamp}.csv")

def _scan_dispatch(a, on_ready):
    """Shared index scan + concurrent dispatch with a live heartbeat. Calls
    on_ready(key, dest, form_type) for every new in-scope return. Returns
    (n_dispatched, stats_dict)."""
    existing = load_existing(a.existing)
    idx = resolve_index(a)
    want = {FORM_TYPES[k]["norm"]: k for k in FORM_TYPES}
    caps = {"990": a.limit_990, "990pf": a.limit_990pf}
    disp = {"990": 0, "990pf": 0}
    yr = set(a.years) if a.years else None
    yrx = set(a.years_exclude) if getattr(a, "years_exclude", None) else None
    lbl = f"[{a.shard_label}] " if getattr(a, "shard_label", "") else ""
    seen = set(); scanned = 0; t0 = time.time(); last = [t0, 0, 0]  # [time, scanned, dl] at last hb
    def uncapped(k): return caps[k] == 0 or disp[k] < caps[k]
    pool = ThreadPoolExecutor(max_workers=a.workers); inflight = {}
    def harvest(block):
        if not inflight: return
        rdy = (wait(list(inflight), return_when=FIRST_COMPLETED)[0] if block
               else [f for f in list(inflight) if f.done()])
        for f in rdy: inflight.pop(f); f.result()
    def hb(force=False):
        now = time.time()
        if force or now - last[0] >= 5:
            s = on_ready.stats
            dt = max(now - last[0], 1e-6)                    # window since last heartbeat
            srate = (scanned - last[1]) / dt                 # CURRENT scan rate, not cumulative
            drate = (s['dl'] - last[2]) / dt                 # CURRENT download rate (files/s)
            print(f"{lbl}scanned {scanned:,} ({srate:,.0f}/s) | dispatched 990={disp['990']:,} "
                  f"990pf={disp['990pf']:,} | downloaded {s['dl']:,} ({drate:,.0f}/s) | kept {s['kept']:,} "
                  f"({s['grants']:,} grants) | deleted {s['del']:,} | failed {s.get('fail',0):,}", flush=True)
            last[0], last[1], last[2] = now, scanned, s['dl']
    for row in iter_index_rows(idx):
        scanned += 1; hb()
        fk = want.get(_norm(_get(row, "formtype") or ""))
        if not fk or not uncapped(fk):
            if all(not uncapped(k) for k in caps): break
            continue
        key = xml_key_for(row)
        if not key: continue
        oid = _get(row, "objectid") or key.rsplit("/", 1)[-1]
        if oid in seen: continue
        seen.add(oid)
        base = os.path.basename(key)
        if base in existing: continue
        year = filing_year(row, a.year_field)
        if yr and year not in yr: continue
        if yrx and year in yrx: continue
        dest = os.path.join(a.data_dir, FORM_TYPES[fk]["dir"], year, base)
        disp[fk] += 1
        inflight[pool.submit(on_ready, key, dest, fk)] = True
        harvest(block=False)
        while len(inflight) >= a.workers * 8: harvest(block=True)
        if all(not uncapped(k) for k in caps): break
    while inflight: harvest(block=True)
    pool.shutdown(); hb(force=True)
    return disp["990"] + disp["990pf"]

def stage_acquire(a):
    """FUSED download+parse: each worker downloads ONE return, parses it right
    away, records it, and keeps only grant-bearing XML -- concurrently, so
    parsing overlaps downloading (network is the bottleneck). Live heartbeat."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    grant_rows = []; ref_rows = []; lock = threading.Lock()
    def work(key, dest, ft):
        st = download_one(key, dest, a.overwrite)
        if st == "failed":
            with lock: work.stats["fail"] += 1
            return
        with lock: work.stats["dl"] += 1
        fl = parse_filing(dest, ft, flag_support_org=True)
        base = os.path.basename(dest)
        with lock:
            if fl is not None:
                ref_rows.append([base, now, fl["form_type"], fl["filer_name"], fl["filer_ein"], fl["filer_support_org"], fl["tax_year"]])
            if fl is not None and fl["rows"]:
                grant_rows.extend(fl["rows"]); work.stats["kept"] += 1; work.stats["grants"] += len(fl["rows"])
            else:
                if not a.keep_xml:
                    try: os.remove(dest)
                    except OSError: pass
                work.stats["del"] += 1
    work.stats = {"dl": 0, "kept": 0, "grants": 0, "del": 0, "fail": 0}
    print("[acquire] fused download+parse (parsing overlaps downloading)...", flush=True)
    _scan_dispatch(a, work)
    with open(_p(a.work_dir, "_parsed", a.stamp), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(PARSED_COLS); w.writerows(grant_rows)
    _write_ref(a, ref_rows)
    fso = sum(1 for r in ref_rows if r[5]); gso = sum(1 for r in grant_rows if "foundation" in str(r[5]).lower())
    print(f"[acquire] {len(ref_rows)} returns ({fso} filer_support_org) | {len(grant_rows)} college grants "
          f"({gso} grantee_support_org) -> _parsed_{a.stamp}.csv", flush=True)

def _bool_str(v):
    """True"/"False" regardless of whether v is a real bool (single-process, still
    in memory) or the string "True"/"False" (re-read from a merged shard CSV) --
    a naive `"True" if v else "False"` would misfire on the string "False", which
    is truthy in Python."""
    if isinstance(v, bool):
        return "True" if v else "False"
    return "True" if str(v).strip().lower() == "true" else "False"

def _write_ref(a, ref_rows):
    dst = getattr(a, "append_file", None) or a.existing   # shards -> own file, no races
    hdr = not os.path.exists(dst) or os.path.getsize(dst) == 0
    with open(dst, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if hdr: w.writerow(REF_COLS)
        w.writerows(ref_rows)
    # upload deliverable: xml_file,timestamp,form_type,filer_name,filer_ein,return_year,funder_support_org
    upload_rows = [[r[0], r[1], r[2], r[3], r[4], r[6], _bool_str(r[5])] for r in ref_rows]
    with open(_p(a.work_dir, "ref_xml_processed_incremental", a.stamp), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(REF_UPLOAD_COLS); w.writerows(upload_rows)

def stage_download(a):
    """download ONLY (when parse is not requested): fetch to disk + manifest,
    with the same live heartbeat."""
    manifest = []; lock = threading.Lock()
    def work(key, dest, ft):
        if download_one(key, dest, a.overwrite) != "failed":
            with lock: manifest.append((dest, ft)); work.stats["dl"] += 1
        else:
            with lock: work.stats["fail"] += 1
    work.stats = {"dl": 0, "kept": 0, "grants": 0, "del": 0, "fail": 0}
    print("[download] scanning index; downloading everything not already processed...", flush=True)
    _scan_dispatch(a, work)
    with open(_p(a.work_dir, "_manifest", a.stamp), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["xml_path", "form_type"]); w.writerows(manifest)
    print(f"[download] {len(manifest)} new returns downloaded -> _manifest_{a.stamp}.csv", flush=True)

def stage_parse(a):
    """parse ONLY (from a prior download's manifest), with progress every 200 files."""
    man = _p(a.work_dir, "_manifest", a.stamp)
    if not os.path.exists(man): sys.exit(f"[parse] manifest missing: {man}")
    rows = list(csv.reader(open(man, encoding="utf-8")))[1:]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    grant_rows = []; ref_rows = []; n_grant = 0
    print(f"[parse] parsing {len(rows):,} downloaded returns...", flush=True)
    for i, (dest, ft) in enumerate(rows, 1):
        if not os.path.exists(dest): continue
        fl = parse_filing(dest, ft, flag_support_org=True)     # tag, never exclude
        base = os.path.basename(dest)
        if fl is not None:
            ref_rows.append([base, now, fl["form_type"], fl["filer_name"], fl["filer_ein"], fl["filer_support_org"], fl["tax_year"]])
        if fl is not None and fl["rows"]:
            grant_rows.extend(fl["rows"]); n_grant += 1
        elif not a.keep_xml:
            try: os.remove(dest)
            except OSError: pass
        if i % 200 == 0 or i == len(rows):
            print(f"  parsed {i:,}/{len(rows):,} | grant-bearing {n_grant:,} | {len(grant_rows):,} grants", flush=True)
    with open(_p(a.work_dir, "_parsed", a.stamp), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(PARSED_COLS); w.writerows(grant_rows)
    _write_ref(a, ref_rows)
    fso = sum(1 for r in ref_rows if r[5]); gso = sum(1 for r in grant_rows if "foundation" in str(r[5]).lower())
    print(f"[parse] {len(ref_rows)} returns recorded ({fso} filer_support_org) | {len(grant_rows)} college grants "
          f"from {n_grant} returns ({gso} grantee_support_org) -> _parsed_{a.stamp}.csv", flush=True)

def stage_match(a):
    src = _p(a.work_dir, "_parsed", a.stamp)
    if not os.path.exists(src): sys.exit(f"[match] parsed file missing: {src}")
    d = pd.read_csv(src, dtype=str, keep_default_na=False)
    dim = build_dim(a.dim)                                    # ONLY reference file
    cl = Anthropic()
    seen = {}
    for _, r in d.iterrows():
        k = (qnorm(r["grantee"]), str(r["grantee_city"]).upper(), str(r["grantee_state"]).upper())
        if k not in seen:
            seen[k] = {"key":k,"grantee":r["grantee"],"city":r["grantee_city"],
                       "state":r["grantee_state"],"zip":r["grantee_zip"],"country":r["grantee_country"]}
    resmap, new_dim = resolve_institutions(list(seen.values()), dim, cl, use_batch=not a.sync)
    ip = []; can = []
    for _, r in d.iterrows():
        k = (qnorm(r["grantee"]), str(r["grantee_city"]).upper(), str(r["grantee_state"]).upper())
        cid = resmap.get(k, "")
        can.append(cid)
        ip.append(dim["info"].get(cid, {}).get("ipeds", "") if cid not in ("", "NA", "NCI") else "")
    d["ipeds_unitid"] = ip; d["canonical_id"] = can
    d.to_csv(_p(a.work_dir, "_matched", a.stamp), index=False, encoding="utf-8-sig")
    # upload deliverable drops ipeds_unitid (always blank for a brand-new mint anyway)
    new_dim_upload = [[row[0], row[1], row[3], row[4], row[5], row[6]] for row in new_dim]
    with open(_p(a.work_dir, "dim_institution_incremental", a.stamp), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(DIM_UPLOAD_COLS); w.writerows(new_dim_upload)
    _write_match_audit(a, d, seen, resmap, dim, new_dim)
    print(f"[match] {len(seen)} unique grantees | dim-resolved deterministically + Haiku residual "
          f"| new dim institutions: {len(new_dim)} -> _matched_{a.stamp}.csv", flush=True)

def _write_match_audit(a, d, seen, resmap, dim, new_dim):
    """One row per UNIQUE grantee: its name+address, what it resolved to, the
    disposition, and an empty `correct_ipeds_unitid` to fill for a write-back.
    New mints and review cases sort to the top, ordered by grant dollars, so the
    highest-impact resolutions are audited first."""
    new_cids = {row[0] for row in new_dim}
    agg = {}                                              # key -> [n_grants, total_amount]
    for _, r in d.iterrows():
        k = (qnorm(r["grantee"]), str(r["grantee_city"]).upper(), str(r["grantee_state"]).upper())
        v = agg.setdefault(k, [0, 0.0]); v[0] += 1
        try: v[1] += float(re.sub(r"[,$\s]", "", str(r["grant_amount"])) or 0)
        except ValueError: pass
    rank = {"new_mint": 0, "review": 1, "matched": 2, "NCI": 3, "NA": 4}
    rows = []
    for g in seen.values():
        cid = resmap.get(g["key"], ""); info = dim["info"].get(cid, {})
        dispo = ("NA" if cid == "NA" else "NCI" if cid == "NCI" else "review" if cid == ""
                 else "new_mint" if cid in new_cids else "matched")
        n, tot = agg.get(g["key"], [0, 0.0])
        rows.append([g["grantee"], g["city"], g["state"], g["zip"], g["country"], dispo,
                     dispo == "new_mint", cid, info.get("name", ""), info.get("city", ""),
                     info.get("ipeds", ""), info.get("class", ""), n, round(tot, 2), "", ""])
    rows.sort(key=lambda x: (rank.get(x[5], 9), -x[13]))
    with open(_p(a.work_dir, "match_audit", a.stamp), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(AUDIT_COLS); w.writerows(rows)
    nmint = sum(1 for x in rows if x[6]); nrev = sum(1 for x in rows if x[5] == "review")
    print(f"[match] audit -> match_audit_{a.stamp}.csv ({len(rows):,} grantees | {nmint} new mints, "
          f"{nrev} review) -- fill correct_ipeds_unitid to write back", flush=True)

def stage_tag(a):
    src = _p(a.work_dir, "_matched", a.stamp)
    if not os.path.exists(src): sys.exit(f"[tag] matched file missing: {src}")
    # keep_default_na=False: canonical_id legitimately holds the literal "NA" for
    # excluded grantees -- pandas' default sniffing would silently blank it.
    d = pd.read_csv(src, dtype=str, keep_default_na=False)
    cl = Anthropic()
    tags = tag_purposes(list(d["grant_purpose"]), cl, use_batch=not a.sync) if len(d) else {}
    out = []
    for _, r in d.iterrows():
        gm = " ".join([str(r["grantee"]), str(r["grantee_addr1"]), str(r["grantee_addr2"])]).lower()
        gso = "foundation" in str(r["grantee"]).lower()          # grantee_support_org
        out.append([r["xml_file"], r["filer_name"], r["filer_ein"], r["return_year"], r["grantee"],
            r["grantee_addr1"], r["grantee_addr2"], r["grantee_city"], r["grantee_state"], r["grantee_zip"],
            r["grantee_country"], r["grantee_addr_type"], r["grant_amount"], r["grant_purpose"],
            r["ipeds_unitid"], "", gm, r["form_type"], r["canonical_id"],   # uid BLANK -> SQL assigns
            tags.get(qnorm(r["grant_purpose"]), "other"), gso])
    # upload deliverable: trimmed/reordered to GRANTSDB_UPLOAD_COLS
    idx = [GRANTSDB_COLS.index(c) for c in GRANTSDB_UPLOAD_COLS]
    out_upload = [[row[i] for i in idx] for row in out]
    with open(_p(a.work_dir, "grantsdb_incremental", a.stamp), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(GRANTSDB_UPLOAD_COLS); w.writerows(out_upload)
    print(f"[tag] {len(out)} grant rows | uid left blank (SQL auto-assigns) | grantee_support_org=True on "
          f"{sum(1 for x in out if x[20])} -> grantsdb_incremental_{a.stamp}.csv", flush=True)

# approx per-year in-scope volume (index filing-year histogram, CLAUDE.md sec.8);
# used ONLY to balance year-shards -- not correctness-critical.
_YEAR_WEIGHTS = {
    "2011": 210, "2012": 251, "2013": 307, "2014": 342, "2015": 379,
    "2016": 410, "2017": 429, "2018": 456, "2019": 477, "2020": 504,
    "2021": 618, "2022": 695, "2023": 706, "2024": 719, "2025": 691, "2026": 147,
}

def _partition_years(n):
    """Greedy longest-processing-time split of the known heavy years into n
    balanced buckets. bucket[0] additionally absorbs EVERY other year (pre-2011,
    unknown, future) via --years-exclude, so coverage has no gaps."""
    buckets = [[] for _ in range(n)]; load = [0.0] * n
    for y in sorted(_YEAR_WEIGHTS, key=lambda k: -_YEAR_WEIGHTS[k]):
        i = min(range(n), key=lambda j: load[j])
        buckets[i].append(y); load[i] += _YEAR_WEIGHTS[y]
    return buckets

def stage_parallel_years(a):
    """Fan download+parse out into N processes split by tax year, each writing
    its OWN record file, then merge grant rows + records for a single match+tag."""
    n = min(a.parallel_years, len(_YEAR_WEIGHTS))
    idx = resolve_index(a)                                  # resolve ONCE; children reuse
    buckets = _partition_years(n)
    excl = sorted(set().union(*buckets[1:])) if n > 1 else []
    print(f"[parallel] {n} year-shards, {a.workers} workers each "
          f"({n * a.workers} connections, {n} parse cores):", flush=True)
    for k, b in enumerate(buckets):
        print(f"    shard y{k}: {'ALL OTHER + ' if k == 0 else ''}{','.join(b) or '(none)'}", flush=True)
    procs = []
    for k, b in enumerate(buckets):
        if k != 0 and not b: continue                       # empty bucket -> skip
        rec = _p(a.work_dir, "_yearshard_rec", f"{a.stamp}_{k}")
        cmd = [sys.executable, os.path.abspath(__file__),
               "--steps", "download,parse", "--stamp", f"{a.stamp}_y{k}",
               "--data-dir", a.data_dir, "--work-dir", a.work_dir,
               "--existing", a.existing, "--append-file", rec,
               "--index-file", idx, "--year-field", a.year_field,
               "--workers", str(a.workers), "--shard-label", f"y{k}",
               "--limit-990", str(a.limit_990), "--limit-990pf", str(a.limit_990pf)]
        if a.overwrite: cmd.append("--overwrite")
        if a.keep_xml: cmd.append("--keep-xml")
        if k == 0:
            if excl: cmd += ["--years-exclude", *excl]      # catch-all shard
        else:
            cmd += ["--years", *b]
        procs.append((k, subprocess.Popen(cmd)))            # inherit stdout -> live heartbeats
    rc = 0
    for k, p in procs:
        p.wait()
        print(f"[parallel] shard y{k} exited rc={p.returncode}", flush=True)
        rc = rc or p.returncode
    if rc: sys.exit(f"[parallel] a shard failed (rc={rc}) -- not merging")
    # 1) merge per-shard grant rows -> the single _parsed_{stamp}.csv match reads
    merged = _p(a.work_dir, "_parsed", a.stamp); nrows = 0
    with open(merged, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(PARSED_COLS)
        for k, _ in procs:
            sp = _p(a.work_dir, "_parsed", f"{a.stamp}_y{k}")
            if not os.path.exists(sp): continue
            for row in list(csv.reader(open(sp, encoding="utf-8-sig")))[1:]:
                w.writerow(row); nrows += 1
    # 2) merge per-shard record files -> (a) append to the real skip list AND
    #    (b) emit ONE consolidated ref_xml_processed_incremental_{stamp}.csv (the
    #    SQL-load file), matching the single-process three-increment contract.
    seen = load_existing(a.existing); merged_rows = []
    for k, _ in procs:
        rec = _p(a.work_dir, "_yearshard_rec", f"{a.stamp}_{k}")
        if not os.path.exists(rec): continue
        for row in list(csv.reader(open(rec, encoding="utf-8")))[1:]:
            if row and row[0] not in seen:
                seen.add(row[0]); merged_rows.append(row)
    hdr = not os.path.exists(a.existing) or os.path.getsize(a.existing) == 0
    with open(a.existing, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if hdr: w.writerow(REF_COLS)
        w.writerows(merged_rows)
    # upload deliverable: xml_file,timestamp,form_type,filer_name,filer_ein,return_year
    upload_rows = [[r[0], r[1], r[2], r[3], r[4], r[6], _bool_str(r[5])] for r in merged_rows]
    with open(_p(a.work_dir, "ref_xml_processed_incremental", a.stamp), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(REF_UPLOAD_COLS); w.writerows(upload_rows)
    # 3) drop per-shard fragments now that both consolidated files exist
    for k, _ in procs:
        for frag in (_p(a.work_dir, "_parsed", f"{a.stamp}_y{k}"),
                     _p(a.work_dir, "_yearshard_rec", f"{a.stamp}_{k}"),
                     _p(a.work_dir, "ref_xml_processed_incremental", f"{a.stamp}_y{k}")):
            try: os.remove(frag)
            except OSError: pass
    print(f"[parallel] merged {nrows:,} grant rows -> _parsed_{a.stamp}.csv | "
          f"{len(merged_rows):,} new records -> {os.path.basename(a.existing)} "
          f"+ ref_xml_processed_incremental_{a.stamp}.csv | shard fragments cleaned", flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="download,parse,match,tag")
    ap.add_argument("--stamp", default=time.strftime("%Y%m%d"))
    ap.add_argument("--data-dir", default=SCRIPT_DIR)
    ap.add_argument("--work-dir", default=os.path.join(SCRIPT_DIR, "incremental"))
    # the TWO data dependencies (both live beside the script by default):
    ap.add_argument("--dim", default=os.path.join(SCRIPT_DIR, "dim_institution.csv"))
    ap.add_argument("--existing", default=os.path.join(SCRIPT_DIR, "ref_xml_processed.csv"))
    ap.add_argument("--year-field", default="tax"); ap.add_argument("--years", nargs="*", default=None)
    ap.add_argument("--years-exclude", nargs="*", default=None,
                    help="download every in-scope year EXCEPT these (catch-all shard)")
    ap.add_argument("--append-file", default=None,
                    help="record processed filenames here instead of --existing "
                         "(so parallel shards never write the same file)")
    ap.add_argument("--shard-label", default="",
                    help="short tag prefixed to this process's heartbeat lines")
    ap.add_argument("--parallel-years", type=int, default=1,
                    help="fan download+parse out into N processes split by tax year, "
                         "then merge and run match+tag once")
    ap.add_argument("--limit-990", type=int, default=0); ap.add_argument("--limit-990pf", type=int, default=0)
    ap.add_argument("--workers", type=int, default=24); ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--keep-xml", action="store_true")
    ap.add_argument("--index-file", default=None); ap.add_argument("--index-key", default=None)
    ap.add_argument("--refresh-index", action="store_true")
    ap.add_argument("--sync", action="store_true",
                    help="use synchronous API calls instead of the (cheaper, slower) Batch API")
    a = ap.parse_args()
    os.makedirs(a.work_dir, exist_ok=True)
    steps = [s.strip() for s in a.steps.split(",") if s.strip()]
    if any(s in steps for s in ("match", "tag")) and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: match/tag stages need ANTHROPIC_API_KEY")
    t0 = time.time()
    if a.parallel_years > 1 and "download" in steps and "parse" in steps:
        stage_parallel_years(a)          # fan out download+parse by year across processes
    elif "download" in steps and "parse" in steps:
        stage_acquire(a)                 # FUSED: parse overlaps download (efficient, no disk hoard)
    elif "download" in steps:
        stage_download(a)                # download-only: fetch all to a manifest for a later parse
    elif "parse" in steps:
        stage_parse(a)                   # parse-only: from a prior download's manifest
    if "match" in steps: stage_match(a)
    if "tag"   in steps: stage_tag(a)
    print(f"DONE steps={steps} | t={time.time()-t0:.0f}s")

if __name__ == "__main__":
    main()
