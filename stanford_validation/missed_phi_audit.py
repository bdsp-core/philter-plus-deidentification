import re, sys, json, os, glob, collections
notesdir, deiddir, knownf = sys.argv[1:4]
known = json.load(open(knownf))
HIGH = {  # high-confidence PHI that must not survive
 'phone_fax': re.compile(r'(?<!\d)(?:\(\d{3}\)\s?|\d{3}[-.\s])\d{3}[-.\s]\d{4}(?!\d)'),
 'email':     re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b'),
 'ssn':       re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
 'url':       re.compile(r'(?:https?://|www\.)\S{4,}', re.I),
 'mrn_longnum': re.compile(r'(?<![\d.])\d{7,10}(?![\d.])'),
}
# name residuals: only a LEAK if the surname token also appears verbatim in the INPUT note
NAME = re.compile(r'\b(?:Dr\.?\s+([A-Z][a-z]{2,})|([A-Z][a-z]{2,}),?\s+[A-Z][a-z]{2,},?\s*(?:M\.?D\.?|D\.?O\.?|R\.?N\.?|N\.?P\.?))')
COMMON={'The','And','For','With','Normal','Final','Report','History','Impression','Findings','Patient','Right','Left','Both','Blood','Mode','Study','Date','Exam','None','New','Old','Time'}
high=collections.Counter(); name_leaks=[]; name_surr=0; known_leak=0; known_tot=0; ex=collections.defaultdict(list)
for path in sorted(glob.glob(os.path.join(notesdir,'*.txt'))):
    b=os.path.basename(path); dp=os.path.join(deiddir,b)
    if not os.path.exists(dp): continue
    inp=open(path,errors='replace').read(); deid=open(dp,errors='replace').read()
    for lab,rx in HIGH.items():
        for m in rx.finditer(deid):
            high[lab]+=1
            if len(ex[lab])<4: ex[lab].append(m.group(0)[:40])
    for m in NAME.finditer(deid):
        tok = m.group(1) or m.group(2)
        if not tok or tok in COMMON: continue
        if re.search(r'\b'+re.escape(tok)+r'\b', inp):   # surname present in input -> real leak
            name_leaks.append((b, tok))
        else:
            name_surr += 1                                 # not in input -> a surrogate (fine)
    for t in known.get(b,{}).get('phi_names',[]):
        known_tot+=1
        if re.search(r'\b'+re.escape(t)+r'\b', deid): known_leak+=1
print("=== MISSED-PHI RECALL AUDIT (220 multi-type notes; input-verified) ===")
print(f"known patient/author NAME tokens leaked: {known_leak}/{known_tot}")
print(f"provider/name residuals: {name_surr} surrogates (ok) + {len(name_leaks)} REAL LEAKS (surname also in input)")
if name_leaks: print(f"   REAL NAME LEAKS: {name_leaks[:12]}")
print("high-confidence PHI residuals (should be 0):")
for lab in HIGH:
    print(f"   {lab:12s} {high[lab]:3d}   e.g. {ex[lab]}")
