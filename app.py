#!/usr/bin/env python3
"""
Med Research Finder -- window launcher
=====================================
Run:  python app.py

Opens a window (in your browser, served from localhost only). Paste the
directory URLs, press Run, get each doctor's research topics back. Nothing
else to configure: the condition and the topic vocabulary are worked out
from the pages and the papers themselves.

Needs FIRECRAWL_API_KEY in .env (and an email address, which NCBI requires
and the app remembers after the first run).
"""

import builtins
import html
import json
import os
import threading
import urllib.error
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import directory_scraper
import profiler

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")

# One entry per run, keyed by an id the page is handed when it starts.
# A single shared state would let two tabs (or two people) watch each
# other's run and believe the results were their own.
RUNS = {}
LOCK = threading.Lock()
_RUN_SEQ = 0
MAX_ACTIVE_RUNS = 1


def new_run():
    global _RUN_SEQ
    with LOCK:
        _RUN_SEQ += 1
        run_id = f"run{_RUN_SEQ}"
        RUNS[run_id] = {"lines": [], "done": False, "results": [],
                        "note": "", "doctors": [], "csv": "",
                        "condition": "", "problems": [], "detail": ""}
        # Keep the history short; these hold every paper's worth of log.
        for stale in list(RUNS)[:-5]:
            RUNS.pop(stale, None)
    return run_id


def make_logger(run_id):
    def log(*parts):
        with LOCK:
            run = RUNS.get(run_id)
            if run is not None:
                run["lines"].append(" ".join(str(p) for p in parts))
    return log


def set_field(run_id, **fields):
    with LOCK:
        run = RUNS.get(run_id)
        if run is not None:
            run.update(fields)


def remember_email(email):
    """NCBI needs an address on every request; ask once, not every run."""
    try:
        lines = []
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH) as fh:
                lines = [ln for ln in fh.read().splitlines()
                         if not ln.startswith("NCBI_EMAIL=")]
        lines.append(f"NCBI_EMAIL={email}")
        with open(ENV_PATH, "w") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:
        pass                                # convenience only, never fatal


PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Med Research Finder</title>
<style>
  html {{ background: #ffffff; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 820px;
         margin: 2rem auto; padding: 0 1rem; line-height: 1.45;
         color: #1a1a1a; background: #ffffff; }}
  h1 {{ font-size: 1.55rem; color: #1a1a1a; margin-bottom: .2rem; }}
  .sub {{ color: #555; margin-top: 0; }}
  h2 {{ font-size: 1.05rem; margin: 1.5rem 0 .3rem; color: #1a1a1a; }}
  p {{ color: #1a1a1a; }}
  .hint {{ color: #555; font-size: .9rem; margin: .2rem 0 .6rem; }}
  .reminder {{ background: #fff8e1; border: 1px solid #e6d9a8;
               border-radius: 6px; padding: .6rem .8rem; font-size: .92rem;
               margin: .4rem 0 .8rem; }}
  textarea, input[type=email] {{ width: 100%; box-sizing: border-box;
      padding: .5rem; border: 1px solid #bbb; border-radius: 6px;
      font-size: .95rem; background: #fff; color: #1a1a1a; }}
  textarea {{ height: 110px; }}
  button {{ margin-top: 1rem; padding: .6rem 1.8rem; font-size: 1rem;
           background: #1d4ed8; color: #fff; border: 0; border-radius: 6px;
           cursor: pointer; }}
  button:disabled {{ background: #93a6d8; cursor: default; }}
  .note {{ background: #f6f6f6; border-radius: 6px; padding: .7rem .9rem;
           color: #444; font-size: .93rem; margin: 1.2rem 0 0; }}
  #results {{ margin-top: 1.5rem; }}
  #results h2, #searched h2 {{ border-bottom: 2px solid #1a1a1a;
                               padding-bottom: .25rem; }}
  #searched {{ margin-top: 2rem; }}
  .plain {{ font-size: .93rem; color: #333; line-height: 1.7; }}
  .limit {{ color: #6b5b1f; font-size: .88rem; }}
  .problem {{ background: #fef2f2; border: 1px solid #fecaca;
              border-radius: 6px; padding: .7rem .9rem; margin: 1.2rem 0 0;
              font-size: .93rem; color: #7f1d1d; }}
  .problem ul {{ margin: .4rem 0 0; padding-left: 1.2rem; }}
  .problem li {{ margin: .2rem 0; }}
  .problem details {{ margin-top: .6rem; }}
  .problem summary {{ cursor: pointer; color: #9a3412; font-size: .85rem; }}
  .problem pre {{ white-space: pre-wrap; font-size: .78rem; color: #555;
                  background: #fff; border-radius: 4px; padding: .5rem;
                  margin-top: .4rem; max-height: 12rem; overflow-y: auto; }}
  .doc {{ margin-bottom: 1.4rem; padding-bottom: 1rem;
          border-bottom: 1px solid #eee; }}
  .doc h3 {{ margin: 0 0 .1rem; font-size: 1.08rem; }}
  .doc .meta {{ color: #666; font-size: .85rem; margin: 0 0 .45rem; }}
  .doc ul {{ margin: 0; padding-left: 1.2rem; }}
  .doc li {{ margin: .12rem 0; }}
  .stars {{ color: #b45309; letter-spacing: 1px; }}
  .thin {{ color: #666; font-size: .9rem; }}
  .detected {{ background: #eef2ff; border: 1px solid #c7d2fe;
               border-radius: 6px; padding: .55rem .8rem; font-size: .93rem;
               margin: 0 0 1.2rem; }}
  .intro {{ border-left: 3px solid #d4d4d8; padding-left: .9rem;
            margin: 1rem 0 1.6rem; }}
  .intro p {{ margin: .5rem 0; }}
  .legend {{ background: #fafafa; border: 1px solid #e5e5e5;
             border-radius: 8px; padding: .9rem 1.1rem; margin-top: 1.6rem; }}
  .legend h3 {{ margin: 0 0 .5rem; font-size: 1rem; }}
  .legend p {{ margin: .5rem 0; font-size: .93rem; }}
  .legend ul {{ margin: .4rem 0; padding-left: 1.3rem; font-size: .93rem; }}
  .legend li {{ margin: .25rem 0; }}
  .caveat {{ border-top: 1px solid #e5e5e5; padding-top: .6rem;
             color: #555; }}
</style>
</head>
<body>
<h1>Med Research Finder</h1>
<p class="sub">What each doctor actually publishes on, from PubMed.</p>

<div class="intro">
  <p><b>What this does.</b> Paste the web addresses of hospital
  &ldquo;find a doctor&rdquo; pages. This tool reads every doctor listed on
  them, looks each one up in PubMed (the national database of medical
  research), and shows you the specific subjects each of them publishes
  on &mdash; particular drugs, procedures, and clinical problems.</p>
  <p>It works out on its own which condition the directory covers, so
  there is nothing to choose or configure. A search takes roughly half a
  minute per doctor, so a directory page of twenty runs about ten
  minutes.</p>
  <p>The results appear below as <b>Most prominent researchers</b>. A
  complete spreadsheet &mdash; every doctor, every subject, with exact
  paper counts &mdash; is also saved to the <code>output</code> folder
  next to this program, and the full path is printed at the bottom of the
  page when the search finishes.</p>
</div>

<h2>Step 1 &mdash; Directory pages</h2>
<p>Enter the URL for every hospital directory you'd like to search,
separated by semicolons ( <b>;</b> ).</p>
<div class="reminder"><b>Reminder:</b> you may need to provide a separate
link for every <i>page</i> of a directory. If there are two pages of
pulmonologists, provide a URL for each page.<br>
<span class="limit">Up to {max_urls} pages per search. Each link must be a
hospital or medical school page listing doctors by name.</span></div>
<textarea id="urls" placeholder="https://hospital.org/find-a-doctor/pulmonology; https://hospital.org/find-a-doctor/pulmonology?page=2"></textarea>

<div id="emailbox" style="{email_display}">
  <h2>Your email</h2>
  <p class="hint">PubMed requires a contact address on every request.
  Entered once, then remembered.</p>
  <input type="email" id="email" value="{email}" placeholder="you@example.com">
</div>

<h2>Step 2 &mdash; Run</h2>
<button id="run" onclick="start()">Run</button>

<div class="legend">
  <h3>How to read the results</h3>
  <p>Each doctor is listed with the subjects they publish on, <b>most
  distinctive first</b>. The order is not simply who publishes most: a
  subject that nearly everyone in the group writes about tells you little,
  so subjects that set a doctor apart from their colleagues rise to the
  top.</p>
  <p><b>Asterisks mark first-authored papers.</b> One asterisk per paper
  on that subject where the doctor was the <i>first</i> author:</p>
  <ul>
    <li><span class="stars">&lowast;&lowast;&lowast;</span> &mdash; three
    first-authored papers on that subject.</li>
    <li><span class="stars">&lowast;x12</span> &mdash; twelve of them. Past
    five, the count replaces the row of asterisks so the list stays
    readable. <b>A number means more, not less.</b></li>
    <li>No asterisks &mdash; the doctor contributed to papers on that
    subject without being first author, which is common on large
    multi-center studies.</li>
  </ul>
  <p>First authorship usually means the work was that person's to drive,
  rather than one name among many. A doctor with 30 papers and many
  first-authored ones is often running their own research program; one
  with 300 papers and few may be a senior collaborator on other people's
  studies. Both are accomplished &mdash; they are different things.</p>
  <p class="caveat">Publishing is not the same as clinical skill. A doctor
  with no papers at all may be the better choice for your care. This shows
  what someone researches, which is a different question &mdash; use it as
  a starting point for conversation, not a verdict.</p>
</div>

<div id="note"></div>
<div id="results"></div>
<div id="searched"></div>

<script>
let RUN_ID = null;

// The page tells people not to refresh, but a ten-minute search should not
// be lost to a stray reload: the run lives on the server, so remember its
// id and reattach on load.
function rememberRun(id) {{
  RUN_ID = id;
  try {{ localStorage.setItem('mrf_run', id); }} catch (e) {{}}
}}
function forgetRun() {{
  try {{ localStorage.removeItem('mrf_run'); }} catch (e) {{}}
}}
window.addEventListener('load', function () {{
  let saved = null;
  try {{ saved = localStorage.getItem('mrf_run'); }} catch (e) {{}}
  if (saved) {{
    RUN_ID = saved;
    document.getElementById('run').disabled = true;
    poll();
  }}
}});

async function start() {{
  const body = new URLSearchParams({{
    urls: document.getElementById('urls').value,
    email: (document.getElementById('email')||{{value:''}}).value,
  }});
  document.getElementById('run').disabled = true;
  document.getElementById('results').innerHTML = '';
  document.getElementById('searched').innerHTML = '';
  document.getElementById('note').innerHTML =
    '<p class="note">Gathering data, do not refresh this page, ' +
    'this may take a few minutes :)</p>';
  const resp = await fetch('/run', {{method: 'POST', body}});
  const info = await resp.json();
  // Poll only this run: another tab's run must never render here.
  rememberRun(info.run);
  poll();
}}
function esc(s) {{
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}
function render(results) {{
  if (!results.length) return '';
  return results.map(function (doc) {{
    const topics = doc.topics.map(function (t) {{
      const st = t.stars ? ' <span class="stars">' + esc(t.stars) + '</span>' : '';
      return '<li>' + esc(t.topic) + st + '</li>';
    }}).join('');
    return '<div class="doc"><h3>' + esc(doc.name) + '</h3>' +
           '<p class="meta">' + esc(doc.meta) + '</p>' +
           '<ul>' + topics + '</ul></div>';
  }}).join('');
}}
async function poll() {{
  if (!RUN_ID) return;
  const r = await fetch('/log?run=' + encodeURIComponent(RUN_ID));
  const data = await r.json();

  if (data.problems && data.problems.length) {{
    const items = data.problems.map(function (p) {{
      return '<li>' + esc(p) + '</li>'; }}).join('');
    // Diagnostics stay folded away: useful for a bug report, unhelpful
    // as the first thing someone reads when a search fails.
    const detail = data.detail
      ? '<details><summary>Technical details</summary><pre>' +
        esc(data.detail) + '</pre></details>' : '';
    document.getElementById('note').innerHTML =
      '<div class="problem"><b>That didn\\'t work.</b><ul>' + items +
      '</ul>' + detail + '</div>';
    document.getElementById('run').disabled = false;
    forgetRun();
    return;
  }}

  if (!data.done) {{
    document.getElementById('note').innerHTML =
      '<p class="note">' + esc(data.note ||
        'Gathering data, do not refresh this page, ' +
        'this may take a few minutes :)') + '</p>';
  }} else {{
    // Finished: the running note goes away, but a message that explains
    // an empty result (no doctors found, an error) has to stay.
    document.getElementById('note').innerHTML =
      (data.results && data.results.length) || !data.note
        ? '' : '<p class="note">' + esc(data.note) + '</p>';
  }}

  if (data.results && data.results.length) {{
    // Name the condition on screen: if it guessed wrong from the pages,
    // that should be obvious before anyone reads the topics.
    const banner = data.condition
      ? '<p class="detected">Showing research on <b>' + esc(data.condition) +
        '</b>, detected from the pages you entered.</p>' : '';
    document.getElementById('results').innerHTML =
      '<h2>Most prominent researchers</h2>' + banner + render(data.results);
  }}

  if (data.done && data.doctors && data.doctors.length) {{
    let tail = '<h2>Doctors searched</h2><p class="plain">' +
               data.doctors.map(esc).join('<br>') + '</p>';
    if (data.csv) {{
      tail += '<p class="plain">Full results saved to:<br>' +
              esc(data.csv) + '</p>';
    }}
    document.getElementById('searched').innerHTML = tail;
  }}

  if (data.done) {{
    document.getElementById('run').disabled = false;
    forgetRun();
    return;
  }}
  setTimeout(poll, 1500);
}}
</script>
</body>
</html>
"""


def render_page():
    email = os.environ.get("NCBI_EMAIL", "")
    return PAGE.format(
        email=html.escape(email),
        email_display="display:none" if email else "",
        max_urls=directory_scraper.MAX_URLS,
    )


TRY_AGAIN = ("Please try again, and make sure each link goes to a medical "
             "provider directory — not plain text, and not a link to "
             "something else.")


def friendly_error(exc):
    """Turn any failure into something a non-technical person can act on.

    Returns (message, technical_detail). The detail is kept for the
    collapsed section and for bug reports; it never leads.
    """
    if isinstance(exc, directory_scraper.FriendlyError):
        return exc.message, exc.detail

    text = str(exc)
    lowered = text.lower()
    detail = f"{type(exc).__name__}: {text}"

    if "firecrawl api key" in lowered or "fc-" in lowered:
        return ("This tool needs a Firecrawl key to read directory pages, "
                "and none is set up. Whoever installed this can add one to "
                "the .env file.", detail)
    if "payment" in lowered or "credit" in lowered or "402" in text:
        return ("The page-reading service has run out of credits, so the "
                "directory pages could not be read. This is an account "
                "issue, not a problem with what you entered.", detail)
    if "429" in text or "rate limit" in lowered:
        return ("The medical research database is asking us to slow down. "
                "Please wait a few minutes and try again.", detail)
    if isinstance(exc, (urllib.error.URLError, OSError)) or \
            "urlopen" in lowered or "connection" in lowered or \
            "timed out" in lowered or "nodename" in lowered:
        return ("Could not reach the internet. Check your connection and "
                "try again.", detail)
    if "dns" in lowered or "resolution" in lowered or \
            "name or service not known" in lowered:
        return ("That web address could not be found. Check that the link "
                "opens in your browser, then copy it again from the "
                "address bar.", detail)
    if "404" in text or "not found" in lowered:
        return ("One of those pages could not be found. Check that each "
                "link still opens in your browser. " + TRY_AGAIN, detail)

    return ("Something went wrong while searching. " + TRY_AGAIN, detail)


FIND_THE_DIRECTORY = ("On the hospital's website, look for a link named "
                      "\"Find a Doctor\", \"Our Doctors\", \"Our Team\" or "
                      "\"Physicians\". Open it, then copy the address of "
                      "that page from your browser's address bar.")


def explain_rejections(rejected):
    """Say why nothing was searched, in terms of what to do next."""
    if not rejected:
        return ["No doctors were found on those pages. " + TRY_AGAIN]

    reasons = {verdict for _, verdict, _ in rejected}
    problems = []

    if "not_medical" in reasons:
        what = next((w for _, v, w in rejected if v == "not_medical" and w),
                    "")
        problems.append(
            (f"That page lists {what}, not medical providers. "
             if what else
             "The people on that page are not medical providers. ")
            + "This tool searches medical research, so it only works with "
              "directories of doctors and other clinicians.")
    if "home_page" in reasons:
        problems.append(
            "That is the hospital's home page, not a list of doctors. "
            + FIND_THE_DIRECTORY)
    if "not_a_directory" in reasons:
        problems.append(
            "That page does not list doctors to choose from — it may be an "
            "article, a patient-information page, or a single doctor's "
            "profile. " + FIND_THE_DIRECTORY)
    if "too_complex" in reasons:
        problems.append(
            "That page is too big and complicated for this tool to read — "
            "booking sites like Zocdoc and Healthgrades pack thousands of "
            "listings, filters and adverts into one page.")
        problems.append(
            "This tool works best on a hospital's or medical school's own "
            "directory, where doctors are listed with the institution they "
            "belong to. That institution is what lets it find their "
            "research; a booking listing does not carry one.")
    if "no_doctors" in reasons:
        problems.append(
            "No doctors could be read from that page. If it does list them, "
            "the list may only appear after you run a search — do the "
            "search yourself, then paste the address of the results page.")
    return problems


def pipeline(run_id, urls, email):
    log = make_logger(run_id)
    try:
        from Bio import Entrez
        Entrez.email = email
        if os.environ.get("NCBI_API_KEY"):
            Entrez.api_key = os.environ["NCBI_API_KEY"]

        api_key = directory_scraper.get_api_key()
        set_field(run_id, note=f"Reading {len(urls)} directory page(s)...")
        log(f"Reading {len(urls)} directory page(s) ...")
        rows, detected, rejected = directory_scraper.build_roster(
            urls, api_key, log=log)
        if not rows:
            set_field(run_id, problems=explain_rejections(rejected))
            return
        if rejected:
            # Some pages worked. Say which did not, but carry on with the
            # rest rather than throwing away a good search.
            log("Skipped: " + ", ".join(url for url, _, _ in rejected))

        out_dir = os.path.join(HERE, "output")
        os.makedirs(out_dir, exist_ok=True)
        # Per-run filename: concurrent runs must not overwrite each other.
        roster_path = os.path.join(out_dir, f"researchers_{run_id}.csv")
        directory_scraper.write_roster(rows, roster_path)
        log(f"Found {len(rows)} doctors.")
        log("")

        researchers = profiler.load_researchers(roster_path)
        set_field(run_id, doctors=[r["name"] for r in researchers])

        def progress(done, total):
            set_field(run_id, note=f"Gathering data — searched {done} of "
                                   f"{total} doctors. Do not refresh this "
                                   f"page, this may take a few minutes :)")

        csv_path, result_rows = profiler.run_auto(
            detected, researchers, out_dir, log=log, on_progress=progress)
        set_field(run_id, condition=detected, csv=csv_path or "")

        # Reshape the flat CSV rows into per-doctor blocks for the page.
        by_doctor = {}
        for row in result_rows:
            entry = by_doctor.setdefault(row["researcher"], {
                "name": row["researcher"],
                "meta": f"{row['their_focus_papers']} papers",
                "topics": [],
            })
            entry["topics"].append({
                "topic": row["topic"],
                "stars": "*" * row["first_author_papers"]
                         if row["first_author_papers"] <= 5
                         else f"*x{row['first_author_papers']}",
            })
        with LOCK:
            if run_id in RUNS:
                RUNS[run_id]["results"] = list(by_doctor.values())

    except BaseException as exc:                    # SystemExit included
        log(f"ERROR: {exc}")
        message, detail = friendly_error(exc)
        set_field(run_id, problems=[message], detail=detail)
    finally:
        with LOCK:
            if run_id in RUNS:
                RUNS[run_id]["done"] = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, content_type="text/html; charset=utf-8", code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            self._send(render_page())
        elif path == "/log":
            run_id = parse_qs(query).get("run", [""])[0]
            with LOCK:
                run = RUNS.get(run_id)
                payload = ({"done": run["done"],
                            "results": list(run["results"]),
                            "condition": run.get("condition", ""),
                            "note": run.get("note", ""),
                            "problems": list(run.get("problems", [])),
                            "detail": run.get("detail", ""),
                            "doctors": list(run.get("doctors", [])),
                            "csv": run.get("csv", "")} if run else
                           {"done": True, "results": [], "doctors": [],
                            "csv": "", "condition": "", "problems": [],
                            "detail": "",
                            "note": "This run is no longer available. "
                                    "Press Run to start a new one."})
            self._send(json.dumps(payload), "application/json")
        else:
            self._send("not found", code=404)

    def do_POST(self):
        if self.path != "/run":
            self._send("not found", code=404)
            return
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode())
        raw_urls = form.get("urls", [""])[0].strip()
        email = (form.get("email", [""])[0].strip()
                 or os.environ.get("NCBI_EMAIL", ""))

        urls, problems = directory_scraper.validate_urls(raw_urls)
        if not email or "@" not in email:
            problems.append("Please enter a valid email address — PubMed "
                            "requires one on every request.")

        # Each run holds open PubMed connections for minutes. Letting them
        # pile up gets the user's own address rate-limited, so refuse
        # rather than degrade every run at once.
        with LOCK:
            active = sum(1 for r in RUNS.values() if not r["done"])
        if active >= MAX_ACTIVE_RUNS and not problems:
            problems.append(
                f"A search is already running. Please wait for it to finish "
                f"before starting another — PubMed limits how fast anyone "
                f"may search, and running several at once gets them all "
                f"blocked.")

        run_id = new_run()
        if problems or not urls:
            if not problems:
                problems = ["Please paste at least one directory web address."]
            with LOCK:
                RUNS[run_id]["problems"] = problems
                RUNS[run_id]["done"] = True
            self._send(json.dumps({"ok": False, "run": run_id}),
                       "application/json")
            return

        if email != os.environ.get("NCBI_EMAIL", ""):
            os.environ["NCBI_EMAIL"] = email
            remember_email(email)

        threading.Thread(target=pipeline, args=(run_id, urls, email),
                         daemon=True).start()
        self._send(json.dumps({"ok": True, "run": run_id}),
                   "application/json")


def main():
    directory_scraper.load_env()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Med Research Finder: {url}")
    print("(Press Ctrl+C to quit.)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
