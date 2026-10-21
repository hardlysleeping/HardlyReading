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

"""HardlyReading — HTTP API + static UI. Stdlib only."""
import json
import os
import shutil
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine  # noqa: E402


def find_free_port(start=8788, host="127.0.0.1"):
    for p in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host if host not in ("0.0.0.0", "::") else "127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("no free port")


ENGINE = engine.Engine()


class Handler(BaseHTTPRequestHandler):
    server_version = "HardlyReading/1.9.0"

    def log_message(self, fmt, *args):
        pass  # quiet

    # ---------- helpers
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, name):
        p = os.path.join(HERE, name)
        try:
            with open(p, "rb") as fh:
                self._send(200, fh.read(), "text/html; charset=utf-8")
        except OSError:
            self._send(404, "not found", "text/plain")

    def _asset(self, rel, ctype):
        """Serve a single file from the app directory with an explicit type.

        Used for the PWA files (manifest, service worker, icons). Only files
        that a route names explicitly reach this, and the path is contained to
        the app directory regardless.
        """
        root = os.path.realpath(HERE)
        p = os.path.realpath(os.path.join(root, rel))
        if not p.startswith(root + os.sep) or not os.path.isfile(p):
            return self._send(404, "not found", "text/plain")
        try:
            with open(p, "rb") as fh:
                self._send(200, fh.read(), ctype)
        except OSError:
            self._send(404, "not found", "text/plain")

    def _vendor(self, rel):
        """Serve the bundled pdf.js module and its worker.

        Vendored rather than pulled from a CDN: the app is local-first and has
        to work with no internet beyond archive.org itself. The path is pinned
        to the vendor directory so a crafted URL cannot walk out of it.
        """
        root = os.path.realpath(os.path.join(HERE, "vendor"))
        p = os.path.realpath(os.path.join(root, rel.lstrip("/")))
        if not p.startswith(root + os.sep) or not os.path.isfile(p):
            return self._send(404, "not found", "text/plain")
        ctype = ("text/javascript; charset=utf-8" if p.endswith((".mjs", ".js"))
                 else "application/octet-stream")
        try:
            with open(p, "rb") as fh:
                self._send(200, fh.read(), ctype)
        except OSError:
            self._send(404, "not found", "text/plain")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    @staticmethod
    def _resolve_dir(jid):
        """Directory behind a job id, or a `folder:` pseudo-id from the library
        scan. None when neither resolves."""
        if jid.startswith(ENGINE.FOLDER_PREFIX):
            return ENGINE.folder_path(jid[len(ENGINE.FOLDER_PREFIX):])
        j = ENGINE.get(jid)
        return j.dir if j and j.dir else None

    @staticmethod
    def _library_item(job, series, name, path, entry, status="done"):
        """One library row: page count, kind and reading position for a file.

        Rows can also describe work that is not on disk yet (queued, planned,
        failed): the Library is where you look for a title, so a known item
        with no file should say "not downloaded" and offer to fetch it rather
        than be invisible until it lands.
        """
        ident = (entry or {}).get("identifier") or ""
        if not os.path.isfile(path):
            return {
                "job": job, "key": engine.book_key(entry), "series": series,
                "name": name, "size": (entry or {}).get("size", 0),
                "pages": 0, "kind": "pending", "readable": False,
                "progress": 0, "finished": False, "downloaded": False,
                "status": status, "identifier": ident,
                "item_url": f"https://archive.org/details/{ident}" if ident else "",
            }
        pages = engine.comic_pages(path)
        is_pdf = path.lower().endswith(".pdf")
        if not pages and is_pdf:
            # No client-side renderer, but the count is readable from the PDF
            # structure, so the library shows real totals instead of 0p.
            pages = [str(i) for i in range(engine.pdf_page_count(path))]
        key = engine.book_key(entry)
        done = ENGINE.get_progress(key)
        return {
            "job": job,
            "key": key,
            "series": series,
            "name": name,
            "size": os.path.getsize(path),
            "pages": len(pages),
            "kind": "pdf" if is_pdf else ("cbz" if pages else "other"),
            "readable": bool(pages) or is_pdf,
            "progress": done,
            "finished": bool(pages) and done >= len(pages) - 1,
            # The Library needs to tell "on disk" from "merely queued", and to
            # offer a link back to the item it came from.
            "downloaded": True,
            # A file that exists mid-download is not readable yet; the row must
            # say so instead of offering a reader for a half-written archive.
            "partial": status in ("downloading", "verifying", "resume"),
            "status": status,
            "identifier": ident,
            "item_url": f"https://archive.org/details/{ident}" if ident else "",
        }

    @staticmethod
    def _library_from_jobs():
        """Library rows contributed by finished jobs, plus the real files they
        cover (so the folder scan can skip duplicates)."""
        PENDING = ("planned", "queued", "resume", "resolving", "downloading",
                   "verifying", "failed")
        items, seen = [], set()
        for j in ENGINE.jobs:
            if not j.items or j.status == "cancelled":
                continue
            if j.status != "done" and j.status not in PENDING:
                continue
            for e in j.items:
                if e.get("skip"):
                    continue
                name = e.get("outname") or ""
                if not name:
                    continue
                p = os.path.join(j.dir, name) if j.dir else name
                exists = bool(j.dir) and os.path.isfile(p)
                if exists:
                    seen.add(os.path.realpath(p))
                elif j.status == "done":
                    # Promised by a finished job but not there: nothing to show.
                    continue
                items.append(Handler._library_item(
                    j.id, j.series or j.label, name, p, e, status=j.status))
        return items, seen

    # ---------- routes
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        path = u.path

        if path in ("/", "/index.html"):
            return self._html("index.html")

        if path.startswith("/vendor/"):
            return self._vendor(path[len("/vendor/"):])

        # PWA assets. The service worker must be served from the root to get
        # scope "/", and a manifest needs the application/manifest+json type or
        # browsers ignore it.
        if path == "/manifest.webmanifest":
            return self._asset("manifest.webmanifest", "application/manifest+json")
        if path == "/sw.js":
            return self._asset("sw.js", "text/javascript; charset=utf-8")
        if path.startswith("/icons/"):
            name = path[len("/icons/"):]
            if "/" in name or not name.endswith(".png"):
                return self._send(404, "not found", "text/plain")
            return self._asset(os.path.join("icons", name), "image/png")

        if path == "/api/state":
            return self._send(200, ENGINE.snapshot())

        if path == "/api/covered":
            return self._send(200, {"covered": ENGINE.covered()})

        if path == "/api/search":
            query = (qs.get("q") or [""])[0].strip()
            if not query:
                return self._send(400, {"error": "empty query"})
            try:
                page = int((qs.get("page") or ["1"])[0])
                filt = (qs.get("filter") or ["all"])[0]
                if filt not in ("all", "collection", "bundle"):
                    filt = "all"
                try:
                    rows = int((qs.get("rows") or ["24"])[0])
                except ValueError:
                    rows = 24
                rows = max(10, min(100, rows))
                res = engine.search_documents(query, page=page, rows=rows,
                                              kind=filt)
                res["rows"] = rows
                return self._send(200, res)
            except Exception as e:  # noqa: BLE001
                return self._send(502, {"error": str(e)})

        if path == "/api/files":
            jid = (qs.get("id") or [""])[0]
            d = self._resolve_dir(jid)
            if not d or not os.path.isdir(d):
                return self._send(404, {"error": "no files for that job"})
            if jid.startswith(ENGINE.FOLDER_PREFIX):
                # A folder the library found by scanning, not a job.
                entries = [{"outname": n, "identifier": None, "file": n}
                           for n in sorted(os.listdir(d))
                           if n.lower().endswith((".cbz", ".cbr", ".cbt", ".zip", ".pdf"))]
            else:
                entries = [e for e in ENGINE.get(jid).items if not e.get("skip")]
            out = []
            for e in entries:
                p = os.path.join(d, e.get("outname") or "")
                if not os.path.isfile(p):
                    continue
                pages = engine.comic_pages(p)
                is_pdf = p.lower().endswith(".pdf")
                if not pages and is_pdf:
                    npages = engine.pdf_page_count(p)
                    pages = [str(i) for i in range(npages)]
                out.append({
                    "name": e.get("outname"),
                    "size": os.path.getsize(p),
                    "pages": len(pages),
                    "kind": "pdf" if is_pdf else ("cbz" if pages else "other"),
                    "readable": bool(pages) or is_pdf,
                    "key": engine.book_key(e),
                    "progress": ENGINE.get_progress(engine.book_key(e)),
                })
            return self._send(200, {"dir": d, "files": out})

        if path == "/api/job":
            # What is inside one job, fetched on demand: the queue poll carries
            # counts only, because a collection can hold hundreds of items and
            # /api/state is polled every second.
            j = ENGINE.get((qs.get("id") or [""])[0])
            if not j:
                return self._send(404, {"error": "no such job"})
            rows = []
            for e in j.items[:500]:
                rows.append({
                    "name": e.get("outname") or e.get("name") or "",
                    "identifier": e.get("identifier") or "",
                    "size": e.get("size") or 0,
                    "skip": bool(e.get("skip")),
                    "status": e.get("status") or "",
                })
            return self._send(200, {"id": j.id, "label": j.label,
                                    "series": j.series, "total": len(j.items),
                                    "items": rows})

        if path == "/api/thumb":
            # Cover for a library row: page 0, downscaled and cached server-side.
            # Returns 404 rather than a broken image so the UI can fall back to
            # a placeholder without a second round trip.
            jid = (qs.get("id") or [""])[0]
            name = os.path.basename((qs.get("file") or [""])[0])
            d = self._resolve_dir(jid)
            if not d or not name:
                return self._send(404, {"error": "no such file"})
            target = os.path.realpath(os.path.join(d, name))
            if not target.startswith(os.path.realpath(d)):
                return self._send(400, {"error": "bad path"})
            try:
                blob = engine.page_thumb(target)
            except Exception:
                blob = b""
            if not blob:
                return self._send(404, {"error": "no cover"})
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(blob)
            return

        if path == "/api/page":
            jid = (qs.get("id") or [""])[0]
            name = (qs.get("file") or [""])[0]
            try:
                idx = int((qs.get("page") or ["0"])[0])
            except ValueError:
                idx = 0
            j = ENGINE.get(jid)
            d = self._resolve_dir(jid)
            if not d:
                return self._send(404, {"error": "no such job"})
            # Never trust the client with a path: resolve inside the folder.
            target = os.path.realpath(os.path.join(d, os.path.basename(name)))
            if not target.startswith(os.path.realpath(d)):
                return self._send(400, {"error": "bad path"})
            pages = engine.comic_pages(target)
            if not pages:
                return self._send(404, {"error": "no pages"})
            idx = max(0, min(idx, len(pages) - 1))
            try:
                if (qs.get("list") or [""])[0] == "1":
                    return self._send(200, {"pages": pages, "count": len(pages)})
                data, ctype = engine.read_page(target, pages[idx])
                return self._send(200, data, ctype)
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": str(e)})

        if path == "/api/book":
            # Raw file for the browser's own PDF viewer.
            jid = (qs.get("id") or [""])[0]
            name = (qs.get("file") or [""])[0]
            d = self._resolve_dir(jid)
            if not d:
                return self._send(404, {"error": "no such job"})
            target = os.path.realpath(os.path.join(d, os.path.basename(name)))
            if not target.startswith(os.path.realpath(d)) or not os.path.isfile(target):
                return self._send(404, {"error": "not found"})
            try:
                size = os.path.getsize(target)
            except OSError as e:
                return self._send(500, {"error": str(e)})
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", "inline")
            self.end_headers()
            try:
                with open(target, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile, length=1 << 20)
            except OSError:
                pass  # client went away mid-stream
            return

        if path == "/api/library":
            # Every downloaded file: first from job state, then anything else
            # sitting in the download folder that no job accounts for.
            items, seen = self._library_from_jobs()
            for f in ENGINE.scan_folder():
                p = os.path.join(ENGINE.settings["download_dir"], f["series"], f["name"])
                rp = os.path.realpath(p)
                if rp in seen or not os.path.isfile(p):
                    continue
                seen.add(rp)
                items.append(self._library_item(
                    job=ENGINE.FOLDER_PREFIX + f["series"],
                    series=f["series"],
                    name=f["name"],
                    path=p,
                    entry={"outname": f["name"]},
                ))
            items.sort(key=lambda x: (x["series"].lower(), x["name"] or ""))
            # A missing folder is why a library can look empty when files do
            # exist elsewhere; the UI says so instead of "nothing downloaded".
            ddir = ENGINE.settings.get("download_dir") or ""
            return self._send(200, {"items": items, "count": len(items),
                                    "dir": ddir,
                                    "dir_missing": not os.path.isdir(ddir)})

        if path == "/api/progress":
            key = (qs.get("key") or [""])[0]
            return self._send(200, {"page": ENGINE.get_progress(key)})

        if path == "/api/job":
            jid = (qs.get("id") or [""])[0]
            j = ENGINE.get(jid)
            if not j:
                return self._send(404, {"error": "no such job"})
            return self._send(200, {
                "job": j.summary(),
                "items": j.items,
            })

        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        body = self._body()

        if path == "/api/add":
            urls = body.get("urls") or []
            if isinstance(urls, str):
                urls = [u.strip() for u in urls.splitlines() if u.strip()]
            added, errors = [], []
            for url in urls:
                try:
                    added.append(ENGINE.add(url))
                except Exception as e:  # noqa: BLE001
                    errors.append({"url": url, "error": str(e)})
            return self._send(200, {"added": added, "errors": errors})

        if path == "/api/progress":
            try:
                page = int(body.get("page") or 0)
                total = int(body.get("total") or 0)
            except (TypeError, ValueError):
                return self._send(400, {"error": "page/total must be numbers"})
            ENGINE.set_progress(body.get("key") or "", page, total)
            return self._send(200, {"ok": True})

        if path == "/api/retry":
            jid = body.get("id") or ""
            j = ENGINE.get(jid)
            if not j:
                return self._send(404, {"error": "no such job"})
            with ENGINE.lock:
                j.status = "queued"
                j.next_try_at = 0
                j.fatal = False
                j.error = ""
                j.message = "queued again"
                ENGINE.save_state()
            ENGINE.kick()
            return self._send(200, {"job": j.summary()})

        if path == "/api/confirm":
            jid = body.get("id") or ""
            try:
                return self._send(200, {"job": ENGINE.confirm(jid)})
            except Exception as e:  # noqa: BLE001
                return self._send(400, {"error": str(e)})

        if path == "/api/open-folder":
            import engine as _e
            jid = body.get("id") or ""
            j = ENGINE.get(jid)
            target = (j.dir if j and j.dir else ENGINE.settings["download_dir"])
            ok = _e.open_folder(target)
            return self._send(200, {"opened": ok, "path": target})

        if path == "/api/settings":
            with ENGINE.lock:
                for k, v in (body or {}).items():
                    if k not in ENGINE.settings:
                        continue
                    # `download_dir` must stay a real path string; coercing a
                    # number here would crash os.path.join downstream.
                    if k == "download_dir":
                        if not isinstance(v, str):
                            continue
                        # Store the trimmed value; "" means "use the default"
                        # and is resolved by Engine.download_root().
                        v = v.strip()
                    ENGINE.settings[k] = v
                ENGINE.save_state()
            return self._send(200, {"settings": ENGINE.settings})

        if path == "/api/pause":
            ENGINE.paused = bool(body.get("paused"))
            if not ENGINE.paused:
                ENGINE.kick()
            return self._send(200, {"paused": ENGINE.paused})

        if path == "/api/cancel":
            try:
                return self._send(200, {"job": ENGINE.cancel(body.get("id") or "")})
            except Exception as e:  # noqa: BLE001
                return self._send(400, {"error": str(e)})

        if path == "/api/move":
            # Two shapes: {id, index} jumps anywhere, {id, delta} shifts by one.
            jid = body.get("id") or ""
            try:
                if "index" in body:
                    ok = ENGINE.move_to(jid, int(body["index"]))
                else:
                    ok = ENGINE.move(jid, int(body.get("delta", 0)))
            except (TypeError, ValueError):
                return self._send(400, {"error": "index/delta must be a number"})
            if not ok:
                return self._send(409, {"error": "that job cannot be reordered"})
            return self._send(200, {"ok": True})

        if path == "/api/remove":
            try:
                return self._send(200, ENGINE.remove(body.get("id") or ""))
            except Exception as e:  # noqa: BLE001
                return self._send(400, {"error": str(e)})

        if path == "/api/clear-history":
            try:
                return self._send(200, ENGINE.clear_history())
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": str(e)})

        if path == "/api/delete":
            try:
                return self._send(200, ENGINE.delete_file(body.get("id") or "",
                                                          body.get("file") or ""))
            except Exception as e:  # noqa: BLE001
                return self._send(400, {"error": str(e)})

        return self._send(404, {"error": "not found"})


def main():
    argv = sys.argv[1:]

    def opt(flag, default):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return argv[i + 1]
        return default

    want = int(opt("--port", os.environ.get("IA_PORT", "8788")))
    host = opt("--host", os.environ.get("IA_HOST", "127.0.0.1"))
    no_browser = "--no-browser" in argv

    port = find_free_port(want, host)
    srv = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"

    # Resume anything interrupted by the last shutdown, and start the worker so
    # a job that was mid-download actually continues.
    ENGINE.kick()

    print(f"HardlyReading running at {url}")
    if host in ("0.0.0.0", "::"):
        print(f"  (also reachable on your LAN — listening on all interfaces)")

    if not no_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
        srv.shutdown()


if __name__ == "__main__":
    main()
