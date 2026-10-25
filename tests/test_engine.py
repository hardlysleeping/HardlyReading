"""Offline tests for the parts that do not need archive.org.

Everything here runs against a temporary config and download folder, so the
suite is safe on a machine with a real library and safe in CI. Anything that
would talk to the network is monkeypatched at the module boundary.
"""

import atexit
import http.client
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Point every path the engine computes at import time at a temp folder. This has
# to happen before `import engine`, because STATE_FILE/THUMB_DIR are module-level.
_TMP = tempfile.mkdtemp(prefix="hr-test-")
atexit.register(shutil.rmtree, _TMP, True)
os.environ["IA_CONFIG_DIR"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CONFIG_HOME"] = os.environ["IA_CONFIG_DIR"]
os.environ["IA_STATE"] = os.path.join(_TMP, "state.json")
os.environ["IA_HISTORY"] = os.path.join(_TMP, "history.db")
os.environ["IA_DOWNLOAD_DIR"] = os.path.join(_TMP, "library")
os.makedirs(os.environ["XDG_CONFIG_HOME"], exist_ok=True)

import engine  # noqa: E402
import server  # noqa: E402


def fresh_engine():
    """A clean Engine over the temp state, with no leftover jobs."""
    e = engine.Engine()
    with e.lock:
        e.jobs = []
        e.order = []
        e.categories = []
        e.save_state()
    return e


class PathsTest(unittest.TestCase):
    def test_config_lands_in_the_temp_folder(self):
        # IA_CONFIG_DIR has to win on every platform, otherwise a test run (or a
        # second install) writes into the real config file.
        self.assertTrue(engine.user_config_dir().startswith(_TMP))
        self.assertTrue(engine.THUMB_DIR.startswith(_TMP))
        self.assertTrue(engine.STATE_FILE.startswith(_TMP))
        self.assertTrue(engine.HISTORY_DB.startswith(_TMP))

    def test_documents_dir_is_absolute(self):
        docs = engine.documents_dir()
        self.assertTrue(os.path.isabs(docs))

    def test_download_override_wins(self):
        # The env var exists so a test instance cannot write into the real
        # library. It must beat the setting.
        e = fresh_engine()
        e.settings["download_dir"] = "/somewhere/else"
        self.assertEqual(e.download_root(), os.environ["IA_DOWNLOAD_DIR"])


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.e = fresh_engine()

    def split(self, total, jobs):
        self.e.settings["jobs_in_flight"] = total
        self.e.settings["parallel_jobs"] = jobs
        return self.e.file_concurrency()

    def test_budget_is_shared_not_multiplied(self):
        self.assertEqual(self.split(12, 1), 12)
        self.assertEqual(self.split(12, 2), 6)
        self.assertEqual(self.split(12, 3), 4)
        self.assertEqual(self.split(12, 4), 3)

    def test_never_zero_files_per_job(self):
        # 1 file over 4 jobs is still one each, not a job that downloads nothing.
        self.assertEqual(self.split(1, 4), 1)
        self.assertEqual(self.split(0, 2), 1)

    def test_jobs_are_clamped_to_four(self):
        self.e.settings["parallel_jobs"] = 99
        self.assertEqual(self.e.target_workers(), 4)
        self.e.settings["parallel_jobs"] = 0
        self.assertEqual(self.e.target_workers(), 1)
        self.e.settings["parallel_jobs"] = "nonsense"
        self.assertEqual(self.e.target_workers(), 2)


class QueueOrderTest(unittest.TestCase):
    def setUp(self):
        self.e = fresh_engine()
        self.ids = ["a", "b", "c"]
        for i in self.ids:
            self.e.jobs.append(engine.Job(id=i, kind="item", source="s", label=i,
                                          query=i, status="queued"))
            self.e.order.append(i)

    def test_move_to_top_and_bottom(self):
        self.assertTrue(self.e.move_to("c", 0))
        self.assertEqual(self.e.order[0], "c")
        self.assertTrue(self.e.move_to("c", 99))   # clamped, not an error
        self.assertEqual(self.e.order[-1], "c")

    def test_move_one_step(self):
        self.assertTrue(self.e.move("b", -1))
        self.assertEqual(self.e.order, ["b", "a", "c"])
        self.assertTrue(self.e.move("b", +1))
        self.assertEqual(self.e.order, ["a", "b", "c"])

    def test_running_job_cannot_be_reordered(self):
        self.e.get("b").status = "downloading"
        self.assertFalse(self.e.move_to("b", 0))
        self.assertEqual(self.e.order, ["a", "b", "c"])

    def test_unknown_job(self):
        self.assertFalse(self.e.move_to("nope", 0))
        self.assertFalse(self.e.move("nope", 1))


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.e = fresh_engine()
        self.job = engine.Job(id="r1", kind="collection", source="s", label="l",
                              query="madmagazine", status="paused")
        self.e.jobs.append(self.job)
        self.e.order.append("r1")

    def test_resume_with_known_items_skips_resolving(self):
        self.job.items = [{"name": "a.cbz"}]
        self.job.pause_reason = "not enough space"
        out = self.e.resume("r1")
        self.assertEqual(out["status"], "resume")
        self.assertEqual(self.job.pause_reason, "")

    def test_resume_without_items_re_resolves(self):
        self.job.items = []
        self.assertEqual(self.e.resume("r1")["status"], "queued")

    def test_resume_refuses_a_job_that_is_not_paused(self):
        self.job.status = "downloading"
        with self.assertRaises(ValueError):
            self.e.resume("r1")

    def test_resume_does_not_spend_a_retry(self):
        self.job.items = [{"name": "a.cbz"}]
        before = self.job.retries
        self.e.resume("r1")
        self.assertEqual(self.job.retries, before)


class FileHelpersTest(unittest.TestCase):
    def test_aria_input_is_removed_but_nothing_else(self):
        d = tempfile.mkdtemp(dir=_TMP)
        with open(os.path.join(d, "aria.txt"), "w") as fh:
            fh.write("https://example/x.cbz\n")
        with open(os.path.join(d, "x.cbz"), "wb") as fh:
            fh.write(b"data")
        engine.Engine._drop_aria(d)
        self.assertFalse(os.path.exists(os.path.join(d, "aria.txt")))
        self.assertTrue(os.path.exists(os.path.join(d, "x.cbz")))

    def test_drop_aria_tolerates_a_missing_file(self):
        engine.Engine._drop_aria(tempfile.mkdtemp(dir=_TMP))  # must not raise

    def test_sanitize_strips_characters_windows_rejects(self):
        out = engine.sanitize('Mad:  #12 <special>? "x" | y * z')
        for ch in ':<>?"|*':
            self.assertNotIn(ch, out)
        self.assertTrue(out.startswith("Mad"))

    def test_sanitize_collapses_whitespace_and_caps_length(self):
        self.assertEqual(engine.sanitize("a   b\n\tc"), "a b c")
        self.assertEqual(len(engine.sanitize("x" * 400)), 120)

    def test_identifier_from_urls(self):
        self.assertEqual(engine._ident_from_url("https://archive.org/details/mad-archive_202405"),
                         "mad-archive_202405")
        self.assertEqual(engine._ident_from_url("https://archive.org/download/foo_bar/x.cbz"),
                         "foo_bar")
        self.assertEqual(engine._ident_from_url("https://example.com/nothing"), "")

    def test_issue_number_and_year(self):
        self.assertEqual(engine.issue_number("Mad Magazine 012 (1954).cbz"), 12)
        self.assertEqual(engine.issue_year("Mad Magazine 012 (1954).cbz"), 1954)


class SearchPagingTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._real = engine.http_json

    def tearDown(self):
        engine.http_json = self._real

    def fake(self, payloads):
        def _f(url, timeout=60):
            self.calls.append(url)
            return payloads[min(len(self.calls) - 1, len(payloads) - 1)]
        engine.http_json = _f

    def test_normal_page_keeps_its_number(self):
        self.fake([{"response": {"numFound": 5, "start": 24, "docs": [
            {"identifier": "a", "title": "A", "description": "A"}]}}])
        out = engine.search_page("mad", page=2, rows=24)
        self.assertEqual(out["page"], 2)
        self.assertFalse(out["deep_paging"])
        self.assertEqual(len(out["docs"]), 1)
        self.assertEqual(out["docs"][0]["desc"], "")  # description repeats title

    def test_refused_deep_page_falls_back_to_one(self):
        self.fake([
            {"error": "deep paging is not supported past 10000"},
            {"response": {"numFound": 99, "start": 0, "docs": [{"identifier": "b", "title": "B"}]}},
        ])
        out = engine.search_page("mad", page=99999)
        self.assertEqual(out["page"], 1)
        self.assertTrue(out["deep_paging"])
        self.assertEqual(out["numFound"], 99)
        self.assertIn("page=1", self.calls[1])

    def test_two_refusals_return_an_empty_page_not_a_crash(self):
        self.fake([{"error": "nope"}])
        out = engine.search_page("mad", page=99999)
        self.assertEqual(out["docs"], [])
        self.assertTrue(out["deep_paging"])
        self.assertIn("nope", out["note"])


class DiskTest(unittest.TestCase):
    def test_report_reads_a_real_filesystem(self):
        e = fresh_engine()
        report = server.disk_report(e)
        self.assertEqual(report["path"], e.download_root())
        self.assertGreater(report["total"], 0)
        self.assertGreaterEqual(report["free"], 0)
        self.assertIn("thumbs", report["caches"])

    def test_report_survives_a_missing_library_folder(self):
        e = fresh_engine()
        e.settings["download_dir"] = os.path.join(_TMP, "nope", "deeper")
        report = server.disk_report(e)          # walks up to the nearest parent
        self.assertFalse(report["exists"])
        self.assertGreater(report["total"], 0)

    def test_clear_caches_empties_only_the_caches(self):
        for d in (engine.THUMB_DIR, engine.STREAM_DIR):
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "page.jpg"), "wb") as fh:
                fh.write(b"x" * 100)
        freed, removed = server.clear_caches()
        self.assertEqual(removed, 2)
        self.assertEqual(freed, 200)
        self.assertEqual(os.listdir(engine.THUMB_DIR), [])
        self.assertEqual(os.listdir(engine.STREAM_DIR), [])

    def test_free_bytes_returns_a_number(self):
        self.assertGreater(engine.free_bytes(_TMP), 0)


