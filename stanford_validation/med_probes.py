import re
# TRUE medical values that MUST survive de-id — each anchored by a unit or clinical keyword so it is
# unambiguously clinical (not a date/id/zip fragment). group(1) if present is the value checked for survival.
PROBES = [
 ('bp_mmHg',  re.compile(r'\d{2,3}/\d{2,3}\s*mm\s?hg', re.I)),
 ('bp_range', re.compile(r'[<>]\s?\d{2,3}/\d{2,3}')),
 ('bp_ctx',   re.compile(r'(?i)(?:\bBP\b|\bB/P\b|blood pressure)[:\s]*(\d{2,3}/\d{2,3})')),
 ('velocity', re.compile(r'(?i)(\d{1,3}/\d{1,3})\s*cm/s')),
 ('strength', re.compile(r'(?i)(?:strength|motor|power)[^.\n]{0,15}?([0-5]/5)\b')),
 ('reflex',   re.compile(r'(?i)(?:reflex|dtr|biceps|triceps|patell|brachi|ankle|knee)[^.\n]{0,15}?([0-4]\+?/4)\b')),
 ('murmur',   re.compile(r'(?i)(?:murmur|grade)[^.\n]{0,12}?([1-6]/6)\b')),
 ('pain',     re.compile(r'(?i)pain[^.\n]{0,12}?\b((?:10|[0-9])/10)\b')),
 ('temp_F',   re.compile(r'(?i)\b(9[5-9]|10[0-6])(?:\.\d)?\s*°?\s*F\b')),
 ('temp_C',   re.compile(r'(?i)\b(3[5-9](?:\.\d)?)\s*°?\s*C\b')),
 ('hr',       re.compile(r'(?i)(?:\bHR\b|heart rate|\bpulse\b)[:\s]*(\d{2,3})\b')),
 ('rr',       re.compile(r'(?i)(?:\bRR\b|resp(?:iration|iratory)?)[:\s]*(\d{1,2})\b')),
 ('o2sat',    re.compile(r'(?i)(?:SpO2|SaO2|O2 ?sat|\bsat\b)[:\s]*(\d{2,3})\s*%')),
 ('o2_ra',    re.compile(r'(?i)(\d{2,3})\s*%\s*(?:on\s*)?(?:RA\b|room air)')),
 ('weight',   re.compile(r'(?i)\b(\d{2,3}(?:\.\d)?)\s*(?:kg|lbs?|pounds)\b')),
 ('height_cm',re.compile(r'(?i)\b(\d{2,3})\s*cm\b(?![/\d])')),
 ('bmi',      re.compile(r'(?i)\bBMI[:\s]*(\d{2}(?:\.\d)?)')),
 ('ef',       re.compile(r'(?i)(?:\bEF\b|ejection fraction)[:\s]*(\d{2})\s*%')),
 ('pct',      re.compile(r'(?i)(?:stenosis|blockage|occlusion|reduction|improvement)[^.\n]{0,12}?(\d{1,3})\s*%')),
 ('lab_unit', re.compile(r'(?i)\b(\d+(?:\.\d+)?)\s*(?:mg/dL|mmol/L|mEq/L|g/dL|ng/mL|mcg/mL|IU/L|U/L|k/uL)\b')),
 ('lab_named',re.compile(r'(?i)\b(?:sodium|potassium|chloride|CO2|BUN|creat(?:inine)?|glucose|hemoglobin|Hgb|Hct|WBC|platelets?|INR|A1c|HbA1c|TSH|calcium|magnesium|phosph\w+|albumin|bili\w*|troponin|BNP|lactate)[:\s]+(\d{1,3}(?:\.\d+)?)(?!\d)')),
 ('dose_mg',  re.compile(r'(?i)\b(\d+(?:\.\d+)?)\s*mg\b')),
 ('dimension',re.compile(r'(?i)\b(\d+(?:\.\d+)?\s*[xX]\s*\d+(?:\.\d+)?)\s*(?:cm|mm)\b')),
]
def matches(text):
    out=[]
    for label,rx in PROBES:
        for m in rx.finditer(text):
            out.append((label, m.group(1) if m.groups() and m.group(1) else m.group(0)))
    return out
