"""
Streams a raw Companies House BasicCompanyData CSV file, filters for active,
established B2B manufacturing/construction/wholesale suppliers, discovers their
true corporate website (blocking SEO company directories), and writes
data/sample_companies.csv.
"""

import csv
import os
import random
import re
import time
from typing import Dict, List
from urllib.parse import urlparse
import requests

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS


TARGET_SIC_PREFIXES = (
    "10", "11", "13", "16", "17", "20", "22", "23", "24", "25",
    "26", "27", "28", "29", "30", "31", "32", "33",
    "41", "42", "43", "46",
)

ESTABLISHED_ACCOUNT_CATEGORIES = (
    "FULL",
    "MEDIUM",
    "GROUP",
    "SMALL",
    "TOTAL EXEMPTION FULL",
    "AUDIT EXEMPTION SUBSIDIARY",
)

# Expanded blocklist including all UK Companies House clone/scraper directories
DIRECTORY_BLOCKLIST = (
    "gov.uk", "endole.co.uk", "companycheck.co.uk", "duedil.com",
    "pomanda.com", "checkaco.com", "ukdata.com", "corpium.co.uk",
    "companiesintheuk.co.uk", "opengovuk.com", "doogal.co.uk",
    "gbrbusiness.com", "chamberofcommerce.uk", "ukcompanyhub.co.uk",
    "firstreport.co.uk", "companypulse.co.uk", "ukcompanydir.com",
    "tradesmenonline.co.uk", "bing.com", "google.com", "yahoo.com",
    "duckduckgo.com", "linkedin.com", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "youtube.com", "yell.com", "trustpilot.com",
    "dnb.com", "bloomberg.com", "wikipedia.org", "kompass.com", "192.com",
    "bizdb.co.uk", "companieslist.co.uk", "datalog.co.uk", "cylex-uk.co.uk",
    "scoot.co.uk", "thomsonlocal.com", "checkatrade.com", "bark.com",
    "find-and-update.company-information.service.gov.uk", "companydirectorcheck.com",
    "ukfirm.co.uk", "bizstats.co.uk", "companysearchesmadesimple.com",
    "globaldatabase.com", "zoominfo.com", "crunchbase.com", "pitchbook.com",
    "construction.co.uk", "bidstats.uk", "open-contracting.org",
)

STOP_SLUG_WORDS = {
    "limited", "ltd", "plc", "llp", "uk", "the", "and", "company", "co",
    "holdings", "group", "services", "international", "united", "kingdom",
}


def clean_header_dict(row: Dict[str, str]) -> Dict[str, str]:
    return {k.strip(): (v.strip() if v else "") for k, v in row.items() if k}


def is_blocked_domain(host: str) -> bool:
    host = host.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return True
    if any(b in host for b in DIRECTORY_BLOCKLIST):
        return True
    # Catch generic SEO scraper domains containing 'company', 'companies', 'directory', 'postcode'
    if any( kw in host for kw in ("companies", "companydir", "companyhub", "ukbusiness", "postcode", "dir.co.uk") ):
        return True
    return False


def is_procurement_realistic_supplier(clean_row: Dict[str, str]) -> bool:
    if clean_row.get("CompanyStatus", "").upper() != "ACTIVE":
        return False

    acct_cat = clean_row.get("Accounts.AccountCategory", "").upper()
    if acct_cat not in ESTABLISHED_ACCOUNT_CATEGORIES:
        return False

    postcode = clean_row.get("RegAddress.PostCode", "")
    if len(postcode) < 5:
        return False

    sic_1 = clean_row.get("SICCode.SicText_1", "")
    if not sic_1 or not sic_1.startswith(TARGET_SIC_PREFIXES):
        return False

    return True


def probe_direct_uk_domain(company_name: str) -> str:
    """
    Tests whether www.<company-slug>.co.uk or .com resolves directly.
    """
    # Remove parenthetical region tags like '(WORCESTER)' or '(ALCESTER)'
    cleaned = re.sub(r"\([^)]*\)", " ", company_name.lower())
    words = re.sub(r"[^a-z0-9\s]", " ", cleaned).split()
    core_words = [w for w in words if w not in STOP_SLUG_WORDS]
    if not core_words:
        return ""

    slugs = []
    slugs.append("".join(core_words))
    if len(core_words) > 1:
        slugs.append("-".join(core_words))
    if len(core_words) >= 3:
        slugs.append("".join(core_words[:2]))

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    for slug in slugs:
        if len(slug) < 4:
            continue
        for tld in (".co.uk", ".com"):
            url = f"https://www.{slug}{tld}"
            try:
                resp = requests.get(url, headers=headers, timeout=4, allow_redirects=True)
                if resp.status_code == 200 and "text/html" in resp.headers.get("Content-Type", ""):
                    final_host = urlparse(resp.url).netloc.lower()
                    if not is_blocked_domain(final_host):
                        return f"https://{final_host}"
            except requests.RequestException:
                continue

    return ""


