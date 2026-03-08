"""
Tests for Leidos.py — state management, scraping helpers, and email.
All HTTP calls are mocked; no real network requests are made.
"""
import email as email_lib
import email.header
import json
import smtplib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import Leidos


# ── Helpers ──────────────────────────────────────────────────────────────────

def _decode_mime_subject(raw_msg: str) -> str:
    """Decode the Subject header from a raw MIME email string."""
    msg = email_lib.message_from_string(raw_msg)
    parts = email_lib.header.decode_header(msg.get("Subject", ""))
    return "".join(b.decode(enc or "utf-8") if isinstance(b, bytes) else b for b, enc in parts)


def _get_html_body(raw_msg: str) -> str:
    """Extract and decode the HTML body from a raw MIME email string."""
    msg = email_lib.message_from_string(raw_msg)
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            return payload.decode("utf-8") if payload else ""
    return raw_msg


def _mock_session(html: str) -> MagicMock:
    session = MagicMock()
    session.get.return_value.text = html
    session.get.return_value.raise_for_status = MagicMock()
    return session


SAMPLE_JOB = {
    "req": "R-00123",
    "title": "Data Analyst",
    "location": "Baltimore MD",
    "clearance": "None",
    "posted": "January 01, 2026",
    "url": "https://careers.leidos.com/jobs/12345",
}


# ── load_state ────────────────────────────────────────────────────────────────

class TestLoadState(unittest.TestCase):

    def test_returns_default_when_file_missing(self):
        with patch.object(Leidos, "STATE_FILE", Path("/nonexistent/state.json")):
            result = Leidos.load_state()
        self.assertEqual(result, {"known_reqs": [], "last_checked": None, "total_seen": 0})

    def test_reads_existing_file(self):
        data = {"known_reqs": ["R-001", "R-002"], "last_checked": "2026-01-01", "total_seen": 2}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(data, f)
            tmp = Path(f.name)
        try:
            with patch.object(Leidos, "STATE_FILE", tmp):
                result = Leidos.load_state()
            self.assertEqual(result["known_reqs"], ["R-001", "R-002"])
            self.assertEqual(result["total_seen"], 2)
        finally:
            tmp.unlink(missing_ok=True)


# ── save_state ────────────────────────────────────────────────────────────────

