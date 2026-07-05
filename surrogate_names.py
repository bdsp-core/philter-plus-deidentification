"""Deterministic, gender-preserving fake-name surrogates for the `surrogate` output format.

Same real name -> same fake name (keyed HMAC into large realistic lists, no stored table, not
reversible without the salt). First names map to a SAME-GENDER fake first name; surnames to a fake
surname; case / initials / separators are preserved so "White, Neal J." -> "Baker, Aaron K.".

Sources (offline, no external calls): nltk `names` corpus (gendered first names + gender lookup) and
Philter's own `filters/blacklists/lastnames_minus_fps.json` (161k surnames).
"""
from __future__ import annotations
import hmac, hashlib, os, json, re

SALT = os.environ.get("SURROGATE_SALT", "philter-surrogate-v1").encode()
_S = {}


def _load():
    if _S:
        return _S
    from nltk.corpus import names
    male = sorted({n for n in names.words("male.txt")})
    female = sorted({n for n in names.words("female.txt")})
    _S["male"], _S["female"] = male, female
    _S["male_set"] = {n.lower() for n in male}
    _S["female_set"] = {n.lower() for n in female}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "filters/blacklists/lastnames_minus_fps.json")
    try:
        data = json.load(open(path))
        raw = list(data.keys()) if isinstance(data, dict) else list(data)
        last = sorted({s.capitalize() for s in raw if isinstance(s, str) and s.isalpha() and len(s) > 2})
    except Exception:
        last = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis"]
    _S["last"] = last
    return _S


def _idx(token: str, n: int) -> int:
    h = hmac.new(SALT, token.lower().encode("utf-8", "ignore"), hashlib.sha256).digest()
    return int.from_bytes(h[:8], "big") % max(n, 1)


def _match_case(src: str, fake: str) -> str:
    if src.isupper():
        return fake.upper()
    if src[:1].isupper():
        return fake[:1].upper() + fake[1:].lower()
    return fake.lower()


def _fake_token(token: str, gender_hint=None) -> str:
    S = _load()
    if len(token) == 1:                                    # single letter (initial / list label) -> keep as-is
        return token                                       # surrogating adds no de-id value and mangles "A." "B." markers
    low = token.lower()
    is_first = low in S["male_set"] or low in S["female_set"]
    if is_first:
        g = gender_hint
        if g not in ("M", "F"):
            if low in S["female_set"] and low not in S["male_set"]:
                g = "F"
            elif low in S["male_set"] and low not in S["female_set"]:
                g = "M"
            else:
                g = "F" if _idx("g:" + low, 2) else "M"    # ambiguous/unknown -> stable coin flip
        pool = S["female"] if g == "F" else S["male"]
    else:
        pool = S["last"]
    idx = _idx(token, len(pool))
    fake = pool[idx]
    if fake.lower() == low:                                # never map a name to ITSELF (the input name is
        fake = pool[(idx + 1) % len(pool)]                 # in the pool, so ~1/N of the time it collides)
    return _match_case(token, fake)


def fake_name_span(span: str, prefix: str = "") -> str:
    """Replace a Philter NAME span with a surrogate, token by token, keeping separators/format.
    `prefix` = a little preceding text used to infer gender from a courtesy title."""
    gh = None
    if re.search(r"(?i)\bMrs\.?\s*$|\bMs\.?\s*$|\bMiss\s*$", prefix):
        gh = "F"
    elif re.search(r"(?i)\bMr\.?\s*$", prefix):
        gh = "M"
    parts = re.split(r"([^A-Za-z])", span)                 # keep the delimiters
    return "".join(_fake_token(p, gh) if p.isalpha() else p for p in parts)
