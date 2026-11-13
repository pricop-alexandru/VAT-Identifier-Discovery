"""
End-to-end Proof of Concept pipeline for UK VAT Identifier Discovery.
Prioritizes footer legal links (/terms, /privacy, /legal, /imprint) over top-navbar
links, validates Modulus 97/9755 checksums locally, and verifies every candidate
live against HMRC.
"""

import csv
import os
import re
import time
from typing import Dict, List, Set, Tuple
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

from src.extractors.candidate_miner import extract_vat_candidates_from_text
from src.verifiers.hmrc_verifier import HMRCVatVerifier


FALLBACK_LEGAL_PATHS = (
    "/terms-and-conditions",
    "/terms",
    "/privacy-policy",
    "/privacy",
    "/legal",
    "/contact-us",
    "/contact",
    "/about-us",
)

# High-priority legal keywords (checked first) vs secondary keywords (contact/about)
HIGH_PRIORITY_LEGAL_KEYWORDS = (
    "term", "condition", "privacy", "legal", "imprint",
    "vat", "policy", "compliance", "delivery", "shipping",
)
SECONDARY_LEGAL_KEYWORDS = ("contact", "about")


def fetch_page_and_legal_links(url: str, base_domain: str, timeout: int = 8) -> Tuple[str, str, List[str]]:
    """
    Fetches a URL, returns (visible_text, final_redirected_base_url, prioritized_legal_links).
    Scans bottom-of-page (footer) links and high-priority legal keywords first.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200 or "text/html" not in resp.headers.get("Content-Type", ""):
            return "", base_domain, []

        final_parsed = urlparse(resp.url)
        final_base = f"{final_parsed.scheme}://{final_parsed.netloc}"
        base_netloc = final_parsed.netloc.lower().replace("www.", "")

        soup = BeautifulSoup(resp.text, "html.parser")
        high_priority_links: List[str] = []
        secondary_links: List[str] = []

        # Iterate in reverse document order so footer links are captured before top navbar links
        for a_tag in reversed(soup.find_all("a", href=True)):
            href = a_tag["href"].strip()
            if href.startswith(("mailto:", "tel:", "javascript:")):
                continue
            link_text = a_tag.get_text(" ", strip=True).lower()
            full_url = urljoin(resp.url, href).split("#")[0]
            parsed = urlparse(full_url)
            target_netloc = parsed.netloc.lower().replace("www.", "")

            if target_netloc != base_netloc:
                continue
            if full_url.lower().endswith((".jpg", ".png", ".gif", ".svg", ".zip", ".mp4")):
                continue
            if full_url.rstrip("/") == final_base.rstrip("/"):
                continue

            combined_hint = f"{parsed.path.lower()} {link_text}"
            if any(kw in combined_hint for kw in HIGH_PRIORITY_LEGAL_KEYWORDS):
                if full_url not in high_priority_links:
                    high_priority_links.append(full_url)
            elif any(kw in combined_hint for kw in SECONDARY_LEGAL_KEYWORDS):
                if full_url not in secondary_links:
                    secondary_links.append(full_url)

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        prioritized = high_priority_links[:6] + secondary_links[:3]
        return soup.get_text(separator=" ", strip=True), final_base, prioritized[:8]
    except requests.RequestException:
        return "", base_domain, []


def gather_domain_text(domain: str) -> str:
    if not domain:
        return ""
    base_url = domain if domain.startswith("http") else f"https://{domain}"
    base_url = base_url.rstrip("/")

    home_text, resolved_base, dynamic_links = fetch_page_and_legal_links(base_url, base_url)
    if not home_text and not base_url.startswith("https://www."):
        # Retry with www. if bare domain failed
        www_url = base_url.replace("https://", "https://www.")
        home_text, resolved_base, dynamic_links = fetch_page_and_legal_links(www_url, www_url)

    aggregated = home_text
    visited: Set[str] = {base_url.rstrip("/"), resolved_base.rstrip("/")}

    # Interleave high-priority discovered links with fallback paths so /terms and /privacy are always tried
    urls_to_visit: List[str] = []
    for link in dynamic_links[:5]:
        urls_to_visit.append(link)
    for path in FALLBACK_LEGAL_PATHS:
        candidate_url = f"{resolved_base.rstrip('/')}{path}"
        if candidate_url not in urls_to_visit:
            urls_to_visit.append(candidate_url)
    for link in dynamic_links[5:]:
        if link not in urls_to_visit:
            urls_to_visit.append(link)

    pages_fetched = 0
    for sub_url in urls_to_visit:
        if pages_fetched >= 7:
            break
        norm_sub = sub_url.rstrip("/")
        if norm_sub in visited:
            continue
        visited.add(norm_sub)
        sub_text, _, _ = fetch_page_and_legal_links(sub_url, resolved_base)
        if sub_text:
            aggregated += "\n" + sub_text
            pages_fetched += 1

    return aggregated


def gather_search_snippets_for_vat(company_name: str, company_number: str, domain: str) -> str:
    """
    Queries DDGS strictly scoped to the company's domain (site:host) or its unique
    8-digit Companies House number. Never searches by company name alone.
    """
    if not domain:
        return ""

    host = urlparse(domain).netloc.lower().replace("www.", "")
    if not host:
        return ""

    no_parens = re.sub(r"\([^)]*\)", " ", company_name)
    clean_name = re.sub(
        r"\b(LIMITED|LTD|PLC|LLP)\b\.?", "", no_parens, flags=re.IGNORECASE
    ).strip()

    queries = [
        f'site:{host} ("VAT" OR "EORI" OR "GB")',
        f'"{clean_name}" "{company_number}" ("VAT" OR "GB")',
    ]

    snippets_blob = []
    for q in queries:
        try:
            time.sleep(1.2)
            with DDGS() as ddgs:
                results = list(ddgs.text(q, region="uk-en", max_results=5))
            for r in results:
                title = r.get("title", "")
                body = r.get("body", "") or r.get("snippet", "")
                href = r.get("href", "") or r.get("url", "")
                snippets_blob.append(f"{title} | {body} | {href}")
        except Exception:
            time.sleep(2.0)
            continue

    return "\n".join(snippets_blob)


def process_company_sample(input_csv: str, output_csv: str) -> Dict[str, int]:
    verifier = HMRCVatVerifier(delay_seconds=1.0)
    stats = {
        "total_companies": 0,
        "domains_located": 0,
        "stratum_a_total": 0,
        "stratum_a_verified": 0,
        "stratum_b_total": 0,
        "stratum_b_verified": 0,
        "candidates_passed_checksum": 0,
        "prevented_false_positives": 0,
        "derived_from_eori_verified": 0,
    }

    results_rows: List[Dict[str, str]] = []

    with open(input_csv, mode="r", encoding="utf-8") as infile:
        reader = csv.DictReader(infile)
        for row in reader:
            stats["total_companies"] += 1
            c_num = row["company_number"].strip()
            c_name = row["company_name"].strip()
            c_pc = row["postcode"].strip()
            stratum = row.get("stratum", "B").strip()
            domain = row.get("candidate_domain", "").strip()

            if stratum == "A":
                stats["stratum_a_total"] += 1
            else:
                stats["stratum_b_total"] += 1

            candidates = []
            if domain:
                stats["domains_located"] += 1

                # 1. Crawl homepage + footer legal pages (/terms, /privacy, /legal, /contact)
                aggregated_text = gather_domain_text(domain)

                # 2. Extract checksum-valid candidates from domain crawl
                candidates = extract_vat_candidates_from_text(
                    text=aggregated_text,
                    target_company_number=c_num,
                    target_postcode=c_pc,
                )

                # 3. Only if domain exists but HTML crawl found no candidates, check domain/CRN-locked snippets
                if not candidates:
                    snippet_text = gather_search_snippets_for_vat(c_name, c_num, domain)
                    candidates = extract_vat_candidates_from_text(
                        text=snippet_text,
                        target_company_number=c_num,
                        target_postcode=c_pc,
                    )

            stats["candidates_passed_checksum"] += len(candidates)

            verified_vat = ""
            verified_hmrc_name = ""
            final_status = "NOT_FOUND" if domain else "NO_DOMAIN"
            winning_source_type = ""

            for cand in candidates:
                res = verifier.verify_and_resolve(
                    vat_9_digits=cand.vat_number,
                    target_company_name=c_name,
                    target_postcode=c_pc,
                )
                if res.entity_match_status in ("VERIFIED_MATCH", "VAT_GROUP_MATCH"):
                    verified_vat = f"GB{cand.vat_number}"
                    verified_hmrc_name = res.hmrc_registered_name or ""
                    final_status = res.entity_match_status
                    winning_source_type = cand.source_type
                    if cand.source_type == "DERIVED_FROM_EORI":
                        stats["derived_from_eori_verified"] += 1
                    break
                elif res.entity_match_status == "PREVENTED_FALSE_POSITIVE":
                    stats["prevented_false_positives"] += 1
                    print(
                        f"    [!] Prevented False Positive on {c_name}: "
                        f"GB{cand.vat_number} belongs to '{res.hmrc_registered_name}' ({res.hmrc_postcode})"
                    )

            if final_status in ("VERIFIED_MATCH", "VAT_GROUP_MATCH"):
                if stratum == "A":
                    stats["stratum_a_verified"] += 1
                else:
                    stats["stratum_b_verified"] += 1

            print(
                f"  [{stats['total_companies']}] [Stratum {stratum}] {c_name} ({c_num}) -> {final_status} "
                f"{verified_vat} {f'[{verified_hmrc_name}]' if verified_hmrc_name else ''}"
            )

            results_rows.append({
                "company_number": c_num,
                "company_name": c_name,
                "postcode": c_pc,
                "stratum": stratum,
                "candidate_domain": domain,
                "status": final_status,
                "verified_vat": verified_vat,
                "hmrc_registered_name": verified_hmrc_name,
                "discovery_source_type": winning_source_type,
                "candidates_evaluated": str(len(candidates)),
            })

    os.makedirs(os.path.dirname(output_csv) or ".", exist_ok=True)
    with open(output_csv, mode="w", encoding="utf-8", newline="") as outfile:
        fieldnames = [
            "company_number", "company_name", "postcode", "stratum",
            "candidate_domain", "status", "verified_vat",
            "hmrc_registered_name", "discovery_source_type", "candidates_evaluated",
        ]
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results_rows)

    return stats


if __name__ == "__main__":
    summary = process_company_sample(
        input_csv="data/sample_companies.csv",
        output_csv="data/poc_results.csv",
    )
    print("\nPoC Execution Summary:", summary)