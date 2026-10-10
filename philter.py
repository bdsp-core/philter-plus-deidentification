import re
import warnings
import json
import os
import nltk
import itertools
import chardet
import pickle
from chardet.universaldetector import UniversalDetector
from nltk.stem.wordnet import WordNetLemmatizer
from coordinate_map import CoordinateMap
from nltk.tag.stanford import StanfordNERTagger
import subprocess
import numpy
import random
import string
import time
from textmethods import get_clean, get_tokens
import pandas as pd
from constants import *

# ---------------------------------------------------------------------------------------------
# CONTEXT RULE for tokens that are BOTH a clinical term and a surname (Senna, Graves, Bell,
# Prader, Wilson). No word list can settle these: "Senna 8.6 mg" is a laxative, "Mr. Senna" is a
# person, and the string is identical. Whitelisting leaks the name everywhere; excluding it
# corrupts every medication list. So decide from the NEIGHBOURING TEXT instead — put the token
# back only when its context is clinical, and never when its context is personal.
# This implements the behaviour DEID_METHODOLOGY.md describes ("<Capitalized> disease/syndrome/
# sign/stain is kept while Mr. Wilson is still removed"), which was documented but never written:
# only a single hardcoded down_syndrome_safe.txt pattern existed.
_CTX_CLINICAL_AFTER = re.compile(
    r"^[\s\-]*(?:[A-ZÀ-Þ][A-Za-zà-ÿ']*[\s\-]+){0,2}"
    r"(disease|syndrome|sign|stain|test|maneuver|manoeuvre|reflex|phenomenon|palsy|"
    r"encephalopathy|sarcoma|lymphoma|carcinoma|tumou?r|fracture|classification|criteria|score|"
    r"scale|index|method|position|nodule|bodies|body|cells|cell|fiber|fibre|angle|ligament|canal|"
    r"gland|duct|space|triangle|notch|deformity|contracture|an[ae]mia|ulcer|node|nodes|plexus|"
    # stains / special preparations: "Congo red", "Wright stain", "Masson trichrome", "Giemsa"
    r"red|blue|silver|trichrome|giemsa|elastic|reticulin|immunostain|antibody|"
    # molecular: "KRAS mutation", "BRAF amplification", "EGFR expression"
    r"mutation|mutations|gene|genes|amplification|expression|fusion|deletion|duplication|"
    r"variant|allele|locus|positivity|hybridization|hybridisation|"
    # surgical / procedural: "Pfannenstiel incision", "Kocher maneuver", "Foley catheter"
    r"incision|approach|flap|graft|procedure|technique|suture|catheter|tube|valve|prosthesis|"
    r"stent|clamp|forceps|retractor|speculum|blade|splint|cast|brace|shunt|drain|port|"
    r"projection|view|plane|line|block|repair|resection|anastomosis)\b", re.I)
_CTX_MED_AFTER = re.compile(
    r"^[\s:\-]*(\d|mg\b|mcg\b|ug\b|gram|ml\b|unit|units|tab\b|tabs\b|tablet|cap\b|caps\b|"
    r"capsule|susp|solution|syrup|patch|cream|ointment|supp|po\b|iv\b|im\b|sc\b|sq\b|prn\b|"
    r"bid\b|tid\b|qid\b|qhs\b|qd\b|daily|nightly|weekly|hs\b|ac\b|pc\b)", re.I)
# Personal context WINS over clinical context: a credential or honorific means it is a person.
_CTX_PERSON_BEFORE = re.compile(
    r"\b(mr|mrs|ms|miss|dr|doctor|prof|attending|resident|rn|md|do|np|pa|by|per)\W{0,3}$", re.I)
# Anatomy named after a person, where the eponym FOLLOWS the anatomical noun: "circle of Willis",
# "foramen of Monro", "vein of Galen", "artery of Heubner", "foramina of Luschka and Magendie".
# Reports also drop the "of" ("foramen Monro"). The noun list is short and specific on purpose,
# so "... of Smith" in ordinary prose is not affected.
_CTX_ANATOMY_BEFORE = re.compile(
    r"\b(circle|foramen|foramina|vein|veins|artery|aqueduct|fissure|cistern|cave|canal|gyrus|gyri|pouch|cleft|"
    r"loop|ganglion)\s+(?:of\s+)?(?:the\s+)?(?:[A-Za-z]+\s+and\s+)?$", re.I)
# ...and where the anatomical noun FOLLOWS the eponym but is not in _CTX_CLINICAL_AFTER:
# "Meckel's cave", "Heschl gyrus", "Rathke cleft", "Chiari II malformation", "Wernicke's area".
_CTX_ANATOMY_AFTER = re.compile(
    r"^(?:['\u2019]s)?[\s\-]*(?:(?:type\s+)?(?:[IV]{1,3}|[1-4])\s+)?(cave|gyrus|gyri|pouch|cleft|fissure|aqueduct|cistern|"
    r"vein|veins|spaces|malformation|turcica|embolization|embolisation)\b"
    r"|^(?:['\u2019]s|s)\s+(area|horn|canals?|cave|type|disease|syndrome|dementia|palsy|paralysis|paresis|"
    r"concentric|sign|phenomenon|tumou?r|malformation|encephalopathy|aphasia)\b|^-like\b", re.I)
# Two-name eponyms: either name is clinical when its partner is next to it ("Sturge-Weber",
# "Spetzler-Martin grade", "Miller Fisher", "Pierre Robin sequence", "Lewy body").
_EPONYM_PARTNERS = {
    "weber": {"sturge", "osler", "rendu"}, "sturge": {"weber"}, "martin": {"spetzler"}, "spetzler": {"martin"},
    "fisher": {"miller"}, "miller": {"fisher"}, "robin": {"virchow", "pierre"}, "virchow": {"robin"},
    "pierre": {"robin"}, "korsakoff": {"wernicke"}, "wernicke": {"korsakoff"}, "jakob": {"creutzfeldt", "creutzfeld"},
    "creutzfeldt": {"jakob"}, "creutzfeld": {"jakob"}, "walker": {"dandy"}, "dandy": {"walker"}, "hess": {"hunt"},
    "hunt": {"hess"}, "bignami": {"marchiafava"}, "marchiafava": {"bignami"}, "lindau": {"hippel"},
    "hippel": {"lindau"}, "fraumeni": {"li"}, "chester": {"erdheim"}, "erdheim": {"chester"},
    "dorfman": {"rosai"}, "rosai": {"dorfman"}, "chiari": {"arnold", "budd"}, "arnold": {"chiari"},
    "lewy": {"body", "bodies"}, "ramsay": {"hunt"}, "tolosa": {"hunt"}, "warburg": {"walker"},
}
_EPONYM_PARTNERS["walker"] = {"dandy", "warburg"}
_EPONYM_PARTNERS["hunt"] = {"hess", "ramsay", "tolosa"}


def _has_eponym_partner(w, before, after):
    partners = _EPONYM_PARTNERS.get(w)
    if not partners:
        return False
    b = re.findall(r"[a-z]+", before.lower())[-1:]
    a = re.findall(r"[a-z]+", after.lower())[:1]
    return bool(partners & set(b + a))


# Medical compounds that no word list can enumerate: "multisequence" = multi + sequence,
# "retrodental" = retro + dental, "frontoparietotemporal" = fronto + parieto + temporal.
_MED_PREFIXES = (
    "multi", "retro", "post", "pre", "non", "peri", "para", "sub", "supra", "infra", "intra", "extra", "inter",
    "trans", "micro", "macro", "hypo", "hyper", "hemi", "semi", "bi", "tri", "re", "co", "anti", "pseudo", "neuro",
    "pachy", "lepto", "un", "over", "under", "mid", "antero", "postero", "infero", "supero", "latero", "medio",
    "ventro", "dorso", "fronto", "parieto", "temporo", "occipito", "cerebello", "thalamo", "striato", "ponto",
    "calloso", "cortico", "spino", "cranio", "cervico", "basi", "oto", "atlanto", "ganglio", "steno", "iso",
    "poly", "pan", "endo", "epi", "ecto", "meso", "auto", "hetero", "homo", "angio", "vasculo", "veno", "arterio")
_PLURAL_ABBREV = re.compile(r"^[A-Z]{2,6}s$")
# Endings that mark a lower-case word as medical: procedures, conditions, enzymes, drug classes.
_MED_SUFFIX = re.compile(
    r"(ostomy|otomy|ectomy|plasty|pexy|rrhaphy|graphy|scopy|scopic|itis|osis|oses|omas?|omata|pathy|pathic|penia|"
    r"plegia|paresis|algia|emia|uria|trophy|trophic|plasia|plastic|genic|genesis|lysis|lytic|oidal|iform|"
    r"ase|mab|nib|statin|cillin|mycin|floxacin|pril|sartan|olol|azepam|platin|rubicin|taxel)$")


def _clin_after(after):
    """_CTX_CLINICAL_AFTER, allowing a possessive first: "Wilson's disease", "Bell's palsy"."""
    return _CTX_CLINICAL_AFTER.match(re.sub(r"^['\u2019]s\b", "", after))


def _near_index(words):
    """The words plus every single-letter deletion of each, for near-miss spelling lookups."""
    out = set()
    for w in words:
        if len(w) >= 6:
            out.add(w)
            out.update(w[:i] + w[i + 1:] for i in range(len(w)))
    return out


def _near_report_word(w, index):
    """True if w is within about one edit of a common report word: "decrased", "subactue", "nornal"."""
    return w in index or any((w[:i] + w[i + 1:]) in index for i in range(len(w)))
# Eponyms that take a type or grade number, which must not be read as a personal suffix ("Smith II").
_NUMBERED_EPONYMS = {"chiari", "fazekas", "fisher", "koos", "knosp", "simpson", "spetzler", "hess"}
_CTX_TYPE_NUMBER = re.compile(r"^\s*\(?\s*(?:type\s+|grade\s+|score\s+(?:of\s+)?)?(?:[IV]{1,3}|[0-5](?:\.5)?)\b", re.I)


def _is_medical_compound(w, vocabs, _depth=0, strict=False):
    """True for prefix + known word. strict (used for Capitalized tokens, which could be surnames)
    needs a prefix of 3+ letters and a remainder of 5+ letters, so "Bishop" and "Coburn" do not pass."""
    def known(x):
        forms = [x] + [x[:-len(s)] for s in ("s", "es", "ly", "ed", "ing") if x.endswith(s) and len(x) - len(s) >= 4]
        return len(x) >= (5 if strict else 4) and any(f in v for f in forms for v in vocabs)
    for p in _MED_PREFIXES:
        if strict and len(p) < 3:
            continue
        if w.startswith(p) and len(w) >= len(p) + 4:
            rest = w[len(p):]
            if known(rest) or (_depth < 1 and _is_medical_compound(rest, vocabs, _depth + 1, strict)):
                return True
    return False


_CTX_PERSON_AFTER = re.compile(r"^\s*,?\s*(m\.?d|d\.?o|r\.?n|np|pa|phd|jr|sr|iii|ii|iv)\b", re.I)
# --- strong evidence that a Capitalized word is a person's name, whatever lists it is on ---------
# Report signature lines: "Attending Radiologist: First M. Last, MD", "Fellow: First Last",
# "Acknowledged by Last, RN", "Electronically signed by: Last, First A MD". The cue is followed by at
# most three capitalized words or initials before the token, all on one line. MEASURED on 10,000
# Stanford radiology reports: 8.6% of the places naming the report's author survived, nearly all
# here, because a given name that is also a clinical term (the put-back list) was restored when
# the credential sat after the SURNAME and nothing marked the given name as a person.
_CTX_SIGNATURE_BEFORE = re.compile(
    r"(?i:\b(?:(?:radiologist|neuroradiologist|resident|fellow|technologist|physician|provider|attending|surgeon|nurse)[ \t]*:"
    r"|(?:acknowledged|interpreted|dictated|signed|read|approved|verified|finalized|transcribed|reported|entered)[ \t]+by[ \t]*:?"
    r"|(?:with|to)[ \t]+provider))"
    r"[ \t]*(?:(?:D[Rr]\.?|[A-Z]\.|[A-Z][A-Za-z'\-]*),?[ \t]+){0,3}$")   # a full stop only after an initial
# An honorific directly before the word: "Dr. Aspen", "Dr Aspen", "DR. ZELBRECHT", "Mr. Zelbrecht".
# Written in capitals, DR counts only with its full stop ("DR CHEST" is a radiograph).
_CTX_HONORIFIC_BEFORE = re.compile(
    r"(?:\b(?:Dr|Doctor|Mr|Mrs|Ms|Miss|Prof)\.?|\b(?:dr|doctor)\.?|\b(?:DR|DOCTOR|PROF)\.)[ \t]+$")
# "MR" and "MS" in capitals are magnetic resonance and multiple sclerosis in a radiology report:
# "MR BRAIN", "MS PROTOCOL", and at the end of a sentence "compared with prior MR. Findings ...".
# So "MR." and "MS." count as honorifics only before a word that is itself in capitals and is not
# ordinary vocabulary: "MR. ZELBRECHT WAS SEDATED" but not "PRIOR MR. FINDINGS ARE UNCHANGED".
_CTX_HONORIFIC_CAPS_BEFORE = re.compile(r"\b(?:MR|MRS|MS|MISS)\.[ \t]+$")
# "DISCUSSED WITH DR ZELBRECHT": DR in capitals without its full stop, when a preposition comes before it.
_CTX_DR_AFTER_PREPOSITION = re.compile(r"\b(?:WITH|BY|TO|FROM|PER)[ \t]+DR[ \t]+$")
# The second word of a name after a title: "Dr. Crystal [Zelbrecht]", "DR. CRYSTAL [ZELBRECHT] AT 1430".
_CTX_TITLE_THEN_GIVEN_BEFORE = re.compile(
    r"(?:\b(?:Dr|Doctor|DR|DOCTOR|dr|doctor)\.?)[ \t]+[A-Z][A-Za-z'\-]+[ \t]+$")
# Short words that follow a name in a sentence written in capitals and are never part of it.
_AFTER_A_NAME = {"at", "on", "of", "the", "and", "or", "for", "with", "by", "to", "in", "from", "re", "who", "was", "is",
                 "has", "had", "will", "via", "per", "as", "regarding", "about", "md", "do", "rn", "np", "pa", "phd"}
# The word starts a name that ends in a credential: "Crystal Zelbrecht, MD", "Crystal A. Zelbrecht MD".
# Without a comma only the credentials that are not also words: "WILL DO" must not read as a name and "DO".
_CTX_NAME_THEN_CREDENTIAL = re.compile(
    r"^(?:[ \t]+[A-Z]\.?)?[ \t]+([A-Z][A-Za-z'\-]+)"
    r"(?:[ \t]*,[ \t]*(?:M\.?D|D\.?O|R\.?N|N\.?P|P\.?A|P[Hh]\.?D|MBBS|CRNA)|[ \t]+(?:MD|RN|NP|P[Hh]D|MBBS|CRNA))\b")
# The word is itself followed by a credential, after a comma: "ZELBRECHT, MD".
_CTX_CREDENTIAL_AFTER = re.compile(r"^[ \t]*,[ \t]*(?:M\.?D|R\.?N|N\.?P|P[Hh]\.?D|MBBS|CRNA)\b")


def _is_named(txt, start, end, w, vocab=()):
    """Strong evidence that the Capitalized word at txt[start:end] is a person's name.

    `vocab` is ordinary report vocabulary, consulted where the place alone could mislead.
    """
    if w in _SIGNATURE_NOT_NAMES:
        return False
    if (_CTX_SIGNATURE_BEFORE.search(txt[max(0, start - 80):start])
            or _CTX_HONORIFIC_BEFORE.search(txt[max(0, start - 12):start])
            or _CTX_DR_AFTER_PREPOSITION.search(txt[max(0, start - 16):start])):
        return True
    # The surname after "Dr. Given". The word after "Dr. Zelbrecht" is as often a verb or a service
    # ("DR. ZELBRECHT AGREED", "Dr. Zelbrecht Stroke team"), so it counts only if it is not ordinary
    # vocabulary. MEASURED on 10,000 MGB reports: without that test 73 more ordinary words were replaced.
    if _CTX_TITLE_THEN_GIVEN_BEFORE.search(txt[max(0, start - 48):start]):
        if w not in _AFTER_A_NAME and w not in vocab:
            return True
    # "First Last, MD": the word after must not be ordinary vocabulary, or the exam title
    # "FUNCTIONAL MRI BRAIN WITH NP TESTING" reads as a person called Brain With, NP.
    then = _CTX_NAME_THEN_CREDENTIAL.match(txt[end:end + 40])
    if then and then.group(1).lower() not in vocab:
        return True
    return bool(_CTX_HONORIFIC_CAPS_BEFORE.search(txt[max(0, start - 8):start]) and txt[start:end].isupper() and w not in vocab)
# Words that stand where a name would in a signature line but are not names.
_SIGNATURE_NOT_NAMES = {
    "none", "not", "no", "see", "unknown", "pending", "the", "this", "attending", "resident", "fellow", "staff",
    "radiology", "neuroradiology", "radiologist", "neuroradiologist", "technologist", "physician", "provider",
    "department", "service", "team", "imaging", "nursing", "pharmacy", "emergency", "neurology", "neurosurgery",
    "medicine", "surgery", "pediatrics", "oncology", "cardiology", "pathology", "anesthesia", "dr", "md", "rn", "np",
    "pa", "do", "phd", "ccrn", "bsn", "msn", "crna", "rt", "mbbs",
    # words of a role title, which stand between the cue and the name: "signed by Interpreting Nuclear Medicine Physician:"
    "interpreting", "nuclear", "supervising", "reviewing", "reading", "referring", "ordering", "performing", "primary",
    "chief", "senior", "junior", "signing", "dictating", "teaching", "consulting", "covering", "on", "call", "of", "and"}
# Dose/unit context a few tokens away, for comma- or ampersand-separated medication lists
# ("Senna, docusate 100 mg"). Adjacency-only missed these; the personal-context veto still applies.
_CTX_MED_WINDOW = re.compile(
    r"^[^.\n]{0,40}?\b\d+(\.\d+)?\s*(mg|mcg|ug|g|ml|unit|units|tab|tabs|tablet|cap|caps|capsule)\b", re.I)
# camelCase medical tokens (AlkPhos, HgbA1c) are never personal names; guarded by the name check.
# A bare integer of 7+ digits is never a lab value, vital, dose or score -- it is an identifier.
# Used to stop the CD (cardinal-number) SAFE filter from blessing identifiers earlier filters missed.
_POS_LONG_DIGITS = re.compile(r"\d{7,}")
_CTX_CAMEL_MED = re.compile(r"^[A-Z][a-z]+[A-Z][A-Za-z0-9]*$")
# --- capitalized report words that are not names ----------------------------------------------------
# Found by counting, place by place, the capitalized ordinary words that were replaced in 108,000
# radiology reports (bdsp-bind-database, deid/check_capitalized_words.py). A measure that asks whether
# MOST uses of a word are replaced cannot see these, because each is replaced in one position only.
#
# Entries of the provider roster that are not people. The roster is made from a provider table that
# also lists clinics, services and machines, and the roster is consulted before anything else, so
# every capitalized use became a made-up name: "Follow-up MRI is recommended" -> "Schurmann-pelfrey
# MRI is recommended" (1,400 places), "CyberKnife" (590 places). Only entries measured to do harm are
# listed; a rule ("every part is an ordinary word") would also drop given names such as "In-Sun".
_ROSTER_NOT_PEOPLE = {"follow-up", "sub-acute", "neuro-oncology", "cyberknife", "intraop", "reformat", "crani", "callback"}
# ...and words of the same kind that no list held, so that they are kept once they are off the roster.
_REPORT_WORDS = {"cyberknife", "intraop", "reformat", "crani", "callback", "visualase"}
# Report vocabulary that is also a rare name, put back unless the text says it is a person: "Aortic
# Arch:", "Cerebellopontine Angles", "Rim-enhancing lesion", "Tracer: FDG", "Burr hole", "Mega cisterna
# magna", "Moya-moya".
_REPORT_AMBIGUOUS = {"arch", "angles", "rim", "dura", "tracer", "coma", "burr", "mega", "moya"}
# A unit of measurement named after a person, after a number: "3 Tesla" (replaced in 9,800 places,
# a third of the Stanford reports), "1.5-Tesla", "20 Gray in 5 fractions".
_UNIT_EPONYM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?[ \t-]{0,2}(Tesla|Gray|Hertz|Kelvin|Gauss|Curie|Becquerel|Sievert|French)\b")
# "Gray-white differentiation", "Gray matter", "Grey/white junction": the colour, not Dr. Gray.
_CTX_GRAY_AFTER = re.compile(r"(?:[ \t]*[-/][ \t]*white\b|[ \t]+(?:and[ \t]+white[ \t]+)?matter\b|[ \t]*-?[ \t]*scale\b|[ \t]+zone\b)", re.I)
# "T2 Star weighted images".
_CTX_T2_BEFORE = re.compile(r"\bT2[ \t*-]*$", re.I)
# The start of a sentence, a line, a list item, or what follows a heading's colon. Not after an initial ("J. Best").
_CTX_SENTENCE_START = re.compile(r"(?:\A[ \t]*|(?<![^A-Za-z][A-Z])[.;?!][ \t]+|:[ \t]+|\n[ \t]*(?:(?:[-*\u2022]|\d{1,2}[.)])[ \t]*)?)\Z")
_CTX_LOWER_NEXT = re.compile(r"(?:-[a-z]+)*[ \t]+[a-z]{2,}\b")
# ...unless what follows says a person is acting: "Best was notified", "Stone reviewed the images".
_CTX_PERSON_VERB = re.compile(
    r"(?:-[a-z]+)*[ \t]+(?:(?:was|is|has|had|were|will)[ \t]+(?:been[ \t]+)?(?:notified|informed|paged|called|contacted|"
    r"consulted|aware|present|available|reviewed)|made[ \t]+aware|reviewed|notified|called|paged|agrees|agreed|states|stated|"
    r"reports|reported|denies|denied|said|says|recommends|recommended|discussed|signed|dictated|interpreted|saw|spoke|"
    r"confirmed|acknowledged|and[ \t]+(?:I|Dr)\b)\b")
