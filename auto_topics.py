#!/usr/bin/env python3
"""
Automatic condition + topic discovery
=====================================
Lets the profiler run with no hand-written config: the condition to profile
for is inferred from the directory pages, and the topic vocabulary is
derived from the papers themselves.

Three sources of topics, because no single one is enough:

1. **MeSH headings.** PubMed's own indexing. Excellent for procedures,
   populations, and established concepts; useless for anything recent.
2. **Chemical/substance names.** PubMed tags most drugs discussed in a
   paper, which catches medications MeSH has no heading for.
3. **Drug-name morphology.** New drugs are absent from both of the above
   for years, but they are still *named* in the abstract, and drug names
   are highly regular: -mab is a monoclonal antibody, -nib a kinase
   inhibitor, -parib a PARP inhibitor. Matching those endings finds
   etrasimod and cenobamate the year they are published, in any specialty.

Plus a small set of specialty-independent clinical phrases ("refractory",
"real-world"), which are about how medicine is practiced rather than about
any one disease, so they are safe to hard-code.
"""

import re
import time
from collections import Counter

from Bio import Entrez

# ---------------------------------------------------------------------------
# Specialty-independent clinical concepts. These describe research posture,
# not a disease, so the same list works for hepatology and neurosurgery.
# ---------------------------------------------------------------------------
UNIVERSAL_PHRASES = {
    "Refractory / failed therapy": [
        "refractory", "treatment-refractory", "nonresponse", "non-response",
        "loss of response", "treatment failure", "intractable",
        "drug-resistant", "drug resistant"],
    "Treatment sequencing / switching": [
        "treatment sequencing", "positioning", "switching", "second-line",
        "third-line", "first-line", "cycling"],
    "Combination therapy": [
        "combination therapy", "dual therapy", "combination treatment"],
    "Dose optimization / monitoring": [
        "dose escalation", "dose optimization", "therapeutic drug monitoring",
        "trough level", "drug levels", "dose-response"],
    "Biomarkers / prediction": [
        "biomarker", "biomarkers", "predictive model", "precision medicine",
        "personalized medicine", "risk score", "prediction model"],
    "Machine learning / AI": [
        "machine learning", "deep learning", "artificial intelligence",
        "neural network", "algorithm"],
    "Surgery / procedural": [
        "surgery", "surgical", "resection", "operative", "postoperative"],
    "Imaging": [
        "mri", "magnetic resonance", "ultrasound", "computed tomography",
        "pet imaging", "radiography"],
    "Genetics / genomics": [
        "genetic", "genomic", "genome-wide", "mutation", "variant",
        "sequencing", "gene therapy"],
    "Microbiome": [
        "microbiome", "microbiota", "dysbiosis", "fecal microbiota"],
    "Diet / nutrition": ["diet", "dietary", "nutrition", "nutritional"],
    "Pregnancy / reproductive": [
        "pregnancy", "pregnant", "conception", "reproductive", "fertility",
        "teratogenic", "lactation"],
    "Pediatric": [
        "pediatric", "paediatric", "children", "childhood", "adolescent",
        "infant"],
    "Older adults / frailty": [
        "older adults", "elderly", "geriatric", "frailty", "sarcopenia"],
    "Quality of life / symptoms": [
        "quality of life", "fatigue", "patient-reported", "symptom burden",
        "depression", "anxiety"],
    "Disparities / access": [
        "disparities", "disparity", "health equity", "socioeconomic",
        "racial", "access to care", "underserved", "insurance"],
    "Cost / health economics": [
        "cost-effectiveness", "cost effectiveness", "healthcare costs",
        "economic burden", "value-based"],
    "Guidelines / consensus": [
        "guideline", "guidelines", "consensus", "recommendations",
        "position statement", "appropriate use"],
    "Real-world / registry data": [
        "real-world", "real world", "registry", "claims data",
        "population-based", "administrative data"],
    "Clinical trial (RCT)": [
        "randomized", "randomised", "placebo-controlled", "phase 3",
        "phase iii", "phase 2", "phase ii", "double-blind"],
    "Meta-analysis / review": [
        "meta-analysis", "systematic review", "pooled analysis"],
    "Telehealth / digital": [
        "telehealth", "telemedicine", "digital health", "mobile health",
        "wearable", "remote monitoring"],
    "COVID-19": ["covid-19", "covid", "sars-cov-2", "pandemic"],
}

# Drug-name endings with high precision. Deliberately excludes ambiguous
# ones like -tide (peptide) and -stat (thermostat).
DRUG_SUFFIXES = (
    "mab", "nib", "ciclib", "parib", "imod", "afil", "prazole", "gliptin",
    "flozin", "sartan", "statin", "cycline", "mycin", "oxacin", "vastatin",
    "tinib", "zomib", "lisib", "degib", "rafenib", "sertib", "vir",
)
_DRUG_RE = re.compile(
    r"\b([a-z]{4,}(?:" + "|".join(DRUG_SUFFIXES) + r"))\b", re.I)

