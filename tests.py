#!/usr/bin/env python3
"""
Regression tests
================
Run before every change:  python tests.py

Every case here is a bug that actually happened. The tool is a stack of
interacting heuristics -- word lists, thresholds, ordering rules -- and
fixing one has repeatedly broken another: tolerating "url1, url2" broke
addresses containing commas; treating "director" as a non-clinical role
rejected a real hospital directory because one doctor runs the centre;
keeping unfinished runs in memory to stop them being evicted mid-search
made them accumulate forever.

None of that was caught by reasoning about the change. It was caught by
re-running the old cases, which is what this file makes cheap.

No network: everything here runs offline in about a second.
"""

import sys
from collections import Counter

import app
import auto_topics as at
import directory_scraper as ds
import profiler

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append(f"{name}: got {got!r}, wanted {want!r}")


def section(title):
    print(f"\n{title}")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
def test_urls():
    section("URL validation")
    accept = [
        ("plain directory",
         "https://nyulangone.org/locations/inflammatory-bowel-disease-center"),
        # Regression: commas are legal inside a URL. Splitting on every one
        # tore this address into three fragments.
        ("commas in query",
         "https://nyulangone.org/doctors/x?types=demographic,specialist,condition"),
        ("two urls, semicolon", "https://a-hosp.org/x; https://b-hosp.org/y"),
        ("two urls, comma-space", "https://a-hosp.org/x, https://b-hosp.org/y"),
        # Regression: autocorrect turns a double space into ". ", which
        # used to weld two links into one unparseable string.
        ("autocorrect period", "https://a-hosp.org/x;.  https://b-hosp.org/y"),
        ("newline separated", "https://a-hosp.org/x\nhttps://a-hosp.org/y"),
        ("no scheme", "nyulangone.org/doctors"),
        ("directory subdomain root", "https://doctors.mountsinai.org"),
        ("exactly the URL cap",
         ";".join(f"https://h{i}-hosp.org/d" for i in range(ds.MAX_URLS))),
    ]
    for label, raw in accept:
        good, problems = ds.validate_urls(raw)
        check(f"accept {label}", bool(good) and not problems, True)

    reject = [
        ("empty", ""), ("prose", "please find me a good doctor"),
        ("prose with dots", "I want Dr. Smith at N.Y.U. please"),
        ("1MB of text", "a" * 1_000_000),
        ("over the URL cap",
         ";".join(f"https://h{i}-hosp.org/d" for i in range(ds.MAX_URLS + 1))),
        ("hospital home page", "https://nyulangone.org"),
        ("home page with slash", "https://nyulangone.org/"),
        ("www home page", "https://www.nyulangone.org"),
        ("youtube", "https://www.youtube.com/watch?v=x"),
        ("wikipedia", "https://en.wikipedia.org/wiki/List_of_people"),
        ("file scheme", "file:///etc/passwd"),
        ("javascript scheme", "javascript:alert(1)"),
        ("path traversal", "../../../../etc/passwd"),
        ("localhost", "http://127.0.0.1:8080/admin"),
        ("private network", "http://192.168.1.1/config"),
        ("cloud metadata", "http://169.254.169.254/latest/meta-data/"),
        ("embedded credentials", "https://user:pass@hosp.org/docs"),
        ("null byte", "https://hosp.org/\x00doctors"),
        ("bidi override", "https://hosp.org/‮gnp.exe"),
        ("zero width", "https://hosp​.org/docs"),
        ("5000 characters", "https://hosp.org/" + "a" * 5000),
        ("binary/PNG", "\x89PNG\r\n\x1a\n\x00"),
        ("script tag", "<script>alert(1)</script>"),
    ]
    for label, raw in reject:
        good, problems = ds.validate_urls(raw)
        check(f"reject {label}", bool(good) and not problems, False)
    print(f"  {len(accept)} accepted, {len(reject)} rejected")


