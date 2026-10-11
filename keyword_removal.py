import re
import hmac, hashlib, os

# Deterministic FAKE institution names (hiding in plain sight, like the other surrogates), so a scrubbed
# site name reads as a plausible hospital instead of "*****". Same real name -> same fake (keyed HMAC).
_SALT = os.environ.get("SURROGATE_SALT", "philter-surrogate-v1").encode()
_SITE_PLACE = ("Fairview Riverside Lakeside Summit Cedar Hillcrest Brookfield Oakmont Parkview Westgate "
               "Northshore Southridge Maplewood Sunrise Greenfield Pinecrest Bayside Ridgemont Clearwater "
               "Stonebridge Meadowbrook Glenwood Harborview Kingston Auburn").split()
_SITE_TYPE = ["Hospital", "Medical Center", "Health System", "Regional Medical Center",
              "Community Hospital", "Health Care", "Clinic", "Memorial Hospital"]


def _fake_site(matched):
    h = hmac.new(_SALT, ("site:" + matched.lower().strip()).encode("utf-8", "ignore"), hashlib.sha256).digest()
    place = _SITE_PLACE[int.from_bytes(h[:4], "big") % len(_SITE_PLACE)]
    typ = _SITE_TYPE[int.from_bytes(h[4:8], "big") % len(_SITE_TYPE)]
    return f"{place} {typ}"



# ---------------------------------------------------------------------------------------------
# Three tiers, because one flat `re.escape` + IGNORECASE list has two opposite defects at once.
#
# PRECISION. IGNORECASE on a short acronym replaces ordinary words with plausible hospital names,
# and plausibly enough that no downstream reader can tell it happened:
#     "bi-frontal atrophy"               -> "Bayside Community Hospital-frontal atrophy"
#     "confirmed SCC of the left tonsil" -> "confirmed Kingston Hospital of the left tonsil"
# Measured by the BIND imaging session on 108,155 identified reports: `BI` matched 69 times with
# 60 NOT written "BI" (a bi/Bi standing alone), and `SCC` 54 times across 47 reports, 12 of which
# also speak of a carcinoma or a semicircular canal. This is data corruption, not a privacy
# defect -- it destroys a cancer diagnosis -- and in clinical notes both are far more frequent
# than in imaging reports.
#
# RECALL. `re.escape` turns a pattern into a literal, so an entry written as a regex matches only
# its own source text, which never occurs. "Mass General Hospital" matched NOTHING and survived
# unredacted, as did the curly-apostrophe "Brigham and Women’s".
#
# Both followed from one line. The list stays the authority on WHICH names are sites; these sets
# say only HOW each is matched.
# ---------------------------------------------------------------------------------------------

# Matched case-SENSITIVELY. Short acronyms that collide with ordinary or medical words under
# IGNORECASE. Clinicians write these upper-case essentially always, so the recall cost is slight
# and the precision gain is large.
_CASE_SENSITIVE = {
    # ONLY the entries that genuinely collide with a word or a name. Case-sensitivity costs recall
    # -- a note writing "bidmc" in lower case would then survive -- so it is spent only where
    # IGNORECASE actually does damage. MGH, BWH, BIDMC, NSMC and the rest are not English words and
    # are left case-insensitive deliberately; widening this set would trade real recall for nothing.
    "BI",        # "bi" of bi-frontal, bi-parietal, bi-hemispheric
    "Mayo",      # "mayo" in a diet or intake note
    "GREGoR",    # "Gregor", a person's name
    "SCC",       # squamous cell carcinoma / semicircular canal -- also vetoed below
    "MEE",       # middle ear effusion -- also vetoed below
}

# A site match is VETOED when one of these words appears near it, because the abbreviation has a
# medical meaning that is far more likely in a note. SCC is squamous cell carcinoma and the
# semicircular canal; MEE is a middle ear effusion.
_VETO_NEAR = {
    "SCC": ("squamous", "carcinoma", "semicircular", "canal", "cell", "tonsil", "dehiscen",
            "cochlea", "vestibul"),
    "MEE": ("middle ear", "effusion", "tympan", "otitis"),
}
_VETO_WINDOW = 60

# Entries written as patterns, kept AS PATTERNS rather than escaped. Only these four, listed
# explicitly -- un-escaping the whole 254-entry list would silently reinterpret it as regex, a
# worse failure mode than the one being fixed.
_REGEX_SITES = [
    r"Brigham and Women[’']?s",
    r"Brigham And Women'?s/Mass(achusetts)? General Health Care Center",
    r"Mass(achusetts)? General Hospital",
    r"Brigham and Women\s+Hospital",
]

