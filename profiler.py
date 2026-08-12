#!/usr/bin/env python3
"""
PubMed topic profiler
=====================
For every clinician/researcher with at least N papers on a focus condition,
lists the specific topics they publish on, ranked by volume, with asterisks
marking papers where they were FIRST AUTHOR.

    Axelrad:
       Mesalamine
       Refractory / treatment-refractory disease*
       Etrasimod (Velsipity)**

One asterisk per first-author paper on that topic. First authorship usually
means the work was theirs to drive rather than a name on a consortium paper.

The tool is disease-agnostic: the focus condition, the vocabulary of tracked
topics, and the list of people to profile all live in two editable files
(a JSON config and a CSV of researchers). See examples/ for a complete,
working ulcerative colitis setup you can copy for any other specialty.

WHY THIS READS ABSTRACTS, NOT JUST MeSH
---------------------------------------
MeSH (PubMed's controlled vocabulary) lags reality: new drugs wait years for
a heading, and clinical phrases like "refractory disease" are not MeSH terms
at all. A MeSH-only search would report that nobody studies the newest
therapies, which is false. So this script matches the config's tracked_terms
against titles and abstracts, and folds MeSH headings in on top for the
concepts MeSH covers well.

USAGE
-----
    python profiler.py --config examples/ulcerative_colitis.json \
                       --researchers examples/researchers_uc.csv

NCBI requires an email address on every request. Set it once:

    export NCBI_EMAIL="you@example.com"
    export NCBI_API_KEY="..."        # optional, triples the request rate

or pass --email on the command line. In Google Colab, see README.md.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from urllib.error import HTTPError

try:
    from Bio import Entrez
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "biopython"])
    from Bio import Entrez

try:
    import pandas as pd
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pandas"])
    import pandas as pd


# MeSH headings too generic to be worth reporting, regardless of specialty.
# Add disease-specific ones (the focus condition itself, its umbrella terms)
# via "extra_boring_mesh" in the config.
BORING_MESH = {
    "Humans", "Male", "Female", "Adult", "Aged", "Middle Aged", "Adolescent",
    "Child", "Young Adult", "Aged, 80 and over", "Infant", "Child, Preschool",
    "Retrospective Studies", "Prospective Studies", "Cohort Studies",
    "Follow-Up Studies", "Longitudinal Studies", "Cross-Sectional Studies",
    "Treatment Outcome", "Severity of Illness Index", "Risk Factors",
    "Time Factors", "Incidence", "Prevalence", "United States", "Animals",
    "Mice", "Reproducibility of Results", "Chronic Disease",
    "Remission Induction", "Double-Blind Method", "Surveys and Questionnaires",
    "Quality of Life",
}


def die_on_403(exc, url_context):
    """Per project policy: on HTTP 403, dump everything we know and stop."""
    print("\n" + "!" * 70)
    print("HTTP 403 FORBIDDEN -- stopping, as configured.")
    print("!" * 70)
    print(f"While contacting : {url_context}")
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
Likely causes for a 403 from NCBI E-utilities:
  1. Missing/placeholder email     -> NCBI blocks anonymous heavy users.
  2. Rate limit exceeded            -> without an API key the cap is
                                       3 requests/second; NCBI escalates
                                       repeat offenders from 429 to 403.
  3. IP block (shared cloud IP)     -> common on Colab/AWS; an API key
                                       usually resolves it because limits
                                       become per-key, not per-IP.
  4. Malformed or oversized query.
Get a free API key: https://www.ncbi.nlm.nih.gov/account/settings/
""")
    raise SystemExit(1)


def load_config(path):
    with open(path) as fh:
        cfg = json.load(fh)
    required = ["focus", "tracked_terms"]
    for key in required:
        if key not in cfg:
            raise SystemExit(f"Config {path} is missing required key: {key!r}")
    cfg.setdefault("start_year", 2018)
    cfg.setdefault("max_papers", 500)
    cfg.setdefault("min_focus_papers", 10)
    cfg.setdefault("topics_per_researcher", 12)
    cfg.setdefault("min_papers_per_topic", 2)
    cfg.setdefault("max_stars", 5)
    cfg.setdefault("extra_boring_mesh", [])
    focus = cfg["focus"]
    if not focus.get("mesh_terms") and not focus.get("text_terms"):
        raise SystemExit(
            "focus needs at least one of mesh_terms / text_terms "
            "so papers can be attributed to the condition."
        )
    return cfg


def load_researchers(path):
    """CSV columns: name, author, affiliation. Extra columns are ignored."""
    people = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        missing = {"name", "author", "affiliation"} - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(
                f"{path} is missing column(s): {', '.join(sorted(missing))}. "
                "Expected header: name,author,affiliation"
            )
        for row in reader:
            if row["name"].strip():
                people.append({k: row[k].strip() for k in
                               ("name", "author", "affiliation")})
    if not people:
        raise SystemExit(f"No researchers found in {path}.")
    return people


