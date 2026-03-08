"""
Tests for LeidosPlaywright.py — state management and email (browser scraping mocked).
The Playwright browser is fully mocked; no real browser or network is used.
"""
import email as email_lib
import email.header
import json
import smtplib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).parent.parent))
import LeidosPlaywright


def _decode_mime_subject(raw_msg: str) -> str:
    msg = email_lib.message_from_string(raw_msg)
    parts = email_lib.header.decode_header(msg.get("Subject", ""))
    return "".join(b.decode(enc or "utf-8") if isinstance(b, bytes) else b for b, enc in parts)


def _get_html_body(raw_msg: str) -> str:
    msg = email_lib.message_from_string(raw_msg)
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            return payload.decode("utf-8") if payload else ""
    return raw_msg


SAMPLE_JOB = {
    "req": "R-00456",
    "title": "Senior Data Scientist",
    "location": "McLean VA",
    "clearance": "None",
    "posted": "February 15, 2026",
    "url": "https://careers.leidos.com/jobs/99999",
}


# ── load_state ────────────────────────────────────────────────────────────────

class TestLoadState(unittest.TestCase):

    def test_returns_default_when_file_missing(self):
        with patch.object(LeidosPlaywright, "STATE_FILE", Path("/nonexistent/state.json")):
            result = LeidosPlaywright.load_state()
        self.assertEqual(result, {"known_reqs": [], "last_checked": None, "total_seen": 0})

    def test_reads_existing_state_file(self):
        data = {"known_reqs": ["R-001"], "last_checked": "2026-02-01", "total_seen": 1}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(data, f)
            tmp = Path(f.name)
        try:
            with patch.object(LeidosPlaywright, "STATE_FILE", tmp):
                result = LeidosPlaywright.load_state()
            self.assertEqual(result["known_reqs"], ["R-001"])
        finally:
            tmp.unlink(missing_ok=True)


# ── save_state ────────────────────────────────────────────────────────────────

class TestSaveState(unittest.TestCase):

    def test_writes_json_to_file(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            with patch.object(LeidosPlaywright, "STATE_FILE", tmp):
                LeidosPlaywright.save_state({"known_reqs": ["R-500"], "last_checked": None, "total_seen": 1})
            result = json.loads(tmp.read_text(encoding="utf-8"))
            self.assertEqual(result["known_reqs"], ["R-500"])
        finally:
            tmp.unlink(missing_ok=True)


# ── send_email ────────────────────────────────────────────────────────────────

class TestSendEmail(unittest.TestCase):

    def _smtp_mock(self):
        mock_srv = MagicMock()
        smtp_cls = MagicMock()
        smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_srv)
        smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
        return smtp_cls, mock_srv

    def test_console_output_when_no_smtp_pass(self):
        with patch.object(LeidosPlaywright, "SMTP_PASS", ""):
            with patch("builtins.print") as mock_print:
                LeidosPlaywright.send_email([SAMPLE_JOB])
        output = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("SMTP_PASS", output)

    def test_no_smtp_call_without_password(self):
        with patch.object(LeidosPlaywright, "SMTP_PASS", ""), patch("smtplib.SMTP") as mock_smtp:
            LeidosPlaywright.send_email([SAMPLE_JOB])
        mock_smtp.assert_not_called()

    def test_connects_to_smtp_with_password(self):
        smtp_cls, _ = self._smtp_mock()
        with patch.object(LeidosPlaywright, "SMTP_PASS", "test-pass"), patch("smtplib.SMTP", smtp_cls):
            LeidosPlaywright.send_email([SAMPLE_JOB])
        smtp_cls.assert_called_once_with(LeidosPlaywright.SMTP_HOST, LeidosPlaywright.SMTP_PORT)

    def test_email_contains_job_details(self):
        smtp_cls, mock_srv = self._smtp_mock()
        captured = []
        mock_srv.sendmail.side_effect = lambda f, t, m: captured.append(m)
        with patch.object(LeidosPlaywright, "SMTP_PASS", "pwd"), patch("smtplib.SMTP", smtp_cls):
            LeidosPlaywright.send_email([SAMPLE_JOB])
        bodies = [_get_html_body(m) for m in captured]
        self.assertTrue(any("Senior Data Scientist" in b for b in bodies))
        self.assertTrue(any("McLean VA" in b for b in bodies))

    def test_subject_singular_one_job(self):
        smtp_cls, mock_srv = self._smtp_mock()
        captured = []
        mock_srv.sendmail.side_effect = lambda f, t, m: captured.append(m)
        with patch.object(LeidosPlaywright, "SMTP_PASS", "pwd"), patch("smtplib.SMTP", smtp_cls):
            LeidosPlaywright.send_email([SAMPLE_JOB])
        subjects = [_decode_mime_subject(m) for m in captured]
        self.assertTrue(any("1 New Leidos Data Job " in s for s in subjects))

    def test_subject_plural_multiple_jobs(self):
        smtp_cls, mock_srv = self._smtp_mock()
        captured = []
        mock_srv.sendmail.side_effect = lambda f, t, m: captured.append(m)
        with patch.object(LeidosPlaywright, "SMTP_PASS", "pwd"), patch("smtplib.SMTP", smtp_cls):
            LeidosPlaywright.send_email([SAMPLE_JOB] * 4)
        subjects = [_decode_mime_subject(m) for m in captured]
        self.assertTrue(any("4 New Leidos Data Jobs" in s for s in subjects))

    def test_smtp_error_handled_gracefully(self):
        smtp_cls = MagicMock()
        smtp_cls.return_value.__enter__ = MagicMock(side_effect=smtplib.SMTPException("auth error"))
        smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
        with patch.object(LeidosPlaywright, "SMTP_PASS", "bad"), patch("smtplib.SMTP", smtp_cls):
            try:
                LeidosPlaywright.send_email([SAMPLE_JOB])
            except Exception as e:
                self.fail(f"send_email raised unexpectedly: {e}")


