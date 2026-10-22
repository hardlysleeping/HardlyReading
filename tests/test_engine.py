"""Offline tests for the parts that do not need archive.org.

Everything here runs against a temporary config and download folder, so the
suite is safe on a machine with a real library and safe in CI. Anything that
would talk to the network is monkeypatched at the module boundary.
"""

import atexit
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Point every path the engine computes at import time at a temp folder. This has
# to happen before `import engine`, because STATE_FILE/THUMB_DIR are module-level.
_TMP = tempfile.mkdtemp(prefix="hr-test-")
atexit.register(shutil.rmtree, _TMP, True)
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
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
        e.save_state()
    return e


class PathsTest(unittest.TestCase):
    def test_config_lands_in_the_temp_folder(self):
        self.assertTrue(engine.user_config_dir().startswith(_TMP))
        self.assertTrue(engine.THUMB_DIR.startswith(_TMP))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
