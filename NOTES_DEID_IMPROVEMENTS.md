# Notes de-id improvements — plan (grounded in a local run on real STARR notes)

Goal: move Philter output from **redaction (asterisks)** to **surrogate replacement** ("hiding in plain
sight") so downstream NLP/analysis isn't tripped, stop over-removing clinical content, and preserve note
formatting. Dev/validation on **Stanford STARR** notes, then backfill existing sites.

## Reproducible local harness (no AWS needed)
```bash
cd ~/GithubRepos/philter-plus-deidentification            # branch AWS_Integration
python3 -m venv venv && . venv/bin/activate
pip install nltk==3.8.1 dateparser chardet numpy pandas lxml xmltodict word2number \
            'setuptools<81' pyprof2calltree            # setuptools shims distutils for py3.12+
python -c "import nltk;[nltk.download(d) for d in ['punkt','punkt_tab','averaged_perceptron_tagger','averaged_perceptron_tagger_eng','maxent_ne_chunker','maxent_ne_chunker_tab','words','stopwords']]"
# sample notes come from db_phi_starr/<extract>/clinical_note.csv column `text` (PHI — keep local only)
python main.py -i <notes_dir>/ -o <out_dir>/ -f configs/philter_one.json \
       --outputformat asterisk -e False -v False -x /tmp/phi.json -c /tmp/coords.json
```
(The Stanford NER warning is harmless — the jar isn't installed; Philter falls back to its regex/POS filters.)

## Demonstrated baseline (real STARR cardiology note, asterisk mode)
Input (excerpt):
```
Maron, David Joel, MD     12/10/2019  7:27 PM
... the ABPM data suggests ¿ 24 hour SYS and DIA hypertension (132/82 mmHg)
... Ambulatory BP: 24-Hour <130/80, Awake <135/85, Asleep, <120/75.
David J. Maron, MD
```
Current output:
```
*****, ***** ****, MD     **/**/****  7:27 PM
... the **** data suggests ¿ 24 hour SYS and *** hypertension (***/** mmHg)
... Ambulatory BP: 24-Hour <130/80, Awake <***/**, Asleep, <120/75.
***** *. *****, MD
```
Three problems, all reproduced:
1. **Medical content destroyed (highest priority).** Blood-pressure readings `132/82`, `135/85` are redacted
   as dates, and "DIA" (diastolic) is dropped — and it's **inconsistent** (`130/80`, `120/75` survive). `NN/NN`
   collides with the date regexes.
2. **Asterisk soup.** Dates → `**/**/****`, names → `*****, ***** ****` — exactly what trips downstream NLP.
3. **Formatting.** *Intact here* — asterisk mode is char-for-char. The paragraph/line-break mangling reported
   is in the **XML → surrogate `.txt`** path (`surrogator.py`), not asterisk mode. Confirm & fix there.

## Where each fix lives (all within the existing framework)

### 1 & 2 & 3 — Surrogate replacement (shifted dates + fake names, fewer asterisks)
- `surrogator.py` already: reads Philter XML tags, **date-shifts** DATE tags, replaces other PHI by tag type,
  writes `.txt`. It is **not wired into the fast AWS path** (`process_parquet_aws.py` runs asterisk Philter only).
- **Do:** in the parquet path, run Philter in tag mode (`--prod` → i2b2 XML) then `surrogator` per note.
- **Per-patient shift is already available:** the input parquet carries **`ShiftedDays`** (and
  `ShiftedContactDate`) per row (see CLAUDE.md schema) — feed that into `surrogator.lookup_date_shift`
  (replace the notes_metadata-folder lookup / random fallback) so note dates match the structured de-id dates.
  In OMOP terms this is `identity.patient_crosswalk.date_shift_offset_days`.
- **Names → deterministic fake names:** `replace_other_surrogate` — make the pseudonym a stable hash of the
  real token (same real name → same fake name within a patient), not random per occurrence.

### 4 — Stop over-removing medical terms (the BP case)
- Root cause: date regexes in `filters/regex/dates/*_transformed.txt` (`phi_type: DATE`) match bare `NN/NN`.
  They already carry long negative lookarounds (`(?<!\spain\s)`, `(?!\ssystolic)`, `(?!%)`, …) — but **miss
  `mmHg`** and the `(SYS/DIA … NNN/NN mmHg)` vitals shape.
- Mechanism to use: `exclude:false` "safe" whitelists already exist — including **`filters/regex/safe/bp_safe.txt`**,
  `sao2_safe`, `ekg_safe`, `measurement_safe`. Strengthen `bp_safe` (and add a vitals-ratio safe: `\d{2,3}/\d{2,3}\s*mmHg`,
  and `BP:? \d{2,3}/\d{2,3}`) so BP/ratios are protected; add `mmHg` to the date-regex negative lookahead.
- **Method (do this empirically):** build a STARR **false-positive test set** — notes with known vitals/labs/
  ratios — and a **recall test set** (known PHI) so tuning a whitelist can't silently drop real PHI. Iterate:
  measure FP (medical destroyed) and FN (PHI leaked) on every change. Lead with the highest-frequency FPs.

### 5 — Preserve formatting
- Asterisk mode preserves layout; the surrogate/`.txt` reconstruction in `surrogator.py` is the suspect
  (paragraph/line breaks collapsed). Confirm by running `--prod` → `surrogator`, diff whitespace vs input;
  fix the reconstruction to keep original newlines/indentation so sections remain isolable.

## Sequencing
1. **Harness + test sets** (FP medical + FN PHI) on STARR — the safety net for everything else.
2. **Medical-preservation pass** (#4): strengthen `bp_safe` + date negative-context; re-measure FP/FN. *(fast, high value, demonstrable on the note above)*
3. **Surrogate wiring** (#1–3): Philter-tag → `surrogator` with `ShiftedDays`; deterministic fake names.
4. **Formatting** (#5): fix `surrogator` `.txt` reconstruction.
5. **Integrate into `process_parquet_aws.py`** (the fast S3-parquet path) + validate end-to-end on a STARR batch.
6. **Backfill** existing sites with the improved pipeline.

## Notes on wiring to our OMOP pipeline
Our identified OMOP `note.note_text` holds the real text; `identity.patient_crosswalk.date_shift_offset_days`
is the canonical shift. The de-id notes path = Philter-tag → surrogator(shift, fake names, keep formatting) →
de-id `omop_prod.note`; raw text tiers to parquet-on-S3. Same shift as structured de-id ⇒ note dates align
with structured dates and with the gold standard.