class TestSaveState(unittest.TestCase):

    def test_writes_readable_json(self):
        state = {"known_reqs": ["R-999"], "last_checked": "2026-01-01", "total_seen": 1}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            with patch.object(Leidos, "STATE_FILE", tmp):
                Leidos.save_state(state)
            result = json.loads(tmp.read_text(encoding="utf-8"))
            self.assertEqual(result["known_reqs"], ["R-999"])
        finally:
            tmp.unlink(missing_ok=True)

    def test_overwrites_existing_file(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            with patch.object(Leidos, "STATE_FILE", tmp):
                Leidos.save_state({"known_reqs": ["R-001"], "last_checked": None, "total_seen": 1})
                Leidos.save_state({"known_reqs": ["R-002"], "last_checked": None, "total_seen": 1})
            result = json.loads(tmp.read_text(encoding="utf-8"))
            self.assertEqual(result["known_reqs"], ["R-002"])
        finally:
            tmp.unlink(missing_ok=True)


# ── get_total_pages ───────────────────────────────────────────────────────────

class TestGetTotalPages(unittest.TestCase):

    def test_calculates_pages_from_count(self):
        session = _mock_session("Showing 1-25 of 50 results")
        self.assertEqual(Leidos.get_total_pages(session), 2)

    def test_rounds_up_partial_page(self):
        session = _mock_session("Showing 1-25 of 51 results")
        self.assertEqual(Leidos.get_total_pages(session), 3)

    def test_single_page_for_small_count(self):
        session = _mock_session("Showing 1-10 of 10 results")
        self.assertEqual(Leidos.get_total_pages(session), 1)

    def test_default_when_count_missing(self):
        session = _mock_session("<html>no count here</html>")
        self.assertEqual(Leidos.get_total_pages(session), 7)

    def test_default_on_request_exception(self):
        session = MagicMock()
        session.get.side_effect = Exception("network error")
        self.assertEqual(Leidos.get_total_pages(session), 7)


# ── get_job_urls_from_page ────────────────────────────────────────────────────

class TestGetJobUrlsFromPage(unittest.TestCase):

    def test_extracts_job_links(self):
        html = 'href="/jobs/12345-data-engineer" href="/jobs/67890-analyst"'
        urls = Leidos.get_job_urls_from_page(_mock_session(html), 1)
        self.assertIn("https://careers.leidos.com/jobs/12345-data-engineer", urls)
        self.assertIn("https://careers.leidos.com/jobs/67890-analyst", urls)

    def test_deduplicates_urls(self):
        html = 'href="/jobs/12345" href="/jobs/12345"'
        urls = Leidos.get_job_urls_from_page(_mock_session(html), 1)
        self.assertEqual(len(urls), 1)

    def test_strips_query_parameters(self):
        html = 'href="/jobs/12345?ref=search&page=1"'
        urls = Leidos.get_job_urls_from_page(_mock_session(html), 1)
        self.assertNotIn("?", urls[0])

    def test_returns_empty_on_request_failure(self):
        session = MagicMock()
        session.get.side_effect = Exception("timeout")
        self.assertEqual(Leidos.get_job_urls_from_page(session, 1), [])

    def test_page_2_uses_page_param_in_url(self):
        session = _mock_session("")
        Leidos.get_job_urls_from_page(session, 2)
        called_url = session.get.call_args[0][0]
        self.assertIn("page=2", called_url)


# ── get_job_detail ────────────────────────────────────────────────────────────

def _job_html(req="R-00123", title="Data Analyst", location="Baltimore MD",
              clearance="None", date="January 01, 2026") -> str:
    return (
        f"<html><h1>{title}</h1><body>"
        f"Job #: {req} Location: {location} Clearance: {clearance} {date}"
        f"</body></html>"
    )


class TestGetJobDetail(unittest.TestCase):

    def test_parses_all_fields(self):
        result = Leidos.get_job_detail(_mock_session(_job_html()), "https://careers.leidos.com/jobs/1")
        self.assertEqual(result["req"], "R-00123")
        self.assertEqual(result["title"], "Data Analyst")
        self.assertIn("Baltimore", result["location"])
        self.assertEqual(result["posted"], "January 01, 2026")
        self.assertEqual(result["url"], "https://careers.leidos.com/jobs/1")

    def test_returns_none_for_inactive_job(self):
        html = "<html><body>This job is no longer active. Please search for other jobs.</body></html>"
        result = Leidos.get_job_detail(_mock_session(html), "https://careers.leidos.com/jobs/1")
        self.assertIsNone(result)

    def test_returns_none_when_req_missing(self):
        html = "<html><h1>Some Job</h1><body>Location: MD</body></html>"
        result = Leidos.get_job_detail(_mock_session(html), "https://careers.leidos.com/jobs/1")
        self.assertIsNone(result)

    def test_returns_none_on_exception(self):
        session = MagicMock()
        session.get.side_effect = Exception("connection reset")
        result = Leidos.get_job_detail(session, "https://careers.leidos.com/jobs/1")
        self.assertIsNone(result)

    def test_fallback_title_from_url_slug(self):
        html = "<html><body>Job #: R-00999 Location: DC</body></html>"
        result = Leidos.get_job_detail(
            _mock_session(html),
            "https://careers.leidos.com/jobs/99999-senior-data-engineer"
        )
        self.assertIsNotNone(result)
        self.assertIn("Senior Data Engineer", result["title"])

    def test_see_posting_fallback_when_fields_missing(self):
        html = "<html><body>Job #: R-00456</body></html>"
        result = Leidos.get_job_detail(_mock_session(html), "https://careers.leidos.com/jobs/456")
        self.assertEqual(result["location"], "See posting")
        self.assertEqual(result["posted"], "See posting")


# ── send_email ────────────────────────────────────────────────────────────────

class TestSendEmail(unittest.TestCase):

    def _smtp_mock(self):
        mock_srv = MagicMock()
        smtp_cls = MagicMock()
        smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_srv)
        smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
        return smtp_cls, mock_srv

    def test_prints_to_console_when_no_smtp_pass(self):
        with patch.object(Leidos, "SMTP_PASS", ""):
            with patch("builtins.print") as mock_print:
                Leidos.send_email([SAMPLE_JOB])
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("SMTP_PASS", printed)

    def test_does_not_call_smtp_without_password(self):
        with patch.object(Leidos, "SMTP_PASS", ""), patch("smtplib.SMTP") as mock_smtp:
            Leidos.send_email([SAMPLE_JOB])
        mock_smtp.assert_not_called()

    def test_calls_smtp_when_password_set(self):
        smtp_cls, _ = self._smtp_mock()
        with patch.object(Leidos, "SMTP_PASS", "app-password"), patch("smtplib.SMTP", smtp_cls):
            Leidos.send_email([SAMPLE_JOB])
        smtp_cls.assert_called_once_with(Leidos.SMTP_HOST, Leidos.SMTP_PORT)

    def test_email_subject_singular(self):
        smtp_cls, mock_srv = self._smtp_mock()
        captured = []
        mock_srv.sendmail.side_effect = lambda f, t, m: captured.append(m)
        with patch.object(Leidos, "SMTP_PASS", "pwd"), patch("smtplib.SMTP", smtp_cls):
            Leidos.send_email([SAMPLE_JOB])
        subjects = [_decode_mime_subject(m) for m in captured]
        self.assertTrue(any("1 New Leidos Data Job " in s for s in subjects))

    def test_email_subject_plural(self):
        smtp_cls, mock_srv = self._smtp_mock()
        captured = []
        mock_srv.sendmail.side_effect = lambda f, t, m: captured.append(m)
        with patch.object(Leidos, "SMTP_PASS", "pwd"), patch("smtplib.SMTP", smtp_cls):
            Leidos.send_email([SAMPLE_JOB, SAMPLE_JOB, SAMPLE_JOB])
        subjects = [_decode_mime_subject(m) for m in captured]
        self.assertTrue(any("3 New Leidos Data Jobs" in s for s in subjects))

    def test_email_contains_job_title(self):
        smtp_cls, mock_srv = self._smtp_mock()
        captured = []
        mock_srv.sendmail.side_effect = lambda f, t, m: captured.append(m)
        with patch.object(Leidos, "SMTP_PASS", "pwd"), patch("smtplib.SMTP", smtp_cls):
            Leidos.send_email([SAMPLE_JOB])
        bodies = [_get_html_body(m) for m in captured]
        self.assertTrue(any("Data Analyst" in b for b in bodies))

    def test_smtp_failure_does_not_raise(self):
        smtp_cls = MagicMock()
        smtp_cls.return_value.__enter__ = MagicMock(side_effect=smtplib.SMTPException("auth failed"))
        smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
        with patch.object(Leidos, "SMTP_PASS", "bad-pwd"), patch("smtplib.SMTP", smtp_cls):
            try:
                Leidos.send_email([SAMPLE_JOB])
            except Exception as e:
                self.fail(f"send_email raised unexpectedly: {e}")


