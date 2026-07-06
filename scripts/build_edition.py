#!/usr/bin/env python3
"""
build_edition.py — nightly builder for the arXiv Observing Log.

Queries the public arXiv API for the last 48h of astro-ph.GA / astro-ph.CO /
astro-ph.IM submissions, scores each paper against config/keywords.json, adds a
short *extractive* (no-LLM) gloss to strong matches, and writes a pre-built
edition the front end loads instantly:

    data/latest.json                 <- newest edition
    data/editions/YYYY-MM-DD.json    <- dated archive copy

Design goals:
  * Standard library + `requests` only. No API keys. Runs free on GitHub Actions.
  * NEVER crash the build. Any failure fetching/parsing arXiv logs a warning and
    exits 0, leaving the previous edition in place so the site keeps working.
  * Output schema matches exactly what index.html expects (see EDITION SCHEMA).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET

import requests

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "keywords.json")
DATA_DIR = os.path.join(ROOT, "data")
EDITIONS_DIR = os.path.join(DATA_DIR, "editions")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")

CATEGORIES = ["astro-ph.GA", "astro-ph.CO", "astro-ph.IM"]
ML_CROSSLISTS = {"cs.LG", "stat.ML", "cs.CV", "cs.AI"}

API_URL = "https://export.arxiv.org/api/query"
MAX_RESULTS = 200
# Submission window in hours. Defaults to 48h (the nightly cadence); override with
# ARXIV_WINDOW_HOURS for a wider one-off cut (e.g. seeding, or after a holiday gap).
try:
    WINDOW_HOURS = int(os.environ.get("ARXIV_WINDOW_HOURS", "48"))
except ValueError:
    WINDOW_HOURS = 48
STRONG_TIER = 5  # score >= this gets an extractive gloss (matches front-end TOP_TIER)

# Polite, identifiable User-Agent per arXiv API etiquette.
USER_AGENT = (
    "arxiv-observing-log/1.0 "
    "(+https://github.com/nicolas-garc/arxiv-observing-log; nightly edition builder)"
)

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def log(msg):
    print("[build_edition] " + msg, flush=True)


def load_keywords():
    """Load keywords from config; fall back to a sane default if unreadable."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        kws = [k.strip() for k in data.get("keywords", []) if k and k.strip()]
        if kws:
            return kws
        log("config/keywords.json had no usable keywords; using defaults.")
    except Exception as e:  # noqa: BLE001
        log("could not read config/keywords.json (%s); using defaults." % e)
    return [
        "machine learning", "deep learning", "neural network",
        "simulation-based inference", "galaxy morphology", "emulator",
        "normalizing flow", "graph neural network", "cosmological parameter",
        "N-body simulation",
    ]


def collapse_ws(s):
    """Normalize whitespace exactly like the front end (replace(/\\s+/g, ' '))."""
    return re.sub(r"\s+", " ", (s or "")).strip()