class ProgressTest(unittest.TestCase):
    def test_progress_round_trip_and_history_clear(self):
        e = fresh_engine()
        e.set_progress("abc", 14, 52)
        self.assertEqual(e.get_progress("abc"), 14)
        e.clear_history()
        self.assertEqual(e.get_progress("abc"), 0)


class AuthTest(unittest.TestCase):
    def tearDown(self):
        server.clear_password()

    def test_no_password_means_no_gate(self):
        server.clear_password()
        self.assertFalse(server.auth_required())

    def test_set_and_verify(self):
        server.set_password("correct-horse")
        self.assertTrue(server.auth_required())
        self.assertTrue(server.password_ok("correct-horse"))
        self.assertFalse(server.password_ok("wrong"))
        self.assertFalse(server.password_ok(""))

    def test_the_file_holds_a_hash_not_the_password(self):
        server.set_password("correct-horse")
        with open(server.AUTH_FILE) as fh:
            blob = fh.read()
        self.assertNotIn("correct-horse", blob)
        self.assertIn("pbkdf2_sha256", blob)
        if os.name != "nt":     # chmod 0600 is a POSIX guarantee; Windows uses ACLs
            self.assertEqual(stat.S_IMODE(os.stat(server.AUTH_FILE).st_mode), 0o600)

    def test_sessions(self):
        server.clear_password()
        token = server.new_session()
        self.assertTrue(server.session_valid(token))
        self.assertFalse(server.session_valid("invented"))
        self.assertFalse(server.session_valid(None))
        server._SESSIONS[token] = 1.0          # long expired
        self.assertFalse(server.session_valid(token))

    def test_clearing_signs_everyone_out(self):
        server.set_password("eight-chars")
        token = server.new_session()
        server.clear_password()
        self.assertFalse(server.auth_required())
        self.assertFalse(server.session_valid(token))

    def test_lockout_is_per_client(self):
        server.forget_login_failures()
        for _ in range(server.LOGIN_FAILS_MAX):
            server.note_login_failure("10.0.0.5")
        self.assertGreater(server.login_locked_for("10.0.0.5"), 0)
        self.assertEqual(server.login_locked_for("10.0.0.6"), 0)

    def test_session_table_is_capped(self):
        server.clear_password()
        server._SESSIONS.clear()
        for _ in range(server.MAX_SESSIONS + 20):
            server.new_session()
        self.assertLessEqual(len(server._SESSIONS), server.MAX_SESSIONS)


