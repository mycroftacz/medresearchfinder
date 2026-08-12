# Med Research Finder

Find out what any group of doctors *actually* publishes on.

Paste the URLs of hospital "find a doctor" pages, press Run, and get each
physician's research topics back — ranked by how much they publish on them,
with asterisks marking the papers they first-authored:

```
Orrin Devinsky          254 papers
   SUDEP / mortality *******
   Genetics / precision therapy ********
   Cannabidiol *********
   Drug-resistant / refractory ***
   ...

Claude Steriade          34 papers
   Encephalitis ****
   Autoantibodies ***
   Status epilepticus *
   ...
```

### Reading the output

Topics are listed **most distinctive first**, not simply most published. A
subject nearly everyone in the group writes about says little about any one
of them, so subjects that set a doctor apart rise to the top.

**Asterisks mark first-authored papers** — one per paper on that topic where
the doctor was first author:

| Notation | Meaning |
|---|---|
| `***` | Three first-authored papers on that topic |
| `*x12` | Twelve of them. Past five, the count replaces the asterisks so the list stays readable — **a number means more, not less** |
| *(none)* | Contributed without being first author, common on large multi-center studies |

First authorship usually means the work was that person's to drive rather
than one name among many. A doctor with 30 papers and many first-authored
ones is often running their own program; one with 300 and few may be a
senior collaborator on other people's studies. Both are accomplished — they
are different things. It is why Steriade's 34-paper profile tells you more
about her than Devinsky's 254-paper one does about him.

There is nothing to configure. The condition being profiled and the topic
vocabulary are both worked out automatically, so the same tool covers
hepatology, neurosurgery, or anything else with a PubMed footprint.

## Quick start

```bash
pip install -r requirements.txt
python app.py
```

A window opens in your browser:

1. **Paste the directory URLs**, separated by semicolons (`;`). *You may
   need a separate link for every page of a directory* — if there are two
   pages of pulmonologists, provide a URL for each page.
2. **Press Run.**

That's the whole workflow. The app reads each page, works out which
condition the directory covers, looks up every doctor on PubMed, and lists
their topics. A results CSV with exact counts also lands in `output/`.

Scraping needs a [Firecrawl](https://firecrawl.dev) API key (the free tier
is fine). Put it in a `.env` file next to `app.py`:

```
FIRECRAWL_API_KEY=fc-your-key-here
NCBI_API_KEY=optional-but-makes-PubMed-3x-faster
```

`.env` is gitignored — never commit keys. The app asks once for the email
address PubMed requires on every request, then remembers it.

Expect roughly 30 seconds per doctor without an NCBI API key, three times
faster with one (free, from
[NCBI account settings](https://www.ncbi.nlm.nih.gov/account/settings/)).

## How it decides what counts as a topic

The hard part is that no single source of topics is sufficient.

**MeSH**, PubMed's controlled vocabulary, is excellent for procedures,
populations, and established concepts — and useless for anything recent. New
drugs wait years for a heading, and clinical phrases like "refractory
disease" are not MeSH terms at all. A MeSH-only tool would report that
nobody studies the newest therapies, which is false.

So topics come from three places at once:

1. **MeSH headings** on each paper, minus the ones too generic to be worth
   printing (`Humans`, `Retrospective Studies`).
2. **Chemical/substance tags**, which catch drugs that have no MeSH heading.
3. **Drug-name morphology.** Drug names are strikingly regular — `-mab` is a
   monoclonal antibody, `-nib` a kinase inhibitor, `-parib` a PARP
   inhibitor. Matching those endings finds etrasimod and cenobamate the year
   they are published, in any specialty, with no list to maintain.

Drug names are only trusted in aggregate: a term appearing in one abstract
is noise, the same term across nine papers is a topic. That is why the tool
reads every doctor's papers before profiling anyone — the vocabulary is
learned from the group's whole corpus.

On top of that sits a small set of specialty-independent clinical concepts
(*refractory*, *real-world data*, *disparities*, *treat-to-target*). Those
describe how medicine is practiced rather than any one disease, so they
apply equally to cardiology and oncology.

The condition itself is detected from the directory page, then resolved
against PubMed's MeSH database to pick up its synonyms — "epilepsy" also
finds papers indexed only as "seizure disorder". When several pages
disagree, the page listing the most doctors wins.

## Running it without the window

The scraper and the profiler are separate programs; either runs alone.

```bash
# directory URLs -> roster CSV
python directory_scraper.py \
    --urls "https://hosp.org/gi-docs;https://hosp.org/gi-docs?page=2" \
    --out my_researchers.csv

# roster CSV -> profiles (automatic vocabulary)
python profiler.py --researchers my_researchers.csv --auto "ulcerative colitis"
```

This is the path to take when you want to **edit the roster before
profiling** — dropping the surgeons and psychologists a directory lists
alongside the physicians, or fixing a name PubMed indexes unusually.

### Hand-written topic lists (optional)

For a curated vocabulary — a specific drug list, your own topic labels —
pass a JSON config instead of `--auto`:

```bash
python profiler.py --config examples/ulcerative_colitis.json \
                   --researchers examples/researchers_uc.csv
```

Three worked examples ship in `examples/` (ulcerative colitis,
triple-negative breast cancer, epilepsy). The keys are documented in
[examples/ulcerative_colitis.json](examples/ulcerative_colitis.json). This
is strictly optional; automatic mode needs none of it.

### The roster CSV

```csv
name,author,affiliation,notes
Jordan Axelrad,Axelrad J,NYU Langone|NYU|New York University,
```

- `author` is the PubMed author-search form: `Surname Initials`. Names are
  converted automatically (`Jordan E. Axelrad, MD` → `Axelrad J`).
- `affiliation` filters PubMed's affiliation field. List **alternatives with
  `|`** — institutions publish under several names, and this matters more
  than it sounds: some NYU authors have 10 papers under "New York
  University" and zero under "NYU Langone". The scraper fills these in.
- `notes` and any other extra column are ignored by the program.

## Tests

```bash
python tests.py
```

Runs offline in about a second. **Run it before every change**, because
this tool is a stack of interacting heuristics — word lists, thresholds,
ordering rules — and fixing one has repeatedly broken another:

- tolerating `url1, url2` broke addresses containing commas in a query
  string, which real directory pages use;
- treating "director" as a non-clinical role rejected a genuine hospital
  directory, because one doctor listed there runs the centre;
- keeping unfinished runs in memory, so they could not be evicted
  mid-search, made them accumulate without limit.

None of those were caught by reasoning about the change. Every case in
`tests.py` is a bug that actually happened, so the file only grows when
something breaks.

## Known limits

- **Author disambiguation is name + affiliation only.** Daniel Friedman and
  David E. Friedman are both `Friedman D` at NYU, and PubMed cannot separate
  them either — their profiles come back identical. The scraper warns when
  it spots a collision; writing `Friedman DE` in the CSV splits them.
- **Directories are JavaScript-heavy.** The scraper waits 8 seconds for the
  provider list to render and retries at 20 seconds if a page comes back
  empty. A page that still yields nothing usually hides its list behind a
  search button — link straight to a results page.
- **Topic matching is keyword-based.** A paper that merely *mentions* a drug
  counts toward it.
- **Publication volume is not clinical skill.** A doctor with no papers may
  be the better clinician; this tool measures what someone researches, which
  is a different question. Treat the output as a starting point for
  conversation, not a verdict.

## License

MIT — see [LICENSE](LICENSE).