# ── scrape_all_jobs (mocked browser) ─────────────────────────────────────────

class TestScrapeAllJobs(unittest.TestCase):

    def _make_playwright_mock(self, listing_html: str, detail_body: str):
        """Build a fully mocked Playwright context."""
        page = MagicMock()
        page.content.return_value = listing_html
        page.inner_text.return_value = "Showing 1-1 of 1"
        page.goto = MagicMock()
        page.wait_for_timeout = MagicMock()

        # Alternate content/inner_text calls for detail page
        page.inner_text.side_effect = [
            "Showing 1-1 of 1",  # listing page total count
            detail_body,         # detail page body
        ]
        page.content.side_effect = [
            listing_html,        # listing page links
            f"<h1>Data Analyst</h1>",  # detail page HTML for title
        ]

        ctx = MagicMock()
        ctx.new_page.return_value = page
        browser = MagicMock()
        browser.new_context.return_value = ctx
        pw = MagicMock()
        pw.chromium.launch.return_value = browser
        return pw, page

    def test_returns_list(self):
        listing = 'href="/jobs/12345-data-analyst"'
        detail = "Job #: R-00789 Location: Baltimore MD Clearance: None January 01, 2026"
        pw, _ = self._make_playwright_mock(listing, detail)

        with patch("LeidosPlaywright.sync_playwright") as mock_sp:
            mock_sp.return_value.__enter__ = MagicMock(return_value=pw)
            mock_sp.return_value.__exit__ = MagicMock(return_value=False)
            result = LeidosPlaywright.scrape_all_jobs(headless=True)

        self.assertIsInstance(result, list)

    def test_skips_inactive_jobs(self):
        listing = 'href="/jobs/12345"'
        detail = "Job #: R-00789 This job is no longer active"
        pw, page = self._make_playwright_mock(listing, detail)
        page.inner_text.side_effect = ["Showing 1-1 of 1", detail]
        page.content.side_effect = [listing, "<h1>Old Job</h1>"]

        with patch("LeidosPlaywright.sync_playwright") as mock_sp:
            mock_sp.return_value.__enter__ = MagicMock(return_value=pw)
            mock_sp.return_value.__exit__ = MagicMock(return_value=False)
            result = LeidosPlaywright.scrape_all_jobs(headless=True)

        self.assertEqual(result, [])


# ── run() integration ─────────────────────────────────────────────────────────

class TestRun(unittest.TestCase):

    def _tmp_state(self) -> Path:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        tmp.unlink()
        return tmp

    def test_init_mode_no_email_sent(self):
        tmp = self._tmp_state()
        try:
            with patch.object(LeidosPlaywright, "STATE_FILE", tmp), \
                 patch("LeidosPlaywright.scrape_all_jobs", return_value=[SAMPLE_JOB]), \
                 patch("LeidosPlaywright.send_email") as mock_email:
                LeidosPlaywright.run(init_mode=True)
            mock_email.assert_not_called()
        finally:
            tmp.unlink(missing_ok=True)

    def test_check_mode_emails_new_jobs(self):
        tmp = self._tmp_state()
        try:
            with patch.object(LeidosPlaywright, "STATE_FILE", tmp), \
                 patch("LeidosPlaywright.scrape_all_jobs", return_value=[SAMPLE_JOB]), \
                 patch("LeidosPlaywright.send_email") as mock_email:
                LeidosPlaywright.run(init_mode=False)
            mock_email.assert_called_once()
        finally:
            tmp.unlink(missing_ok=True)

    def test_known_jobs_not_re_emailed(self):
        tmp = self._tmp_state()
        existing = {"known_reqs": [SAMPLE_JOB["req"]], "last_checked": None, "total_seen": 1}
        tmp.write_text(json.dumps(existing), encoding="utf-8")
        try:
            with patch.object(LeidosPlaywright, "STATE_FILE", tmp), \
                 patch("LeidosPlaywright.scrape_all_jobs", return_value=[SAMPLE_JOB]), \
                 patch("LeidosPlaywright.send_email") as mock_email:
                LeidosPlaywright.run(init_mode=False)
            mock_email.assert_not_called()
        finally:
            tmp.unlink(missing_ok=True)

    def test_state_persisted_after_run(self):
        tmp = self._tmp_state()
        try:
            with patch.object(LeidosPlaywright, "STATE_FILE", tmp), \
                 patch("LeidosPlaywright.scrape_all_jobs", return_value=[SAMPLE_JOB]), \
                 patch("LeidosPlaywright.send_email"):
                LeidosPlaywright.run(init_mode=True)
            state = json.loads(tmp.read_text(encoding="utf-8"))
            self.assertIn(SAMPLE_JOB["req"], state["known_reqs"])
            self.assertIsNotNone(state["last_checked"])
        finally:
            tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