class CategoryTest(unittest.TestCase):
    def setUp(self):
        self.e = fresh_engine()
        self.e.add_category("Favourites")

    def test_add_and_duplicates(self):
        self.assertEqual(self.e.categories[0]["id"], "favourites")
        with self.assertRaises(ValueError):
            self.e.add_category("favourites")
        with self.assertRaises(ValueError):
            self.e.add_category("   ")

    def test_rename_rejects_a_name_already_in_use(self):
        self.e.add_category("Later")
        with self.assertRaises(ValueError):
            self.e.rename_category("later", "Favourites")
        self.e.rename_category("later", "Soon")
        self.assertEqual([c["name"] for c in self.e.categories], ["Favourites", "Soon"])

    def test_pinned_come_first_and_there_are_three(self):
        for n in ("A", "B", "C"):
            self.e.add_category(n)
        ids = [c["id"] for c in self.e.categories]
        for cid in ids[:3]:
            self.e.pin_category(cid, True)
        with self.assertRaises(ValueError):
            self.e.pin_category(ids[3], True)
        self.assertEqual([c["id"] for c in self.e.categories][:3], ids[:3])

    def test_an_unpinned_category_cannot_jump_a_pin(self):
        self.e.add_category("Later")
        self.e.pin_category("favourites", True)
        self.assertFalse(self.e.move_category("later", -1))

    def test_delete_returns_jobs_to_uncategorised(self):
        job = engine.Job(id="j1", kind="item", source="s", label="l", query="q",
                         category="favourites")
        self.e.jobs.append(job)
        self.e.order.append("j1")
        self.e.delete_category("favourites")
        self.assertEqual(job.category, "")
        self.assertEqual(self.e.categories, [])

    def test_unknown_ids_are_refused(self):
        with self.assertRaises(ValueError):
            self.e.rename_category("nope", "Something")
        with self.assertRaises(ValueError):
            self.e.delete_category("nope")
        with self.assertRaises(ValueError):
            self.e.pin_category("nope", True)

    def test_filing_a_job(self):
        job = engine.Job(id="j2", kind="item", source="s", label="l", query="q2")
        self.e.jobs.append(job)
        self.e.order.append("j2")
        self.assertEqual(self.e.set_job_category("j2", "favourites"), "favourites")
        self.assertEqual(self.e.set_job_category("j2", ""), "")
        with self.assertRaises(ValueError):
            self.e.set_job_category("j2", "nope")
        with self.assertRaises(ValueError):
            self.e.set_job_category("no-such-job", "")

    def test_categories_survive_a_restart(self):
        self.e.save_state()
        self.assertEqual([c["id"] for c in engine.Engine().categories], ["favourites"])


