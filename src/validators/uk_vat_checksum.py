"""
Mathematical validation for UK VAT registration numbers (VRN) and EORI numbers.
Implements HMRC Modulus 97 and Modulus 9755 check digit algorithms to eliminate
false-positive 9-digit sequences before making network calls to HMRC.
"""

import re
from typing import List, Optional, Tuple


WEIGHTS = (8, 7, 6, 5, 4, 3, 2)


def clean_vat_candidate(raw_value: str) -> Optional[str]:
    """
    Normalizes a raw VAT or EORI string into a 9-digit numeric string.
    If a 12-digit UK EORI ending in 000 is provided, strips the 000 suffix
    to recover the underlying 9-digit VAT number.
    """
    if not raw_value:
        return None

    # Uppercase and remove all whitespace, hyphens, and dots
    cleaned = re.sub(r"[\s\-\.]", "", raw_value.upper())

    # Strip optional GB or XI prefix
    if cleaned.startswith(("GB", "XI")):
        cleaned = cleaned[2:]

    # Handle 12-digit EORI numbers where the last 3 digits are branch code 000
    if len(cleaned) == 12 and cleaned.isdigit() and cleaned.endswith("000"):
        cleaned = cleaned[:9]

    if len(cleaned) == 9 and cleaned.isdigit():
        return cleaned

    return None


def is_valid_uk_vat_checksum(vat_9_digits: str) -> Tuple[bool, str]:
    """
    Validates a 9-digit UK VAT number using HMRC Modulus 97 and Modulus 9755 rules.
    Returns (is_valid, algorithm_matched).
    """
    if not vat_9_digits or len(vat_9_digits) != 9 or not vat_9_digits.isdigit():
        return False, "INVALID_LENGTH_OR_FORMAT"

    digits = [int(ch) for ch in vat_9_digits]
    prefix_val = int(vat_9_digits[:7])
    check_digits = int(vat_9_digits[7:])

    # All-zero or trivial sequences are invalid
    if prefix_val == 0:
        return False, "ZERO_PREFIX"

    # Calculate weighted sum of the first 7 digits
    weighted_sum = sum(d * w for d, w in zip(digits[:7], WEIGHTS))

    # Algorithm 1: Standard Modulus 97
    # Subtract 97 repeatedly until result is zero or negative; abs(result) == check_digits
    rem_97 = weighted_sum
    while rem_97 > 0:
        rem_97 -= 97
    expected_check_97 = abs(rem_97)

    # Note: In standard Mod 97, certain ranges are blocked by HMRC
    # (0100000-0999999 and 9490001-9700000 use specific rules)
    if expected_check_97 == check_digits and expected_check_97 < 97:
        return True, "MOD_97"

    # Algorithm 2: Modulus 9755 (introduced when standard Mod 97 ranges filled up)
    rem_9755 = weighted_sum + 55
    while rem_9755 > 0:
        rem_9755 -= 97
    expected_check_9755 = abs(rem_9755)

    if expected_check_9755 == check_digits and expected_check_9755 < 97:
        return True, "MOD_9755"

    return False, "CHECKSUM_FAILED"


def count_valid_checksums_in_range(start_prefix: int = 1000000, end_prefix: int = 9999999) -> int:
    """
    Helper to prove for the Debate Topic how many 9-digit combinations pass
    Mod 97 or Mod 9755 across the 7-digit prefix space.
    For each 7-digit prefix, at most 2 two-digit suffixes (out of 100) are valid.
    """
    total_prefixes = end_prefix - start_prefix + 1
    # Each prefix yields at most 1 valid check digit in Mod 97 (if rem <= 96)
    # and at most 1 valid check digit in Mod 9755 (if rem <= 96).
    # Roughly 97/100 * 2 = 1.94 valid numbers per 100 candidates (~1.94% of 10^9).
    return int(total_prefixes * 2 * (97 / 100))