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
import json
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
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
    """Per-OS config directory, no external dependency."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    p = os.path.join(base, "hardlyreading")
    os.makedirs(p, exist_ok=True)
    return p


def default_download_dir() -> str:
    home = os.path.expanduser("~")
    for name in ("Downloads", "downloads"):
        p = os.path.join(home, name)
        if os.path.isdir(p):
            return os.path.join(p, "HardlyReading")
    return os.path.join(home, "HardlyReading")


STATE_FILE = os.environ.get("IA_STATE") or os.path.join(user_config_dir(), "state.json")
HISTORY_DB = os.environ.get("IA_HISTORY") or os.path.join(user_config_dir(), "history.db")


# ---------------------------------------------------------------- http helpers

def http_json(url: str, timeout: int = 60):
    """GET a URL and return parsed JSON. No retry here — callers that can
    recover (the resolver, the per-item probe) do their own attempts."""

    req = urllib.request.Request(url, headers={"User-Agent": "HardlyReading/1.1.0"})
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
        skipped = [i for i in self.items if i.get("skip")]
        return {
            "id": self.id, "kind": self.kind, "source": self.source,
            "label": self.label, "status": self.status,
            "n_items": len(self.items), "total_bytes": self.total_bytes,
            "done_bytes": self.done_bytes, "speed": self.speed,
            "message": self.message, "error": self.error,
            "retries": self.retries, "created": self.created,
            "updated": self.updated,
            "scoped_query": self.scoped_query,
            "dir": self.dir,
            "series": self.series,
            "skipped": len(skipped),
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


def search_page(query: str, rows: int = 24, page: int = 1,
                mediatype: str = "texts", sort: str = "downloads desc",
                title_only: bool = True, collections_only: bool = False):
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

    params = [("q", q), ("rows", str(rows)), ("page", str(page)),
              ("output", "json"),
              ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "year"),
              ("fl[]", "mediatype"), ("fl[]", "downloads"),
              ("fl[]", "creator")]
    if sort:
        params.append(("sort[]", sort))
    url = SEARCH_API + "?" + urllib.parse.urlencode(params)
    resp = http_json(url)["response"]
    docs = resp.get("docs", [])
    return {
        "numFound": resp.get("numFound", 0),
        "start": resp.get("start", 0),
        "page": page,
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
            "thumb": f"https://archive.org/services/img/{d.get('identifier')}",
        } for d in docs],
    }


def search_documents(query: str, page: int = 1, rows: int = 24,
                     kind: str = "all", workers: int = 10):
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
                          collections_only=True)
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
        raw = search_page(query, rows=max(rows * 4, 60), page=page)
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

    raw = search_page(query, rows=rows, page=page)
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

    def __init__(self, workers: int = 12):
        self.workers = workers

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
                    entry = {
                        "identifier": ident,
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
        self.current = None
        self.paused = False
        self.settings = {
            "download_dir": default_download_dir(),
            "jobs_in_flight": 12,
            "between_jobs": 30,
            "preview": True,
            "komga_naming": True,
            # Skip anything whose checksum already exists in the destination.
            "skip_existing": True,
            # Show the "reader" label in the reader header.
            "show_brand": True,
        }
        self._worker = None
        self._conn = None
        self._lock_db = threading.Lock()
        self._load_state()

    # ---- persistence
    def _load_state(self):
        try:
            with open(STATE_FILE) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return
        self.settings.update(d.get("settings") or {})
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
        self.order = [j.id for j in self.jobs]

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

    # ---- queue
    def add(self, url: str):
        c = classify_url(url)
        if not c:
            raise ValueError("Empty link")
        kind, query, label = c
        with self.lock:
            for j in self.jobs:
                if j.status != "cancelled" and j.query == query and j.kind == kind:
                    raise ValueError("Already in the list")
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, source=url,
                      label=label or query, query=query)
            self.jobs.append(job)
            self.order.append(job.id)
            self.save_state()
        self.kick()
        return job.summary()

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
            # One malformed job must not blank the whole UI. A summary that
            # throws is reported as an error card instead of killing the poll.
            jobs = []
            for j in self.jobs:
                try:
                    jobs.append(j.summary())
                except Exception as exc:  # noqa: BLE001
                    jobs.append({"id": getattr(j, "id", "?"), "status": "failed",
                                 "label": getattr(j, "label", "?"),
                                 "error": f"state error: {exc}", "n_items": 0,
                                 "total_bytes": 0, "done_bytes": 0,
                                 "message": "", "skipped": 0})
            return {
                "jobs": jobs,
                "current": self.current,
                "paused": self.paused,
                "busy": bool(self.current),
                "settings": dict(self.settings),
                "counts": {
                    "queued": sum(1 for j in self.jobs if j.status == "queued"),
                    "active": sum(1 for j in self.jobs if j.status in
                                  ("resolving", "downloading", "verifying")),
                    "done": sum(1 for j in self.jobs if j.status == "done"),
                    "failed": sum(1 for j in self.jobs if j.status == "failed"),
                },
            }

    # ---- worker
    def kick(self):
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _next_queued(self):
        with self.lock:
            now = time.time()
            for jid in self.order:
                j = self.get(jid)
                if not j:
                    continue
                if j.status == "resume":
                    return j
                if j.status == "queued":
                    # A retryable failure waits on the ladder before trying again
                    if j.next_try_at and now < j.next_try_at:
                        continue
                    return j
        return None

    def _run(self):
        """Worker loop: take the next due job, process or resume it, sleep
        between jobs. Exits when nothing is queued or scheduled for retry."""
        while True:
            if self.paused:
                time.sleep(0.5)
                continue
            job = self._next_queued()
            if not job:
                time.sleep(2)   # nothing runnable now; a retry may become due
                with self.lock:
                    if not any(j.status in ("queued", "resume") for j in self.jobs):
                        return
                continue
            if job.status == "resume":
                entries = [i for i in job.items if not i.get("skip")]
                with self.lock:
                    self.current = job.id
                try:
                    self._download(job, entries)
                finally:
                    with self.lock:
                        self.current = None
                        self.save_state()
            else:
                self._process(job)
            time.sleep(min(self.settings.get("between_jobs", 30), 60))

    def _process(self, job: Job):
        with self.lock:
            self.current = job.id
            job.status = "resolving"
            job.message = "Looking up items…"
            job.updated = time.time()
            self.save_state()

        def progress(msg):
            job.message = msg
            job.updated = time.time()

        try:
            items = Resolver().resolve(job, progress=progress)
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

            # The named entries are what gets downloaded and verified, so they
            # are what the job must hold. plan_filenames returns copies, so
            # assigning `items` would leave every entry without an `outname`.
            kept_ids = {e.get("identifier") for e in ok}
            skipped = [i for i in items if i.get("skip")
                       or i.get("identifier") not in kept_ids]

            # --- delta-vs-folder: drop anything already on disk, matched by
            # archive's own checksum rather than by filename. Renaming a file
            # or re-downloading a collection under a new label must not fetch
            # the same bytes twice. Entries with no md5 are always kept.
            job.dir = os.path.join(self.settings["download_dir"],
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
            # Known now, not at download time, so the plan screen can show the
            # destination and warn about free space before anything starts.
            job.status = "planned"
            job.message = (f"{len(ok)} item{'s' if len(ok) != 1 else ''} ready"
                           f" · {human_size(job.total_bytes)}")
            job.updated = time.time()
            with self.lock:
                self.save_state()

            if self.settings.get("preview", True):
                return  # wait for the user to confirm on the plan screen
            self._download(job, ok)
        except Exception as e:  # noqa: BLE001
            job.status = "failed"
            job.error = str(e)
            job.message = "Could not resolve"
        finally:
            job.updated = time.time()
            with self.lock:
                self.current = None
                self.save_state()

    # ---- download
    def _download(self, job: Job, entries):
        aria = aria2c_path()
        # The folder is the Komga SERIES, not the raw job label, so a series
        # pulled in two passes lands in one folder instead of two.
        dest = os.path.join(self.settings["download_dir"],
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

        if pending:
            inp = os.path.join(dest, "aria.txt")
            write_aria_input(pending, inp)
            self.save_state()

            cmd = ([aria, "-i", "aria.txt",
                    "-j", str(self.settings.get("jobs_in_flight", 12))]
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
                sp = re.search(r"DL:([\d.]+\w+)", line)
                if sp:
                    job.speed = _parse_speed(sp.group(1))
                job.updated = time.time()

            proc.wait()
        job.done_bytes = job.total_bytes

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
            self.current = job.id
        try:
            self._download(job, entries)
        except Exception as e:  # noqa: BLE001
            job.status = "failed"
            job.error = str(e)
        finally:
            with self.lock:
                self.current = None
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
            if self.current == job_id:
                self.current = None
            self.save_state()
        return {"removed": job_id}

    def delete_file(self, job_id, filename):
        """Delete one downloaded file, its entry, and its reading position."""
        j = self.get(job_id)
        if not j or not j.dir:
            raise ValueError("no such job")
        base = os.path.basename(filename)
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


def open_folder(path: str) -> bool:
    """Reveal a folder in the OS file manager. Never raises."""
    try:
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)  # noqa: S606  (Windows only)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- downloader

# Many files x 1 connection beats few files x 4: archive.org throttles EACH
# connection to ~0.5-1.2 MiB/s, so aggregate throughput scales with how many
# files are in flight. Measured on our runs: -j12 -x1 -s1 gave 10-15 MiB/s
# against ~3.5 MiB/s for -j4 -x4 -s4.
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
    (PDF 1.5+) would hide both, so this returns 0 rather than guessing when
    neither matches; the reader still works, it just shows no total.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return 0
    if not data.startswith(b"%PDF"):
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

