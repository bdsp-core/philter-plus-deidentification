import re, sys, json, os, glob, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[0])))
from med_probes import matches
notesdir, deiddir, knownf = sys.argv[1:4]
known = json.load(open(knownf))
DATE=re.compile(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b')
def present(tok,t): return re.search(r'\b'+re.escape(tok)+r'\b',t) is not None
fn_name=fn_date=phi_total=date_total=0
percat=collections.defaultdict(lambda:[0,0])  # label -> [destroyed, total]
for path in sorted(glob.glob(os.path.join(notesdir,'*.txt'))):
    b=os.path.basename(path); orig=open(path,errors='replace').read()
    dp=os.path.join(deiddir,b)
    if not os.path.exists(dp): continue
    deid=open(dp,errors='replace').read()
    for tok in known.get(b,{}).get('phi_names',[]):
        phi_total+=1
        if present(tok,deid): fn_name+=1
    for m in set(DATE.findall(orig)):
        date_total+=1
        if m in deid: fn_date+=1
    for label,val in matches(orig):
        percat[label][1]+=1
        if val not in deid: percat[label][0]+=1
tot_d=sum(v[0] for v in percat.values()); tot_t=sum(v[1] for v in percat.values())
print(f"FN name-leaks {fn_name}/{phi_total} | FN date-leaks {fn_date}/{date_total}")
print(f"FP MEDICAL destroyed: {tot_d}/{tot_t} ({100*tot_d/max(tot_t,1):.1f}%)")
for label in sorted(percat, key=lambda k:-percat[k][0]):
    d,t=percat[label]
    if t: print(f"   {label:11s} {d:4d}/{t:<4d} destroyed" + ("  <== " if d else ""))
