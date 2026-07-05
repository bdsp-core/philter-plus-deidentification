# Clinical Note De-identification — Methodology

*How free-text clinical notes are de-identified in the BDSP pipeline. Written for transparency; a
version of this is intended for public documentation on the project website.*

## Goal

Remove protected health information (PHI) from free-text clinical notes so the notes can be shared for
research under a de-identified data-use model, while **preserving the clinical and scientific content**
that makes the notes useful. Two failure modes matter equally:

- **Leaving PHI in** (a re-identification risk), and
- **Destroying medical information** — over-aggressive redaction that removes lab values, biomarkers,
  disease names, genomic coordinates, etc., which quietly ruins the data for research.

Our design treats both as first-class; most of the engineering below is about the second, which
conventional redaction tools handle poorly.

## Two stages: detect, then replace with surrogates

1. **PHI detection.** We use the **Philter** NLP engine (rule/pattern/gazetteer-based) to locate spans of
   likely PHI and label each with a type (name, date, location, contact, identifier, age ≥ 90, …).
2. **Surrogate replacement ("hiding in plain sight").** Instead of blacking PHI out with asterisks, we
   replace it with realistic *surrogates* of the same type. This keeps the text natural for downstream
   NLP and human review, and adds a security property (below). Replacement is **deterministic** (a keyed
   hash), so the same real value always maps to the same surrogate, but the mapping is not reversible
   without the secret key.

### What each PHI type becomes

| PHI type | Replacement |
|---|---|
| **Dates** | the real date **shifted by that patient's canonical offset** (the same per-patient shift applied to the structured data), so the note's internal timeline and its alignment with structured events are preserved, but absolute dates are hidden. Unparseable/short forms fall back to redaction rather than leak. |
| **Names** | a **deterministic, gender-preserving fake name** drawn from large realistic name lists (≈161k surnames, thousands of gendered first names). Same real name → same fake name everywhere (coreference preserved); first-name gender is matched; capitalization/initials/format are preserved. |
| **Locations / towns / addresses** | a **fake place** (fake city/street from generic pools); street numbers and ZIP codes are deterministically perturbed; generic suffixes (Street, Ave, state) preserved. |
| **Identifiers, contacts, ages ≥ 90** | redacted. |
| **Institution names (site-specific)** | removed via a curated per-site list (e.g. Stanford Health Care, SHC, Lucile Packard, campus addresses, portal URLs). |

### A security benefit of surrogate names

Because real names are replaced with **plausible fake names** (not asterisks), a reader cannot tell,
for any given name in the output, whether it is a surrogate or a real name that slipped through
detection. This makes any residual name **non-identifiable in isolation** — an extra layer of protection
beyond detection recall alone.

## Preserving medical information

Clinical text is full of tokens that *look* like PHI to a naive detector — biomarkers (`HER2`, `Ki-67`),
lab techniques (`ISH`, `FISH`, `PAS`), blood pressures (`152/64`), genomic coordinates
(`arr[GRCh37] …(23656936_28520313)`), and **eponymous diseases** (`Prader-Willi`, `Angelman`,
`Parkinson`) that are literally people's surnames. We protect these with several mechanisms:

- **A curated clinical whitelist** of biomarkers, stains, microorganisms, lab/technique abbreviations, and
  standard clinical labels (`DOB`, `HPI`, …) that must never be removed.
- **A large dictionary whitelist** (English + medical vocabulary, minus known personal names) so ordinary
  words a detector mis-flags are protected.
- **Context-aware eponym preservation.** Any `<Capitalized> disease/syndrome/sign/stain/…` is kept
  (so `Von Kossa stain`, `Prader-Willi syndrome` survive) *while the same surname still gets removed when
  it appears as a person* (`Mr. Wilson`).
- **Measurement/date disambiguation.** A `NN/NN` whose first number exceeds 12 cannot be a month, so it is
  treated as a **blood pressure/measurement, not a date**; single-digit `N/N` (e.g. `2/2`, "secondary
  to") is treated as shorthand; blood pressures with `mmHg`/ranges are protected.
- **Genomic/cytogenetic coordinates** (chromosome bands, base-pair ranges, `Mb`/`kb` sizes) are preserved.
- **Age < 90** and `NNF`/`NNM` sex shorthand are preserved (only ages ≥ 90 are PHI).

### The whitelist-restore guard

Rather than rely solely on the detector's internal precedence, a final **guard** runs over the output:
any token whose text is a known-safe term is **restored to the original** before it can be redacted or
surrogated. It is **case-aware** — a lowercase dictionary word (never a name in clinical prose) is always
restored, while a capitalized token is restored only if it is a curated medical term (so real names,
which are capitalized, are not accidentally kept). The guard is a single linear pass with set lookups —
negligible cost even at hundreds of millions of notes.

## Formatting is preserved

De-identification operates character-by-character and copies every non-PHI character verbatim, including
whitespace, line breaks, and column alignment — so note structure (sections, tables, signatures) is
retained and downstream tools can still parse it.

## Validation

We evaluate on real notes across note types (progress notes, radiology, pathology), measuring two axes:

- **Recall (leakage).** We scan the de-identified output for surviving PHI patterns (names verified
  against the input, phone/email/SSN/URL, identifiers) — the target is zero real leaks.
- **Precision (medical destruction).** Categorized probes (blood pressures, labs, doses, dimensions,
  biomarkers, genomic coordinates, eponyms) confirm clinical values survive.

Every change is also human-reviewable in a side-by-side viewer (original vs de-identified, color-coded by
change type), which drives an iterative "find an error class → fix it → re-measure" loop.

## Known limitations & ongoing work

- **Name detection recall** is the residual risk: rare or non-Western names the detector doesn't recognize
  can be missed. This is mitigated by (a) the surrogate property above and (b) — the strongest fix —
  injecting the **known patient and provider names from the structured record** into detection per note,
  so a patient's own name is caught regardless of the general model.
- Whitelists and site lists are living artifacts, expanded continuously from validation findings.
