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
import urllib.parse
import urllib.request
from collections import Counter
from urllib.error import HTTPError

FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v1/scrape"

# Each URL costs a Firecrawl credit and a minute of PubMed time, so the
# input is capped rather than trusted.
MAX_URLS = 10
MAX_DOCTORS = 150


# Sites that are certainly not clinician directories. This is not a
# security boundary -- it is a courtesy, so an obvious mistake fails
# instantly instead of spending a scrape to discover YouTube has no
# doctors on it. Anything not listed here is still checked by reading the
# page.
NON_DIRECTORY_HOSTS = {
    "youtube.com", "youtu.be", "google.com", "facebook.com", "fb.com",
    "instagram.com", "twitter.com", "x.com", "tiktok.com", "reddit.com",
    "amazon.com", "netflix.com", "wikipedia.org", "linkedin.com",
    "pinterest.com", "spotify.com", "twitch.tv", "ebay.com", "yahoo.com",
    "bing.com", "chatgpt.com", "openai.com", "claude.ai", "gmail.com",
}

# Addresses that never host a public directory; blocked so a pasted
# internal URL cannot turn this into a probe of a private network.
PRIVATE_HOST_PATTERN = re.compile(
    r"^(localhost|127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|"
    r"\[?::1\]?|0\.0\.0\.0)", re.I)

# A real hostname: dot-separated labels ending in an alphabetic TLD. Prose
# is otherwise happy to pose as one -- "at N.Y.U. please" has dots in it.
VALID_HOST_PATTERN = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)

