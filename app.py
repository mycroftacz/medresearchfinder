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


def new_run():
    global _RUN_SEQ
    with LOCK:
        _RUN_SEQ += 1
        run_id = f"run{_RUN_SEQ}"
        RUNS[run_id] = {"lines": [], "done": False, "results": []}
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
  #status {{ color: #555; font-size: .88rem; margin-top: .8rem;
             white-space: pre-wrap; font-family: ui-monospace, monospace;
             max-height: 11rem; overflow-y: auto; background: #f6f6f6;
             border-radius: 6px; padding: .6rem; display: none; }}
  #results {{ margin-top: 1.5rem; }}
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
</div>

<h2>Step 1 &mdash; Directory pages</h2>
<p>Enter the URL for every hospital directory you'd like to search,
separated by semicolons ( <b>;</b> ).</p>
<div class="reminder"><b>Reminder:</b> you may need to provide a separate
link for every <i>page</i> of a directory. If there are two pages of
pulmonologists, provide a URL for each page.</div>
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

<div id="status"></div>
<div id="results"></div>

<script>
let RUN_ID = null;
async function start() {{
  const body = new URLSearchParams({{
    urls: document.getElementById('urls').value,
    email: (document.getElementById('email')||{{value:''}}).value,
  }});
  document.getElementById('run').disabled = true;
  document.getElementById('results').innerHTML = '';
  document.getElementById('status').style.display = 'block';
  document.getElementById('status').textContent = 'Starting...';
  const resp = await fetch('/run', {{method: 'POST', body}});
  const info = await resp.json();
  // Poll only this run: another tab's run must never render here.
  RUN_ID = info.run;
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
  document.getElementById('status').textContent = data.lines.join('\\n');
  const el = document.getElementById('status');
  el.scrollTop = el.scrollHeight;
  if (data.results && data.results.length) {{
    // Name the condition on screen: if it guessed wrong from the pages,
    // that should be obvious before anyone reads the topics.
    const banner = data.condition
      ? '<p class="detected">Showing research on <b>' + esc(data.condition) +
        '</b>, detected from the pages you entered.</p>' : '';
    document.getElementById('results').innerHTML =
      banner + render(data.results);
  }}
  if (data.done) {{ document.getElementById('run').disabled = false; return; }}
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
    )


def pipeline(run_id, raw_urls, email):
    log = make_logger(run_id)
    try:
        from Bio import Entrez
        Entrez.email = email
        if os.environ.get("NCBI_API_KEY"):
            Entrez.api_key = os.environ["NCBI_API_KEY"]

        api_key = directory_scraper.get_api_key()
        urls = directory_scraper.split_urls(raw_urls)
        log(f"Reading {len(urls)} directory page(s) ...")
        rows, detected = directory_scraper.build_roster(
            urls, api_key, log=log)
        if not rows:
            log("No doctors were found on those pages. If the directory "
                "hides its list behind a search button, link straight to a "
                "results page.")
            return

        out_dir = os.path.join(HERE, "output")
        os.makedirs(out_dir, exist_ok=True)
        # Per-run filename: concurrent runs must not overwrite each other.
        roster_path = os.path.join(out_dir, f"researchers_{run_id}.csv")
        directory_scraper.write_roster(rows, roster_path)
        log(f"Found {len(rows)} doctors.")
        log("")

        researchers = profiler.load_researchers(roster_path)
        _, result_rows = profiler.run_auto(
            detected, researchers, out_dir, log=log)

        with LOCK:
            if run_id in RUNS:
                RUNS[run_id]["condition"] = detected

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

    except SystemExit as exc:
        log(f"STOPPED: {exc}")
    except Exception as exc:
        log(f"ERROR: {exc}")
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
                payload = ({"lines": list(run["lines"]), "done": run["done"],
                            "results": list(run["results"]),
                            "condition": run.get("condition", "")} if run else
                           {"lines": ["This run is no longer available. "
                                      "Press Run to start a new one."],
                            "done": True, "results": []})
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

        problems = []
        if not raw_urls:
            problems.append("Please paste at least one directory URL.")
        if not email or "@" not in email:
            problems.append("Please enter a valid email -- PubMed requires one.")

        run_id = new_run()
        if problems:
            with LOCK:
                RUNS[run_id]["lines"] = problems
                RUNS[run_id]["done"] = True
            self._send(json.dumps({"ok": False, "run": run_id}),
                       "application/json")
            return

        if email != os.environ.get("NCBI_EMAIL", ""):
            os.environ["NCBI_EMAIL"] = email
            remember_email(email)

        threading.Thread(target=pipeline, args=(run_id, raw_urls, email),
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
