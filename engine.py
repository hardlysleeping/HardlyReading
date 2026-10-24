# HardlyReading — desktop web app for Internet Archive magazines, comics and PDFs
# Copyright (C) 2026 hardlysleeping
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.

"""HardlyReading — engine: queue, resolver, downloader, verifier.

Stdlib only. Runs on Windows / Linux / macOS.
"""
import atexit
import json
import hashlib
import html
import io
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------- constants

SEARCH_API = "https://archive.org/advancedsearch.php"
META_API = "https://archive.org/metadata/{}"

# Best-first. .cbr/.cbz ARE the scan for many comic/magazine items; a Text PDF
# is often just the OCR reflow sitting beside it.
PREFERRED_FORMATS = ("Comic Book RAR", "Comic Book ZIP",
                     "Image Container PDF", "Text PDF")

# This app is for DOCUMENTS. archive.org also holds movies/audio/software/
# etree; none of those belong in a magazine/book downloader, so mediatype is
# always constrained to these.
DOC_TYPES = ("texts",)

# Formats we never treat as a deliverable.
DERIVATIVE_FORMATS = {"Additional Text PDF", "EPUB", "LCP Encrypted EPUB",
                      "Daisy", "chOCR", "DjVuTXT", "Djvu XML", "hOCR",
                      "OCR Page Index", "OCR Search Text", "Scandata",
                      "Page Numbers JSON", "Item Tile", "Metadata",
                      "Archive BitTorrent", "Single Page Processed JP2 ZIP"}

FORMAT_LABEL = {
    "Comic Book RAR": "CBR (comic)",
    "Comic Book ZIP": "CBZ (comic)",
    "Image Container PDF": "PDF (scan)",
    "Text PDF": "PDF (text)",
}


# ---------------------------------------------------------------- paths

def user_config_dir() -> str:
    """Per-OS config directory, no external dependency.

    IA_CONFIG_DIR wins on every platform: it is what lets a second copy run
    beside the first (or a test instance exist at all) without sharing a
    state file. Otherwise the native location is used.
    """
    override = os.environ.get("IA_CONFIG_DIR")
    if override:
        p = override
        os.makedirs(p, exist_ok=True)
        return p
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    p = os.path.join(base, "hardlyreading")
    os.makedirs(p, exist_ok=True)
    return p


