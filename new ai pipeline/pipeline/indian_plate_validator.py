"""
pipeline/indian_plate_validator.py
-----------------------------------
Closes a real gap found during forensic analysis: the format regex in
ocr.py / plate_aggregator.py accepts anything shaped like 2 letters +
digits + letters + digits — so 'HH01DP1218' passes as "valid" even
though HH is not a real Indian state/UT code. Only MH (Maharashtra)
is real.

This is exactly why, in one real merge test, the wrong plate won a
confidence tie-break: both candidates were "format-valid," so the
tie-break had no way to prefer the one with a real state code.

Usage: import is_real_state_code() and call it alongside (not instead
of) is_valid_indian_plate() from ocr.py — format-valid AND real-state-
code should both be true for a plate to be treated as trustworthy.
"""

from __future__ import annotations

import re

# Official Indian state/UT vehicle registration codes (stable government data).
# A few codes have real historical naming variation — both listed where that's
# the case so a genuinely valid plate is never wrongly rejected:
#   Odisha:      OD (current) and OR (older, still seen on roads)
#   Telangana:   TS (current) and TG (also used in practice)
#   Uttarakhand: UK (common)  and UA (also used)
# Extend this list if plates from a region appear that aren't covered.
INDIAN_STATE_CODES: frozenset[str] = frozenset({
    "AN",   # Andaman & Nicobar Islands
    "AP",   # Andhra Pradesh
    "AR",   # Arunachal Pradesh
    "AS",   # Assam
    "BR",   # Bihar
    "CH",   # Chandigarh
    "CG",   # Chhattisgarh
    "DD",   # Daman & Diu (legacy, merged into DN)
    "DL",   # Delhi
    "DN",   # Dadra & Nagar Haveli and Daman & Diu
    "GA",   # Goa
    "GJ",   # Gujarat
    "HR",   # Haryana
    "HP",   # Himachal Pradesh
    "JK",   # Jammu & Kashmir
    "JH",   # Jharkhand
    "KA",   # Karnataka
    "KL",   # Kerala
    "LA",   # Ladakh
    "LD",   # Lakshadweep
    "MP",   # Madhya Pradesh
    "MH",   # Maharashtra
    "MN",   # Manipur
    "ML",   # Meghalaya
    "MZ",   # Mizoram
    "NL",   # Nagaland
    "OD",   # Odisha (current code)
    "OR",   # Odisha (older code, still seen)
    "PY",   # Puducherry
    "PB",   # Punjab
    "RJ",   # Rajasthan
    "SK",   # Sikkim
    "TN",   # Tamil Nadu
    "TS",   # Telangana (current code)
    "TG",   # Telangana (also used)
    "TR",   # Tripura
    "UP",   # Uttar Pradesh
    "UK",   # Uttarakhand (common code)
    "UA",   # Uttarakhand (also used)
    "WB",   # West Bengal
    "BH",   # Bharat Series (new unified national registration)
})

# Same format as ocr.py's STANDARD_RTO_REGEX — kept separate so this
# file has zero import dependency on that module and can be tested alone.
_PLATE_FORMAT_RE = re.compile(r"^([A-Z]{2})\d{1,2}[A-Z]{1,3}\d{4}$")
_BHARAT_RE       = re.compile(r"^\d{2}BH\d{4}[A-Z]{1,2}$")


def is_real_state_code(plate_text: str) -> bool:
    """
    True only if the plate is format-valid AND its 2-letter prefix is a
    real Indian state/UT code.

    Strictly stronger than the format-only check — rejects 'HH01DP1218'
    outright (HH is not a real code), instead of letting it enter a
    confidence vote against 'MH01DP1218'.

    Bharat-Series plates (e.g. 22BH1234AA) are always accepted — their
    prefix is a year, not a state code, so state-code filtering doesn't
    apply.
    """
    if not plate_text:
        return False
    if _BHARAT_RE.match(plate_text):
        return True
    m = _PLATE_FORMAT_RE.match(plate_text)
    if not m:
        return False
    return m.group(1) in INDIAN_STATE_CODES


def filter_to_real_plates(candidate_texts: list[str]) -> list[str]:
    """
    Given several OCR candidate reads, keep only ones with a real state
    code. Use this to narrow the vote pool before plate resolution.
    Returns the original list unchanged if filtering would produce an
    empty list (safety: don't throw away all evidence when unsure).
    """
    real = [t for t in candidate_texts if is_real_state_code(t)]
    return real if real else candidate_texts


def best_plate_candidate(candidates: list[tuple[str, float]]) -> tuple[str, float] | None:
    """
    Given a list of (plate_text, confidence) tuples, pick the best one
    using state-code validity as a tiebreaker.

    Priority order:
      1. Format-valid + real state code, highest confidence
      2. Format-valid only (no real state code), highest confidence
      3. Any candidate, highest confidence (fallback)

    Returns None if the list is empty.
    """
    if not candidates:
        return None
    real_valid   = [(t, c) for t, c in candidates if is_real_state_code(t)]
    format_valid = [(t, c) for t, c in candidates if _PLATE_FORMAT_RE.match(t)]
    if real_valid:
        return max(real_valid, key=lambda x: x[1])
    if format_valid:
        return max(format_valid, key=lambda x: x[1])
    return max(candidates, key=lambda x: x[1])