# Words that happen to end like drugs.
_NOT_DRUGS = {
    "combination", "elaborate", "deliberate", "severe", "reservoir",
    "behavior", "behaviour", "endeavor", "survivor", "prior", "superior",
    "inferior", "posterior", "anterior", "interior", "exterior", "junior",
    "senior", "warrior", "savior", "vapor", "favor", "flavor", "humor",
    "tumor", "rumor", "donor", "minor", "major", "honor", "labor", "color",
}

# MeSH headings too generic to be worth reporting in any specialty.
BORING_MESH = {
    "Humans", "Male", "Female", "Adult", "Aged", "Middle Aged", "Adolescent",
    "Child", "Young Adult", "Aged, 80 and over", "Infant", "Child, Preschool",
    "Infant, Newborn", "Retrospective Studies", "Prospective Studies",
    "Cohort Studies", "Follow-Up Studies", "Longitudinal Studies",
    "Cross-Sectional Studies", "Treatment Outcome", "Severity of Illness Index",
    "Risk Factors", "Time Factors", "Incidence", "Prevalence",
    "United States", "Animals", "Mice", "Rats", "Reproducibility of Results",
    "Chronic Disease", "Remission Induction", "Double-Blind Method",
    "Surveys and Questionnaires", "Quality of Life", "Case-Control Studies",
    "Predictive Value of Tests", "Sensitivity and Specificity",
    "Randomized Controlled Trials as Topic", "Treatment Failure",
    "Disease Progression", "Recurrence", "Prognosis", "Risk Assessment",
    "Age Factors", "Sex Factors", "Databases, Factual", "Registries",
    "Practice Guidelines as Topic", "Health Services Accessibility",
}

# Where a study was run is not what it is about. MeSH tags geography
# liberally, so "Sweden" outranks real topics for anyone using Nordic
# registry data.
GEOGRAPHIC_MESH = {
    "United States", "Europe", "Canada", "Japan", "China", "Sweden",
    "Denmark", "Norway", "Finland", "Netherlands", "Germany", "France",
    "Italy", "Spain", "United Kingdom", "England", "Scotland", "Ireland",
    "Australia", "New Zealand", "India", "Israel", "Korea", "Taiwan",
    "Brazil", "Mexico", "Switzerland", "Belgium", "Austria", "Poland",
    "Portugal", "Greece", "Turkey", "Russia", "South Africa", "Singapore",
    "Hong Kong", "Asia", "Africa", "North America", "South America",
    "Scandinavian and Nordic Countries", "European Union",
    "Developing Countries", "Latin America", "Middle East",
}
BORING_MESH |= GEOGRAPHIC_MESH

STOPWORDS = {
    "the", "and", "for", "with", "disease", "diseases", "disorder",
    "disorders", "syndrome", "center", "centre", "clinic", "program",
    "medicine", "medical", "care", "health", "institute", "department",
    "division", "service", "services", "specialty", "specialties",
}


STEM_LEN = 6


def _stem(word):
    """Fold a word to a comparison key.

    Suffix-stripping is too brittle here: 'epilepsy' and 'epileptic' strip
    to different stems, and 'seizure'/'seizures' did too, which let the
    condition's own vocabulary leak back in as topics. A fixed-length
    prefix is cruder but folds all of those together, which is the only
    property this needs.
    """
    return word.lower()[:STEM_LEN]


def lookup_mesh(condition, pause=0.35):
    """Plain-English condition -> (display label, synonyms) from MeSH.

    Returns (None, []) when MeSH has nothing, in which case the caller
    falls back to matching the raw phrase.
    """
    try:
        # MeSH ranks narrow children above the general heading -- searching
        # "epilepsy" returns 20 subtypes before plain "Epilepsy" -- so cast
        # a wide net and pick deliberately below.
        handle = Entrez.esearch(db="mesh", term=condition, retmax=40)
        ids = Entrez.read(handle)["IdList"]
        handle.close()
        time.sleep(pause)
        if not ids:
            return None, []
        handle = Entrez.esummary(db="mesh", id=",".join(ids))
        records = Entrez.read(handle)
        handle.close()
        time.sleep(pause)
    except Exception:
        return None, []

    target = condition.strip().lower()
    best, best_score = None, -1
    for record in records:
        terms = [str(t) for t in record.get("DS_MeshTerms", [])]
        if not terms:
            continue
        lowered = [t.lower() for t in terms]
        # Prefer the descriptor that actually *is* the thing asked for, not
        # a narrow child of it ("Epilepsy", not "Epilepsy, Reflex"). The
        # heading itself matching outranks a synonym matching, which
        # outranks the phrase merely appearing somewhere; extra words mean
        # extra specificity, so they break ties downward.
        primary = lowered[0]
        inverted = ", ".join(reversed(target.split(" ", 1)))
        if primary == target or primary == inverted:
            score = 1000
        elif target in lowered:
            score = 500 - len(terms[0].split())
        elif any(target in t for t in lowered):
            score = 100 - len(terms[0].split())
        else:
            continue
        if score > best_score:
            best, best_score = terms, score

    if not best:
        return None, []
    return best[0], best


