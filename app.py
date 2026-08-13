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
import re
import secrets
import threading
import urllib.error
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import auto_topics
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

# This server listens on localhost, which does NOT mean only this page can
# reach it: any site open in the browser can post a form to 127.0.0.1 and
# have the browser send it. Without a check, a page you happened to visit
# could start searches on your Firecrawl credits and rewrite your stored
# email. Requests must carry a token that only the page we served knows.
SESSION_TOKEN = secrets.token_urlsafe(32)

# Hosts this server will answer to. Loopback covers running it on your own
# machine. A hosting service knows its own address and passes it in, which
# is better than asking someone to copy it into a settings page -- the
# hostname is not knowable until the service exists, and getting it wrong
# refuses every search.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}

def _register_host(value):
    for name in (value or "").replace(",", " ").split():
        name = name.strip().lower()
        if "//" in name:                      # a full URL was supplied
            name = name.split("//", 1)[1]
        name = name.split("/")[0].split(":")[0]
        if name:
            ALLOWED_HOSTS.add(name)

for _source in ("RENDER_EXTERNAL_HOSTNAME",   # Render sets this itself
                "RENDER_EXTERNAL_URL",
                "FLY_APP_NAME",
                "PUBLIC_HOST"):               # manual override, any host
    _register_host(os.environ.get(_source, ""))
if os.environ.get("FLY_APP_NAME"):
    ALLOWED_HOSTS.add(f"{os.environ['FLY_APP_NAME']}.fly.dev".lower())


def new_run():
    global _RUN_SEQ
    with LOCK:
        _RUN_SEQ += 1
        run_id = f"r{_RUN_SEQ}-{secrets.token_urlsafe(9)}"
        # "progress" is transient and must never outlive the run;
        # "note" is a finished message meant to be read afterwards.
        # One field serving both left a stale "do not refresh" on screen
        # after a completed search, which read as a hang.
        RUNS[run_id] = {"lines": [], "done": False, "results": [],
                        "progress": "", "note": "", "doctors": [],
                        "csv": "", "condition": "", "problems": [],
                        "detail": "", "thin": [], "broad": False}
        # Keep the history short, but never discard a run that is still
        # going: evicting it lost the page's only handle on its own search
        # ("this run is no longer available" mid-search) and let the
        # one-at-a-time guard through, since a forgotten run counts as
        # nobody running.
        finished = [rid for rid, r in RUNS.items() if r["done"]]
        for stale in finished[:-5]:
            RUNS.pop(stale, None)
        # Backstop: a run that somehow never reports finishing must not
        # pin its logs in memory forever.
        while len(RUNS) > 50:
            RUNS.pop(next(iter(RUNS)), None)
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


