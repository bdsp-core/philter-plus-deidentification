# Stanford de-id validation harness (local, no PHI committed)

Reusable scorers for iterating on the de-id pipeline against real STARR notes (kept local only).

- `med_probes.py` — categorized clinical-value probes (BP, vitals, labs, doses, dims, velocity...).
- `score_deid.py` — FP (medical destroyed) + FN (name-leak, date-leak). Usage:
  `python score_deid.py <notes_dir> <deid_dir> <known.json>`
- `missed_phi_audit.py` — **recall audit**: scans de-id OUTPUT for surviving PHI (phone/email/ssn/url,
  long-numbers, provider/`Name, MD`). Distinguishes surrogate fakes from real leaks by checking whether the
  surname also appears in the INPUT. Usage: `python missed_phi_audit.py <notes> <deid> <known.json>`
- `stanford_remove_miner.py` — mines surviving institution/clinic/Stanford phrases from de-id output ->
  candidates for a Stanford-specific `keyword_removal` list. Usage: `python stanford_remove_miner.py <deid>`

## Findings (2026-07-04, 220 notes = 100 clinical + 80 radiology + 40 pathology)
- **Recall**: 0/12 known name tokens leaked; 262 name residuals were ALL surrogates, **0 real name leaks**;
  0 phone/email/ssn/url; the 1 flagged "MRN" was a genomic base-pair coordinate (medical, not PHI).
- **Medical precision**: 0% of tested clinical values destroyed — but a NEW gap: **genomic coordinates**
  (`arr[GRCh37] ...(23656936_28520313)`, base-pair ranges) get partially redacted in path/cytogenetics -> add
  to the clinical whitelist.
- **Stanford remove-list candidates**: `SHC` (Stanford Health Care, 10x) + facility/lab names (Immunoperoxidase
  Laboratory, Virology Laboratory, Breast Health Center, Behavioral Health, Eye Clinic, Occupational Health...).
  Also: facility names with embedded words get partially name-surrogated (e.g. "Martie ... Laboratory") ->
  whole-phrase removal via the Stanford keyword list is the right fix.