def discover_company_domain(company_name: str, company_number: str, postcode: str) -> str:
    """
    Finds the official corporate website by searching Clean Name + Postcode Outcode
    first (to avoid CRN clone directories), then falling back to direct slug probing.
    """
    no_parens = re.sub(r"\([^)]*\)", " ", company_name)
    clean_name = re.sub(
        r"\b(LIMITED|LTD|PLC|LLP|UNITED KINGDOM|UK)\b\.?", "", no_parens, flags=re.IGNORECASE
    ).strip()
    outcode = postcode.split()[0] if " " in postcode else postcode[:4]

    # Query 1: Name + Postcode Outcode (ranks the company's real homepage #1)
    # Query 2: Name + Official site
    queries = [
        f"{clean_name} {outcode}",
        f"{clean_name} official website UK",
    ]
    for q in queries:
        try:
            time.sleep(0.8)
            with DDGS() as ddgs:
                results = list(ddgs.text(q, region="uk-en", max_results=6))
            for r in results:
                href = r.get("href", "") or r.get("url", "")
                parsed = urlparse(href)
                host = parsed.netloc.lower()
                if host.startswith("www."):
                    host = host[4:]
                if is_blocked_domain(host):
                    continue
                return f"https://{host}"
        except Exception:
            pass

    return probe_direct_uk_domain(company_name)


def build_sample_csv(
    raw_ch_csv: str,
    output_sample_csv: str,
    sample_size: int = 50,
    random_seed: int = 42,
) -> None:
    print(f"[1/3] Streaming {raw_ch_csv} and filtering for established B2B suppliers...")
    eligible_pool: List[Dict[str, str]] = []

    with open(raw_ch_csv, mode="r", encoding="utf-8", errors="replace") as infile:
        reader = csv.DictReader(infile)
        for idx, raw_row in enumerate(reader):
            row = clean_header_dict(raw_row)
            if is_procurement_realistic_supplier(row):
                acct_cat = row.get("Accounts.AccountCategory", "").upper()
                is_large_filer = acct_cat in ("FULL", "MEDIUM", "GROUP", "AUDIT EXEMPTION SUBSIDIARY")
                stratum = "A" if is_large_filer else "B"

                eligible_pool.append({
                    "company_number": row.get("CompanyNumber", "").zfill(8),
                    "company_name": row.get("CompanyName", ""),
                    "postcode": row.get("RegAddress.PostCode", ""),
                    "sic_code": row.get("SICCode.SicText_1", ""),
                    "stratum": stratum,
                })

            if idx > 0 and idx % 100000 == 0:
                print(f"  ...scanned {idx:,} rows (found {len(eligible_pool):,} established B2B suppliers)")

    print(f"[2/3] Total established B2B suppliers found: {len(eligible_pool):,}")
    random.seed(random_seed)

    stratum_a_pool = [c for c in eligible_pool if c["stratum"] == "A"]
    stratum_b_pool = [c for c in eligible_pool if c["stratum"] == "B"]

    target_a = min(len(stratum_a_pool), sample_size // 2)
    target_b = min(len(stratum_b_pool), sample_size - target_a)

    sampled = random.sample(stratum_a_pool, target_a) + random.sample(stratum_b_pool, target_b)
    print(f"[3/3] Discovering website domains for {len(sampled)} sampled companies (Stratum A={target_a}, Stratum B={target_b})...")

    final_rows = []
    domains_found = 0
    for i, comp in enumerate(sampled, 1):
        domain = discover_company_domain(
            company_name=comp["company_name"],
            company_number=comp["company_number"],
            postcode=comp["postcode"],
        )
        if domain:
            domains_found += 1
        comp["candidate_domain"] = domain
        final_rows.append(comp)
        print(f"  [{i}/{len(sampled)}] [Stratum {comp['stratum']}] {comp['company_name']} ({comp['company_number']}) -> {domain or 'NO_DOMAIN_FOUND'}")

    os.makedirs(os.path.dirname(output_sample_csv) or ".", exist_ok=True)
    with open(output_sample_csv, mode="w", encoding="utf-8", newline="") as outfile:
        fieldnames = [
            "company_number", "company_name", "postcode",
            "sic_code", "stratum", "candidate_domain",
        ]
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(final_rows)

    print(f"Done! Found genuine corporate domains for {domains_found}/{len(final_rows)} companies. Saved to {output_sample_csv}")


if __name__ == "__main__":
    build_sample_csv(
        raw_ch_csv="data/raw_companies_house.csv",
        output_sample_csv="data/sample_companies.csv",
        sample_size=50,
    )