# ---------------------------------------------------------------------------
# Is this a directory of clinicians?
# ---------------------------------------------------------------------------
def test_clinical():
    section("Clinician classification")
    cases = [
        # (organization, profession, credentials, should_accept, label)
        ("hospital", "physicians", "MD", True, "physicians"),
        # Regression: one doctor's title is "Center Director", and treating
        # director as non-clinical rejected the whole real directory.
        ("hospital", "doctors", "MD", True, "directory with a Center Director"),
        ("academic medical center", "gastroenterologists", "MD", True,
         "specialists"),
        ("hospital", "resident physicians", "MD", True, "residents are MDs"),
        ("hospital", "nurses", "RN", True, "nurses"),
        ("dental practice", "dentists", "DDS", True, "dentists"),
        ("pharmacy", "pharmacists", "PharmD", True, "pharmacists"),
        ("counseling practice", "therapists", "LCSW", True, "therapists"),
        ("law firm", "attorneys", "JD", False, "lawyers"),
        ("accounting firm", "accountants", "CPA", False, "accountants"),
        ("university", "students", "", False, "students"),
        ("medical school", "medical students", "", False, "medical students"),
        ("hospital", "executives", "MBA", False, "hospital executives"),
        ("hospital", "chaplains", "MDiv", False, "chaplains"),
        ("hospital", "board of trustees", "", False, "trustees"),
        ("animal hospital", "veterinarians", "DVM", False, "veterinarians"),
        ("football club", "players", "", False, "athletes"),
        ("software company", "engineers", "", False, "engineers"),
    ]
    for org, profession, cred, expect, label in cases:
        people = [{"name": "A B", "credentials": cred, "specialty": ""}] * 6
        verdict, _ = ds.looks_clinical(org, profession, people)
        check(f"clinical {label}", not verdict, expect)

    # The exact shape of the bug that rejected a real IBD directory: the
    # page describes doctors, and one of them runs the centre. A job title
    # must never disqualify the colleagues listed beside it, so this is
    # checked on the real mixture rather than a uniform list.
    mixed = [
        {"name": "D H", "credentials": "MD", "specialty": "Center Director"},
        {"name": "J A", "credentials": "MD", "specialty": "Gastroenterologist"},
        {"name": "S C", "credentials": "MD", "specialty": "Chief of Service"},
        {"name": "A F", "credentials": "MD", "specialty": "Program Director"},
    ]
    verdict, why = ds.looks_clinical("hospital", "doctors", mixed)
    check("leadership titles do not disqualify a real directory",
          (not verdict, why), (True, ""))

    # And the inverse: a page whose people are administrators is refused
    # even though it sits on a hospital's own website.
    admins = [{"name": "A B", "credentials": "MBA",
               "specialty": "Chief Financial Officer"}] * 5
    verdict, _ = ds.looks_clinical("hospital", "administrators", admins)
    check("administrators still refused", bool(verdict), True)
    print(f"  {len(cases)} directory types plus mixed-title cases")


# ---------------------------------------------------------------------------
# Is this string a person?
# ---------------------------------------------------------------------------
def test_person_names():
    section("Person-name filter")
    people = [
        "Jordan E. Axelrad", "Bo Shen", "Ng Wei Ming", "Anne-Marie O'Brien",
        "Sinéad O'Connor", "Jürgen Müller", "Tina Q. He",
        "Beth A. Leeman-Markowski", "Elie S. Al Kazzi", "Le-Chu Su",
        # Regression: a nav-word blocklist rejected these real surnames.
        "Page Brown", "Reed Booker", "Grace Church", "Ann Call",
        "Frank List", "Mary Main",
    ]
    for name in people:
        check(f"person {name}", ds.looks_like_person_name(name), True)

    not_people = [
        "Cardiology", "Pediatric Cardiology", "Ear, Nose & Throat",
        "Cancer Center", "Department of Medicine", "Orthopedic Surgery",
        "Our Doctors", "Find a Doctor", "Patient Portal", "Pain Management",
        "Contact Us", "Read More", "View Profile", "Book Online",
        "Make an Appointment", "Insurance & Billing", "Back to Top",
        "Show More", "Next Page", "Select Location",
    ]
    for label in not_people:
        check(f"not a person {label}", ds.looks_like_person_name(label), False)
    print(f"  {len(people)} names, {len(not_people)} labels")


# ---------------------------------------------------------------------------
# Names into PubMed author form
# ---------------------------------------------------------------------------
def test_author_form():
    section("Author names")
    cases = [
        ("Jordan E. Axelrad", "Axelrad JE"),
        ("Dr. Edward V. Loftus Jr.", "Loftus EV"),
        ("Elie S. Al Kazzi", "Al Kazzi ES"),
        ("Joyce O'Shaughnessy", "O'Shaughnessy J"),
        ("Adam Faye", "Faye A"),
        # Middle initials must survive: three different Capos at one
        # hospital are told apart by nothing else.
        ("Joseph A. Capo", "Capo JA"),
        ("Joseph M. Capo", "Capo JM"),
    ]
    for raw, want in cases:
        check(f"author {raw}", ds.to_author_form(raw), want)

    # The search itself drops back to the first initial: "Hudesman DP"
    # finds 11 of his 64 papers because most are indexed without it.
    check("search name", profiler.search_name("Hudesman DP"), "Hudesman D")
    check("initials D vs DP", profiler._initials_compatible("D", "DP"), True)
    check("initials JT vs JA", profiler._initials_compatible("JT", "JA"), False)
    check("initials blank", profiler._initials_compatible("", "SM"), True)
    print(f"  {len(cases)} names plus initial matching")