class HiddenTest(unittest.TestCase):
    def test_hide_and_unhide(self):
        e = fresh_engine()
        e.set_hidden("hidden-test-key", True)
        self.assertIn("hidden-test-key", e.hidden_keys())
        e.set_hidden("hidden-test-key", False)
        self.assertNotIn("hidden-test-key", e.hidden_keys())


class QuietHandler(server.Handler):
    def log_message(self, *a):
        pass


class GateTest(unittest.TestCase):
    """The gate itself, over HTTP, on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        server.clear_password()

    def tearDown(self):
        server.clear_password()

    def req(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(data))
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        raw, ctype = r.read(), r.getheader("Set-Cookie")
        conn.close()
        return r.status, raw, ctype

    def test_open_when_there_is_no_password(self):
        self.assertEqual(self.req("GET", "/api/state")[0], 200)

    def test_everything_but_the_shell_is_locked(self):
        server.set_password("correct-horse")
        self.assertEqual(self.req("GET", "/api/state")[0], 401)
        self.assertEqual(self.req("GET", "/api/library")[0], 401)
        self.assertEqual(self.req("POST", "/api/pause", {"paused": True})[0], 401)
        # the shell and the auth probe have to stay open or the gate cannot draw
        self.assertEqual(self.req("GET", "/")[0], 200)
        self.assertEqual(self.req("GET", "/api/auth")[0], 200)

    def test_login_sets_a_session_that_opens_the_api(self):
        server.set_password("correct-horse")
        status, _, cookie = self.req("POST", "/api/login", {"password": "correct-horse"})
        self.assertEqual(status, 200)
        self.assertIn("hr_session=", cookie or "")
        token = cookie.split("hr_session=")[1].split(";")[0]
        self.assertEqual(self.req("GET", "/api/state", cookie=f"hr_session={token}")[0], 200)

    def test_a_wrong_password_is_refused(self):
        server.set_password("correct-horse")
        self.assertEqual(self.req("POST", "/api/login", {"password": "nope"})[0], 401)

    def test_a_huge_body_is_refused_before_it_is_read(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/api/add")
        conn.putheader("Content-Length", str(server.Handler.MAX_BODY + 1))
        conn.endheaders()          # no body: the answer must not wait for one
        r = conn.getresponse()
        r.read()
        conn.close()
        self.assertEqual(r.status, 413)


class ListenTest(unittest.TestCase):
    def test_moving_to_loopback_never_falls_back_to_all_interfaces(self):
        real = server._open_listener
        seen = []

        def refuse(host, port):
            seen.append(host)
            raise OSError("in use")

        server._open_listener = refuse
        old = dict(server._LISTEN)
        server._LISTEN.update(srv=None, thread=None, host="0.0.0.0", port=8788)
        try:
            with self.assertRaises(RuntimeError):
                server.set_local_only(True)
            self.assertIn("127.0.0.1", seen)
            self.assertNotIn("0.0.0.0", seen)
        finally:
            server._open_listener = real
            server._LISTEN.update(old)

    def test_a_failed_swap_leaves_the_requested_setting_alone(self):
        real = server._open_listener
        server._open_listener = lambda host, port: (_ for _ in ()).throw(OSError("no"))
        old = dict(server._LISTEN)
        server._LISTEN.update(srv=None, thread=None, host="0.0.0.0", port=8788)
        # the settings endpoint saves the request first, then applies it
        server.ENGINE.settings["local_only"] = True
        try:
            server.Handler._apply_listen(True)
            # nothing is listening, so the flag must keep the request rather
            # than flip to False and come back on every interface next start
            self.assertTrue(server.ENGINE.settings["local_only"])
        finally:
            server._open_listener = real
            server._LISTEN.update(old)

    def test_local_means_loopback(self):
        h = object.__new__(server.Handler)
        h.client_address = ("127.0.0.1", 5000)
        self.assertTrue(h._is_local())
        h.client_address = ("10.0.0.7", 5000)
        self.assertFalse(h._is_local())


class CollectionBrowseTest(unittest.TestCase):
    """Opening a collection must list what the queue would resolve, and adding
    from it must queue only the picked issues."""

    def test_lists_members_with_paging_and_order(self):
        e = fresh_engine()
        docs = [{"identifier": f"item{i}", "title": f"Issue {i}",
                 "date": "1963-01-01", "year": "1963", "item_size": 1000 * i}
                for i in range(1, 4)]
        seen = {}

        def fake(query, rows=24, page=1, sort="", title_only=True,
                 extra_fields=None, **kw):
            seen.update(query=query, rows=rows, page=page, sort=sort,
                        title_only=title_only, extra=extra_fields)
            return {"numFound": 4242, "docs": docs}

        with mock.patch.object(engine, "search_page", fake):
            res = e.collection_items("madmagazine", offset=100, rows=50,
                                     order="newest")

        # Same scoping the queue's collection jobs use.
        self.assertEqual(seen["query"], "collection:madmagazine")
        self.assertFalse(seen["title_only"])
        self.assertEqual(seen["sort"], "date desc")
        self.assertIn("item_size", seen["extra"])
        # offset 100 with 50-row pages is page 3, on a page boundary.
        self.assertEqual(seen["page"], 3)
        self.assertEqual(res["total"], 4242)
        self.assertEqual(res["offset"], 100)
        self.assertEqual([i["id"] for i in res["items"]], ["item1", "item2", "item3"])
        self.assertEqual(res["items"][2]["size"], 3000)

    def test_marks_issues_already_in_the_library(self):
        e = fresh_engine()
        job = engine.Job(id="j1", kind="collection", source="x", label="x",
                         query="collection:madmagazine", status="done")
        job.items = [{"identifier": "item2", "outname": "Issue 2.pdf"}]
        with e.lock:
            e.jobs.append(job)
            e.order.append(job.id)

        def fake(query, rows=24, page=1, sort="", title_only=True,
                 extra_fields=None, **kw):
            return {"numFound": 2, "docs": [
                {"identifier": "item1", "title": "One"},
                {"identifier": "item2", "title": "Two"}]}

        with mock.patch.object(engine, "search_page", fake):
            res = e.collection_items("madmagazine")
        self.assertEqual(res["items"][1]["have"], "done")
        self.assertEqual(res["items"][0]["have"], "")

    def test_rejects_identifiers_that_are_not_identifiers(self):
        e = fresh_engine()
        for bad in ("", "../etc/passwd", "has space", "a" * 200):
            with self.assertRaises(ValueError):
                e.collection_items(bad)

    def test_add_many_queues_picks_and_reports_the_rest(self):
        e = fresh_engine()
        with mock.patch.object(e, "kick", lambda: None):
            first = e.add_many(["issue-a", "issue-b", "issue-a", "bad id!"])
            second = e.add_many(["issue-b", "issue-c"])
        self.assertEqual([j["label"] for j in first["added"]], ["issue-a", "issue-b"])
        self.assertEqual(first["skipped"], ["issue-a"])
        self.assertEqual(first["invalid"], ["bad id!"])
        self.assertEqual([j["label"] for j in second["added"]], ["issue-c"])
        self.assertEqual(second["skipped"], ["issue-b"])
        kinds = {j.query: j.kind for j in e.jobs}
        self.assertEqual(kinds["issue-c"], "item")
        self.assertEqual(len(e.jobs), 3)

    def test_add_many_refuses_an_unknown_category(self):
        e = fresh_engine()
        with mock.patch.object(e, "kick", lambda: None):
            e.add_many(["issue-a"], category="nope")
        self.assertEqual(e.jobs[0].category, "")


class HiddenCssTest(unittest.TestCase):
    """A class that sets `display` beats the `hidden` attribute, which is how the
    unlock overlay ended up covering an app that had no password."""

    def test_every_hidden_element_has_a_hidden_rule(self):
        html = open(os.path.join(ROOT, "index.html")).read()
        css = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
        classes = set()
        for tag in re.finditer(r"<[a-z]+[^>]*\bhidden\b[^>]*>", html):
            m = re.search(r'class="([^"]+)"', tag.group(0))
            if m:
                classes.update(m.group(1).split())
        rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        offenders = []
        for cls in classes:
            sets_display = any(
                "display:" in body and any(sel.strip() == f".{cls}" for sel in selector.split(","))
                for selector, body in rules
            )
            if sets_display and f".{cls}[hidden]" not in css:
                offenders.append(cls)
        self.assertEqual(offenders, [], "classes needing a [hidden] rule")


_real_kick = None


def setUpModule():
    """Never start real job workers.

    Nothing here asserts that a download completes — the queue tests assert state
    transitions — but a started worker really does try to resolve and download,
    which means live network calls and subprocesses, and daemon threads still
    doing that when the interpreter exits. That is what segfaulted the run on
    CPython 3.10 after all 71 tests had passed.
    """
    global _real_kick
    _real_kick = engine.Engine.kick
    engine.Engine.kick = lambda self: None


def tearDownModule():
    """Leave no background threads behind."""
    if _real_kick is not None:
        engine.Engine.kick = _real_kick
    engine.shutdown_workers()
    for t in threading.enumerate():
        if t is not threading.current_thread():
            t.join(3)


class WorkerShutdownTest(unittest.TestCase):
    def test_a_worker_returns_when_shutdown_is_set(self):
        e = fresh_engine()
        t = threading.Thread(target=e._run, daemon=True, name="job-worker")
        t.start()
        try:
            engine.shutdown_workers(timeout=5)
            self.assertFalse(t.is_alive(), "worker kept running through shutdown")
        finally:
            engine._SHUTDOWN.clear()    # leave the rest of the suite unaffected


class BackfillThreadTest(unittest.TestCase):
    """A background thread that only sleeps is a thread that is still running
    when the interpreter shuts down. On 3.10 that segfaulted the suite."""

    def tearDown(self):
        fresh_engine()          # leave the shared temp state empty for others

    def test_no_thread_when_there_is_nothing_to_backfill(self):
        fresh_engine()          # clear, then read that cleared state back
        e = engine.Engine()
        self.assertIsNone(e._backfill_thread)

    def test_a_job_without_a_description_starts_one(self):
        e = fresh_engine()
        job = engine.Job(id="j1", kind="item", source="x", label="x",
                         status="done")
        job.items = [{"identifier": "some-item", "outname": "a.pdf"}]
        with e.lock:
            e.jobs.append(job)
            e.save_state()          # the next engine reads state from disk
        with mock.patch.object(engine.Engine, "backfill_descriptions",
                              lambda self: None):
            e2 = engine.Engine()
        self.assertIsNotNone(e2._backfill_thread)
        e2._backfill_thread.join(5)


class AdvancedSearchTest(unittest.TestCase):
    """Advanced search has to reach archive.org as real query syntax, and only
    as syntax this app composed itself."""

    def test_no_fields_means_no_filters(self):
        self.assertEqual(engine.advanced_filters(), "")
        self.assertEqual(engine.advanced_filters(language="klingon",
                                                 year_from="", year_to=""), "")

    def test_years_are_validated_and_ordered(self):
        # UI sends the boxes in the order typed; the range is still a range.
        self.assertEqual(engine.advanced_filters(year_from="1979", year_to="1970"),
                         "year:[1970 TO 1979]")
        # One bound is an open range, not a broken one.
        self.assertEqual(engine.advanced_filters(year_from="1970"), "year:[1970 TO *]")
        self.assertEqual(engine.advanced_filters(year_to="1979"), "year:[* TO 1979]")
        # Junk is dropped rather than pasted in.
        self.assertEqual(engine.advanced_filters(year_from="19"), "")
        self.assertEqual(engine.advanced_filters(year_from="19xy"), "")

    def test_language_matches_the_tokens_the_index_uses(self):
        # Most items carry the ISO code, some the English word. Matching only
        # one of them silently returned 4 results instead of 621.
        self.assertEqual(engine.advanced_filters(language="English"),
                         "language:(eng OR English)")
        self.assertEqual(engine.advanced_filters(language="  german "),
                         "language:(ger OR deu OR German)")

    def test_a_quote_cannot_escape_its_phrase(self):
        # The value ends up inside a Lucene phrase; a stray quote would let it
        # become query syntax of the caller's choosing.
        got = engine.advanced_filters(creator='harvey" OR collection:secret')
        self.assertEqual(got, 'creator:("harvey OR collection:secret")')
        self.assertEqual(got.count('"'), 2)
        self.assertNotIn(") OR collection", got)

    def test_filters_and_sort_reach_the_request(self):
        seen = {}

        def fake_json(url):
            seen.setdefault("urls", []).append(url)
            return {"response": {"numFound": 1, "start": 0,
                                 "docs": [{"identifier": "x", "title": "X"}]}}

        with mock.patch.object(engine, "http_json", fake_json):
            engine.search_documents("mad magazine", rows=5,
                                    filters="year:[1970 TO 1979]",
                                    sort="publicdate asc")

        # Classifying the page costs one metadata call per result, so the search
        # URL is not necessarily the last one asked for.
        url = next(u for u in seen["urls"] if "advancedsearch" in u)
        self.assertIn("year%3A%5B1970+TO+1979%5D", url)      # the filter
        self.assertIn("sort%5B%5D=publicdate+asc", url)       # the sort
        # Scoped, then narrowed: the document restriction is not replaced.
        self.assertIn("mediatype%3A%28texts%29", url)

    def test_every_offered_sort_is_a_known_mode(self):
        # The UI offers these keys; a typo would silently fall back.
        for key in ("", "downloads", "newest", "oldest", "title"):
            self.assertIn(key, engine.SORT_MODES)
        self.assertEqual(engine.SORT_MODES["newest"], "publicdate desc")


class ShelfTest(unittest.TestCase):
    """Titles kept in the library with no file on disk."""

    def setUp(self):
        self.e = fresh_engine()
        with self.e.lock:
            self.e.shelf = []
            self.e.save_state()

    def tearDown(self):
        with self.e.lock:
            self.e.shelf = []
            self.e.save_state()

    def test_add_is_idempotent_and_updates_the_name(self):
        self.e.shelf_add("mad_1", "Mad 1")
        self.e.shelf_add("mad_1", "Mad Magazine 1")
        self.assertEqual(self.e.shelf_ids(), ["mad_1"])
        self.assertEqual(self.e.shelf[0]["name"], "Mad Magazine 1")

    def test_a_blank_identifier_is_refused(self):
        self.assertFalse(self.e.shelf_add("   "))
        self.assertEqual(self.e.shelf_ids(), [])

    def test_remove_takes_it_off_the_shelf(self):
        self.e.shelf_add("mad_2", "Mad 2")
        self.assertTrue(self.e.shelf_remove("mad_2"))
        self.assertEqual(self.e.shelf_ids(), [])

    def test_shelf_survives_a_restart(self):
        self.e.shelf_add("mad_3", "Mad 3")
        self.assertIn("mad_3", engine.Engine().shelf_ids())

    def test_stream_and_library_agree_on_the_progress_key(self):
        # A read from a search card and the shelf row in the library have to
        # name the same book, or reading it twice starts from page one again.
        self.assertEqual(engine.book_key({"identifier": "mad_4"}),
                         engine.book_key({"identifier": "mad_4", "desc": "blurb"}))



class FlattenTest(unittest.TestCase):
    """A dump unpacked into per-issue folders must collapse into one series."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hr-flat-")
        # download_root() prefers IA_DOWNLOAD_DIR (set for the whole suite), so
        # the parked pile lands there unless this test moves it too.
        self._env = os.environ.get("IA_DOWNLOAD_DIR")
        os.environ["IA_DOWNLOAD_DIR"] = self.tmp
        self.e = fresh_engine()
        self.e.settings["download_dir"] = self.tmp
        self.series = os.path.join(self.tmp, "Sick Magazine")
        os.makedirs(self.series)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("IA_DOWNLOAD_DIR", None)
        else:
            os.environ["IA_DOWNLOAD_DIR"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _issue(self, name, main=None, extra=True):
        main = main or f"{name}_jp2.cbz"
        d = os.path.join(self.series, name)
        os.makedirs(d)
        with open(os.path.join(d, main), "wb") as fh:
            fh.write(b"PK\x03\x04" + name.encode())
        if extra:
            open(os.path.join(d, name + "_djvu.txt"), "w").write("ocr")
            with open(os.path.join(d, name + "_text.pdf"), "wb") as fh:
                fh.write(b"%PDF-1.4 text")
        return d

    def test_nested_issues_become_one_folder(self):
        self._issue("sick01aug60")
        self._issue("sick02")
        r = self.e.flatten_series(self.series)
        self.assertEqual(r["folders"], 2)
        self.assertEqual(r["moved"], 2)
        files = sorted(os.listdir(self.series))
        self.assertEqual(files, ["sick01aug60.cbz", "sick02.cbz"])
        self.assertFalse([n for n in files if os.path.isdir(
            os.path.join(self.series, n))])

    def test_leftovers_are_moved_not_deleted(self):
        self._issue("sick03")
        self.e.flatten_series(self.series)
        park = os.path.join(self.tmp, engine.FLATTEN_DIR, "Sick Magazine", "sick03")
        self.assertTrue(os.path.isfile(os.path.join(park, "sick03_text.pdf")))
        self.assertTrue(os.path.isfile(os.path.join(park, "sick03_djvu.txt")))

    def test_parked_folder_is_invisible_to_the_library_scan(self):
        self._issue("sick04")
        self.e.flatten_series(self.series)
        names = [x["name"] for x in self.e.scan_folder()]
        self.assertIn("sick04.cbz", names)
        self.assertNotIn("sick04_text.pdf", names)

    def test_pdf_is_taken_when_there_is_no_archive(self):
        self._issue("sick05", main="sick05.pdf", extra=False)
        r = self.e.flatten_series(self.series)
        self.assertEqual(r["moved"], 1)
        self.assertEqual(os.listdir(self.series), ["sick05.pdf"])

    def test_a_flat_series_is_left_alone(self):
        open(os.path.join(self.series, "sick06.cbz"), "wb").write(b"PK\x03\x04")
        r = self.e.flatten_series(self.series)
        self.assertEqual((r["folders"], r["moved"]), (0, 0))
        self.assertEqual(os.listdir(self.series), ["sick06.cbz"])

    def test_library_flatten_walks_every_series(self):
        other = os.path.join(self.tmp, "Cracked Magazine")
        os.makedirs(other)
        for series, issue in ((self.series, "a"), (other, "b")):
            d = os.path.join(series, issue)
            os.makedirs(d)
            open(os.path.join(d, issue + ".cbz"), "wb").write(b"PK\x03\x04" + issue.encode())
        t = self.e.flatten_library()
        self.assertEqual(t["series"], 2)
        self.assertEqual(t["moved"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