# Dropped rather than revived, each for a reason.
#   The four date patterns and the EPOCH pattern are dead today, and reviving them would run
#   _fake_site over a DATE -- "5 Jan 2019" would become "Kingston Hospital". A date is not a site
#   name, and philter2 handles dates properly as of commits 6b70248/48a313a, taking written-out and
#   hyphenated dates whole and keeping their precision. Reviving these would be a new defect.
#   The two pre-escaped \b forms are exact duplicates of the plain "BCH"/"CHB" entries below.
_DROP = {
    "EPOCH\s\d{1,2}:\s?(\d{4}|\d{1,2}/\d{1,2})\s\d{1,2}:\d{1,2}\s?to\s?(\d{2,4}|\d{1,2}/\d{1,2})\s\d{1,2}:\d{1,2}",
    "\d{1,2}\s?(jan|feb|mar|apr|may|june|jun|july|jul|aug|sept|sep|oct|nov|dec)\.?\s+?\d{2,4}",
    "\d{1,2}\s?(Jan|Feb|Mar|Apr|May|June|Jun|July|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?\s+?\d{2,4}",
    "\d{1,2}\s?(january|february|march|april|may|june|july|august|september|october|november|december)\s+?\d{2,4}",
    "\d{1,2}\s?(January|February|March|April|May|June|July|August|September|October|November|December)\s+?\d{2,4}",
    "\bBCH\b",
    "\bCHB\b",
    "Brigham and Women\\s Hospital",
    "Brigham and Women\u2019?s",
    "Brigham And Women'?s/Mass(achusetts)? General Health Care Center",
    "Mass(achusetts)? General Hospital",
}


def _vetoed(kw, text, start, end):
    """True when a medical reading of this abbreviation is the likely one here."""
    words = _VETO_NEAR.get(kw)
    if not words:
        return False
    ctx = text[max(0, start - _VETO_WINDOW):end + _VETO_WINDOW].lower()
    return any(w in ctx for w in words)


def _build(site_list):
    """(compiled, keyword) pairs. Short case-sensitive acronyms also refuse a match touching a
    hyphen, which is what let \bBI\b eat the "bi" of "bi-frontal": \b treats - as a boundary."""
    out = []
    for kw in site_list:
        if kw in _DROP:
            continue
        cs = kw in _CASE_SENSITIVE
        flags = re.DOTALL if cs else (re.IGNORECASE | re.DOTALL)
        if cs and len(kw) <= 4:
            pat = rf'(?<![\w-]){re.escape(kw)}(?![\w-])'
        else:
            pat = rf'\b{re.escape(kw)}\b'
        out.append((re.compile(pat, flags=flags), kw))
    for rx in _REGEX_SITES:
        out.append((re.compile(rx, flags=re.IGNORECASE | re.DOTALL), None))
    return out


