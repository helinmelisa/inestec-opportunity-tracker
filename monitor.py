#!/usr/bin/env python3
"""
INESC TEC Opportunity Monitor
Opens a local web page in your browser for settings & live log.
Monitors inesctec.pt/en/opportunities for new openings matching
Bachelor's qualification + CS/AI/CV work areas and sends email alerts.
"""
from __future__ import annotations

import collections
import json
import os
import queue
import re
import smtplib
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPPORTUNITIES_URL = "https://www.inesctec.pt/en/opportunities/list?type=open"

# Keywords matched against Work Area (case-insensitive, partial match)
WORK_AREA_KEYWORDS = [
    "computer vision",
    "machine learning",
    "deep learning",
    "artificial intelligence",
    "computer science",
    "computer engineering",
    "software engineering",
    "data science",
    "image processing",
    "natural language processing",
    "nlp",
    "neural network",
    "pattern recognition",
    "signal processing",
    "algorithm",
    "programming",
    "robotics",
    "autonomous",
    "perception",
]

# Academic qualifications that qualify (case-insensitive)
QUALIFICATION_KEYWORDS = ["bachelor"]

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".inesctec_monitor_config.json")
STATE_FILE  = os.path.join(os.path.expanduser("~"), ".inesctec_monitor_state.json")
LOG_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.log")
PORT        = 8766
HEADERS     = {"User-Agent": "Mozilla/5.0 (compatible; INESCTECMonitor/1.0)"}

LOG_MAX_LINES  = 500   # lines kept in monitor.log
STATE_MAX_REFS = 500   # max seen_refs entries kept in state

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_config: dict        = {}
_state:  dict        = {"seen_refs": [], "matched_opps": {}}
_monitor             = None
_status: str         = "Not running"
_log_history         = collections.deque(maxlen=300)
_sse_clients: list   = []
_sse_lock            = threading.Lock()

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _rotate_log():
    """Trim monitor.log to the last LOG_MAX_LINES lines."""
    if not os.path.exists(LOG_FILE):
        return
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if len(lines) > LOG_MAX_LINES:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-LOG_MAX_LINES:])
    except Exception:
        pass


def _prune_state():
    """Keep state compact: cap seen_refs and drop matched_opps expired > 7 days."""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=7)).isoformat()

    refs = _state.get("seen_refs", [])
    if len(refs) > STATE_MAX_REFS:
        _state["seen_refs"] = refs[-STATE_MAX_REFS:]

    opps = _state.get("matched_opps", {})
    stale = [ref for ref, opp in opps.items() if opp.get("deadline", "9999") < cutoff]
    for ref in stale:
        del opps[ref]

    if stale or len(refs) > STATE_MAX_REFS:
        save_json(STATE_FILE, _state)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str):
    ts   = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    _log_history.append(line)
    with _sse_lock:
        for q in _sse_clients:
            try:
                q.put_nowait(line)
            except queue.Full:
                pass


def _set_status(msg: str):
    global _status
    _status = msg

# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def fetch_opportunities() -> list[dict]:
    """Fetch all open opportunities from the listing page.

    Table has 10 cells per data row:
      [0] empty  [1] Ref  [2] Position  [3] Qualification  [4] Work Area
      [5] Centre  [6] Advisor  [7] Deadline+"Apply now"  [8] counter  [9] links
    """
    resp = requests.get(OPPORTUNITIES_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    table = soup.find("table")
    if not table:
        return []

    opportunities = []
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 9:
            continue

        ref = cells[1].get_text(strip=True)
        if not ref.startswith("AE"):
            continue

        # Deadline cell contains "2026-05-14Apply now" — strip the suffix
        raw_deadline = cells[7].get_text(strip=True)
        deadline = raw_deadline.replace("Apply now", "").strip()

        # URL is in the last link cell
        url = ""
        for a in cells[9].find_all("a", href=True):
            href = a["href"]
            if "/opportunities/" in href:
                url = href if href.startswith("http") else f"https://www.inesctec.pt{href}"
                break

        opportunities.append({
            "ref":           ref,
            "position":      cells[2].get_text(strip=True),
            "qualification": cells[3].get_text(strip=True),
            "work_area":     cells[4].get_text(strip=True),
            "centre":        cells[5].get_text(strip=True),
            "advisor":       cells[6].get_text(strip=True),
            "deadline":      deadline,
            "url":           url,
        })

    return opportunities


def fetch_detail_summary(url: str) -> str:
    """Fetch the detail page and extract the description/summary text."""
    if not url:
        return ""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Try common content containers
        for selector in [".opportunity-description", ".content-body", "article", "main .content", ".job-description"]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(" ", strip=True)
                if len(text) > 100:
                    return text[:800] + ("…" if len(text) > 800 else "")

        # Fallback: grab all <p> tags in main content
        paragraphs = []
        for p in soup.select("main p, .main p, article p"):
            t = p.get_text(strip=True)
            if len(t) > 40:
                paragraphs.append(t)
        if paragraphs:
            combined = " ".join(paragraphs[:4])
            return combined[:800] + ("…" if len(combined) > 800 else "")
    except Exception:
        pass
    return ""


def is_matching(opp: dict) -> tuple[bool, list[str]]:
    """Return (matches, matched_keywords) for qualification + work area filters."""
    qual_lower = opp["qualification"].lower()
    qual_ok = any(kw in qual_lower for kw in QUALIFICATION_KEYWORDS)
    if not qual_ok:
        return False, []

    area_lower = opp["work_area"].lower()
    matched = [kw for kw in WORK_AREA_KEYWORDS if kw in area_lower]
    return bool(matched), matched

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(cfg: dict, subject: str, body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["email_from"]
    msg["To"]      = cfg["email_to"]
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP(cfg.get("smtp_host", "smtp.gmail.com"),
                      int(cfg.get("smtp_port", 587))) as s:
        s.ehlo()
        s.starttls()
        s.login(cfg.get("smtp_user", cfg["email_from"]), cfg["smtp_pass"])
        s.sendmail(cfg["email_from"], cfg["email_to"], msg.as_string())


def _writer_url(opp: dict) -> str:
    """Deep-link into the letter writer with this opportunity pre-filled."""
    params = urllib.parse.urlencode({
        "ref":       opp.get("ref", ""),
        "work_area": opp.get("work_area", ""),
        "centre":    opp.get("centre", ""),
        "advisor":   opp.get("advisor", ""),
        "deadline":  opp.get("deadline", ""),
        "url":       opp.get("url", ""),
        "summary":   opp.get("summary", "")[:500],
        "position":  opp.get("position", ""),
    })
    return f"http://localhost:8767/?{params}"


def build_email_body(matches: list[dict]) -> str:
    cards = []
    for opp in matches:
        kw_chips = "".join(
            f'<span style="display:inline-block;background:#e8f5e9;color:#2e7d32;'
            f'border-radius:12px;padding:2px 10px;font-size:.8rem;margin:2px;">{kw}</span>'
            for kw in opp.get("matched_keywords", [])
        )
        summary_html = ""
        if opp.get("summary"):
            summary_html = (
                f'<p style="color:#555;font-size:.88rem;margin:8px 0 0 0;line-height:1.6;">'
                f'{opp["summary"]}</p>'
            )

        # AI-generated draft letter block
        letter_html = ""
        if opp.get("draft_letter"):
            letter_text = opp["draft_letter"].replace("\n", "<br>")
            letter_html = f"""
            <div style="margin-top:14px;padding:14px 16px;background:#f8f9fa;
                        border-left:3px solid #1a237e;border-radius:0 6px 6px 0;">
              <div style="font-size:.78rem;font-weight:700;color:#1a237e;
                          letter-spacing:.5px;margin-bottom:8px;">✦ AI DRAFT LETTER</div>
              <div style="font-size:.85rem;color:#333;line-height:1.7;">{letter_text}</div>
            </div>"""

        writer_link = _writer_url(opp)
        buttons = f"""
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:12px;">
          {"" if opp.get("url") else ""}
          <a href="{opp.get('url','#')}" style="display:inline-block;background:#388e3c;color:#fff;
             padding:7px 16px;border-radius:6px;text-decoration:none;font-size:.85rem;font-weight:600;">
            Apply Now →
          </a>
          <a href="{writer_link}" style="display:inline-block;background:#1a237e;color:#fff;
             padding:7px 16px;border-radius:6px;text-decoration:none;font-size:.85rem;font-weight:600;">
            ✦ Open in Letter Writer
          </a>
        </div>"""

        cards.append(f"""
        <div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px 20px;margin-bottom:16px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;">
            <div>
              <span style="font-size:.78rem;color:#888;font-weight:600;">{opp['ref']}</span>
              <h3 style="margin:2px 0 4px 0;font-size:1rem;color:#1a1a1a;">{opp['work_area']}</h3>
              <span style="font-size:.85rem;color:#555;">{opp['position']}</span>
            </div>
            <div style="text-align:right;">
              <div style="font-size:.82rem;color:#c62828;font-weight:600;">⏰ Deadline: {opp['deadline']}</div>
              <div style="font-size:.82rem;color:#555;margin-top:2px;">{opp['centre']}</div>
            </div>
          </div>
          <div style="margin:8px 0 4px 0;">
            <span style="font-size:.82rem;color:#555;">👤 {opp['advisor']}</span>
            &nbsp;&nbsp;
            <span style="font-size:.82rem;background:#e3f2fd;color:#1565c0;padding:2px 8px;border-radius:10px;">
              🎓 {opp['qualification']}
            </span>
          </div>
          <div style="margin:6px 0;">{kw_chips}</div>
          {summary_html}
          {letter_html}
          {buttons}
        </div>""")

    cards_html = "\n".join(cards)
    count = len(matches)
    return f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
background:#f5f5f5;padding:20px;">
<div style="max-width:700px;margin:0 auto;">
  <div style="background:#2e7d32;color:#fff;padding:20px 24px;border-radius:8px 8px 0 0;">
    <h2 style="margin:0;font-size:1.2rem;">🔬 INESC TEC — {count} New Opportunit{'y' if count==1 else 'ies'} for You</h2>
    <p style="margin:6px 0 0 0;opacity:.85;font-size:.88rem;">
      Bachelor's qualification · CS / AI / Computer Vision areas
    </p>
  </div>
  <div style="background:#fff;padding:20px 24px;border-radius:0 0 8px 8px;
              box-shadow:0 2px 8px rgba(0,0,0,.08);">
    {cards_html}
    <hr style="border:none;border-top:1px solid #eee;margin:16px 0;">
    <p style="color:#aaa;font-size:.75rem;text-align:center;">
      Sent by INESC TEC Monitor ·
      <a href="{OPPORTUNITIES_URL}" style="color:#aaa;">View all opportunities</a>
    </p>
  </div>
</div>
</body></html>"""

# ---------------------------------------------------------------------------
# Monitor thread
# ---------------------------------------------------------------------------

class MonitorThread(threading.Thread):
    def __init__(self, cfg: dict, interval_minutes: int):
        super().__init__(daemon=True)
        self.cfg      = cfg
        self.interval = interval_minutes * 60
        self._stop    = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            _set_status("Checking for new opportunities…")
            try:
                self._check()
            except Exception as exc:
                _log(f"[ERROR] {exc}")
            now  = datetime.now().strftime("%H:%M:%S")
            mins = self.cfg.get("interval", 60)
            _set_status(f"Last check: {now} — next in {mins} min")
            self._stop.wait(self.interval)

    def _check(self):
        seen = set(_state.get("seen_refs", []))
        try:
            opportunities = fetch_opportunities()
        except Exception as exc:
            _log(f"[ERROR] Failed to fetch opportunities: {exc}")
            return

        _log(f"Fetched {len(opportunities)} open opportunit{'y' if len(opportunities)==1 else 'ies'}.")

        new_opps   = [o for o in opportunities if o["ref"] not in seen]
        notify     = []

        for opp in new_opps:
            seen.add(opp["ref"])
            matches, keywords = is_matching(opp)
            if matches:
                _log(f"  MATCH [{', '.join(keywords)}] — {opp['ref']}: {opp['work_area'][:60]}")
                opp["matched_keywords"] = keywords
                if self.cfg.get("fetch_details", True):
                    opp["summary"] = fetch_detail_summary(opp["url"])
                # Generate draft letter if Ollama is available
                if self.cfg.get("generate_letters", True):
                    try:
                        from letter_generator import generate_letter, ollama_available
                        if ollama_available():
                            model = self.cfg.get("ollama_model", "llama3.1:8b")
                            _log(f"    Generating draft letter with {model}…")
                            opp["draft_letter"] = generate_letter(opp, model=model)
                            _log(f"    Draft letter ready.")
                        else:
                            _log(f"    Ollama not running — skipping letter generation.")
                    except Exception as exc:
                        _log(f"    [LETTER ERROR] {exc}")
                # Persist to state (strip draft_letter — too large)
                store = {k: v for k, v in opp.items() if k != "draft_letter"}
                _state.setdefault("matched_opps", {})[opp["ref"]] = store
                notify.append(opp)
            else:
                _log(f"  skip — {opp['ref']}: {opp['work_area'][:55]} ({opp['qualification']})")

        _state["seen_refs"] = list(seen)
        save_json(STATE_FILE, _state)

        if not new_opps:
            _log("No new opportunities since last check.")
            return

        if notify:
            try:
                subject = (f"[INESC TEC] {len(notify)} new opportunit"
                           f"{'y' if len(notify)==1 else 'ies'} matching your filters")
                send_email(self.cfg, subject, build_email_body(notify))
                _log(f"  Email sent to {self.cfg['email_to']}")
            except Exception as exc:
                _log(f"  [EMAIL ERROR] {exc}")
        else:
            _log(f"  {len(new_opps)} new opportunit{'y' if len(new_opps)==1 else 'ies'} found but none match your filters.")

# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

def _tracked_html(opps: list[dict]) -> str:
    if not opps:
        return """
        <div class="card">
          <div class="card-body" style="text-align:center;padding:48px 24px">
            <div style="font-size:2rem;margin-bottom:12px">📭</div>
            <div style="font-weight:600;color:var(--gray-700);margin-bottom:6px">No active opportunities yet</div>
            <div style="font-size:13px;color:var(--gray-400)">Matching opportunities will appear here once the monitor finds them. Click <b>Check Now</b> on the Dashboard to scan immediately.</div>
          </div>
        </div>"""

    today = datetime.now().date()
    rows = []
    for opp in opps:
        kw_chips = "".join(
            f'<span class="kw-chip" style="font-size:11px;padding:2px 9px">{k}</span>'
            for k in opp.get("matched_keywords", [])
        )
        # Deadline urgency colour
        dl_color = "var(--gray-500)"
        try:
            dl_date = datetime.strptime(opp["deadline"], "%Y-%m-%d").date()
            days_left = (dl_date - today).days
            if days_left <= 3:
                dl_color = "var(--red-600)"
            elif days_left <= 7:
                dl_color = "#d97706"
        except ValueError:
            days_left = None

        days_label = ""
        if days_left is not None:
            days_label = f'<span style="font-size:11px;color:{dl_color};font-weight:600;margin-left:6px">({days_left}d left)</span>'

        writer_url = _writer_url(opp)
        apply_url  = opp.get("url", "#")

        rows.append(f"""
        <tr>
          <td class="td-ref">
            <span class="ref-badge">{opp['ref']}</span>
          </td>
          <td class="td-main">
            <div class="opp-title">{opp['work_area']}</div>
            <div class="opp-meta">{opp['centre']} &nbsp;·&nbsp; {opp['advisor']}</div>
            <div style="margin-top:5px">{kw_chips}</div>
          </td>
          <td class="td-qual">
            <span class="qual-badge">{opp['qualification']}</span>
          </td>
          <td class="td-deadline">
            <span style="font-weight:600;color:{dl_color}">{opp['deadline']}</span>
            {days_label}
          </td>
          <td class="td-actions">
            <a href="{apply_url}" target="_blank" class="action-btn action-btn--green">Apply</a>
            <a href="{writer_url}" target="_blank" class="action-btn action-btn--indigo">✦ Letter</a>
          </td>
        </tr>""")

    rows_html = "\n".join(rows)
    return f"""
    <div class="card" style="overflow:visible">
      <div class="card-header">
        <span class="card-title">Active matched opportunities</span>
        <span style="font-size:12px;color:var(--gray-400)">{len(opps)} active · expired ones hidden</span>
      </div>
      <div style="overflow-x:auto">
        <table class="opp-table">
          <thead>
            <tr>
              <th>Ref</th>
              <th>Opportunity</th>
              <th>Qualification</th>
              <th>Deadline</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </div>"""


def _active_opps() -> list[dict]:
    """Return matched opportunities whose deadline is today or in the future."""
    today = datetime.now().date()
    result = []
    for opp in _state.get("matched_opps", {}).values():
        try:
            dl = datetime.strptime(opp.get("deadline", ""), "%Y-%m-%d").date()
            if dl >= today:
                result.append(opp)
        except ValueError:
            result.append(opp)  # keep if deadline unparseable
    result.sort(key=lambda o: o.get("deadline", ""))
    return result


def _render_html() -> str:
    cfg        = _config
    running    = _monitor is not None
    seen_count = len(_state.get("seen_refs", []))
    kw_chips   = "".join(f'<span class="kw-chip">{kw}</span>' for kw in WORK_AREA_KEYWORDS)
    qual_chips = "".join(f'<span class="kw-chip kw-chip--blue">{kw}</span>' for kw in QUALIFICATION_KEYWORDS)
    log_lines  = "".join(f'<div class="line">{l}</div>' for l in _log_history)
    fetch_chk  = "checked" if cfg.get("fetch_details", True) else ""
    gen_chk    = "checked" if cfg.get("generate_letters", True) else ""
    active_opps = _active_opps()
    active_count = len(active_opps)

    start_btn = ('<form method="POST" action="/start" style="display:contents">'
                 '<button class="btn btn--primary" type="submit">'
                 '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="5,3 19,12 5,21"/></svg>'
                 'Start Monitoring</button></form>') if not running else \
                '<button class="btn btn--disabled" disabled>'\
                '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="5,3 19,12 5,21"/></svg>'\
                'Start Monitoring</button>'

    stop_btn  = ('<form method="POST" action="/stop" style="display:contents">'
                 '<button class="btn btn--danger" type="submit">'
                 '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="3" width="18" height="18" rx="2"/></svg>'
                 'Stop</button></form>') if running else \
                '<button class="btn btn--disabled" disabled>'\
                '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="3" width="18" height="18" rx="2"/></svg>'\
                'Stop</button>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>INESC TEC Monitor</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
  *, *::before, *::after {{box-sizing:border-box;margin:0;padding:0}}

  :root {{
    --green-900: #14532d;
    --green-700: #15803d;
    --green-600: #16a34a;
    --green-500: #22c55e;
    --green-100: #dcfce7;
    --green-50:  #f0fdf4;
    --red-600:   #dc2626;
    --red-100:   #fee2e2;
    --amber-500: #f59e0b;
    --blue-600:  #2563eb;
    --blue-100:  #dbeafe;
    --gray-900:  #111827;
    --gray-700:  #374151;
    --gray-500:  #6b7280;
    --gray-400:  #9ca3af;
    --gray-200:  #e5e7eb;
    --gray-100:  #f3f4f6;
    --gray-50:   #f9fafb;
    --white:     #ffffff;
    --radius-sm: 6px;
    --radius:    10px;
    --radius-lg: 14px;
    --shadow-sm: 0 1px 2px rgba(0,0,0,.05);
    --shadow:    0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.05);
    --shadow-md: 0 4px 6px rgba(0,0,0,.06), 0 2px 4px rgba(0,0,0,.04);
  }}

  body {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--gray-50);
    color: var(--gray-900);
    min-height: 100vh;
    font-size: 14px;
    line-height: 1.5;
  }}

  /* ── TOPBAR ── */
  .topbar {{
    background: linear-gradient(135deg, var(--green-900) 0%, var(--green-700) 100%);
    padding: 0 32px;
    display: flex;
    align-items: stretch;
    gap: 0;
    box-shadow: 0 2px 12px rgba(0,0,0,.18);
    position: sticky;
    top: 0;
    z-index: 100;
  }}
  .topbar-brand {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 18px 0;
    margin-right: 32px;
    text-decoration: none;
  }}
  .topbar-brand svg {{ opacity: .9 }}
  .topbar-brand h1 {{
    font-size: 15px;
    font-weight: 700;
    color: #fff;
    letter-spacing: -.1px;
    white-space: nowrap;
  }}
  .topbar-nav {{
    display: flex;
    align-items: stretch;
    gap: 2px;
    flex: 1;
  }}
  .tab-btn {{
    padding: 0 18px;
    border: none;
    background: transparent;
    color: rgba(255,255,255,.65);
    font-size: 13.5px;
    font-weight: 500;
    cursor: pointer;
    border-bottom: 3px solid transparent;
    transition: color .15s, border-color .15s;
    white-space: nowrap;
    font-family: inherit;
  }}
  .tab-btn:hover {{ color: rgba(255,255,255,.9) }}
  .tab-btn.active {{ color: #fff; border-bottom-color: var(--green-500); font-weight: 600 }}
  .tab-badge {{
    display: inline-flex; align-items: center; justify-content: center;
    background: var(--green-500); color: #fff;
    border-radius: 20px; min-width: 18px; height: 18px;
    font-size: 11px; font-weight: 700; padding: 0 5px;
    margin-left: 6px; vertical-align: middle;
  }}
  .topbar-stat {{
    display: flex;
    align-items: center;
    gap: 6px;
    margin-left: auto;
    padding: 0;
  }}
  .stat-pill {{
    background: rgba(255,255,255,.12);
    border: 1px solid rgba(255,255,255,.18);
    border-radius: 20px;
    padding: 5px 12px;
    font-size: 12px;
    color: rgba(255,255,255,.85);
    font-weight: 500;
  }}

  /* ── LAYOUT ── */
  .tab-panel {{ display: none }}
  .tab-panel.active {{ display: block }}
  .page {{ max-width: 880px; margin: 0 auto; padding: 28px 24px 48px }}

  /* ── CARDS ── */
  .card {{
    background: var(--white);
    border: 1px solid var(--gray-200);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow);
    margin-bottom: 16px;
    overflow: hidden;
  }}
  .card-header {{
    padding: 16px 22px;
    border-bottom: 1px solid var(--gray-100);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }}
  .card-title {{
    font-size: 13px;
    font-weight: 700;
    color: var(--gray-700);
    text-transform: uppercase;
    letter-spacing: .6px;
  }}
  .card-body {{ padding: 20px 22px }}
  .card-body + .card-body {{ padding-top: 0 }}

  /* ── STATUS ── */
  .status-row {{
    display: flex;
    align-items: center;
    gap: 12px;
  }}
  .status-indicator {{
    width: 10px; height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
  }}
  .status-indicator.running {{
    background: var(--green-500);
    box-shadow: 0 0 0 3px rgba(34,197,94,.2);
    animation: pulse 2s infinite;
  }}
  .status-indicator.stopped {{ background: var(--gray-400) }}
  @keyframes pulse {{
    0%,100% {{ box-shadow: 0 0 0 3px rgba(34,197,94,.2) }}
    50%      {{ box-shadow: 0 0 0 6px rgba(34,197,94,.06) }}
  }}
  .status-text {{ font-size: 14px; font-weight: 500; color: var(--gray-700) }}

  /* ── STAT CARDS ── */
  .stat-grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin-bottom: 16px;
  }}
  .stat-card {{
    background: var(--white);
    border: 1px solid var(--gray-200);
    border-radius: var(--radius);
    padding: 16px 18px;
    box-shadow: var(--shadow-sm);
  }}
  .stat-label {{ font-size: 11px; font-weight: 600; color: var(--gray-500); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px }}
  .stat-value {{ font-size: 24px; font-weight: 700; color: var(--gray-900) }}
  .stat-value.green {{ color: var(--green-600) }}

  /* ── BUTTONS ── */
  .btn-group {{ display: flex; gap: 8px; flex-wrap: wrap }}
  .btn {{
    display: inline-flex; align-items: center; gap: 6px;
    padding: 8px 16px;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    font-size: 13.5px; font-weight: 600;
    cursor: pointer; font-family: inherit;
    transition: all .15s;
    text-decoration: none;
    white-space: nowrap;
  }}
  .btn:active {{ transform: scale(.97) }}
  .btn--primary {{ background: var(--green-600); color: #fff; border-color: var(--green-700) }}
  .btn--primary:hover {{ background: var(--green-700) }}
  .btn--danger  {{ background: var(--red-600); color: #fff }}
  .btn--danger:hover  {{ background: #b91c1c }}
  .btn--outline {{
    background: var(--white); color: var(--gray-700);
    border-color: var(--gray-200);
    box-shadow: var(--shadow-sm);
  }}
  .btn--outline:hover {{ background: var(--gray-50); border-color: var(--gray-300) }}
  .btn--subtle {{
    background: var(--gray-100); color: var(--gray-700);
    border-color: var(--gray-200);
  }}
  .btn--subtle:hover {{ background: var(--gray-200) }}
  .btn--disabled {{ background: var(--gray-100); color: var(--gray-400); cursor: not-allowed; border-color: var(--gray-200) }}
  .btn--sm {{ padding: 5px 11px; font-size: 12px }}

  /* ── FORM ── */
  .form-section {{ margin-bottom: 24px }}
  .form-section-title {{
    font-size: 11px; font-weight: 700; color: var(--gray-500);
    text-transform: uppercase; letter-spacing: .6px;
    margin-bottom: 14px; padding-bottom: 8px;
    border-bottom: 1px solid var(--gray-100);
  }}
  .form-row {{
    display: grid;
    grid-template-columns: 180px 1fr;
    align-items: center;
    gap: 12px;
    margin-bottom: 10px;
  }}
  .form-row label {{
    font-size: 13px; font-weight: 500; color: var(--gray-700);
  }}
  .form-row input[type=email],
  .form-row input[type=text],
  .form-row input[type=password],
  .form-row input[type=number] {{
    width: 100%;
    padding: 8px 12px;
    border: 1.5px solid var(--gray-200);
    border-radius: var(--radius-sm);
    font-size: 13.5px;
    font-family: inherit;
    color: var(--gray-900);
    background: var(--white);
    outline: none;
    transition: border-color .15s, box-shadow .15s;
  }}
  .form-row input:focus {{
    border-color: var(--green-600);
    box-shadow: 0 0 0 3px rgba(22,163,74,.12);
  }}
  .form-hint {{
    font-size: 12px; color: var(--gray-400);
    margin-top: 8px; line-height: 1.6;
    grid-column: 2;
  }}
  .interval-wrap {{ display: flex; align-items: center; gap: 8px }}
  .interval-wrap input {{ width: 72px }}
  .interval-unit {{ font-size: 13px; color: var(--gray-500) }}

  /* Toggle switch */
  .toggle-row {{
    display: flex; align-items: flex-start; gap: 12px;
    margin-bottom: 12px; padding: 10px 12px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--gray-200);
    background: var(--gray-50);
  }}
  .toggle-row:last-of-type {{ margin-bottom: 0 }}
  .toggle {{
    position: relative; width: 36px; height: 20px; flex-shrink: 0; margin-top: 1px;
  }}
  .toggle input {{ opacity: 0; width: 0; height: 0; position: absolute }}
  .toggle-track {{
    position: absolute; inset: 0;
    background: var(--gray-300); border-radius: 20px;
    cursor: pointer; transition: background .2s;
  }}
  .toggle-track::after {{
    content: ''; position: absolute;
    width: 14px; height: 14px;
    background: #fff; border-radius: 50%;
    top: 3px; left: 3px;
    transition: transform .2s;
    box-shadow: 0 1px 3px rgba(0,0,0,.2);
  }}
  .toggle input:checked + .toggle-track {{ background: var(--green-600) }}
  .toggle input:checked + .toggle-track::after {{ transform: translateX(16px) }}
  .toggle-label {{ font-size: 13px; color: var(--gray-700); font-weight: 500; line-height: 1.4 }}
  .toggle-desc {{ font-size: 12px; color: var(--gray-400); margin-top: 1px }}

  /* ── FILTERS / CHIPS ── */
  .filter-section {{ margin-bottom: 14px }}
  .filter-label {{
    font-size: 11px; font-weight: 700; color: var(--gray-500);
    text-transform: uppercase; letter-spacing: .5px; margin-bottom: 8px;
  }}
  .kw-chip {{
    display: inline-flex; align-items: center;
    background: var(--green-50); color: var(--green-700);
    border: 1px solid var(--green-100);
    border-radius: 20px; padding: 3px 11px;
    font-size: 12px; font-weight: 500; margin: 3px;
  }}
  .kw-chip--blue {{
    background: var(--blue-100); color: var(--blue-600);
    border-color: #bfdbfe;
  }}

  /* ── LOG ── */
  .log-wrap {{
    background: #0f1117;
    border-radius: var(--radius);
    border: 1px solid #1e2433;
    overflow: hidden;
  }}
  .log-toolbar {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 14px;
    background: #161b27;
    border-bottom: 1px solid #1e2433;
  }}
  .log-toolbar-title {{
    font-size: 11px; font-weight: 600; color: #4b5563;
    text-transform: uppercase; letter-spacing: .5px;
    display: flex; align-items: center; gap: 6px;
  }}
  .log-dot {{ width: 8px; height: 8px; border-radius: 50% }}
  .log-dot.red {{ background: #ff5f57 }}
  .log-dot.yellow {{ background: #ffbd2e }}
  .log-dot.green {{ background: #28ca42 }}
  #log-box {{
    padding: 12px 16px;
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    font-size: 12px; height: 300px; overflow-y: auto;
    line-height: 1.7; color: #8b949e;
  }}
  .line {{ border-bottom: 1px solid rgba(255,255,255,.03); padding: 0 }}
  .line.l-match {{ color: #4ade80 }}
  .line.l-error  {{ color: #f87171 }}
  .line.l-email  {{ color: #60a5fa }}
  .line.l-warn   {{ color: #fbbf24 }}

  /* ── HOW-TO ── */
  .step {{ display: flex; gap: 16px; margin-bottom: 20px }}
  .step-num {{
    width: 28px; height: 28px; border-radius: 50%;
    background: var(--green-600); color: #fff;
    font-size: 12px; font-weight: 700;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0; margin-top: 1px;
  }}
  .step-body h4 {{ font-size: 13px; font-weight: 600; color: var(--gray-800, #1f2937); margin-bottom: 4px }}
  .step-body p, .step-body li {{ font-size: 13px; color: var(--gray-600, #4b5563); line-height: 1.7 }}
  .step-body ol {{ padding-left: 16px }}
  .step-body code {{
    background: var(--gray-100); color: var(--gray-700);
    padding: 2px 7px; border-radius: 4px; font-size: 12px;
    font-family: 'SF Mono','Fira Code',monospace;
  }}
  .divider {{ border: none; border-top: 1px solid var(--gray-100); margin: 20px 0 }}

  .log-legend {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--gray-500) }}
  .legend-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0 }}

  /* ── TRACKED TABLE ── */
  .opp-table {{
    width: 100%; border-collapse: collapse;
    font-size: 13px;
  }}
  .opp-table thead tr {{
    background: var(--gray-50);
    border-bottom: 2px solid var(--gray-200);
  }}
  .opp-table th {{
    padding: 10px 16px; text-align: left;
    font-size: 11px; font-weight: 700; color: var(--gray-500);
    text-transform: uppercase; letter-spacing: .5px;
    white-space: nowrap;
  }}
  .opp-table tbody tr {{
    border-bottom: 1px solid var(--gray-100);
    transition: background .1s;
  }}
  .opp-table tbody tr:last-child {{ border-bottom: none }}
  .opp-table tbody tr:hover {{ background: var(--gray-50) }}
  .opp-table td {{ padding: 14px 16px; vertical-align: top }}
  .td-ref    {{ white-space: nowrap; width: 1% }}
  .td-main   {{ min-width: 240px }}
  .td-qual   {{ white-space: nowrap; width: 1% }}
  .td-deadline {{ white-space: nowrap; width: 1% }}
  .td-actions {{ white-space: nowrap; width: 1% }}
  .ref-badge {{
    display: inline-block;
    background: var(--gray-100); color: var(--gray-600);
    border-radius: 5px; padding: 3px 8px;
    font-size: 11.5px; font-weight: 600; font-family: monospace;
  }}
  .opp-title {{ font-weight: 600; color: var(--gray-900); margin-bottom: 3px }}
  .opp-meta  {{ font-size: 12px; color: var(--gray-400) }}
  .qual-badge {{
    background: var(--indigo-50, #eef2ff); color: var(--indigo-600, #4f46e5);
    border: 1px solid var(--indigo-100, #e0e7ff);
    border-radius: 20px; padding: 3px 10px;
    font-size: 11.5px; font-weight: 500;
  }}
  .action-btn {{
    display: inline-block; padding: 5px 12px;
    border-radius: 5px; font-size: 12px; font-weight: 600;
    text-decoration: none; margin-right: 5px;
    transition: opacity .15s;
  }}
  .action-btn:hover {{ opacity: .85 }}
  .action-btn--green  {{ background: var(--green-100); color: var(--green-600) }}
  .action-btn--indigo {{ background: #eef2ff; color: #4f46e5 }}

  footer {{
    text-align: center; color: var(--gray-400);
    font-size: 12px; padding: 24px;
  }}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-brand">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,.9)" stroke-width="2">
      <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
    </svg>
    <h1>INESC TEC Monitor</h1>
  </div>
  <nav class="topbar-nav">
    <button class="tab-btn active" onclick="showTab('dashboard',this)">Dashboard</button>
    <button class="tab-btn" onclick="showTab('tracked',this)">Tracked {'<span class="tab-badge">' + str(active_count) + '</span>' if active_count else ''}</button>
    <button class="tab-btn" onclick="showTab('settings',this)">Settings</button>
    <button class="tab-btn" onclick="showTab('help',this)">How to use</button>
  </nav>
  <div class="topbar-stat">
    <span class="stat-pill">{seen_count} tracked</span>
  </div>
</div>

<!-- ═══════════════ DASHBOARD ═══════════════ -->
<div id="tab-dashboard" class="tab-panel active">
<div class="page">

  <div class="stat-grid">
    <div class="stat-card">
      <div class="stat-label">Status</div>
      <div style="display:flex;align-items:center;gap:8px;margin-top:4px">
        <span class="status-indicator {'running' if running else 'stopped'}"></span>
        <span style="font-size:13px;font-weight:600;color:{'var(--green-600)' if running else 'var(--gray-500)'}">
          {'Running' if running else 'Stopped'}
        </span>
      </div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Opportunities tracked</div>
      <div class="stat-value green">{seen_count}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Check interval</div>
      <div class="stat-value">{cfg.get('interval', 60)}<span style="font-size:14px;font-weight:500;color:var(--gray-400)"> min</span></div>
    </div>
  </div>

  <div class="card">
    <div class="card-header">
      <span class="card-title">Current status</span>
    </div>
    <div class="card-body">
      <div class="status-row">
        <span class="status-indicator {'running' if running else 'stopped'}"></span>
        <span class="status-text" id="status-text">{_status}</span>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-header"><span class="card-title">Controls</span></div>
    <div class="card-body">
      <div class="btn-group">
        {start_btn}
        {stop_btn}
        <form method="POST" action="/test" style="display:contents">
          <button class="btn btn--outline" type="submit">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
            Test Email
          </button>
        </form>
        <form method="POST" action="/check" style="display:contents">
          <button class="btn btn--subtle" type="submit">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
            Check Now
          </button>
        </form>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-header"><span class="card-title">Active Filters</span></div>
    <div class="card-body">
      <div class="filter-section">
        <div class="filter-label">Qualification</div>
        {qual_chips}
      </div>
      <div class="filter-section" style="margin-bottom:0">
        <div class="filter-label">Work area keywords</div>
        {kw_chips}
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-header">
      <span class="card-title">Activity Log</span>
      <button class="btn btn--outline btn--sm"
              onclick="document.getElementById('log-box').innerHTML=''">
        Clear
      </button>
    </div>
    <div class="log-wrap" style="border-radius:0;border:none;border-top:1px solid var(--gray-100)">
      <div class="log-toolbar">
        <div style="display:flex;gap:6px">
          <span class="log-dot red"></span>
          <span class="log-dot yellow"></span>
          <span class="log-dot green"></span>
        </div>
        <span class="log-toolbar-title">monitor output</span>
        <span></span>
      </div>
      <div id="log-box">{log_lines}</div>
    </div>
  </div>

</div>
</div>

<!-- ═══════════════ SETTINGS ═══════════════ -->
<div id="tab-settings" class="tab-panel">
<div class="page">

  <div class="card">
    <div class="card-header"><span class="card-title">Email &amp; SMTP</span></div>
    <div class="card-body">
      <form method="POST" action="/save">
        <div class="form-section">
          <div class="form-section-title">Delivery</div>
          <div class="form-row">
            <label>Send from</label>
            <input type="email" name="email_from" value="{cfg.get('email_from','')}" placeholder="you@gmail.com" required>
          </div>
          <div class="form-row">
            <label>Send to</label>
            <input type="email" name="email_to" value="{cfg.get('email_to','')}" placeholder="notify@email.com" required>
          </div>
        </div>

        <div class="form-section">
          <div class="form-section-title">SMTP</div>
          <div class="form-row">
            <label>Host</label>
            <input type="text" name="smtp_host" value="{cfg.get('smtp_host','smtp.gmail.com')}">
          </div>
          <div class="form-row">
            <label>Port</label>
            <input type="number" name="smtp_port" value="{cfg.get('smtp_port','587')}" style="width:90px">
          </div>
          <div class="form-row">
            <label>Username</label>
            <input type="text" name="smtp_user" value="{cfg.get('smtp_user','')}">
          </div>
          <div class="form-row">
            <label>App Password</label>
            <input type="password" name="smtp_pass" value="{cfg.get('smtp_pass','')}">
          </div>
          <div class="form-row" style="align-items:start">
            <div></div>
            <p class="form-hint" style="margin-top:0">
              Gmail → myaccount.google.com → Security → App Passwords → create one
            </p>
          </div>
        </div>

        <div class="form-section">
          <div class="form-section-title">Monitoring</div>
          <div class="form-row">
            <label>Check every</label>
            <div class="interval-wrap">
              <input type="number" name="interval" min="10" max="1440" value="{cfg.get('interval', 60)}">
              <span class="interval-unit">minutes</span>
            </div>
          </div>
        </div>

        <div class="form-section">
          <div class="form-section-title">Options</div>
          <div class="toggle-row">
            <label class="toggle">
              <input type="checkbox" name="fetch_details" value="1" {fetch_chk}>
              <span class="toggle-track"></span>
            </label>
            <div>
              <div class="toggle-label">Fetch detail page per match</div>
              <div class="toggle-desc">Adds a short description from the listing to the email</div>
            </div>
          </div>
          <div class="toggle-row" style="margin-top:8px">
            <label class="toggle">
              <input type="checkbox" name="generate_letters" value="1" {gen_chk}>
              <span class="toggle-track"></span>
            </label>
            <div>
              <div class="toggle-label">Generate AI draft letter</div>
              <div class="toggle-desc">Requires Ollama running locally — one letter per matching opportunity</div>
            </div>
          </div>
          <div class="form-row" style="margin-top:12px">
            <label>Ollama model</label>
            <input type="text" name="ollama_model" value="{cfg.get('ollama_model','llama3.1:8b')}" placeholder="llama3.1:8b">
          </div>
        </div>

        <button class="btn btn--primary" type="submit">Save Settings</button>
      </form>
    </div>
  </div>

</div>
</div>

<!-- ═══════════════ HOW TO USE ═══════════════ -->
<div id="tab-help" class="tab-panel">
<div class="page">

  <div class="card">
    <div class="card-header"><span class="card-title">Getting started</span></div>
    <div class="card-body">

      <div class="step">
        <div class="step-num">1</div>
        <div class="step-body">
          <h4>Configure email in Settings</h4>
          <p>Fill in your Gmail address, App Password, and the address you want alerts sent to. Click Save Settings, then Test Email to confirm it works.</p>
        </div>
      </div>

      <div class="step">
        <div class="step-num">2</div>
        <div class="step-body">
          <h4>Start monitoring</h4>
          <p>Click <strong>Start Monitoring</strong> on the Dashboard. The monitor checks the INESC TEC opportunities page on the configured interval and emails you when something new matches your filters.</p>
        </div>
      </div>

      <div class="step">
        <div class="step-num">3</div>
        <div class="step-body">
          <h4>Auto-start on login (Mac)</h4>
          <p>Run once in Terminal — the monitor will start automatically on every login:</p>
          <p style="margin-top:6px"><code>launchctl load ~/Library/LaunchAgents/com.inesctec.monitor.plist</code></p>
        </div>
      </div>

      <div class="step">
        <div class="step-num">4</div>
        <div class="step-body">
          <h4>AI letter generation (optional)</h4>
          <p>Install <strong>Ollama</strong> from ollama.com, then pull a model:</p>
          <p style="margin-top:4px"><code>ollama pull llama3.1:8b</code></p>
          <p style="margin-top:6px">Enable <strong>Generate AI draft letter</strong> in Settings. Each alert email will contain a personalised draft letter and an <em>Open in Letter Writer</em> button.</p>
        </div>
      </div>

      <hr class="divider">

      <div class="card-title" style="margin-bottom:12px">Log colour guide</div>
      <div class="log-legend">
        <span class="legend-item"><span class="legend-dot" style="background:#4ade80"></span> MATCH — new opportunity matched your filters</span>
        <span class="legend-item"><span class="legend-dot" style="background:#60a5fa"></span> Email sent successfully</span>
        <span class="legend-item"><span class="legend-dot" style="background:#f87171"></span> Error (network or SMTP)</span>
        <span class="legend-item"><span class="legend-dot" style="background:#fbbf24"></span> Warning</span>
        <span class="legend-item"><span class="legend-dot" style="background:#8b949e"></span> Info / skip</span>
      </div>

    </div>
  </div>

</div>
</div>

<!-- ═══════════════ TRACKED ═══════════════ -->
<div id="tab-tracked" class="tab-panel">
<div class="page">

  {_tracked_html(active_opps)}

</div>
</div>

<footer>INESC TEC Opportunity Monitor · localhost:{PORT}</footer>

<script>
  function showTab(name, btn) {{
    document.querySelectorAll('.tab-panel').forEach(function(p){{ p.classList.remove('active'); }});
    document.querySelectorAll('.tab-btn').forEach(function(b){{ b.classList.remove('active'); }});
    document.getElementById('tab-' + name).classList.add('active');
    btn.classList.add('active');
    localStorage.setItem('inesctec_tab', name);
  }}

  (function() {{
    var saved = localStorage.getItem('inesctec_tab');
    if (saved) {{
      var btn = document.querySelector('.tab-btn[onclick*="\\'' + saved + '\\'"]');
      if (btn) btn.click();
    }}
  }})();

  // Forward settings fields with action buttons
  (function() {{
    var sf = document.querySelector('form[action="/save"]');
    ['start','stop','test','check'].forEach(function(a) {{
      var f = document.querySelector('form[action="/' + a + '"]');
      if (!f || !sf) return;
      f.addEventListener('submit', function() {{
        sf.querySelectorAll('input').forEach(function(inp) {{
          var h = document.createElement('input');
          h.type='hidden'; h.name=inp.name; h.value=inp.value;
          f.appendChild(h);
        }});
      }});
    }});
  }})();

  // Color-code log lines
  function colorLine(el) {{
    var t = el.textContent;
    if (/MATCH/i.test(t))       el.classList.add('l-match');
    else if (/ERROR/i.test(t))  el.classList.add('l-error');
    else if (/email sent/i.test(t)) el.classList.add('l-email');
    else if (/warn/i.test(t))   el.classList.add('l-warn');
  }}
  document.querySelectorAll('#log-box .line').forEach(colorLine);

  var box = document.getElementById('log-box');
  if (box) box.scrollTop = box.scrollHeight;

  var es = new EventSource('/stream');
  es.onmessage = function(e) {{
    var b = document.getElementById('log-box');
    if (!b) return;
    var d = document.createElement('div');
    d.className = 'line';
    d.textContent = e.data;
    colorLine(d);
    b.appendChild(d);
    b.scrollTop = b.scrollHeight;
  }};

  setInterval(function() {{
    fetch('/status.json').then(function(r){{return r.json();}}).then(function(d){{
      var t = document.getElementById('status-text');
      if (t) t.textContent = d.status;
      var ind = document.querySelectorAll('.status-indicator');
      ind.forEach(function(el) {{
        el.className = 'status-indicator ' + (d.running ? 'running' : 'stopped');
      }});
    }});
  }}, 4000);
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = _render_html().encode()
            self._respond(200, "text/html; charset=utf-8", body)
        elif path == "/stream":
            self._sse()
        elif path == "/status.json":
            body = json.dumps({"status": _status, "running": _monitor is not None}).encode()
            self._respond(200, "application/json", body)
        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length).decode()
        data   = {k: v[0] for k, v in parse_qs(raw).items()}
        path   = urlparse(self.path).path

        global _config, _monitor

        if path == "/save":
            _config.update({
                "email_from":    data.get("email_from", ""),
                "email_to":      data.get("email_to", ""),
                "smtp_host":     data.get("smtp_host", "smtp.gmail.com"),
                "smtp_port":     data.get("smtp_port", "587"),
                "smtp_user":     data.get("smtp_user", ""),
                "smtp_pass":     data.get("smtp_pass", ""),
                "interval":          int(data.get("interval", 60)),
                "fetch_details":     data.get("fetch_details") == "1",
                "generate_letters":  data.get("generate_letters") == "1",
                "ollama_model":      data.get("ollama_model", "llama3.1:8b"),
            })
            save_json(CONFIG_FILE, _config)
            _log("Settings saved.")
            self._redirect("/")

        elif path == "/start":
            if _monitor is None:
                _apply_settings_from_post(data)
                err = _validate_config(_config)
                if err:
                    _log(f"[CONFIG ERROR] {err} — please save settings first.")
                else:
                    _monitor = MonitorThread(_config, int(_config.get("interval", 60)))
                    _monitor.start()
                    _set_status(f"Monitoring — checking every {_config.get('interval',60)} min")
                    _log(f"Monitoring started. Interval: {_config.get('interval',60)} min")
            self._redirect("/")

        elif path == "/stop":
            if _monitor is not None:
                _monitor.stop()
                _monitor = None
                _set_status("Stopped.")
                _log("Monitoring stopped.")
            self._redirect("/")

        elif path == "/test":
            _apply_settings_from_post(data)
            err = _validate_config(_config)
            if err:
                _log(f"[CONFIG ERROR] {err} — please save settings first.")
            else:
                def _do_test():
                    try:
                        _log("Sending test email…")
                        send_email(_config,
                            "[INESC TEC Monitor] Test email",
                            "<h2 style='color:#2e7d32'>✓ It works!</h2>"
                            "<p>INESC TEC Monitor is configured correctly. "
                            "You'll receive alerts here when new matching opportunities appear.</p>")
                        _log(f"  Test email sent to {_config['email_to']}")
                    except Exception as exc:
                        _log(f"  [EMAIL ERROR] {exc}")
                threading.Thread(target=_do_test, daemon=True).start()
            self._redirect("/")

        elif path == "/check":
            _apply_settings_from_post(data)
            err = _validate_config(_config)
            if err:
                _log(f"[CONFIG ERROR] {err} — please save settings first.")
            else:
                def _do_check():
                    _set_status("Running manual check…")
                    try:
                        t = MonitorThread(_config, 99999)
                        t._check()
                    except Exception as exc:
                        _log(f"[ERROR] {exc}")
                    _set_status("Manual check done.")
                threading.Thread(target=_do_check, daemon=True).start()
            self._redirect("/")

        else:
            self._respond(404, "text/plain", b"Not found")

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q: queue.Queue = queue.Queue(maxsize=200)
        with _sse_lock:
            _sse_clients.append(q)
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_clients.remove(q)
                except ValueError:
                    pass

    def _respond(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, url: str):
        self.send_response(303)
        self.send_header("Location", url)
        self.end_headers()

    def log_message(self, *args):
        pass

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _apply_settings_from_post(data: dict):
    if not data.get("email_from"):
        return
    global _config
    _config.update({
        "email_from":    data.get("email_from", ""),
        "email_to":      data.get("email_to", ""),
        "smtp_host":     data.get("smtp_host", "smtp.gmail.com"),
        "smtp_port":     data.get("smtp_port", "587"),
        "smtp_user":     data.get("smtp_user", ""),
        "smtp_pass":     data.get("smtp_pass", ""),
        "interval":         int(data.get("interval", 60)),
        "fetch_details":    data.get("fetch_details") == "1",
        "generate_letters": data.get("generate_letters") == "1",
        "ollama_model":     data.get("ollama_model", "llama3.1:8b"),
    })
    save_json(CONFIG_FILE, _config)


def _validate_config(cfg: dict) -> str | None:
    for key in ("email_from", "email_to", "smtp_host", "smtp_port", "smtp_pass"):
        if not cfg.get(key):
            return f"Missing: {key.replace('_', ' ')}"
    return None

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _config = load_json(CONFIG_FILE, {})
    _state  = load_json(STATE_FILE,  {"seen_refs": []})
    _rotate_log()
    _prune_state()

    server = ThreadedHTTPServer(("127.0.0.1", PORT), Handler)
    url    = f"http://localhost:{PORT}"
    print(f"INESC TEC Monitor running at {url}")
    print("Press Ctrl+C to quit.\n")

    if not _validate_config(_config):
        _monitor = MonitorThread(_config, int(_config.get("interval", 60)))
        _monitor.start()
        _set_status(f"Monitoring — checking every {_config.get('interval', 60)} min")
        print(f"Auto-started monitoring (interval: {_config.get('interval', 60)} min)")

    threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