# What we ask Firecrawl's LLM extraction to pull off each page.
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "organization_type": {
            "type": "string",
            "description": "What kind of organization this website belongs "
                           "to, in two or three words -- for example "
                           "'hospital', 'medical school', 'law firm', "
                           "'university department', 'software company'.",
        },
        "people_profession": {
            "type": "string",
            "description": "The profession of the people listed on this "
                           "page, as a single plain word or phrase: "
                           "'physicians', 'lawyers', 'engineers', "
                           "'professors', 'staff'. Report what they "
                           "actually are, not what you expect them to be.",
        },
        "institution": {
            "type": "string",
            "description": "The parent hospital, health system, or university "
                           "name -- e.g. 'NYU Langone', 'Mount Sinai', "
                           "'Mayo Clinic'. NOT the name of the individual "
                           "clinic, center, or department within it.",
        },
        "is_clinician_directory": {
            "type": "boolean",
            "description": "True only if the PURPOSE of this page is to "
                           "list clinicians to choose from, as a "
                           "'find a doctor' directory or a department's "
                           "team page does. False for a hospital home page, "
                           "a news article, a patient-information page, or "
                           "a single doctor's own profile page -- even when "
                           "such a page happens to name one or two doctors "
                           "in passing.",
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
        "people": {
            "type": "array",
            "description": "Everyone listed on the page, whatever their "
                           "profession.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Full name without credentials"},
                    "credentials": {
                        "type": "string",
                        "description": "Letters after the name exactly as "
                                       "printed -- MD, DO, PhD, JD, Esq, "
                                       "PE. Empty if none are shown.",
                    },
                    "specialty": {
                        "type": "string",
                        "description": "Their stated specialty, practice "
                                       "area, or job title as printed.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    "required": ["people"],
}

EXTRACT_PROMPT = (
    "Describe this web page factually. Do NOT assume it is medical: it may "
    "be a law firm, a university department, a company, or anything else. "
    "Report what kind of organization the site belongs to and what "
    "profession the listed people actually practise. List every individual "
    "person named on the page with their credentials and specialty or job "
    "title exactly as printed. Give the person's name only, without titles "
    "like Dr. or credentials like MD. Also give the parent organization "
    "(e.g. 'NYU Langone'), not the individual clinic or department, and, "
    "only if the page is about one specific medical condition, name that "
    "condition."
)

# Letters that only a clinician carries. PhD and MPH are deliberately
# absent: plenty of non-clinicians hold them.
CLINICAL_CREDENTIALS = {
    "md", "do", "mbbs", "mbchb", "mbbch", "dds", "dmd", "dpm", "dvm",
    "pharmd", "np", "dnp", "pa", "pa-c", "rn", "aprn", "crna", "cnm",
    "psyd", "od", "dc", "rd", "msn", "fnp", "anp", "pmhnp", "lcsw",
}

# Words that place a directory outside medicine. Checked against the
# organization type, the stated profession, and people's job titles.
NON_CLINICAL_MARKERS = {
    "law", "lawyer", "lawyers", "attorney", "attorneys", "legal",
    "solicitor", "barrister", "paralegal", "counsel", "litigation",
    "engineer", "engineering", "architect", "architecture", "accountant",
    "accounting", "auditor", "banker", "banking", "finance", "financial",
    "insurance", "realtor", "estate", "software", "technology", "sales",
    "marketing", "recruiting", "consultant", "consulting", "journalist",
    "politician", "clergy", "veterinary",
}

# Words that mark a directory as medical even when nobody lists letters.
CLINICAL_MARKERS = {
    "hospital", "health", "healthcare", "medical", "medicine", "clinic",
    "clinical", "physician", "physicians", "doctor", "doctors", "surgeon",
    "surgeons", "surgery", "nurse", "nursing", "provider", "providers",
    "practitioner", "psychiatry", "psychiatrist", "pediatric",
    "pediatrics", "oncology", "cardiology", "neurology", "radiology",
    "dermatology", "gastroenterology", "orthopedic", "orthopedics",
    "anesthesiology", "pathology", "urology", "obstetrics", "gynecology",
    "ophthalmology", "endocrinology", "rheumatology", "nephrology",
    "pulmonology", "geriatrics", "epilepsy", "cancer", "cardiac",
}

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


class FriendlyError(Exception):
    """An error with something a non-technical person can act on.

    Carries the technical detail separately so the interface can show the
    plain-English message and keep the diagnostics out of the way.
    """

    def __init__(self, message, detail=""):
        super().__init__(message)
        self.message = message
        self.detail = detail


def die_on_403(exc, url):
    """On HTTP 403, gather everything we know and stop."""
    body = ""
    try:
        body = exc.read().decode("utf-8", "replace")[:2000]
    except Exception:
        pass
    headers = dict(exc.headers) if exc.headers else {}

    detail = (f"HTTP 403 Forbidden while reading {url}\n"
              f"Error: {exc}\nHeaders: {headers}\n"
              f"Body: {body}\n\n"
              "A 403 comes from one of two places:\n"
              "  1. Firecrawl itself -- expired key or plan limits; the "
              "body above will say so.\n"
              "  2. The hospital's website refusing automated reading. "
              "Hospital sites often sit behind bot protection "
              "(Cloudflare, Akamai).")
    print("\n" + "!" * 70)
    print("HTTP 403 FORBIDDEN -- stopping, as configured.")
    print("!" * 70)
    print(detail)

    raise FriendlyError(
        "That hospital's website would not let this tool read the page. "
        "Some hospital sites block automated reading. Try a different "
        "directory page, or try again later.",
        detail)


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
    physicians = extract.get("people") or []
    clinical_focus = (extract.get("clinical_focus") or "").strip()
    organization = (extract.get("organization_type") or "").strip()
    profession = (extract.get("people_profession") or "").strip()

    # Check the profession first: "this page lists lawyers" is a more
    # useful thing to tell someone than "this page is not a directory".
    verdict, what = looks_clinical(organization, profession, physicians)
    if verdict:
        log(f"    the people listed are not clinicians"
            + (f" ({what})" if what else ""))
        return institution, [], clinical_focus, verdict, what

    # A page can sit on a hospital domain, name a doctor or two, and still
    # not be a directory -- a hospital home page usually features one.
    # Believe the page's own purpose over the presence of a stray name.
    if extract.get("is_clinician_directory") is False:
        log("    this page is not a directory of clinicians")
        return institution, [], clinical_focus, "not_a_directory", ""

    log(f"    {len(physicians)} providers found"
        + (f" ({institution.split('|')[0]}"
           f"{', ' + clinical_focus if clinical_focus else ''})"
           if institution else ""))
    return institution, physicians, clinical_focus, "", ""


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
    rejected = []
    for url in urls:
        log(f"Scraping {url} ...")
        institution, physicians, clinical_focus, verdict, what = scrape_page(
            url, api_key, log=log)

        if not verdict and not physicians:
            # Slow directory: give the page a lot longer before giving up.
            log("    nothing found -- retrying with a longer page wait ...")
            (institution, physicians, clinical_focus,
             verdict, what) = scrape_page(url, api_key, log=log,
                                          wait_ms=20000)

        if verdict:
            rejected.append((url, verdict, what))
            continue
        if not physicians:
            log(f"    WARNING: no providers on this page. If the directory "
                f"needs a search click, link straight to a results page.")
            rejected.append((url, "no_doctors", ""))
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
    if len(rows) > MAX_DOCTORS:
        log(f"    NOTE: {len(rows)} people found; searching the first "
            f"{MAX_DOCTORS}. PubMed rate-limits heavy use, and a larger "
            f"batch risks being blocked partway through.")
        rows = rows[:MAX_DOCTORS]

    detected = focus_votes.most_common(1)[0][0] if focus_votes else ""
    return rows, detected, rejected


def write_roster(rows, out_path):
    with open(out_path, "w", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["name", "author", "affiliation", "notes"])
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def split_urls(raw):
    """The UI asks for semicolon-separated URLs; be forgiving about it.

    Any whitespace separates too, not just semicolons. That covers one
    link per line, and it covers the autocorrect on phones and Macs that
    turns a double space into ". " -- which would otherwise weld two
    links into one unparseable string.
    """
    parts = re.split(r"[;,\s]+", raw)
    cleaned = []
    for part in parts:
        # Strip what pasting drags along: sentence punctuation left by
        # autocorrect, and the <angle brackets> mail clients wrap links in.
        part = part.strip().strip("<>\"'“”‘’")
        part = part.rstrip(".,;:!?")
        if part:
            cleaned.append(part)
    return cleaned


# Subdomains that host a directory at their root, so a bare address there
# is legitimate: doctors.example.org, findadoc.example.org.
DIRECTORY_SUBDOMAINS = ("doctor", "doctors", "physician", "physicians",
                        "provider", "providers", "find", "findadoc",
                        "finddoctor", "faculty", "team", "ourdoctors")


def _words(text):
    return set(re.findall(r"[a-z][a-z-]+", (text or "").lower()))


def looks_clinical(organization, profession, people):
    """Are these actually clinicians? Returns (verdict, description).

    The extractor is agreeable -- ask it for physicians and it will label
    whoever it finds as physicians, lawyers included. So the answer is
    decided here, from evidence the wording cannot bend: the letters after
    people's names, and the words the page uses about itself.
    """
    described = _words(organization) | _words(profession)
    titles = set()
    for person in people:
        titles |= _words(person.get("specialty", ""))

    credentials = set()
    for person in people:
        for token in re.split(r"[,\s./]+", (person.get("credentials") or "")):
            token = token.strip().lower()
            if token:
                credentials.add(token)

    clinicians = sum(
        1 for person in people
        if any(t.strip().lower() in CLINICAL_CREDENTIALS
               for t in re.split(r"[,\s./]+", person.get("credentials") or ""))
    )

    non_clinical = (described | titles) & NON_CLINICAL_MARKERS
    clinical = (described | titles) & CLINICAL_MARKERS

    # Letters after the name are the strongest evidence either way.
    if clinicians and clinicians >= max(1, len(people) // 4):
        return "", ""
    if non_clinical and not clinical and not clinicians:
        return "not_medical", (profession or organization or
                               ", ".join(sorted(non_clinical)))
    if clinical:
        return "", ""
    if credentials and not clinicians:
        # Everyone has letters and none of them are clinical ones.
        return "not_medical", (profession or organization or
                               "people who are not clinicians")
    if people and not clinical and not clinicians:
        return "not_medical", (profession or organization or
                               "people who are not clinicians")
    return "", ""


def is_site_root(url):
    """Is this the front door of a site rather than a page within it?

    Judged before any scraping, because asking the page is unreliable: a
    hospital home page reads as a doctor directory to a language model,
    since it features doctors. The URL is the honest signal -- a directory
    practically always lives at a path, not at the bare domain.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.path.strip("/") or parsed.query:
        return False
    host = parsed.netloc.split("@")[-1].split(":")[0].lower()
    label = host.split(".")[0]
    if label == "www":
        label = host.split(".")[1] if host.count(".") > 1 else ""
    # A site whose whole purpose is listing doctors may legitimately do it
    # at its root.
    return label not in DIRECTORY_SUBDOMAINS


def validate_urls(raw):
    """Check pasted input before spending a single request on it.

    Returns (good_urls, problems). Problems are phrased for the person who
    pasted them, not for a log file.
    """
    # Guard the pathological paste before parsing it. Truncating instead
    # would split a URL down the middle and report that fragment as
    # malformed, which tells the user nothing about the real problem.
    if len(raw) > 20000:
        return [], [f"That is far more text than this tool accepts. Please "
                    f"paste up to {MAX_URLS} links, separated by "
                    f"semicolons."]

    entries = split_urls(raw)
    if not entries:
        return [], ["Please paste at least one directory web address."]

    # Whitespace separates entries, so a typed sentence arrives as a pile
    # of words. Recognise that as one mistake instead of complaining about
    # every word in it.
    if not any("://" in e or "." in e for e in entries):
        return [], ["That looks like a sentence rather than a web address. "
                    "Open the hospital's \"find a doctor\" page in your "
                    "browser and copy the address from the address bar — it "
                    "should start with https://"]

    good, problems, seen = [], [], set()

    for entry in entries:
        candidate = entry if "://" in entry else "https://" + entry
        try:
            parsed = urllib.parse.urlparse(candidate)
        except ValueError:
            parsed = None

        host = (parsed.netloc.split("@")[-1].split(":")[0].lower()
                if parsed else "")

        if not parsed or parsed.scheme not in ("http", "https") or not host:
            shown = entry if len(entry) <= 60 else entry[:57] + "..."
            problems.append(
                f'"{shown}" is not a web address. Paste the link to a '
                f"hospital's doctor directory, copied from your browser's "
                f"address bar (it should start with https://).")
            continue

        if PRIVATE_HOST_PATTERN.match(host):
            problems.append(f'"{entry}" is not a public web address.')
            continue

        if not VALID_HOST_PATTERN.match(host):
            shown = entry if len(entry) <= 60 else entry[:57] + "..."
            problems.append(
                f'"{shown}" is not a web address. Paste the link to a '
                f"hospital's doctor directory, copied from your browser's "
                f"address bar (it should start with https://).")
            continue

        bare = host[4:] if host.startswith("www.") else host
        root = ".".join(bare.split(".")[-2:])
        if bare in NON_DIRECTORY_HOSTS or root in NON_DIRECTORY_HOSTS:
            problems.append(
                f"{bare} is not a hospital directory. Paste a link to a "
                f"hospital or medical school's \"find a doctor\" page.")
            continue

        normalized = candidate.rstrip("/")

        if is_site_root(normalized):
            problems.append(
                f"{bare} is the hospital's home page, not a list of "
                f"doctors. Open their \"Find a Doctor\" or \"Our Team\" "
                f"page and paste the address of that page instead.")
            continue

        if normalized.lower() in seen:
            continue                       # same page twice: quietly ignore
        seen.add(normalized.lower())
        good.append(normalized)

    if len(good) > MAX_URLS:
        return [], [f"You entered {len(good)} links; this tool searches up "
                    f"to {MAX_URLS} pages at a time. Please run the rest "
                    f"separately."]

    # Nothing usable and a scattering of complaints means the input was
    # prose, not a list of links -- say that once.
    if not good and len(problems) > 2:
        return [], ["That doesn't look like a list of web addresses. Open "
                    "the hospital's \"find a doctor\" page in your browser, "
                    "copy the address from the address bar, and paste it "
                    "here. Separate several with semicolons."]

    # Deduplicate and cap: five variations of the same complaint is noise.
    unique, shown = [], set()
    for problem in problems:
        if problem not in shown:
            shown.add(problem)
            unique.append(problem)
    if len(unique) > 3:
        unique = unique[:3] + [f"...and {len(unique) - 3} more entries that "
                               f"are not web addresses."]

    return good, unique


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

    rows, detected, rejected = build_roster(urls, api_key, args.affiliation)
    if detected:
        print(f"\nDetected condition: {detected}")
    for url, verdict, what in rejected:
        print(f"Skipped {url}: {verdict.replace('_', ' ')}"
              + (f" ({what})" if what else ""))
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
