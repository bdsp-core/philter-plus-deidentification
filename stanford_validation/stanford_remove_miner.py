import re, sys, os, glob, collections
deiddir = sys.argv[1]
# site-identifying proper nouns / abbreviations (geographic + org names). NOT generic clinical depts.
SITE_MARKERS = re.compile(r'\b(Stanford|Lucile|Packard|LPCH|SHC|Palo\s?Alto|Menlo|El\s?Camino|Valleycare|Tri[- ]?Valley|Redwood City|Emeryville|Pleasanton|Hillview|Blake Wilbur|Boswell|Ford\b|300 Pasteur|Welch Rd|Quarry Rd)\b', re.I)
# generic medical specialties -> a "<specialty> Laboratory/Clinic/Center" is NOT a site identifier -> keep
SPECIALTY = set("""virology immunoperoxidase breast eye behavioral occupational pulmonary cardiology
neurology oncology radiology pathology hematology urology dermatology orthopedic orthopedics surgery
psychiatry gastroenterology nephrology endocrinology rheumatology infectious pediatric geriatric
primary internal family emergency dental vision sleep pain wound imaging genetics molecular clinical
microbiology chemistry cytology histology""".split())
FACILITY_TAIL = re.compile(r'\b((?:[A-Z][A-Za-z&.\-]+ ){1,4}(?:Hospital|Clinic|Center|Centre|Medical|Health|Healthcare|University|Institute|Foundation|Laboratory|Pavilion|Campus))\b')
site_hits=collections.Counter(); phrase_candidates=collections.Counter(); generic_kept=collections.Counter()
for path in glob.glob(os.path.join(deiddir,'*.txt')):
    t=open(path,errors='replace').read()
    for m in SITE_MARKERS.finditer(t): site_hits[m.group(1)]+=1
    for m in FACILITY_TAIL.finditer(t):
        ph=m.group(1).strip(); words=[w.lower() for w in ph.split()]
        if any(w in SPECIALTY for w in words):
            generic_kept[ph]+=1                         # generic dept -> KEEP (not PHI)
        elif SITE_MARKERS.search(ph):
            phrase_candidates[ph]+=1                     # site-identifying facility -> remove candidate
print("=== SITE-IDENTIFYING tokens (remove-list candidates) ===")
for k,c in site_hits.most_common(20): print(f"   {c:4d}  {k}")
print("=== site-identifying FACILITY phrases (remove-list candidates) ===")
for k,c in phrase_candidates.most_common(20): print(f"   {c:4d}  {k}")
print("=== generic clinical depts detected -> KEPT (NOT PHI, per Brandon): sample ===")
for k,c in list(generic_kept.most_common(12)): print(f"   {c:4d}  {k}")