def documents_dir() -> str:
    """The user's Documents folder, honouring XDG on Linux.

    Read from ~/.config/user-dirs.dirs rather than assuming ~/Documents, because
    a localized or relocated home puts it somewhere else and writing a library
    into a folder nobody opens is the same as losing it.
    """
    home = os.path.expanduser("~")
    # Windows: Documents can be redirected into OneDrive, and writing the
    # library to a folder Explorer never shows is the same as losing it.
    if os.name == "nt":
        for cand in (os.path.join(os.environ.get("OneDrive", ""), "Documents"),
                     os.path.join(os.environ.get("USERPROFILE", home), "Documents")):
            if cand and os.path.isdir(cand):
                return cand
        return os.path.join(home, "Documents")
    if sys.platform == "darwin":
        return os.path.join(home, "Documents")
    cfg = os.path.join(home, ".config", "user-dirs.dirs")
    try:
        with open(cfg, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("XDG_DOCUMENTS_DIR"):
                    val = line.split("=", 1)[1].strip().strip('"')
                    if val:
                        return val.replace("$HOME", home)
    except OSError:
        pass
    return os.path.join(home, "Documents")


def default_download_dir() -> str:
    """Where downloads go when the setting is empty: <Documents>/HardlyReading.

    Documents rather than Downloads: these are things you keep and read, not
    files you clear out of a download tray. Falls back to the home directory if
    there is no Documents folder at all.
    """
    docs = documents_dir()
    base = docs if os.path.isdir(docs) else os.path.expanduser("~")
    return os.path.join(base, "HardlyReading")


STATE_FILE = os.environ.get("IA_STATE") or os.path.join(user_config_dir(), "state.json")
HISTORY_DB = os.environ.get("IA_HISTORY") or os.path.join(user_config_dir(), "history.db")


# ---------------------------------------------------------------- http helpers

def http_json(url: str, timeout: int = 60):
    """GET a URL and return parsed JSON. No retry here — callers that can
    recover (the resolver, the per-item probe) do their own attempts."""

    req = urllib.request.Request(url, headers={"User-Agent": "HardlyReading/2.11.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---------------------------------------------------------------- job model

@dataclass
class Job:
    id: str
    kind: str                  # search | collection | item | query
    source: str                # what the user pasted
    label: str                 # human title
    query: str = ""            # resolved advancedsearch query or identifier
    scoped_query: str = ""     # what archive.org was actually asked (shown in UI)
    dir: str = ""              # where this job's files land
    series: str = ""           # Komga series folder (one folder per series)
    status: str = "queued"     # queued|resolving|planned|downloading|verifying|done|failed|cancelled
    items: list = field(default_factory=list)   # resolved entries
    total_bytes: int = 0
    done_bytes: int = 0
    speed: float = 0.0
    message: str = ""
    error: str = ""
    retries: int = 0
    next_try_at: float = 0.0   # retry ladder: earliest time a retry may run
    fatal: bool = False        # a permanent failure — never retried
    desc: str = ""             # the source's own blurb, if it had a useful one
    # How many issues the user asked for, and from which end. 0 means all.
    # Applied once the listing is in reading order, so "25 newest" means the
    # last 25 issues of the run, not whichever 25 archive.org listed first.
    limit: int = 0
    want_order: str = "oldest"  # oldest | newest
    available: int = 0          # how many the listing offered before limiting
    pause_reason: str = ""      # space — why a job stopped without failing
    # Files downloading right now, keyed by aria2's gid. A job runs several at
    # once (-j), so one percentage for the whole job hides what is happening.
    live: dict = field(default_factory=dict)
    # What the user filed this job under. "" is uncategorised, and that is what
    # everything is until someone says otherwise.
    category: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def summary(self):
        free = 0
        try:
            probe = self.dir
            while probe and not os.path.isdir(probe):
                probe = os.path.dirname(probe)
            if probe:
                free = shutil.disk_usage(probe).free
        except OSError:
            pass
        # Duplicates, not the ones the count left out: "7 duplicates skipped"
        # on a job limited to 2 of 9 is a lie about what happened.
        skipped = [i for i in self.items if i.get("skip")
                   and not str(i.get("reason", "")).startswith(("over the", "not selected"))]
        return {
            "id": self.id, "kind": self.kind, "source": self.source,
            "label": self.label, "status": self.status,
            "n_items": len(self.items), "total_bytes": self.total_bytes,
            "done_bytes": self.done_bytes, "speed": self.speed,
            "message": self.message, "error": self.error,
            "live": list(self.live.values())[-12:],
            "retries": self.retries, "created": self.created,
            "updated": self.updated,
            "scoped_query": self.scoped_query,
            "dir": self.dir,
            "series": self.series,
            "category": self.category,
            # How many issues were asked for, from which end, and how many the
            # listing offered — so the card can say "the newest 25 of 312".
            "limit": self.limit, "want_order": self.want_order,
            "available": self.available, "pause_reason": self.pause_reason,
            # What this job will actually fetch, which is not len(items): the
            # items list also holds everything skipped or over the limit.
            "n_take": len([i for i in self.items if not i.get("skip")]),
            # The source's own blurb, so the queue card can say what this is
            # rather than only what it is called.
            "desc": self.desc,
            # First real identifier, so the UI can show the item's cover without
            # a second request. Falls back to the source URL for jobs whose
            # items have not resolved yet.
            "identifier": (next((i.get("identifier") for i in self.items
                                 if i.get("identifier") and not i.get("skip")), "")
                           or _ident_from_url(self.source or "")),
            "skipped": len(skipped),
            # How many items the job actually expected to fetch. A job can end
            # "done" with zero of them: every candidate was skipped or missing,
            # which is not the same thing as success.
            "n_ok": len([i for i in self.items if not i.get("skip")]),
            # Items genuinely on disk. A job can be "done" with none of them:
            # every candidate missing at the source is not a success.
            "n_disk": sum(1 for i in self.items
                          if i.get("outname") and os.path.isfile(
                              os.path.join(self.dir, i["outname"]))),
            "free_bytes": free,
            "fits": (free == 0) or (self.total_bytes <= free),
        }


# ---------------------------------------------------------------- resolver

def pick_file(files):
    """One good artifact per item: prefer the scan, prefer `original`.

    Order: Comic Book RAR (.cbr) -> Comic Book ZIP (.cbz) -> Image Container
    PDF -> Text PDF. Skips history/ superseded copies. Within a format, prefer
    source == original; largest wins.
    """
    live = [f for f in files
            if "history/" not in f.get("name", "")
            and "~1~" not in f.get("name", "")]
    for fmt in PREFERRED_FORMATS:
        cands = [f for f in live if f.get("format") == fmt]
        if not cands:
            continue
        originals = [f for f in cands
                     if (f.get("source") or "").lower() == "original"]
        pool = originals or cands
        return max(pool, key=lambda f: int(f.get("size", 0) or 0))
    for f in live:
        if f.get("name", "").lower().endswith((".pdf", ".cbr", ".cbz")):
            return f
    return None


def find_jp2(files):
    """The raw-scan zip, when an item carries one."""
    for f in files:
        n = f.get("name", "").lower()
        if n.endswith(".zip") and ("jp2" in n or "_jp2" in n):
            return f
    return None


def sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return re.sub(r"\s+", " ", name).strip()[:120]


def search_ids(query: str, rows: int = 1000, sort: str = ""):
    """Enumerate a listing's members.

    Verified against real collections, none of archive's sort modes gives
    reading order on its own:

      year asc        -> 4, 2, 3, 8, 6, 5, 1, 15 …   (ties within a year are
                         arbitrary, and `year` is the SCAN year)
      (no sort)       -> 333, 49, 75, 159, 162, 5 …   (arbitrary)
      titleSorter asc -> 148, 149, 171, 173, 214 …    (ascending, but string
                         sort puts `Cracked 22` and `Cracked Monster Party 24`
                         elsewhere, and a multi-title collection alphabetises
                         the spin-offs instead)

    So the order is decided by the caller from the issue numbers, not by the
    query. `sort` is kept for callers that genuinely want archive's ordering.
    """
    pairs = [("q", query), ("rows", str(rows)), ("page", "1"),
             ("output", "json"), ("fl[]", "identifier")]
    if sort:
        pairs.append(("sort[]", sort))
    url = SEARCH_API + "?" + urllib.parse.urlencode(pairs)
    resp = http_json(url)["response"]
    return [d["identifier"] for d in resp["docs"]], resp["numFound"]


def scope_query(query: str, mediatype: str = "texts") -> str:
    """Re-express a plain user query as a scoped advancedsearch query.

    archive.org's default search is FULL-TEXT, so `mad magazine` also matches
    court filings and Twitch chat logs. Title-scoping plus a document mediatype
    turns a pile of noise into the actual publication run.

    A query that already uses field syntax is passed through, except that we
    always pin it to documents — this app does not download video, audio, or
    software, so an unrestricted query would surface things it cannot use.
    """
    q = (query or "").strip()
    if not q:
        return q
    if not re.search(r"\b(title|collection|subject|creator|mediatype|identifier|year)\s*:",
                     q, re.I):
        words = [w for w in re.split(r"\s+", q) if w]
        q = "title:(" + " ".join(words) + ")"
    # Pin to text unless the query already names a mediatype or a collection
    # (collection:(x) already implies a container; adding mediatype:(texts)
    # would return 0 because the collection item itself is mediatype
    # `collection`).
    if not re.search(r"\bmediatype\s*:", q, re.I) \
            and not re.search(r"\bcollection\s*:", q, re.I):
        q = f"{q} AND mediatype:({mediatype})"
    return q


def _ident_from_url(url: str) -> str:
    """Pull an archive.org identifier out of a details/ or download/ URL."""
    m = re.search(r"archive\.org/(?:details|download|embed|stream)/([^/?#]+)", url or "")
    return m.group(1) if m else ""


def plain_text(value, limit: int = 400) -> str:
    """Archive.org descriptions are HTML, sometimes a list, often with tags.

    Reduce to readable prose for a card: strip tags, collapse whitespace, cut
    at a word boundary. The UI renders it as text, never as HTML.
    """
    if isinstance(value, list):
        value = " ".join(str(v) for v in value if v)
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0]
        return cut + "…"
    return text


def _norm_words(text: str) -> str:
    """Words only, lowercased — for comparing a description with its title."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def useful_desc(desc: str, title: str) -> str:
    """A description worth showing, or "".

    Most archive.org collections repeat their own title in the description
    field, so a card ends up with the same words twice and no information. If
    the description is the title again (or a prefix of it, or two words long)
    it earns no space on the card.
    """
    d, t = _norm_words(desc), _norm_words(title)
    if not d:
        return ""
    if not t:
        return desc
    if d == t or d in t or t in d:
        return ""
    if len(d.split()) < 4:
        return ""          # "Im sure", "cbr+pdf" — noise, not a description
    return desc


# Sort modes offered by the search UI. archive.org's advancedsearch takes these
# as sort[] values; "" is left out so its own ordering (most downloaded) stands.
SORT_MODES = {
    "": "downloads desc",
    "downloads": "downloads desc",
    "newest": "publicdate desc",
    "oldest": "publicdate asc",
    "title": "titleSorter asc",
}

# Only the languages the UI offers, mapped to the tokens the index actually
# uses. Most items carry the ISO 639 code ("eng"), a minority the English word
# ("English") — matching only one of them silently dropped most results, which
# is why both are listed. A free-text language field would also be a way to
# write arbitrary query syntax.
ADVANCED_LANGUAGES = {
    "english": ["eng", "English"],
    "spanish": ["spa", "Spanish"],
    "french": ["fre", "fra", "French"],
    "german": ["ger", "deu", "German"],
    "italian": ["ita", "Italian"],
    "portuguese": ["por", "Portuguese"],
    "dutch": ["dut", "nld", "Dutch"],
    "japanese": ["jpn", "Japanese"],
}


def _lucene_phrase(value, limit: int = 120) -> str:
    """A quoted phrase safe to paste into a query.

    Quotes and backslashes are the only characters that can end a phrase early,
    so they are dropped; the rest is plain text inside quotes.
    """
    v = str(value or "").replace('"', " ").replace("\\", " ")
    v = " ".join(v.split())[:limit]
    return f'"{v}"' if v else ""


def advanced_filters(year_from=None, year_to=None, language="", creator="",
                     subject="") -> str:
    """Compose the extra clauses for an advanced search. Returns "" if unused.

    Every value is validated or quoted: this string goes straight into the
    query sent to archive.org, so a stray quote must not be able to add clauses
    of its own.
    """
    parts = []

    def year(v):
        v = str(v or "").strip()
        return v if v.isdigit() and len(v) == 4 else ""

    y1, y2 = year(year_from), year(year_to)
    if y1 and y2 and y1 > y2:
        y1, y2 = y2, y1
    if y1 or y2:
        parts.append(f"year:[{y1 or '*'} TO {y2 or '*'}]")

    langs = ADVANCED_LANGUAGES.get(str(language or "").strip().lower())
    if langs:
        parts.append("language:(" + " OR ".join(langs) + ")")

    for field, value in (("creator", creator), ("subject", subject)):
        phrase = _lucene_phrase(value)
        if phrase:
            parts.append(f"{field}:({phrase})")

    return " AND ".join(parts)


def search_page(query: str, rows: int = 24, page: int = 1,
                mediatype: str = "texts", sort: str = "downloads desc",
                title_only: bool = True, collections_only: bool = False,
                extra_fields=None, filters: str = ""):
    """Search archive.org for browsing.

    Scoping is shared with the queue path via scope_query() so browsing and
    pulling always mean the same thing.

    collections_only overrides mediatype with `collection`: a collection is its
    own mediatype on archive.org, so `mediatype:texts` silently excludes every
    collection page (e.g. `madmagazine`), which is usually what you want to
    find when hunting for a run to pull.
    """
    if collections_only:
        q = scope_query(query, "collection") if title_only else \
            f"{query} AND mediatype:(collection)"
    else:
        q = scope_query(query, mediatype) if title_only else query
    # Advanced filters are ANDed after scoping so they narrow the scoped query
    # rather than replacing its document restriction.
    if filters:
        q = f"({q}) AND {filters}"

    fields = ["identifier", "title", "year", "mediatype", "downloads",
              "creator", "description"]
    # Callers that list a collection's members also want the issue date and the
    # item size; asking for them only there keeps ordinary search payloads lean.
    for f in (extra_fields or []):
        if f not in fields:
            fields.append(f)
    params = [("q", q), ("rows", str(rows)), ("page", str(page)),
              ("output", "json")] + [("fl[]", f) for f in fields]
    if sort:
        params.append(("sort[]", sort))
    url = SEARCH_API + "?" + urllib.parse.urlencode(params)
    payload = http_json(url)
    if "response" not in payload:
        # archive.org refuses pages past its 10,000-result deep-paging limit
        # rather than returning an empty page. Fall back to the first page and
        # say so, instead of surfacing a bare "'response'" to the UI.
        msg = str(payload.get("error") or "")
        params = [(k, v) for k, v in params if k != "page"] + [("page", "1")]
        payload = http_json(SEARCH_API + "?" + urllib.parse.urlencode(params))
        # The results below are page 1, so say page 1 rather than echoing the
        # page that was refused.
        page = 1
        deep_paging = True
        if "response" not in payload:
            return {"numFound": 0, "start": 0, "page": 1, "scoped_query": q,
                    "docs": [], "filtered": "all", "visible": 0,
                    "deep_paging": True, "note": msg[:200]}
    resp = payload["response"]
    docs = resp.get("docs", [])
    return {
        "numFound": resp.get("numFound", 0),
        "start": resp.get("start", 0),
        "page": page,
        "deep_paging": locals().get("deep_paging", False),
        "scoped_query": q,
        "docs": [{
            "identifier": d.get("identifier"),
            "title": d.get("title") if isinstance(d.get("title"), str)
                     else (d.get("title") or [d.get("identifier")])[0],
            "year": d.get("year"),
            "mediatype": d.get("mediatype"),
            "downloads": d.get("downloads"),
            "creator": d.get("creator") if isinstance(d.get("creator"), str)
                       else (d.get("creator") or [""])[0],
            # A blurb that only repeats the title reads as a rendering bug, so
            # it is dropped before it reaches the card.
            "desc": useful_desc(
                plain_text(d.get("description")),
                (d.get("title") or [d.get("identifier")] or [""])[0]
                if isinstance(d.get("title"), list)
                else (d.get("title") or d.get("identifier") or "")),
            "thumb": f"https://archive.org/services/img/{d.get('identifier')}",
        } for d in docs],
    }


def search_documents(query: str, page: int = 1, rows: int = 24,
                     kind: str = "all", workers: int = 10,
                     filters: str = "", sort: str = ""):
    """Search documents, optionally filtered by what each result HOLDS.

    `kind`:
      all        — every document result
      collection — real admin collections (has members, no payload)
      bundle     — a single item holding many issues (not a collection tag)

    The strict filters require classifying each visible result via /metadata,
    because archive.org's own labels do not distinguish these (see probe_item).
    That is one small request per tile, so it runs only for the current page.
    """
    if kind == "collection":
        # A collection item is mediatype `collection`, so it is excluded by the
        # documents-only `mediatype:(texts)` filter. Query the container type
        # directly for this filter, then confirm it really holds no payload.
        raw = search_page(query, rows=max(rows * 3, 60), page=page,
                          collections_only=True, filters=filters,
                          sort=sort or "downloads desc")
        idents = [d["identifier"] for d in raw["docs"]]
        probed = {p["identifier"]: p for p in classify_many(idents, workers=workers)}
        docs = []
        for d in raw["docs"]:
            p = probed.get(d["identifier"]) or {}
            if p.get("kind") == "collection":
                docs.append({**d, "hold_kind": "collection",
                             "scans": p.get("scans"),
                             "collections": p.get("collections")})
        return {
            "numFound": raw["numFound"],
            "page": page,
            "scoped_query": raw["scoped_query"],
            "filtered": kind,
            "visible": len(docs),
            "docs": docs[:rows],
        }

    if kind == "bundle":
        # Ask archive.org for a wide page, classify, then take the first N that
        # match — otherwise a filtered page can come back nearly empty.
        raw = search_page(query, rows=max(rows * 4, 60), page=page,
                          filters=filters, sort=sort or "downloads desc")
        idents = [d["identifier"] for d in raw["docs"]]
        probed = classify_many(idents, workers=workers)
        by_id = {p["identifier"]: p for p in probed}
        docs = []
        for d in raw["docs"]:
            p = by_id.get(d["identifier"]) or {}
            if p.get("kind") == kind:
                docs.append({**d, "hold_kind": p.get("kind"),
                             "scans": p.get("scans")})
            if len(docs) >= rows:
                break
        return {
            "numFound": raw["numFound"],
            "page": page,
            "scoped_query": raw["scoped_query"],
            "filtered": kind,
            "visible": len(docs),
            "docs": docs,
        }

    raw = search_page(query, rows=rows, page=page, filters=filters,
                      sort=sort or "downloads desc")
    idents = [d["identifier"] for d in raw["docs"]]
    probed = {p["identifier"]: p for p in classify_many(idents, workers=workers)}
    for d in raw["docs"]:
        p = probed.get(d["identifier"]) or {}
        d["hold_kind"] = p.get("kind")
        d["scans"] = p.get("scans")
        d["members"] = p.get("members")
    raw["filtered"] = "all"
    raw["visible"] = len(raw["docs"])
    return raw


def probe_item(ident: str, count_members: bool = False) -> dict:
    """Classify one archive.org identifier by what it actually HOLDS.

    mediatype is not trustworthy for this. Measured on real items:

        identifier                    mediatype    files  scans  members
        madmagazine                   collection      10      0      835
        mensmagazines                 collection       7      0     3553
        The-MAD-Magazine-Collection   texts         2801   1593        0
        mad-magazine-180-1976         texts           20      2        0

    `mediatype:collection` means "this is an administrative container" — it has
    members and no payload — but archive.org also labels plenty of single issues
    and stray uploads `collection`, and the genuinely useful "collected works in
    one listing" (2801 files, 1593 scans) is a `texts` item instead.

    Classification is (has_payload, scan_count), and deliberately does NOT query
    the member count: `collection:<id>` costs ~0.2-7s depending on cache state,
    and running one per tile made a 24-result page take 19 seconds. A container
    is recognised by having NO scan payload plus the docs/collection marker;
    the member count is fetched only when a caller explicitly asks for it.
    """
    try:
        m = http_json(META_API.format(ident))
    except Exception as e:  # noqa: BLE001
        return {"identifier": ident, "kind": "error", "error": str(e),
                "title": ident, "scans": 0, "members": None}

    md = m.get("metadata", {}) or {}
    files = m.get("files", []) or []
    title = md.get("title") or ident
    if not isinstance(title, str):
        title = str(title[0]) if title else ident

    scans = 0
    for f in files:
        name = (f.get("name") or "").lower()
        if "history/" in name or "~1~" in name:
            continue
        if f.get("format") in PREFERRED_FORMATS:
            scans += 1
        elif name.endswith((".pdf", ".cbr", ".cbz")):
            scans += 1

    collections = md.get("collection") or []
    if isinstance(collections, str):
        collections = [collections]

    # A real container: archive says mediatype collection AND it holds no scans.
    # Single issues misfiled as collections have scans, so they land in
    # bundle/single instead of being offered as a listing to expand.
    if md.get("mediatype") == "collection" and scans == 0:
        kind = "collection"
    elif scans >= 5:
        kind = "bundle"
    elif scans >= 1:
        kind = "single"
    else:
        kind = "empty"

    members = None
    if count_members:
        try:
            members = search_ids(f"collection:{ident}", rows=1)[1]
        except Exception:  # noqa: BLE001
            members = None

    return {
        "identifier": ident,
        "kind": kind,
        "title": title,
        "mediatype": md.get("mediatype"),
        "collections": collections[:6],
        "scans": scans,
        "members": members,
        "files": len(files),
        "thumb": f"https://archive.org/services/img/{ident}",
    }


def classify_many(idents, workers: int = 10):
    """Probe many identifiers in parallel, keeping input order.

    Returns one dict per identifier with `kind` of collection / bundle / single
    (or `skip` plus a reason when the item has nothing we can download).
    """

    from concurrent.futures import ThreadPoolExecutor as _TPE
    with _TPE(max_workers=workers) as ex:
        return list(ex.map(probe_item, idents))


def classify_url(url: str):
    """Turn a pasted archive.org URL into (kind, query, label)."""
    url = url.strip()
    if not url:
        return None
    if url.startswith("collection:"):
        return ("collection", url, url.replace("collection:", "Collection: "))
    m = re.search(r"archive\.org/details/([^/?#]+)", url)
    if m:
        ident = m.group(1)
        if "tab=collection" in url:
            return ("collection", f"collection:{ident}", ident)
        return ("item", ident, ident)
    m = re.search(r"archive\.org/search[^#]*", url)
    if m:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse("https://" + m.group(0)).query)
        q = (qs.get("query") or qs.get("q") or [""])[0]
        if q:
            return ("search", q, q)
    if ":" in url and "://" not in url:
        return ("query", url, url[:60])
    return ("query", url, url[:60])


class Resolver:
    """Turns a job into a concrete list of artifacts."""

    def __init__(self, workers: int = 12, engine=None):
        self.workers = workers
        self.engine = engine

    def _desc_for(self, ident: str, md: dict) -> str:
        """This item's blurb, remembered in the index as it is used."""
        desc = useful_desc(plain_text(md.get("description")),
                           md.get("title") or ident)
        if self.engine is not None:
            self.engine.store_desc(ident, desc)
        return desc

    def resolve(self, job: Job, progress=None):
        if job.kind in ("search", "query"):
            # Same scoping the Search tab uses, so browsing and pulling agree.
            scoped = scope_query(job.query)
            job.scoped_query = scoped
            ids, num_found = search_ids(scoped)
            if num_found == 0:
                raise RuntimeError(f"No results for “{job.query}”")
        elif job.kind == "collection":
            ident = job.query.replace("collection:", "")
            job.scoped_query = f"collection:{ident}"
            ids, num_found = search_ids(f"collection:{ident}")
        elif job.kind == "item":
            job.scoped_query = f"item:{job.query}"
            ids, num_found = [job.query], 1
        else:
            raise RuntimeError(f"Unknown job kind {job.kind}")

        if not ids:
            raise RuntimeError("That link matched nothing on archive.org")

        if progress:
            progress(f"Found {num_found} item{'s' if num_found != 1 else ''}")

        def fetch(ident):
            for attempt in range(3):
                try:
                    m = http_json(META_API.format(ident))
                    files = m.get("files", [])
                    f = pick_file(files)
                    jp2 = find_jp2(files)
                    if not f:
                        fmts = ", ".join(sorted({
                            x.get("format", "?") for x in files
                            if x.get("format") not in DERIVATIVE_FORMATS
                        }))[:160]
                        return {"identifier": ident, "skip": True,
                                "reason": f"no PDF/CBZ scan ({fmts or 'none'})",
                                "title": m.get("metadata", {}).get("title", ident)}
                    md = m.get("metadata") or {}
                    if not job.desc:
                        # First useful blurb wins: a collection's rows usually
                        # share one description, and repeating it per item is
                        # noise.
                        job.desc = useful_desc(plain_text(md.get("description")),
                                               md.get("title") or job.label)
                    entry = {
                        "identifier": ident,
                        # This item's own blurb, filtered the same way a search
                        # card filters, falling back to the collection's.
                        "desc": (self._desc_for(ident, md) or job.desc),
                        "title": m.get("metadata", {}).get("title", ident),
                        "file": f["name"],
                        "format": f.get("format"),
                        "format_label": FORMAT_LABEL.get(f.get("format"), f.get("format")),
                        "size": int(f.get("size", 0) or 0),
                        "md5": f.get("md5"),
                        "url": (f"https://{m['server']}{m['dir']}/"
                                f"{urllib.parse.quote(f['name'])}"),
                        "jp2": bool(jp2),
                        # archive's own date, used only when the filename has
                        # none: `Cracked111.pdf` is dateless but date=1973.
                        "meta_year": _meta_year(m.get("metadata") or {}),
                    }
                    return entry
                except Exception as e:  # noqa: BLE001
                    if attempt == 2:
                        return {"identifier": ident, "skip": True,
                                "reason": f"error: {e}", "title": ident}
                    time.sleep(1 + attempt)

        results = []
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for i, entry in enumerate(ex.map(fetch, ids), 1):
                results.append(entry)
                if progress and i % 5 == 0:
                    progress(f"Resolving {i}/{len(ids)}")
        return results


# ---------------------------------------------------------------- engine

class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs: list[Job] = []
        self.order: list[str] = []
        # Jobs being worked on right now. The queue runs several at once (the
        # `parallel_jobs` setting), so "current" is a set, not one id.
        self.running = set()
        self._claimed = set()
        self._workers = []
        self.paused = False
        self.settings = {
            "download_dir": default_download_dir(),
            "jobs_in_flight": 12,
            "between_jobs": 30,
            # Off by default: adding to the queue starts downloading. Turn it
            # on if you want to see the file plan and size before anything runs.
            "review_before_download": False,
            "komga_naming": True,
            # Skip anything whose checksum already exists in the destination.
            "skip_existing": True,
            # Show the "reader" label in the reader header.
            "show_brand": True,
            # Installed as a user service when switched on; off unless asked.
            "autostart": False,
            # Free space kept back so a download cannot fill the disk. Belongs
            # here rather than as a constant so it can be tuned per install
            # (the settings API only accepts keys that already exist).
            "space_reserve_mb": SPACE_RESERVE_MB,
            # How many jobs may download at the same time. Two by default: it
            # keeps the network busy without turning the machine into a heater.
            "parallel_jobs": 2,
            # Listen on loopback only. Off = reachable from the phone/tablet on
            # the same network (no password). Toggled live from Settings.
            "local_only": False,
        }
        self._worker = None
        self._conn = None
        # Queue categories: labels the user makes. The order here IS the tab
        # order, with pinned ones kept at the front. Empty by default — every
        # job is uncategorised until someone says otherwise.
        self.categories: list = []
        # Titles kept for later with no file on disk. The library used to be
        # "what the queue finished"; this is the other half — something you
        # found, want to keep, and read straight from archive.org.
        self.shelf: list = []
        self._lock_db = threading.Lock()
        self._load_state()
        # Jobs queued before descriptions were stored have none. Fill them in
        # the background from archive.org, once, so old rows explain themselves
        # without the user re-adding anything.
        #
        # Only started when there is something to fill: a thread per engine that
        # only sleeps through interpreter shutdown left the test suite with ~30
        # of them alive at exit, which segfaulted the run on CPython 3.10.
        needs_backfill = any(
            not j.desc and any(i.get("identifier") for i in j.items)
            for j in self.jobs)
        self._backfill_thread = None
        if needs_backfill:
            self._backfill_thread = threading.Thread(
                target=self.backfill_descriptions, daemon=True,
                name="desc-backfill")
            self._backfill_thread.start()

    # ---- start-up integration
    UNIT_NAME = "hardlyreading.service"

    def _unit_dir(self) -> str:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        return os.path.join(base, "systemd", "user")

    def autostart_supported(self) -> bool:
        """Only systemd user services are wired up; other platforms say so."""
        return bool(shutil.which("systemctl")) and sys.platform.startswith("linux")

    def autostart_state(self) -> str:
        """'on', 'off' or 'unsupported' — read from disk, not from the setting."""
        if not self.autostart_supported():
            return "unsupported"
        return ("on" if os.path.isfile(os.path.join(self._unit_dir(), self.UNIT_NAME))
                else "off")

    def set_autostart(self, on: bool):
        """Install or remove the user unit. Returns (ok, message).

        The queue already survives a restart — state.json holds it and aria2
        resumes .aria2 partials — so this only decides whether the server comes
        back on its own.
        """
        if not self.autostart_supported():
            return (False, "Automatic start is only wired up for systemd on Linux. "
                           "On other systems, run the server from your own start-up "
                           "shortcut; the queue and partial downloads survive either way.")
        unit_dir = self._unit_dir()
        unit = os.path.join(unit_dir, self.UNIT_NAME)
        if not on:
            try:
                subprocess.run(["systemctl", "--user", "disable", "--now", self.UNIT_NAME],
                               capture_output=True, timeout=25)
            except Exception:
                pass
            try:
                os.remove(unit)
            except OSError:
                pass
            try:
                subprocess.run(["systemctl", "--user", "daemon-reload"],
                               capture_output=True, timeout=25)
            except Exception:
                pass
            return (True, "Will not start automatically any more.")

        here = os.path.dirname(os.path.abspath(__file__))
        # sys.executable may be a virtualenv belonging to whatever launched the
        # app. A unit must run a system interpreter that will still exist later.
        interp = sys.executable
        for cand in ("/usr/bin/python3", "/usr/local/bin/python3",
                     shutil.which("python3") or ""):
            if cand and os.path.isfile(cand) and "venv" not in cand:
                interp = cand
                break
        # aria2c often lives outside the path a unit inherits, so pin it.
        path_parts = ["/usr/local/bin", "/usr/bin", "/bin"]
        brew = "/home/linuxbrew/.linuxbrew/bin"
        if os.path.isdir(brew):
            path_parts.insert(0, brew)
        body = f"""[Unit]
Description=HardlyReading (Internet Archive magazine, comic and PDF library)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={here}
ExecStart={interp} -B server.py --port {getattr(self, "port", 8788)}
Environment=PATH={':'.join(path_parts)}
Environment=IA_ARIA2={aria2c_path() or ''}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
        try:
            os.makedirs(unit_dir, exist_ok=True)
            with open(unit, "w") as fh:
                fh.write(body)
        except OSError as e:
            return (False, f"Cannot write {unit}: {e}")
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"],
                           capture_output=True, timeout=25)
            r = subprocess.run(["systemctl", "--user", "enable", self.UNIT_NAME],
                               capture_output=True, timeout=25, text=True)
            if r.returncode != 0:
                return (False, (r.stderr or r.stdout or "systemctl failed").strip()[:200])
        except Exception as e:
            return (False, str(e))
        return (True, "Starts with your session. Downloads interrupted by a "
                      "restart resume from where they stopped.")

    def favourites(self) -> set:
        """Book keys marked as favourites."""
        try:
            conn = self._db()
            if not conn:
                return set()
            return {r[0] for r in conn.execute("SELECT book FROM favourites")}
        except Exception:
            return set()

    def hidden_keys(self) -> set:
        """Book keys filed away — still in the library, just not shown."""
        try:
            conn = self._db()
            if not conn:
                return set()
            return {r[0] for r in conn.execute("SELECT book FROM hidden")}
        except Exception:
            return set()

    def set_hidden(self, book_key: str, on: bool):
        try:
            conn = self._db()
            if not conn:
                return
            with self._lock_db:
                if on:
                    conn.execute("INSERT OR REPLACE INTO hidden (book, added) "
                                 "VALUES (?,?)", (book_key, time.time()))
                else:
                    conn.execute("DELETE FROM hidden WHERE book=?", (book_key,))
                conn.commit()
        except Exception:
            pass

    def set_favourite(self, book_key: str, on: bool):
        try:
            conn = self._db()
            if not conn:
                return
            with self._lock_db:
                if on:
                    conn.execute("INSERT OR REPLACE INTO favourites (book, added) "
                                 "VALUES (?,?)", (book_key, time.time()))
                else:
                    conn.execute("DELETE FROM favourites WHERE book=?", (book_key,))
                conn.commit()
        except Exception:
            pass

    def cached_desc(self, ident: str, max_age: float = 7 * 86400):
        """A description from the index, or None when it is absent or stale.

        An empty string is cached too: "archive.org has nothing for this item"
        is an answer worth remembering, and without it every open would ask
        again for the items that will never have one.
        """
        try:
            conn = self._db()
            if not conn:
                return None
            row = conn.execute(
                "SELECT desc, updated FROM descriptions WHERE ident=?",
                (ident.lower(),)).fetchone()
            if not row:
                return None
            if time.time() - float(row[1] or 0) > max_age:
                return None
            return row[0] or ""
        except Exception:
            return None

    def store_desc(self, ident: str, desc: str):
        try:
            conn = self._db()
            if not conn:
                return
            with self._lock_db:
                conn.execute(
                    "INSERT INTO descriptions (ident, desc, updated) VALUES (?,?,?) "
                    "ON CONFLICT(ident) DO UPDATE SET desc=excluded.desc, "
                    "updated=excluded.updated",
                    (ident.lower(), desc or "", time.time()))
                conn.commit()
        except Exception:
            pass

    def backfill_descriptions(self):
        """Give existing jobs the blurb they were queued without.

        Runs once at startup, in the background, and only for jobs that have an
        identifier but no description. A job whose items are already on disk
        still gets the text, so its library row can show it.
        """
        try:
            if _SHUTDOWN.wait(2):  # let the server come up first
                return
            for job in list(self.jobs):
                if _SHUTDOWN.is_set():
                    return
                if job.desc:
                    continue
                ident = next((i.get("identifier") for i in job.items
                              if i.get("identifier")), "")
                if not ident:
                    continue
                try:
                    m = http_json(META_API.format(ident))
                except Exception:
                    continue
                md = m.get("metadata") or {}
                desc = useful_desc(plain_text(md.get("description")),
                                   md.get("title") or job.label)
                if not desc:
                    continue
                with self.lock:
                    job.desc = desc
                    # Library rows read the description off their entry, so the
                    # items need it too.
                    for it in job.items:
                        it.setdefault("desc", desc)
                self.save_state()
        except Exception:
            pass               # never let a nicety break startup

    # ---- persistence
    def _load_state(self):
        try:
            with open(STATE_FILE) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return
        self.settings.update(d.get("settings") or {})
        # Categories are just labels; a corrupt entry is dropped rather than
        # taking the queue with it.
        for c in (d.get("categories") or []):
            if isinstance(c, dict) and c.get("id") and c.get("name"):
                self.categories.append({"id": str(c["id"]), "name": str(c["name"]),
                                        "pinned": bool(c.get("pinned"))})
        for sh in (d.get("shelf") or []):
            if isinstance(sh, dict) and sh.get("ident"):
                self.shelf.append({"ident": str(sh["ident"]),
                                   "name": str(sh.get("name") or sh["ident"]),
                                   "desc": str(sh.get("desc") or ""),
                                   "added": float(sh.get("added") or 0)})
        for jd in d.get("jobs", []):
            try:
                j = Job(**{k: v for k, v in jd.items()
                           if k in Job.__dataclass_fields__})
                # Interrupted work resumes rather than vanishing. A job that had
                # already resolved keeps its manifest and goes straight back to
                # downloading — re-resolving would re-walk every /metadata
                # (minutes on a large bundle) and lose the plan the user saw.
                if j.status in ("downloading", "verifying"):
                    if j.items and any(not i.get("skip") for i in j.items):
                        j.status = "resume"
                        j.message = "resuming after restart"
                    else:
                        j.status = "queued"
                        j.message = "resumed after restart"
                elif j.status == "resolving":
                    j.status = "queued"
                    j.message = "resumed after restart"
                self.jobs.append(j)
            except TypeError:
                continue
        # Restore the saved order, then append any job the file did not know
        # about, so a hand-edited or older state file cannot hide work.
        saved = d.get("order") or []
        known = {j.id for j in self.jobs}
        self.order = [jid for jid in saved if jid in known]
        self.order += [j.id for j in self.jobs if j.id not in self.order]

    # ---- reading progress (SQLite)
    def _db(self):
        """Lazy SQLite handle for reading progress.

        Separate from state.json: progress changes on every page turn, and
        rewriting the whole queue file for that would be absurd. WAL mode so a
        reader poll and a queue save never block each other.
        """
        if getattr(self, "_conn", None) is not None:
            return self._conn
        try:
            os.makedirs(os.path.dirname(HISTORY_DB) or ".", exist_ok=True)
            conn = sqlite3.connect(HISTORY_DB, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS progress (
                    book    TEXT PRIMARY KEY,
                    page    INTEGER NOT NULL DEFAULT 0,
                    total   INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL
                )""")
            # Descriptions live in the same index: they are metadata that never
            # changes, and re-asking archive.org for every row on every open is
            # both slow and rude.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS favourites (
                    book    TEXT PRIMARY KEY,
                    added   REAL NOT NULL
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS descriptions (
                    ident   TEXT PRIMARY KEY,
                    desc    TEXT NOT NULL DEFAULT '',
                    updated REAL NOT NULL
                )""")
            # Items the user has filed away: kept in the library data, dropped
            # from the default view. Same key as favourites.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hidden (
                    book    TEXT PRIMARY KEY,
                    added   REAL NOT NULL
                )""")
            conn.commit()
            self._conn = conn
        except Exception:  # noqa: BLE001
            # A broken DB must never break downloading.
            self._conn = None
        return self._conn

    def get_progress(self, book_key: str) -> int:
        """Reading position, keyed by a STABLE identity — not a job id.

        A job id is random and a filename can change if the same issue is
        re-downloaded through a different listing, so neither survives a
        second device or a re-pull. The key is therefore derived from the
        archive.org identifier plus the artefact, which is the same on every
        machine, and from the output name as a fallback for files with no
        identifier (hand-added ones).
        """
        c = self._db()
        if not c:
            return 0
        try:
            row = c.execute("SELECT page FROM progress WHERE book=?",
                            (book_key,)).fetchone()
            return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    def set_progress(self, book_key: str, page: int, total: int = 0):
        c = self._db()
        if not c:
            return
        try:
            with self._lock_db:
                c.execute(
                    "INSERT INTO progress (book, page, total, updated) "
                    "VALUES (?,?,?,?) ON CONFLICT(book) DO UPDATE SET "
                    "page=excluded.page, total=excluded.total, "
                    "updated=excluded.updated",
                    (book_key, int(page), int(total), time.time()))
                c.commit()
        except Exception:  # noqa: BLE001
            pass

    def save_state(self):
        with self.lock:
            d = {"settings": self.settings,
                 # The queue order is user-controlled now, so it has to survive
                 # a restart separately from the jobs themselves.
                 "order": [jid for jid in self.order
                           if any(j.id == jid for j in self.jobs)],
                 "categories": self.categories,
                 "shelf": self.shelf,
                 "jobs": [asdict(j) for j in self.jobs]}
            tmp = STATE_FILE + ".tmp"
            try:
                os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
                with open(tmp, "w") as fh:
                    json.dump(d, fh, indent=1)
                os.replace(tmp, STATE_FILE)
            except OSError:
                # A state write must never take the app down; the queue is still
                # in memory and the next save will retry.
                pass

    # ---- shelf: kept titles with nothing downloaded

    def shelf_ids(self) -> list:
        return [s["ident"] for s in self.shelf if s.get("ident")]

    def shelf_add(self, ident: str, name: str = "", desc: str = "") -> bool:
        """Keep an archive.org item in the library without queueing a download.

        Stored by identifier, so re-adding is idempotent and the entry survives
        a restart. Reading streams from archive.org; nothing touches the disk.
        """
        ident = (ident or "").strip()
        if not ident:
            return False
        with self.lock:
            for s in self.shelf:
                if s.get("ident") == ident:
                    s["name"] = name or s.get("name") or ident
                    if desc:
                        s["desc"] = desc
                    break
            else:
                self.shelf.append({"ident": ident, "name": name or ident,
                                   "desc": desc or "", "added": time.time()})
        self.save_state()
        return True

    def shelf_remove(self, ident: str) -> bool:
        ident = (ident or "").strip()
        if not ident:
            return False
        with self.lock:
            before = len(self.shelf)
            self.shelf = [s for s in self.shelf if s.get("ident") != ident]
            gone = len(self.shelf) != before
        if gone:
            self.save_state()
        return gone

    # ---- categories

    MAX_PINNED = 3

    def _cat_slug(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "category"
        taken = {c["id"] for c in self.categories}
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    def _cats_normalised(self):
        """Pinned first, otherwise the order the user arranged. The sort is
        stable, so each group keeps its own internal order."""
        self.categories.sort(key=lambda c: 0 if c.get("pinned") else 1)

    def add_category(self, name: str):
        name = (name or "").strip()
        if not name:
            raise ValueError("a category needs a name")
        if any(c["name"].lower() == name.lower() for c in self.categories):
            raise ValueError("that category already exists")
        cat = {"id": self._cat_slug(name), "name": name, "pinned": False}
        self.categories.append(cat)
        self.save_state()
        return cat

    def rename_category(self, cid: str, name: str):
        name = (name or "").strip()
        if not name:
            raise ValueError("a category needs a name")
        # The same rule as add: two tabs with one name are a bug, not a feature.
        if any(c["id"] != cid and c["name"].lower() == name.lower()
               for c in self.categories):
            raise ValueError("that category already exists")
        for c in self.categories:
            if c["id"] == cid:
                c["name"] = name
                self.save_state()
                return c
        raise ValueError("no such category")

    def delete_category(self, cid: str):
        """Deleting a category never deletes a job: those jobs go back to
        uncategorised."""
        before = len(self.categories)
        self.categories = [c for c in self.categories if c["id"] != cid]
        if len(self.categories) == before:
            raise ValueError("no such category")
        for j in self.jobs:
            if j.category == cid:
                j.category = ""
        self.save_state()
        return True

    def pin_category(self, cid: str, on: bool):
        cat = next((c for c in self.categories if c["id"] == cid), None)
        if not cat:
            raise ValueError("no such category")
        if on and not cat.get("pinned"):
            if sum(1 for c in self.categories if c.get("pinned")) >= self.MAX_PINNED:
                raise ValueError(f"only {self.MAX_PINNED} categories can be pinned")
            cat["pinned"] = True
        elif not on:
            cat["pinned"] = False
        self._cats_normalised()
        self.save_state()
        return cat

    def move_category(self, cid: str, delta: int):
        idx = next((i for i, c in enumerate(self.categories) if c["id"] == cid), None)
        if idx is None:
            raise ValueError("no such category")
        j = idx + delta
        if j < 0 or j >= len(self.categories):
            return False
        before = [c["id"] for c in self.categories]
        self.categories[idx], self.categories[j] = self.categories[j], self.categories[idx]
        # Pinned first is a rule, not a preference: a swap that would push an
        # unpinned category above a pinned one is undone, and reported as "no
        # move" rather than claiming to have done something.
        self._cats_normalised()
        if [c["id"] for c in self.categories] == before:
            return False
        self.save_state()
        return True

    def set_job_category(self, jid: str, cid: str):
        if cid and not any(c["id"] == cid for c in self.categories):
            raise ValueError("no such category")
        j = self.get(jid)
        if not j:
            raise ValueError("no such job")
        j.category = cid or ""
        j.updated = time.time()
        self.save_state()
        return j.category

    # ---- queue
    def add(self, url: str, limit: int = 0, want_order: str = "oldest", category: str = ""):
        c = classify_url(url)
        if not c:
            raise ValueError("Empty link")
        kind, query, label = c
        # A limit only makes sense for a collection or a search query: one item
        # URL is already exactly one issue.
        limit = max(0, int(limit or 0))
        want_order = "newest" if str(want_order).lower().startswith("new") else "oldest"
        # New jobs are uncategorised unless the tab you added from asked for a
        # category. An id that no longer exists would be a dangling label.
        category = category if any(c2["id"] == category for c2 in self.categories) else ""
        with self.lock:
            for j in self.jobs:
                if j.status != "cancelled" and j.query == query and j.kind == kind:
                    raise ValueError("Already in the list")
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, source=url,
                      label=label or query, query=query,
                      limit=limit, want_order=want_order, category=category)
            self.jobs.append(job)
            self.order.append(job.id)
            self.save_state()
        self.kick()
        return job.summary()

    def collection_items(self, ident: str, offset: int = 0, rows: int = 100,
                         order: str = "oldest") -> dict:
        """One window of a collection's members, for the search drill-down.

        Same `collection:<id>` scoping the queue uses, so what you see here is
        what a queued collection would have resolved to.
        """
        ident = (ident or "").strip()
        if not IDENT_RE.match(ident):
            raise ValueError("invalid collection id")
        rows = max(1, min(200, int(rows or 100)))
        # archive.org pages in whole pages: land the request on a page boundary
        # so `offset` and what comes back always agree.
        offset = max(0, int(offset or 0))
        offset -= offset % rows
        want = "newest" if str(order).lower().startswith("new") else "oldest"
        sort = "date desc" if want == "newest" else "date asc"
        res = search_page(f"collection:{ident}", rows=rows,
                          page=offset // rows + 1, sort=sort, title_only=False,
                          extra_fields=["date", "item_size"])
        have = self.covered()
        items = []
        for d in res.get("docs") or []:
            iid = d.get("identifier") or ""
            if not iid:
                continue
            c = have.get(iid) or {}
            items.append({
                "id": iid,
                "title": d.get("title") or iid,
                "date": d.get("date") or "",
                "year": d.get("year") or "",
                "size": int(d.get("item_size") or 0),
                "have": c.get("status") or "",
                "job": c.get("job") or "",
            })
        return {"identifier": ident, "total": int(res.get("numFound") or 0),
                "offset": offset, "rows": rows, "order": want, "items": items}

    def add_many(self, idents, category: str = ""):
        """Queue individual items by identifier, in one save.

        The collection browser adds exactly the issues picked, not the whole
        collection, so each identifier becomes its own `item` job. Anything
        already queued is reported rather than raised: picking one issue you
        already have should not fail the other nineteen.
        """
        category = category if any(c["id"] == category for c in self.categories) else ""
        added, skipped, invalid = [], [], []
        with self.lock:
            present = {j.query for j in self.jobs
                       if j.kind == "item" and j.status != "cancelled"}
            for raw in idents:
                ident = str(raw or "").strip()
                if not IDENT_RE.match(ident):
                    invalid.append(str(raw or ""))
                    continue
                if ident in present:
                    skipped.append(ident)
                    continue
                job = Job(id=uuid.uuid4().hex[:12], kind="item",
                          source=f"https://archive.org/details/{ident}",
                          label=ident, query=ident, category=category)
                self.jobs.append(job)
                self.order.append(job.id)
                present.add(ident)
                added.append(job.summary())
            if added:
                self.save_state()
        if added:
            self.kick()
        return {"added": added, "skipped": skipped, "invalid": invalid}

    def download_root(self) -> str:
        """The folder downloads land in, with the setting's empty value resolved.

        An empty or whitespace setting means "the default". Without this, the
        join below silently means the process working directory — which on a
        launcher means files appear inside the app folder.
        """
        # IA_DOWNLOAD_DIR wins: it is what a test run or a second instance uses
        # to stay away from the real library.
        d = (os.environ.get("IA_DOWNLOAD_DIR") or "").strip()
        if d:
            return d
        d = (self.settings.get("download_dir") or "").strip()
        return d or default_download_dir()

    def get(self, job_id):
        for j in self.jobs:
            if j.id == job_id:
                return j
        return None

    def snapshot(self):
        """Everything the UI polls for: jobs, live job id, pause flag, settings
        and per-status counts. Built defensively — one bad job yields an error
        card rather than an empty queue."""
        with self.lock:
            # Emit in run order, not insertion order: the queue is reorderable,
            # so the position in this array is what the run order actually is.
            order_index = {jid: i for i, jid in enumerate(self.order)}
            # One malformed job must not blank the whole UI. A summary that
            # throws is reported as an error card instead of killing the poll.
            jobs = []
            for j in sorted(self.jobs,
                            key=lambda j: order_index.get(j.id, len(order_index))):
                try:
                    s = j.summary()
                    s["pos"] = order_index.get(j.id, -1) + 1
                    jobs.append(s)
                except Exception as exc:  # noqa: BLE001
                    jobs.append({"id": getattr(j, "id", "?"), "status": "failed",
                                 "label": getattr(j, "label", "?"),
                                 "error": f"state error: {exc}", "n_items": 0,
                                 "total_bytes": 0, "done_bytes": 0,
                                 "message": "", "skipped": 0,
                                 "pos": order_index.get(getattr(j, "id", ""), -1) + 1})
            return {
                "jobs": jobs,
                "current": (sorted(self.running)[0] if self.running else None),
                # Everything downloading right now, so the header can say
                # "3 downloading" instead of one job's name.
                "running_jobs": sorted(self.running),
                "parallel_jobs": self.target_workers(),
                "paused": self.paused,
                "busy": bool(self.running),
                "settings": dict(self.settings),
                # Tab order as the user arranged it; pinned ones are already
                # first because pinning re-sorts the list.
                "categories": [dict(c) for c in self.categories],
                "max_pinned": self.MAX_PINNED,
                # Identifiers kept in the library without a file, so a search
                # card can say "in library" instead of offering to add again.
                "shelf": [s["ident"] for s in self.shelf if s.get("ident")],
                # The UI shows this when the folder field is left empty, so an
                # empty box is explained instead of looking like "no folder".
                "download_default": default_download_dir(),
                "counts": {
                    "queued": sum(1 for j in self.jobs if j.status == "queued"),
                    "active": sum(1 for j in self.jobs if j.status in
                                  ("resolving", "downloading", "verifying")),
                    "done": sum(1 for j in self.jobs if j.status == "done"),
                    "failed": sum(1 for j in self.jobs if j.status == "failed"),
                },
            }

    # ---- worker
    FOLDER_PREFIX = "folder:"
    # Library rows that exist only on the shelf, not on disk.
    SHELF_PREFIX = "shelf:"

    def folder_path(self, series):
        """Resolve a `folder:<series>` pseudo-id to a directory under the
        download root. Returns None if it would escape that root."""
        root = os.path.realpath(self.settings.get("download_dir")
                                or default_download_dir())
        target = os.path.realpath(os.path.join(root, series or ""))
        if target != root and not target.startswith(root + os.sep):
            return None
        return target if os.path.isdir(target) else None

    def scan_folder(self):
        """Files on disk that no job accounts for.

        The library is built from job state, so a removed job (or a lost
        state.json) used to hide files that were still sitting in the download
        folder. This walks the folder so whatever is actually there shows up.
        """
        root = self.settings.get("download_dir") or default_download_dir()
        if not os.path.isdir(root):
            return []
        exts = (".cbz", ".cbr", ".cbt", ".zip", ".pdf")
        out = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            series = os.path.relpath(dirpath, root)
            if series == ".":
                series = "Loose files"
            for fn in filenames:
                if not fn.lower().endswith(exts):
                    continue
                p = os.path.join(dirpath, fn)
                if not os.path.isfile(p):
                    continue
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                out.append({"series": series, "name": fn, "size": size})
        return out

    def covered(self):
        """Map archive identifier -> queue state, for the search grid.

        The UI needs to know before you click a card whether that item is
        already queued, downloading or sitting in the library. Identifiers only
        exist once a job has resolved, so this walks resolved entries.
        """
        out = {}
        with self.lock:
            for j in self.jobs:
                for e in j.items:
                    ident = e.get("identifier")
                    if not ident:
                        continue
                    prev = out.get(ident)
                    # A finished job outranks an older cancelled one for the
                    # same item, otherwise the badge flickers between visits.
                    if prev and prev["status"] == "done":
                        continue
                    out[ident] = {
                        "job": j.id,
                        "status": j.status,
                        "file": e.get("outname") or "",
                        "skip": bool(e.get("skip")),
                    }
        return out

    def target_workers(self) -> int:
        """How many workers the queue should be running, clamped to 1–4."""
        try:
            n = int(self.settings.get("parallel_jobs", 2))
        except (TypeError, ValueError):
            n = 2
        return max(1, min(4, n))

    def file_concurrency(self) -> int:
        """aria2 children per job.

        The total is what matters for a slow machine, so several jobs at once
        share the same budget instead of multiplying it: 12 in flight in total,
        not 12 each.
        """
        try:
            total = int(self.settings.get("jobs_in_flight", 12))
        except (TypeError, ValueError):
            total = 12
        return max(1, round(total / self.target_workers()))

    def kick(self):
        """Start enough workers to run the configured number of jobs at once."""
        with self.lock:
            self._workers = [t for t in getattr(self, "_workers", []) if t.is_alive()]
            want = self.target_workers()
            for _ in range(max(0, want - len(self._workers))):
                t = threading.Thread(target=self._run, daemon=True,
                                     name="job-worker")
                t.start()
                self._workers.append(t)

    def move_to(self, job_id: str, index: int) -> bool:
        """Put a waiting job at `index` in the run order.

        The index is clamped, not rejected: "move to bottom" from the last row
        should be a no-op, not an error. Only work that has not started can be
        reordered — promising a new position to a running download would be a
        claim the file cannot honour.
        """
        with self.lock:
            if job_id not in self.order:
                return False
            job = self.get(job_id)
            if not job or job.status not in ("planned", "queued", "resume", "failed"):
                return False
            self.order.remove(job_id)
            index = max(0, min(index, len(self.order)))
            self.order.insert(index, job_id)
            self.save_state()
            return True

    def move(self, job_id: str, delta: int) -> bool:
        """Shift a waiting job by `delta` places, e.g. -1 for one step up."""
        if job_id not in self.order:
            return False
        return self.move_to(job_id, self.order.index(job_id) + delta)

    def _has_pending(self) -> bool:
        with self.lock:
            return bool(self._claimed) or any(
                j.status in ("queued", "resume") for j in self.jobs)

    def _next_queued(self, claim: bool = False):
        """First runnable job in queue order, optionally claimed.

        Claiming matters once several workers share the queue: two threads
        must not walk into the same job. A claim lives only in memory — a
        restart simply offers the job again.
        """
        with self.lock:
            now = time.time()
            for jid in self.order:
                j = self.get(jid)
                if not j or jid in self._claimed:
                    continue
                if j.status == "resume":
                    if claim:
                        self._claimed.add(jid)
                    return j
                if j.status == "queued":
                    # A retryable failure waits on the ladder before trying again
                    if j.next_try_at and now < j.next_try_at:
                        continue
                    if claim:
                        self._claimed.add(jid)
                    return j
        return None

    def _run(self):
        """One worker: take a due job, process or resume it, sleep, repeat.

        Several of these run at once (see `parallel_jobs`). A worker exits when
        there is nothing left that could run — claimed jobs belong to other
        workers, and a retry due in an hour is not a reason to idle forever.
        """
        while not _SHUTDOWN.is_set():
            if self.paused:
                _SHUTDOWN.wait(0.5)
                continue
            job = self._next_queued(claim=True)
            if not job:
                with self.lock:
                    busy_elsewhere = bool(self._claimed)
                if busy_elsewhere:
                    _SHUTDOWN.wait(1)
                    continue
                _SHUTDOWN.wait(2)   # nothing runnable now; a retry may become due
                if not self._has_pending():
                    with self.lock:
                        self._workers = [t for t in self._workers
                                         if t is not threading.current_thread()]
                    return
                continue
            # One bad job must never stop the queue. Anything that escapes a
            # job's own error handling is recorded against that job and the
            # loop moves on, which is the whole point of a queue.
            try:
                with self.lock:
                    self.running.add(job.id)
                if job.status == "resume":
                    entries = [i for i in job.items if not i.get("skip")]
                    self._download(job, entries)
                else:
                    self._process(job)
            except Exception as exc:  # noqa: BLE001
                with self.lock:
                    job.status = "failed"
                    job.error = str(exc)
                    job.message = "stopped unexpectedly"
                    job.updated = time.time()
                    self.save_state()
            finally:
                with self.lock:
                    self.running.discard(job.id)
                    self._claimed.discard(job.id)
                    self.save_state()
            # Politeness gap to archive.org, per worker. With two workers the
            # effective rate is two jobs per gap, which is the point.
            time.sleep(min(self.settings.get("between_jobs", 30), 60))

    def _process(self, job: Job):
        with self.lock:
            with self.lock:
                self.running.add(job.id)
            job.status = "resolving"
            job.message = "Looking up items…"
            job.updated = time.time()
            self.save_state()

        def progress(msg):
            job.message = msg
            job.updated = time.time()

        try:
            items = Resolver(engine=self).resolve(job, progress=progress)
            ok = [i for i in items if not i.get("skip")]

            # --- Komga layout: one folder per series, files named so they
            # natural-sort in reading order. Applied to the entries themselves
            # so the download, the verify step and the on-disk names all agree.
            if self.settings.get("komga_naming", True) and ok:
                series = listing_series(job.label, job.scoped_query or job.query, ok)
                ok = dedupe_formats(ok)
                series, ok = plan_filenames(ok, series)
                job.series = series
            else:
                for e in ok:
                    e["outname"] = e.get("outname") or safe_name(
                        e.get("file") or e["identifier"])
                job.series = safe_name(job.label)

            # --- how many did they ask for? `ok` is in reading order by now,
            # so taking a slice of it means "the first 25 issues" or "the newest
            # 25", not whichever ones archive.org happened to list first.
            job.available = len(ok)
            if job.limit and len(ok) > job.limit:
                if job.want_order == "newest":
                    ok = ok[-job.limit:]
                else:
                    ok = ok[:job.limit]

            # The named entries are what gets downloaded and verified, so they
            # are what the job must hold. plan_filenames returns copies, so
            # assigning `items` would leave every entry without an `outname`.
            kept_ids = {e.get("identifier") for e in ok}
            skipped = [i for i in items if i.get("skip")
                       or i.get("identifier") not in kept_ids]
            # Anything dropped by the count the user chose should say so;
            # otherwise the item list reads as though they already had it.
            for n, i in enumerate(skipped):
                if i.get("identifier") not in kept_ids and not i.get("skip"):
                    i = dict(i)
                    i["skip"] = True
                    i["reason"] = (f"over the {job.limit} you asked for"
                                   if job.limit else "not selected")
                    skipped[n] = i

            # --- delta-vs-folder: drop anything already on disk, matched by
            # archive's own checksum rather than by filename. Renaming a file
            # or re-downloading a collection under a new label must not fetch
            # the same bytes twice. Entries with no md5 are always kept.
            job.dir = os.path.join(self.download_root(),
                                   safe_name(job.series or job.label))
            if self.settings.get("skip_existing", True) and job.dir:
                have = md5_index(job.dir)
                if have:
                    already, fresh = [], []
                    for e in ok:
                        m = (e.get("md5") or "").lower()
                        if m and m in have:
                            e = dict(e)
                            e["skip"] = True
                            e["reason"] = f"already have it ({have[m]})"
                            already.append(e)
                        else:
                            fresh.append(e)
                    if already:
                        ok = fresh
                        skipped = skipped + already

            job.items = ok + skipped
            job.total_bytes = sum(i.get("size", 0) for i in ok)

            # Nothing left to fetch — every entry is already on disk, or none
            # was downloadable. Offering a Download button for zero files is a
            # dead end, so finish the job instead of waiting for a confirm.
            if not ok:
                if skipped:
                    # Everything was already on disk: that is success, not a
                    # dead end with a Download button that would do nothing.
                    job.status = "done"
                    job.message = f"already have all {len(skipped)} item(s)"
                else:
                    # Nothing resolved at all — a bad identifier or an item
                    # with no PDF/CBZ/rar. Say so instead of reporting success.
                    job.status = "failed"
                    job.error = "no downloadable files"
                    job.message = "nothing to download"
                job.updated = time.time()
                with self.lock:
                    self.save_state()
                return

            # Known now, not at download time, so the plan screen can show the
            # destination and warn about free space before anything starts.
            job.status = "planned"
            job.message = (f"{len(ok)} item{'s' if len(ok) != 1 else ''} ready"
                           f" · {human_size(job.total_bytes)}")
            job.updated = time.time()
            with self.lock:
                self.save_state()

            if self.settings.get("review_before_download", False):
                return  # opt-in: wait for the user to confirm on the plan screen
            self._download(job, ok)
        except Exception as e:  # noqa: BLE001
            job.status = "failed"
            job.error = str(e)
            job.message = "Could not resolve"
        finally:
            job.updated = time.time()
            with self.lock:
                self.running.discard(job.id)
                self.save_state()

    # ---- download
    def _download(self, job: Job, entries):
        aria = aria2c_path()
        # The folder is the Komga SERIES, not the raw job label, so a series
        # pulled in two passes lands in one folder instead of two.
        dest = os.path.join(self.download_root(),
                            safe_name(job.series or job.label))
        os.makedirs(dest, exist_ok=True)
        job.dir = dest

        # Only fetch what is not already complete. aria2's `-c` resumes a
        # partial file, but this skips finished ones entirely — that is what
        # makes a restart after a crash cheap instead of re-walking the listing.
        pending = []
        for e in entries:
            p = os.path.join(dest, e["outname"])
            try:
                if os.path.exists(p) and (not e.get("size")
                                          or os.path.getsize(p) == e["size"]):
                    continue
            except OSError:
                pass
            pending.append(e)

        with self.lock:
            job.status = "downloading"
            job.message = (f"Downloading {len(pending)} file(s)…"
                           if pending else "Checking existing files…")
            job.done_bytes = 0
            self.save_state()

        if aria is None:
            job.status = "failed"
            job.fatal = True
            job.error = ("aria2c was not found. Install it, or put aria2c in this "
                         "app's bin/ folder.")
            self.save_state()
            return

        # Download in chunks that fit. Handing aria2 the whole list means it
        # fills the disk partway through and leaves truncated files behind; this
        # way the job stops BEFORE the first file that will not fit, in a state
        # that can be resumed once space is freed.
        reserve = int(self.settings.get("space_reserve_mb", SPACE_RESERVE_MB)) * 1024 * 1024
        while pending:
            free = free_bytes(dest)
            # 0 means "could not tell" (a permission problem, an odd filesystem),
            # not "no room". Refusing to download because the check failed would
            # read as a full disk that is not full — let aria2 decide instead.
            check_space = free > 0
            take, acc = [], 0
            for e in pending:
                size = int(e.get("size") or 0)
                if check_space and size and acc + size + reserve > free:
                    break
                take.append(e)
                acc += size
            if not take and check_space:
                # Even the next file alone does not fit: pause rather than
                # start and fail. The job keeps its place in the queue.
                need = int(pending[0].get("size") or 0)
                with self.lock:
                    job.status = "paused"
                    job.pause_reason = "space"
                    job.live = {}
                    job.speed = 0.0
                    job.message = (f"Paused — needs {human_size(need)} "
                                   f"but only {human_size(free)} free")
                    job.updated = time.time()
                    self.save_state()
                return
            self._run_aria(aria, job, dest, take)
            pending = pending[len(take):]
        # (the single-run path below is replaced by _run_aria)

        job.done_bytes = job.total_bytes
        job.live = {}
        self._drop_aria(dest)

        # --- verify: a job is done ONLY if every file checks out
        job.status = "verifying"
        job.message = "Checking files…"
        self.save_state()
        good, problems = verify_manifest(dest, entries)

        verdict, note = classify_failure(problems, len(entries))

        if verdict == "ok":
            job.status = "done"
            job.message = f"{good} file(s) · {human_size(job.total_bytes)}"
            job.error = ""
        elif verdict == "tail":
            # A small tail is normal archive.org noise. The work is done;
            # report what is missing and stop rather than retrying forever.
            job.status = "done"
            job.message = f"{good}/{len(entries)} downloaded · {note}"
            job.error = ""
        elif failure_is_fatal(note):
            job.status = "failed"
            job.fatal = True
            job.message = note
            job.error = "This item cannot be downloaded"
        else:
            job.retries += 1
            delay = next_retry_delay(job.retries - 1)
            job.next_try_at = time.time() + delay
            job.status = "queued"
            job.message = (f"retrying in {human_eta(delay)} "
                           f"(attempt {job.retries + 1}) · {note}")
        self.save_state()

    @staticmethod
    def _drop_aria(dest):
        """Remove the aria2 input file once the batch it describes is done."""
        try:
            os.unlink(os.path.join(dest, "aria.txt"))
        except OSError:
            pass

    def _run_aria(self, aria, job, dest, entries):
        """Run aria2 for one batch of files, streaming its progress into the job."""
        write_aria_input(entries, os.path.join(dest, "aria.txt"))
        self.save_state()

        # Shared budget across concurrent jobs, so "3 jobs at once" means more
        # parallelism at the job level, not 36 sockets.
        cmd = ([aria, "-i", "aria.txt",
                "-j", str(self.file_concurrency())]
               + ARIA_FLAGS + ["--summary-interval", "5"])

        proc = subprocess.Popen(cmd, cwd=dest, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        while True:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if not line:
                continue
            m = re.search(r"\((\d{1,3})%\)", line)
            if m:
                job.done_bytes = int(job.total_bytes * int(m.group(1)) / 100)
            # [#gid 4.5MiB/41MiB(11%) CN:1 DL:1.2MiB ETA:35s]
            f = re.match(r"\[#(\w+)\s+([\d.]+\w+)/([\d.]+\w+)\((\d{1,3})%\)"
                         r"(?:.*?DL:([\d.]+\w+))?", line)
            if f:
                gid, done, total, pct, sp = f.groups()
                job.live[gid] = {"gid": gid, "pct": int(pct),
                                 "done": _parse_speed(done),
                                 "total": _parse_speed(total),
                                 "speed": _parse_speed(sp) if sp else 0}
            sp = re.search(r"DL:([\d.]+\w+)", line)
            if sp:
                job.speed = _parse_speed(sp.group(1))
            job.updated = time.time()

        proc.wait()

    def resume(self, job_id: str) -> dict:
        """Restart a job that paused itself (out of space).

        Deliberately separate from Retry: a paused job is not a failure, and
        re-queueing it must not count as one more attempt on the retry ladder.
        If the disk is still too full it pauses again immediately, with the new
        numbers, rather than spending the job's retries discovering that.
        """
        with self.lock:
            j = self.get(job_id)
            if not j:
                raise ValueError("no such job")
            if j.status != "paused":
                raise ValueError("that job is not paused")
            # "resume" means the item list is already known: the worker goes
            # straight back into the download instead of re-walking a listing
            # that may be a thousand items long.
            j.status = "resume" if j.items else "queued"
            j.pause_reason = ""
            j.message = "picked up where it stopped"
            j.updated = time.time()
            self.save_state()
            out = j.summary()
        self.kick()
        return out

    def confirm(self, job_id):
        """User pressed Download on the plan screen."""
        with self.lock:
            j = self.get(job_id)
            if not j or j.status != "planned":
                raise ValueError("That job is not waiting for confirmation")
            entries = [i for i in j.items if not i.get("skip")]
        threading.Thread(target=self._confirmed_download,
                         args=(j, entries), daemon=True).start()
        return j.summary()

    def _confirmed_download(self, job, entries):
        with self.lock:
            with self.lock:
                self.running.add(job.id)
        try:
            self._download(job, entries)
        except Exception as e:  # noqa: BLE001
            job.status = "failed"
            job.error = str(e)
        finally:
            with self.lock:
                self.running.discard(job.id)
                self.save_state()

    def cancel(self, job_id):
        """Mark a not-yet-downloading job cancelled so it never runs."""
        with self.lock:
            j = self.get(job_id)
            if not j:
                raise ValueError("no such job")
            if j.status in ("downloading", "verifying", "resume"):
                raise ValueError("that download is already running")
            j.status = "cancelled"
            j.message = "cancelled"
            j.updated = time.time()
            self.save_state()
        return j.summary()

    def remove(self, job_id):
        """Dismiss a job from the list entirely; files on disk are untouched."""
        with self.lock:
            if not self.get(job_id):
                raise ValueError("no such job")
            self.jobs = [x for x in self.jobs if x.id != job_id]
            self.order = [i for i in self.order if i != job_id]
            self.running.discard(job_id)
            self._claimed.discard(job_id)
            self.save_state()
        return {"removed": job_id}

    def delete_file(self, job_id, filename):
        """Delete one downloaded file, its entry, and its reading position.

        Handles both a real job and a `folder:` pseudo-id, so a file the library
        found by scanning can be deleted the same way as a queued one.
        """
        base = os.path.basename(filename)

        if job_id.startswith(self.FOLDER_PREFIX):
            d = self.folder_path(job_id[len(self.FOLDER_PREFIX):])
            if not d:
                raise ValueError("no such folder")
            target = os.path.realpath(os.path.join(d, base))
            if not target.startswith(os.path.realpath(d)) or not os.path.isfile(target):
                raise ValueError("no such file")
            self._clear_progress("file:" + base.lower())
            os.remove(target)
            return {"deleted": base}

        j = self.get(job_id)
        if not j or not j.dir:
            raise ValueError("no such job")
        target = os.path.realpath(os.path.join(j.dir, base))
        if not target.startswith(os.path.realpath(j.dir)) or not os.path.isfile(target):
            raise ValueError("no such file")
        key = ""
        with self.lock:
            for e in j.items:
                if e.get("outname") == base:
                    key = book_key(e)
                    e["skip"] = True   # drop from library without deleting the job
            if key:
                self._clear_progress(key)
            self.save_state()
        os.remove(target)
        return {"deleted": base}

    def _clear_progress(self, book_key):
        c = self._db()
        if not c:
            return
        try:
            with self._lock_db:
                c.execute("DELETE FROM progress WHERE book=?", (book_key,))
                c.commit()
        except Exception:  # noqa: BLE001
            pass

    def clear_history(self):
        """Forget every saved reading position. Files on disk are untouched."""
        c = self._db()
        if not c:
            return {"cleared": 0}
        with self._lock_db:
            n = c.execute("SELECT COUNT(*) FROM progress").fetchone()[0]
            c.execute("DELETE FROM progress")
            c.commit()
        return {"cleared": n}


def _parse_speed(s: str) -> float:
    """aria2 prints speeds like 1.2MiB, 350KiB, 12B."""
    m = re.match(r"([\d.]+)([KMGT]?)i?B", s)
    if not m:
        return 0.0
    v = float(m.group(1))
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    return v * mult.get(m.group(2), 1)


def classify_failure(problems, entry_count):
    """Decide retryable vs fatal, and whether the tail is small enough to leave.

    A handful of failures out of a large run is normal archive.org noise — an
    empty metadata record, a dark item, one flaky storage node. That tail gets
    REPORTED, not investigated or retried forever.
    """
    if not problems:
        return "ok", ""

    n = len(problems)
    names = ", ".join(f"{a}" for a, _ in problems[:3])
    more = f" +{n - 3} more" if n > 3 else ""
    # A couple of misses on a big run: report and accept it.
    if n <= max(2, int(entry_count * 0.05)):
        return "tail", f"{n} of {entry_count} unavailable ({names}{more})"
    return "retry", f"{n} of {entry_count} failed ({names}{more})"


def failure_is_fatal(msg: str) -> bool:
    """Errors that will never succeed on retry."""
    m = (msg or "").lower()
    for sig in ("not found", "does not exist", "is dark", "restricted",
                "borrow", "lending", "no longer available"):
        if sig in m:
            return True
    return False


# Retry ladder in seconds: 15 min, 1 h, 3 h, 8 h.
RETRY_LADDER = (15 * 60, 60 * 60, 3 * 60 * 60, 8 * 60 * 60)


def next_retry_delay(attempt: int) -> float:
    """Backoff for a failed download attempt: 15m, 1h, 3h, then 8h and it stays
    there. Whether to keep retrying is decided separately by failure_is_fatal()
    — long waits are deliberate, archive.org outages run for hours."""

    if attempt < len(RETRY_LADDER):
        return RETRY_LADDER[attempt]
    return RETRY_LADDER[-1]


# ---------------------------------------------------------------- komga naming
#
# Komga's own rule (docs: "What happens during a Scan"): "Komga will create a
# Series for each subfolder ... and a Book for each file found, placed inside the
# Series corresponding to the parent folder of the file." So the ONLY thing that
# makes a tidy library is: one folder == one series, files named so they
# natural-sort in reading order inside it.
#
# Reading order: Komga natural-sorts filenames, so `001`/`002` order correctly
# and `10` does not land before `9`.

# Words that are packaging, not the publication's name. Stripped from the
# series folder so `The MAD Magazine Collection (ALL 550 ISSUES)` and
# `Mad Magazine (1 - 500)` both become `Mad Magazine`.
_NOISE_PARENS = re.compile(
    r"[\(\[]\s*(?:all\b[^)\]]*|full\b[^)\]]*|complete\b[^)\]]*|collection\b[^)\]]*"
    r"|issue[s]?\b[^)\]]*|\d+\s*(?:issues?|books?)\b[^)\]]*"
    r"|cbr\b[^)\]]*|pdf\b[^)\]]*"
    r"|\d+\s*[-–—]\s*\d+"
    r")\s*[\)\]]", re.I)

# " - Extra Books and Issues Collection {cbr+pdf}" — a descriptor tail, not the
# publication name. Matches a dash followed by words containing collection/
# extra/books/issues/archive/digital.
_SERIES_DESC = re.compile(
    r"\s*[-–—]\s*[^-–—]*\b(?:collection|extra|archive|digi[tz]al|books|issues)\b[^-–—]*$",
    re.I)

_SERIES_TAIL = re.compile(
    r"\s*[-–—]?\s*(?:issue|issues|vol|volume)\s*\d+\s*[-–—]\s*\d+\s*$", re.I)

_YEAR_RANGE = re.compile(r"\(\s*(\d{4})\s*[-–—]\s*(\d{4})\s*\)")

_BRACES = re.compile(r"\{[^}]*\}")


def series_name(title: str, fallback: str = "") -> str:
    """Derive ONE stable folder name for a listing's series.

    Every issue in a collection must land in the SAME folder or Komga shows a
    separate series per issue. That means dropping the per-issue bits — a year
    range, "Issue 01-24", an issue count — from the title.

    Measured on real titles: `MAD Magazine (2018-2022) Issue 01-24`,
    `Mad Magazine (1 - 500)` and `The MAD Magazine Collection (ALL 550 ISSUES)`
    must all resolve to the same folder, or one publication becomes three
    Komga series. Komga matches series by FOLDER NAME, and it is
    case-sensitive, so casing is normalised too.
    """
    t = (title or fallback or "").strip()
    t = _BRACES.sub(" ", t)          # {cbr+pdf}
    t = _NOISE_PARENS.sub(" ", t)    # (ALL 550 ISSUES), (1 - 500), (2018-2022)
    t = _SERIES_TAIL.sub("", t)      # trailing "Issue 01-24"
    t = _YEAR_RANGE.sub(" ", t)
    t = _SERIES_DESC.sub("", t)      # " - Extra Books and Issues Collection"
    # Drop a leading article: "The MAD Magazine Collection" is the same
    # publication as "MAD Magazine" as far as a reader is concerned.
    t = re.sub(r"^(?:the|a|an)\s+", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" -–—,.")
    t = title_case_series(t)
    return safe_name(t or fallback or "Untitled")


_SMALL_WORDS = {"and", "or", "the", "of", "in", "on", "at", "to", "for", "a", "an", "vs"}


def title_case_series(t: str) -> str:
    """Casing that collapses `MAD Magazine` / `Mad Magazine` / `mad magazine`.

    Not a strict Title Case — an already-mixed run like `McFarlane` is left
    alone; only the all-caps and all-lower shapes are normalised, so distinct
    stylings are not flattened into each other.
    """
    if not t:
        return t
    words = t.split(" ")
    if all(w.isupper() for w in words if w.isalpha()) or \
       all(w.islower() for w in words if w.isalpha()):
        out = []
        for i, w in enumerate(words):
            if not w.isalpha() or (i and w.lower() in _SMALL_WORDS):
                out.append(w.lower())
            else:
                out.append(w[:1].upper() + w[1:].lower())
        return " ".join(out)
    return t


def issue_number(filename: str):
    """Pull the issue number out of an archive.org file name.

    Real shapes seen in our pulls:
        MAD Magazine 001 (2018).cbr        -> 1
        MAD Magazine 022 (2021).cbr        -> 22
        MAD/1950s/1952/MAD Comic 001.pdf   -> 1
        Cracked22 1961.pdf                 -> 22
        MAD001.pdf                         -> 1

    The trailing 4-digit year is never the issue, and a leading year is never
    one either — those are filtered before choosing.
    """
    base = os.path.basename(filename)
    stem = os.path.splitext(base)[0]

    # Prefer an explicit #123 or "Issue 123"
    m = re.search(r"(?:#|\bissue\s*)(\d{1,4})\b", stem, re.I)
    if m:
        return int(m.group(1))

    # Otherwise every number NOT looking like a year/decade, in order of
    # preference: last one is usually the issue, earlier ones are years.
    nums = [(m.group(1), m.start()) for m in re.finditer(r"(?<!\d)(\d{1,4})(?!\d)", stem)]
    if not nums:
        return None

    def is_yearish(tok):
        n = int(tok)
        return (len(tok) == 4 and 1900 <= n <= 2099) or \
               (len(tok) == 4 and n % 10 == 0 and 1900 <= n <= 2100)

    candidates = [t for t in nums if not is_yearish(t[0])]
    if not candidates:
        return None
    return int(candidates[-1][0])


def issue_year(filename: str):
    m = re.search(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", os.path.basename(filename))
    return int(m.group(1)) if m else None


_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
_MONTH_ABBR = {m[:3].lower(): i for m, i in _MONTHS.items()}


def issue_date(text: str):
    """Find the publication date in a title or filename.

    archive.org's own `date` field is padded to Jan 1 for most scans, so it
    cannot order issues. The real date lives in the name:

        1985-03            1998-08-24        Vogue UK 2020-08
        December 1965      March 1985        MAD 180 1976
    """
    s = os.path.basename(text or "")
    # ISO-ish: 1985-03 or 1985-03-24 (also 1985_03)
    m = re.search(r"(?<!\d)(19\d{2}|20\d{2})[-_./](\d{1,2})(?:[-_./](\d{1,2}))?(?!\d)", s)
    if m:
        y = int(m.group(1))
        mo = int(m.group(2))
        day = int(m.group(3)) if m.group(3) else 0
        if 1 <= mo <= 12:
            return (y, mo, day)
    # Month name + year
    m = re.search(r"\b(" + "|".join(list(_MONTHS) + list(_MONTH_ABBR)) + r")[a-z]*\.?\s+(\d{4})\b",
                  s, re.I)
    if m:
        return (int(m.group(2)), _MONTHS.get(m.group(1).lower())
                or _MONTH_ABBR.get(m.group(1).lower()), 0)
    # Year + month name
    m = re.search(r"\b(\d{4})\s+(" + "|".join(list(_MONTHS) + list(_MONTH_ABBR)) + r")[a-z]*",
                  s, re.I)
    if m:
        return (int(m.group(1)), _MONTHS.get(m.group(2).lower())
                or _MONTH_ABBR.get(m.group(2).lower()), 0)
    # Bare year: still better than nothing for ordering
    m = re.search(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", s)
    if m:
        return (int(m.group(1)), 0, 0)
    return None


def komga_filename(series: str, filename: str, index: int = 0,
                   total: int = 0, year_fallback: int = 0) -> str:
    """`<Series> NNN (<Year>).ext` — sorts in reading order, reads cleanly.

    NNN is the issue number when there is one, zero-padded so `9` sorts before
    `10`. Only when the file carries no issue number at all does the running
    index take over, so the folder always sorts deterministically.

    The padding width grows for large numbers (999 -> 3 digits, 1000 -> 4), so a
    500-issue run and a 1600-issue run both sort correctly.
    """
    ext = os.path.splitext(filename)[1].lower() or ".pdf"
    src = filename

    dt = issue_date(src)
    year = (dt[0] if dt else 0) or year_fallback or issue_year(src)

    num = issue_number(src)
    if num is None:
        # A file with no printed number is not "issue 001" — inventing one
        # mislabels it. But with several such files a bare name would sort
        # arbitrarily, so only fall back to position when there IS a run.
        if total <= 1:
            return safe_name(series + " - " + os.path.splitext(src)[0]) + ext
        num = index + 1
        width = max(3, len(str(total or num)))
    else:
        width = max(3, len(str(num)))

    parts = f"{series} {num:0{width}d}"
    if year:
        parts += f" ({year})"
    return safe_name(parts) + ext


def dedupe_formats(entries):
    """One file per issue, even when the item offers .cbr AND .pdf of it.

    Items routinely carry both a `Comic Book RAR` and a `Text PDF` of the same
    issue. Pulling both puts two copies of one issue in the library, which Komga
    shows as a duplicate. Prefer the artefact `pick_file` already ranked: entries
    arriving here are already the chosen artefact per item, so this collapses
    only cross-ENTRY collisions (e.g. two listings holding the same issue) by
    the same key, keeping the first in ladder order.
    """
    seen = {}
    kept = []
    for e in entries:
        key = (issue_number(e.get("file") or "") or e["identifier"],
               (e.get("title") or "").lower()[:40])
        if key in seen:
            continue
        seen[key] = e
        kept.append(e)
    return kept


def effective_year(entry) -> int:
    """Best year for an entry: from its filename, else archive's own date."""
    f = entry.get("file") or ""
    return (issue_date(f) or (0, 0, 0))[0] or entry.get("meta_year") or 0


def plan_filenames(entries, title_fallback: str = ""):
    """Assign every entry a Komga-correct series folder + file name.

    Returns (series, named_entries).

    ORDER: by numeric issue number, then year, then identifier.

    Not archive's own order, and not filename-derived dates — both were tested
    and neither is correct (see search_ids). The printed issue number is the
    one thing that actually defines reading order for a publication, so it is
    the primary key and the folder sorts by a zero-padded running index built
    from that order.

    Files must be UNIQUE within the folder; two same-named files overwrite.
    """
    series = series_name(title_fallback or "Untitled")
    total = len(entries)

    def sort_key(e):
        f = e.get("file") or ""
        num = issue_number(f)
        title_num = issue_number(e.get("title") or "")
        return (
            0 if (num is not None or title_num is not None) else 1,
            num if num is not None else (title_num if title_num is not None else 0),
            effective_year(e) or 9999,
            e.get("identifier") or "",
        )

    ordered = sorted(entries, key=sort_key)

    used = set()
    out = []
    for i, e in enumerate(ordered):
        name = komga_filename(series, e.get("file") or e["identifier"], i, total,
                              year_fallback=e.get("meta_year") or 0)
        if name in used:
            stem, ext = os.path.splitext(name)
            name = f"{stem} [{e['identifier']}]{ext}"
        used.add(name)
        e2 = dict(e)
        e2["outname"] = name
        out.append(e2)
    return series, out


def listing_series(job_label: str, query: str, items) -> str:
    """Pick the series folder for a whole job.

    For a COLLECTION the collection's own title must win. Item titles inside a
    collection are wildly inconsistent — a real run of `collection:crackedmagazine`
    contains items whose titles include `Horror Tales of the 1980's (Cracked
    Magazine)`, so averaging or majority-voting item titles names the folder
    after the odd one out. That is why the collection name is resolved from
    archive.org itself, not inferred from members.

      1. an explicit title:() phrase in the query  -> the user's own words
      2. the collection id, resolved via /metadata -> the real collection title
      3. the job label
      4. the most common item title (last resort, single items only)
    """
    m = re.search(r"title:\s*\(([^)]+)\)", query or "", re.I)
    if m and m.group(1).strip():
        return series_name(m.group(1).strip(), job_label)

    m = re.search(r"collection:\s*([^\s)]+)", query or "", re.I)
    if m:
        ident = m.group(1).strip()
        title = collection_title(ident)
        if title:
            return series_name(title, ident)
        pretty = re.sub(r"[-_]+", " ", ident)
        pretty = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", pretty)
        return series_name(pretty or ident, ident)

    if items and len(items) == 1:
        # A single item's title IS the issue, e.g. `Cracked 75` or
        # `MAD Magazine 180 1976`. Using it verbatim as the series name then
        # duplicates the number in the filename AND files every later issue
        # under a different "series". Strip the trailing issue and any
        # parenthesised season/date so the folder is the publication.
        raw = items[0].get("title") or job_label
        base = re.sub(r"[\s_,#-]*\([^)]*\)\s*$", "", raw).strip()        # (Fall 1998)
        # `Texas Architect Magazine, July 1959` -> month/year suffix
        base = re.sub(r"[\s_,#-]*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                      r"[a-z]*\.?\s*\(?19\d{2}\)?\s*$", "", base, flags=re.I).strip()
        base = re.sub(r"[\s_,#-]*\(?19\d{2}\)?\s*$", "", base).strip()   # 1976
        base = re.sub(r"[\s_,#-]*(?:issue\s*)?\d{1,4}\s*$", "", base,
                      flags=re.I).strip()                                # 180
        base = re.sub(r"[\s_,#-]*\(?19\d{2}\)?\s*$", "", base).strip()   # year again
        if not base:                      # title was only a number
            base = re.sub(r"[-_]+(?:19\d{2})?[-_]?\d{0,4}$", "", job_label) or job_label
        return series_name(base, job_label)
    if items:
        # Same trap for a run: the most common item title is still an issue
        # title (`Heavy Metal 3`), so strip the tail before naming the folder.
        common = _most_common([i.get("title") or "" for i in items]) or job_label
        base = re.sub(r"[\s_,#-]*\([^)]*\)\s*$", "", common).strip()
        base = re.sub(r"[\s_,#-]*\(?19\d{2}\)?\s*$", "", base).strip()
        base = re.sub(r"[\s_,#-]*(?:issue\s*|#\s*)\d{1,4}\s*$", "", base,
                      flags=re.I).strip()
        base = re.sub(r"[\s_,#-]*\(?19\d{2}\)?\s*$", "", base).strip()
        if not base:
            base = _most_common([i.get("title") or "" for i in items]) or job_label
        # Strip a trailing bare number only when every member agrees on the
        # same prefix, otherwise a title like `Heavy Metal 3` and
        # `Heavy Metal 4` would be left alone anyway - they agree, so strip.
        base = re.sub(r"[\s_,#-]+\d{1,4}$", "", base).strip() or base
        return series_name(base, job_label)
    return series_name(job_label, "Untitled")


_COLLECTION_TITLE_CACHE = {}


# What counts as an archive.org identifier, for the paths that accept one from
# the browser instead of from a pasted URL.
IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def collection_title(ident: str) -> str:
    """The human title of a collection, from archive.org (cached)."""
    if ident in _COLLECTION_TITLE_CACHE:
        return _COLLECTION_TITLE_CACHE[ident]
    title = ""
    try:
        m = http_json(META_API.format(ident))
        md = m.get("metadata") or {}
        t = md.get("title") or ""
        if isinstance(t, list):
            t = t[0] if t else ""
        title = str(t)
    except Exception:  # noqa: BLE001
        title = ""
    _COLLECTION_TITLE_CACHE[ident] = title
    return title


def _meta_year(md) -> int:
    """archive.org's own year/date for an item, as an int (0 when absent).

    Used ONLY to fill a gap in ordering — never to override a date parsed from
    the filename, and never as an issue number. It is unreliable for magazines
    (many scans are batch-dated), but for a dateless `Cracked111.pdf` whose
    metadata says 1973 it is the only signal available.
    """
    if not isinstance(md, dict):
        return 0
    for key in ("year", "date", "publicdate"):
        v = md.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        if not v:
            continue
        m = re.search(r"(19\d{2}|20\d{2})", str(v))
        if m:
            return int(m.group(1))
    return 0


def _most_common(values):
    """The value shared by most entries — a listing's title, ignoring outliers."""
    from collections import Counter
    vals = [v for v in values if v]
    if not vals:
        return ""
    return Counter(vals).most_common(1)[0][0]


# ---------------------------------------------------------------- reader
#
# Ultra-basic preview: list the pages inside a downloaded file and serve them
# one at a time. A CBZ is a zip, so `zipfile` reads it directly with no
# dependency. PDFs are opened by the browser itself (iframe), because rendering
# PDF pages server-side needs a whole PDF engine — far too heavy for a preview.

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def book_key(entry, job=None) -> str:
    """A stable identity for one downloaded book, identical on every device.

    Deliberately NOT the job id (random per run) and not the filename alone
    (changes if the same issue is re-pulled through another listing). The
    archive.org identifier plus the artefact name is the same everywhere, falls
    back to the output name for hand-added files, and is namespaced so it can
    never collide with a different library later.
    """
    ident = entry.get("identifier") if isinstance(entry, dict) else None
    if ident:
        art = (entry.get("file") or entry.get("outname") or "").lower()
        return f"ia:{ident}:{art}"
    name = (entry.get("outname") if isinstance(entry, dict) else "") or ""
    return f"file:{os.path.basename(name).lower()}"


def comic_pages(path: str):
    """Page names inside a CBZ/CBT/.zip, or a CBR, in reading order.

    Zip entry order is NOT reliably the page order (uploaders add pages
    incrementally), so pages are sorted naturally by the trailing number in the
    filename: `... 0030.jpg` before `... 0045.jpg`.

    CBR is RAR, which the stdlib cannot read. It is listed through an external
    extractor (bsdtar, 7z, 7za or unrar) and pages are streamed out one at a
    time; without one of those installed, a CBR simply has no pages.
    """
    low = path.lower()
    if low.endswith((".cbz", ".cbt", ".zip")):
        try:
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist()
                         if not n.endswith("/") and n.lower().endswith(IMAGE_EXTS)]
        except (zipfile.BadZipFile, OSError):
            return []
        return sorted(names, key=_natkey)

    if low.endswith((".cbr", ".rar")):
        out = _rar_list(path)
        return sorted(out, key=_natkey) if out else []

    return []


def _natkey(n):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", n)]


def _rar_tool():
    """First RAR extractor that works, or None.

    unrar is tried first: 7z cannot read every RAR variant (it rejects these
    RAR4 comic archives outright), so it is only a fallback.
    """
    for name, args in (("unrar", ["lb"]), ("bsdtar", ["-tf"]),
                       ("7z", ["l", "-ba"]), ("7za", ["l", "-ba"])):
        exe = shutil.which(name)
        if exe:
            return exe, args
    return None


def _rar_list(path: str):
    tool = _rar_tool()
    if not tool:
        return []
    exe, args = tool
    try:
        p = subprocess.run([exe] + args + [path], capture_output=True,
                           timeout=120, text=True)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    lines = [ln.strip() for ln in (p.stdout or "").splitlines()]
    # 7z pads output with archive metadata; keep only page-looking entries.
    return [ln for ln in lines if ln and ln.lower().endswith(IMAGE_EXTS)]


# Cached cover thumbnails live under the config directory, never in the app
# folder, so a read-only install still works.
THUMB_DIR = os.path.join(user_config_dir(), "thumbs")


def page_thumb(path: str, width: int = 96) -> bytes:
    """A small JPEG of page 0, for the library grid.

    Page images are ~2400 px wide and megabytes each, so ten rows of full-size
    covers is tens of MB per page. Downscale once and cache on disk, keyed by
    the archive's size+mtime so a replaced file re-renders.
    """
    try:
        st = os.stat(path)
    except OSError:
        return b""
    key = hashlib.sha1(
        f"{os.path.realpath(path)}|{st.st_size}|{int(st.st_mtime)}|{width}"
        .encode()).hexdigest()
    cache = os.path.join(THUMB_DIR, key + ".jpg")
    if os.path.isfile(cache):
        with open(cache, "rb") as fh:
            return fh.read()
    # A PDF has no page list to read, so render page one with poppler when it is
    # installed. Without it the row keeps its placeholder rather than lying.
    if path.lower().endswith(".pdf"):
        return _pdf_thumb(path, cache, width)

    pages = comic_pages(path)
    if not pages:
        return b""
    data, _ = read_page(path, pages[0][0] if isinstance(pages[0], (list, tuple))
                        else pages[0])
    try:
        from PIL import Image
    except ImportError:
        return b""          # no Pillow: the UI just shows no cover
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            h = max(1, round(width * im.height / max(1, im.width)))
            im = im.resize((width, h), Image.LANCZOS)
            out = io.BytesIO()
            im.save(out, "JPEG", quality=78)
    except Exception:
        return b""
    blob = out.getvalue()
    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
        tmp = cache + ".part"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, cache)
    except OSError:
        pass
    return blob


def _pdf_thumb(path: str, cache: str, width: int = 96) -> bytes:
    """First page of a PDF as a small JPEG, via pdftoppm.

    Optional by design: poppler is common on Linux and bundled by most package
    managers, but it is not something this app installs, so a missing binary
    just means no cover.
    """
    exe = shutil.which("pdftoppm")
    if not exe:
        return b""
    out_root = cache[:-4] if cache.endswith(".jpg") else cache
    try:
        # Render straight into the cache directory, then fold the one page into
        # the cached JPEG so the next call is a file read.
        subprocess.run([exe, "-jpeg", "-f", "1", "-l", "1",
                        "-scale-to", str(width * 3), path, out_root],
                       check=True, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        produced = next((os.path.join(os.path.dirname(out_root), f)
                         for f in os.listdir(os.path.dirname(out_root) or ".")
                         if f.startswith(os.path.basename(out_root))
                         and f.endswith(".jpg")), "")
        if not produced:
            return b""
        try:
            from PIL import Image
            with Image.open(produced) as im:
                im = im.convert("RGB")
                h = max(1, round(width * im.height / max(1, im.width)))
                im = im.resize((width, h), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=78)
            blob = buf.getvalue()
        finally:
            try:
                os.remove(produced)
            except OSError:
                pass
        try:
            tmp = cache + ".part"
            with open(tmp, "wb") as fh:
                fh.write(blob)
            os.replace(tmp, cache)
        except OSError:
            pass
        return blob
    except Exception:
        return b""


def read_page(path: str, name: str):
    """Bytes + a content type for one page inside the archive."""
    if path.lower().endswith((".cbr", ".rar")):
        data = _rar_read(path, name)
    else:
        with zipfile.ZipFile(path) as z:
            data = z.read(name)
    ext = os.path.splitext(name)[1].lower()
    ctype = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
             ".bmp": "image/bmp"}.get(ext, "image/jpeg")
    return data, ctype


def _rar_read(path: str, name: str) -> bytes:
    """One member from a RAR, streamed to stdout by whichever extractor exists."""
    tool = _rar_tool()
    if not tool:
        raise RuntimeError("no RAR extractor found (install unrar, 7z or bsdtar)")
    exe, _ = tool
    base = os.path.basename(exe)
    if base == "bsdtar":
        cmd = [exe, "-xOf", path, name]
    elif base == "unrar":
        cmd = [exe, "p", "-inul", "-p-", path, name]
    else:  # 7z / 7za
        cmd = [exe, "e", "-so", path, name]
    p = subprocess.run(cmd, capture_output=True, timeout=120)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError((p.stderr or b"").decode("utf-8", "replace").strip()
                           or "could not extract page")
    return p.stdout


def human_eta(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h, m = divmod(seconds // 60, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d, rem = divmod(seconds, 86400)
    return f"{d}d {rem // 3600}h"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


# ---------------------------------------------------------------- filesystem

def safe_name(name: str) -> str:
    """Make an archive.org metadata filename safe on EVERY platform.

    Archive names routinely contain characters that Windows rejects outright
    (`< > : " / \\ | ? *`), and a trailing dot or space is also illegal there.
    Unhandled, these fail as a download error that looks like a network fault.
    """
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Windows: no trailing dots/spaces, and reserved device names
    name = name.rstrip(". ")
    stem, ext = os.path.splitext(name)
    if stem.upper() in {"CON", "PRN", "AUX", "NUL"} or \
       re.fullmatch(r"(COM|LPT)[1-9]", stem.upper()):
        stem = "_" + stem
    return (stem + ext)[:150] or "unnamed"


def move_file(src: str, dst: str):
    """Move without assuming same-filesystem.

    `os.replace` raises EXDEV across devices (external drive -> home), so copy
    then remove. Never leave a truncated file at the destination: copy to a
    temp name beside the target, then os.replace within that filesystem.
    """
    dstdir = os.path.dirname(dst) or "."
    os.makedirs(dstdir, exist_ok=True)
    tmp = os.path.join(dstdir, "." + os.path.basename(dst) + ".part")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    try:
        os.remove(src)
    except OSError:
        pass


def magic_ok(path: str) -> bool:
    """The artifact must start with a real signature.

    archive.org serves a 404 as HTTP 200 HTML, so a "finished" download can be a
    ~200-byte error page. This is the check that catches it.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return False
    return (head[:5] == b"%PDF-"
            or head[:4] == b"PK\x03\x04"
            or head[:4] == b"Rar!"
            or head[:3] == b"7z\xbc")


def free_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


# ---------------------------------------------------------------- downloader

# Many files x 1 connection beats few files x 4: archive.org throttles EACH
# connection to ~0.5-1.2 MiB/s, so aggregate throughput scales with how many
# files are in flight. Measured on our runs: -j12 -x1 -s1 gave 10-15 MiB/s
# against ~3.5 MiB/s for -j4 -x4 -s4.
# Free space kept back so a download cannot fill the disk completely. A paused
# job needs room to resume plus whatever the OS wants; 256 MB is the floor.
SPACE_RESERVE_MB = 256

# Set once, at exit: worker and backfill threads check it and return instead of
# being killed mid-call when the interpreter finalises, which segfaults (seen on
# CPython 3.10 under CI, after the suite had already reported OK).
_SHUTDOWN = threading.Event()


def shutdown_workers(timeout: float = 3.0) -> None:
    """Ask background threads to stop, then wait briefly for them.

    Daemon threads are not stopped by the interpreter, they are abandoned while
    possibly inside subprocess or sqlite; that is the crash this prevents.
    """
    _SHUTDOWN.set()
    deadline = time.monotonic() + timeout
    for t in list(threading.enumerate()):
        if t is threading.current_thread() or t.name not in ("job-worker", "desc-backfill"):
            continue
        t.join(max(0.0, deadline - time.monotonic()))


atexit.register(shutdown_workers)

ARIA_FLAGS = ["-x", "1", "-s", "1", "-c",
              "--file-allocation=none",
              "--auto-file-renaming=false",
              "--allow-overwrite=true",
              "--max-tries=10", "--retry-wait=5",
              "--console-log-level=warn"]


def aria2c_path():
    """Locate the downloader. `IA_ARIA2` wins so the launcher decides once."""
    env = os.environ.get("IA_ARIA2")
    if env and (os.path.isfile(env) or shutil.which(env)):
        return env
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin",
                     "aria2c.exe" if os.name == "nt" else "aria2c")
    if os.path.isfile(p):
        return p
    return shutil.which("aria2c")


def write_aria_input(entries, path):
    """Write the aria2 input file.

    `out=` takes the rest of the line verbatim, so spaces and parens in
    filenames need no quoting.
    """
    with open(path, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(e["url"] + "\n  out=" + e["outname"] + "\n")


def pdf_page_count(path: str) -> int:
    """Page count from the PDF's own structure, without a rendering library.

    Counts `/Type /Page` objects, which is what every PDF writer emits, and
    falls back to `/Count` from the page tree root. Compressed object streams
    (PDF 1.5+) hide both, so this returns 0 rather than guessing when neither
    matches; pdf.js reports the true number in the reader and that total is
    saved with the reading position.

    The header is located rather than required at byte 0: uploads carry junk
    before it (a PHP error page, a BOM) and such a file is still a valid PDF.
    """
    try:
        with open(path, "rb") as fh:
            if b"%PDF" not in fh.read(4096):
                return 0
            fh.seek(0)
            data = fh.read()
    except OSError:
        return 0
    n = len(re.findall(rb"/Type\s*/Page[^s]", data))
    if n:
        return n
    counts = [int(m) for m in re.findall(rb"/Count\s+(\d+)", data)]
    return max(counts) if counts else 0


def md5_index(root: str):
    """{md5: filename} for every archive already under `root`.

    Used by delta-vs-folder so a re-run fetches only what is missing. Hashes are
    cached in a small sidecar keyed by (size, mtime), because rehashing a
    100-issue library on every plan would take longer than the download it is
    meant to avoid. Only comic/PDF extensions are considered — the sidecar and
    partial downloads are ignored.
    """
    root = os.path.realpath(root)
    if not os.path.isdir(root):
        return {}
    cache_path = os.path.join(root, ".md5-index.json")
    cache = {}
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}

    index = {}
    fresh = {}
    changed = False
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            low = fn.lower()
            if not low.endswith((".cbz", ".cbr", ".cbt", ".zip", ".pdf")):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            key = str(st.st_size) + ":" + str(int(st.st_mtime))
            digest = cache.get(full)
            if not isinstance(digest, dict) or digest.get("key") != key:
                try:
                    h = hashlib.md5()
                    with open(full, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
                    digest = {"key": key, "md5": h.hexdigest()}
                    changed = True
                except OSError:
                    continue
            fresh[full] = digest
            index[digest["md5"]] = os.path.relpath(full, root)

    if changed or fresh != cache:
        try:
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(fresh, fh)
        except OSError:
            pass
    return index


def verify_manifest(job_dir: str, entries, magic=True):
    """Byte-exact verification. Size is the STRONG check.

    A truncated PDF still opens and looks plausible; only the byte count against
    archive.org's own metadata catches it. Returns (ok, problems).
    """
    problems = []
    ok = 0
    for e in entries:
        p = os.path.join(job_dir, e["outname"])
        if not os.path.exists(p):
            problems.append((e["outname"], "missing"))
            continue
        got = os.path.getsize(p)
        want = e.get("size") or 0
        if want and got != want:
            problems.append((e["outname"], f"size {got} != {want} ({got-want:+d})"))
            continue
        if magic and not magic_ok(p):
            problems.append((e["outname"], "not a PDF/ZIP/RAR (likely an error page)"))
            continue
        ok += 1
    stray = [f for f in os.listdir(job_dir) if f.endswith(".aria2")]
    if stray:
        problems.append(("*", f"{len(stray)} unfinished control files"))
    return ok, problems


# ---------------------------------------------------------------- streaming
# Reading a book straight from archive.org, with nothing written to the
# library folder. The site already publishes one JPEG per page as a
# "derivative", so a page turn is a ~400 kB request rather than a 50 MB file
# download. This is what makes Read-without-Download possible; it needs a
# connection every time, and that limit is stated in the UI.

STREAM_DIR = os.path.join(user_config_dir(), "stream")
STREAM_WIDTH = 800            # px: the sharp/economical middle of the offered sizes
STREAM_MAX_PAGES = 1200       # a hard stop for the page-count probe
_IA_BASE = "https://archive.org"


def _http_get(url: str, timeout: int = 25):
    """GET returning (status, body). Never raises: the UI handles the status."""
    req = urllib.request.Request(url, headers={"User-Agent": "HardlyReading"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""


def _item_files(ident: str):
    """The item's file list plus its metadata, or None when it is unreachable."""
    code, body = _http_get(f"{_IA_BASE}/metadata/{urllib.parse.quote(ident)}", 15)
    if code != 200:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def stream_item_pages(ident: str, width: int = STREAM_WIDTH) -> int:
    """How many pages an item exposes, for the reader's counter.

    archive.org does not report a page count for these items: metadata has no
    `imagecount`, and pages *past* the end return 200 with a repeated image
    instead of a 404, so probing forward would happily run forever. The count
    has to come from a derivative that lists the leaves, so try those in order
    of cost and fall back to zero — the reader then counts pages by whatever
    was actually fetched and shows "?" for the total, which is honest.
    """
    if not ident:
        return 0
    meta = _item_files(ident)
    if not meta:
        return 0

    md = meta.get("metadata") or {}
    for key in ("imagecount", "page_count", "pagecount"):
        for src in (md, meta):
            try:
                n = int(src.get(key) or 0)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                return min(n, STREAM_MAX_PAGES)

    # Cheapest reliable list of leaves: the OCR page index (a few hundred
    # bytes) or the page-numbers sidecar (a few kB).
    names = [f.get("name", "") for f in (meta.get("files") or [])]
    for name in names:
        low = name.lower()
        if low.endswith(("_hocr_pageindex.json.gz", "_page_numbers.json")):
            code, body = _http_get(
                f"{_IA_BASE}/download/{urllib.parse.quote(ident)}/"
                f"{urllib.parse.quote(name)}", 20)
            if code != 200 or not body:
                continue
            if low.endswith(".gz"):
                try:
                    import gzip
                    body = gzip.decompress(body)
                except OSError:
                    continue
            try:
                doc = json.loads(body)
            except ValueError:
                continue
            if isinstance(doc, dict):
                leaves = doc.get("pages") or []
            elif isinstance(doc, list):
                # The page index is [[char_offset, length, ...], ...] — one
                # row per leaf, in reading order — so its length is the count.
                return min(len(doc), STREAM_MAX_PAGES)
            else:
                leaves = []
            last = -1
            for e in leaves:
                if isinstance(e, dict):
                    v = e.get("leafNum", -1)
                elif isinstance(e, (list, tuple)) and e:
                    v = e[0]
                else:
                    continue
                try:
                    last = max(last, int(v))
                except (TypeError, ValueError):
                    pass
            if last >= 0:
                return min(last + 1, STREAM_MAX_PAGES)
    return 0


def item_description(ident: str, title: str = "", engine=None) -> str:
    """The description archive.org holds for one item, filtered like a card.

    Checked against the index first: descriptions never change, and a row that
    is opened twice should not cost two requests. An empty result is cached as
    well, so items with nothing to say are asked exactly once.
    """
    if not ident:
        return ""
    if engine is not None:
        hit = engine.cached_desc(ident)
        if hit is not None:
            return hit
    try:
        meta = _item_files(ident)
    except Exception:
        return ""
    if not meta:
        return ""
    md = meta.get("metadata") or {}
    desc = useful_desc(plain_text(md.get("description")),
                       title or md.get("title") or ident)
    if engine is not None:
        engine.store_desc(ident, desc)
    return desc


def stream_page(ident: str, page: int, width: int = STREAM_WIDTH):
    """One page of a streamed book as (bytes, content_type).

    Cached on disk by identifier/page/width: flipping back through pages is
    free, and a re-open does not re-fetch what was already seen. Raises
    LookupError when the page cannot be had, which the route turns into a 404
    so the UI can say "stopped" rather than showing a broken image.
    """
    if not ident:
        raise LookupError("no identifier")
    page = max(0, int(page))
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", ident)
    cache = os.path.join(STREAM_DIR, f"{safe}_{page}_w{width}.jpg")
    if os.path.isfile(cache):
        with open(cache, "rb") as fh:
            return fh.read(), "image/jpeg"

    url = (f"{_IA_BASE}/download/{urllib.parse.quote(ident)}/page/"
           f"n{page}_w{width}.jpg")
    code, blob = _http_get(url, 30)
    if code != 200 or not blob or not blob.startswith(b"\xff\xd8"):
        raise LookupError(f"page {page} unavailable (HTTP {code})")
    try:
        os.makedirs(STREAM_DIR, exist_ok=True)
        tmp = cache + ".part"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, cache)
    except OSError:
        pass          # a read-only config dir still streams, just uncached
    return blob, "image/jpeg"