def build_focus(condition, pause=0.35):
    """Everything needed to decide whether a paper is 'on topic'.

    Returns {label, match_terms, stems}. A paper counts when any synonym
    appears in its title/abstract, or when any of its MeSH headings shares
    a stem with the condition -- the stem test is what pulls in the whole
    family ('Drug Resistant Epilepsy', 'Epilepsies, Partial') without
    enumerating it.
    """
    condition = (condition or "").strip()
    label, synonyms = lookup_mesh(condition, pause=pause) if condition \
        else (None, [])

    if not label:
        label = condition.title() if condition else "All research"
        synonyms = [condition] if condition else []

    match_terms = sorted({s for s in synonyms if s}, key=len, reverse=True)

    # Stems identify the condition's own vocabulary, so they must stay
    # tight: a stem picked up from a single quirky synonym ("Eating
    # Epilepsy" -> "eat") would swallow unrelated papers. Words from the
    # heading itself always count; words from synonyms only count when
    # several synonyms share them.
    core = [w for w in re.findall(r"[a-z]{4,}", (label + " " + condition).lower())
            if w not in STOPWORDS]
    synonym_words = Counter()
    for term in synonyms:
        for word in set(re.findall(r"[a-z]{4,}", term.lower())):
            if word not in STOPWORDS:
                synonym_words[_stem(word)] += 1
    stems = {_stem(w) for w in core}
    stems.update(s for s, n in synonym_words.items() if n >= 2)
    return {
        "label": label,
        "match_terms": match_terms,
        "stems": stems,
        "patterns": [re.compile(r"\b" + re.escape(t) + r"\b", re.I)
                     for t in match_terms],
    }


def is_focus_paper(article, focus):
    """Does this paper belong to the condition we're profiling?"""
    if not focus["match_terms"] and not focus["stems"]:
        return True                       # no condition detected: count all
    for pattern in focus["patterns"]:
        if pattern.search(article["text"]):
            return True
    for heading in article["mesh"]:
        words = re.findall(r"[a-z]{4,}", heading.lower())
        if any(_stem(w) in focus["stems"] for w in words):
            return True
    return False


def _drug_names(text):
    found = set()
    for match in _DRUG_RE.finditer(text):
        token = match.group(1).lower()
        if token not in _NOT_DRUGS and len(token) >= 6:
            found.add(token)
    return found


def discover_vocabulary(all_articles, focus, min_papers=3, max_terms=400):
    """Learn this corpus's topic vocabulary before profiling anyone.

    Drug names and substances are only trustworthy in aggregate -- a single
    paper mentioning a word ending in -nib is more likely a typo than a
    kinase inhibitor. Terms must show up in several papers, across the
    whole group, to be admitted.
    """
    counts = Counter()
    for article in all_articles:
        seen = set()
        seen.update(_drug_names(article["text"]))
        seen.update(s.lower() for s in article.get("substances", []))
        for term in seen:
            counts[term] += 1

    vocabulary = {}
    for term, count in counts.most_common():
        if count < min_papers or len(vocabulary) >= max_terms:
            continue
        # Skip anything that is really just the condition restated.
        if any(_stem(w) in focus["stems"]
               for w in re.findall(r"[a-z]{4,}", term.lower())):
            continue
        vocabulary[term.title()] = re.compile(
            r"\b" + re.escape(term) + r"\b", re.I)
    return vocabulary


_UNIVERSAL_PATTERNS = {
    label: [re.compile(r"\b" + re.escape(v) + r"\b", re.I) for v in variants]
    for label, variants in UNIVERSAL_PHRASES.items()
}


def find_undiscriminating(corpus, vocabulary, focus, threshold=0.45,
                          min_corpus=30):
    """Topics so common in this corpus that they say nothing about anyone.

    Every epilepsy paper is about seizures and half of them mention EEG, so
    those crowd out the findings that actually separate one doctor from
    another. Which terms are uninformative depends on the field, and a
    hand-written list per specialty is exactly what this tool exists to
    avoid -- so measure it instead: a topic on nearly every paper in the
    group's corpus is background, not signal.
    """
    if len(corpus) < min_corpus:
        return set()                       # too little data to judge
    counts = Counter()
    for article in corpus:
        for topic in topics_for(article, vocabulary, focus, exclude=None):
            counts[topic] += 1
    ceiling = threshold * len(corpus)
    return {topic for topic, n in counts.items() if n >= ceiling}


def topics_for(article, vocabulary, focus, exclude=frozenset()):
    """Every topic one paper touches, from all three sources."""
    found = set()

    for label, patterns in _UNIVERSAL_PATTERNS.items():
        if any(p.search(article["text"]) for p in patterns):
            found.add(label)

    for label, pattern in vocabulary.items():
        if pattern.search(article["text"]):
            found.add(label)

    for heading in article["mesh"]:
        if heading in BORING_MESH:
            continue
        words = re.findall(r"[a-z]{4,}", heading.lower())
        if words and all(_stem(w) in focus["stems"] for w in words):
            continue                      # the condition itself, not a topic
        found.add(heading)

    return found - (exclude or frozenset())