# ── run() integration ─────────────────────────────────────────────────────────

class TestRun(unittest.TestCase):

    def _state_tmp(self) -> Path:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        tmp.unlink()
        return tmp

    def test_init_mode_seeds_jobs_no_email(self):
        tmp = self._state_tmp()
        fake_job = {**SAMPLE_JOB, "req": "R-00123"}
        try:
            with patch.object(Leidos, "STATE_FILE", tmp), \
                 patch("Leidos.requests.Session"), \
                 patch("Leidos.get_total_pages", return_value=1), \
                 patch("Leidos.get_job_urls_from_page", return_value=[SAMPLE_JOB["url"]]), \
                 patch("Leidos.get_job_detail", return_value=fake_job), \
                 patch("Leidos.send_email") as mock_email:
                Leidos.run(init_mode=True)

            mock_email.assert_not_called()
            state = json.loads(tmp.read_text(encoding="utf-8"))
            self.assertIn("R-00123", state["known_reqs"])
            self.assertIsNotNone(state["last_checked"])
        finally:
            tmp.unlink(missing_ok=True)

    def test_check_mode_emails_new_jobs(self):
        tmp = self._state_tmp()
        fake_job = {**SAMPLE_JOB, "req": "R-99999"}
        try:
            with patch.object(Leidos, "STATE_FILE", tmp), \
                 patch("Leidos.requests.Session"), \
                 patch("Leidos.get_total_pages", return_value=1), \
                 patch("Leidos.get_job_urls_from_page", return_value=[fake_job["url"]]), \
                 patch("Leidos.get_job_detail", return_value=fake_job), \
                 patch("Leidos.send_email") as mock_email:
                Leidos.run(init_mode=False)

            mock_email.assert_called_once()
            sent_jobs = mock_email.call_args[0][0]
            self.assertEqual(sent_jobs[0]["req"], "R-99999")
        finally:
            tmp.unlink(missing_ok=True)

    def test_check_mode_no_email_when_all_known(self):
        tmp = self._state_tmp()
        fake_job = {**SAMPLE_JOB, "req": "R-00123"}
        existing_state = {"known_reqs": ["R-00123"], "last_checked": None, "total_seen": 1}
        tmp.write_text(json.dumps(existing_state), encoding="utf-8")
        try:
            with patch.object(Leidos, "STATE_FILE", tmp), \
                 patch("Leidos.requests.Session"), \
                 patch("Leidos.get_total_pages", return_value=1), \
                 patch("Leidos.get_job_urls_from_page", return_value=[fake_job["url"]]), \
                 patch("Leidos.get_job_detail", return_value=fake_job), \
                 patch("Leidos.send_email") as mock_email:
                Leidos.run(init_mode=False)

            mock_email.assert_not_called()
        finally:
            tmp.unlink(missing_ok=True)

    def test_state_updated_after_run(self):
        tmp = self._state_tmp()
        fake_job = {**SAMPLE_JOB, "req": "R-77777"}
        try:
            with patch.object(Leidos, "STATE_FILE", tmp), \
                 patch("Leidos.requests.Session"), \
                 patch("Leidos.get_total_pages", return_value=1), \
                 patch("Leidos.get_job_urls_from_page", return_value=[fake_job["url"]]), \
                 patch("Leidos.get_job_detail", return_value=fake_job), \
                 patch("Leidos.send_email"):
                Leidos.run(init_mode=True)

            state = json.loads(tmp.read_text(encoding="utf-8"))
            self.assertIn("R-77777", state["known_reqs"])
            self.assertIsNotNone(state["last_checked"])
            self.assertGreater(state["total_seen"], 0)
        finally:
            tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