def compile_patterns(tracked_terms):
    """Precompile whole-word, case-insensitive patterns once."""
    return {
        label: [re.compile(r"\b" + re.escape(v) + r"\b", re.I)
                for v in variants]
        for label, variants in tracked_terms.items()
    }


def find_paper_ids(author, affiliation, start_year, max_papers, pause):
    """Search PubMed for one author's PMIDs."""
    query = (
        f'{author}[Author] AND {affiliation}[Affiliation] '
        f'AND ("{start_year}"[PDAT] : "3000"[PDAT])'
    )
    try:
        handle = Entrez.esearch(db="pubmed", term=query, retmax=max_papers)
        result = Entrez.read(handle)
        handle.close()
    except HTTPError as exc:
        if exc.code == 403:
            die_on_403(exc, f"esearch for query: {query}")
        raise
    time.sleep(pause)
    return result["IdList"]


def _text_of(citation):
    """Title + abstract as one string."""
    article = citation.get("Article", {})
    parts = [str(article.get("ArticleTitle", ""))]
    abstract = article.get("Abstract", {}).get("AbstractText", [])
    parts.extend(str(chunk) for chunk in abstract)
    return " ".join(parts)


def _is_first_author(citation, surname, initials):
    """Was this researcher the first listed author?

    PubMed orders AuthorList as printed, so position 0 is first author.
    Consortium papers lead with a CollectiveName and are never a match.
    """
    authors = citation.get("Article", {}).get("AuthorList", [])
    if not authors:
        return False
    first = authors[0]
    if "LastName" not in first:              # collective/group authorship
        return False
    if str(first["LastName"]).lower() != surname.lower():
        return False
    if not initials:
        return True
    their_initials = str(first.get("Initials", ""))
    # "Axelrad J" should match a record indexed as "Axelrad JE"
    return their_initials.upper().startswith(initials.upper())


def _is_focus(mesh, text, focus, focus_patterns):
    if any(term in mesh for term in focus.get("mesh_terms", [])):
        return True
    return any(p.search(text) for p in focus_patterns)


def fetch_articles(pmids, surname, initials, focus, focus_patterns, pause):
    """One record per paper, with its topics and first-author flag."""
    articles = []

    for i in range(0, len(pmids), 200):
        chunk = ",".join(pmids[i:i + 200])
        try:
            handle = Entrez.efetch(db="pubmed", id=chunk, retmode="xml")
            records = Entrez.read(handle)
            handle.close()
        except HTTPError as exc:
            if exc.code == 403:
                die_on_403(exc, f"efetch for {len(pmids[i:i+200])} PMIDs")
            raise
        time.sleep(pause)

        for entry in records.get("PubmedArticle", []):
            citation = entry["MedlineCitation"]
            mesh = [
                str(h["DescriptorName"])
                for h in citation.get("MeshHeadingList", [])
            ]
            text = _text_of(citation)
            articles.append({
                "pmid": str(citation["PMID"]),
                "mesh": mesh,
                "is_focus": _is_focus(mesh, text, focus, focus_patterns),
                "text": text,
                "first_author": _is_first_author(citation, surname, initials),
            })

    return articles


def topics_for(article, patterns, boring):
    """Every topic this paper touches, from both sources.

    A set, so a paper mentioning a drug six times still counts once.
    """
    found = {
        label for label, pats in patterns.items()
        if any(p.search(article["text"]) for p in pats)
    }
    found.update(t for t in article["mesh"] if t not in boring)
    return found


def profile(articles, patterns, boring):
    """Topic -> (papers, first-author papers), over one person's focus papers."""
    total = Counter()
    first = Counter()
    for article in articles:
        if not article["is_focus"]:
            continue
        for topic in topics_for(article, patterns, boring):
            total[topic] += 1
            if article["first_author"]:
                first[topic] += 1
    return total, first


def stars(n, max_stars):
    """One asterisk per first-author paper, collapsed once it gets silly."""
    if n == 0:
        return ""
    if n <= max_stars:
        return "*" * n
    return f"*x{n}"