def fetch_feed():
    """Single request to the arXiv API. Returns raw Atom XML text, or None on failure."""
    cat_query = " OR ".join("cat:" + c for c in CATEGORIES)
    params = {
        "search_query": "(" + cat_query + ")",
        "start": 0,
        "max_results": MAX_RESULTS,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    try:
        resp = requests.get(
            API_URL, params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:  # noqa: BLE001
        log("arXiv API request failed (%s)." % e)
        return None


def parse_entries(xml_text):
    """Parse Atom XML into paper dicts. Returns [] on parse failure."""
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:  # noqa: BLE001
        log("could not parse arXiv response as XML (%s)." % e)
        return []

    papers = []
    for entry in root.findall(ATOM + "entry"):
        def text(tag):
            node = entry.find(ATOM + tag)
            return collapse_ws(node.text) if node is not None and node.text else ""

        abs_url = text("id")
        title = text("title")
        abstract = text("summary")
        published = text("published")

        authors = []
        for a in entry.findall(ATOM + "author"):
            name = a.find(ATOM + "name")
            if name is not None and name.text:
                authors.append(collapse_ws(name.text))

        cats = []
        for c in entry.findall(ATOM + "category"):
            term = c.get("term")
            if term and term not in cats:
                cats.append(term)

        pdf = ""
        for link in entry.findall(ATOM + "link"):
            if link.get("title") == "pdf":
                pdf = link.get("href", "")
                break
        if not pdf and abs_url:
            # Fallback: derive pdf URL from the abs URL.
            pdf = abs_url.replace("/abs/", "/pdf/")

        if not abs_url or not title:
            continue

        papers.append({
            "id": abs_url,
            "title": title,
            "abstract": abstract,
            "published": published,
            "authors": authors,
            "cats": cats,
            "pdf": pdf,
        })
    return papers


def parse_dt(s):
    """Parse an arXiv ISO-8601 timestamp (e.g. 2024-01-02T03:04:05Z) to aware UTC."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def within_window(papers, hours):
    """Keep papers submitted within the last `hours`."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    kept = []
    for p in papers:
        dt = parse_dt(p.get("published", ""))
        if dt is not None and dt >= cutoff:
            kept.append(p)
    return kept


def score_paper(p, keywords):
    """
    Score matches the front end (scorePaper):
      * per keyword: (title occurrences * 4) + min(abstract occurrences, 3)
      * +3 if cross-listed to an ML category (ML boost, on by default)
    Counting uses str.split like the front end for identical results.
    """
    title = p["title"].lower()
    abstract = p["abstract"].lower()
    s = 0
    for kw_raw in keywords:
        kw = kw_raw.lower()
        if not kw:
            continue
        # mirror JS "str.split(kw).length - 1" for occurrence counts
        s += (len(title.split(kw)) - 1) * 4
        s += min(len(abstract.split(kw)) - 1, 3)
    if any(c in ML_CROSSLISTS for c in p["cats"]):
        s += 3
    return s


def extractive_gloss(p, keywords):
    """
    Pick the single most keyword-relevant sentence from the abstract as a gloss.
    Purely extractive — no summarization model, no external calls.
    """
    abstract = p["abstract"].strip()
    if not abstract:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", abstract)
    kws = [k.lower() for k in keywords if k]

    best_sentence = sentences[0]
    best_hits = 0
    for idx, sent in enumerate(sentences):
        low = sent.lower()
        hits = sum(low.count(kw) for kw in kws)
        # Prefer the keyword-densest sentence; ties keep the earlier one.
        if hits > best_hits:
            best_hits, best_sentence = hits, sent

    # If no sentence contained a keyword, best_sentence stays as the first one.
    chosen = collapse_ws(best_sentence)
    if len(chosen) > 200:
        chosen = chosen[:197].rsplit(" ", 1)[0] + "…"
    return chosen


def build_edition(papers, keywords):
    for p in papers:
        p["score"] = score_paper(p, keywords)
        if p["score"] >= STRONG_TIER:
            gloss = extractive_gloss(p, keywords)
            if gloss:
                p["ai_summary"] = gloss
    papers.sort(
        key=lambda p: (p["score"], p.get("published", "")),
        reverse=True,
    )
    return {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "papers": papers,
    }


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    keywords = load_keywords()
    log("loaded %d keywords." % len(keywords))

    xml_text = fetch_feed()
    if xml_text is None:
        log("no data fetched; leaving previous edition untouched. Exiting cleanly.")
        return 0

    papers = parse_entries(xml_text)
    log("parsed %d entries from feed." % len(papers))

    papers = within_window(papers, WINDOW_HOURS)
    log("%d papers within the last %dh." % (len(papers), WINDOW_HOURS))

    if not papers:
        # arXiv is quiet (weekend / holiday gap). Don't blank the site — keep the
        # previous edition so visitors still get a pre-built page. Once built_at
        # ages past the front end's 36h cutoff it falls back to a live fetch on
        # its own. This mirrors the network-failure path above.
        log("no papers in window; preserving previous edition. Exiting cleanly.")
        return 0

    edition = build_edition(papers, keywords)
    strong = sum(1 for p in edition["papers"] if p["score"] >= STRONG_TIER)
    log("built edition: %d papers, %d strong matches." % (len(edition["papers"]), strong))

    write_json(LATEST_PATH, edition)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    write_json(os.path.join(EDITIONS_DIR, today + ".json"), edition)
    log("wrote %s and data/editions/%s.json" % (os.path.relpath(LATEST_PATH, ROOT), today))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001  — last-resort guard: never crash the build.
        log("unexpected error (%s); exiting cleanly to protect the build." % e)
        sys.exit(0)
