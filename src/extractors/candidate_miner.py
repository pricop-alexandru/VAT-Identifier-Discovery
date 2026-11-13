"""
Extracts UK VAT and EORI candidates from raw web pages or search snippets.
Uses proximity window matching around tax/registration keywords plus Modulus 97/9755
mathematical filtering to maximize recall while preventing third-party contamination.
"""

import re
from dataclasses import dataclass
from typing import List

from src.validators.uk_vat_checksum import clean_vat_candidate, is_valid_uk_vat_checksum


# Matches explicit GB/XI prefixed 9 or 12 digit numbers (with optional spaces/hyphens)
EXPLICIT_GB_PATTERN = re.compile(
    r"\b(?:GB|XI)[\s\-\.]?(?:\d[\s\-\.]?){8}\d(?:[\s\-\.]?\d{3})?\b",
    re.IGNORECASE,
)

# Matches any 9-digit or 12-digit sequence (including UK 3-4-2 and 3-3-3 spaced formats)
# e.g., 123456789, 123 4567 89, 123 456 789, 123-4567-89, or 123456789000
GENERIC_9_OR_12_DIGIT_PATTERN = re.compile(
    r"(?<!\d)(?:\d{9}|\d{12}|\d{3}[\s\-]\d{4}[\s\-]\d{2}|\d{3}[\s\-]\d{3}[\s\-]\d{3}|\d{3}[\s\-]\d{3}[\s\-]\d{3}[\s\-]\d{3})(?!\d)"
)

TAX_CONTEXT_KEYWORDS = (
    "vat",
    "v.a.t",
    "value added tax",
    "eori",
    "tax id",
    "tax no",
    "tax registration",
    "vat reg",
)

THIRD_PARTY_POISON_TERMS = (
    "designed by",
    "website by",
    "built by",
    "powered by",
    "klarna",
    "clearpay",
    "stripe",
    "paypal",
    "shopify",
    "feefo",
    "trustpilot",
)


@dataclass
class ExtractedCandidate:
    vat_number: str
    source_type: str  # EXPLICIT_GB, CONTEXT_VAT, or DERIVED_FROM_EORI
    checksum_algorithm: str
    crn_anchored_on_page: bool
    postcode_anchored_on_page: bool
    third_party_warning: bool
    context_snippet: str
    confidence_score: float


def check_crn_on_page(text: str, company_number: str) -> bool:
    if not company_number:
        return False
    padded = company_number.strip().zfill(8)
    unpadded = padded.lstrip("0")
    patterns = [rf"\b{re.escape(padded)}\b"]
    if len(unpadded) >= 6 and unpadded != padded:
        patterns.append(rf"\b{re.escape(unpadded)}\b")
    combined = "|".join(patterns)
    return bool(re.search(combined, text, re.IGNORECASE))


def check_postcode_on_page(text: str, postcode: str) -> bool:
    if not postcode:
        return False
    norm_pc = re.sub(r"\s+", "", postcode.upper())
    if len(norm_pc) < 5:
        return False
    outcode, incode = norm_pc[:-3], norm_pc[-3:]
    pattern = rf"\b{re.escape(outcode)}\s*{re.escape(incode)}\b"
    return bool(re.search(pattern, text, re.IGNORECASE))


def extract_vat_candidates_from_text(
    text: str,
    target_company_number: str,
    target_postcode: str,
) -> List[ExtractedCandidate]:
    """
    Scans text for explicit GB numbers and any 9/12-digit numbers near tax keywords,
    filters via Modulus 97/9755 checksum, and scores each candidate.
    """
    if not text:
        return []

    crn_present = check_crn_on_page(text, target_company_number)
    postcode_present = check_postcode_on_page(text, target_postcode)

    raw_matches = []

    # 1. Explicit GB/XI prefixed numbers anywhere in text
    for match in EXPLICIT_GB_PATTERN.finditer(text):
        raw_matches.append((match.group(0), "EXPLICIT_GB", match.start(), match.end()))

    # 2. Any 9-digit or 12-digit number within +/- 100 chars of a VAT/EORI keyword
    for match in GENERIC_9_OR_12_DIGIT_PATTERN.finditer(text):
        start_idx, end_idx = match.start(), match.end()
        ctx_start = max(0, start_idx - 100)
        ctx_end = min(len(text), end_idx + 100)
        window_lower = text[ctx_start:ctx_end].lower()

        if any(kw in window_lower for kw in TAX_CONTEXT_KEYWORDS):
            # Ignore if the 8-digit CRN is embedded inside this match or if preceded by 'company' / 'tel'
            immediate_prefix = text[max(0, start_idx - 25) : start_idx].lower()
            if any(tel_kw in immediate_prefix for tel_kw in ("tel", "phone", "call", "fax", "mob", "ico")):
                continue
            source_label = "DERIVED_FROM_EORI" if "eori" in window_lower else "CONTEXT_VAT"
            raw_matches.append((match.group(0), source_label, start_idx, end_idx))

    seen_vats = {}

    for raw_str, source_type, start_idx, end_idx in raw_matches:
        cleaned_digits = re.sub(r"[^0-9]", "", raw_str)
        if len(cleaned_digits) == 12 and cleaned_digits.endswith("000"):
            source_type = "DERIVED_FROM_EORI"

        candidate_9 = clean_vat_candidate(raw_str)
        if not candidate_9:
            continue

        # Ensure we didn't accidentally grab the company's own CRN padded with a digit
        if target_company_number and target_company_number.zfill(8) in candidate_9:
            continue

        is_valid, algo = is_valid_uk_vat_checksum(candidate_9)
        if not is_valid:
            continue

        win_start = max(0, start_idx - 80)
        win_end = min(len(text), end_idx + 80)
        snippet = re.sub(r"\s+", " ", text[win_start:win_end]).strip()
        snippet_lower = snippet.lower()

        third_party_flag = any(term in snippet_lower for term in THIRD_PARTY_POISON_TERMS)

        score = 0.55
        if source_type == "EXPLICIT_GB":
            score += 0.20
        elif source_type == "CONTEXT_VAT":
            score += 0.15
        elif source_type == "DERIVED_FROM_EORI":
            score += 0.15
        if crn_present:
            score += 0.15
        if postcode_present:
            score += 0.10
        if third_party_flag:
            score -= 0.45

        score = max(0.05, min(0.99, round(score, 2)))

        candidate_obj = ExtractedCandidate(
            vat_number=candidate_9,
            source_type=source_type,
            checksum_algorithm=algo,
            crn_anchored_on_page=crn_present,
            postcode_anchored_on_page=postcode_present,
            third_party_warning=third_party_flag,
            context_snippet=snippet,
            confidence_score=score,
        )

        if candidate_9 not in seen_vats or candidate_obj.confidence_score > seen_vats[candidate_9].confidence_score:
            seen_vats[candidate_9] = candidate_obj

    return sorted(seen_vats.values(), key=lambda x: x.confidence_score, reverse=True)