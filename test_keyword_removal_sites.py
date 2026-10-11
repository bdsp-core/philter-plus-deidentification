#!/usr/bin/env python3
"""Regression tests for the site list's two defects.

Each PRECISION case is a real clinical phrase that the flat `re.escape` + IGNORECASE list turned
into a plausible hospital name -- measured by the BIND imaging session on 108,155 identified
reports and reproduced here. Each RECALL case is a real institution name the list was written to
catch and silently did not, because `re.escape` turned its pattern into a literal.

loom/deid_bench.py passes 35/35 both before and after, so it covers none of this.
"""
import re
import sys

import keyword_removal as kr

FAKE = re.compile(r"(" + "|".join(kr._SITE_PLACE) + r")\s+("
                  + "|".join(t.replace(" ", r"\s") for t in kr._SITE_TYPE) + r")")


def scrubbed(t):
    return kr.remove_keywords(t)


def has_fake(t):
    return bool(FAKE.search(t))


PRECISION = [
    # (text, the substring that must survive untouched)
    ("There is bi-frontal atrophy without acute infarct.", "bi-frontal"),
    ("Bi-parietal thinning is noted.", "Bi-parietal"),
    ("Biopsy confirmed SCC of the left tonsil.", "SCC"),
    ("The superior SCC is dehiscent on the right.", "SCC"),
    ("Squamous cell carcinoma (SCC) was identified.", "SCC"),
    ("A middle ear effusion (MEE) is present.", "MEE"),
    ("The patient declined mayo on the sandwich.", "mayo"),
    ("Discussed with Dr. Gregor at length.", "Gregor"),
    ("bilateral bi-hemispheric slowing", "bi-hemispheric"),
]

RECALL = [
    # (text, the institution wording that must NOT survive)
    ("The patient was transferred from Mass General Hospital yesterday.", "Mass General Hospital"),
    ("Seen at Massachusetts General Hospital.", "Massachusetts General Hospital"),
    ("Referred by Brigham and Women’s.", "Brigham and Women’s"),
    ("Referred by Brigham and Women's.", "Brigham and Women's"),
    ("Record from BIDMC neurology.", "BIDMC"),
    ("Transferred from MGH overnight.", "MGH"),
    ("Followed at BCH since infancy.", "BCH"),
    ("Imaging at Mayo Clinic in 2019.", "Mayo Clinic"),
]

DATES_UNTOUCHED = [
    # Reviving the four dead date patterns would have run _fake_site over a DATE. A date is not a
    # site name, and philter2 handles dates properly as of 6b70248/48a313a.
    "Study performed 5 Jan 2019 at 14:00.",
    "Follow-up 12 March 2020 as scheduled.",
    "EPOCH 3: 1430 12:00 to 1445 12:30",
]

fails = []
print("  PRECISION — a clinical term must not become a hospital")
for text, keep in PRECISION:
    out = scrubbed(text)
    ok = keep in out and not has_fake(out)
    print(f"    {'PASS' if ok else 'FAIL'}  {text}")
    if not ok:
        print(f"           -> {out}")
        fails.append(("precision", text, out))

print("\n  RECALL — a real institution name must not survive")
for text, gone in RECALL:
    out = scrubbed(text)
    ok = gone not in out and has_fake(out)
    print(f"    {'PASS' if ok else 'FAIL'}  {text}")
    if not ok:
        print(f"           -> {out}")
        fails.append(("recall", text, out))

print("\n  DATES — must pass through this mechanism untouched")
for text in DATES_UNTOUCHED:
    out = scrubbed(text)
    ok = not has_fake(out)
    print(f"    {'PASS' if ok else 'FAIL'}  {text}")
    if not ok:
        print(f"           -> {out}")
        fails.append(("date", text, out))

print("\n  DETERMINISM — the same name must give the same fake name")
a, b = scrubbed("Seen at Massachusetts General Hospital."), scrubbed("At Massachusetts General Hospital again.")
fa, fb = FAKE.search(a), FAKE.search(b)
ok = fa and fb and fa.group(0) == fb.group(0)
print(f"    {'PASS' if ok else 'FAIL'}  same wording -> same fake name")
if not ok:
    fails.append(("determinism", a, b))

n = len(PRECISION) + len(RECALL) + len(DATES_UNTOUCHED) + 1
print(f"\n  {n - len(fails)}/{n} passed")
sys.exit(1 if fails else 0)