def slugify(label):
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def run(cfg, researchers, out_dir):
    focus = cfg["focus"]
    patterns = compile_patterns(cfg["tracked_terms"])
    focus_patterns = [re.compile(r"\b" + re.escape(t) + r"\b", re.I)
                      for t in focus.get("text_terms", [])]
    # The focus condition itself and anything the config flags as noise
    # should never appear as a "topic".
    boring = BORING_MESH | set(cfg["extra_boring_mesh"]) \
             | set(focus.get("mesh_terms", []))
    pause = 0.11 if Entrez.api_key else 0.35

    profiles = {}
    excluded = []

    for person in researchers:
        name = person["name"]
        surname = person["author"].split()[0]
        initials = person["author"].split()[1] if " " in person["author"] else ""
        print(f"Fetching {name} ...", end=" ", flush=True)

        try:
            pmids = find_paper_ids(
                person["author"], person["affiliation"],
                cfg["start_year"], cfg["max_papers"], pause,
            )
            if not pmids:
                print("NO PAPERS -- check name/affiliation spelling")
                excluded.append((name, 0, "no papers found"))
                continue

            articles = fetch_articles(
                pmids, surname, initials, focus, focus_patterns, pause,
            )
            hits = [a for a in articles if a["is_focus"]]
            n_first = sum(1 for a in hits if a["first_author"])
            print(f"{len(articles)} papers, {len(hits)} on "
                  f"{focus['label']} ({n_first} first-author)")

            if len(hits) < cfg["min_focus_papers"]:
                excluded.append(
                    (name, len(hits),
                     f"under {cfg['min_focus_papers']} {focus['label']} papers")
                )
                continue

            total, first = profile(articles, patterns, boring)
            profiles[name] = {
                "total": total,
                "first": first,
                "focus_papers": len(hits),
                "first_author_papers": n_first,
            }

        except SystemExit:
            raise
        except Exception as exc:
            print(f"ERROR: {exc}")
            excluded.append((name, 0, f"error: {exc}"))

    # ---- the report ------------------------------------------------------
    print("\n" + "=" * 70)
    print(f"RESEARCHERS WITH {cfg['min_focus_papers']}+ "
          f"{focus['label'].upper()} PAPERS SINCE {cfg['start_year']}")
    print("* = one first-author paper on that topic")
    print("=" * 70)

    rows = []
    ordered = sorted(profiles.items(), key=lambda kv: -kv[1]["focus_papers"])

    for name, data in ordered:
        print(f"\n{name}:  [{data['focus_papers']} {focus['label']} papers, "
              f"{data['first_author_papers']} as first author]")

        ranked = [
            (topic, n) for topic, n in data["total"].most_common()
            if n >= cfg["min_papers_per_topic"]
        ][:cfg["topics_per_researcher"]]

        if not ranked:
            print("   -- no topic clears the threshold --")
            continue

        for topic, n in ranked:
            n_first = data["first"][topic]
            print(f"   {topic}{stars(n_first, cfg['max_stars'])}")
            rows.append({
                "researcher": name,
                "topic": topic,
                "papers": n,
                "first_author_papers": n_first,
                "share_of_their_focus_papers":
                    round(n / data["focus_papers"], 3),
                "their_focus_papers": data["focus_papers"],
            })

    if excluded:
        print("\n" + "-" * 70)
        print("EXCLUDED:")
        for name, n_hits, why in sorted(excluded, key=lambda x: -x[1]):
            print(f"   {name}  ({n_hits} {focus['label']} papers -- {why})")

    os.makedirs(out_dir, exist_ok=True)
    out_csv = os.path.join(
        out_dir, f"{slugify(focus['label'])}_topics_by_researcher.csv"
    )
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print(f"\nSaved {out_csv} (same data plus exact paper counts).")
    return out_csv


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Profile what each researcher publishes on, for any "
                    "condition, from PubMed titles/abstracts/MeSH.")
    ap.add_argument("--config", required=True,
                    help="JSON config: focus condition, tracked terms, "
                         "thresholds. See examples/.")
    ap.add_argument("--researchers", required=True,
                    help="CSV with columns: name,author,affiliation")
    ap.add_argument("--email", default=os.environ.get("NCBI_EMAIL", ""),
                    help="Contact email NCBI requires "
                         "(or set NCBI_EMAIL env var).")
    ap.add_argument("--api-key", default=os.environ.get("NCBI_API_KEY", ""),
                    help="NCBI API key, optional but 3x faster "
                         "(or set NCBI_API_KEY env var).")
    ap.add_argument("--out-dir", default="output",
                    help="Where to write the CSV (default: output/)")
    args = ap.parse_args(argv)

    if not args.email or "@" not in args.email:
        raise SystemExit(
            "NCBI requires a contact email. Pass --email you@example.com "
            "or set the NCBI_EMAIL environment variable."
        )
    Entrez.email = args.email
    if args.api_key:
        Entrez.api_key = args.api_key

    cfg = load_config(args.config)
    researchers = load_researchers(args.researchers)
    run(cfg, researchers, args.out_dir)


if __name__ == "__main__":
    main()