# --- numbers that are not identifiers ------------------------------------------------------------------
# Found by looking up every number of one to four digits in 108,000 radiology reports at the same place
# in the output (bdsp-bind-database, deid/check_numbers.py). A changed number is a silent error.
#
# References to images. Reports cite the series and image on which a finding is seen: "(4/23)",
# "(5/12, 5/14)", "series 4 image 23". The tagger reads "4/23" as a date, so the pair was shifted
# ("(4/23)" -> "(05/23)", "(3/45)" -> "(April 2045)"), and an image number at the end of a sentence
# was replaced by another number ("image 23." -> "image 47."). One Stanford report in nine had at
# least one changed. The images are released with the reports, so these references matter.
_IMAGE_WORD = re.compile(
    r"(?i)\b(?:series|ser|se|images?|img|im|slices?|frames?)[ \t]*(?:#|no\.?|number)?[ \t]*"
    r"(\d{1,4}(?:[ \t]*/[ \t]*\d{1,4})?(?:[ \t]*(?:,|-|\u2013|and|through|to|&)[ \t]*\d{1,4}(?:[ \t]*/[ \t]*\d{1,4})?)*)"
    r"(?![\d/.-]\d|\d)")
_IMAGE_PAIRS = re.compile(
    r"\((?:[ \t]*(?:series|se)[ \t]*)?(\d{1,3})[ \t]*/[ \t]*(\d{1,4})(?:[ \t]*-[ \t]*\d{1,4})?"
    r"(?:[ \t]*(?:[,;&]|and)[ \t]*\d{1,3}[ \t]*/[ \t]*\d{1,4}(?:[ \t]*-[ \t]*\d{1,4})?)*[ \t]*\)", re.I)
# ...but one pair that could be a month and day, right after a word for an examination or a date, is a
# date and is left to the date rules: "prior MRI (3/14)".
_DATE_WORD_BEFORE = re.compile(r"(?i)\b(?:mri|mr|mra|ct|cta|pet|exam|examination|study|studies|scan|scans|prior|dated|date|"
                               r"from|on|comparison|compared|since|until|done|performed)[ \t]*$")
# Radiotracers: "18F-FDG" became "23F-WINOWIECKI". The isotope, and the compound if it is a known one.
_ISOTOPE_N = r"(?:18|11|13|15|68|99m|123|124|125|131|64|89|82|201|111|177|90|223)"
_ISOTOPE_E = r"(?:F|C|N|O|Ga|Tc|I|Cu|Zr|Rb|Tl|In|Lu|Y|Ra)"
_RADIOTRACER = re.compile(
    rf"(?<![\w.])(?:[\[(]?{_ISOTOPE_N}[\])]?[ \t-]?{_ISOTOPE_E}\]?|{_ISOTOPE_E}[ \t-]?{_ISOTOPE_N})(?![a-z])"
    r"(?:[ \t-]?(?:FDG|fluorodeoxyglucose|florbetapir|florbetaben|flutemetamol|flortaucipir|PiB|PIB|FDOPA|DOPA|DOTATATE|"
    r"DOTATOC|PSMA|choline|fluciclovine|MDP|HMPAO|ECD|sestamibi|DTPA|MIBG|ioflupane|FLT|FET|FMISO|NaF|raclopride|"
    r"methionine|acetate|ammonia|water|tetrofosmin|pertechnetate|MAA|exametazime|bicisate|AV-?45|AV-?1451|MK-?6240|"
    r"PBR-?28|UCB-?J)\b)?")
# --- dates that the replacement walk takes as whole dates, before any other step sees their parts ---
# Philter's own date rules miss some ways of writing a date, and other steps of the walk used to break
# others before the date step was reached:
#   "April 4, 2019"   the provider roster holds April and August and is consulted first, so the month
#                     became a made-up first name and the real day stayed: "Laraine 4, 8930"
#   "03-04-2019"      a bare one- to three-digit number is kept, and a hyphen does not stop that rule
#                     as a slash does, so the real month and day stayed: "03-04-8930"
#   "2019.3.4"        not recognised, left as written
#   "March 4"         left as written in two of five places
# Measured on 108,000 radiology reports: 7,000 dates written with April or August were damaged, and
# about 700 real dates (complete, or month and day) were left as written.
_MON_FULL = "January|February|March|April|May|June|July|August|September|October|November|December"
_MON = rf"(?:{_MON_FULL}|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)"
_DAY, _MNUM, _YR = r"(?:0?[1-9]|[12]\d|3[01])", r"(?:0?[1-9]|1[0-2])", r"(?:19|20)\d{2}"
_NB, _NE = r"(?<![\w/.-])", r"(?![\w/-]|\.\d)"          # a number that is not part of a longer one
_WRITTEN_DATE = re.compile(
    rf"\b{_MON}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b"                 # March 4, 2019
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}\.?,?\s+\d{{4}}\b"       # 4 March 2019
    rf"|{_NB}{_DAY}-{_MON}-(?:{_YR}|\d{{2}})\b"                                  # 4-Mar-2019, 04-MAR-19
    rf"|{_NB}{_YR}-{_MON}-{_DAY}\b"                                              # 2019-Mar-04 (I0001 report headers)
    rf"|{_NB}{_DAY}{_MON}(?:{_YR}|\d{{2}})\b"                                    # 04MAR2019, 4Mar19
    rf"|\b{_MON}\.?,?\s+(?:of\s+)?\d{{4}}\b"                                   # March 2019, March of 2019
    rf"|{_NB}{_MNUM}-{_DAY}-(?:{_YR}|\d{{2}}){_NE}"                              # 03-04-2019, 3-4-19
    rf"|{_NB}{_YR}([-./]){_MNUM}\1{_DAY}{_NE}"                                   # 2019-03-04, 2019.3.4
    rf"|{_NB}{_MNUM}-{_YR}{_NE}", re.I)                                          # 03-2019
# A weekday before a complete date. The weekday is an ordinary word and was kept, so the text read
# "Monday, April 03, 2019": the real weekday beside the shifted date, which is both wrong and tells
# the shift modulo seven. The pair is taken together and the weekday is set to that of the shifted
# date. Capitalized weekdays only, so that "sat" in a sentence is never touched.
_WD = (r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|"
       r"SATURDAY|SUNDAY|Mon|Tues|Tue|Wed|Thurs|Thur|Thu|Fri|Sat|Sun)")
_WEEKDAY_DATE = re.compile(
    rf"\b{_WD}\.?,?[ \t]+(?:(?i:{_MON})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?i:{_MON})\.?,?\s+\d{{4}}\b"
    rf"|{_MNUM}/{_DAY}/(?:{_YR}|\d{{2}}){_NE}|{_MNUM}-{_DAY}-(?:{_YR}|\d{{2}}){_NE})")
_WEEKDAY_FIRST = re.compile(rf"({_WD})(\.?,?[ \t]+)")
# A range of days in one month: "March 4-6, 2019".
_DATE_RANGE = re.compile(
    rf"\b({_MON})\.?\s+({_DAY})(?:st|nd|rd|th)?(\s*(?:-|\u2013|to|through|and|&)\s*)({_DAY})(?:st|nd|rd|th)?,?\s+({_YR})\b", re.I)
# A month and a day with no year. The month has a capital letter, and a three-letter abbreviation in
# capitals is left alone because it is as often something else (SEP, MAR, OCT, DEC).
_DATE_NO_YEAR = re.compile(
    rf"\b(?:{_MON}|{_MON_FULL.upper()})\.?[ \t]+{_DAY}(?:st|nd|rd|th)?"
    rf"(?![\w/:%-]|\.\d|[ \t]*(?:mm|cm|ml|cc|mg|min|sec|hours?|hrs?|days?|weeks?|months?|years?|x|times)\b)")
# A month by name alone is a date where a word of time stands before it: "in March", "since August".
# Not before a capitalized word ("by April Jones") or a hyphen ("May-Thurner"). "May" needs a stricter
# word before it than the other months, because "and May represent" is the verb.
_MONTH_ALONE = re.compile(
    rf"\b(?i:in|since|until|of|from|early|late|mid|last|this|next|before|after|around|during|through|by|and|to)"
    rf"[ \t-]+((?:{_MON_FULL.replace('May|', '')}|{_MON_FULL.upper().replace('MAY|', '')}))\b"
    rf"(?![\w-]|\.?[ \t]*\d|[ \t]+[A-Z][a-z])"
    rf"|\b(?i:in|since|until|of|from|early|late|mid|last|this|next|before|after|around|during|through)"
    rf"[ \t-]+(May|MAY)\b(?![\w-]|\.?[ \t]*\d|[ \t]+[A-Z][a-z])")
