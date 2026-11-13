"""
Queries HMRC's official Check-a-UK-VAT-Number service and performs strict
bi-directional entity resolution between the HMRC tax record and Companies House.
"""

import os
import re
import time
from dataclasses import dataclass
from typing import Optional, Set, Tuple
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup


LEGAL_SUFFIXES = {
    "ltd", "limited", "plc", "llp", "uk", "holdings", "group",
    "services", "company", "co", "international", "and", "the",
}

# Generic trade words that cannot by themselves prove two companies are the same
GENERIC_TRADE_WORDS = {
    "tyres", "motors", "construction", "building", "builders", "electrical",
    "engineering", "solutions", "systems", "supplies", "distribution",
    "healthcare", "cosmetics", "packaging", "scaffolding", "glazing",
    "ceiliings", "refurbishments", "homes", "properties", "property",
    "leisure", "clothing", "import", "export", "industrial", "industries",
}

UK_POSTCODE_REGEX = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b",
    re.IGNORECASE,
)


@dataclass
class HMRCVerificationResult:
    vat_number: str
    hmrc_is_registered: bool
    hmrc_registered_name: Optional[str]
    hmrc_postcode: Optional[str]
    entity_match_status: str  # VERIFIED_MATCH, VAT_GROUP_MATCH, PREVENTED_FALSE_POSITIVE, HMRC_NOT_FOUND
    name_jaccard_similarity: float
    postcode_exact_match: bool
    postcode_outcode_match: bool


def normalize_postcode(pc: Optional[str]) -> str:
    if not pc:
        return ""
    return re.sub(r"[^A-Z0-9]", "", pc.upper())


def tokenize_company_name(name: Optional[str]) -> Set[str]:
    """
    Tokenizes company name while preserving single-letter initials (so 'A A SMITH'
    and 'A & B SMITH' remain distinct) and stripping only legal suffixes.
    """
    if not name:
        return set()
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", name.lower())
    tokens = {t for t in cleaned.split() if len(t) >= 1 and t not in LEGAL_SUFFIXES}
    return tokens


def compute_name_similarity(name_a: Optional[str], name_b: Optional[str]) -> Tuple[float, int]:
    """
    Returns (jaccard_similarity, distinctive_shared_token_count).
    """
    tokens_a = tokenize_company_name(name_a)
    tokens_b = tokenize_company_name(name_b)
    if not tokens_a or not tokens_b:
        return 0.0, 0

    intersection = tokens_a.intersection(tokens_b)
    union = tokens_a.union(tokens_b)
    distinctive_shared = {t for t in intersection if len(t) > 1 and t not in GENERIC_TRADE_WORDS}

    jaccard = round(len(intersection) / len(union), 3)
    return jaccard, len(distinctive_shared)


