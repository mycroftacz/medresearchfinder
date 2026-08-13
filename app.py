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
import sys
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
                        "detail": "", "thin": [], "broad": False,
                        "pct": -1}
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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Which doctor is most published?</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,300;6..72,400;6..72,500&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  /* Editorial register, from the UI critique. Two hues: deep navy ground,
     warm off-white ink, with one sand accent reserved for orientation --
     eyebrow, secondary action, disclosure markers. Saturated red and
     yellow are kept for things that are genuinely wrong, which in a
     medical tool is the only honest use for them. */
  html {{ background: #1B1F4B; }}
  body {{
    margin: 0; padding: 0;
    background: #1B1F4B; color: #F2F0EA;
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 16px; line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }}
  .page {{ max-width: 1120px; margin: 0 auto; padding: 64px 40px 96px; }}

  /* --- masthead: proposition left, the task right ------------------- */
  .masthead {{
    display: grid; grid-template-columns: 1.05fr 1fr; gap: 60px;
    align-items: start; padding-bottom: 44px;
    border-bottom: 1px solid rgba(255,255,255,0.12);
  }}
  .eyebrow {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: 11px; letter-spacing: .16em; text-transform: uppercase;
    color: #E8B25C; margin: 0 0 20px;
  }}
  h1 {{
    font-family: Newsreader, Georgia, serif; font-weight: 400;
    font-size: 54px; line-height: 1.03; letter-spacing: -0.02em;
    color: #F4EFE2; margin: 0 0 22px; max-width: 20ch;
    text-wrap: balance;
  }}
  .lede {{ margin: 0 0 14px; font-size: 17px; line-height: 1.62;
           color: #A9AECB; max-width: 62ch; text-wrap: pretty; }}
  .lede strong {{ color: #F2F0EA; font-weight: 500; }}

  /* --- controls ----------------------------------------------------- */
  .field {{ margin-bottom: 26px; }}
  .fieldhead {{
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 12px; margin-bottom: 8px;
  }}
  label {{ font-size: 13px; letter-spacing: .02em; color: #F2F0EA; }}
  .counter {{ font-family: "IBM Plex Mono", ui-monospace, monospace;
              font-size: 11px; color: #7C82A8; }}
  textarea, input[type=email] {{
    width: 100%; box-sizing: border-box; padding: 12px 13px;
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.18); border-radius: 0;
    color: #F2F0EA; font-size: 15px;
    font-family: "IBM Plex Mono", ui-monospace, monospace;
  }}
  textarea {{ height: 104px; resize: vertical; line-height: 1.5; }}
  textarea::placeholder, input::placeholder {{ color: #6B709A; }}
  textarea:focus, input[type=email]:focus {{
    outline: none; border-color: #E8B25C;
    background: rgba(255,255,255,0.09);
  }}
  .help {{ margin: 8px 0 0; font-size: 12.5px; line-height: 1.5;
           color: #7C82A8; }}

  /* Primary action: the one solid object on the page. */
  #run {{
    width: 100%; padding: 14px 16px; border: 0; border-radius: 0;
    background: #F2F0EA; color: #1B1F4B; cursor: pointer;
    font-family: inherit; font-size: 15px; letter-spacing: .01em;
  }}
  #run:hover {{ background: #FFFFFF; }}
  #run:disabled {{ background: #6E7196; color: #D6D9E8; cursor: default; }}
  .act {{ border-top: 1px solid rgba(255,255,255,0.12); padding-top: 18px; }}
  .act-note {{ margin: 10px 0 0; font-size: 12px; color: #A9AECB;
               text-align: center; }}

  /* Secondary action: outlined, quiet, in the accent. */
  .example-wrap {{ margin-top: 26px; }}
  #example {{
    display: inline-flex; align-items: center; gap: 10px;
    padding: 13px 20px; background: transparent;
    border: 1px solid #E8B25C; border-radius: 0; color: #E8B25C;
    font-family: inherit; font-size: 15px; letter-spacing: .01em;
    cursor: pointer;
  }}
  #example:hover {{ background: rgba(232,178,92,0.10); }}
  #example:disabled {{ border-color: #6E7196; color: #8A8FB5;
                       cursor: default; background: transparent; }}
  .example-note {{ margin: 10px 0 0; font-size: 12px; color: #7C82A8; }}
  button:focus-visible, a:focus-visible, summary:focus-visible {{
    outline: 2px solid #E8B25C; outline-offset: 2px;
  }}

  /* --- progress and completion -------------------------------------- */
  .note {{
    display: flex; align-items: center; gap: .8rem;
    margin: 28px 0 0; padding: 16px 18px;
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.18); border-left: 3px solid #E8B25C;
    color: #F2F0EA; font-size: 15px;
  }}
  .note.working::before {{
    content: ""; flex: none; width: .6rem; height: .6rem;
    border-radius: 50%; background: #E8B25C;
    animation: pulse 1.4s ease-in-out infinite;
  }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; transform: scale(1); }}
    50%      {{ opacity: .3; transform: scale(.7); }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .note.working::before {{ animation: none; }}
  }}
  .note.finished {{ border-left-color: #8FD3B0; }}
  .notebody {{ flex: 1; min-width: 0; }}
  .track {{ height: 4px; background: rgba(255,255,255,0.14);
            margin-top: .6rem; overflow: hidden; }}
  .fill {{ height: 100%; background: #E8B25C; transition: width .5s ease; }}
  .problem {{
    margin: 28px 0 0; padding: 16px 18px;
    background: rgba(169,59,38,0.14);
    border: 1px solid rgba(232,140,120,0.45); border-left: 3px solid #E08A72;
    color: #F6DCD4; font-size: 15px;
  }}
  .problem ul {{ margin: .5rem 0 0; padding-left: 1.1rem; }}
  .problem li {{ margin: .25rem 0; }}
  .problem summary {{ cursor: pointer; color: #E8B25C; font-size: 12.5px; }}
  .problem pre {{ white-space: pre-wrap; font-size: 12px; color: #C9CDE4;
                  background: rgba(0,0,0,0.22); padding: .6rem;
                  margin-top: .5rem; max-height: 12rem; overflow-y: auto;
                  font-family: "IBM Plex Mono", ui-monospace, monospace; }}

  /* --- results ------------------------------------------------------- */
  h2 {{
    font-family: Newsreader, Georgia, serif; font-weight: 400;
    font-size: 27px; letter-spacing: -0.01em; color: #F4EFE2;
    margin: 56px 0 6px; padding-bottom: 12px;
    border-bottom: 1px solid rgba(255,255,255,0.18);
  }}
  .samename {{
    margin: 14px 0 0; padding: 14px 16px; font-size: 13.5px;
    background: rgba(255,255,255,0.05);
    border-left: 3px solid #E8B25C; color: #A9AECB;
  }}
  .samename b {{ color: #F2F0EA; font-weight: 500; }}
  .doc {{ padding: 26px 0; border-bottom: 1px solid rgba(255,255,255,0.12); }}
  .doc h3 {{
    font-family: Newsreader, Georgia, serif; font-weight: 400;
    font-size: 22px; color: #F4EFE2; margin: 0 0 2px;
  }}
  .doc .meta {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: 11px; color: #7C82A8; margin: 0 0 14px;
    letter-spacing: .04em;
  }}
  .doc ul {{ margin: 0; padding: 0; list-style: none;
             display: flex; flex-direction: column; gap: 5px; }}
  .doc li {{ display: grid; grid-template-columns: 68px 1fr; gap: 14px;
             align-items: baseline; font-size: 15px; color: #F2F0EA; }}
  .stars {{ font-family: "IBM Plex Mono", ui-monospace, monospace;
            font-size: 13px; color: #E8B25C; letter-spacing: 1px; }}
  .caution-inline {{ font-size: 12.5px; color: #E8B25C; margin: 0 0 10px; }}
  .verify {{ font-size: 12.5px; color: #7C82A8; margin: 14px 0 0; }}
  .verify a {{ color: #A9AECB; text-decoration: underline;
               text-underline-offset: 3px; }}
  .verify a:hover {{ color: #E8B25C; }}
  .plain {{ font-size: 14px; color: #A9AECB; line-height: 1.75;
            word-break: break-word; }}
  #thin ul {{ margin: .4rem 0 0; padding-left: 1.1rem; }}
  #thin li {{ font-size: 14px; color: #A9AECB; }}
  #searched a, #results a {{ color: #E8B25C; }}

  /* --- documentation, collapsed by default -------------------------- */
  .docs {{ margin-top: 64px; border-top: 1px solid rgba(255,255,255,0.18); }}
  details {{ border-bottom: 1px solid rgba(255,255,255,0.12); }}
  details > summary {{
    list-style: none; cursor: pointer; padding: 20px 0;
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 20px; font-family: Newsreader, Georgia, serif; font-size: 20px;
    color: #F4EFE2;
  }}
  details > summary::-webkit-details-marker {{ display: none; }}
  details > summary::after {{ content: "+"; color: #E8B25C;
                              font-family: "IBM Plex Mono", monospace;
                              font-size: 17px; }}
  details[open] > summary::after {{ content: "\2212"; }}
  .disclosure {{ padding: 0 0 24px; max-width: 68ch; }}
  .disclosure p {{ margin: 0 0 12px; font-size: 15px; line-height: 1.62;
                   color: #A9AECB; }}
  .disclosure p strong, .disclosure b {{ color: #F2F0EA; font-weight: 500; }}
  .disclosure ul {{ margin: 10px 0; padding-left: 1.1rem;
                    font-size: 15px; color: #A9AECB; }}
  .disclosure li {{ margin: .3rem 0; }}
  .disclosure code {{ font-family: "IBM Plex Mono", ui-monospace, monospace;
                      font-size: 13px; color: #E8B25C;
                      word-break: break-all; }}
  .rule-label {{
    font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 11px;
    letter-spacing: .1em; text-transform: uppercase; color: #E8B25C;
  }}
  .caveat {{
    margin: 44px 0 0; font-family: Newsreader, Georgia, serif;
    font-size: 19px; line-height: 1.55; color: #A9AECB; max-width: 62ch;
  }}

  @media (max-width: 900px) {{
    .page {{ padding: 40px 22px 72px; }}
    .masthead {{ grid-template-columns: 1fr; gap: 40px; }}
    h1 {{ font-size: 36px; }}
    h2 {{ font-size: 23px; margin-top: 40px; }}
    /* 16px keeps iOS from zooming the page when a field is tapped. */
    textarea, input[type=email] {{ font-size: 16px; }}
    #example {{ width: 100%; justify-content: center; }}
    .doc li {{ grid-template-columns: 1fr; gap: 0; }}
    .stars {{ display: block; }}
    .caveat {{ font-size: 17px; }}
  }}
</style>
</head>
<body>
<div class="page">

  <header class="masthead">
    <div class="pitch">
      <p class="eyebrow">PubMed &middot; hospital directories</p>
      <h1>Which doctor is most widely published on your specific problem?</h1>
      <p class="lede">Paste the web addresses of hospital &ldquo;find a
        doctor&rdquo; pages. This tool reads every doctor listed on them,
        looks each one up in <strong>PubMed</strong>, the national database
        of medical research, and shows the specific subjects each of them
        publishes on &mdash; particular drugs, procedures and clinical
        problems.</p>
      <p class="lede">It works out on its own which condition the directory
        covers, so there is nothing to choose or configure.</p>
      <div class="example-wrap">
        <button id="example" onclick="runExample()">Click to see an example
          search <span aria-hidden="true">&rarr;</span></button>
        <p class="example-note">An inflammatory bowel disease centre
          &middot; sixteen doctors</p>
      </div>
    </div>

    <div class="task">
      <div class="field">
        <div class="fieldhead">
          <label for="urls">Specialist pages</label>
          <span class="counter">up to {max_urls}</span>
        </div>
        <textarea id="urls" placeholder="https://hospital.org/find-a-doctor/pulmonology; https://hospital.org/find-a-doctor/pulmonology?page=2"></textarea>
        <p class="help">Separate several with semicolons. A directory split
          across pages needs a link for each page &mdash; two pages of
          pulmonologists means two links.</p>
      </div>

      <div class="field" id="emailbox" style="{email_display}">
        <div class="fieldhead">
          <label for="email">Your email</label>
        </div>
        <input type="email" id="email" value="{email}" required
               placeholder="you@example.com">
        <p class="help">The research database requires a contact address
          with every search. It is used for nothing else.</p>
      </div>

      <div class="act">
        <button id="run" onclick="start()">Search</button>
        <p class="act-note">Keep this tab open until the results appear</p>
      </div>
    </div>
  </header>

  <div id="note"></div>
  <div id="results"></div>
  <div id="thin"></div>
  <div id="searched"></div>

  <section class="docs">
    <details>
      <summary>What to paste, and what won&rsquo;t work</summary>
      <div class="disclosure">
        <p><span class="rule-label">Works</span><br>
          A page from a <strong>research hospital or medical school</strong>
          that lists doctors by name &mdash; a department&rsquo;s team page,
          or a condition centre&rsquo;s list of specialists. These are the
          places where doctors both treat patients and publish research,
          which is what this tool measures.</p>
        <p><code>https://nyulangone.org/locations/inflammatory-bowel-disease-center</code></p>
        <p><span class="rule-label">Won&rsquo;t</span><br>
          A hospital&rsquo;s front page or its list of departments. Those
          name specialties, not people &mdash; click through to the
          specialty you care about first, then paste that page.</p>
        <p><span class="rule-label">Not yet</span><br>
          Booking sites. Zocdoc, Healthgrades, Vitals and similar services
          are not supported: their pages are built for appointments and are
          too large for this tool to read, and their listings identify a
          practice rather than a hospital, which is what makes a research
          search possible.</p>
        <p><span class="rule-label">Thin results</span><br>
          Small local practices will run, but most community doctors do not
          publish research at all, so the usual answer is &ldquo;too little
          published research to profile&rdquo;. That is a real answer, not a
          fault &mdash; but an excellent local doctor will look identical to
          a poor one here.</p>
      </div>
    </details>

    <details>
      <summary>How to read the results</summary>
      <div class="disclosure">
        <p>Each doctor is listed with the subjects they publish on,
          <strong>most distinctive first</strong>.</p>
        <p>The list is not simply ordered by who publishes most. A subject
          that nearly everyone in the group writes about cannot tell you who
          to see, so it is pushed down; a subject that sets one doctor apart
          from their colleagues is pushed up.</p>
        <p>Say you search an epilepsy centre. Every doctor there publishes on
          seizures and on brain scans, so those subjects say nothing about
          any one of them. But if one doctor has written nine papers on a
          particular drug and nobody else has written any, that rises to the
          top of their list &mdash; because it is the thing that makes them
          different from the colleague in the next office.</p>
        <p><strong>Asterisks mark first-authored papers.</strong> One per
          paper on that subject where the doctor was the <em>first</em>
          author.</p>
        <ul>
          <li><span class="stars">&lowast;&lowast;&lowast;</span> &mdash;
            three first-authored papers on that subject.</li>
          <li><span class="stars">&lowast;x12</span> &mdash; twelve of them.
            Past five the count replaces the marks, so a number means
            <strong>more</strong>, not less.</li>
          <li>No marks &mdash; the doctor contributed without being first
            author, which is common on large multi-centre studies.</li>
        </ul>
        <p>First authorship usually means the work was that person&rsquo;s to
          drive rather than one name among many. A doctor with 30 papers and
          many first-authored ones is often running their own programme; one
          with 300 and few may be a senior collaborator on other
          people&rsquo;s studies. Both are accomplished &mdash; they are
          different things.</p>
      </div>
    </details>
  </section>

  <p class="caveat">Publishing is not the same as clinical skill. A doctor
    with no papers at all may be the better choice for your care &mdash;
    this shows what someone researches, which is a different question.</p>

</div>

<script>
const TOKEN = "{token}";
let RUN_ID = null;
let ANNOUNCED = false;   // scroll to the results only once per run
let BUSY = false;        // a second click must not start a second search

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

function showProblems(list) {{
  document.getElementById('note').innerHTML =
    '<div class="problem"><b>That didn\\'t work.</b><ul>' +
    list.map(function (p) {{ return '<li>' + esc(p) + '</li>'; }}).join('') +
    '</ul></div>';
}}

const EXAMPLE_URL =
  'https://nyulangone.org/locations/inflammatory-bowel-disease-center';

async function runExample() {{
  if (BUSY) return;
  document.getElementById('urls').value = EXAMPLE_URL;
  const emailEl = document.getElementById('email');
  const emailBox = document.getElementById('emailbox');
  const emailAsked = emailEl && emailBox &&
                     emailBox.style.display !== 'none';

  // The research database insists on a contact address, so an example
  // search cannot skip it. Fill the address in, then say exactly what is
  // still missing rather than failing silently.
  if (emailAsked &&
      !/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(emailEl.value.trim())) {{
    showProblems(['The example page is filled in above. Add your email ' +
                  'address and the search will start.']);
    emailEl.scrollIntoView({{behavior: 'auto', block: 'center'}});
    emailEl.focus();
    return;
  }}

  await start();
  // Take them to the progress line; when it finishes, poll() carries them
  // on to the results.
  document.getElementById('note')
          .scrollIntoView({{behavior: 'auto', block: 'center'}});
}}

async function start() {{
  if (BUSY) return;
  // Check here before anything is sent. The page used to paint
  // "Gathering data..." the instant Run was pressed, so a search refused
  // for a missing email looked like a search that had started and then
  // broken.
  const urls = document.getElementById('urls').value.trim();
  const emailEl = document.getElementById('email');
  const emailBox = document.getElementById('emailbox');
  const emailAsked = emailEl && emailBox &&
                     emailBox.style.display !== 'none';
  const email = emailEl ? emailEl.value.trim() : '';

  const problems = [];
  if (!urls) {{
    problems.push('Please paste at least one directory web address.');
  }}
  if (emailAsked && !/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(email)) {{
    problems.push('Please enter your email address. The medical research ' +
                  'database requires one with every search.');
  }}
  if (problems.length) {{
    showProblems(problems);
    if (emailAsked && !email) emailEl.focus();
    else document.getElementById('urls').focus();
    return;
  }}

  const body = new URLSearchParams({{urls: urls, email: email,
                                    token: TOKEN}});
  BUSY = true;
  document.getElementById('run').disabled = true;
  document.getElementById('example').disabled = true;
  document.getElementById('results').innerHTML = '';
  document.getElementById('thin').innerHTML = '';
  document.getElementById('searched').innerHTML = '';
  ANNOUNCED = false;

  const resp = await fetch('/run', {{method: 'POST', body}});
  const info = await resp.json();
  if (!info.ok) {{
    // The server refused it; poll once so its reason is displayed
    // instead of a progress note for a search that never began.
    BUSY = false;
    document.getElementById('run').disabled = false;
    document.getElementById('example').disabled = false;
    if (info.run) {{ RUN_ID = info.run; poll(); }}
    else showProblems(['That search could not be started. Please reload ' +
                       'the page and try again.']);
    return;
  }}
  // Only now is a search genuinely under way.
  document.getElementById('note').innerHTML =
    '<div class="note working"><div class="notebody">Starting the ' +
    'search. Please keep this page open.<div class="track">' +
    '<div class="fill" style="width:2%"></div></div></div></div>';
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
    BUSY = false;
    document.getElementById('run').disabled = false;
    document.getElementById('example').disabled = false;
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
  if (message) {{
    const running = !data.done;
    const cls = data.done ? 'note finished' : 'note working';
    // Show how far along it is, not just that it is going. Eight minutes
    // of an unchanging sentence is indistinguishable from a stall.
    const bar = (running && data.pct >= 0)
      ? '<div class="track"><div class="fill" style="width:' +
        Math.max(2, Math.min(100, data.pct)) + '%"></div></div>' : '';
    document.getElementById('note').innerHTML =
      '<div class="' + cls + '"><div class="notebody">' +
      (data.done ? '&#10003; ' : '') + esc(message) + bar +
      '</div></div>';
  }} else {{
    document.getElementById('note').innerHTML = '';
  }}

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
    BUSY = false;
    document.getElementById('run').disabled = false;
    document.getElementById('example').disabled = false;
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
    if isinstance(exc, (profiler.RateLimited, profiler.ApiKeyRejected)):
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
        set_field(run_id,
                  progress=f"Reading {len(urls)} directory page(s)...",
                  pct=0)
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
                      progress=f"Searching PubMed — {done} of {total} "
                               f"doctors done. Please keep this page open.",
                      pct=int(round(100 * done / max(total, 1))))

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
                            "pct": run.get("pct", -1),
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
                            "pct": -1,
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
    # Python block-buffers stdout when it is a pipe rather than a
    # terminal, which is exactly what a hosting service gives it. Startup
    # diagnostics then sit in a buffer a server never flushes, because it
    # never exits -- so the notes below were invisible in the one place
    # they were written for.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except AttributeError:                     # very old Python
        pass

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
        if not os.environ.get("NCBI_EMAIL"):
            print("NOTE: NCBI_EMAIL is not set, so every visitor is asked "
                  "for an email address before they can search. Set it to "
                  "your own address and the question disappears.")
        if not os.environ.get("NCBI_API_KEY"):
            print("NOTE: NCBI_API_KEY is not set. Searches work without "
                  "one but run about three times slower, because doctors "
                  "must be looked up one at a time.")
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