# ---------------------------------------------------------------------------
# Injection and hostile data
# ---------------------------------------------------------------------------
def test_injection():
    section("Injection defences")
    # .env is a line-based file; a newline in the address could add
    # settings of its own, including a replacement API key.
    check("email with newline",
          app.valid_email("a@b.com\nFIRECRAWL_API_KEY=stolen"), False)
    check("email with space", app.valid_email("a b@c.com"), False)
    check("real email", app.valid_email("someone@example.org"), True)
    check("real email with plus",
          app.valid_email("x.y+z@sub.domain.org"), True)

    query = profiler.build_query("Smith J[Author] OR 1=1", "NYU", 2018)
    check("query injection stripped", "OR 1=1" in query, False)
    check("query still well formed", "[Author]" in query, True)
    check("empty author gives no query",
          profiler.build_query("", "NYU", 2018), "")

    # Spreadsheets execute cells beginning with these characters.
    for prefix in ("=", "+", "-", "@"):
        check(f"csv formula {prefix}",
              profiler._defuse_formula(prefix + "cmd").startswith("'"), True)
    check("csv normal text untouched",
          profiler._defuse_formula("Axelrad"), "Axelrad")

    # Invisible characters pass through HTML escaping and can reverse text.
    check("bidi stripped", "‮" in ds._as_text("John ‮Smith"), False)
    check("accents kept", ds._as_text("Jürgen"), "Jürgen")
    check("apostrophe kept", ds._as_text("O'Connor"), "O'Connor")
    print("  email, query, spreadsheet and invisible-character defences")


def test_malformed_scrape():
    section("Malformed data from the scraper")
    payloads = [
        "people is a string", ["entries are strings"], [None], [1, 2, 3],
        [{"name": {"a": "b"}}], [{"name": ["A", "B"]}], [{"name": None}],
        [{"name": "A B", "credentials": 42}],
        [{"name": "A B", "specialty": ["x"]}], None,
    ]
    for raw in payloads:
        try:
            people = ds._normalize_people(raw)
            ds.looks_clinical("hospital", "physicians", people)
        except Exception as exc:                       # noqa: BLE001
            FAILURES.append(f"malformed {raw!r}: {type(exc).__name__}")
    check("oversized people list capped",
          len(ds._normalize_people(
              [{"name": f"D{i} Smith"} for i in range(10_000)]))
          <= ds.MAX_DOCTORS * 5, True)
    print(f"  {len(payloads)} malformed shapes, none may crash")


# ---------------------------------------------------------------------------
# Counting and thresholds
# ---------------------------------------------------------------------------
def test_numbers():
    section("Report arithmetic")
    check("stars 0", profiler.stars(0, 5), "")
    check("stars 3", profiler.stars(3, 5), "***")
    check("stars at cap", profiler.stars(5, 5), "*****")
    check("stars collapse", profiler.stars(6, 5), "*x6")

    profiles = {"Dr Test": {"total": Counter({"Alpha": 8}),
                            "first": Counter({"Alpha": 3}),
                            "focus_papers": 10, "first_author_papers": 3,
                            "verify": ""}}
    rows = profiler._report(profiles, [], "Test", 5, 2018, 12, 2, 5,
                            log=lambda *a: None,
                            doc_freq=Counter({"Alpha": 8}), corpus_size=10)
    check("row count", len(rows), 1)
    check("papers", rows[0]["papers"], 8)
    check("first-author papers", rows[0]["first_author_papers"], 3)
    check("share of work", rows[0]["share_of_their_focus_papers"], 0.8)

    # A person with papers but no recurring subject must still be
    # reported, not silently dropped from the page.
    thin = {"Dr Thin": {"total": Counter({"X": 1}), "first": Counter(),
                        "focus_papers": 6, "first_author_papers": 0,
                        "verify": ""}}
    excluded = []
    rows = profiler._report(thin, excluded, "Test", 5, 2018, 12, 2, 5,
                            log=lambda *a: None,
                            doc_freq=Counter({"X": 1}), corpus_size=10)
    check("no-topic researcher still reported", len(excluded), 1)
    print("  asterisks, shares, counts and the no-topic case")


