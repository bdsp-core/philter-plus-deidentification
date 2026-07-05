"""Deterministic fake-place surrogates for LOCATION / TOWN spans (hiding-in-plain-sight, like names).

Same real place -> same fake place (keyed HMAC into generic city/street pools; not reversible without
the salt). Address structure and generic suffixes (Street, Ave, Drive, City...) are preserved; only the
distinctive place words are swapped, and numeric street numbers are perturbed deterministically.
"""
from __future__ import annotations
import hmac, hashlib, os, re

SALT = os.environ.get("SURROGATE_SALT", "philter-surrogate-v1").encode()

CITIES = ("Fairview Riverton Oakdale Lakewood Springdale Brookfield Clearwater Greenville Kingston "
          "Ashford Bridgeport Cedarville Maplewood Fern Creek Hartwell Millbrook Northgate Pinehurst "
          "Rockport Stonebridge Westfield Auburn Belmont Clifton Danbury Easton Glenwood Harmony Ivywood "
          "Kirkland Lambert Monroe Norwood Oakhaven Parkside Quincy Rutland Sheridan Thornton Utica "
          "Vernon Winslow Yorkville Amberton Briarcliff Crestwood Dover Elmhurst Foxboro Granby Hilltop").split()
STREETS = ("Oak Maple Birch Cedar Elm Pine Willow Aspen Chestnut Walnut Hawthorn Linden Sycamore Poplar "
           "Juniper Magnolia Dogwood Cypress Redwood Spruce Sunset Highland Meadow Prairie River Lakeview "
           "Ridgeway Fairmont Kingsley Berkshire Coventry Devonshire Essex Franklin Garrison Hampton").split()
SUFFIX = {"street","st","avenue","ave","road","rd","drive","dr","lane","ln","boulevard","blvd","way","court",
          "ct","circle","cir","place","pl","terrace","ter","parkway","pkwy","highway","hwy","suite","ste",
          "apt","unit","floor","fl","building","bldg","city","county","north","south","east","west","the",
          "of","and","po","box"}


def _idx(token: str, pool) -> str:
    h = hmac.new(SALT, ("place:" + token.lower()).encode("utf-8", "ignore"), hashlib.sha256).digest()
    return pool[int.from_bytes(h[:8], "big") % len(pool)]


def _match_case(src: str, fake: str) -> str:
    if src.isupper():
        return fake.upper()
    if src[:1].isupper():
        return fake[:1].upper() + fake[1:].lower()
    return fake.lower()



STATE_ABBR = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
STATE_NAME = ("Alabama Alaska Arizona Arkansas California Colorado Connecticut Delaware Florida Georgia "
              "Hawaii Idaho Illinois Indiana Iowa Kansas Kentucky Louisiana Maine Maryland Massachusetts "
              "Michigan Minnesota Mississippi Missouri Montana Nebraska Nevada Ohio Oregon Texas Utah "
              "Vermont Virginia Washington Wisconsin Wyoming Colorado Oregon Nevada Arizona").split()
def _pick_diff(tag, src, pool):
    """Deterministic element of `pool` that is not `src` (so a state never maps to itself)."""
    h = hmac.new(SALT, ("state:" + tag).encode("utf-8", "ignore"), hashlib.sha256).digest()
    i = int.from_bytes(h[:8], "big") % len(pool)
    if pool[i].upper() == src.upper():
        i = (i + 1) % len(pool)
    return pool[i]


def _fake_state(tok):
    up = tok.upper()
    if len(tok) == 2 and up in STATE_ABBR:
        if not tok.isupper():
            return tok                                    # lowercase 2-letter -> not treated as a state
        return _match_case(tok, _pick_diff(up, up, STATE_ABBR))
    tl = tok.title()
    if tl in STATE_NAME:
        return _match_case(tok, _pick_diff(tl, tl, STATE_NAME))
    return None

def _place_token(token: str) -> str:
    st = _fake_state(token)
    if st is not None:
        return st
    low = token.lower()
    if low in SUFFIX or len(token) <= 2:
        return token                                  # keep generic suffix/short words
    pool = STREETS if low in {s.lower() for s in STREETS} else CITIES
    return _match_case(token, _idx(token, pool))


def fake_place_span(span: str) -> str:
    """Replace a LOCATION/TOWN span with a surrogate place, preserving format. Street numbers are
    perturbed deterministically (kept same length) so the address shape survives but the value changes."""
    def num(m):
        d = m.group(0)
        h = hmac.new(SALT, ("num:" + d).encode(), hashlib.sha256).digest()
        shift = (int.from_bytes(h[:4], "big") % 900) + 10
        return str(int(d) + shift)[:len(d)] if d.isdigit() else d
    parts = re.split(r"([^A-Za-z0-9])", span)
    out = []
    for p in parts:
        if p.isalpha():
            out.append(_place_token(p))
        elif p.isdigit() and len(p) <= 6:
            out.append(num(re.match(r"\d+", p)))
        else:
            out.append(p)
    return "".join(out)
