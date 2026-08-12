#!/usr/bin/env python3
"""
PubMed Topic Profiler -- window launcher
========================================
Run:  python app.py

Presents a window where the user pastes hospital-directory URLs
(semicolon-separated, one URL per directory PAGE), picks a disease config,
and clicks Run. The app then:

  1. scrapes each directory page with Firecrawl and builds the roster CSV
  2. profiles every extracted physician against PubMed
  3. drops the report + CSV in output/ and shows the report

Needs FIRECRAWL_API_KEY and NCBI email (both can live in .env).
"""

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

import directory_scraper
import profiler

HERE = os.path.dirname(os.path.abspath(__file__))

INSTRUCTIONS = (
    "Paste the URL of every hospital-directory page you want to search, "
    "separated by semicolons ( ; ).\n\n"
    "Reminder: provide a separate link for EVERY PAGE of a directory. "
    "If a directory lists its pulmonologists across two pages, paste two "
    "URLs -- one for each page."
)

EXAMPLE = ("https://hospital.org/find-a-doctor/gastroenterology; "
           "https://hospital.org/find-a-doctor/gastroenterology?page=2")


class App:
    def __init__(self, root):
        self.root = root
        root.title("PubMed Topic Profiler")
        root.minsize(720, 560)

        directory_scraper.load_env()

        body = ttk.Frame(root, padding=12)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Step 1 -- Directory pages", font=("", 13, "bold")
                  ).pack(anchor="w")
        ttk.Label(body, text=INSTRUCTIONS, wraplength=680, justify="left"
                  ).pack(anchor="w", pady=(4, 6))

        self.urls = tk.Text(body, height=5, wrap="word")
        self.urls.pack(fill="x")
        self.urls.insert("1.0", "")
        ttk.Label(body, text=f"e.g.  {EXAMPLE}", foreground="gray",
                  wraplength=680, justify="left").pack(anchor="w", pady=(2, 10))

        ttk.Label(body, text="Step 2 -- Disease / condition",
                  font=("", 13, "bold")).pack(anchor="w")
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(4, 10))
        self.config_path = tk.StringVar(
            value=os.path.join(HERE, "examples", "ulcerative_colitis.json"))
        ttk.Entry(row, textvariable=self.config_path).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=self.pick_config).pack(
            side="left", padx=(6, 0))

        ttk.Label(body,
                  text="The config JSON names the condition and the topics "
                       "to track. Copy an example from examples/ for a new "
                       "disease.",
                  foreground="gray", wraplength=680, justify="left"
                  ).pack(anchor="w", pady=(0, 10))

        ttk.Label(body, text="Step 3 -- Your email (required by PubMed/NCBI)",
                  font=("", 13, "bold")).pack(anchor="w")
        self.email = tk.StringVar(value=os.environ.get("NCBI_EMAIL", ""))
        ttk.Entry(body, textvariable=self.email, width=40).pack(
            anchor="w", pady=(4, 12))

        self.run_button = ttk.Button(body, text="Run", command=self.start)
        self.run_button.pack(anchor="w")

        ttk.Label(body, text="Progress", font=("", 13, "bold")).pack(
            anchor="w", pady=(12, 2))
        self.log_box = tk.Text(body, height=12, state="disabled",
                               background="#111", foreground="#ddd")
        self.log_box.pack(fill="both", expand=True)

        self.log_queue = queue.Queue()
        self.root.after(150, self.drain_log)

    # ---- helpers ---------------------------------------------------------

    def pick_config(self):
        path = filedialog.askopenfilename(
            initialdir=os.path.join(HERE, "examples"),
            filetypes=[("JSON config", "*.json")])
        if path:
            self.config_path.set(path)

    def log(self, message):
        self.log_queue.put(str(message))

    def drain_log(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", line + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self.drain_log)

    # ---- pipeline --------------------------------------------------------

    def start(self):
        raw_urls = self.urls.get("1.0", "end").strip()
        email = self.email.get().strip()
        config_path = self.config_path.get().strip()

        if not raw_urls:
            self.log("Please paste at least one directory URL.")
            return
        if not email or "@" not in email:
            self.log("Please enter a valid email -- NCBI requires one.")
            return
        if not os.path.exists(config_path):
            self.log(f"Config not found: {config_path}")
            return

        self.run_button.configure(state="disabled")
        thread = threading.Thread(
            target=self.pipeline, args=(raw_urls, config_path, email),
            daemon=True)
        thread.start()

    def pipeline(self, raw_urls, config_path, email):
        try:
            import builtins
            from Bio import Entrez
            Entrez.email = email
            if os.environ.get("NCBI_API_KEY"):
                Entrez.api_key = os.environ["NCBI_API_KEY"]

            api_key = directory_scraper.get_api_key()
            urls = directory_scraper.split_urls(raw_urls)
            self.log(f"Step 1: scraping {len(urls)} directory page(s) ...")
            rows = directory_scraper.build_roster(urls, api_key, log=self.log)
            if not rows:
                self.log("No providers were extracted from those pages. "
                         "If the directory loads its list via a search box, "
                         "link directly to a results page.")
                return

            out_dir = os.path.join(HERE, "output")
            os.makedirs(out_dir, exist_ok=True)
            roster_path = os.path.join(out_dir, "researchers_from_urls.csv")
            directory_scraper.write_roster(rows, roster_path)
            self.log(f"Roster written: {roster_path}  ({len(rows)} people)")

            self.log("\nStep 2: profiling everyone against PubMed "
                     "(this is the slow part) ...")
            cfg = profiler.load_config(config_path)
            researchers = profiler.load_researchers(roster_path)

            # Route profiler's print() output into the window.
            real_print = builtins.print
            builtins.print = lambda *a, **k: self.log(
                " ".join(str(x) for x in a))
            try:
                csv_path = profiler.run(cfg, researchers, out_dir)
            finally:
                builtins.print = real_print

            self.log(f"\nDone. Report above; CSV: {csv_path}")
        except SystemExit as exc:
            self.log(f"STOPPED: {exc}")
        except Exception as exc:
            self.log(f"ERROR: {exc}")
        finally:
            self.run_button.configure(state="normal")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