EMAIL_RE = re.compile(r"^[^@\s,;<>\"'\\]{1,64}@[a-z0-9]"
                      r"([a-z0-9-]{0,61}[a-z0-9])?"
                      r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$", re.I)


def valid_email(email):
    return bool(EMAIL_RE.match((email or "").strip()))


def remember_email(email):
    """NCBI needs an address on every request; ask once, not every run.

    The value is written into .env, so it must be a single clean line: an
    address containing a newline could otherwise append its own settings
    to the file and replace the API key.
    """
    email = (email or "").strip()
    if not valid_email(email):
        return
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
<title>Which doctor is most published?</title>
<style>
  html {{
    background-color: #eaf4fc;
    /* A wash of very small Y shapes that thins out down the page: the
       fading white layer on top is what turns the texture into a
       gradient, so the gradient is made of the Ys themselves. */
    background-image:
      linear-gradient(180deg,
        rgba(255,255,255,0.00) 0%,
        rgba(255,255,255,0.55) 42%,
        rgba(255,255,255,0.92) 78%,
        rgba(255,255,255,1.00) 100%),
      url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2728%27%20height%3D%2728%27%20viewBox%3D%270%200%2028%2028%27%3E%3Cg%20fill%3D%27none%27%20stroke%3D%27%237fb2dc%27%20stroke-width%3D%271.05%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Cpath%20d%3D%27M4%205%20L7%208.4%20L10%205%20M7%208.4%20L7%2012%27%2F%3E%3Cpath%20d%3D%27M18%2019%20L21%2022.4%20L24%2019%20M21%2022.4%20L21%2026%27%2F%3E%3C%2Fg%3E%3C%2Fsvg%3E"),
      linear-gradient(180deg, #d5e9f8 0%, #e9f4fc 55%, #f7fbfe 100%);
    background-repeat: no-repeat, repeat, no-repeat;
    background-size: cover, 28px 28px, cover;
    background-attachment: fixed, fixed, fixed;
  }}
  body {{ font-family: -apple-system, system-ui, sans-serif;
         max-width: 820px; box-sizing: border-box;
         margin: 2rem auto; padding: 1.6rem 1.9rem 2.4rem;
         line-height: 1.45; color: #1a1a1a;
         /* Text sat straight on the Y texture and fought it. A mostly
            opaque sheet keeps every heading and paragraph legible while
            the pattern still shows through faintly and frames the page
            down both margins. */
         background: rgba(255, 255, 255, 0.90);
         border: 1px solid rgba(127, 178, 220, 0.35);
         border-radius: 14px;
         box-shadow: 0 2px 20px rgba(31, 74, 120, 0.10); }}
  h1 {{ font-size: 1.55rem; color: #16324f; margin: 0 0 .6rem;
        line-height: 1.25; }}
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
  #results h2, #searched h2, #thin h2 {{ border-bottom: 2px solid #1a1a1a;
                                          padding-bottom: .25rem; }}
  #thin {{ margin-top: 2rem; }}
  #thin ul {{ padding-left: 1.2rem; }}
  .caution-inline {{ color: #92400e; font-size: .85rem; margin: 0 0 .4rem; }}
  .verify {{ font-size: .82rem; color: #666; margin: .4rem 0 0; }}
  .verify a {{ color: #1d4ed8; }}
  .samename {{ background: #fff8e1; border: 1px solid #e6d9a8;
               border-radius: 6px; padding: .6rem .8rem; font-size: .9rem;
               margin: 0 0 1.2rem; }}
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
  .intro {{ border-left: 3px solid #d4d4d8; padding-left: .9rem;
            margin: 1rem 0 1.6rem; }}
  .intro p {{ margin: .5rem 0; }}
  .scope {{ background: #f0f7ff; border: 1px solid #b9d5f2;
            border-radius: 8px; padding: .9rem 1.1rem; margin: 0 0 1.6rem; }}
  .scope h3 {{ margin: 0 0 .5rem; font-size: 1rem; }}
  .scope p {{ margin: .5rem 0; font-size: .93rem; }}
  .example-url {{ background: #fff; border: 1px solid #cfe0f0;
                  border-radius: 6px; padding: .5rem .7rem; }}
  .example-url code {{ font-size: .84rem; word-break: break-all; }}
  .example-why {{ color: #555; font-size: .85rem; }}
  .note.finished {{ background: #ecfdf5; border: 1px solid #a7f3d0;
                    color: #065f46; font-weight: 600; }}
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
<h1>Which doctor is most widely published on your specific problem?</h1>

<div class="intro">
  <p><b>What does this tool do?</b> When you paste the web addresses of
  hospital &ldquo;find a doctor&rdquo; pages, this tool reads every doctor
  listed on them, looks each one up in PubMed (the national database of
  medical research), and shows you the specific subjects each of them
  publishes on (specific drugs, procedures, and clinical problems).</p>
  <p>It works out on its own which condition the directory covers, so
  there is nothing to choose or configure. A search takes a little while,
  so you may see a message advising you to wait.</p>
  <p>The results appear below as &ldquo;<b>Most prominent
  researchers</b>.&rdquo; A complete spreadsheet (every doctor, every
  subject, with exact paper counts) is also saved to the
  <code>output</code> folder next to this program, and the full path is
  printed at the bottom of the page when the search finishes.</p>
</div>

<div class="scope">
  <h3>What to paste, and what not to</h3>
  <p><b>Best results:</b> a page from a <b>research hospital or medical
  school</b> that lists doctors by name &mdash; a department's team page,
  or a condition centre's list of specialists. These are the places where
  doctors both treat patients and publish research, which is what this
  tool measures. A page that works well looks like this:</p>
  <p class="example-url"><code>https://nyulangone.org/locations/inflammatory-bowel-disease-center</code><br>
  <span class="example-why">&mdash; one condition, one hospital, doctors
  listed by name.</span></p>
  <p><b>Won't work &mdash; booking sites.</b> Zocdoc, Healthgrades, Vitals
  and similar services are not supported yet. Their pages are built for
  booking appointments and are too large and complex for this tool to
  read, and their listings identify a practice rather than a hospital,
  which is what makes a research search possible.</p>
  <p><b>Works, but expect little &mdash; small local practices.</b> You can
  paste a private practice's page and it will run. Most community doctors
  do not publish research at all, so the usual result is
  &ldquo;too little published research to profile&rdquo;. That is a real
  answer, not a fault &mdash; but it is not what this tool is designed to
  find, and an excellent local doctor will look identical to a poor one
  here.</p>
  <p><b>Won't work &mdash; a hospital's front page or its list of
  departments.</b> Those name specialties, not people. Click through to
  the specialty you care about first, then paste that page.</p>
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

<!-- Status sits directly under the button, above the reading guide: while
     a search runs this is the only thing telling the user it started, and
     below the guide it was off-screen exactly when it mattered. -->
<div id="note"></div>

<div class="legend">
  <h3>How to read the results</h3>
  <p>Each doctor is listed with the subjects they publish on, <b>most
  distinctive first</b>.</p>
  <p><b>What &ldquo;most distinctive&rdquo; means.</b> The list is not
  simply ordered by who publishes most. A subject that nearly everyone in
  the group writes about cannot tell you who to see, so it is pushed down;
  a subject that sets one doctor apart from their colleagues is pushed
  up.</p>
  <p>Say you search an epilepsy centre. Every doctor there publishes on
  seizures and on brain scans, so those subjects say nothing about any one
  of them. But if one doctor has written nine papers on a particular
  drug and nobody else has written any, that rises to the top of their
  list &mdash; because it is the thing that makes them different from the
  colleague in the next office.</p>
  <p>So a subject near the top means <i>this doctor in particular</i>
  works on it, not merely that they have published a lot. Both matter, and
  the paper count beside each name tells you the volume.</p>
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

<div id="results"></div>
<div id="thin"></div>
<div id="searched"></div>

<script>
const TOKEN = "{token}";
let RUN_ID = null;
let ANNOUNCED = false;   // scroll to the results only once per run

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
    token: TOKEN,
  }});
  document.getElementById('run').disabled = true;
  document.getElementById('results').innerHTML = '';
  document.getElementById('thin').innerHTML = '';
  document.getElementById('searched').innerHTML = '';
  document.getElementById('note').innerHTML =
    '<p class="note">Gathering data, do not refresh this page, ' +
    'this may take a few minutes :)</p>';
  ANNOUNCED = false;
  const resp = await fetch('/run', {{method: 'POST', body}});
  const info = await resp.json();
  // Poll only this run: another tab's run must never render here.
  rememberRun(info.run);
  poll();
}}
function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}
function render(results) {{
  if (!results.length) return '';
  return results.map(function (doc) {{
    const topics = doc.topics.map(function (t) {{
      const st = t.stars ? ' <span class="stars">' + esc(t.stars) + '</span>' : '';
      return '<li>' + esc(t.topic) + st + '</li>';
    }}).join('');
    const caution = doc.caution
      ? '<p class="caution-inline">' + esc(doc.caution) + '</p>' : '';
    // PubMed cannot tell two people with the same surname and initial
    // apart, so every profile carries the search behind it.
    const verify = doc.verify
      ? '<p class="verify"><a href="' + esc(doc.verify) + '" target="_blank" ' +
        'rel="noopener">See these papers on PubMed</a> — check they are the ' +
        'right person</p>' : '';
    return '<div class="doc"><h3>' + esc(doc.name) + '</h3>' +
           '<p class="meta">' + esc(doc.meta) + '</p>' + caution +
           '<ul>' + topics + '</ul>' + verify + '</div>';
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
    document.title = 'Which doctor is most published?';
    if (!ANNOUNCED) {{
      ANNOUNCED = true;
      // Deferred: scrolling in the same tick as the DOM updates gets the
      // smooth scroll cancelled by the layout shift.
      setTimeout(function () {{
        document.getElementById('note')
                .scrollIntoView({{behavior: 'auto', block: 'start'}});
      }}, 200);
    }}
    return;
  }}

  // While running, show progress; once finished, show only the finished
  // message. Progress text must never survive completion -- a leftover
  // "do not refresh" on a finished search looks exactly like a hang.
  const message = data.done ? data.note : (data.progress || data.note ||
      'Gathering data, do not refresh this page, this may take a few ' +
      'minutes :)');
  const noteClass = data.done ? 'note finished' : 'note';
  document.getElementById('note').innerHTML =
    message ? '<p class="' + noteClass + '">' +
              (data.done ? '&#10003; ' : '') + esc(message) + '</p>' : '';

  if (data.results && data.results.length) {{
    // Deliberately no condition banner: it named a single specialty even
    // when the search spanned several, which read as an error in an
    // otherwise correct report. The condition is still visible in the
    // results filename and the progress log.
    // A whole-specialty search has no disease to filter on, so a colleague
    // with the same surname and initial can be mistaken for the doctor.
    const warn = data.broad
      ? '<p class="samename"><b>Check these are the right people.</b> This ' +
        'page covers a whole specialty rather than one condition, so ' +
        'nothing narrows the search except the name and hospital. Doctors ' +
        'who share a surname and first initial with a colleague can have ' +
        'their research mixed together. Use the PubMed link under each ' +
        'person to confirm.</p>' : '';
    document.getElementById('results').innerHTML =
      '<h2>Most prominent researchers</h2>' + warn + render(data.results);
  }}

  if (data.done && data.thin && data.thin.length) {{
    // "Nothing found" is an answer, not an omission -- most excellent
    // clinicians do not publish. Showing it beats silence.
    const rows = data.thin.map(function (t) {{
      const count = t.papers === 1 ? '1 paper' : t.papers + ' papers';
      return '<li>' + esc(t.name) + ' — ' + esc(count) + '</li>';
    }}).join('');
    document.getElementById('thin').innerHTML =
      '<h2>Too little published research to profile</h2>' +
      '<p class="plain">These doctors were searched, but there is not ' +
      'enough published work to describe what they research. This says ' +
      'nothing about their skill as clinicians — most doctors do not ' +
      'publish.</p><ul class="plain">' + rows + '</ul>';
  }}

  if (data.done && data.doctors && data.doctors.length) {{
    let tail = '<h2>Doctors searched</h2><p class="plain">' +
               data.doctors.map(esc).join('<br>') + '</p>';
    if (data.csv) {{
      // A file path is only useful to whoever owns the machine. Offer the
      // spreadsheet as a download, and mention the path only when the
      // program is running on the reader's own computer.
      tail += '<p class="plain"><a href="/results?run=' +
              encodeURIComponent(RUN_ID) + '" download>Download the full ' +
              'spreadsheet</a> — every doctor, every subject, exact ' +
              'paper counts.</p>';
      if (data.local) {{
        tail += '<p class="plain">Saved on this computer at:<br>' +
                esc(data.csv) + '</p>';
      }}
    }}
    document.getElementById('searched').innerHTML = tail;
  }}

  if (data.done) {{
    document.getElementById('run').disabled = false;
    forgetRun();
    // Completion has to announce itself: the results render below the
    // fold, and a new user has no reason to know to scroll. The tab
    // title covers anyone who switched away during the wait.
    document.title = '✓ Finished — Which doctor is most published?';
    if (!ANNOUNCED) {{
      ANNOUNCED = true;
      // Deferred: scrolling in the same tick as the DOM updates gets the
      // scroll cancelled by the layout shift.
      setTimeout(function () {{
        // Land on the output itself. The status line is up by the button
        // now, so scrolling there would move the page away from results.
        const target = ['results', 'thin', 'note'].map(function (id) {{
          return document.getElementById(id);
        }}).find(function (el) {{ return el && el.innerHTML.trim(); }});
        if (target) target.scrollIntoView({{behavior: 'auto', block: 'start'}});
      }}, 200);
    }}
    return;
  }}
  document.title = 'Searching… — Which doctor is most published?';
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
        token=SESSION_TOKEN,
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
    if isinstance(exc, profiler.RateLimited):
        return str(exc), f"{type(exc).__name__}: {exc}"

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
    if "department_list" in reasons:
        problems.append(
            "That page lists departments and specialties, not individual "
            "doctors. Choose the specialty you care about, and paste the "
            "address of the page that then lists its doctors by name.")
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
    if "timeout" in reasons:
        problems.append(
            "That page took too long to load and could not be read. Very "
            "large directory pages often do. Try a page that lists one "
            "specialty rather than the whole hospital, or try again in a "
            "few minutes.")
    if "blocked" in reasons:
        blocked = next((w for _, v, w in rejected if v == "blocked" and w), "")
        problems.append(blocked or
                        "That hospital's website would not let this tool "
                        "read the page.")
    if "unreadable" in reasons:
        problems.append("That page could not be read. " + TRY_AGAIN)
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
        set_field(run_id, progress=f"Reading {len(urls)} directory page(s)...")
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
            set_field(run_id,
                      progress=f"Gathering data — searched {done} of "
                               f"{total} doctors. Do not refresh this "
                               f"page, this may take a few minutes :)")

        csv_path, result_rows, thin = profiler.run_auto(
            detected, researchers, out_dir, log=log, on_progress=progress)
        set_field(run_id, condition=detected, csv=csv_path or "",
                  thin=thin,
                  broad=auto_topics.is_discipline(detected))

        # Reshape the flat CSV rows into per-doctor blocks for the page.
        by_doctor = {}
        for row in result_rows:
            entry = by_doctor.setdefault(row["researcher"], {
                "name": row["researcher"],
                "meta": f"{row['their_focus_papers']} papers",
                "caution": "",
                "verify": "",
                "topics": [],
            })
            entry["verify"] = row.get("verify_on_pubmed", "")
            if row["their_focus_papers"] < profiler.MIN_PROFILE_PAPERS * 2:
                entry["caution"] = ("Based on few papers — read this as a "
                                    "hint, not a picture of their work.")
            entry["topics"].append({
                "topic": row["topic"],
                "stars": "*" * row["first_author_papers"]
                         if row["first_author_papers"] <= 5
                         else f"*x{row['first_author_papers']}",
            })
        # Finishing with nobody profiled is a legitimate outcome, not a
        # failure -- say so, or the empty page reads as a broken search.
        if not by_doctor:
            summary = (f"Finished. Searched {len(researchers)} "
                       f"{'doctor' if len(researchers) == 1 else 'doctors'}; "
                       f"none has enough published research to profile.")
            if thin:
                summary += " Their paper counts are listed below."
        else:
            summary = (f"Finished. {len(by_doctor)} of {len(researchers)} "
                       f"doctors had enough published research to profile.")

        with LOCK:
            if run_id in RUNS:
                RUNS[run_id]["results"] = list(by_doctor.values())
                RUNS[run_id]["note"] = summary
                RUNS[run_id]["progress"] = ""

    except BaseException as exc:                    # SystemExit included
        log(f"ERROR: {exc}")
        message, detail = friendly_error(exc)
        set_field(run_id, problems=[message], detail=detail)
    finally:
        with LOCK:
            if run_id in RUNS:
                RUNS[run_id]["done"] = True
                RUNS[run_id]["progress"] = ""     # never outlives the run


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
        elif path == "/results":
            run_id = parse_qs(query).get("run", [""])[0]
            with LOCK:
                run = RUNS.get(run_id)
                csv_path = run.get("csv", "") if run else ""
            # Only ever serve the file this run wrote, inside the output
            # folder: the run id is the authorisation, and the path is
            # never taken from the request.
            out_dir = os.path.realpath(os.path.join(HERE, "output"))
            real = os.path.realpath(csv_path) if csv_path else ""
            if not real or not real.startswith(out_dir + os.sep) \
                    or not os.path.isfile(real):
                self._send("no results file for that search", code=404)
                return
            try:
                with open(real, "rb") as fh:
                    body = fh.read()
            except OSError:
                self._send("results file could not be read", code=404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             "attachment; filename=\"" +
                             os.path.basename(real) + "\"")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/log":
            run_id = parse_qs(query).get("run", [""])[0]
            with LOCK:
                run = RUNS.get(run_id)
                payload = ({"done": run["done"],
                            "results": list(run["results"]),
                            "condition": run.get("condition", ""),
                            "note": run.get("note", ""),
                            "progress": run.get("progress", ""),
                            "problems": list(run.get("problems", [])),
                            "detail": run.get("detail", ""),
                            "thin": list(run.get("thin", [])),
                            "broad": run.get("broad", False),
                            "doctors": list(run.get("doctors", [])),
                            "csv": run.get("csv", ""),
                            "local": not os.environ.get("PORT")}
                           if run else
                           {"done": True, "results": [], "doctors": [],
                            "csv": "", "condition": "", "problems": [],
                            "detail": "", "thin": [], "progress": "", "broad": False,
                            "local": not os.environ.get("PORT"),
                            "note": "This run is no longer available. "
                                    "Press Run to start a new one."})
            self._send(json.dumps(payload), "application/json")
        else:
            self._send("not found", code=404)

    def _own_page(self):
        """Reject anything not addressed to this server by a name it knows.

        A browser will happily post a form from any site to localhost, and
        a stale DNS name can resolve here too, so both the token and the
        Host header are checked. When deployed, the public hostname is the
        legitimate one -- without it here, every search on a hosted copy
        answers 403 and the tool looks broken.
        """
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ALLOWED_HOSTS

    def do_POST(self):
        if self.path != "/run":
            self._send("not found", code=404)
            return
        if not self._own_page():
            self._send(json.dumps({"ok": False}), "application/json", 403)
            return
        length = min(int(self.headers.get("Content-Length", 0) or 0), 200_000)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))

        if not secrets.compare_digest(form.get("token", [""])[0],
                                      SESSION_TOKEN):
            # Not from our page: silently refuse rather than start work.
            self._send(json.dumps({"ok": False}), "application/json", 403)
            return

        raw_urls = form.get("urls", [""])[0].strip()
        email = (form.get("email", [""])[0].strip()
                 or os.environ.get("NCBI_EMAIL", ""))

        urls, problems = directory_scraper.validate_urls(raw_urls)
        if not valid_email(email):
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

    # A hosting service hands the port over in the environment. Its
    # presence is what distinguishes "deployed" from "run on my laptop":
    # deployed means listen on every interface and do not try to open a
    # browser on a machine that has no screen.
    port_from_host = os.environ.get("PORT")
    if port_from_host:
        server = ThreadingHTTPServer(("0.0.0.0", int(port_from_host)),
                                     Handler)
        print(f"Med Research Finder listening on port {port_from_host}")
        known = sorted(h for h in ALLOWED_HOSTS
                       if h not in ("127.0.0.1", "localhost",
                                    "[::1]", "::1"))
        if known:
            print(f"Answering to: {', '.join(known)}")
        else:
            print("WARNING: this service's public hostname is unknown, so "
                  "every search will be refused. Set PUBLIC_HOST to the "
                  "address people visit.")
    else:
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
