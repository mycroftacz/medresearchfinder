# PubMed Topic Profiler

Find out what any group of physician-researchers *actually* publishes on.

Give it a condition (any disease with a [MeSH heading](https://meshb.nlm.nih.gov))
and a list of clinicians, and for everyone with enough papers on that
condition it produces a ranked profile of their specific research topics —
drugs, procedures, clinical situations — with asterisks marking the papers
they first-authored:

```
Jordan Axelrad:  [48 Ulcerative colitis papers, 11 as first author]
   Clinical trial (RCT)
   Refractory / failed therapy**
   Vedolizumab (Entyvio)*
   Microbiome / FMT***
   ...
```

One asterisk per first-author paper on that topic. First authorship usually
means the work was theirs to drive rather than a name on a consortium paper.

A patient choosing between specialists, a fellow choosing a mentor, or a
department mapping local expertise can use the same tool — only the two
input files change.

## Why it reads abstracts, not just MeSH

MeSH (PubMed's controlled vocabulary) lags reality. New drugs wait years for
a heading, and everyday clinical phrases like "refractory disease" or
"treatment sequencing" are not MeSH terms at all. A MeSH-only search would
report that nobody studies the newest therapies, which is false. This tool
matches your `tracked_terms` against titles and abstracts, then folds MeSH
headings in on top for the concepts MeSH covers well.

## Quick start (window)

```bash
pip install -r requirements.txt
python app.py
```

A window opens with three steps:

1. **Directory pages** — paste the URL of every hospital-directory page you
   want to search, separated by semicolons (`;`). *Provide a separate link
   for every page of a directory:* if a directory lists its pulmonologists
   across two pages, paste two URLs, one per page.
2. **Disease config** — pick a JSON config (see `examples/`, or write your
   own — [Adapting it to your specialty](#adapting-it-to-your-specialty)).
3. **Email** — NCBI requires a contact email on every PubMed request.

Click Run. The app scrapes each page with [Firecrawl](https://firecrawl.dev),
extracts the physicians into a roster CSV, profiles every one of them
against PubMed, and writes the report + CSV to `output/`.

Scraping needs a Firecrawl API key (free tier is fine). Put it in a `.env`
file next to `app.py`:

```
FIRECRAWL_API_KEY=fc-your-key-here
NCBI_API_KEY=optional-but-3x-faster
```

`.env` is gitignored — never commit keys.

You can also run the scraper headless, which just writes the roster CSV:

```bash
python directory_scraper.py --urls "https://hosp.org/gi-docs;https://hosp.org/gi-docs?page=2" --out my_researchers.csv
```

Physician names are converted to PubMed author form automatically
(`Jordan E. Axelrad, MD` → `Axelrad J`). Skim the CSV before profiling —
unusual compound surnames occasionally need a manual touch-up, and you can
delete anyone you don't want profiled (the scrape includes surgeons,
psychologists, etc. if the directory lists them).

## Quick start (command line, no scraping)

```bash
pip install -r requirements.txt
export NCBI_EMAIL="you@example.com"      # required by NCBI on every request
export NCBI_API_KEY="..."                # optional; free, and 3x faster

python profiler.py \
    --config examples/ulcerative_colitis.json \
    --researchers examples/researchers_uc.csv
```

The report prints to the terminal and a CSV with exact counts lands in
`output/`. A run over ~40 researchers takes about 10 minutes without an API
key, ~4 with one.

Get a free API key at <https://www.ncbi.nlm.nih.gov/account/settings/>
(NCBI account → Settings → API Key Management).

### Google Colab

```python
!git clone https://github.com/YOURNAME/pubmed-profiler
%cd pubmed-profiler
!pip -q install -r requirements.txt
!python profiler.py --config examples/ulcerative_colitis.json \
                    --researchers examples/researchers_uc.csv \
                    --email you@example.com
```

Download the CSV from `output/` via the folder icon in the left sidebar.

## Adapting it to your specialty

Everything disease-specific lives in two files. Copy the examples and edit.

### 1. The config (JSON)

| Key | What it does |
|---|---|
| `focus.label` | How the condition prints in the report |
| `focus.mesh_terms` | Exact MeSH heading(s) for the condition — look them up at [meshb.nlm.nih.gov](https://meshb.nlm.nih.gov) |
| `focus.text_terms` | Fallback phrases matched in titles/abstracts, for papers too new to have MeSH indexing yet |
| `tracked_terms` | `"Display label": ["spelling", "variants", ...]` — the topics the report can see. Case-insensitive, whole-word. **This is the main lever.** Include brand and generic drug names, procedures, and clinical situations you care about. |
| `extra_boring_mesh` | MeSH headings to suppress as topics (umbrella terms, sibling diseases) |
| `min_focus_papers` | Researchers below this many on-condition papers are dropped |
| `start_year`, `max_papers`, `topics_per_researcher`, `min_papers_per_topic`, `max_stars` | Tuning knobs; the example's defaults are sensible |

A minimal cardiology config would look like:

```json
{
  "focus": {
    "label": "Heart failure",
    "mesh_terms": ["Heart Failure"],
    "text_terms": ["heart failure", "HFrEF", "HFpEF"]
  },
  "tracked_terms": {
    "SGLT2 inhibitors": ["empagliflozin", "dapagliflozin", "sglt2"],
    "Sacubitril/valsartan": ["sacubitril", "entresto"],
    "Cardiac transplant": ["heart transplant", "cardiac transplantation"]
  }
}
```

### 2. The researcher list (CSV)

```csv
name,author,affiliation,notes
Jordan Axelrad,Axelrad J,NYU,
```

- `author` is the PubMed author-search form: `Surname Initials`.
- `affiliation` is matched against PubMed's affiliation field; a distinctive
  fragment ("Sinai", "Mayo") beats the full institution name. Beware of
  fragments that match multiple institutions ("Sinai" also matches
  Cedars-Sinai).
- `notes` (and any other extra column) is ignored by the program — use it
  for your own bookkeeping.
- If someone returns 0 papers, the affiliation string is the usual culprit:
  physicians move, and some hospitals index under multiple names.

## Reading the results

- Topics are ranked by paper volume within that person's on-condition papers.
- `*` = one first-author paper on that topic (`*x12` once it gets silly).
- The CSV adds `share_of_their_focus_papers`, useful for spotting someone
  whose niche *is* your topic versus someone who touched it twice.

**Caveats:** author disambiguation is name+affiliation only, so common
surnames can pick up strays. Topic matching is keyword-based — a paper
*mentioning* a drug in the abstract counts toward it. Treat the output as a
map for further reading, not a verdict.

## License

MIT — see [LICENSE](LICENSE).
