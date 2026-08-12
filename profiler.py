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

The tool is disease-agnostic and runs in either of two modes:

  --auto "<condition>"   Nothing to write. The topic vocabulary is derived
                         from the papers themselves (see auto_topics.py),
                         and topics are ranked by what distinguishes each
                         researcher from the rest of the group.

  --config <file.json>   A hand-written vocabulary, for when you want
                         specific drug groupings and your own topic labels.
                         See examples/.

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
    python profiler.py --researchers my_doctors.csv --auto "heart failure"

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
import urllib.parse
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

import auto_topics

# Below this many papers a topic profile is not evidence, it is decoration:
# two papers on one subject share enough headings to fill a twelve-topic
# list that reads like a career. Report the count instead.
MIN_PROFILE_PAPERS = 5


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


def build_query(author, affiliation, start_year):
    """The exact PubMed query used, so a person can check it themselves."""
    variants = [a.strip() for a in affiliation.split("|") if a.strip()]
    if len(variants) > 1:
        clause = "(" + " OR ".join(f"{v}[Affiliation]" for v in variants) + ")"
    else:
        clause = f"{variants[0]}[Affiliation]" if variants else ""
    return (f'{author}[Author] AND {clause} '
            f'AND ("{start_year}"[PDAT] : "3000"[PDAT])')


def pubmed_url(query):
    return "https://pubmed.ncbi.nlm.nih.gov/?term=" + urllib.parse.quote(query)