def remove_keywords(text):
    _sites = [
        "EPOCH\s\d{1,2}:\s?(\d{4}|\d{1,2}/\d{1,2})\s\d{1,2}:\d{1,2}\s?to\s?(\d{2,4}|\d{1,2}/\d{1,2})\s\d{1,2}:\d{1,2}",
        "Brigham and Women’?s",
        "Brigham And Women'?s/Mass(achusetts)? General Health Care Center",
        "Brigham and Women\\s Hospital",
        "Brigham & Women's Hospital",
        "Brigham and Women's Hospital",
        "Brigham and Women's Hosptial",
        "Brigham and Womens",
        "Brigham and Womens Hospital",
        "Brigham and Women's Hosptial",
        "Brigham and Women's Faulkner Hospital",
        "Brigham And Womens Primary Care",
        "Mass(achusetts)? General Hospital",
        "Massachusetts General Hospital",
        "Lawrence General Hospital",
        "Massachusetts General",
        "Mass General Brigham",
        "South Shore Health Systems",
        "South Shore Health",
        "South Shore Hospital",
        "A Teaching Affiliate of Harvard Medical School",
        "Harvard Medical School",
        "HMS",
        "Partners Neurology Resident",
        "Partners Compliance",
        "MGH",
        "BWH",
        "MGB",
        "\d{1,2}\s?(jan|feb|mar|apr|may|june|jun|july|jul|aug|sept|sep|oct|nov|dec)\.?\s+?\d{2,4}",
        "\d{1,2}\s?(Jan|Feb|Mar|Apr|May|June|Jun|July|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?\s+?\d{2,4}",
        "\d{1,2}\s?(january|february|march|april|may|june|july|august|september|october|november|december)\s+?\d{2,4}",
        "\d{1,2}\s?(January|February|March|April|May|June|July|August|September|October|November|December)\s+?\d{2,4}",
        "medical school",
        "SALEM HOSPITAL",
        "Newton-Wellesley Hospital",
        "Newton Wellesley Hospital",
        "NWH",
        "Union Hospital",
        "McLean Hospital",
        "Cooley Dickinson Hospital",
        "Cooley Dickinson",
        "North Shore Medical Center",
        "Wentworth Douglas Hospital",
        "Partners Healthcare",
        "Milford Regional Medical Ctr",
        "MILFORD REGIONAL MEDICAL CENTER",
        "BWF",
        "NCH",
        "Nantucket Cottage Hospital",
        "NSMC",
        "DFCI",
        "Dana-Farber Cancer Institute",
        "Dana Farber Cancer Institute",
        "Dana Farber Cancer Institiute",
        "Harbor Medical Associates",
        "HMA",
        "Wentworth-Douglass Hospital",
        "WDH",
        "Spaulding Rehabilitation Hospital",
        "SRH",
        "Mass Eye and Ear",
        "MASSACHUSETTS EYE & EAR",
        "MASSACHUSETTS EYE AND EAR",
        "MASSACHUSETTS EYE AND EAR INFIRMARY",
        "MEE",
        "MEEI",
        "Martha's Vineyard Hospital",
        "MVH",
        "Spaulding Hospital",
        "SCC",
        "SHAUGHNESSY-KAPLAN REHABILITATION HOSPITAL",
        "F A U L K N E R   H O S P I T A L",
        "FAULKNER HOSPITAL",
        "Brigham and Women's Faulkner Hospital",
        "BRIGHAM INTERNAL MEDICINE ASSOCIATES",
        "BRIGHAM INTERNAL MEDICINE",
        "BRIGHAM AND WOMEN'S PHYSICIAN GROUP",
        "ROBERT B. BRIGHAM ARTHRITIS CENTER",
        "BRIGHAM MEDICAL GROUP",
        "BRIGHAM ORTHOPEDIC ASSOCIATES",
        "BRIGHAM EMERGENCY DEPT VISIT",
        "GENERAL MEDICAL ASSOCIATES",
        "WOMEN'S HEALTH CENTER",
        "NORWOOD HEALTHCARE ASSOCIATES",
        "SOUTHERN JAMAICA PLAIN HEALTH CENTER",
        "Union Hospital",
        "Beverly Hospital",
        "Addison Gilbert Hospital",
        "TUFTS MEDICAL CENTER",
        "St Luke's Hospital",
        "MaineHealth's",
        "Sandra",
        "Samartian",
        "Renagel",
        "Orlando",
        "obrien",
        "NYU",
        "Mooremary",
        "Mayo",
        "kuwaiti",
        "fra ncisco",
        "Francisco",
        "Framingham",
        "Fogarty",
        "Crawford",
        "North Shore Physicians Group",
        "BIDMC",
        "Affiliation: BIDMC",
        "Bidmc-Medicine",
        "BIDHC",
        "Bilhpc",
        "Lahey Danvers",
        "BI",
        "BID#",
        "BID#:",
        "BID #",
        "BID #:",
        "BIH#",
        "BIH#:",
        "BIH #:",
        "BMC",
        "Lahey",
        "BETH ISAREL",
        "Beth Israel Deaconess Medical Center",
        "Beth Isarel Deaconess Healthcare - Washington Square Group",
        "Beth-Israel Deaconess HealthCare-Lexington",
        "Beth-Israel Deaconess",
        "deaconess healthcare",
        "Lahey Health",
        "Lahey Outpatient Center Danvers",
        "BETH ISAREL LAHEY HEALTH",
        "Beth Israel Deaconess Medical Center/East",
        "Uphams Corner Health Center",
        "Hebrew Senior Life",
        "Nashua Eye Associates",
        "330 Brookline Avenue, E/CC-6",
        "330 Brookline Ave",
        "East Campus",
        "3rd floor Shapiro",
        "MR#",
        "MR#:",
        "mr#",
        "MRN",
        "SHAPIRO CLINICAL CENTER",
        "Joslin Diabetes Center",
        "Merrimack Valley Internal Medicine Associates",
        "UMASS BOSTON",
        "Puya",
        "Yvo",
        "Boston Children's Hospital",
        "Boston Childrens Hospital",
        "Boston Children’s Hospital",
        "Childrens Hospital",
        "Children's Hospital",
        "Children’s Hospital",
        "Children's",
        "Children’s",
        "Childrens Hospital",
        "Children’s Hospital",
        "children's hospital",
        "childrens hosp",
        "children's hosp",
        "children’s hosp",
        "CHILDREN'S HOSPITAL BOSTON",
        "BCH",
        ":BCH",
        "\bBCH\b",
        "General Instructions BCH",
        "CHB",
        "\bCHB\b",
        "300 Longwood Avenue",
        "300 Longwood Ave",
        "www.BostonChildrens.org",
        "www.childrenshospital.org",
        "www.mychildrens.org",
        "Boston Children's Hospital",
        "St. Elizabeth's",
        "St. Elizabeth's Medical Center",
        "Martha Eliot Health Center",
        "New England Pediatric Care Center",
        "New England Pediatric Care",
        "78 Boston Road",
        "NEPC",
        "CHILDREN'S HOSPITAL BOSTON",
        "Elliot Hospital",
        "Floating Hospital",
        "MyChildren's Portal",
        "MyChildrens",
        "South Shore Hospital",
        "Boston Childrens Physicians",
        "Cambridge Hospital",
        "Cambridge Health Alliance",
        "Boston Children's Waltham",
        "MassGeneral Hospital for Children",
        "Lowell General Hospital",
        "Johns Hopkins Hospital",
        "The Johns Hopkins Hospital",
        "Alameda Hospital",
        "UCSF",
        "University of California, San Francisco",
        "Stanford Hospital",
        "Stanford Neuroscience Health Center",
        "Stanford Center for Autonomic Disorders",
        "Stanford Neuroscience",
        "Quarry Rd",
        "Quarry Road",
        "Stanford Hospital and Clinics",
        "Menlo Medical Clinic",
        "www.menloclinic.com",
        "Bayside Medical Group",
        "Stanford Children’s Health",
        "Stanford University",
        "Stanford Health Care",
        "Stanford Healthcare",
        "Stanford Healthcare TriValley",
        "Stanford University School of Medicine",
        "Diablo Valley Child Neurology",
        "Shield Healthcare",
        "https://gregor.stanford.edu/",
        "GREGoR",
        "https://evercore.stanfordmed.org/",
        "STANFORD MEDICINE",
        "http://prism.stanfordmedicine.org",
        "Stanford Pulmonary Lab",
        "Stanford University Medical Center",
        "Stanford Family Practice",
        "Stanford Hospital and Clinics",
        "UCSF Stanford Health Care",
        "STANFORD MEDICAL GROUP",
        "STANFORD MEDICAL CENTER",
        "LUCILE SALTER PACKARD",
        "LUCILE SALTER PACKARD CHILDREN'S HOSPITAL",
        "Stanford Hematology",
        "ACCESSION NUMBER:",
        "ACCESSION #:",
        "Accession#:",
        "Carol Ann Read Breast Health Center",
        "John Muir Health",
        "Stanford Center for Inherited Cardiovascular Disease",
        "Palo Alto Medical Foundation",
        "Stanford Sleep Medicine Center",
        "Subject Code:",
        "Hospital # :",
        "MEDICAL RECORD #:",
        "Packard's Children's Health Alliance",
        "Sutter Pacific Medical Center",
        "Stanford Pain Management Center",
        "Lucile Packard Children's Hospital",
        "Palo Alto Medical Clinic",
        "UNIVERSITY OF CALIFORNIA SAN FRANCISCO ",
        "El Camino Hospital",
        "Mount Sinai Medical Center",
        "Mayo Clinic",
        "Cleveland Clinic",
    ]



    for pattern, kw in _build(_sites):
        def _sub(m, kw=kw):
            if _vetoed(kw, m.string, m.start(), m.end()):
                return m.group(0)                                    # a medical term, left alone
            return _fake_site(m.group(0))                            # fake site name, not "*****"
        text = pattern.sub(_sub, text)

    return text


# Example usage:
'''
paragraph = ""
result = remove_keywords(paragraph)
print("Deidentified Result:\n", result)
'''
