#!/usr/bin/env python3
"""
Directory scraper: hospital directory URLs -> researcher CSV
============================================================
Part one of the two-part tool. Give it the URLs of hospital / faculty
directory pages (one URL per PAGE -- a directory with two pages of
pulmonologists needs two URLs) and it uses Firecrawl to scrape each page,
extract the physicians listed on it, and write the
``name,author,affiliation`` CSV that profiler.py takes as input.

Usage (headless):

    python directory_scraper.py --urls "https://hosp.org/gi-docs;https://hosp.org/gi-docs?page=2" \
                                --out my_researchers.csv

Most people will instead launch the window:  python app.py

Needs a Firecrawl API key (https://firecrawl.dev), read from the
FIRECRAWL_API_KEY environment variable or a .env file next to this script.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from urllib.error import HTTPError

FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v1/scrape"

# What we ask Firecrawl's LLM extraction to pull off each page.
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "institution": {
            "type": "string",
            "description": "The parent hospital, health system, or university "
                           "name -- e.g. 'NYU Langone', 'Mount Sinai', "
                           "'Mayo Clinic'. NOT the name of the individual "
                           "clinic, center, or department within it.",
        },
        "clinical_focus": {
            "type": "string",
            "description": "The single medical condition or disease this "
                           "directory page is about, as a plain-English "
                           "phrase a doctor would recognize -- e.g. "
                           "'epilepsy', 'ulcerative colitis', 'heart "
                           "failure'. If the page covers a broad specialty "
                           "rather than one condition, name the specialty "
                           "(e.g. 'pulmonology').",
        },
        "institution_aliases": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Other names this same institution publishes "
                           "under in academic papers, including its "
                           "abbreviation and its university name. For NYU "
                           "Langone: ['NYU', 'New York University']. Give 2-3 "
                           "distinct short forms, not the clinic name.",
        },
        "physicians": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Full name without credentials"},
                    "credentials": {"type": "string",
                                    "description": "e.g. MD, DO, MD PhD"},
                    "specialty": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    "required": ["physicians"],
}

EXTRACT_PROMPT = (
    "This is a page from a hospital or medical-school directory of clinicians. "
    "List every individual physician/provider shown on the page with their "
    "credentials and specialty if given. Use the person's name only, without "
    "titles like Dr. or credentials like MD. Also give the parent hospital, "
    "health system, or university the directory belongs to (e.g. 'NYU "
    "Langone'), not the individual clinic or center name, and the single "
    "medical condition the page is about."
)

CREDENTIAL_TOKENS = {
    "md", "do", "phd", "mbbs", "mph", "ms", "msc", "mba", "rn", "np", "pa",
    "pa-c", "facg", "facs", "facc", "faap", "facp", "aga", "agaf", "dnp",
    "rd", "cdn", "pharmd", "bcps", "msn", "frcp", "frcs", "med",
}
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
HONORIFICS = {"dr", "prof", "professor", "mr", "ms", "mrs", "mx"}


def load_env(path=None):
    """Tiny .env reader so we don't need the python-dotenv dependency."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def get_api_key(explicit=None):
    load_env()
    key = explicit or os.environ.get("FIRECRAWL_API_KEY", "")
    if not key:
        raise SystemExit(
            "No Firecrawl API key. Set FIRECRAWL_API_KEY in the environment "
            "or in a .env file next to this script (FIRECRAWL_API_KEY=fc-...). "
            "Free keys: https://firecrawl.dev"
        )
    return key


def die_on_403(exc, url):
    """Per project policy: on HTTP 403, dump everything we know and stop."""
    print("\n" + "!" * 70)
    print("HTTP 403 FORBIDDEN -- stopping, as configured.")
    print("!" * 70)
    print(f"While scraping   : {url}")
    print(f"Full error       : {exc}")
    print(f"Server headers   : {dict(exc.headers) if exc.headers else 'none'}")
    body = ""
    try:
        body = exc.read().decode("utf-8", "replace")[:2000]
    except Exception:
        pass
    if body:
        print(f"Response body    :\n{body}")
    print("""
A 403 here can come from two different places:
  1. Firecrawl itself (bad/expired API key, plan limits) -- the response
     body above will mention your key or account if so.
  2. The TARGET SITE refusing to be scraped -- Firecrawl relays that.
     Hospital sites often sit behind aggressive bot protection
     (Cloudflare, Akamai). Options: try the page again later, use
     Firecrawl's stealth proxy option, or copy the names off the page
     by hand into the CSV -- the profiler doesn't care how the CSV
     was made.
""")
    raise SystemExit(1)