_SEASON_YEAR = re.compile(r"(?i)(?:(?:early|late|mid)[ \t-]*)?(?:spring|summer|fall|autumn|winter)(?:[ \t]+of)?[ \t]+(?:19|20)\d{2}")
_MONTH_NUMBER = {m: n for n, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_W_MONTH_YEAR = re.compile(rf"({_MON})\.?,?\s+(?:of\s+)?(\d{{4}})", re.I)
_W_MONTH_DAY = re.compile(rf"({_MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?", re.I)
_W_MONTH = re.compile(rf"{_MON}\.?", re.I)
# ---------------------------------------------------------------------------------------------


class Philter:
    """ 
        General text filtering class,
        can filter using whitelists, blacklists, regex's and POS
    """
    def __init__(self, config):
        self.filenames = []
        self.texts    = {}
        self.known_phi = {}
        if "verbose" in config:
            self.verbose = config["verbose"]
        if "gold" in config:
            self.gold = config["gold"]
        else:
            self.gold = True
        if "run_eval" in config:
            self.run_eval = config["run_eval"]
        if "dependent" in config:
            self.dependent = config["dependent"]
        if "time_profile" in config:
            self.time_profile = config["time_profile"]
        else :
            self.time_profile = False
        if "freq_table" in config:
            self.freq_table = config["freq_table"]
        if "initials" in config:
            self.initials = config["initials"]   
        if "finpath" in config:
            if not os.path.exists(config["finpath"]):
                raise Exception("Filepath does not exist", config["finpath"])
            self.finpath = config["finpath"]
            self.texts = self._read_texts()
        if "phi_text" in config:
            self.texts = config["phi_text"]
            if "filenames" in config:
               self.filenames = config["filenames"]
            else:
               raise Exception("Object with list of filenames not provided")
        if "known_phi" in config:
            self.known_phi = config["known_phi"]
        if "foutpath" in config:
            if not os.path.exists(config["foutpath"]):
                raise Exception("Filepath does not exist", config["foutpath"])
            self.foutpath = config["foutpath"]
        if "anno_folder" in config:
            if not os.path.exists(config["anno_folder"]):
                raise Exception("Filepath does not exist", config["anno_folder"])
            self.anno_folder = config["anno_folder"]
        
        if "coords" in config:
            self.coords = config["coords"]
        
        if "eval_out" in config:
            self.eval_outpath = config["eval_out"]

        if "outformat" in config:
            self.outformat = config["outformat"]
        else:
            self.outformat = "asterisk"
        
        if "ucsfformat" in config:
            self.ucsf_format = config["ucsfformat"]

        # Per-patient date shift for the "surrogate" output format: DATE spans are replaced with
        # the real date shifted by this many days (the canonical per-patient offset), not asterisks.
        # `date_shifts` may be an inline {filename_basename: days} dict or a path to such a JSON;
        # `default_date_shift` applies when a file has no entry.
        self.date_shifts = {}
        self.default_date_shift = int(config.get("default_date_shift", 0))
        _ds = config.get("date_shifts")
        if isinstance(_ds, dict):
            self.date_shifts = _ds
        elif isinstance(_ds, str) and os.path.exists(_ds):
            self.date_shifts = json.loads(open(_ds).read())

        if "known_names" in config:
            self.known_names_path = config["known_names"]


        if "filters" in config:
            if not os.path.exists(config["filters"]):
                raise Exception("Filepath does not exist", config["filters"])
            self.patterns = json.loads(open(config["filters"], "r").read())
            if ("known_phi" in config) and ("namesprobe" in config):
                raise Exception ("Both mongo probes collection and a probes "
                                 + "file provided. Please remove one and try "
                                 + "again.")
            self.dynamic = False
            if ("namesprobe" in config) or ("known_phi" in config):
                self.dynamic = True
                dynamic_blacklist = {
                    "notes": "These are known phi that are not safe",
                    "filepath": "",
                    "phi_type": "PROBEDYNAMICSET",
                    "exclude": True,
                    "pos": ["NNP", "NNPS", "CD", "NN", "NNS", "IN", "JJ",
                            "VBD", "VBG", "RB"],
                    "type": "dynamic_set",
                    "title": "Dynamic Blacklist"}
                if ("namesprobe" in config):
                    if not os.path.exists(config["namesprobe"]):
                        raise Exception("Filepath does not exist",
                                        config["namesprobe"])
                    dynamic_blacklist["filepath"] = config["namesprobe"]
                elif ("known_phi" in config):
                    dynamic_blacklist["filepath"] = "Mongo.mongo"
                self.patterns.append(dynamic_blacklist)


        if "xml" in config:
            if not os.path.exists(config["xml"]):
                raise Exception("Filepath does not exist", config["xml"])
            self.xml = json.loads(open(config["xml"], "r", encoding='utf-8').read())

        if "stanford_ner_tagger" in config:
            try:
                if (not os.path.exists(config["stanford_ner_tagger"]
                                       ["classifier"])
                    and config["stanford_ner_tagger"]["download"] == False):
                    raise Exception("Filepath does not exist",
                                    config["stanford_ner_tagger"]["classifier"])
                else:
                    #download the ner data
                    process = subprocess.Popen("cd generate_dataset && ./download_ner.sh".split(), stdout=subprocess.PIPE)
                    output, error = process.communicate()
                self.stanford_ner_tagger_classifier = config["stanford_ner_tagger"]["classifier"]
                if not os.path.exists(config["stanford_ner_tagger"]["jar"]):
                    raise Exception("Filepath does not exist",
                                    config["stanford_ner_tagger"]["jar"])
                self.stanford_ner_tagger_jar = config["stanford_ner_tagger"]["jar"]
            except Exception as err:
                if __debug__: print("WARNING: Stanford NER tagger "
                                    + "Exception: {0}".format(err))
        #we lazy load our tagger only if there's a corresponding pattern
        self.stanford_ner_tagger = None

        if "cachepos" in config and config["cachepos"]:
            self.cache_to_disk = True
            self.pos_path = config["cachepos"]
            if not os.path.isdir(self.pos_path):
                os.makedirs(self.pos_path)
        else:
            self.cache_to_disk = False
            self.pos_path = None 

        #All coordinate maps stored here
        self.coordinate_maps = []

        #create a memory for pos tags
        self.pos_tags = {}

        #create a memory for tokenized text
        self.cleaned = {}

        #create a memory for tokenized anno text
        self.clean_filtered = {}

        #create a memory for include coordinate map
        self.include_map = CoordinateMap()

        #create a memory for exclude coordinate map
        self.exclude_map = CoordinateMap()

        #create a memory for FULL exclude coordinate map (including non-whitelisted words)
        self.full_exclude_map = {}

        #create a memory for the list of known PHI types
        self.phi_type_list = ['HOLIDAYS', 'DATE', 'ID', 'NAME', 'CONTACT',
                              'AGE>=90', 'AGE<90', 'OTHER', 'LOCATION',
                              'PROBE', 'PROBEREGEX', 'PROBEDYNAMICSET',
                              'PROBEREGEXCONTEXT', 'TOWN']

        #create a memory for the corrdinate maps of known PHI types    
        self.phi_type_dict = {}
        for phi_type in self.phi_type_list:
            self.phi_type_dict[phi_type] = [CoordinateMap()]

        #create a memory for stored coordinate data
        self.data_all_files = {}

        #create a memory for pattern index, with titles
        self.pattern_indexes = {}

        #create a memory for token counts per note
        #keys are filename, values are lists of time measurements (order same as regex_name_list below)
        self.token_data = {}

        #create a memory for regex time profiling if time profile flag is true
        if self.time_profile:
            self.regex_name_list = []
            self.current_regex_time_profile = []
            self.overall_regex_time_profile = {}

        #initialize our patterns
        self.init_patterns()


    def _read_texts(self):
        if not self.finpath:
            raise Exception("Input directory undefined: ", self.finpath)

        for root, dirs, files in os.walk(self.finpath):
            for filename in files:
                if (not filename.endswith("txt")) or 'meta_data' in filename:
                    continue

                filepath = os.path.join(root, filename)

                self.filenames.append(filepath)
                encoding = self.detect_encoding(filepath)
                fhandle = open(filepath, "r", encoding=encoding['encoding'],
                               errors='surrogateescape')
                self.texts[filepath] = fhandle.read()
                fhandle.close()


    def get_pos(self, filename, cleaned):
        if self.cache_to_disk:
            pos_path = self.pos_path
            filename = filename.split("/")[-1]
            file_ = pos_path + str(filename)
            if filename not in self.pos_tags:
                self.pos_tags[filename] = {}
            if not os.path.isfile(file_):
                   with open(file_, 'wb') as f:
                        tags = nltk.pos_tag(cleaned)
                        pickle.dump(tags, f)
                        return tags
            else:
                   with open(file_, 'rb') as f:
                        self.pos_tags[filename] = pickle.load(f)
        else:
            if filename not in self.pos_tags:
                self.pos_tags[filename] = nltk.pos_tag(cleaned)
       
        return self.pos_tags[filename]
    
    def get_clean(self, filename, text, pre_process= r"[^a-zA-Z0-9]"):
        tokens = 0
        if filename not in self.cleaned:
            self.cleaned[filename] = {}
            # Use pre-process to split sentence by spaces AND symbols, while preserving spaces in the split list
            lst = re.split("(\s+)", text)
            cleaned = []
            for item in lst:
                if len(item) > 0:
                    if item.isspace() == False:
                        split_item = re.split("(\s+)", re.sub(pre_process,
                                                              " ", item))
                        for elem in split_item:
                            if len(elem) > 0:
                                cleaned.append(elem)
                                tokens += 1
                    else:
                        cleaned.append(item)
            self.cleaned[filename] = (cleaned,tokens)
        return self.cleaned[filename]

    def get_clean_filtered(self, filename, text,
                           pre_process= r"[^a-zA-Z0-9\*]",
                           phi_matcher=re.compile(r"\*+")):
        phi_tokens = 0
        if filename not in self.clean_filtered:
            self.clean_filtered[filename] = {}
            # Use pre-process to split sentence by spaces AND symbols, while preserving spaces in the split list
            lst = re.split("(\s+)", text)
            clean_filtered = []
            for item in lst:
                if len(item) > 0:
                    if item.isspace() == False:
                        split_item = re.split("(\s+)", re.sub(pre_process,
                                                              " ", item))
                        for elem in split_item:
                            if len(elem) > 0:
                                clean_filtered.append(elem)
                                if phi_matcher.search(elem):
                                    phi_tokens += 1
                    else:
                        clean_filtered.append(item)
            self.clean_filtered[filename] = (clean_filtered,phi_tokens)
        return self.clean_filtered[filename]
    
    def init_patterns(self):
        """ given our input pattern config will load our sets and pre-compile our regex"""

        known_pattern_types = set(["regex", "set", "dynamic_set",
                                   "regex_context", "dynamic_regex",
                                   "dynamic_regex_context",
                                   "stanford_ner", "pos_matcher", "match_all"])
        require_files = set(["regex", "set"])
        require_pos = set(["pos_matcher"])
        set_filetypes = set(["pkl", "json", "txt", "mongo"])
        regex_filetypes = set(["txt"])
        reserved_list = set(["data", "coordinate_map"])
        #first check that data is formatted, can be loaded etc. 
        for i,pattern in enumerate(self.patterns):
            self.pattern_indexes[pattern['title']] = i
            if (pattern["type"] in require_files
                and not os.path.exists(pattern["filepath"])):
                raise Exception("Config filepath does not exist",
                                pattern["filepath"])
            for k in reserved_list:
                if k in pattern:
                    raise Exception("Error, Keyword is reserved", k, pattern)
            if pattern["type"] not in known_pattern_types:
                raise Exception("Pattern type is unknown", pattern["type"])
            if pattern["type"] == "set":
                if pattern["filepath"].split(".")[-1] not in set_filetypes:
                    raise Exception("Invalid filetype", pattern["filepath"],
                                    "must be of", set_filetypes)
                self.patterns[i]["data"] = self.init_set(pattern["filepath"])
            if pattern["type"] == "dynamic_set":
                if pattern["filepath"].split(".")[-1] not in set_filetypes:
                    raise Exception("Invalid filetype", pattern["filepath"],
                                    "must be of", set_filetypes)
                self.patterns[i]["data"] = {}
                self.patterns[i]["dyndata"] = self.init_set(pattern["filepath"])
            if pattern["type"] == "regex" or pattern["type"] == "regex_context":
                if pattern["filepath"].split(".")[-1] not in regex_filetypes:
                    raise Exception("Invalid filteype", pattern["filepath"],
                                    "must be of", regex_filetypes)
                self.patterns[i]["data"] = None
                self.patterns[i]["data"] = self.precompile(pattern["filepath"])
            if pattern["type"] == "dynamic_regex":
                if pattern["filepath"].split(".")[-1] not in regex_filetypes:
                    raise Exception("Invalid filteype", pattern["filepath"],
                                    "must be of", regex_filetypes)
                self.patterns[i]["data"] = None
                self.patterns[i]["dyndata"] = self.precompile(pattern["filepath"])
            if pattern["type"] == "dynamic_regex_context":
                if pattern["filepath"].split(".")[-1] not in regex_filetypes:
                    raise Exception("Invalid filteype", pattern["filepath"],
                                    "must be of", regex_filetypes)
                self.patterns[i]["data"] = None
                self.patterns[i]["dyndata"] = self.precompile(pattern["filepath"])
    
    def precompile(self, filepath):
        """ precompiles our regex to speed up pattern matching"""
        
        for line in open(filepath,"r"):
            if not line.strip().startswith("#"):
               regex = line.strip()
        re_compiled = None
        with warnings.catch_warnings(): #NOTE: this is not thread safe! but we want to print a more detailed warning message
            warnings.simplefilter(action="error", category=FutureWarning) # in order to print a detailed message
            try:
                re_compiled = re.compile(regex)
            except FutureWarning as warn:
                print("FutureWarning: {0} in file ".format(warn) + filepath)
                warnings.simplefilter(action="ignore", category=FutureWarning)
                re_compiled = re.compile(regex) # assign nevertheless
        return re_compiled
               
    def init_set(self, filepath):
        """ loads a set of words, (must be a dictionary or set shape) returns result"""
        map_set = {}
        if filepath.endswith(".pkl"):
            try:
                with open(filepath, "rb") as pickle_file:
                    map_set = pickle.load(pickle_file)
            except UnicodeDecodeError:
                with open(filepath, "rb") as pickle_file:
                    map_set = pickle.load(pickle_file, encoding = 'latin1')
        elif filepath.endswith(".json"):
            map_set = json.loads(open(filepath, "r").read())
        elif filepath.endswith(".txt"): # dynamic set
            names_probe_types = ['lname', 'fname', 'mname', 'preferred_name']
            address_probe_types = ['ADDR', 'empr_city', 'EMERG_CITY',
                                   'EMERG_CITY_2', 'FATHER_CITY', 'MOTHER_CITY']
            try:
                probes_file = pd.read_csv(filepath, sep='\t', index_col=False,
                                          usecols=['value', 'phi_type',
                                                   'note_key'],
                                          dtype=str, encoding='latin-1')
                #names_probes = probes_file.loc[(probes_file['phi_type'] in
                 #                               names_probe_types)]
                names_probes = probes_file.loc[(probes_file['phi_type'] == 'lname') | (probes_file['phi_type'] == 'fname') | (probes_file['phi_type'] == 'mname') | (probes_file['phi_type'] == 'preferred_name')]
                zip_probes = probes_file.loc[(probes_file['phi_type'] == 'ZIP')]
                mrn_probes = probes_file.loc[(probes_file['phi_type'] == 'MRN')]
                phone_probes = probes_file.loc[(probes_file['phi_type']
                                                == 'phone')]
                address_probes = probes_file.loc[(probes_file['phi_type'] == 'ADDR') | (probes_file['phi_type'] == 'empr_city') | 
                    (probes_file['phi_type'] == 'EMERG_CITY') | (probes_file['phi_type'] == 'EMERG_CITY_2') | 
                    	(probes_file['phi_type'] == 'FATHER_CITY') | (probes_file['phi_type'] == 'MOTHER_CITY')]
                #address_probes = probes_file.loc[(probes_file['phi_type'] in
                 #                                 address_probe_types)]
                workplace_probes = probes_file.loc[(probes_file['phi_type']
                                                    == 'empr_id_cmt')]
            except pd.errors.EmptyDataError as err:
                print("Pandas Empty Data Error: " + filepath
                       + " is empty {0}".format(err))
                return {}, {}
            except ValueError as err:
                print("Value Error: " + filepath
                       + " is invalid {0}".format(err))
                return {}, {}
            # Make list of lists in dictionary: map_set = {tup(probe,probe_type): [note_key1, note_key2]}
            probes_type_list = [names_probes, zip_probes, mrn_probes,
                                phone_probes, address_probes, workplace_probes]
            # Names
            for i in probes_type_list:
               for index, row in i.iterrows():
                   probe_type = row['phi_type']
                   if probe_type in names_probe_types:
                      probe_type = 'name'
                   elif probe_type in address_probe_types: 
                      probe_type = 'address'
                   elif probe_type == 'empr_id_cmt':
                       probe_type = 'workplace'
                   probe_type = probe_type.lower()  
                   # Alternate value column name:
                   #value = row['clean_value'] 
                   value = row['value']
                   note_key = row['note_key']
                   if note_key in map_set:
                      if probe_type in map_set[note_key]:
                         if value not in map_set[note_key][probe_type]:
                            map_set[note_key][probe_type].append(value)
                      else:
                         map_set[note_key][probe_type] = [value]
                   else:
                       map_set[note_key] = {probe_type: [value]}
        elif filepath.endswith(".mongo"):
             map_set = self.known_phi
        else:
            raise Exception("Invalid filteype",filepath)
        return map_set

    def _update_dynamic_patterns(self, filename, 
                                 include_singles = False,
                                 include_nonames = False):
        nonames = ['md', 'dr', 'pt', 'no', 'in', 'none', 'of', 'by', 'ct',
                   'none', 'medical', 'pathology', 'patient', 'study', 'nan',
                   'per', 'contact']
        nocities = ['friend', 'cousin', 'brother', 'normal', 'nephew',
                    'likely', 'unknown', 'rescue' ,'decline']
        map_set = {}
        context_probes = []
        regex_probes = []
        pat_idx_dynbl = self.pattern_indexes["Dynamic Blacklist"]
        
        if self.known_phi:
            note_key = filename
        elif (filename.find('.txt') != -1) or (filename.find('.xml') != -1):
            file_note_key = os.path.basename(filename).replace('\n','')
            file_note_key = file_note_key.replace('.txt','')
            file_note_key = file_note_key.lstrip('0')
            file_note_key = file_note_key.replace('.xml','')
            file_note_key = file_note_key.replace('_utf8','')
            note_key = file_note_key
        else:
            raise Exception("PROGRAMM ERROR: Invalid filename ", filename)

        name_pattern = re.compile(r"\b([A-Z]\'[a-zA-Z]+\b|[A-Z]\s[a-zA-Z]+\b|[A-Z]\.[A-Z]\.[A-Z]\.|[A-Z]\.[A-Z]\.)")
        dyndata = self.patterns[pat_idx_dynbl]["dyndata"]
        if note_key in dyndata:
            for probe_type in dyndata[note_key]:
                for probe in dyndata[note_key][probe_type]:
                    probe_str = str(probe)
                    probe_clean = get_clean(probe_str)
                    # Get names probes for regex search
                    if (probe_type == 'name'
                        and name_pattern.match(probe_str)):
                        regex_probes.append(probe_str)
                    phone_regex = ''
                    address_regex = ''
                    workplace_regex = ''
                    name_regex = ''
                    counter = 0
                    for pc in probe_clean:
                        
                        ### Name
                        if probe_type == 'name':
                            prb = re.sub(r"[^a-zA-Z0-9]+", "",
                                         str(pc).lower().strip())
                            if ((include_singles or len(prb) > 1)
                                and (include_nonames or prb not in nonames)):
                                map_set[prb] = note_key
                                prb_with_s = prb + 's'
                                map_set[prb_with_s] = note_key
                                for p in [prb, prb_with_s]:
                                    name_regex = ('(?i)\d+[\/\-\.]\d+[\/\-\.]'
                                                  + '\d+\s+\d*\:\d*|\d+[\/\-\.]'
                                                  + '\d+[\/\-\.]\d+' + p)
                                    regex_probes.append(name_regex)

                            # If single character or in list of nonames,
                            # add to list of context probes
                            else:
                                if prb.isdigit() == False:
                                    context_probes.append(prb)

                        ### Zip/MRN
                        if probe_type == 'zip' or probe_type == 'mrn':
                            prb = pc
                            map_set[prb] = note_key

                        ### Phone
                        if probe_type == 'phone':
                            pc = re.sub(r"[^0-9]+", "",str(pc).strip())
                            # Allow for any number of non-alphanumeric
                            # characters to separate each digit,
                            # as long as all digits in probe are present
                            if pc != '' and counter < len(probe_clean)-1:
                                phone_regex = (phone_regex + pc
                                               + '([^0-9A-Za-z]{1,3})?')
                            if pc != '' and counter == len(probe_clean)-1:
                                phone_regex = phone_regex + pc

                        ### Address
                        if probe_type == 'address':
                            pc = str(pc).lower().strip()
                            if pc not in nocities and pc not in large_cities: 
                                if pc != '' and counter < len(probe_clean)-1:
                                    address_regex = (address_regex + pc
                                                     + '([^0-9A-Za-z]{1,5})?')
                                if pc != '' and counter == len(probe_clean)-1:
                                    address_regex = address_regex + pc

                        ### Workplace
                        if probe_type == 'workplace':
                            pc = str(pc).lower().strip()
                            if pc != '' and counter < len(probe_clean)-1:
                                workplace_regex = (workplace_regex + pc
                                                   + '([^0-9A-Za-z]{1,5})?')
                            if pc != '' and counter == len(probe_clean)-1:
                                workplace_regex = workplace_regex + pc

                        counter = counter + 1

                    # Add to regex probes after all probe pieces added
                    if phone_regex != '':
                        regex_probes.append(phone_regex)
                    if address_regex != '':
                        regex_probes.append(address_regex)
                    if workplace_regex != '':
                        regex_probes.append(workplace_regex)
                    if name_regex != '':
                        #print(name_regex)
                        regex_probes.append(name_regex)

        self.patterns[pat_idx_dynbl]["data"] = map_set
        
        # Substitute probes into probes_regex and probes_regex_context
        rgx_types = ["Probes Regex", "Probes Regex Context"]
        for rtype in rgx_types:
            if rtype in self.pattern_indexes:
                ipat = self.pattern_indexes[rtype]
                if rtype == "Probes Regex":
                    rgx_probes = regex_probes
                elif rtype == "Probes Regex Context":
                    rgx_probes = context_probes
                else:
                    rgx_probes = []
                if len(rgx_probes) > 0:
                    rgx = self.patterns[ipat]['dyndata'].pattern
                    regex_string = rgx.replace('"""+probe+r"""',
                                               '|'.join(rgx_probes))
                    #print(regex_string)
                    self.patterns[ipat]['data'] = re.compile(regex_string)
                else:
                    self.patterns[ipat]['data'] = re.compile(r"\b\B") #never match

    def map_coordinates(self, allowed_filetypes=set(["txt", "ano"])):
        """ Runs the set, or regex on the input data 
            generating a coordinate map of hits given 
            (this performs a dry run on the data and doesn't transform)
        """

        #create coordinate maps for each pattern
        for i,pat in enumerate(self.patterns):
            self.patterns[i]["coordinate_map"] = CoordinateMap()
        for filename in self.filenames:

            txt = self.texts[filename]

            # Get full self.include/exclude map before transform
            self.data_all_files[filename] = {"text":txt, "phi":[], "non-phi":[]}

            #create an intersection map of all coordinates we'll be removing
            self.exclude_map.add_file(filename)

            #create an interestion map of all coordinates we'll be keeping
            self.include_map.add_file(filename)
            # add file to phi_type_dict
            for phi_type in self.phi_type_list:
                self.phi_type_dict[phi_type][0].add_file(filename)

            # create empty list for this file's regex profiling data
            if self.time_profile:
               self.current_regex_time_profile = []

            # update dynamic patterns for current note
            if self.dynamic:
                self._update_dynamic_patterns(filename=filename)
            # initialize phi type
            phi_type = "OTHER"

            #### Create inital self.exclude/include for file

            for i,pat in enumerate(self.patterns):
                if pat["type"] == "regex":
                    self.map_regex(filename=filename, text=txt, pattern_index=i)
                elif pat["type"] == "dynamic_set":
                    self.map_set(filename=filename, text=txt, pattern_index=i)
                elif pat["type"] == "set":
                    self.map_set(filename=filename, text=txt, pattern_index=i)
                elif pat["type"] == "dynamic_regex":
                    self.map_regex(filename=filename, text=txt, pattern_index=i)
                elif pat["type"] == "dynamic_regex_context":
                    self.map_regex_context(filename=filename, text=txt,
                                           pattern_index=i)
                elif pat["type"] == "regex_context":
                    self.map_regex_context(filename=filename, text=txt,
                                           pattern_index=i)
                elif pat["type"] == "stanford_ner":
                    self.map_ner(filename=filename, text=txt, pattern_index=i)
                elif pat["type"] == "pos_matcher":
                    self.map_pos(filename=filename, text=txt, pattern_index=i)
                elif pat["type"] == "match_all":
                    self.match_all(filename=filename, text=txt, pattern_index=i)
                else:
                    raise Exception("Error, pattern type not supported: ",
                                    pat["type"])
                self.get_exclude_include_maps(filename, pat, txt)
            if self.time_profile:
                # Add the filename's time profile to larger list
                self.overall_regex_time_profile[filename] = self.current_regex_time_profile

            #create intersection maps for all phi types and add them to a dictionary containing all maps

            # get full exclude map (only updated either on-command by map_regex_context or at the very end of map_coordinates)
            self.full_exclude_map[filename] = self.include_map.get_complement(filename, txt)
                
            for phi_type in self.phi_type_list:
                for start,stop in self.phi_type_dict[phi_type][0].filecoords(filename):
                    self.data_all_files[filename]["phi"].append({"start":start, "stop":stop, "word":txt[start:stop], "phi_type":phi_type, "filepath":""})

        #clear out any data to save ram
        for i,pat in enumerate(self .patterns):
            if "data" in pat:
                del self.patterns[i]["data"]
        return self.full_exclude_map
                
    def map_regex(self, filename="", text="", pattern_index=-1, pre_process= r"[^a-zA-Z0-9]"):
        """ Creates a coordinate map from the pattern on this data
            generating a coordinate map of hits given (dry run doesn't transform)
        """
        if pattern_index < 0 or pattern_index >= len(self.patterns):
            raise Exception("Invalid pattern index: ", pattern_index,
                            "pattern length", len(patterns))
        coord_map = self.patterns[pattern_index]["coordinate_map"]
        regex = self.patterns[pattern_index]["data"]
        if regex == None: return # nothing to match
        regex_name = os.path.basename(self.patterns[pattern_index]['filepath'])
        
        # All regexes except matchall
        if regex != re.compile('.'):
            
            # Add regex name to list if it isn't already in the list
            if self.time_profile:
                if regex_name not in self.regex_name_list:
                    self.regex_name_list.append(regex_name)
                # start timer
                start_time = time.time()
            if __debug__ and self.verbose:
                print("map_regex(): searching for pattern with index "
                      + str(pattern_index) + " \""
                      + self.patterns[pattern_index]["title"]
                      + "\" is " + str(regex))
            matches = regex.finditer(text)
            match_count = 0
            for m in matches:
                match_count += 1
                coord_map.add_extend(filename, m.start(),
                                     m.start()+len(m.group()))
                if __debug__ and self.verbose:
                    print(m)

            self.patterns[pattern_index]["coordinate_map"] = coord_map

        if self.time_profile:
            elapsed_time = time.time() - start_time
            if match_count > 0:
                elapsed_time = (elapsed_time/match_count)
            # Add time to current regex time profile
            self.current_regex_time_profile.append(elapsed_time)
        
        #### MATCHALL/CATCHALL ####
        elif regex == re.compile('.'):
            # Split note the same way we would split for set or POS matching
            matchall_list = re.split("(\s+)", text)
            matchall_list_cleaned = []
            for item in matchall_list:
                if len(item) > 0:
                    if item.isspace() == False:
                        split_item = re.split("(\s+)",
                                              re.sub(pre_process, " ", item))
                        for elem in split_item:
                            if len(elem) > 0:
                                matchall_list_cleaned.append(elem)
                    else:
                        matchall_list_cleaned.append(item)          

            start_coordinate = 0
            for word in matchall_list_cleaned:
                start = start_coordinate
                stop = start_coordinate + len(word)
                word_clean = re.sub(r"[^a-zA-Z0-9]+", "", word.lower().strip())
                if len(word_clean) == 0:
                    #got a blank space or something without any characters or digits, move forward
                    start_coordinate += len(word)
                    continue

                if regex.match(word_clean):
                    coord_map.add_extend(filename, start, stop)
                    
                #advance our start coordinate
                start_coordinate += len(word)

            self.patterns[pattern_index]["coordinate_map"] = coord_map


    def map_regex_context(self, filename="", text="", pattern_index=-1,
                          pre_process= r"[^a-zA-Z0-9]"):
        """ map_regex_context creates a coordinate map from combined regex 
            + PHI coordinates of all previously mapped patterns
        """

        punctuation_matcher = re.compile(r"[^a-zA-Z0-9*]")

        if pattern_index < 0 or pattern_index >= len(self.patterns):
            raise Exception("Invalid pattern index: ", pattern_index,
                            "pattern length", len(patterns))
        
        coord_map = self.patterns[pattern_index]["coordinate_map"]
        regex = self.patterns[pattern_index]["data"]
        if regex == None: return # nothing to match
        regex_name = os.path.basename(self.patterns[pattern_index]['filepath'])
        context = self.patterns[pattern_index]["context"]
        try:
            context_filter = self.patterns[pattern_index]["context_filter"]
        except KeyError:
            warnings.warn("deprecated missing context_filter field in filter "
                          + str(pattern_index)
                          + " of type regex_context, assuming \'all\'",
                          DeprecationWarning)
            context_filter = 'all'

        # Get PHI coordinates
        if context_filter == 'all':
            # current_include_map = self.get_full_include_map(filename)
            current_include_map = self.include_map
            # Create complement exclude map (also excludes punctuation)      
            full_exclude_map = current_include_map.get_complement(filename,
                                                                  text)
        else:
            context_filter_pattern_index = self.pattern_indexes[context_filter]
            full_exclude_map_coordinates = self.patterns[context_filter_pattern_index]['coordinate_map']
            full_exclude_map = {}
            for start,stop in full_exclude_map_coordinates.filecoords(filename):
                full_exclude_map[start] = stop


        # 1. Get coordinates of all include and exclude mathches

        punctuation_matcher = re.compile(r"[^a-zA-Z0-9*]")
        # 2. Find all patterns expressions that match regular expression
        
        # Keep track of time for regex time profiling
        # Add regex name to list if it isn't already in the list
        if self.time_profile:
            if regex_name not in self.regex_name_list:
                self.regex_name_list.append(regex_name)
            # Start timer
            start_time = time.time()
        if not regex == {}:
            matches = regex.finditer(text)
            match_count = 0
            for m in matches:
                match_count += 1
                
                # initialize phi_left and phi_right
                phi_left = False
                phi_right = False
                
                match_start = m.span()[0]
                match_end = m.span()[1]

                # PHI context left and right
                phi_starts = []
                phi_ends = []
                for start in full_exclude_map:
                    phi_starts.append(start)
                    phi_ends.append(full_exclude_map[start])
                    
                if match_start in phi_ends:
                    phi_left = True
                
                if match_end in phi_starts:
                    phi_right = True

                # Get index of m.group()first alphanumeric character in match
                tokenized_matches = []
                match_text = m.group()
                split_match = re.split("(\s+)",
                                       re.sub(pre_process, " ", match_text))

                # Get all spans of tokenized match (because remove() function requires tokenized start coordinates)
                coord_tracker = 0
                for element in split_match:
                    if element != '':
                        if not punctuation_matcher.match(element[0]):
                            current_start = match_start + coord_tracker
                            current_end = current_start + len(element)
                            tokenized_matches.append((current_start,
                                                      current_end))

                            coord_tracker += len(element)
                        else:
                            coord_tracker += len(element)

                ## Check for context, and add to coordinate map
                if ((context == "left" and phi_left == True)
                    or (context == "right" and phi_right == True)
                    or (context == "left_or_right"
                        and (phi_right == True or phi_left == True))
                    or (context == "left_and_right"
                        and (phi_right == True and phi_left == True))):
                    for item in tokenized_matches:
                        coord_map.add_extend(filename, item[0], item[1])

        # Stop time for time profiling
        if self.time_profile:
            elapsed_time = time.time() - start_time
            if match_count > 0:
                elapsed_time = (elapsed_time/match_count)
            self.current_regex_time_profile.append(elapsed_time)

        self.patterns[pattern_index]["coordinate_map"] = coord_map

    def match_all(self, filename="", text="", pattern_index=-1):
        """ Simply maps to the entirety of the file """
        ''' 
        if not os.path.exists(filename):
            raise Exception("Filepath does not exist", filename)
        '''
        if pattern_index < 0 or pattern_index >= len(self.patterns):
            raise Exception("Invalid pattern index: ", pattern_index,
                            "pattern length", len(patterns))

        coord_map = self.patterns[pattern_index]["coordinate_map"]
        #add the entire length of the file
        coord_map.add(filename, 0, len(text))
        self.patterns[pattern_index]["coordinate_map"] = coord_map


    def map_set(self, filename="", text="", pattern_index=-1,
                pre_process= r"[^a-zA-Z0-9]"):
        """ Creates a coordinate mapping of words any words in this set"""
        if pattern_index < 0 or pattern_index >= len(self.patterns):
            raise Exception("Invalid pattern index: ", pattern_index,
                            "pattern length", len(patterns))
        
        map_set = self.patterns[pattern_index]["data"]
        coord_map = self.patterns[pattern_index]["coordinate_map"]
        #get part of speech we will be sending through this set
        #note, if this is empty we will put all parts of speech through the set
        cleaned = self.get_clean(filename,text)[0]
        check_pos = False
        pos_set = set([])
        #print(map_set)
        if "pos" in self.patterns[pattern_index]:
            pos_set = set(self.patterns[pattern_index]["pos"])
        if len(pos_set) > 0:
            check_pos = True
            pos_list = self.get_pos(filename, cleaned)
        else:
            pos_list = zip(cleaned,range(len(cleaned)))
        start_coordinate = 0
        if __debug__ and self.verbose:
            print("map_set(): searching for pattern with index "
                  + str(pattern_index) + " \""
                  + self.patterns[pattern_index]["title"])

        for tup in pos_list:
            word = tup[0]
            pos  = tup[1]
            start = start_coordinate
            stop = start_coordinate + len(word)
            # This converts spaces into empty strings, so we know to skip forward to the next real word
            word_clean = re.sub(r"[^a-zA-Z0-9]+", "", word.lower().strip())
            if len(word_clean) == 0:
                #got a blank space or something without any characters or digits, move forward
                start_coordinate += len(word)
                continue
            if check_pos == False or (check_pos == True and pos in pos_set):
                if word_clean in map_set or word in map_set:
                    coord_map.add_extend(filename, start, stop)
                    if __debug__ and self.verbose:
                        print(word_clean + " " + word)
                else:
                    pass
            #advance our start coordinate
            start_coordinate += len(word)

        self.patterns[pattern_index]["coordinate_map"] = coord_map  

    def map_pos(self, filename="", text="", pattern_index=-1,
                pre_process= r"[^a-zA-Z0-9]"):
        """ Creates a coordinate mapping of words which match this part of speech (POS)"""
        '''
        if not os.path.exists(filename):
            raise Exception("Filepath does not exist", filename)
        '''
        if pattern_index < 0 or pattern_index >= len(self.patterns):
            raise Exception("Invalid pattern index: ", pattern_index,
                            "pattern length", len(patterns))

        if "pos" not in self.patterns[pattern_index]:
            raise Exception("Mapping POS must include parts of speech",
                            pattern_index, "pattern length", len(patterns))
            
        coord_map = self.patterns[pattern_index]["coordinate_map"]
        pos_set = set(self.patterns[pattern_index]["pos"])
        
        # Use pre-process to split sentence by spaces AND symbols, while preserving spaces in the split list

        cleaned = self.get_clean(filename,text)[0]

        pos_list = self.get_pos(filename, cleaned)#pos_list = nltk.pos_tag(cleaned)
        start_coordinate = 0
        for tup in pos_list:
            word = tup[0]
            pos  = tup[1]
            start = start_coordinate
            stop = start_coordinate + len(word)
            word_clean = re.sub(r"[^a-zA-Z0-9]+", "", word.lower().strip())
            if len(word_clean) == 0:
                #got a blank space or something without any characters or digits, move forward
                start_coordinate += len(word)
                continue

            if pos in pos_set:
                # The CD ("cardinal number") rule is a SAFE filter whose stated job is to keep any
                # number "not included in earlier steps" -- labs, vitals, doses. But it is the LAST
                # word on a number, so any identifier that every earlier PHI filter happened to miss
                # gets affirmatively marked safe by it. MEASURED: 81 identifiers (MRN, account and
                # insurance numbers) survived a 2,000-note sample this way, 3.87% of all long digit
                # runs. No lab value, vital, dose or score is a bare 7+ digit integer, so a run that
                # long is never the kind of number this rule exists to protect.
                if pos == "CD" and _POS_LONG_DIGITS.fullmatch(word_clean):
                    start_coordinate += len(word)
                    continue
                coord_map.add_extend(filename, start, stop)
                
            #advance our start coordinate
            start_coordinate += len(word)

        self.patterns[pattern_index]["coordinate_map"] = coord_map

    def map_ner(self, filename="", text="", pattern_index=-1, pre_process= r"[^a-zA-Z0-9]+"):
        """ map NER tagging"""
        '''    
        if not os.path.exists(filename):
            raise Exception("Filepath does not exist", filename)
        '''
        if pattern_index < 0 or pattern_index >= len(self.patterns):
            raise Exception("Invalid pattern index: ", pattern_index, "pattern length", len(patterns))

        #load and create an NER tagger if it doesn't exist
        if self.stanford_ner_tagger == None:
            classifier_path = self.stanford_ner_tagger_classifier #'/usr/local/stanford-ner/classifiers/english.all.3class.distsim.crf.ser.gz'
            jar_path = self.stanford_ner_tagger_jar #'/usr/local/stanford-ner/stanford-ner.jar'     
            self.stanford_ner_tagger = StanfordNERTagger(classifier_path,jar_path)
        
        coord_map = self.patterns[pattern_index]["coordinate_map"]
        pos_set = set([])
        if "pos" in self.patterns[pattern_index]:
            pos_set = set(self.patterns[pattern_index]["pos"])
        if len(pos_set) > 0:
            check_pos = True

        lst = re.split("(\s+)", text)
        cleaned = []
        for item in lst:
            if len(item) > 0:
                cleaned.append(item)
        
        ner_no_spaces = self.stanford_ner_tagger.tag(cleaned)
        #get our ner tags
        ner_set = {}
        for tup in ner_no_spaces:
            ner_set[tup[0]] = tup[1]
        ner_set_with_locations = {}
        start_coordinate = 0
        for w in cleaned:
            if w in ner_set:
                ner_set_with_locations[w] = (ner_set[w], start_coordinate)
            start_coordinate += len(w)


        #for the text, break into words and mark POS
        #with the parts of speech labeled, match any of these to our coordinate
        #add these coordinates to our coordinate map
        start_coordinate = 0
        for word in cleaned:

            word_clean = re.sub(pre_process, "", word.lower().strip())
            if len(word_clean) == 0:
                #got a blank space or something without any characters or digits, move forward
                start_coordinate += len(word)
                continue
            
            if word in ner_set_with_locations:
                ner_tag = ner_set_with_locations[word][0]
                start = ner_set_with_locations[word][1]
                if ner_tag in pos_set:
                    stop = start + len(word)
                    coord_map.add_extend(filename, start, stop)
                    print("FOUND: ",word, "NER: ", ner_tag, start, stop)
            
                    
            #advance our start coordinate
            start_coordinate += len(word)

        self.patterns[pattern_index]["coordinate_map"] = coord_map

    def folder_walk(self, folder):
        """ utility func will make a generator to walk a folder
            returns root_directory,filename

            for example: 
            foo/, bar001.txt
            foo/, bar002.txt

        """
        for root, dirs, files in os.walk(folder):
            for filename in files:
                yield root,filename

    def get_exclude_include_maps(self, filename, pattern, txt):
        coord_map = pattern["coordinate_map"]
        exclude = pattern["exclude"]
        try:
            filter_path = pattern["filepath"]
        except KeyError:
            filter_path = pattern["title"]
        if "phi_type" in pattern:
            phi_type = pattern["phi_type"]
        else:
            phi_type = "OTHER"

        for start,stop in coord_map.filecoords(filename):
            if (pattern['type'] != 'regex_context'
                and pattern['type'] != 'dynamic_set'
                and pattern['type'] != 'dynamic_regex'
                and pattern['type'] != 'dynamic_regex_context'):
                if exclude or exclude == "True":
                    if not self.include_map.does_overlap(filename, start, stop):
                        self.exclude_map.add_extend(filename, start, stop)
                        self.phi_type_dict[phi_type][0].add_extend(filename, start, stop)
                else:
                    if not self.exclude_map.does_overlap(filename, start, stop):
                        self.include_map.add_extend(filename, start, stop)
                        self.data_all_files[filename]["non-phi"].append({"start":start, "stop":stop, "word":txt[start:stop], "filepath":filter_path})
                    else:
                        pass 

            # Add regex_context to map separately
            else:
                if exclude:
                    self.exclude_map.add_extend(filename, start, stop)
                    #print(start)
                    self.include_map.remove(filename, start, stop)
                    self.phi_type_dict[phi_type][0].add_extend(filename, start, stop)
                else:
                    self.include_map.add_extend(filename, start, stop)
                    self.exclude_map.remove(filename, start, stop)
                    self.data_all_files[filename]["non-phi"].append({"start":start, "stop":stop, "word":txt[start:stop], "filepath":filter_path})

    def transform(self):
        """ transform
            turns input files into output PHI files 
            protected health information will be replaced by the replacement character

            transform the data 
            ORDER: Order is preserved prioritiy, 
            patterns at spot 0 will have priority over patterns at index 2 

            **Anything not caught in these passes will be assumed to be PHI
        """
        in_path = self.finpath
        out_path = self.foutpath

        if self.verbose:
            print("RUNNING TRANSFORM")

        if not os.path.exists(in_path):
            raise Exception("File input path does not exist", in_path)
        
        if not os.path.exists(out_path):
            raise Exception("File output path does not exist", out_path)

        #create our final exclude and include maps, priority order
        for root,f in self.folder_walk(in_path):
            #keeps a record of all phi coordinates and text for a given file            
            filename = root+f

            encoding = self.detect_encoding(filename)
            txt = open(filename,"r", encoding=encoding['encoding']).read()

            #now we transform the text
            fbase, fext = os.path.splitext(f)
            outpathfbase = out_path + fbase
            if self.outformat == "asterisk":
                with open(outpathfbase+".txt", "w", encoding='utf-8', errors='surrogateescape') as f:
                    contents = self.transform_text_asterisk(txt, filename)
                    f.write(contents)
                    
            elif self.outformat == "surrogate":
                with open(outpathfbase+".txt", "w", encoding='utf-8', errors='surrogateescape') as f:
                    contents = self.transform_text_surrogate(txt, filename)
                    f.write(contents)

            elif self.outformat == "i2b2":
                with open(outpathfbase+".xml", "w", errors='xmlcharrefreplace') as f: #TODO: should we have an explicit encoding?
                    contents = self.transform_text_i2b2(self.data_all_files[filename])
                    f.write(contents)
            else:
                raise Exception("Outformat not supported: ",
                                self.outformat)        

        if self.run_eval: #output our data for eval
            json.dump(self.data_all_files, open(self.coords, "w"), indent=4)

    # infilename needed for addressing maps
    def transform_text_asterisk(self, txt, infilename):        
        last_marker = 0
        current_chunk = []
        punctuation_matcher = re.compile(r"[^a-zA-Z0-9*]")

        #read the text by character, any non-punc non-overlaps will be replaced
        contents = []
        for i in range(0, len(txt)):

            if i < last_marker:
                continue
            
            if self.include_map.does_exist(infilename, i):
                #add our preserved text
                start,stop = self.include_map.get_coords(infilename, i)
                contents.append(txt[start:stop])
                last_marker = stop
            elif punctuation_matcher.match(txt[i]):
                contents.append(txt[i])
            else:
                contents.append("*")

        return "".join(contents)

    def transform_text_surrogate(self, txt, infilename):
        """Surrogate output (hiding-in-plain-sight): DATE spans -> the real date SHIFTED by the
        patient's canonical offset (not asterisks); all other PHI -> asterisks; every non-PHI
        character (including whitespace and line breaks) is kept verbatim, so note formatting is
        preserved. Never emits an unshifted real date: an unparseable DATE span falls back to
        asterisks."""
        date_spans = {}
        for cm in self.phi_type_dict.get("DATE", []):
            for start, stop in cm.filecoords(infilename):
                date_spans[start] = stop
        name_spans = {}
        for cm in self.phi_type_dict.get("NAME", []):
            for start, stop in cm.filecoords(infilename):
                name_spans[start] = stop
        place_spans = {}
        for ptype in ("LOCATION", "TOWN"):
            for cm in self.phi_type_dict.get(ptype, []):
                for start, stop in cm.filecoords(infilename):
                    place_spans[start] = stop
        # KNOWN-NAME INJECTION: names from the structured record (patient + providers) are surrogated
        # by identity, not by guessing -> catches names the NER misses (e.g. non-Western names).
        known_spans = {}
        if not hasattr(self, "known_names"):
            self.known_names = {}
            import json as _json2
            kp = getattr(self, "known_names_path", None)
            if kp:
                try:
                    self.known_names = _json2.loads(open(kp, encoding="utf-8").read())
                except Exception:
                    self.known_names = {}
        _toks = self.known_names.get(os.path.basename(infilename), [])
        for _t in _toks:
            if len(_t) >= 2:
                for m in re.finditer(r"(?<![A-Za-z])" + re.escape(_t) + r"s?(?![A-Za-z])", txt, re.I):
                    if not m.group(0)[:1].isupper():      # a real name is Capitalized; a lowercase match is
                        continue                          # a common-word collision ("Will" the name vs "will")
                    known_spans[m.start()] = m.end()
        # multi-word place names ("Palo Alto", "Mountain View"): Philter often tags only the first token,
        # leaving the dictionary-word second token ("Alto"/"View") to be restored by the guard. Extend a
        # place span over up to two immediately-following Capitalized words so the whole city is faked as a
        # unit -- but never absorb an all-caps state abbrev or a known/NER-tagged name.
        for _st in list(place_spans):
            _sp = place_spans[_st]
            for _ in range(2):
                _m = re.match(r"[ \t]+([A-Z][a-z]+)\b", txt[_sp:])
                if not _m:
                    break
                _ns = _sp + _m.start(1)
                if _ns in known_spans or _ns in name_spans:
                    break
                _sp = _sp + _m.end()
                place_spans[_st] = _sp
        # CITY, STATE ZIP address pattern ("Palo Alto CA 94304"): treat the 1-3 word city as a LOCATION
        # unit even when Philter mis-tagged its first token as a NAME -- so the whole city is faked as a
        # place (not "Sietsma Alto"). The city's tokens then override any NAME tag / dictionary-restore.
        for _m in re.finditer(r"\b([A-Z][a-z]+(?:[ \t][A-Z][a-z]+){0,2}),?[ \t]+[A-Z]{2}[ \t]+\d{5}\b", txt):
            place_spans[_m.start(1)] = _m.end(1)
            for _ks in [k for k in name_spans if _m.start(1) <= k < _m.end(1)]:
                del name_spans[_ks]                     # city word wrongly tagged as a name -> drop the name tag
        place_char = set()                              # every position inside a place span (used by the guard)
        for _ps, _pe in place_spans.items():
            place_char.update(range(_ps, _pe))
        try:
            import surrogate_names
        except Exception:
            surrogate_names = None
        try:
            import surrogate_places
        except Exception:
            surrogate_places = None
        shift_days = int(self.date_shifts.get(os.path.basename(infilename),
                                              self.default_date_shift))
        # WHITELIST-RESTORE GUARD (case-aware): keep a PHI-tagged word whose text is known-safe,
        # regardless of Philter's internal include/exclude precedence. Names in clinical notes are
        # capitalized mid-sentence, so: a general dictionary word is restored only when lowercase or
        # sentence-initial (never a mid-sentence Capitalized token, which could be a name); a CURATED
        # medical term (biomarker/organism/lab word) is restored in any case. Fast: sets load once.
        if not hasattr(self, "_safe_lower"):
            import json as _json, os as _os
            base = _os.path.dirname(_os.path.abspath(__file__))
            def _load(fn):
                try:
                    d = _json.loads(open(_os.path.join(base, fn), encoding="utf-8").read())
                    return set(k.lower() for k in (d.keys() if isinstance(d, dict) else d))
                except Exception:
                    return set()
            self._safe_lower = _load("filters/whitelists/whitelist_stanford_medical.json")
            # Words that at least five different patients' radiology reports write in lower case and that
            # are on no name list: report vocabulary, including recurrent misspellings.
            self._safe_lower |= _load("filters/whitelists/whitelist_radiology_corpus.json")
            self._report_index = _near_index(_load("filters/whitelists/report_vocabulary_common.json"))
            self._safe_any = _load("filters/whitelists/whitelist_medical_anycase.json")
            # PUT-BACK LIST: clinical vocabulary that is ALSO a surname (senna, graves, crohn,
            # parkinson, gravis — 6,897 terms from OMOP Drug/Condition intersected with the surname
            # blacklist). These cannot go in _safe_any (that would stop the SURNAME being redacted
            # anywhere) and requiring positive clinical context does not work either: MEASURED, 39 of
            # 42 missed "Senna" occurrences were bare medication-list entries with no dose and no
            # clinical noun to trigger on ("Senna, Colace, Dulcolax"). So the default is inverted for
            # this set only: put the term BACK unless the surrounding text says it is a person.
            self._safe_ambig = _load("filters/whitelists/whitelist_medical_ambiguous.json")
            # Real provider surnames. Loaded here so the keep-rules below can refuse to restore
            # one: a signature block is all-caps and would otherwise be kept as an "acronym".
            self._provider = _load("filters/blacklists/provider_roster.json")
            # Second tier: clinical eponyms that are ALSO common GIVEN names (Peter, Thomas). Restoring
            # these by default leaks a real name from any sentence without an honorific to veto on
            # ("Peter reports chest pain"), so they need POSITIVE clinical context to come back.
            self._safe_ambig_strict = _load("filters/whitelists/whitelist_medical_ambiguous_strict.json")
            # English stopwords / function words are NEVER PHI, but the medical dictionary omits many
            # short ones ("of", "to", "in"); without this, a function word Philter mis-tags (e.g. the
            # "of" in an "X of Y" org pattern) falls through to the fake-name fallback ("of" -> "edele").
            _stop = {"of","to","in","is","as","or","an","on","by","at","it","be","he","we","do","if","so",
                     "no","up","my","me","us","the","and","for","are","was","not","but","has","had","his",
                     "her","she","him","its","our","out","who","you","all","any","can","did","been","were",
                     "with","that","this","from","they","have","will","would","there","their","which","when",
                     "what","your","them","then","than","into","over","such","only","some","more","most",
                     "other","about","after","before","between","during","while","because","depending"}
            try:
                from nltk.corpus import stopwords as _sw
                _stop |= set(_sw.words("english"))
            except Exception:
                pass
            self._safe_lower |= _stop
            # "safe" regexes (BP/ratios, genomic coordinates, age) are Philter include-rules, but the
            # surrogate guard cannot rely on Philter's include/exclude precedence -- so apply them here
            # directly. Same clinical values Philter's date/id patterns would otherwise over-redact.
            self._safe_rx = []
            for _rf in ("clinical_ratios_safe.txt", "genomic_coords_safe.txt",
                        "age_demographic_safe.txt", "bp_safe.txt"):
                try:
                    self._safe_rx.append(re.compile(
                        open(_os.path.join(base, "filters/regex/safe", _rf), encoding="utf-8").read().strip()))
                except Exception:
                    pass
            # census name set: the gate for restoring Capitalized dictionary words. A word that is
            # also a name (Vera/Yang/Rishi) stays redacted even when capitalized; a common word that
            # is NOT a name (Laboratory/Negative/Impression) is restored in any case.
            self._first = _load("filters/blacklists/firstnames_minus_fps.json")
            self._names = self._first | _load("filters/blacklists/lastnames_minus_fps.json")
            self._provider -= _ROSTER_NOT_PEOPLE
            self._safe_lower |= _REPORT_WORDS
            self._safe_ambig |= _REPORT_AMBIGUOUS
        safe_char = set()
        if self._safe_lower or self._safe_any:
            def _restore(tok, start, end):
                # restore if: a curated medical term (any case); OR a lowercase dictionary word (never a
                # name in that use); OR a Capitalized dictionary word that is NOT a known name -- common
                # report words like "Laboratory"/"Negative"/"Impression" must survive, but a word that
                # doubles as a name (Vera/Yang) stays redacted (and structured names are surrogated).
                w = re.sub(r"[^a-z0-9]", "", tok.lower())
                if not w:
                    return
                # NAMED: a Capitalized word in a signature line, right after an honorific, or at the
                # start of a name that ends in a credential. That is a person, so no keep-list applies.
                if tok[0].isupper() and _is_named(txt, start, end, w, self._safe_lower):
                    return
                if w in self._safe_any:                          # curated medical term -> keep even in a place
                    safe_char.update(range(start, end)); return
                # PERSON VETO — evaluated BEFORE any put-back, in EITHER case. Notes are full of
                # lowercase names ("seen by smith", "d/w mr. johnson"), and the put-back list is mostly
                # eponyms that are also very common surnames (Stevens-Johnson, Smith-Lemli-Opitz), so a
                # lowercase put-back with no veto restored the real surname verbatim. MEASURED: 409
                # restorations with personal context present in a 2,000-note sample.
                _before = txt[max(0, start - 24):start]
                _after = txt[end:end + 28]
                _is_person = bool(_CTX_PERSON_BEFORE.search(_before) or (
                    _CTX_PERSON_AFTER.match(_after)
                    and not _CTX_ANATOMY_AFTER.match(_after)                      # "Chiari II malformation"
                    and not (w in _NUMBERED_EPONYMS and _CTX_TYPE_NUMBER.match(_after))))   # "Chiari II", "Fazekas 2"
                # The colour gray and the T2 star sequence, by what stands next to them.
                if tok[0].isupper() and not _is_person and (
                        (w in ("gray", "grey") and _CTX_GRAY_AFTER.match(_after))
                        or (w == "star" and _CTX_T2_BEFORE.search(_before))):
                    safe_char.update(range(start, end)); return
                # Anatomy named after a person. Checked before the place rule below, because "in Meckel's
                # cave" and "at Wernicke's area" look like addresses to the place tagger.
                if tok[0].isupper() and not _is_person and (
                        _CTX_ANATOMY_BEFORE.search(txt[max(0, start - 40):start])
                        or _CTX_ANATOMY_AFTER.match(_after)
                        or _has_eponym_partner(w, _before, _after)
                        or (w in _NUMBERED_EPONYMS and _CTX_TYPE_NUMBER.match(_after))
                        # a curated eponym followed by a clinical noun, before "in Wilson" is taken for a place
                        or ((w in getattr(self, "_safe_ambig_strict", ()) or w in getattr(self, "_safe_ambig", ()))
                            and _clin_after(_after))):
                    safe_char.update(range(start, end)); return
                # PUT-BACK (any case): clinical vocabulary that doubles as a surname. Applied before the
                # capitalisation logic because these terms appear lowercase too ("myasthenia gravis").
                if w in getattr(self, "_safe_ambig", ()) and tok[0].islower() and not _is_person:
                    safe_char.update(range(start, end)); return
                if start in place_char:                          # a dictionary word INSIDE a place span
                    return                                       # ("Alto" of "Palo Alto") -> let it be faked
                if w in self._safe_lower and (tok[0].islower() or w not in self._names):
                    safe_char.update(range(start, end)); return
                if tok.islower() and not _is_person and _has_eponym_partner(w, _before, _after):
                    safe_char.update(range(start, end)); return               # "dandy walker malformation"
                if (_PLURAL_ABBREV.match(tok) and not _is_person and w[:-1] not in self._names
                        and w[:-1] not in getattr(self, "_provider", ())):
                    safe_char.update(range(start, end)); return               # "MCAs", "MRAs", "CTs"
                # Lower-case words only: a medical ending, or a near-miss spelling of a common report word.
                if tok.islower() and not _is_person and w not in self._names and (
                        (len(w) >= 8 and _MED_SUFFIX.search(w))               # "cystoventriculostomy"
                        or (len(w) >= 6 and _near_report_word(w, self._report_index))):   # "decrased", "subactue"
                    safe_char.update(range(start, end)); return
                if not _is_person and w not in self._names and (
                        tok.islower() or tok.isupper() or tok.istitle()):
                    _vocabs = (self._safe_lower, self._safe_any)
                    _title = tok.istitle() and not tok.isupper()               # sentence-initial: stricter test
                    if _is_medical_compound(w, _vocabs, strict=_title):        # "multisequence", "retrodental"
                        safe_char.update(range(start, end)); return
                # CONTEXT RULE — the token is Capitalized AND doubles as a surname, so the lists above
                # deliberately refused it. Put it back only if the surrounding text is clinical.
                if tok[0].isupper():
                    if _is_person:
                        return                                   # "Dr. Senna", "Senna, MD" -> person
                    # A common word at the start of a sentence has its capital letter from where it stands:
                    # "Lack of flow void." (which became "Caycho of flow void"), "Near complete resolution.",
                    # "Coil mass is stable.", "CHEST: Ports and devices: None." It is put back when it is a
                    # dictionary word, on no first-name list and not on the roster, the next word is in lower
                    # case, and nothing says a person is acting ("Best was notified"). First names stay
                    # replaced there ("Will reports feeling better"), which is why "Will follow up" does too.
                    if (tok.istitle() and len(w) >= 3 and w in self._safe_lower and w not in self._first
                            and w not in getattr(self, "_provider", ())
                            and _CTX_SENTENCE_START.search(txt[max(0, start - 12):start])
                            and _CTX_LOWER_NEXT.match(_after) and not _CTX_PERSON_VERB.match(_after)):
                        safe_char.update(range(start, end)); return
                    if w in self._safe_lower and (_clin_after(_after)
                                                  or _CTX_MED_AFTER.match(_after)
                                                  or _CTX_MED_WINDOW.search(_after)):
                        safe_char.update(range(start, end)); return   # "Senna 8.6 mg", "Bell palsy"
                    # PUT-BACK: known clinical term that doubles as a surname. Restore unless the
                    # context marks a person. The patient's real name is separately injected from the
                    # structured record, so this does not expose the patient's own surname.
                    _is_prov = w in getattr(self, "_provider", ())
                    if w in getattr(self, "_safe_ambig", ()) and not _is_prov:
                        safe_char.update(range(start, end)); return
                    # Name-dominant tier: only with positive clinical evidence after the token
                    # ("Thomas splint", "Stevens-Johnson syndrome"). Never restored on its own.
                    # A token on the PROVIDER ROSTER needs STRONGER evidence than an ordinary
                    # put-back term. _CTX_MED_AFTER fires on any following DIGIT, so "Thomas 3546"
                    # (a name and a pager number) counted as clinical context and restored a real
                    # provider name: 620 of 827 surviving provider occurrences came back this way.
                    # A bare digit is not clinical evidence -- a named clinical noun or a dose with a
                    # unit is.
                    if (w in getattr(self, "_safe_ambig_strict", ()) or w in getattr(self, "_safe_ambig", ())) and (
                            _clin_after(_after) or _CTX_MED_WINDOW.search(_after)
                            or (_CTX_MED_AFTER.match(_after) and not _is_prov)):
                        safe_char.update(range(start, end)); return
                    if _CTX_CAMEL_MED.match(tok) and w not in self._names:
                        safe_char.update(range(start, end)); return   # "AlkPhos", "HgbA1c"
            # pass A: the whole hyphen/apostrophe compound as blessed (Ki-67, Ber-EP4, tilt-table).
            for m in re.finditer(r"[A-Za-z][A-Za-z0-9'\-]*", txt):
                _restore(m.group(0), m.start(), m.end())
            # pass B: each alphanumeric sub-run, so a compound of ordinary words has each part restored
            # (Laboratory-developed -> Laboratory + developed) with the hyphen kept as punctuation.
            for m in re.finditer(r"[A-Za-z][A-Za-z0-9]*", txt):
                _restore(m.group(0), m.start(), m.end())
            # clinical-ratio / genomic / age "safe" regexes -> keep (BP 152/64, 15q11.2, 66F...)
            for _rx in getattr(self, "_safe_rx", []):
                for m in _rx.finditer(txt):
                    safe_char.update(range(m.start(), m.end()))
            # SIG / FREQUENCY ABBREVIATIONS -> keep. Same failure mode as doses: philter tags the
            # trailing letters and surrogate mode swaps them for a fake NAME. MEASURED: "q8hr" was
            # destroyed in 158/158 notes ("Tylenol 975 q8hr" -> "q8woldeyohannes"), while "q8h"
            # survived - the difference is which fragment got tagged, so cover every form.
            for m in re.finditer(
                    r"(?i)\b(?:q\s*\d+\s*(?:h|hr|hrs|hour|hours|d|day|days|min|mins|wk|week|weeks)"
                    r"|q\.?[dhw]|qod|qhs|qam|qpm|qac|qpc|bid|tid|qid|prn|ac|pc|po|pr|iv|im|sc|sq|sl|ng|"
                    r"npo|gtt|gtts|ud|stat|x\s*\d+\s*(?:d|days|wk|weeks|mo|months))\b", txt):
                safe_char.update(range(m.start(), m.end()))

            # DOSES AND MEASUREMENTS -> keep the NUMBER AND ITS UNIT.
            # Surrogate mode replaces an un-typed number with a plausible DIFFERENT number, which is
            # right for an MRN and dangerous for a dose: measured on a 2,000-note sample, 13.8% of notes
            # had an altered dose (2.88% of all dose expressions), including 5 -> 25 and 1 -> 25. That is
            # a silent five-fold error written into the de-identified record with nothing to flag it.
            # The pre-existing decimal rule below covers "2.5 mg" but NOT the integer form "2 mg",
            # which is why plain doses were corrupted while decimal ones survived.
            # THREE BOUNDS, each of which this pattern was missing, and together they made it keep
            # identifiers: unbounded `\s*` let the number reach a unit many characters away; the
            # single-letter units (g, L, d) matched the FIRST LETTER of an ordinary word; and the digit
            # count was unlimited. So "Acct: 12345678  Loc:" matched as "12345678  L" -- 8-digit account
            # number kept as a volume in litres, and the stolen "L" left "oc" to be surrogated into a
            # fake name. MEASURED: this single pattern accounted for the bulk of the 81 identifiers
            # surviving a 2,000-note sample. A dose is at most a few digits, sits next to its unit, and
            # the unit ends at a non-letter.
            for m in re.finditer(
                    r"(?<![\w.])\d{1,6}(?:\.\d+)?(?:\s{0,3}[-–/]\s{0,3}\d{1,6}(?:\.\d+)?)*\s{0,3}"
                    r"(?:mcg|mg|µg|ug|kg|gm|g|mL|ml|L|cc|units?|iu|mmols|mmol|meq|mEq|ng|pg|mmHg|%)"
                    r"s?(?![A-Za-z])"
                    r"(?:\s{0,3}/\s{0,3}(?:hr|hrs|h|hour|kg|day|d|min|wk|week|m2|dose|24h)\b)?", txt, re.I):
                safe_char.update(range(m.start(), m.end()))
            # clinical measurements -> keep: dimensions ("1.5 x 2.0", "15x10x8"), standalone decimals
            # ("2.5", dose/size), and 0-10 pain/severity scores ("7/10").
            for m in re.finditer(r"(?<![\w.])\d+(?:\.\d+)?(?:\s*[xX]\s*\d+(?:\.\d+)?){1,2}(?![\w])|"
                                 r"(?<![\d.])\d{1,3}\.\d{1,2}(?![\d.])|"
                                 r"(?<![\d/])(?:10|[0-9])/10(?![\d/])", txt):
                safe_char.update(range(m.start(), m.end()))
            # units of measure are never PHI -> keep (protects "0.8 cm", "500 mg" from the surrogate
            # fallback that would otherwise turn a stray-tagged unit into a fake word).
            for m in re.finditer(r"(?<![A-Za-z])(?:mm|cm|km|kg|mg|mcg|ug|ng|ml|dl|cc|oz|lb|iu|meq|mmol|"
                                 r"mmHg|mmhg|bpm|rpm|Hz|kV|mV|nm|um|Gy|cGy|mCi|kcal|cm2|mm2|mm3|cm3)"
                                 r"s?(?![A-Za-z])", txt):
                safe_char.update(range(m.start(), m.end()))
            # references to images -> keep the numbers
            for m in _IMAGE_WORD.finditer(txt):
                safe_char.update(range(m.start(1), m.end(1)))
            for m in _IMAGE_PAIRS.finditer(txt):
                _one_date = (m.group(0).count("/") == 1 and "-" not in m.group(0)
                             and 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31)
                if not (_one_date and _DATE_WORD_BEFORE.search(txt[max(0, m.start() - 14):m.start()])):
                    safe_char.update(range(m.start(), m.end()))
            # radiotracers ("18F-FDG", "F-18 florbetapir", "99mTc-HMPAO") -> keep
            for m in _RADIOTRACER.finditer(txt):
                safe_char.update(range(m.start(), m.end()))
            # "T1weighted", written without its space -> keep
            for m in re.finditer(r"\bT[12][ \t-]?weighted\b", txt, re.I):
                safe_char.update(range(m.start(), m.end()))
            # a unit named after a person, after a number ("3 Tesla", "20 Gray") -> keep
            for m in _UNIT_EPONYM.finditer(txt):
                if m.start(1) not in known_spans:
                    safe_char.update(range(m.start(1), m.end(1)))
            # bare 1-3 digit numbers are vitals/labs/counts, never identifying -> keep (protects tables)
            for m in re.finditer(r"(?<![\w./])\d{1,3}(?![\w./])", txt):
                safe_char.update(range(m.start(), m.end()))
            # all-caps clinical acronyms (QSART, SBP, DBP, HRDB, UCNS...) -> keep, UNLESS the token is a
            # known name (those are surrogated first via known_spans, so this can't keep a known name)
            # ...or a PROVIDER on the roster. A signature block ("KRYSTAL DIMAYA, RN") is all-caps, so
            # this rule was re-keeping provider names the roster filter had correctly tagged: 1,055 of
            # the 2,109 surviving provider-name occurrences on the 10k batch were all-caps.
            # ...or stands where a person is named. In a report written in capitals a surname looks
            # like any other word, so only its place tells: "DISCUSSED WITH DR. ZELBRECHT AT 1430",
            # "RADIOLOGIST: ZELBRECHT, MD". The roster cannot help for a site it does not cover.
            for m in re.finditer(r"\b[A-Z][A-Z0-9]{1,}\b", txt):
                if m.start() not in known_spans and m.group(0).lower() not in getattr(self, "_provider", ()):
                    if (_is_named(txt, m.start(), m.end(), m.group(0).lower(), self._safe_lower)
                            or (m.group(0).lower() not in _SIGNATURE_NOT_NAMES
                                and _CTX_CREDENTIAL_AFTER.match(txt[m.end():m.end() + 16]))):
                        continue
                    safe_char.update(range(m.start(), m.end()))
            # stat / measurement / directional abbreviations written WITH a trailing period ("Max.",
            # "Min.", "Post.") are labels, not names -- keep them (the period disambiguates from the
            # bare name "Max"; and an actual patient named Max is surrogated first via known_spans).
            _statabbr = (r"\b(?:Max|Min|Avg|Med|Std|Sup|Inf|Ant|Post|Lat|Prox|Dist|Sag|Cor|Vol|Freq|"
                         r"Amp|Dur|Ref|Est|Approx|Rel|Abs|Sys|Dia|Dias|Seg|Ext|Flex|Vel|Grad|Circ|"
                         r"Diam|Len|Wt|Ht|Temp|Resp|Vert|Horiz|Obl|Trans|Long|Sup|Ing|Calc)\.")
            for m in re.finditer(_statabbr, txt):
                if m.start() not in known_spans:
                    safe_char.update(range(m.start(), m.end() - 1))   # keep the letters (period is punctuation)
            # mixed-case technical tokens with an internal capital (iSK, mRNA, pTNM, cT2, HbA1c) are
            # medical shorthand, never personal names -> keep.
            for m in re.finditer(r"\b[a-z][A-Z][A-Za-z0-9]*\b", txt):
                if m.start() not in known_spans:
                    safe_char.update(range(m.start(), m.end()))
        # ACCESSION / SPECIMEN NUMBERS are PHI identifiers. Match the WHOLE prefix-YY-NNNNN (or PREFIXdigits)
        # shape as a unit BEFORE tokenizing, and replace it with a deterministic FAKE accession of the same
        # shape — so the all-caps prefix guard can't keep the prefix, and no asterisks are emitted.
        accession = re.compile(r"\b(?:[A-Z]{2,5}\d{0,2}-\d{2}-\d{3,7}|[A-Z]{2,5}\d{2}-\d{4,8}|"
                               r"(?:MSUR|MSR|CYG|CY|SH[A-Z]|SP|SN|OS|CB)\d{6,10})\b")
        accession_spans = {}
        for m in accession.finditer(txt):
            accession_spans[m.start()] = m.end()
        # LONG DIGIT RUNS -> DEFAULT DENY. Everything else about numbers here is default-ALLOW: a digit
        # run survives unless some filter affirmatively tags it, and the CD part-of-speech rule exists
        # precisely to keep "any number not included in earlier steps". Every identifier leak measured on
        # the sample was a different way of failing to tag one -- parenthesised, behind an unusual label,
        # or swallowed by the dose rule. Inverting the default closes the class instead of one shape at a
        # time: no lab value, vital sign, dose or score is a bare 7+ digit integer, so such a run is an
        # identifier unless it is one of the few genuinely numeric clinical notations, exempted here and
        # covered by bench cases.
        # PROVIDER NAMES -> replaced deterministically, like the patient's own name. Tagging them via a
        # filter was not enough: the tag kept losing to other rules. Traced on the 10k batch, the 723
        # surviving occurrences were kept by SIX different mechanisms -- "Whitelist 1" marking them safe
        # (87), safe phrase filters whose coordinate ranges merge with adjacent text and swallow the name
        # ("ordering md safe", "young safe", "MyChart"), and spans correctly tagged PHI that later rules
        # restored. Chasing them one at a time was not converging, so a roster name is now handled at the
        # top of the loop where nothing downstream can put it back.
        # ONLY capitalised occurrences: a roster surname that is also an ordinary word must stay readable
        # in prose ("young adult" vs "Dr. Young"), which is the same rule philter applies to names.
        provider_spans = {}
        _prov = getattr(self, "_provider", ())
        if _prov:
            _prov_med = (getattr(self, "_safe_any", set()) | getattr(self, "_safe_ambig", set())
                         | getattr(self, "_safe_ambig_strict", set()))
            _unit_starts = {u.start(1) for u in _UNIT_EPONYM.finditer(txt)}
            for m in re.finditer(r"\b[A-Z][A-Za-z'\-]{2,}\b", txt):
                _w = m.group(0).lower().strip("'-")
                if _w not in _prov or m.start() in _unit_starts:        # "3 Tesla" is the unit
                    continue
                # A roster surname in an anatomical phrase is anatomy: "circle of Willis", "vein of Galen".
                if (_CTX_ANATOMY_BEFORE.search(txt[max(0, m.start() - 40):m.start()])
                        or _CTX_ANATOMY_AFTER.match(txt[m.end():m.end() + 28])
                        or _has_eponym_partner(_w, txt[max(0, m.start() - 24):m.start()], txt[m.end():m.end() + 28])
                        or (_w in _NUMBERED_EPONYMS and _CTX_TYPE_NUMBER.match(txt[m.end():m.end() + 28])
                            and not _CTX_PERSON_BEFORE.search(txt[max(0, m.start() - 24):m.start()]))):
                    continue
                # A roster surname that is ALSO curated clinical vocabulary keeps its clinical sense:
                # "Thomas splint" is a splint even when a Dr. Thomas exists. Caught by the bench, which
                # is the same failure as a patient named Foley losing "Foley catheter".
                if _w in _prov_med:
                    _aft = txt[m.end():m.end() + 28]
                    if _clin_after(_aft) or _CTX_MED_WINDOW.search(_aft):
                        continue
                provider_spans[m.start()] = m.end()
        longnum_spans = {}
        _exempt = set()
        for _rx in getattr(self, "_safe_rx", []):          # genomic coordinates / ISCN / ratios
            for m in _rx.finditer(txt):
                _exempt.update(range(m.start(), m.end()))
        # NDC drug codes (exactly 11 digits) are deliberately NOT exempted. An account or member number
        # can also be 11 digits, so exempting the length to preserve a product code would reopen the leak
        # class this rule exists to close. An NDC becoming a different 11-digit number costs little --
        # the drug name sits next to it in the text -- while a kept identifier is unrecoverable.
        for m in re.finditer(r"(?<![\d.\-/])\d{7,}(?![\d.])", txt):
            if m.start() not in accession_spans and m.start() not in _exempt:
                longnum_spans[m.start()] = m.end()
        # PHONE / FAX numbers -> a deterministic FAKE phone (hiding in plain sight), matched as a WHOLE
        # unit before the bare-number guard can keep the area code / exchange.
        phone = re.compile(r"(?:\+?1[-.\s])?(?:\(\d{3}\)\s?|\d{3}[-.\s])\d{3}[-.\s]\d{4}\b")
        phone_spans = {}
        for m in phone.finditer(txt):
            phone_spans[m.start()] = m.end()
        # EMAIL -> deterministic fake email (fake domain, generic mailboxes like noreply kept).
        email = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
        email_spans = {}
        for m in email.finditer(txt):
            email_spans[m.start()] = m.end()
        # WEBSITE / URL -> deterministic fake domain (TLD kept, path dropped). TLD-gated so it can't
        # swallow ordinary abbreviations ("e.g.", "St.John").
        url = re.compile(r"\b(?:https?://|www\.)?[a-zA-Z0-9][a-zA-Z0-9-]*(?:\.[a-zA-Z0-9-]+)*"
                         r"\.(?:com|org|net|edu|gov|io|co|us|info|health|care|clinic|md)(?:/[^\s]*)?",
                         re.I)
        url_spans = {}
        for m in url.finditer(txt):
            if m.start() not in email_spans:               # an email's domain is handled by email
                url_spans[m.start()] = m.end()
        # STATE abbreviation in a geographic context ("City, TX" / "TX 94305") -> fake state. Matched as
        # a unit here so the all-caps guard can't keep "TX" verbatim. Gated to comma-or-ZIP context.
        state_spans = {}
        if surrogate_places is not None:
            _STA = set(surrogate_places.STATE_ABBR)
            # 2-letter tokens that are BOTH state abbreviations and common medical credentials/roles: only
            # treat as a state when a ZIP anchors it (never swap "Kelly Lynn, MD" -> a fake state).
            _CRED = {"MD", "DO", "PA", "DC", "ND", "VA"}
            for m in re.finditer(r",[ \t]*([A-Z]{2})\b(?=[ \t]+\d{5}(?:-\d{4})?\b|[ \t]*(?:[\n;)]|$))", txt):
                if m.group(1) in _STA and m.group(1) not in _CRED:
                    state_spans[m.start(1)] = m.end(1)
            for m in re.finditer(r"\b([A-Z]{2})\b(?=[ \t]+\d{5}(?:-\d{4})?\b)", txt):
                if m.group(1) in _STA:
                    state_spans[m.start(1)] = m.end(1)
        # WRITTEN-OUT AND UNUSUAL NUMERIC DATES (see _WRITTEN_DATE above): found here as whole dates and
        # shifted by the per-note offset, before the roster, the keep rules or the name rules see them.
        word_date_spans, _dated = {}, set()
        for _rx in (_WEEKDAY_DATE, _DATE_RANGE, _WRITTEN_DATE, _DATE_NO_YEAR, _MONTH_ALONE, _SEASON_YEAR):
            for m in _rx.finditer(txt):
                _g = 0 if _rx is not _MONTH_ALONE else (1 if m.group(1) else 2)
                _a, _b = m.span(_g)
                if _dated.isdisjoint(range(_a, _b)):
                    word_date_spans[_a] = _b
                    _dated.update(range(_a, _b))
        punctuation_matcher = re.compile(r"[^a-zA-Z0-9*]")
        alpha_run = re.compile(r"[A-Za-z][A-Za-z'\-]*")
        digit_run = re.compile(r"\d+")
        contents = []
        i, N, last_marker = 0, len(txt), 0
        while i < N:
            if i < last_marker:
                i += 1
                continue
            if i in accession_spans:                                # accession/specimen id -> fake (wins over guard)
                stop = accession_spans[i]
                contents.append(self._fake_accession(txt[i:stop]))
                i = last_marker = stop; continue
            if i in word_date_spans:                                # WRITTEN / UNUSUAL DATE -> shifted date
                stop = word_date_spans[i]
                contents.append(self._shift_date_str(txt[i:stop], shift_days))
                i = last_marker = stop; continue
            if i in provider_spans and surrogate_names is not None:  # PROVIDER on the roster -> fake name
                stop = provider_spans[i]
                contents.append(surrogate_names.fake_name_span(txt[i:stop], txt[max(0, i-6):i]))
                i = last_marker = stop; continue
            if i in longnum_spans:                                  # 7+ digit run -> fake digits (deny)
                stop = longnum_spans[i]
                contents.append(self._fake_digits(txt[i:stop]))
                i = last_marker = stop; continue
            if i in phone_spans:                                    # PHONE -> fake phone
                stop = phone_spans[i]
                contents.append(self._fake_phone(txt[i:stop]))
                i = last_marker = stop; continue
            if i in email_spans:                                    # EMAIL -> fake email
                stop = email_spans[i]
                contents.append(self._fake_email(txt[i:stop]))
                i = last_marker = stop; continue
            if i in url_spans:                                      # WEBSITE -> fake domain
                stop = url_spans[i]
                contents.append(self._fake_url(txt[i:stop]))
                i = last_marker = stop; continue
            if i in state_spans and surrogate_places is not None:   # STATE abbrev -> fake state
                stop = state_spans[i]
                contents.append(surrogate_places._fake_state(txt[i:stop]) or txt[i:stop])
                i = last_marker = stop; continue
            if i in known_spans and surrogate_names is not None:    # KNOWN real name -> surrogate
                stop = known_spans[i]
                contents.append(surrogate_names.fake_name_span(txt[i:stop], txt[max(0, i-6):i]))
                i = last_marker = stop; continue
            if i in safe_char:                                      # known-safe medical/dict/number -> keep
                contents.append(txt[i]); i += 1; continue
            if self.include_map.does_exist(infilename, i):          # whitelisted/safe -> keep
                start, stop = self.include_map.get_coords(infilename, i)
                # an include range must never SWALLOW an identity-verified real name that falls inside it
                # (Philter may mark a signature-block surname "safe"); clamp to the next known-name start.
                # ...nor a long digit run: an include range STARTING before an identifier and ending after
                # it skips the loop past the default-deny check entirely (last_marker jumps over it),
                # which is how the final 6 leaks on the 2,000-note sample survived.
                # ...nor the start of a date found above.
                nxt = min((k for k in list(known_spans) + list(longnum_spans) + list(provider_spans)
                           + list(word_date_spans) if i < k < stop), default=stop)
                contents.append(txt[i:nxt]); i = last_marker = nxt; continue
            if i in date_spans:                                     # DATE -> shifted date
                stop = min([date_spans[i]] + [k for k in word_date_spans if i < k < date_spans[i]])
                seg = txt[i:stop]
                # "May represent artifact." The tagger reads the verb at the start of a sentence as the
                # month, and the verb became an invented date ("June 09, 2026 represent artifact"). A
                # month standing alone is a date only where a word of time stands before it, and those
                # places were taken above; what arrives here is the verb, unless it is tagged a name too.
                if (seg.strip().lower() == "may" and i not in name_spans
                        and re.match(r"[ \t]+[a-z]", txt[stop:stop + 4])
                        and not _CTX_PERSON_BEFORE.search(txt[max(0, i - 24):i])
                        and not _is_named(txt, i, stop, "may")):
                    contents.append(seg)
                else:
                    contents.append(self._shift_date_str(seg, shift_days))
                i = last_marker = stop; continue
            if i in name_spans and surrogate_names is not None:      # NAME -> gendered fake name
                stop = name_spans[i]
                # A name pattern can run on over words that are known to be safe: in a report written in
                # capitals the salutation patterns take "ZELBRECHT AT" from "DR. ZELBRECHT AT 1430" and
                # "QUELLORN WAS SEDATED" from "MR. QUELLORN WAS SEDATED". Replace the name only up to the
                # first safe word and let the walk keep that word, as it already does when the safe word
                # comes first.
                for j in range(i + 1, stop):
                    if j in word_date_spans or (j in safe_char and (j - 1) not in safe_char and not txt[j - 1].isalnum()):
                        stop = j
                        break
                seg = txt[i:stop]
                contents.append(surrogate_names.fake_name_span(seg.rstrip(), txt[max(0, i-6):i]) + seg[len(seg.rstrip()):])
                i = last_marker = stop; continue
            if i in place_spans and surrogate_places is not None:    # LOCATION/TOWN -> fake place
                stop = place_spans[i]
                contents.append(surrogate_places.fake_place_span(txt[i:stop]))
                i = last_marker = stop; continue
            # FALLBACK for PHI that matched no specific surrogate category: minimize asterisks -- replace
            # an alphabetic run with a fake proper noun and a digit run with fake digits, so the note
            # reads naturally (only genuinely un-typed leftovers ever become "*").
            am = alpha_run.match(txt, i)
            if am and surrogate_names is not None:
                tok = am.group(0)
                contents.append(surrogate_names.fake_name_span(tok, txt[max(0, i-8):i]))
                i = last_marker = am.end(); continue
            dm = digit_run.match(txt, i)
            if dm:
                contents.append(self._fake_digits(dm.group(0)))
                i = last_marker = dm.end(); continue
            ch = txt[i]
            contents.append(ch if punctuation_matcher.match(ch) else "*")
            i += 1
        return "".join(contents)

    # -- deterministic "hiding in plain sight" surrogates for contact identifiers (phone/email/website).
    #    Same input -> same fake, keyed by SURROGATE_SALT; not reversible without the salt.
    _DOMWORDS = ("bright river summit cedar harbor maple valley crest haven ridge grove brookside "
                 "lakeside parkview meadow stonegate pinehurst vista clearwater fairmont").split()
    _DOMSUFX = "care health clinic medical group wellness family associates partners center".split()

    def _sur_digest(self, tag, s):
        import hmac, hashlib, os
        salt = os.environ.get("SURROGATE_SALT", "philter-surrogate-v1").encode()
        return hmac.new(salt, (tag + ":" + s.lower()).encode("utf-8", "ignore"), hashlib.sha256).digest()

    def _fake_phone(self, orig):
        """Replace a phone/fax with a deterministic fake, preserving the exact punctuation/format and
        forcing valid-looking NPA/NXX leading digits (2-9)."""
        h = self._sur_digest("phone", orig)
        digs = [str(b % 10) for b in h]
        out, di = [], 0
        for ch in orig:
            if ch.isdigit():
                d = digs[di % len(digs)]
                if di in (0, 3) and d in "01":            # area code / exchange must not start 0 or 1
                    d = str(2 + (h[di] % 8))
                out.append(d); di += 1
            else:
                out.append(ch)
        return "".join(out)

    def _fake_digits(self, s):
        """Deterministic fake digit string, same length as `s` (for un-typed numeric identifiers that
        would otherwise be asterisked)."""
        h = self._sur_digest("id", s)
        return "".join(str(h[k % len(h)] % 10) for k in range(len(s)))

    def _fake_accession(self, orig):
        """Deterministic fake accession/specimen id -- fakes letters and digits, keeps the exact format
        (separators, case), so PREFIX-YY-NNNNN reads as a plausible accession instead of asterisks."""
        h = self._sur_digest("acc", orig)
        out, k = [], 0
        for ch in orig:
            if ch.isdigit():
                out.append(str(h[k % len(h)] % 10)); k += 1
            elif ch.isalpha():
                c = h[k % len(h)] % 26
                out.append(chr(ord('A') + c) if ch.isupper() else chr(ord('a') + c)); k += 1
            else:
                out.append(ch)
        return "".join(out)

    def _fake_domain(self, label):
        h = self._sur_digest("dom", label)
        w1 = self._DOMWORDS[int.from_bytes(h[0:4], "big") % len(self._DOMWORDS)]
        w2 = self._DOMSUFX[int.from_bytes(h[4:8], "big") % len(self._DOMSUFX)]
        return w1 + w2

    def _fake_email(self, orig):
        import re as _re
        m = _re.match(r"([^@]+)@(.+)", orig)
        if not m:
            return "*" * len(orig)
        local, domain = m.group(1), m.group(2)
        tld = domain.rsplit(".", 1)[1] if "." in domain else "com"
        generic = {"noreply", "no-reply", "donotreply", "info", "admin", "support",
                   "contact", "help", "office", "billing", "scheduling"}
        loc = local if local.lower() in generic else self._DOMWORDS[
            int.from_bytes(self._sur_digest("eml", local)[0:4], "big") % len(self._DOMWORDS)]
        return loc + "@" + self._fake_domain(domain) + "." + tld

    def _fake_url(self, orig):
        import re as _re
        m = _re.match(r"(https?://|www\.)?(.*)", orig, _re.I)
        scheme, rest = (m.group(1) or ""), m.group(2)
        host = _re.match(r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*", rest).group(0)
        tld = host.rsplit(".", 1)[1] if "." in host else "com"
        return scheme + self._fake_domain(host) + "." + tld   # path dropped (may carry portal tokens)

    def _shift_date_str(self, orig, shift_days):
        """Return `orig` (a detected date) shifted by shift_days, rendered in the same numeric format
        where possible. A shift of 0 is a LEGITIMATE per-patient offset (the offset is secret and drawn
        including 0): the output date is then the real date but is indistinguishable from any shifted
        one, so we emit it normally -- we do NOT asterisk it, because asterisking would MARK the shift=0
        patients and thereby leak a clue about all their other dates. Unparseable dates fall back below.

        The result says as much as the original and no more: a month and year stay a month and year,
        a month and day gain no year, a month alone stays a month. Nothing depends on the day the
        program runs. (Before, "3/2019" became "April 09, 2019" and "March 4" became "April 03, 2026",
        with the day and the year taken from the date of the run.)"""
        import datetime as _dt
        s = orig.strip()
        delta = _dt.timedelta(days=shift_days)
        put = lambda new: orig.replace(s, new) if s in orig else new      # noqa: E731
        # A weekday before the date: shift the date, then name the weekday of the shifted date in the
        # same style ("Mon" or "Monday"). Without a year the weekday cannot be known, and is dropped.
        wd = _WEEKDAY_FIRST.match(s)
        if wd and len(s) > wd.end():
            rest = s[wd.end():]
            new = self._shift_date_str(rest, shift_days)
            when = None
            for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%B %d, %Y"):
                try:
                    when = _dt.datetime.strptime(new, fmt)
                    break
                except ValueError:
                    continue
            if when is None:
                return put(new)
            name = when.strftime("%A")
            name = name if len(wd.group(1)) > 5 else name[:3]           # "Monday" stays long, "Mon" or "Thurs" short
            return put((name.upper() if wd.group(1).isupper() else name) + wd.group(2) + new)
        m = _DATE_RANGE.fullmatch(s)
        if m:
            try:
                month, year = _MONTH_NUMBER[m.group(1)[:3].lower()], int(m.group(5))
                d1 = _dt.datetime(year, month, int(m.group(2))) + delta
                d2 = _dt.datetime(year, month, int(m.group(4))) + delta
                if (d1.year, d1.month) == (d2.year, d2.month):
                    return put(f"{d1:%B} {d1:%d}{m.group(3)}{d2:%d}, {d2:%Y}")
                return put(f"{d1:%B} {d1:%d}{', ' + str(d1.year) if d1.year != d2.year else ''} to {d2:%B} {d2:%d}, {d2:%Y}")
            except ValueError:
                pass
        # A season and a year is not a date: a year may stay, and a season names no month. It used to
        # come out with the year scrambled ("spring 0449").
        if _SEASON_YEAR.fullmatch(s):
            return orig
        numeric_fmts = ["%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y",
                        "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%m.%d.%Y", "%m.%d.%y",
                        "%Y-%b-%d", "%d-%b-%Y", "%d-%b-%y", "%d%b%Y", "%d%b%y"]
        for fmt in numeric_fmts:
            try:
                d = _dt.datetime.strptime(s, fmt)
            except ValueError:
                continue
            new = (d + delta).strftime(fmt)
            return put(new.upper() if s.isupper() else new)
        for fmt in ("%m/%d", "%m-%d"):                  # no year: 2001 has no 29 February, 2000 has
            for year in ("2001", "2000"):
                try:
                    d = _dt.datetime.strptime(s + " " + year, fmt + " %Y")
                except ValueError:
                    continue
                return put((d + delta).strftime(fmt))
        for fmt in ("%m/%Y", "%m-%Y"):                  # month and year: shift the middle of the month
            try:
                d = _dt.datetime.strptime(s, fmt)
            except ValueError:
                continue
            return put((d.replace(day=15) + delta).strftime(fmt))
        m = _W_MONTH_YEAR.fullmatch(s)                  # "March 2019", "March of 2019"
        if m:
            try:
                return put((_dt.datetime(int(m.group(2)), _MONTH_NUMBER[m.group(1)[:3].lower()], 15) + delta).strftime("%B %Y"))
            except ValueError:
                pass
        m = _W_MONTH_DAY.fullmatch(s)                   # "March 4", "March 4th"
        if m:
            for year in (2001, 2000):
                try:
                    d = _dt.datetime(year, _MONTH_NUMBER[m.group(1)[:3].lower()], int(m.group(2)))
                except ValueError:
                    continue
                return put((d + delta).strftime("%B %d"))
        if _W_MONTH.fullmatch(s):                       # "March"
            return put((_dt.datetime(2001, _MONTH_NUMBER[s[:3].lower()], 15) + delta).strftime("%B"))
        # other written dates ("March 4, 2019", "4 March 2019", "4Mar19") via dateparser. It fills in
        # whatever a date leaves out from a base date, so parsing against two base dates shows which
        # parts the text really states.
        try:
            import dateparser
            d = dateparser.parse(s, settings={"RELATIVE_BASE": _dt.datetime(2001, 1, 15)})
            d2 = dateparser.parse(s, settings={"RELATIVE_BASE": _dt.datetime(2004, 6, 20)}) if d else None
            if d and d2:
                has_y, has_m, has_d = d.year == d2.year, d.month == d2.month, d.day == d2.day
                if has_y and has_m and has_d:
                    return (d + delta).strftime("%B %d, %Y")
                if has_y and has_m:
                    return (d.replace(day=15) + delta).strftime("%B %Y")
                if has_m and has_d:
                    return (d + delta).strftime("%B %d")
                if has_m:
                    return (d.replace(day=15) + delta).strftime("%B")
                if has_y:
                    return orig                         # a year alone may stay
        except Exception:
            pass
        # unparseable "date" tokens are almost always ID-like (case/specimen numbers Philter mis-tagged
        # as dates, e.g. "12-34-5678"): fake the digits (never leaks the real value, avoids asterisks).
        if any(c.isdigit() for c in orig):
            return self._fake_accession(orig)
        # purely alphabetic "date" token: a bare season / time-of-day / relative word is NOT a HIPAA date
        # element (no day/month/year), so keep it ("winter months", "morning"); only a lone month or
        # holiday name (an actual date element) is redacted.
        low = s.lower()
        _MONTHS = {"january","february","march","april","may","june","july","august","september",
                   "october","november","december","jan","feb","mar","apr","jun","jul","aug","sep",
                   "sept","oct","nov","dec"}
        _HOLIDAYS = {"christmas","thanksgiving","easter","hanukkah","halloween","new year","new years"}
        if low in _MONTHS or low in _HOLIDAYS:
            return "*" * len(orig)
        return orig                                 # non-specific temporal word -> keep (not identifying)

    def transform_text_i2b2(self, tagdata):
        """creates a string in i2b2-XML format"""
        root = "Philter"
        contents = []
        
        contents.append("<?xml version=\"1.0\" ?>\n")
        contents.append("<"+root+">\n")
        contents.append("<TEXT><![CDATA[")
        contents.append(tagdata['text'])
        contents.append("]]></TEXT>\n")
        contents.append("<TAGS>\n")
        for i in range(len(tagdata['phi'])):
            phi_type = tagdata['phi'][i]['phi_type']
            tagcategory = phi_type
            contents.append("<")
            contents.append(phi_type)
            contents.append(" id=\"P")
            contents.append(str(i))
            contents.append("\" start=\"")
            contents.append(str(tagdata['phi'][i]['start']))
            contents.append("\" end=\"")
            contents.append(str(tagdata['phi'][i]['stop']))
            contents.append("\" text=\"")
            contents.append(tagdata['phi'][i]['word'])
            contents.append("\" TYPE=\"")
            contents.append(phi_type)
            contents.append("\" comment=\"\" />\n")
        

        # for loop over complement - PHI, create additional tags (UNKNOWN)
        contents.append("</TAGS>\n")
        contents.append("</"+root+">\n")
        
        return "".join(contents)
                
    def detect_encoding(self, fp):
        if not os.path.exists(fp):
            raise Exception("Filepath does not exist", fp)

        detector = UniversalDetector()
        with open(fp, "rb") as f:
            for line in f:
                detector.feed(line)
                if detector.done: 
                    break
            detector.close()
        return detector.result

    def phi_context(self, filename, word, word_index, words, context_window=10):
        """ helper function, creates our phi data type with source file, and context window"""
        if not os.path.exists(filename):
            raise Exception("Filepath does not exist", filename)

        left_index = word_index - context_window
        if left_index < 0:
            left_index = 0

        right_index = word_index + context_window
        if right_index >= len(words):
            right_index = len(words) - 1
        window = words[left_index:right_index]

        #get which patterns matched this word
        num_spaces = len(words[:word_index])
        

        return {"filename":filename, "phi":word, "context":window}


    def seq_eval(self,
            note_lst, 
            anno_lst,
            filename,
            punctuation_matcher=re.compile(r"[^a-zA-Z0-9*]"), 
            text_matcher=re.compile(r"[a-zA-Z0-9]"), 
            phi_matcher=re.compile(r"\*+")):
        """ 
            Compares two sequences item by item, 
            returns generator which yields: 
            classifcation, word

            classifications can be TP, FP, FN, TN 
            corresponding to True Positive, False Positive, False Negative and True Negative
        """
        start_coordinate = 0
        for note_word, anno_word in list(zip(note_lst, anno_lst)):

            ##### Get coordinates ######
            start = start_coordinate
            stop = start_coordinate + len(note_word)
            note_word_stripped = re.sub(r"[^a-zA-Z0-9\*]+", "", note_word.strip())
            anno_word_stripped = re.sub(r"[^a-zA-Z0-9\*]+", "", anno_word.strip())
            if len(note_word_stripped) == 0:
                #got a blank space or something without any characters or digits, move forward
                start_coordinate += len(note_word)
                continue

            if phi_matcher.search(anno_word):
                #this contains phi
                
                if note_word == anno_word:               
                    yield "TP", note_word, start_coordinate
                else:
                    if text_matcher.search(anno_word):

                        #this is a complex edge case, 
                        #the phi annotation has some characters *'ed, and some not, 
                        #find the overlap and report any string of chars in anno as FP
                        #and any string of chars in note as FN
                        fn_words = []
                        fp_words = []

                        fn_chunk = []
                        fp_chunk = []
                        for n,a in list(zip(note_word, anno_word)):
                            if n == a:
                                #these characters match, clear our chunks
                                if len(fp_chunk) > 0:
                                    fp_words.append("".join(fp_chunk))
                                    fp_chunk = []
                                if len(fn_chunk) > 0:
                                    fn_words.append("".join(fn_words))
                                    fn_chunk = []
                                
                                continue 
                            if a == "*" and n != "*":
                                fn_chunk.append(n)
                            elif a != "*" and n == "*":
                                fp_chunk.append(a)

                        #clear any remaining chunks
                        if len(fp_chunk) > 0:
                            fp_words.append("".join(fp_chunk))
                        if len(fn_chunk) > 0:
                            fn_words.append("".join(fn_words))

                        #now drain the difference
                        for w in fn_words:
                            yield "FN", w, start_coordinate
                        for w in fp_words:
                            yield "FP", w, start_coordinate

                    else:
                        #simpler case, anno word is completely blocked out except punctuation
                        yield "FN", note_word, start_coordinate

            else:
                if note_word.isspace() == False:
                    #this isn't phi
                    if note_word == anno_word:
                        yield "TN", note_word, start_coordinate
                    else:
                        yield "FP", anno_word, start_coordinate

            #advance our start coordinate
            start_coordinate += len(note_word) 



    def eval(self,
        config,
        note_path="./data/i2b2_notes/",
        anno_path="data/i2b2_anno/",
        anno_suffix="_phi_reduced.ano",
        in_path="data/i2b2_results/",
        summary_output="data/phi/summary.json",
        summary_coords_output="data/phi/summary_coords.json",
        phi_matcher=re.compile("\*+"),
        only_digits=False,
        fn_output = "data/phi/fn.txt",
        fp_output = "data/phi/fp.txt",
        tn_output = "data/phi/tn.txt",
        tp_output = "data/phi/tp.txt",
        fn_tags_context = "data/phi/fn_tags_context.txt",
        fp_tags_context = "data/phi/fp_tags_context.txt",
        fn_tags_nocontext = "data/phi/fn_tags.txt",
        fp_tags_nocontext = "data/phi/fp_tags.txt",
        pre_process=r":|\,|\-|\/|_|~", #characters we're going to strip from our notes to analyze against anno        
        pre_process2= r"[^a-zA-Z0-9]",
        punctuation_matcher=re.compile(r"[^a-zA-Z0-9\*]")):
        """ calculates the effectiveness of the philtering / extraction

            only_digits = <boolean> will constrain evaluation on philtering of only digit types
        """

        if not os.path.exists(anno_path):
            raise Exception("Anno Filepath does not exist", anno_path)
        if not os.path.exists(in_path):
            raise Exception("Input Filepath does not exist", in_path)

        if self.verbose:
            print("RUNNING EVAL")
            
        
        summary = {
            "total_false_positives":0,
            "total_false_negatives":0,
            "total_true_positives": 0,
            "total_true_negatives": 0,
            "false_positives":[], #non-phi words we think are phi
            "true_positives": [], #phi words we correctly identify
            "false_negatives":[], #phi words we think are non-phi
            "true_negatives": [], #non-phi words we correctly identify
            "summary_by_file":{}
        }
        summary_coords = {
            "summary_by_file":{}
        }
        
        all_fn = []
        all_fp = []
        all_tn = []
        all_tp = []

        for root, dirs, files in os.walk(in_path):

            for f in files:
                if not f.endswith(".txt"): # TODO: come up with something better
                    continue               #       to ensure one to one txt file
                                           #       comparisons with anno_path
                #local values per file
                false_positives = [] #non-phi we think are phi
                false_positives_coords = []
                true_positives  = [] #phi we correctly identify
                true_positives_coords = []
                false_negatives = [] #phi we think are non-phi
                false_negatives_coords = []
                true_negatives  = [] #non-phi we correctly identify
                true_negatives_coords = []

                original_filename = note_path+f
                philtered_filename = root+f
                anno_filename = anno_path+''.join(f.split(".")[0])+anno_suffix

                if not os.path.exists(philtered_filename):
                    raise Exception("FILE DOESNT EXIST", philtered_filename)
                
                if not os.path.exists(anno_filename):
                    continue

                encoding1 = self.detect_encoding(philtered_filename)
                philtered = open(philtered_filename,"r").read()
                
                          
                philtered_words = re.split("(\s+)", philtered)
                philtered_words_cleaned = []
                for item in philtered_words:
                    if len(item) > 0:
                        if item.isspace() == False:
                            split_item = re.split("(\s+)", re.sub(punctuation_matcher, " ", item))
                            for elem in split_item:
                                if len(elem) > 0:
                                    philtered_words_cleaned.append(elem)
                        else:
                            philtered_words_cleaned.append(item)
                
                encoding2 = self.detect_encoding(anno_filename)
                anno = open(anno_filename,"r").read()              
                
                anno_words_cleaned = self.get_clean_filtered(anno_filename, anno)[0]
           
                for c,w,r in self.seq_eval(philtered_words_cleaned, anno_words_cleaned, f):

                    # Double check that we aren't adding blank spaces or single punctionation characters to our lists
                    if w.isspace() == False and (re.sub(r"[^a-zA-Z0-9\*]+", "", w) != ""):

                        if c == "FP":
                            false_positives.append(w)
                            false_positives_coords.append([w,r])
                        elif c == "FN":
                            false_negatives.append(w)
                            false_negatives_coords.append([w,r])
                        elif c == "TP":
                            true_positives.append(w)
                            true_positives_coords.append([w,r])
                        elif c == "TN":
                            true_negatives.append(w)
                            true_negatives_coords.append([w,r])
                #update summary
                summary["summary_by_file"][philtered_filename] = {"false_positives":false_positives, "false_negatives":false_negatives, "true_negatives":true_negatives, "num_false_negatives":len(false_negatives), "num_false_positives":len(false_positives), "num_true_positives":len(true_positives), "num_true_negatives":len(true_negatives)}
                summary["total_true_positives"] = summary["total_true_positives"] + len(true_positives)
                summary["total_false_positives"] = summary["total_false_positives"] + len(false_positives)
                summary["total_false_negatives"] = summary["total_false_negatives"] + len(false_negatives)
                summary["total_true_negatives"] = summary["total_true_negatives"] + len(true_negatives)
                all_tn = all_tn + true_negatives
                all_fp = all_fp + false_positives
                all_fn = all_fn + false_negatives
                all_tp = all_tp + true_positives

                # Create coordinate summaries
                summary_coords["summary_by_file"][philtered_filename] = {"false_positives":false_positives_coords,"false_negatives":false_negatives_coords,"true_positives":true_positives_coords,"true_negatives":true_negatives_coords}

        if summary["total_true_positives"]+summary["total_false_negatives"] > 0:
            recall = summary["total_true_positives"]/(summary["total_true_positives"]+summary["total_false_negatives"])
        elif summary["total_false_negatives"] == 0:
            recall = 1.0

        if summary["total_true_positives"]+summary["total_false_positives"] > 0:
            precision = summary["total_true_positives"]/(summary["total_true_positives"]+summary["total_false_positives"])
        elif summary["total_true_positives"] == 0:
            precision = 0.0

        if summary["total_true_negatives"]+summary["total_false_positives"] > 0:
            retention = summary["total_true_negatives"]/(summary["total_true_negatives"]+summary["total_false_positives"])
        else:
            retention = 0.0
        
        ################# DETAILED EVAL ##################
        #save the phi we missed
        json.dump(summary, open(summary_output, "w"), indent=4)
        json.dump(summary_coords, open(summary_coords_output, "w"), indent=4)
        json.dump(all_fn, open(fn_output, "w"), indent=4)
        json.dump(all_fp, open(fp_output, "w"), indent=4)
        json.dump(all_tn, open(tn_output, "w"), indent=4)
        json.dump(all_tp, open(tp_output, "w"), indent=4)
        
        if self.verbose:
            print('\n')
            print("Uncorrected Results:")
            print('\n')
            print("TP:", summary["total_true_positives"],"FN:", summary["total_false_negatives"], "TN:", summary["total_true_negatives"], "FP:", summary["total_false_positives"])
            print("Global Recall: {:.2%}".format(recall))
            print("Global Precision: {:.2%}".format(precision))
            print("Global Retention: {:.2%}".format(retention))


        ###################### Get phi tags #####################
        if self.verbose:
            print('\n')
            print("RUNNING ERRORCHECK")

        if self.gold:
            # Get xml summary
            phi = self.xml
        # Create dictionary to hold fn tags
        fn_tags = {}
        fp_tags = {}
        
        # Keep track of recall and precision for each category
        if not self.ucsf_format: # i2b2 format
            gold_tags = i2b2_tags
        else: # ucsf format
            gold_tags = ucsf_tags
            if not self.initials:
                ucsf_include_tags.remove('Patient_Initials')
                ucsf_include_tags.remove('Provider_Initials')
                ucsf_patient_tags.remove('Patient_Initials')
                ucsf_provider_tags.remove('Provider_Initials')

        rp_summaries = {}
        for i in range(0,len(gold_tags)):
            tag = gold_tags[i]
            fn_key = tag + '_fns'
            tp_key = tag + '_tps'
            rp_summaries[fn_key] = 0
            rp_summaries[tp_key] = 0


        # Create dictionaries for unigram and bigram PHI/non-PHI frequencies
        # Diciontary values look like: [phi_count, non-phi_count]
        unigram_dict = {}
        bigram_dict = {}
        corrected_age_fns = 0


        # Loop through all filenames in summary
        for fn in summary_coords['summary_by_file']:
            # get input notes filename (for filter analysis wit coordinatemap)
            input_filename = self.finpath + os.path.basename(fn)

            current_summary =  summary_coords['summary_by_file'][fn]

            # Get corresponding info in phi_notes
            note_name = fn.split('/')[-1]
            
            if self.gold:
                try:
                    anno_name = note_name.split('.')[0] + ".xml"
                    text = phi[anno_name]['text']
                except KeyError:
                    anno_name = note_name.split('.')[0] + ".txt.xml"
                    text = phi[anno_name]['text']

            else:
                text = open(input_filename,"r").read()


            lst = re.split("(\s+)", text)
            cleaned = []
            cleaned_coords = []
            for item in lst:
                if len(item) > 0:
                    if item.isspace() == False:
                        split_item = re.split("(\s+)", re.sub(r"[^a-zA-Z0-9]", " ", item))
                        for elem in split_item:
                            if len(elem) > 0:
                                cleaned.append(elem)
                    else:
                        cleaned.append(item)
            
            # Get coords for POS tags
            start_coordinate = 0
            pos_coords = []
            for item in cleaned:
                pos_coords.append(start_coordinate)
                start_coordinate += len(item)
            
            pos_list = nltk.pos_tag(cleaned)

            cleaned_with_pos = {}
            for i in range(0,len(pos_list)):
                cleaned_with_pos[str(pos_coords[i])] = [pos_list[i][0], pos_list[i][1]]

            
            if self.gold:
                ########## Get FN tags ##########
                phi_list = phi[anno_name]['phi']

                ######### Create unigram and bigram frequency tables #######
                if self.freq_table:

                    # Create separate cleaned list/coord list without spaces
                    cleaned_nospaces = []
                    coords_nospaces = []
                    for i in range(0,len(cleaned)):
                        if cleaned[i].isspace() == False:
                            cleaned_nospaces.append(cleaned[i])
                            coords_nospaces.append(pos_coords[i])

                    # Loop through all single words and word pairs, and compare with PHI list
                    for i in range(0,len(cleaned_nospaces)-1):
                        #cleaned_nospaces[i]= word, coords_nospaces[i] = start coordinate
                        unigram_word = cleaned_nospaces[i].replace('\n','').replace('\t','').replace(' ','').lower()
                        bigram_word = " ".join([cleaned_nospaces[i].replace('\n','').replace('\t','').replace(' ','').lower(),cleaned_nospaces[i+1].replace('\n','').replace('\t','').replace(' ','').lower()])
                        unigram_start = coords_nospaces[i]
                        bigram_start1 = coords_nospaces[i]
                        bigram_start2 = coords_nospaces[i+1]

                        # Loop through PHI list and compare ranges
                        for phi_item in phi_list:
                            try:
                                phi_start = phi_item['start']
                                phi_end = phi_item['end']
                            except KeyError:
                                phi_start = phi_item['spans'].split('~')[0]
                                phi_end = phi_item['spans'].split('~')[1]
                            if unigram_start in range(int(phi_start), int(phi_end)):
                                # This word is PHI and hasn't been added to the dictionary yet
                                if unigram_word not in unigram_dict:
                                    unigram_dict[unigram_word] = [1, 0]
                               # This word is PHI and has already been added to the dictionary
                                else:
                                    unigram_dict[unigram_word][0] += 1
                            else:
                                # This word is not PHI and hasn't been aded to the dictionary yet
                                if unigram_word not in unigram_dict:
                                    unigram_dict[unigram_word] = [0, 1]
                               # This word is not PHI and has already been added to the dictionary
                                else:
                                    unigram_dict[unigram_word][1] += 1                               
                            if bigram_start1 in range(int(phi_start), int(phi_end)) and bigram_start2 in range(int(phi_start), int(phi_end)):
                                # This word is PHI and hasn't been added to the dictionary yet
                                if bigram_word not in bigram_dict:
                                    bigram_dict[bigram_word] = [1, 0]
                               # This word is PHI and has already been added to the dictionary
                                else:
                                    bigram_dict[bigram_word][0] += 1                                
                            else:
                                # This word is not PHI and hasn't been aded to the dictionary yet
                                if bigram_word not in bigram_dict:
                                    bigram_dict[bigram_word] = [0, 1]
                               # This word is not PHI and has already been added to the dictionary
                                else:
                                    bigram_dict[bigram_word][1] += 1


            # Get tp counts per category
            current_tps = current_summary['true_positives']
            # Initialize list to keep track of non-include tag FPs
            additional_fps = []

            for word in current_tps:
                start_coordinate_tp = word[1]
                for phi_item in phi_list:
                    if self.ucsf_format:
                        phi_start = int(phi_item['spans'].split('~')[0])
                        phi_end = int(phi_item['spans'].split('~')[1])                               
                    else:
                        phi_start = phi_item['start']
                        phi_end = phi_item['end']
                    phi_type = phi_item['TYPE']
                    phi_word = phi_item['text']

                    if not self.ucsf_format:
                        for i in range(0,len(i2b2_tags)):
                            tag = i2b2_tags[i]
                            tp_key = tag + '_tps'
                            if (start_coordinate_tp in range(int(phi_start), int(phi_end))) and (tag == phi_type):
                                rp_summaries[tp_key] += 1
                        # Add these TPs to the FPs list of they are not in the include list
                        if phi_type not in i2b2_include_tags:
                            if (start_coordinate_tp in range(int(phi_start), int(phi_end))):
                                additional_fps.append([text[start_coordinate_tp:start_coordinate_tp + len(word[0])], start_coordinate_tp])                                  
                    #### ucsf
                    if self.ucsf_format:
                        if phi_type not in ucsf_include_tags:
                            if (start_coordinate_tp in range(int(phi_start), int(phi_end))):
                                additional_fps.append([text[start_coordinate_tp:start_coordinate_tp + len(word[0])], start_coordinate_tp])

                        for i in range(0,len(ucsf_tags)):
                            tag = ucsf_tags[i]
                            tp_key = tag + '_tps'
                            if (start_coordinate_tp in range(int(phi_start), int(phi_end))) and (tag == phi_type):
                                rp_summaries[tp_key] += 1
                            
            #### i2b2
            if not self.ucsf_format:
                fn_counter_dict = {}
                for i in range(0,len(i2b2_tags)):
                    tag = i2b2_tags[i]
                    tag_fn_counter = tag + '_fn_counter'
                    fn_counter_dict[tag_fn_counter] = 0
            #### ucsf
            if self.ucsf_format:
                fn_counter_dict = {}
                for i in range(0,len(ucsf_tags)):
                    tag = ucsf_tags[i]
                    tag_fn_counter = tag + '_fn_counter'
                    fn_counter_dict[tag_fn_counter] = 0
                          

            fn_tag_summary = {}
            include_exclude_fns = ''

            # print(self.patterns)
            if current_summary['false_negatives'] != [] and current_summary['false_negatives'] != [""]:              
                counter = 0
                current_fns = current_summary['false_negatives']
             
                for word in current_fns:
                    counter += 1
                    false_negative = word[0]
                    start_coordinate_fn = word[1]

                    # initialize list that will hold info on what matched what
                    filter_file_list_exclude = []
                    filter_file_list_include = []

                    if self.dependent:
                        # Loop through coorinate map objects and match patterns with FPs
                        for i,pattern in enumerate(self.patterns):

                            coord_map = pattern["coordinate_map"]
                            exclude_include = pattern["exclude"]
                            try:
                                filter_path = pattern["filepath"]
                            except KeyError:
                                filter_path = pattern["title"]
                            for start,stop in coord_map.filecoords(input_filename):
                                # Find intersection between ranges
                                word_range = set(range(start_coordinate_fn, start_coordinate_fn + len(false_negative)))
                                filter_range = set(range(start, stop))
                                intersection = word_range & filter_range
                                if intersection != set():
                                    # Add this filter path to the list of things that filtered this word
                                    if exclude_include == True:
                                        filter_file_list_exclude.append(filter_path)
                                    else:
                                        filter_file_list_include.append(filter_path)
                    if self.dependent == False:
                        filter_file_list_exclude.append('')
                        filter_file_list_include.append('')

                    for phi_item in phi_list:                           
                        phi_text = phi_item['text']
                        phi_type = phi_item['TYPE']
                        phi_id = phi_item['id']
                        if self.ucsf_format:
                            phi_start = int(phi_item['spans'].split('~')[0])
                            phi_end = int(phi_item['spans'].split('~')[1])                               
                        else:
                            phi_start = phi_item['start']
                            phi_end = phi_item['end']


                        #### i2b2
                        if not self.ucsf_format:
                            for i in range(0,len(i2b2_tags)):
                                tag = i2b2_tags[i]
                                fn_key = tag + '_fns'
                                tag_fn_counter = tag + '_fn_counter'
                                if (start_coordinate_fn in range(int(phi_start), int(phi_end))) and phi_type == tag:
                                    rp_summaries[fn_key] += 1
                                    fn_counter_dict[tag_fn_counter] += 1

                        
                        #### ucsf
                        if self.ucsf_format:
                            for i in range(0,len(ucsf_tags)):
                                tag = ucsf_tags[i]
                                fn_key = tag + '_fns'
                                tag_fn_counter = tag + '_fn_counter'
                                if (start_coordinate_fn in range(int(phi_start), int(phi_end))) and phi_type == tag:
                                    if tag != 'Age':
                                        rp_summaries[fn_key] += 1
                                        fn_counter_dict[tag_fn_counter] += 1


                        # Find PHI match: fn in text, coord in range
                        if start_coordinate_fn in range(int(phi_start), int(phi_end)):
                            # Get PHI tag
                            phi_tag = phi_type
                            # Get POS tag
                            pos_tag = cleaned_with_pos[str(start_coordinate_fn)][1]
                            
                            # Get 25 characters surrounding FN on either side
                            fn_context = ''
                            context_start = start_coordinate_fn - 25
                            context_end = start_coordinate_fn + len(false_negative) + 25
                            if context_start >= 0 and context_end <= len(text)-1:
                                fn_context = text[context_start:context_end]
                            elif context_start >= 0 and context_end > len(text)-1:
                                fn_context = text[context_start:]
                            else:
                                fn_context = text[:context_end]  
                            
                            # Get fn id, to distinguish betweem multiple entries
                            fn_id = "N" + str(counter)
                            
                            # Get include or exclude
                            if not self.ucsf_format:
                                if phi_tag in i2b2_include_tags:
                                    include_exclude_fns = 'include'
                                else:
                                    include_exclude_fns = 'exclude'
                            if self.ucsf_format:
                                if phi_tag in ucsf_include_tags:
                                    if phi_tag != 'Age':
                                        include_exclude_fns = 'include'
                                    # If age is over 90, include. Else, exclude
                                    else:
                                        fn_stripped = false_negative.replace('.','')
                                        # Is the age an integer?
                                        if fn_stripped.isdigit():
                                            if int(fn_stripped) >= 90:
                                                include_exclude_fns = 'include'
                                                corrected_age_fns += 1
                                            else:
                                                include_exclude_fns = 'exclude'
                                        # Is the age a string?
                                        # Note that this won't catch all age FNs that are spelled ou
                                        # i.e., only the 'ninety' in 'ninety-five' will be marked as include
                                        # This won't affect our recall at all, but it will affect our precision a little
                                        # We will manually need to subtract theses from our FPs and add to our TPs 
                                        else:
                                            if 'ninety' in fn_stripped:
                                                include_exclude_fns = 'include'
                                                corrected_age_fns += 1
                                            else:
                                                include_exclude_fns = 'exclude'

                                else:
                                    include_exclude_fns = 'exclude'
                            ###### Create output dicitonary with id/word/pos/phi
                            fn_tag_summary[fn_id] = [false_negative, phi_tag, pos_tag, fn_context, include_exclude_fns, filter_file_list_exclude, filter_file_list_include]

            if fn_tag_summary != {}:
                fn_tags[fn] = fn_tag_summary


            ####### Get FP tags #########
            fp_tag_summary = {}
            include_exclude_fps = ''
            current_fps = current_summary['false_positives'] + additional_fps
            if current_fps != [] and current_fps != [""]:              
                counter = 0
                for word in current_fps:
                    counter += 1
                    false_positive = word[0]
                    start_coordinate_fp = word[1]

                    # initialize list that will hold info on what matched what
                    filter_file_list_exclude = []
                    filter_file_list_include = []
                    
                    if self.dependent:
                        # Loop through coorinate map objects and match patterns with FPs
                        for i,pattern in enumerate(self.patterns):

                            coord_map = pattern["coordinate_map"]
                            exclude_include = pattern["exclude"]
                            try:
                                filter_path = pattern["filepath"]
                            except KeyError:
                                filter_path = pattern["title"]
                            for start,stop in coord_map.filecoords(input_filename):
                                # print(start,stop,text[start:stop])
                                word_range = set(range(start_coordinate_fp, start_coordinate_fp + len(false_positive)))
                                filter_range = set(range(start, stop))
                                intersection = word_range & filter_range
                                if intersection != set():
                                    # Add this filter path to the list of things that filtered this word
                                    if exclude_include == True:
                                        filter_file_list_exclude.append(filter_path)
                                    else:
                                        filter_file_list_include.append(filter_path)
                    if self.dependent == False:
                        filter_file_list_exclude.append('')
                        filter_file_list_include.append('')
            
                    pos_entry = cleaned_with_pos[str(start_coordinate_fp)]

                    pos_tag = pos_entry[1]

                    # Get 25 characters surrounding FP on either side
                    fp_context = ''
                    context_start = start_coordinate_fp - 25
                    context_end = start_coordinate_fp + len(false_positive) + 25
                    if context_start >= 0 and context_end <= len(text)-1:
                        fp_context = text[context_start:context_end]
                    elif context_start >= 0 and context_end > len(text)-1:
                        fp_context = text[context_start:]
                    else:
                        fp_context = text[:context_end]


                    fp_id = "P" + str(counter)

                    
                    fp_tag_summary[fp_id] = [false_positive, pos_tag, fp_context, filter_file_list_exclude, filter_file_list_include]

            if fp_tag_summary != {}:
                fp_tags[fn] = fp_tag_summary

        # Create frequency table outputs
        if self.freq_table:
            # Unigram table
            with open('./data/phi/unigram_freq_table.csv','w') as f:
                f.write('unigram,phi_count,non-phi_count\n')
                for key in unigram_dict:
                    word = key
                    phi_count = unigram_dict[key][0]
                    non_phi_count = unigram_dict[key][1]
                    f.write(word + ',' + str(phi_count) + ',' + str(non_phi_count) + '\n')
            with open('./data/phi/bigram_freq_table.csv','w') as f:
                f.write('bigram,phi_count,non-phi_count\n')
                for key in bigram_dict:
                    term = key
                    phi_count = bigram_dict[key][0]
                    non_phi_count = bigram_dict[key][1]
                    f.write(term + ',' + str(phi_count) + ',' + str(non_phi_count) + '\n')

        # get specific recalls
        # i2b2
        overall_data = []
        if not self.ucsf_format:
            include_dict = {'fns':0,'tps':0,'fps':summary["total_false_positives"],'tns':summary["total_true_negatives"]}
            patient_phi_dict = {'fns':0,'tps':0,'fps':summary["total_false_positives"],'tns':summary["total_true_negatives"]}
            provider_phi_dict = {'fns':0,'tps':0,'fps':summary["total_false_positives"],'tns':summary["total_true_negatives"]}

            category_dict = {}
            for i in range(0,len(phi_categories)):
                category_tag = phi_categories[i]
                category_fns = category_tag + '_fns'
                category_tps = category_tag + '_tps'

                category_dict[category_fns] = 0
                category_dict[category_tps] = 0


            overall_recall_dict = {}

            for i in range(0,len(i2b2_tags)):
                tag = i2b2_tags[i]
                fn_key = tag + '_fns'
                tp_key = tag + '_tps'
                tag_fn_counter = tag + '_fn_counter'
                tag_cleaned_list = tag + '_cleaned'
                recall_key = tag + '_recall'


                # Get info for overall include dict and category dict
                if tag in i2b2_include_tags:
                    include_dict['fns'] += rp_summaries[fn_key]
                    include_dict['tps'] += rp_summaries[tp_key]

                    if tag in i2b2_patient_tags:
                        patient_phi_dict['fns'] += rp_summaries[fn_key]
                        patient_phi_dict['tps'] += rp_summaries[tp_key]
                    if tag in i2b2_provider_tags:
                        provider_phi_dict['fns'] += rp_summaries[fn_key]
                        provider_phi_dict['tps'] += rp_summaries[tp_key]

                    tag_category = i2b2_category_dict[tag]
                    category_fns = tag_category + '_fns'
                    category_tps = tag_category + '_tps'

                    category_dict[category_fns] += rp_summaries[fn_key]
                    category_dict[category_tps] += rp_summaries[tp_key]
                
                # Get additional TNs and FPs
                if tag not in i2b2_include_tags:
                    include_dict['tns'] += rp_summaries[fn_key]
                    include_dict['fps'] += rp_summaries[tp_key]

                    if tag in i2b2_patient_tags:
                        patient_phi_dict['tns'] += rp_summaries[fn_key]
                        patient_phi_dict['fps'] += rp_summaries[tp_key]   
                    if tag in i2b2_provider_tags:
                        provider_phi_dict['tns'] += rp_summaries[fn_key]
                        provider_phi_dict['fps'] += rp_summaries[tp_key]          


                if rp_summaries[fn_key] != 0:

                    overall_recall_dict[recall_key] = rp_summaries[tp_key] / (rp_summaries[fn_key] + rp_summaries[tp_key])

                else:
                    overall_recall_dict[recall_key] = 1

                overall_data.append([tag,"{:.2%}".format(overall_recall_dict[recall_key]),str(rp_summaries[tp_key]),str(rp_summaries[fn_key])])

        # ucsf
        if self.ucsf_format:
            include_dict = {'fns':0,'tps':0,'fps':summary["total_false_positives"],'tns':summary["total_true_negatives"]}
            patient_phi_dict = {'fns':0,'tps':0,'fps':summary["total_false_positives"],'tns':summary["total_true_negatives"]}
            provider_phi_dict = {'fns':0,'tps':0,'fps':summary["total_false_positives"],'tns':summary["total_true_negatives"]}


            category_dict = {}
            for i in range(0,len(phi_categories)):
                category_tag = phi_categories[i]
                category_fns = category_tag + '_fns'
                category_tps = category_tag + '_tps'

                category_dict[category_fns] = 0
                category_dict[category_tps] = 0


            overall_recall_dict = {}

            for i in range(0,len(ucsf_tags)):
                tag = ucsf_tags[i]
                fn_key = tag + '_fns'
                tp_key = tag + '_tps'
                tag_fn_counter = tag + '_fn_counter'
                tag_cleaned_list = tag + '_cleaned'
                recall_key = tag + '_recall'


                # Get info for overall include dict and category dict
                if tag in ucsf_include_tags:
                    if tag != 'Age':
                        include_dict['fns'] += rp_summaries[fn_key]
                        include_dict['tps'] += rp_summaries[tp_key]

                        if tag in ucsf_patient_tags:
                            patient_phi_dict['fns'] += rp_summaries[fn_key]
                            patient_phi_dict['tps'] += rp_summaries[tp_key]
                        if tag in ucsf_provider_tags:
                            provider_phi_dict['fns'] += rp_summaries[fn_key]
                            provider_phi_dict['tps'] += rp_summaries[tp_key]

                        tag_category = ucsf_category_dict[tag]
                        category_fns = tag_category + '_fns'
                        category_tps = tag_category + '_tps'

                        category_dict[category_fns] += rp_summaries[fn_key]
                        category_dict[category_tps] += rp_summaries[tp_key]
                    else:
                        include_dict['fns'] += corrected_age_fns
                        include_dict['tps'] += rp_summaries[tp_key]
                        include_dict['tns'] += (rp_summaries[fn_key] - corrected_age_fns)

                        if tag in ucsf_patient_tags:
                            patient_phi_dict['fns'] += corrected_age_fns
                            patient_phi_dict['tps'] += rp_summaries[tp_key]
                            patient_phi_dict['tns'] += (rp_summaries[fn_key] - corrected_age_fns)
                        if tag in ucsf_provider_tags:
                            provider_phi_dict['fns'] += corrected_age_fns
                            provider_phi_dict['tps'] += rp_summaries[tp_key]
                            provider_phi_dict['tns'] += (rp_summaries[fn_key] - corrected_age_fns)

                        tag_category = ucsf_category_dict[tag]
                        category_fns = tag_category + '_fns'
                        category_tps = tag_category + '_tps'

                        category_dict[category_fns] += corrected_age_fns
                        category_dict[category_tps] += rp_summaries[tp_key]

                
                # Get additional TNs and FPs
                if tag not in ucsf_include_tags:
                    include_dict['tns'] += rp_summaries[fn_key]
                    include_dict['fps'] += rp_summaries[tp_key]
                
                    if tag in ucsf_patient_tags:
                        patient_phi_dict['tns'] += rp_summaries[fn_key]
                        patient_phi_dict['fps'] += rp_summaries[tp_key]   
                    if tag in ucsf_provider_tags:
                        provider_phi_dict['tns'] += rp_summaries[fn_key]
                        provider_phi_dict['fps'] += rp_summaries[tp_key]  


                if rp_summaries[fn_key] != 0:
                    overall_recall_dict[recall_key] = rp_summaries[tp_key]/(rp_summaries[fn_key] + rp_summaries[tp_key])
                else:
                    overall_recall_dict[recall_key] = 1
                if tag == 'Age':
                    overall_data.append([tag,"{:.2%}".format(overall_recall_dict[recall_key]),str(rp_summaries[tp_key]),str(corrected_age_fns)])
                else:
                    overall_data.append([tag,"{:.2%}".format(overall_recall_dict[recall_key]),str(rp_summaries[tp_key]),str(rp_summaries[fn_key])])
     
        # pretty print tag recalls
        overall_data.sort(key=lambda x: float(x[1][:-1]),reverse=True)
        sorted_overall_data = [["Tag","Recall","TPs","FNs"]]
        for item in overall_data:
            sorted_overall_data.append(item)

        if self.verbose:
            print('\n')
            print("Recall by Tag:")
            col_width = max(len(word) for row in sorted_overall_data for word in row) + 2  # padding
            for row in sorted_overall_data:
                print("".join(word.ljust(col_width) for word in row))



        # Get category recall
        category_data = []
        for i in range(0,len(phi_categories)):
            category_tag = phi_categories[i]
            category_fns = category_tag + '_fns'
            category_tps = category_tag + '_tps'

            category_recall = 0
            if category_dict[category_fns] != 0:
                category_recall = category_dict[category_tps]/(category_dict[category_fns] + category_dict[category_tps])
            else:
                category_recall = 1
            category_data.append([category_tag,"{:.2%}".format(category_recall),str(category_dict[category_tps]),str(category_dict[category_fns])])

        # pretty print category recalls
        category_data.sort(key=lambda x: float(x[1][:-1]),reverse=True)
        sorted_category_data = [["Category","Recall","TPs","FNs"]]
        for item in category_data:
            sorted_category_data.append(item)
        if self.verbose:
            print('\n')
            print('Recall by PHI Category:')
            col_width = max(len(word) for row in category_data for word in row) + 2  # padding
            for row in sorted_category_data:
                print("".join(word.ljust(col_width) for word in row))
        




        ######### Get corrected recall, precision ##########


        corrected_recall = 0
        if include_dict['fns'] != 0:
            corrected_recall = include_dict['tps']/(include_dict['fns'] + include_dict['tps'])
        else:
            corrected_recall = 1

        corrected_precision = 0
        if include_dict['fps'] != 0:
            corrected_precision = include_dict['tps']/(include_dict['fps'] + include_dict['tps'])
        else:
            corrected_precision = 1

        specificity = 0
        if include_dict['fps'] != 0:
            specificity = include_dict['tns']/(include_dict['fps'] + include_dict['tns'])
        else:
            specificity = 1

        print('\n')
        print("Corrected Results:")
        print('\n')
        print("cTP:",include_dict['tps'], "cFN:", include_dict['fns'], "cTN:", include_dict['tns'], "cFP:", include_dict['fps'])
        print("Corrected Recall: " + "{:.2%}".format(corrected_recall))
        print("Corrected Precision: " + "{:.2%}".format(corrected_precision))
        print("Corrected Retention: " + "{:.2%}".format(specificity))
        print('\n')


        ######### Patient-only recall, precision ##########


        patient_recall = 0
        if patient_phi_dict['fns'] != 0:
            patient_recall = patient_phi_dict['tps']/(patient_phi_dict['fns'] + patient_phi_dict['tps'])
        else:
            patient_recall = 1

        patient_precision = 0
        if patient_phi_dict['fps'] != 0:
            patient_precision = patient_phi_dict['tps']/(patient_phi_dict['fps'] + patient_phi_dict['tps'])
        else:
            patient_precision = 1

        patient_specificity = 0
        if patient_phi_dict['fps'] != 0:
            patient_specificity = patient_phi_dict['tns']/(patient_phi_dict['fps'] + patient_phi_dict['tns'])
        else:
            patient_specificity = 1

        print('\n')
        print("Patient-Only Results:")
        print('\n')
        print("cTP:",patient_phi_dict['tps'], "cFN:", patient_phi_dict['fns'], "cTN:", patient_phi_dict['tns'], "cFP:", patient_phi_dict['fps'])
        print("Patient PHI Recall: " + "{:.2%}".format(patient_recall))
        print("Precision: " + "{:.2%}".format(patient_precision))
        print("Retention: " + "{:.2%}".format(patient_specificity))
        print('\n')



       ######### Provider-only recall, precision ##########


        provider_recall = 0
        if provider_phi_dict['fns'] != 0:
            provider_recall = provider_phi_dict['tps']/(provider_phi_dict['fns'] + provider_phi_dict['tps'])
        else:
            patient_recall = 1

        provider_precision = 0
        if provider_phi_dict['fps'] != 0:
            provider_precision = provider_phi_dict['tps']/(provider_phi_dict['fps'] + provider_phi_dict['tps'])
        else:
            patient_precision = 1

        provider_specificity = 0
        if provider_phi_dict['fps'] != 0:
            provider_specificity = provider_phi_dict['tns']/(provider_phi_dict['fps'] + provider_phi_dict['tns'])
        else:
            provider_specificity = 1

        print('\n')
        print("Provider-Only Results:")
        print('\n')
        print("cTP:",provider_phi_dict['tps'], "cFN:", provider_phi_dict['fns'], "cTN:", provider_phi_dict['tns'], "cFP:", provider_phi_dict['fps'])
        print("Provider PHI Recall: " + "{:.2%}".format(provider_recall))
        print("Precision: " + "{:.2%}".format(provider_precision))
        print("Retention: " + "{:.2%}".format(provider_specificity))
        print('\n')



        ######## Summarize FN results #########
        
        ##### With and without context #####
        
        # With context:
        # Condensed tags will contain id, word, PHI tag, POS tag, occurrences
        fn_tags_condensed_context = {}
        # Stores lists that represent distinct groups of words, PHI and POS tags
        fn_tags_condensed_list_context = []
        
        # No context:
        # Condensed tags will contain id, word, PHI tag, POS tag, occurrences
        fn_tags_condensed = {}
        # Stores lists that represent distinct groups of words, PHI and POS tags
        fn_tags_condensed_list = []

        # Keep track of how many distinct combinations we've added to each list
        context_counter = 0
        nocontext_counter = 0
        for fn in fn_tags:
            file_dict = fn_tags[fn] 
            for subfile in file_dict:
                current_list_context = file_dict[subfile]
            ##############################
                current_list_nocontext = current_list_context[:3] + [current_list_context[-3]] + [current_list_context[-2]] + [current_list_context[-1]]
            ############################
                
                word = current_list_context[0]
                phi_tag = current_list_context[1]
                pos_tag = current_list_context[2]
                fn_context = current_list_context[3].replace("\n"," ")
                filter_matches_exclude = current_list_context[5]
                filter_matches_include = current_list_context[6]
                
                # Context: add each occurrence with corresponding filename                    
                fn_tags_condensed_list_context.append(current_list_context)
                key_name = "uniq" + str(context_counter)
                filename = fn.split('/')[-1]
                include_exclude = current_list_context[4]
                fn_tags_condensed_context[key_name] = [word, phi_tag, pos_tag, fn_context, filename, include_exclude, filter_matches_exclude, filter_matches_include]
                context_counter += 1

                # No context
                if current_list_nocontext not in fn_tags_condensed_list:   
                    fn_tags_condensed_list.append(current_list_nocontext)
                    key_name = "uniq" + str(nocontext_counter)
                    fn_tags_condensed[key_name] = [word, phi_tag, pos_tag, 1, include_exclude, filter_matches_exclude, filter_matches_include]
                    nocontext_counter += 1
                else: 
                    uniq_id_index = fn_tags_condensed_list.index(current_list_nocontext)
                    uniq_id = "uniq" + str(uniq_id_index)
                    fn_tags_condensed[uniq_id][3] += 1

        ####### Summariz FP results #######

        # With context
        # Condensed tags will contain id, word, POS tag, occurrences
        fp_tags_condensed_context = {}
        # Stores lists that represent distinct groups of wordss and POS tags
        fp_tags_condensed_list_context = []

        # No context
        # Condensed tags will contain id, word, POS tag, occurrences
        fp_tags_condensed = {}
        # Stores lists that represent distinct groups of wordss and POS tags
        fp_tags_condensed_list = []

        # Keep track of how many distinct combinations we've added to each list
        nocontext_counter = 0
        context_counter = 0
        for fp in fp_tags:
            file_dict = fp_tags[fp]
            for subfile in file_dict:
                current_list_context = file_dict[subfile]
                current_list_nocontext = current_list_context[:2] + [current_list_context[3]] + [current_list_context[4]]

                word = current_list_context[0]
                pos_tag = current_list_context[1]
                fp_context = current_list_context[2].replace("\n"," ")
                filter_matches_exclude = current_list_context[3]
                filter_matches_include = current_list_context[4]

                # Context: add each occurrence with corresponding filename
                fp_tags_condensed_list_context.append(current_list_context)
                key_name = "uniq" + str(context_counter)
                filename = fp.split('/')[-1]
                fp_tags_condensed_context[key_name] = [word, pos_tag, fp_context, filename, filter_matches_exclude, filter_matches_include]
                context_counter += 1

                # No Context
                if current_list_nocontext not in fp_tags_condensed_list:
                    fp_tags_condensed_list.append(current_list_nocontext)
                    key_name = "uniq" + str(nocontext_counter)
                    fp_tags_condensed[key_name] = [word, pos_tag, 1, filter_matches_exclude, filter_matches_include]
                    nocontext_counter += 1
                else:
                    uniq_id_index = fp_tags_condensed_list.index(current_list_nocontext)
                    uniq_id = "uniq" + str(uniq_id_index)
                    fp_tags_condensed[uniq_id][2] += 1


        # Write FN and FP results to outfolder
        # Conext
        with open(self.eval_outpath + "fn_tags_context.txt", "w") as fn_file:
            fn_file.write("key" + "|" + "note_word" + "|" + "phi_tag" + "|" + "pos_tag" + "|" + "context" + "|" + "filename"+ "|" +"include_exclude" + "|" +"exclude_filters" + "|" +"include_filters" +"\n")
            for key in fn_tags_condensed_context:
                current_list = fn_tags_condensed_context[key]
                fn_file.write(key + "|" + current_list[0] + "|" + current_list[1] + "|" + current_list[2] + "|" + current_list[3] + "|" + current_list[4]+ "|" +current_list[5]+ "|" +str(current_list[6]) + "|" +str(current_list[7]) + "\n")
        
        with open(self.eval_outpath + "fp_tags_context.txt", "w") as fp_file:
            fp_file.write("key" + "|" + "note_word" + "|" + "pos_tag" + "|" + "context" + "|" + "filename"+ "|" +"exclude_filters" + "|" +"include_filters" +"\n")
            for key in fp_tags_condensed_context:
                current_list = fp_tags_condensed_context[key]
                fp_file.write(key + "|" + current_list[0] + "|" + current_list[1]  + "|" +  current_list[2] + "|" + current_list[3]+ "|" + str(current_list[4]) + "|" + str(current_list[5]) +"\n")

        # No context
        with open(self.eval_outpath + "fn_tags.txt", "w") as fn_file:
            fn_file.write("key" + "|" + "note_word" + "|" + "phi_tag" + "|" + "pos_tag" + "|" + "occurrences"+"|" +"include_exclude" + "|" +"exclude_filters" + "|" +"include_filters" + "\n")
            for key in fn_tags_condensed:
                current_list = fn_tags_condensed[key]
                fn_file.write(key + "|" + current_list[0] + "|" + current_list[1] + "|" + current_list[2] + "|" + str(current_list[3])+"|" + current_list[4]+ "|" + str(current_list[5])+ "|" + str(current_list[6])+"\n")
        
        with open(self.eval_outpath + "fp_tags.txt", "w") as fp_file:
            fp_file.write("key" + "|" + "note_word" + "|" + "pos_tag" + "|" + "occurrences"+ "|" +"exclude_filters" + "|" +"include_filters" + "\n")
            for key in fp_tags_condensed:
                current_list = fp_tags_condensed[key]
                fp_file.write(key + "|" + current_list[0] + "|" + current_list[1]  + "|" +  str(current_list[2])+ "|" + str(current_list[3]) + "|" + str(current_list[4]) +"\n")            
            
    
    def getphi(self, 
            anno_folder="data/i2b2_anno/", 
            anno_suffix="_phi_reduced.ano", 
            data_folder="data/i2b2_notes/", 
            output_folder="i2b2_phi", 
            filter_regex=None):
        """ get's phi from existing data to build up a data model
        data structure to hold our phi and classify phi we find
            {
                "foo.txt":[
                    {
                        "phi":"1/1/2019",
                        "context":"The data was 1/1/2019 and the patient was happy",
                        "class":"numer" //number, string ... 
                    },...
                ],...
            }
        """
        if self.run_eval:
            print("getphi")


        #use config if exists
        if self.anno_folder != None:
            anno_folder = self.anno_folder

        if self.anno_suffix != "":
            anno_suffix = self.anno_suffix

        phi = {}
        word_counts = {}
        not_phi = {}
        window_size = 10 #amount of words surrounding word to grab

        for root, dirs, files in os.walk(data_folder):
           
            for f in files:
               
                if not os.path.exists(root+f):
                    raise Exception("FILE DOESNT EXIST", root+f)

                if len(anno_suffix) > 0:
                    if not os.path.exists(anno_folder+f.split(".")[0]+anno_suffix):
                        print("FILE DOESNT EXIST", anno_folder+f.split(".")[0]+anno_suffix)
                        continue
                else:
                    if not os.path.exists(anno_folder+f):
                        print("FILE DOESNT EXIST", anno_folder+f)
                        continue

                orig_filename = root+f
                encoding1 = self.detect_encoding(orig_filename)
                orig = open(orig_filename,"r", encoding=encoding1['encoding']).read()

                orig_words = re.split("\s+", orig)

                anno_filename = anno_folder+f.split(".")[0]+anno_suffix
                encoding2 = self.detect_encoding(anno_filename)
                anno = open(anno_filename,"r", encoding=encoding2['encoding']).read()
                anno_words = re.split("\s+", anno)

                anno_dict = {}
                orig_dict = {}

                for w in anno_words:
                    anno_dict[w] = 1

                for i,w in enumerate(orig_words):

                    #check for edge cases that should not be "words"
                    x = w.replace("_","").strip()
                    if len(x) == 0:
                        continue

                    #add all words to our counts
                    if w not in word_counts:
                        word_counts[w] = 0
                    word_counts[w] += 1

                    #check if this word is phi
                    if w not in anno_dict:

                        left_index = i - 10
                        if left_index < 0:
                            left_index = 0

                        right_index = i + 10
                        if right_index >= len(orig_words):
                            right_index = len(orig_words) - 1
                        window = orig_words[left_index:right_index]
                        if f not in phi:
                            phi[f] = []

                        c = "string"
                        if re.search("\d+", w):
                            c = "number"

                        phi[f].append({"phi":w,"context":window,"class":c})
                    else:
                        #add all words to our counts
                        if w not in not_phi:
                            not_phi[w] = 0
                        not_phi[w] += 1

        #save our phi with context
        json.dump(phi, open("data/phi/phi_context.json", "w"), indent=4)

        #save all phi word counts
        counts = {}
        num_phi = {}
        string_phi = {}
        
        for f in phi:
            for d in phi[f]:
                if d["phi"] not in counts:
                    counts[d["phi"]] = 0
                counts[d["phi"]] += 1
                if d["class"] == "number":
                    if d["phi"] not in num_phi:
                        num_phi[d["phi"]] = 0
                    num_phi[d["phi"]] += 1
                else:
                    if d["phi"] not in string_phi:
                        string_phi[d["phi"]] = 0
                    string_phi[d["phi"]] += 1

        #save all phi counts
        json.dump(counts, open("data/phi/phi_counts.json", "w"), indent=4)
        #save phi number counts
        json.dump(num_phi, open("data/phi/phi_number_counts.json", "w"), indent=4)
        #save phi string counts
        json.dump(string_phi, open("data/phi/phi_string_counts.json", "w"), indent=4)
        #save our total word counts
        json.dump(word_counts, open("data/phi/word_counts.json", "w"), indent=4)
        #save our total non_phi counts
        json.dump(not_phi, open("data/phi/non_phi_counts.json", "w"), indent=4)
        
        #get all non_phi counts by number or string
        non_phi_number = {}
        non_phi_string = {}
        for w in word_counts:
            if re.search("\d+", w):
                if w not in non_phi_number:
                    non_phi_number[w] = 0
                non_phi_number[w] += 1
            else:
                if w not in non_phi_string:
                    non_phi_string[w] = 0
                non_phi_string[w] += 1

        #save all phi string counts
        json.dump(non_phi_number, open("data/phi/non_phi_number_counts.json", "w"), indent=4)

        #save all phi number counts
        json.dump(non_phi_string, open("data/phi/non_phi_string_counts.json", "w"), indent=4)


    def mapphi(self, 
            phi_path="data/phi/phi_counts.json", 
            out_path="data/phi/phi_map.json",
            sorted_path="data/phi/phi_sorted.json",  
            digit_char="`", 
            string_char="?"):
        """ given all examples of the phi, creates a general representation 
            
            digit_char = this is what digits are replaced by
            string_char = this is what strings are replaced by
            any_char = this is what any random characters are replaced with
        """
        if self.run_eval:
            print("mapphi")

        d = json.load(open(phi_path, "r"))

        phi_map = {}

        for phi in d:
            wordlst = []
            phi_word = phi["phi"]
            for c in phi_word:
                if re.match("\d+", c):
                    wordlst.append(digit_char)
                elif re.match("[a-zA-Z]+", c):
                    wordlst.append(string_char)
                else:
                    wordlst.append(c)
            word = "".join(wordlst)
            if word not in phi_map:
                phi_map[word] = {'examples':{}}
            if phi_word not in phi_map[word]['examples']:
                phi_map[word]['examples'][phi_word] = []
            phi_map[word]['examples'][phi_word].append(phi) 

        #save the count of all representations
        for k in phi_map:
            phi_map[k]["count"] = len(phi_map[k]["examples"].keys())

        #save all representations
        json.dump(phi_map, open(out_path, "w"), indent=4)

        #save an ordered list of representations so we can prioritize regex building
        items = []
        for k in phi_map:
            items.append({"pattern":k, "examples":phi_map[k]["examples"], "count":len(phi_map[k]["examples"].keys())})

        items.sort(key=lambda x: x["count"], reverse=True)
        json.dump(items, open(sorted_path, "w"), indent=4)