def find_paper_ids(author, affiliation, start_year, max_papers, pause):
    """Search PubMed for one author's PMIDs.

    An affiliation may list alternatives separated by "|". Institutions are
    indexed inconsistently -- the same NYU author appears under "NYU
    Langone", "NYU", and "New York University" -- so any of them counts.
    """
    query = build_query(author, affiliation, start_year)
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
            substances = [
                str(c["NameOfSubstance"])
                for c in citation.get("ChemicalList", [])
            ]
            text = _text_of(citation)
            articles.append({
                "pmid": str(citation["PMID"]),
                "mesh": mesh,
                "substances": substances,
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


def run_auto(condition, researchers, out_dir, log=print, on_progress=None,
             min_focus_papers=10, topics_per_researcher=12,
             min_papers_per_topic=2, max_stars=5, start_year=2018,
             max_papers=500):
    """Profile without a config file: work the vocabulary out from the papers.

    Two passes are unavoidable. Which drug names matter can only be judged
    against the whole group's corpus -- a term appearing in one abstract is
    noise, the same term in nine is a topic -- so every paper is fetched
    first, then the vocabulary is learned, then people are profiled.
    """
    pause = 0.11 if Entrez.api_key else 0.35

    focus = auto_topics.build_focus(condition, pause=pause)
    log(f"Profiling for: {focus['label']}")
    if focus.get("is_discipline"):
        log("   (a specialty rather than one condition, so all of each "
            "doctor's research counts)")
    elif len(focus["match_terms"]) > 1:
        log(f"   (also counting {len(focus['match_terms']) - 1} synonyms "
            f"PubMed indexes this under)")
    log("")

    # ---- pass 1: fetch everyone's papers --------------------------------
    fetched = {}
    queries = {}
    excluded = []
    for index, person in enumerate(researchers):
        if on_progress:
            on_progress(index, len(researchers))
        name = person["name"]
        surname = person["author"].split()[0]
        initials = person["author"].split()[1] if " " in person["author"] else ""
        log(f"Fetching {name} ...")
        try:
            pmids = find_paper_ids(person["author"], person["affiliation"],
                                   start_year, max_papers, pause)
            if not pmids:
                log("    no papers found -- check name/affiliation spelling")
                excluded.append((name, 0, "no papers found"))
                continue
            articles = fetch_articles(pmids, surname, initials,
                                      {}, [], pause)
            for article in articles:
                article["is_focus"] = auto_topics.is_focus_paper(article, focus)
            hits = [a for a in articles if a["is_focus"]]
            n_first = sum(1 for a in hits if a["first_author"])
            log(f"    {len(articles)} papers, {len(hits)} on "
                f"{focus['label']} ({n_first} first-author)")
            fetched[name] = articles
            queries[name] = build_query(person["author"],
                                        person["affiliation"], start_year)
        except SystemExit:
            raise
        except Exception as exc:
            log(f"    ERROR: {exc}")
            excluded.append((name, 0, f"error: {exc}"))

    if on_progress:
        on_progress(len(researchers), len(researchers))

    # ---- learn the vocabulary from the whole corpus ---------------------
    corpus = [a for arts in fetched.values() for a in arts if a["is_focus"]]
    if not corpus:
        log("\nNo papers matched the condition. Nothing to profile.")
        return None, [], []
    vocabulary = auto_topics.discover_vocabulary(corpus, focus)
    background = auto_topics.find_undiscriminating(corpus, vocabulary, focus)
    log(f"\nLearned {len(vocabulary)} drug/substance terms from "
        f"{len(corpus)} papers"
        + (f"; set aside {len(background)} that nearly everyone publishes on."
           if background else "."))

    # ---- pass 2: profile ------------------------------------------------
    # A 10-paper bar is right for a roster of academic leaders and wrong for
    # a community practice, so drop it rather than hand back an empty
    # report. Everything is already fetched; only the threshold changes.
    #
    # It stops at MIN_PROFILE_PAPERS. Below that there is nothing to rank:
    # two papers on one subject produce a twelve-topic profile that looks
    # exactly like a career's worth of work. Those people are reported with
    # their paper count instead, which is the honest answer.
    for bar in (min_focus_papers, MIN_PROFILE_PAPERS):
        if bar > min_focus_papers:
            continue
        profiles, skipped = {}, []
        for name, articles in fetched.items():
            hits = [a for a in articles if a["is_focus"]]
            if len(hits) < bar:
                skipped.append((name, len(hits),
                                f"under {bar} {focus['label']} papers"))
                continue
            total, first = Counter(), Counter()
            for article in hits:
                for topic in auto_topics.topics_for(article, vocabulary,
                                                    focus, background):
                    total[topic] += 1
                    if article["first_author"]:
                        first[topic] += 1
            profiles[name] = {
                "total": total, "first": first,
                "focus_papers": len(hits),
                "first_author_papers":
                    sum(1 for a in hits if a["first_author"]),
                "verify": pubmed_url(queries.get(name, "")),
            }
        if len(profiles) >= 3 or bar == MIN_PROFILE_PAPERS:
            if bar != min_focus_papers:
                log(f"Few researchers cleared {min_focus_papers} papers -- "
                    f"showing everyone with {bar}+ instead.")
            min_focus_papers = bar
            excluded.extend(skipped)
            break

    doc_freq = Counter()
    for article in corpus:
        for topic in auto_topics.topics_for(article, vocabulary, focus,
                                            background):
            doc_freq[topic] += 1

    rows = _report(profiles, excluded, focus["label"], min_focus_papers,
                   start_year, topics_per_researcher, min_papers_per_topic,
                   max_stars, log, doc_freq=doc_freq,
                   corpus_size=len(corpus))
    out_csv = _write_csv(rows, focus["label"], out_dir)
    log(f"\nSaved {out_csv}")

    thin = [{"name": name, "papers": n, "why": why}
            for name, n, why in sorted(excluded, key=lambda x: -x[1])]
    return out_csv, rows, thin


def _rank_topics(counts, doc_freq, corpus_size, min_papers_per_topic, limit):
    """Order a researcher's topics by what makes them different.

    Ranking by raw volume buries the finding you actually want: every
    epilepsy doctor publishes on "Brain", so it outranks the cannabidiol
    trials that are one person's life's work. Weighting each topic by how
    rare it is across the whole group (the standard TF-IDF trick) keeps
    volume mattering while letting a distinctive niche rise.
    """
    import math

    scored = []
    for topic, n in counts.items():
        if n < min_papers_per_topic:
            continue
        rarity = math.log(corpus_size / max(doc_freq.get(topic, 1), 1)) + 1
        scored.append((n * rarity, n, topic))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return [(topic, n) for _, n, topic in scored[:limit]]


def _report(profiles, excluded, label, min_focus_papers, start_year,
            topics_per_researcher, min_papers_per_topic, max_stars, log,
            doc_freq=None, corpus_size=0):
    log("\n" + "=" * 70)
    log(f"RESEARCHERS WITH {min_focus_papers}+ {label.upper()} PAPERS "
        f"SINCE {start_year}")
    log("Topics are listed most-distinctive first, not simply most-published.")
    log("* = one first-author paper on that topic. Past five, the count "
        "replaces")
    log("    the asterisks (*x12 means twelve -- a number means more, "
        "not less).")
    log("=" * 70)

    rows = []
    for name, data in sorted(profiles.items(),
                             key=lambda kv: -kv[1]["focus_papers"]):
        log(f"\n{name}   [{data['focus_papers']} {label} papers, "
            f"{data['first_author_papers']} as first author]")
        if doc_freq:
            ranked = _rank_topics(data["total"], doc_freq, corpus_size,
                                  min_papers_per_topic, topics_per_researcher)
        else:
            ranked = [(t, n) for t, n in data["total"].most_common()
                      if n >= min_papers_per_topic][:topics_per_researcher]
        if not ranked:
            # Papers exist but nothing recurs across them. Say so here --
            # previously this person was simply absent from the report,
            # which reads as "not searched" rather than "nothing found".
            log("   -- no subject recurs across their papers --")
            excluded.append((name, data["focus_papers"],
                             "no subject recurs across their papers"))
            continue
        for topic, n in ranked:
            n_first = data["first"][topic]
            log(f"   {topic}{stars(n_first, max_stars)}")
            rows.append({
                "researcher": name, "topic": topic, "papers": n,
                "first_author_papers": n_first,
                "share_of_their_focus_papers":
                    round(n / data["focus_papers"], 3),
                "their_focus_papers": data["focus_papers"],
                "verify_on_pubmed": data.get("verify", ""),
            })

    if excluded:
        log("\n" + "-" * 70)
        log("NOT ENOUGH PUBLISHED WORK TO PROFILE:")
        for name, n_hits, why in sorted(excluded, key=lambda x: -x[1]):
            log(f"   {name}  ({n_hits} {label} papers -- {why})")
    return rows


def _write_csv(rows, label, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    out_csv = os.path.join(out_dir, f"{slugify(label)}_topics_by_researcher.csv")
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    return out_csv


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
    ap.add_argument("--config",
                    help="JSON config: focus condition, tracked terms, "
                         "thresholds. See examples/. Omit to use --auto.")
    ap.add_argument("--auto", metavar="CONDITION",
                    help="Profile without a config: name the condition in "
                         "plain English (e.g. --auto \"heart failure\") and "
                         "the topic vocabulary is derived from the papers.")
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

    if not args.config and args.auto is None:
        raise SystemExit(
            "Give either --auto \"<condition>\" (vocabulary derived from the "
            "papers) or --config <file.json> (hand-written vocabulary)."
        )

    researchers = load_researchers(args.researchers)
    if args.config:
        run(load_config(args.config), researchers, args.out_dir)
    else:
        run_auto(args.auto, researchers, args.out_dir)


if __name__ == "__main__":
    main()