def scrape_page(url, api_key, log=print, wait_ms=8000):
    """One directory page -> (institution, [ {name, credentials, specialty} ]).

    Hospital directories almost always render their provider list with
    JavaScript after the page loads, so we tell Firecrawl to wait before
    reading. Without the wait these pages return nothing but cookie
    banners.
    """
    payload = json.dumps({
        "url": url,
        "formats": ["extract"],
        "waitFor": wait_ms,
        "onlyMainContent": True,
        "extract": {"prompt": EXTRACT_PROMPT, "schema": EXTRACT_SCHEMA},
    }).encode()
    request = urllib.request.Request(
        FIRECRAWL_ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.load(response)
            break
        except HTTPError as exc:
            if exc.code == 403:
                die_on_403(exc, url)
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            raise RuntimeError(
                f"Firecrawl HTTP {exc.code} for {url}: {body}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            # Transient network/DNS trouble -- worth a couple of retries.
            if attempt == 2:
                raise
            log(f"    network hiccup ({exc}), retrying ...")
            time.sleep(3 * (attempt + 1))

    if not data.get("success"):
        raise RuntimeError(f"Firecrawl reported failure for {url}: "
                           f"{str(data)[:500]}")

    extract = (data.get("data") or {}).get("extract") or {}
    institution = (extract.get("institution") or "").strip()
    aliases = [a.strip() for a in (extract.get("institution_aliases") or [])
               if a and a.strip()]
    # PubMed indexes one institution under several names, so search them all.
    if institution:
        seen_lower = {institution.lower()}
        for alias in aliases:
            if alias.lower() not in seen_lower:
                seen_lower.add(alias.lower())
                institution += f"|{alias}"
    physicians = extract.get("physicians") or []
    clinical_focus = (extract.get("clinical_focus") or "").strip()
    log(f"    {len(physicians)} providers found"
        + (f" ({institution.split('|')[0]}"
           f"{', ' + clinical_focus if clinical_focus else ''})"
           if institution else ""))
    return institution, physicians, clinical_focus


def to_author_form(full_name):
    """'Jordan E. Axelrad' -> 'Axelrad J', the PubMed author-search form.

    PubMed auto-expands initials ('Axelrad J' also finds 'Axelrad JE'),
    so surname + first initial is the safest automatic guess. Rows are
    meant to be eyeballed in the CSV before profiling.
    """
    name = full_name.strip()
    # "Axelrad, Jordan" and "Jordan Axelrad, MD" both contain commas;
    # drop comma-sections that are only credentials, flip the rest.
    if "," in name:
        head, *rest = [p.strip() for p in name.split(",")]
        rest = [p for p in rest
                if p and not all(t.lower().strip(".") in CREDENTIAL_TOKENS
                                 for t in p.split())]
        name = f"{rest[0]} {head}" if rest else head

    tokens = [t for t in re.split(r"\s+", name)
              if t and t.lower().strip(".") not in CREDENTIAL_TOKENS]
    while tokens and tokens[0].lower().strip(".") in HONORIFICS:
        tokens = tokens[1:]
    if not tokens:
        return ""
    if len(tokens) >= 2 and tokens[-1].lower().strip(".") in NAME_SUFFIXES:
        tokens = tokens[:-1]
    if len(tokens) == 1:
        return tokens[0]
    # Multi-word surnames: pull particles in ("Al Kazzi", "van der Woude").
    particles = {"al", "el", "de", "del", "della", "di", "da", "van", "von",
                 "der", "den", "ter", "la", "le", "bin", "ibn", "abu", "st"}
    start = len(tokens) - 1
    while start > 1 and tokens[start - 1].lower().strip(".") in particles:
        start -= 1
    surname = " ".join(tokens[start:])
    first_initial = tokens[0][0].upper()
    return f"{surname} {first_initial}"


def build_roster(urls, api_key, affiliation_override="", log=print):
    """Scrape every page URL and return de-duplicated CSV rows."""
    rows, seen = [], set()
    focus_votes = Counter()
    for url in urls:
        log(f"Scraping {url} ...")
        institution, physicians, clinical_focus = scrape_page(
            url, api_key, log=log)
        if not physicians:
            # Slow directory: give the page a lot longer before giving up.
            log("    nothing found -- retrying with a longer page wait ...")
            institution, physicians, clinical_focus = scrape_page(
                url, api_key, log=log, wait_ms=20000)
        if not physicians:
            log(f"    WARNING: no providers on this page. If the directory "
                f"needs a search click, link straight to a results page.")
        if clinical_focus:
            focus_votes[clinical_focus.lower()] += len(physicians) or 1
        affiliation = affiliation_override or institution or ""
        for person in physicians:
            name = (person.get("name") or "").strip()
            if not name:
                continue
            author = to_author_form(name)
            key = (name.lower(), affiliation.lower())
            if not author or key in seen:
                continue
            seen.add(key)
            rows.append({
                "name": name,
                "author": author,
                "affiliation": affiliation,
                "notes": (person.get("specialty") or "").strip(),
            })

    # Two different people can collapse to the same PubMed author string
    # ("Daniel Friedman" and "David E. Friedman" are both "Friedman D").
    # PubMed can't separate them either, so their profiles would be merged --
    # say so rather than silently reporting one person's blended record.
    counts = {}
    for row in rows:
        counts.setdefault(row["author"], []).append(row["name"])
    for author, names in sorted(counts.items()):
        if len(names) > 1:
            log(f"    NOTE: {' and '.join(names)} both search PubMed as "
                f"'{author}' -- their results will be mixed together. "
                f"Add a middle initial in the CSV to separate them.")

    # When pages disagree about what the directory covers, the page listing
    # the most doctors wins; a stray "sleep medicine" sub-page shouldn't
    # redefine an epilepsy center.
    detected = focus_votes.most_common(1)[0][0] if focus_votes else ""
    return rows, detected


def write_roster(rows, out_path):
    with open(out_path, "w", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["name", "author", "affiliation", "notes"])
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def split_urls(raw):
    """The UI asks for semicolon-separated URLs; be forgiving about it."""
    return [u.strip() for u in raw.split(";") if u.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Scrape hospital directory pages into a researcher CSV "
                    "for profiler.py. Remember: one URL per directory PAGE.")
    ap.add_argument("--urls", required=True,
                    help="Directory page URLs separated by semicolons")
    ap.add_argument("--out", default="researchers.csv",
                    help="Output CSV path (default: researchers.csv)")
    ap.add_argument("--affiliation",
                    default="",
                    help="Force this affiliation string for every row "
                         "(otherwise the institution detected on each page "
                         "is used)")
    ap.add_argument("--api-key", default="",
                    help="Firecrawl API key (default: FIRECRAWL_API_KEY "
                         "env var or .env file)")
    args = ap.parse_args(argv)

    api_key = get_api_key(args.api_key)
    urls = split_urls(args.urls)
    if not urls:
        raise SystemExit("No URLs given.")

    rows, detected = build_roster(urls, api_key, args.affiliation)
    if detected:
        print(f"\nDetected condition: {detected}")
    if not rows:
        raise SystemExit(
            "No providers extracted from any page. If the pages render "
            "their directory with JavaScript after a search click, link "
            "directly to a results page instead."
        )
    out = write_roster(rows, args.out)
    print(f"\nWrote {len(rows)} researchers to {out}")
    print("Open it and sanity-check the author/affiliation columns, then:")
    print(f"  python profiler.py --config <your_config.json> "
          f"--researchers {out}")


if __name__ == "__main__":
    main()
