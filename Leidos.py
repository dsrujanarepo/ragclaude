#!/usr/bin/env python3
"""
Leidos Job Monitor
==================
Checks https://careers.leidos.com for new data-related jobs in MD/VA
(no public trust / none clearance, professional level) and sends an HTML
email to d_srujana@yahoo.com when new postings are found.

First-time setup:
    1. Edit SMTP settings below (or export env variables).
    2. Run:  python3 leidos_job_monitor.py --init
       This seeds the known-jobs list so you won't be spammed.
    3. Run:  bash setup_cron.sh
       This schedules automatic checks every 3 days.

Subsequent runs (done automatically by cron):
    python3 leidos_job_monitor.py
"""

import argparse
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
except ImportError:
    print("Installing requests...")
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

SEARCH_URL = (
    "https://careers.leidos.com/search/clearance/none-public-trust"
    "/job-level/professional/jobs/in/md-maryland-va-virginia"
    "/country/united-states?q=data"
)
BASE_URL = "https://careers.leidos.com"

# Email settings — set SMTP_PASS via environment variable for security
# Yahoo SMTP requires an "App Password" (not your main password).
# Generate one at: https://login.yahoo.com/account/security → App passwords
SMTP_HOST    = os.environ.get("SMTP_HOST",    "smtp.mail.yahoo.com")
SMTP_PORT    = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER    = os.environ.get("SMTP_USER",    "d_srujana@yahoo.com")
SMTP_PASS    = os.environ.get("SMTP_PASS",    "")          # ← set this!
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "d_srujana@yahoo.com")

# State file — sits next to this script
SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR / "leidos_known_jobs.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

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

# ─── SCRAPING ─────────────────────────────────────────────────────────────────

def get_total_pages(session: requests.Session) -> int:
    try:
        r = session.get(SEARCH_URL, timeout=25, headers=HEADERS)
        r.raise_for_status()
        m = re.search(r'Showing \d+-\d+ of (\d+)', r.text)
        if m:
            return max(1, (int(m.group(1)) + 24) // 25)
    except Exception as e:
        print(f"  Warning: couldn't read page count: {e}")
    return 7


def get_job_urls_from_page(session: requests.Session, page: int) -> list:
    url = SEARCH_URL if page == 1 else SEARCH_URL.replace("?q=data", f"?page={page}&q=data")
    try:
        r = session.get(url, timeout=25, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"  Warning: page {page} failed: {e}")
        return []
    links = re.findall(r'href="(/jobs/\d+[^"]*)"', r.text)
    seen, unique = set(), []
    for l in links:
        if l not in seen:
            seen.add(l)
            unique.append(urljoin(BASE_URL, l.split("?")[0]))
    return unique


def get_job_detail(session: requests.Session, url: str) -> dict | None:
    try:
        r = session.get(url, timeout=25, headers=HEADERS)
        html = r.text
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text)

        if "no longer active" in text.lower():
            return None

        req   = re.search(r'Job #:\s*(R-\d+)', text)
        title = re.search(r'<h1[^>]*>([^<]+)</h1>', html)
        loc   = re.search(r'Location:\s*([^\n<|]+)', text)
        clear = re.search(r'Clearance:\s*([^\n<|]+)', text)
        dates = re.findall(
            r'(?:January|February|March|April|May|June|July|August|'
            r'September|October|November|December)\s+\d{1,2},\s+20\d\d',
            text
        )

        if not req:
            return None

        return {
            "req":       req.group(1).strip(),
            "title":     title.group(1).strip() if title else url.split("/")[-1].replace("-", " ").title(),
            "location":  loc.group(1).strip()   if loc   else "See posting",
            "clearance": clear.group(1).strip()  if clear else "See posting",
            "posted":    dates[0]                if dates else "See posting",
            "url":       url,
        }
    except Exception as e:
        print(f"  Warning: job detail failed ({url}): {e}")
        return None

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

    session = requests.Session()
    total_pages = get_total_pages(session)
    print(f"  Pages to scan: {total_pages}")

    all_urls = []
    for page in range(1, total_pages + 1):
        links = get_job_urls_from_page(session, page)
        all_urls.extend(links)
        print(f"    Page {page}: {len(links)} job link(s)")

    all_urls = list(dict.fromkeys(all_urls))
    print(f"  Unique job URLs collected: {len(all_urls)}")
    print("  Fetching job details...")

    new_jobs, all_reqs_seen = [], set()
    for url in all_urls:
        detail = get_job_detail(session, url)
        if not detail:
            continue
        all_reqs_seen.add(detail["req"])
        if detail["req"] not in known_reqs:
            tag = "(seeding)" if init_mode else "🆕 NEW"
            print(f"    {tag}: {detail['req']} — {detail['title']} [{detail['location']}]")
            if not init_mode:
                new_jobs.append(detail)

    print(f"\n  Active jobs found: {len(all_reqs_seen)}")

    if init_mode:
        print(f"  Init complete — {len(all_reqs_seen)} jobs seeded as 'known'. No email sent.")
    else:
        print(f"  New jobs: {len(new_jobs)}")
        if new_jobs:
            send_email(new_jobs)
        else:
            print("  No new postings — no email sent.")

    state["known_reqs"]  = sorted(known_reqs | all_reqs_seen)
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