def test_focus():
    section("Condition versus specialty")
    # A specialty is not a filter: PubMed tags papers with the condition
    # studied, never with the author's specialty, so filtering an ENT
    # roster by "Otolaryngology" discarded every real paper.
    for specialty in ("otolaryngology", "dermatology", "cardiology",
                      "internal medicine", "orthopedic surgery"):
        check(f"specialty {specialty}", at.is_discipline(specialty), True)
    for disease in ("epilepsy", "ulcerative colitis", "heart failure",
                    "triple negative breast cancer"):
        check(f"disease {disease}", at.is_discipline(disease), False)

    # The condition's own vocabulary must not come back as a topic.
    focus = {"label": "Epilepsy", "match_terms": [],
             "stems": {"epilep", "seizur"}, "patterns": [],
             "is_discipline": False}
    article = {"text": "", "mesh": ["Seizures", "Epilepsy",
                                    "Sudden Unexpected Death in Epilepsy"],
               "substances": []}
    topics = at.topics_for(article, {}, focus)
    check("bare condition suppressed", "Seizures" in topics, False)
    check("compound topic kept",
          "Sudden Unexpected Death in Epilepsy" in topics, True)
    check("geography suppressed",
          at.topics_for({"text": "", "mesh": ["Sweden"], "substances": []},
                        {}, focus), set())
    print("  5 specialties, 4 diseases, topic suppression")


def test_rosters():
    section("Roster files")
    import tempfile, os
    cases = [
        ("null bytes", "name,author,affiliation\nA\x00B,X Y,NYU\n", 1),
        ("unicode", "name,author,affiliation\n李医生,Li M,PKU\n", 1),
        ("quoted commas",
         'name,author,affiliation\n"Smith, John","Smith J","NYU, NY"\n', 1),
        # One PubMed request per person: an oversized roster would earn a
        # rate-limit block partway through.
        ("oversized", "name,author,affiliation\n" +
         "".join(f"D{i} X,X D{i},NYU\n" for i in range(10_000)),
         profiler.MAX_RESEARCHERS),
    ]
    for label, content, expected in cases:
        path = tempfile.mktemp(suffix=".csv")
        open(path, "w").write(content)
        try:
            rows = profiler.load_researchers(path, log=lambda *a: None)
            check(f"roster {label}", len(rows), expected)
        except Exception as exc:                       # noqa: BLE001
            FAILURES.append(f"roster {label}: {type(exc).__name__}: {exc}")
        finally:
            os.unlink(path)

    for label, content in [("empty", ""), ("no header", "Smith J,NYU\n")]:
        path = tempfile.mktemp(suffix=".csv")
        open(path, "w").write(content)
        try:
            profiler.load_researchers(path, log=lambda *a: None)
            FAILURES.append(f"roster {label}: accepted, should be rejected")
        except SystemExit:
            pass
        finally:
            os.unlink(path)
    print(f"  {len(cases)} readable, 2 rejected")


def test_output_files():
    section("Output files")
    import tempfile, time as _t
    out = tempfile.mkdtemp()
    rows = [{"researcher": "A", "topic": "t", "papers": 1,
             "first_author_papers": 0, "share_of_their_focus_papers": 0,
             "their_focus_papers": 1, "verify_on_pubmed": ""}]
    first = profiler._write_csv(rows, "epilepsy", out)
    _t.sleep(1.05)
    second = profiler._write_csv(rows, "epilepsy", out)
    # A fixed name per condition meant a second search destroyed the first
    # search's results with no warning.
    check("results not overwritten", first != second, True)

    for hostile in ("../../../../tmp/pwned", "a/b/c", "..\\..\\win"):
        slug = profiler.slugify(hostile)
        check(f"slug {hostile}", "/" in slug or ".." in slug, False)
    print("  timestamped results, safe filenames")


def main():
    for test in (test_urls, test_clinical, test_person_names, test_author_form,
                 test_injection, test_malformed_scrape, test_numbers,
                 test_focus, test_rosters, test_output_files):
        test()

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"FAILED -- {len(FAILURES)} problem(s):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All regression tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
