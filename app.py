#!/usr/bin/env python3
"""
PubMed Topic Profiler -- window launcher
========================================
Run:  python app.py

Opens a window (in your default browser, served from localhost only) where
you paste hospital-directory URLs (semicolon-separated, one URL per
directory PAGE), pick a disease config, and click Run. The app then:

  1. scrapes each directory page with Firecrawl and builds the roster CSV
  2. profiles every extracted physician against PubMed
  3. drops the report + CSV in output/ and streams progress to the window

Needs FIRECRAWL_API_KEY and an NCBI email (both can live in .env).
Everything runs on your machine; the only network calls are to Firecrawl
and NCBI.
"""

import builtins
import glob
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

# ---------------------------------------------------------------------------
# Shared state between the web handlers and the pipeline thread
# ---------------------------------------------------------------------------
STATE = {"lines": [], "running": False, "done": False}
LOCK = threading.Lock()


def log(*parts):
    with LOCK:
        STATE["lines"].append(" ".join(str(p) for p in parts))


PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>PubMed Topic Profiler</title>
<style>
  html {{ background: #ffffff; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 780px;
         margin: 2rem auto; padding: 0 1rem; line-height: 1.45;
         color: #1a1a1a; background: #ffffff; }}
  h1 {{ font-size: 1.5rem; color: #1a1a1a; }}
  h2 {{ font-size: 1.05rem; margin: 1.4rem 0 .3rem; color: #1a1a1a; }}
  p {{ color: #1a1a1a; }}
  .hint {{ color: #555; font-size: .9rem; margin: .2rem 0 .6rem; }}
  .reminder {{ background: #fff8e1; border: 1px solid #e6d9a8; border-radius: 6px;
               padding: .6rem .8rem; font-size: .92rem; margin: .4rem 0 .8rem; }}
  textarea, input[type=text], input[type=email], select {{
      width: 100%; box-sizing: border-box; padding: .5rem;
      border: 1px solid #bbb; border-radius: 6px; font-size: .95rem;
      background: #ffffff; color: #1a1a1a; }}
  textarea {{ height: 90px; }}
  button {{ margin-top: 1rem; padding: .55rem 1.6rem; font-size: 1rem;
           background: #1d4ed8; color: white; border: 0; border-radius: 6px;
           cursor: pointer; }}
  button:disabled {{ background: #93a6d8; cursor: default; }}
  #log {{ background: #111; color: #ddd; font-family: ui-monospace, monospace;
         font-size: .82rem; padding: .8rem; border-radius: 6px;
         white-space: pre-wrap; min-height: 8rem; max-height: 24rem;
         overflow-y: auto; margin-top: 1rem; }}
</style>
</head>
<body>
<h1>PubMed Topic Profiler</h1>

<h2>Step 1 &mdash; Directory pages</h2>
<p>Enter the URL for every hospital directory you'd like to search,
separated by semicolons ( <b>;</b> ).</p>
<div class="reminder"><b>Reminder:</b> you may need to provide a separate
link for every <i>page</i> of a directory. If there are two pages of
pulmonologists, provide a URL for each page.</div>
<textarea id="urls" placeholder="https://hospital.org/find-a-doctor/gastroenterology; https://hospital.org/find-a-doctor/gastroenterology?page=2"></textarea>

<h2>Step 2 &mdash; Disease / condition</h2>
<p class="hint">The config file names the condition and the topics to
track. Copy an example in <code>examples/</code> for a new disease.</p>
<select id="config">{config_options}</select>

<h2>Step 3 &mdash; Your email</h2>
<p class="hint">Required by NCBI on every PubMed request.</p>
<input type="email" id="email" value="{email}" placeholder="you@example.com">

<button id="run" onclick="start()">Run</button>

<div id="log">Waiting to start.</div>

<script>
async function start() {{
  const body = new URLSearchParams({{
    urls: document.getElementById('urls').value,
    config: document.getElementById('config').value,
    email: document.getElementById('email').value,
  }});
  document.getElementById('run').disabled = true;
  await fetch('/run', {{method: 'POST', body}});
  poll();
}}
async function poll() {{
  const r = await fetch('/log');
  const data = await r.json();
  const el = document.getElementById('log');
  el.textContent = data.lines.join('\\n') || 'Starting...';
  el.scrollTop = el.scrollHeight;
  if (data.done) {{ document.getElementById('run').disabled = false; return; }}
  setTimeout(poll, 1200);
}}
</script>
</body>
</html>
"""


def config_label(path):
    """Show the condition each config actually profiles, not its filename."""
    try:
        with open(path) as fh:
            label = (json.load(fh).get("focus") or {}).get("label", "")
    except Exception:
        label = ""
    base = os.path.basename(path)
    return f"{label}  ({base})" if label else base


def render_page():
    configs = sorted(glob.glob(os.path.join(HERE, "examples", "*.json")))
    # No pre-selected condition: an unnoticed default is how you end up
    # profiling epilepsy doctors against a breast-cancer vocabulary.
    options = '<option value="" selected>-- choose a condition --</option>'
    options += "".join(
        f'<option value="{html.escape(p)}">{html.escape(config_label(p))}'
        f"</option>"
        for p in configs
    )
    if not configs:
        options = '<option value="">-- no configs found in examples/ --</option>'
    return PAGE.format(
        config_options=options,
        email=html.escape(os.environ.get("NCBI_EMAIL", "")),
    )


def pipeline(raw_urls, config_path, email):
    try:
        from Bio import Entrez
        Entrez.email = email
        if os.environ.get("NCBI_API_KEY"):
            Entrez.api_key = os.environ["NCBI_API_KEY"]

        cfg = profiler.load_config(config_path)
        focus_label = cfg["focus"]["label"]
        log(f"Profiling for: {focus_label}")
        log("(If that is not the condition you meant, stop and pick a "
            "different config -- everyone will otherwise score zero.)")
        log("")

        api_key = directory_scraper.get_api_key()
        urls = directory_scraper.split_urls(raw_urls)
        log(f"Step 1: scraping {len(urls)} directory page(s) ...")
        rows = directory_scraper.build_roster(urls, api_key, log=log)
        if not rows:
            log("No providers were extracted from those pages. If the "
                "directory loads its list via a search box, link directly "
                "to a results page.")
            return

        out_dir = os.path.join(HERE, "output")
        os.makedirs(out_dir, exist_ok=True)
        roster_path = os.path.join(out_dir, "researchers_from_urls.csv")
        directory_scraper.write_roster(rows, roster_path)
        log(f"Roster written: {roster_path}  ({len(rows)} people)")

        log("")
        log(f"Step 2: profiling everyone for {focus_label} "
            "(this is the slow part) ...")
        researchers = profiler.load_researchers(roster_path)

        real_print = builtins.print
        builtins.print = lambda *a, **k: log(*a)
        try:
            csv_path = profiler.run(cfg, researchers, out_dir)
        finally:
            builtins.print = real_print

        log("")
        log(f"Done. Report above; CSV: {csv_path}")
    except SystemExit as exc:
        log(f"STOPPED: {exc}")
    except Exception as exc:
        log(f"ERROR: {exc}")
    finally:
        with LOCK:
            STATE["running"] = False
            STATE["done"] = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):          # silence request spam
        pass

    def _send(self, body, content_type="text/html; charset=utf-8", code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(render_page())
        elif self.path == "/log":
            with LOCK:
                payload = {"lines": list(STATE["lines"]),
                           "done": STATE["done"]}
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
        config_path = form.get("config", [""])[0].strip()
        email = form.get("email", [""])[0].strip()

        with LOCK:
            if STATE["running"]:
                self._send(json.dumps({"ok": False}), "application/json")
                return
            STATE["lines"] = []
            STATE["done"] = False

        problems = []
        if not raw_urls:
            problems.append("Please paste at least one directory URL.")
        if not email or "@" not in email:
            problems.append("Please enter a valid email -- NCBI requires one.")
        if not config_path or not os.path.exists(config_path):
            problems.append(f"Config not found: {config_path}")
        if problems:
            with LOCK:
                STATE["lines"] = problems
                STATE["done"] = True
            self._send(json.dumps({"ok": False}), "application/json")
            return

        with LOCK:
            STATE["running"] = True
        threading.Thread(target=pipeline,
                         args=(raw_urls, config_path, email),
                         daemon=True).start()
        self._send(json.dumps({"ok": True}), "application/json")


def main():
    directory_scraper.load_env()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"PubMed Topic Profiler window: {url}")
    print("(Close this terminal or press Ctrl+C to quit.)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