class HMRCVatVerifier:
    """
    Verifies 9-digit UK VAT numbers against HMRC's live register on GOV.UK.
    """

    WEB_FORM_URL = "https://www.tax.service.gov.uk/check-vat-number/enter-vat-details"

    def __init__(self, delay_seconds: float = 1.0, oauth_bearer_token: Optional[str] = None):
        self.delay_seconds = delay_seconds
        self.oauth_token = oauth_bearer_token or os.environ.get("HMRC_OAUTH_TOKEN")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "en-GB,en;q=0.9",
        })

    def _lookup_via_govuk_web(self, vat_9_digits: str) -> Tuple[bool, Optional[str], Optional[str]]:
        try:
            get_resp = self.session.get(self.WEB_FORM_URL, timeout=12)
            if get_resp.status_code != 200:
                return False, None, None

            soup = BeautifulSoup(get_resp.text, "html.parser")
            form = soup.find("form")
            if not form:
                return False, None, None

            post_url = urljoin(self.WEB_FORM_URL, form.get("action") or self.WEB_FORM_URL)

            payload = {}
            text_field_found = False
            for inp in form.find_all("input"):
                name = inp.get("name")
                if not name:
                    continue
                inp_type = (inp.get("type") or "text").lower()
                if inp_type == "hidden":
                    payload[name] = inp.get("value", "")
                elif inp_type in ("text", "tel", "number") and not text_field_found:
                    payload[name] = vat_9_digits
                    text_field_found = True

            if not text_field_found:
                payload["target"] = vat_9_digits

            post_resp = self.session.post(
                post_url,
                data=payload,
                headers={"Referer": self.WEB_FORM_URL},
                timeout=12,
                allow_redirects=True,
            )
            if post_resp.status_code != 200:
                return False, None, None

            res_soup = BeautifulSoup(post_resp.text, "html.parser")
            strings = [s.strip() for s in res_soup.stripped_strings if s.strip()]
            full_text = " ".join(strings)

            if (
                "Invalid UK VAT number" in full_text
                or "does not match" in full_text
                or "Enter a valid UK VAT" in full_text
            ):
                return False, None, None

            reg_name = None
            reg_address_parts = []

            for idx, line in enumerate(strings):
                low = line.lower()
                if low == "registered business name" and idx + 1 < len(strings):
                    reg_name = strings[idx + 1]
                elif low == "registered business address" and idx + 1 < len(strings):
                    for addr_line in strings[idx + 1 : idx + 8]:
                        if any(
                            stop in addr_line.lower()
                            for stop in ("search completed", "check another", "print or save", "what did you think")
                        ):
                            break
                        reg_address_parts.append(addr_line)

            if not reg_name:
                return False, None, None

            addr_blob = " ".join(reg_address_parts)
            pc_match = UK_POSTCODE_REGEX.search(addr_blob) or UK_POSTCODE_REGEX.search(full_text)
            reg_postcode = pc_match.group(1).upper() if pc_match else None

            return True, reg_name, reg_postcode

        except requests.RequestException:
            return False, None, None

    def lookup_vat(self, vat_9_digits: str) -> Tuple[bool, Optional[str], Optional[str]]:
        time.sleep(self.delay_seconds)
        return self._lookup_via_govuk_web(vat_9_digits)

    def verify_and_resolve(
        self,
        vat_9_digits: str,
        target_company_name: str,
        target_postcode: str,
    ) -> HMRCVerificationResult:
        """
        Verifies the VAT number against HMRC and cross-checks the returned name
        and postcode against the target Companies House entity.
        """
        is_reg, hmrc_name, hmrc_pc = self.lookup_vat(vat_9_digits)

        if not is_reg:
            return HMRCVerificationResult(
                vat_number=vat_9_digits,
                hmrc_is_registered=False,
                hmrc_registered_name=None,
                hmrc_postcode=None,
                entity_match_status="HMRC_NOT_FOUND",
                name_jaccard_similarity=0.0,
                postcode_exact_match=False,
                postcode_outcode_match=False,
            )

        norm_target_pc = normalize_postcode(target_postcode)
        norm_hmrc_pc = normalize_postcode(hmrc_pc)

        exact_pc = bool(norm_target_pc and norm_hmrc_pc and norm_target_pc == norm_hmrc_pc)
        outcode_pc = bool(
            norm_target_pc and norm_hmrc_pc and norm_target_pc[:-3] == norm_hmrc_pc[:-3]
        )
        jaccard_sim, distinctive_count = compute_name_similarity(target_company_name, hmrc_name)

        # Tier 1: Exact postcode match + shares at least one distinctive name token
        if exact_pc and (jaccard_sim >= 0.45 or distinctive_count >= 1):
            status = "VERIFIED_MATCH" if jaccard_sim >= 0.45 else "VAT_GROUP_MATCH"
        # Tier 2: Same postcode area (outcode) + strong Jaccard similarity
        elif outcode_pc and jaccard_sim >= 0.65 and distinctive_count >= 1:
            status = "VERIFIED_MATCH"
        # Tier 3: Different postcode (e.g. trading HQ vs registered office), requires near-exact name match
        elif jaccard_sim >= 0.85 and distinctive_count >= 1:
            status = "VERIFIED_MATCH"
        else:
            status = "PREVENTED_FALSE_POSITIVE"

        return HMRCVerificationResult(
            vat_number=vat_9_digits,
            hmrc_is_registered=True,
            hmrc_registered_name=hmrc_name,
            hmrc_postcode=hmrc_pc,
            entity_match_status=status,
            name_jaccard_similarity=jaccard_sim,
            postcode_exact_match=exact_pc,
            postcode_outcode_match=outcode_pc,
        )