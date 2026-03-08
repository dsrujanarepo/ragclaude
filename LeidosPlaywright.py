#!/usr/bin/env python3
"""
Leidos Job Monitor
==================
Checks https://careers.leidos.com for new data-related jobs in MD/VA
(no public trust / none clearance, professional level) and sends an HTML
email to d_srujana@yahoo.com when new postings are found.

Uses Playwright (headless Chromium) to bypass the site's bot protection,
which blocks plain HTTP requests with a 403 error.

First-time setup:
    1. Run:  bash setup_cron.sh
       (installs Playwright, seeds known jobs, schedules the cron)

Manual usage:
    python3 leidos_job_monitor.py --init   # seed without emailing
    python3 leidos_job_monitor.py          # check and email if new jobs
"""

import argparse
import json
import os
import re
import smtplib
import subprocess
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

def ensure_playwright():
    """Install playwright + chromium if not already present."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        print("  Installing playwright...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright", "-q"])
    # Download Chromium browser binary
    print("  Downloading Chromium (one-time, ~150 MB)...")
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
    return True

ensure_playwright()
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # noqa: E402

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

SEARCH_URL = (
    "https://careers.leidos.com/search/clearance/none-public-trust"
    "/job-level/professional/jobs/in/md-maryland-va-virginia"
    "/country/united-states?q=data"
)
BASE_URL = "https://careers.leidos.com"

# Email — Yahoo SMTP requires an App Password (not your main password).
# Generate one at: https://login.yahoo.com/account/security → App passwords
# Set SMTP_PASS as an environment variable, or paste it into setup_cron.sh
SMTP_HOST    = os.environ.get("SMTP_HOST",    "smtp.mail.yahoo.com")
SMTP_PORT    = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER    = os.environ.get("SMTP_USER",    "d_srujana@yahoo.com")
SMTP_PASS    = os.environ.get("SMTP_PASS",    "")          # ← required for email
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "d_srujana@yahoo.com")

SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR / "leidos_known_jobs.json"

# ─── STATE ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"known_reqs": [], "last_checked": None, "total_seen": 0}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"  State saved → {STATE_FILE}")

# ─── SCRAPING (Playwright) ────────────────────────────────────────────────────

def scrape_all_jobs(headless: bool = True) -> list[dict]:
    """
    Use a headless Chromium browser to scrape all job listings
    across every page, then fetch details for each job.
    Returns a list of job dicts with req, title, location, clearance,
    posted date, and url.
    """
    jobs = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        # ── Step 1: determine total pages ────────────────────
        print(f"  Loading search results page...")
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=40_000)
        page.wait_for_timeout(2000)

        content = page.content()
        total_match = re.search(r'Showing \d+-\d+ of (\d+)', page.inner_text("body"))
        total_jobs = int(total_match.group(1)) if total_match else 174
        total_pages = max(1, (total_jobs + 24) // 25)
        print(f"  Found {total_jobs} jobs across {total_pages} page(s).")

        # ── Step 2: collect job URLs from every page ─────────
        all_urls: list[str] = []
        for pg in range(1, total_pages + 1):
            if pg > 1:
                pg_url = SEARCH_URL.replace("?q=data", f"?page={pg}&q=data")
                page.goto(pg_url, wait_until="domcontentloaded", timeout=40_000)
                page.wait_for_timeout(1500)

            links = re.findall(r'href="(/jobs/\d+[^"]*)"', page.content())
            unique_pg = list(dict.fromkeys(
                f"{BASE_URL}{l.split('?')[0]}" for l in links
            ))
            all_urls.extend(unique_pg)
            print(f"    Page {pg}: {len(unique_pg)} job link(s)")

        all_urls = list(dict.fromkeys(all_urls))
        print(f"  Total unique job URLs: {len(all_urls)}")

        # ── Step 3: fetch each job page for details ───────────
        print("  Fetching job details (this takes ~2 min for 170+ jobs)...")
        for i, url in enumerate(all_urls, 1):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(500)
                body = page.inner_text("body")

                if "no longer active" in body.lower():
                    continue

                req   = re.search(r'Job #:\s*(R-\d+)', body)
                title = re.search(r'<h1[^>]*>([^<]+)</h1>', page.content())
                loc   = re.search(r'Location:\s*([^\n|]+)', body)
                clear = re.search(r'Clearance:\s*([^\n|]+)', body)
                dates = re.findall(
                    r'(?:January|February|March|April|May|June|July|August|'
                    r'September|October|November|December)\s+\d{1,2},\s+20\d\d',
                    body
                )

                if not req:
                    continue

                jobs.append({
                    "req":       req.group(1).strip(),
                    "title":     title.group(1).strip() if title
                                 else url.split("/")[-1].replace("-", " ").title(),
                    "location":  loc.group(1).strip()   if loc   else "See posting",
                    "clearance": clear.group(1).strip()  if clear else "See posting",
                    "posted":    dates[0]                if dates else "See posting",
                    "url":       url,
                })
                if i % 20 == 0:
                    print(f"    ...processed {i}/{len(all_urls)}")
            except PWTimeout:
                print(f"    Timeout on {url} — skipping")
            except Exception as e:
                print(f"    Error on {url}: {e} — skipping")

        browser.close()

    print(f"  Job details collected: {len(jobs)}")
    return jobs

# ─── EMAIL ────────────────────────────────────────────────────────────────────

def send_email(new_jobs: list):
    if not SMTP_PASS:
        print("\n⚠️  SMTP_PASS not set — skipping email (printing to console instead).")
        print(f"   Found {len(new_jobs)} new job(s):")
        for j in new_jobs:
            print(f"   • [{j['req']}] {j['title']}")
            print(f"     {j['location']} | Clearance: {j['clearance']} | Posted: {j['posted']}")
            print(f"     {j['url']}")
        print()
        return

    subject = f"🆕 {len(new_jobs)} New Leidos Data Job{'s' if len(new_jobs) > 1 else ''} in MD/VA!"

    rows = "".join(f"""
      <tr style="background:{'#fff' if i % 2 == 0 else '#f7f0fc'};">
        <td style="padding:10px 12px;">
          <a href="{j['url']}" style="color:#6b2d8b;font-weight:600;text-decoration:none;">{j['title']}</a>
        </td>
        <td style="padding:10px 12px;">{j['location']}</td>
        <td style="padding:10px 12px;">{j['clearance']}</td>
        <td style="padding:10px 12px;white-space:nowrap;">{j['posted']}</td>
        <td style="padding:10px 12px;font-family:monospace;font-size:12px;">{j['req']}</td>
      </tr>""" for i, j in enumerate(new_jobs))

    html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;background:#f4f4f4;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:900px;margin:30px auto;">
  <tr><td style="background:#6b2d8b;padding:24px 30px;border-radius:10px 10px 0 0;">
    <h1 style="color:white;margin:0;font-size:22px;">🆕 New Leidos Job Alerts</h1>
    <p style="color:#e0c5f8;margin:6px 0 0;font-size:14px;">
      Search: <em>data · MD/VA · No&nbsp;Clearance/Public Trust · Professional</em>
    </p>
  </td></tr>
  <tr><td style="background:white;padding:24px 30px;border:1px solid #ddd;border-top:none;">
    <p style="margin:0 0 18px;">Hi Srujana — <strong>{len(new_jobs)} new posting{'s' if len(new_jobs) > 1 else ''}</strong>
       appeared since the last check on <strong>{datetime.now().strftime('%B %d, %Y')}</strong>.</p>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;border:1px solid #e0d0f0;border-radius:6px;overflow:hidden;">
      <thead>
        <tr style="background:#6b2d8b;">
          <th style="padding:10px 12px;color:white;text-align:left;">Job Title</th>
          <th style="padding:10px 12px;color:white;text-align:left;">Location</th>
          <th style="padding:10px 12px;color:white;text-align:left;">Clearance</th>
          <th style="padding:10px 12px;color:white;text-align:left;">Posted</th>
          <th style="padding:10px 12px;color:white;text-align:left;">Req #</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    <p style="margin:24px 0 0;">
      <a href="{SEARCH_URL}"
         style="background:#6b2d8b;color:white;padding:11px 22px;border-radius:6px;
                text-decoration:none;font-weight:bold;display:inline-block;">
        View All Leidos Jobs →
      </a>
    </p>
  </td></tr>
  <tr><td style="padding:12px 0;text-align:center;font-size:12px;color:#999;">
    Automated alert · runs every 3 days · managed by leidos_job_monitor.py
  </td></tr>
</table>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"  ✅ Email sent to {NOTIFY_EMAIL} ({len(new_jobs)} new job(s)).")
    except Exception as e:
        print(f"  ❌ Email failed: {e}")
        print("     Check your SMTP_PASS (Yahoo App Password) and SMTP settings.")

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run(init_mode: bool = False):
    label = "INIT (seeding known jobs)" if init_mode else "CHECK"
    print(f"\n{'='*60}")
    print(f"  Leidos Job Monitor [{label}]")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    state = load_state()
    known_reqs = set(state.get("known_reqs", []))
    print(f"  Jobs already on file: {len(known_reqs)}")

    # Scrape using headless browser (bypasses 403 bot-protection)
    all_jobs = scrape_all_jobs(headless=True)
    all_reqs_seen = {j["req"] for j in all_jobs}

    new_jobs = []
    for job in all_jobs:
        if job["req"] not in known_reqs:
            tag = "(seeding)" if init_mode else "🆕 NEW"
            print(f"    {tag}: {job['req']} — {job['title']} [{job['location']}]")
            if not init_mode:
                new_jobs.append(job)

    print(f"\n  Active jobs found : {len(all_reqs_seen)}")

    if init_mode:
        print(f"  Init complete — {len(all_reqs_seen)} jobs seeded. No email sent.")
    else:
        print(f"  New jobs         : {len(new_jobs)}")
        if new_jobs:
            send_email(new_jobs)
        else:
            print("  No new postings — no email sent.")

    state["known_reqs"]   = sorted(known_reqs | all_reqs_seen)
    state["last_checked"] = datetime.now().isoformat()
    state["total_seen"]   = len(known_reqs | all_reqs_seen)
    save_state(state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor Leidos jobs and email alerts.")
    parser.add_argument(
        "--init", action="store_true",
        help="Seed the known-jobs list without sending any email (run once before scheduling)."
    )
    args = parser.parse_args()
    run(init_mode=args.init)