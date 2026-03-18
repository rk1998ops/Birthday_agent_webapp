"""
app.py — Birthday Wishes Agent (100% self-contained, Render-ready)
ALL logic is in this one file. Zero custom module imports.
"""

# ═══════════════════════════════════════════════════════════════
#  STANDARD LIBRARY + THIRD-PARTY IMPORTS ONLY
# ═══════════════════════════════════════════════════════════════
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

# ═══════════════════════════════════════════════════════════════
#  CONFIG  (all from environment variables / .env)
# ═══════════════════════════════════════════════════════════════
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_DRIVE_FOLDER_ID  = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
TELEGRAM_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", "")
DATABASE_PATH           = os.getenv("DATABASE_PATH", "/tmp/birthday_agent.db")
MAX_RETRIES             = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY_S           = int(os.getenv("RETRY_DELAY_S", "5"))

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

COL_NAME    = {"name"}
COL_CONTACT = {"contact number", "contact_number", "phone", "mobile"}
COL_DOB     = {"date of birth", "dob", "birthday", "birth date", "date_of_birth"}
COL_CHAT_ID = {"telegram chat id", "telegram_chat_id", "chat id", "chat_id", "tg chat id"}

# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("birthday_app")

# ═══════════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════════
def _db():
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = _db()
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sent_birthdays (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT    NOT NULL,
                contact_number TEXT    NOT NULL,
                birth_month    INTEGER NOT NULL,
                birth_day      INTEGER NOT NULL,
                year_sent      INTEGER NOT NULL,
                sent_at        TEXT    NOT NULL,
                sheet_id       TEXT,
                UNIQUE(name, contact_number, birth_month, birth_day, year_sent)
            );
            CREATE TABLE IF NOT EXISTS run_logs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at         TEXT    NOT NULL,
                messages_sent  INTEGER DEFAULT 0,
                errors         INTEGER DEFAULT 0,
                notes          TEXT
            );
        """)
    conn.close()

def already_sent(name, contact, month, day, year):
    conn = _db()
    cur = conn.execute(
        "SELECT 1 FROM sent_birthdays WHERE name=? AND contact_number=? "
        "AND birth_month=? AND birth_day=? AND year_sent=?",
        (name, contact, month, day, year)
    )
    result = cur.fetchone() is not None
    conn.close()
    return result

def mark_sent(name, contact, month, day, year, sheet_id=""):
    conn = _db()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO sent_birthdays "
            "(name, contact_number, birth_month, birth_day, year_sent, sent_at, sheet_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (name, contact, month, day, year, datetime.utcnow().isoformat(), sheet_id)
        )
    conn.close()

def log_run(sent, errors, notes=""):
    conn = _db()
    with conn:
        conn.execute(
            "INSERT INTO run_logs (run_at, messages_sent, errors, notes) VALUES (?,?,?,?)",
            (datetime.utcnow().isoformat(), sent, errors, notes)
        )
    conn.close()

# ═══════════════════════════════════════════════════════════════
#  GOOGLE SHEETS READER
# ═══════════════════════════════════════════════════════════════
def _build_google_services():
    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
    )
    drive  = build("drive",  "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return drive, sheets

def _col_index(headers, aliases):
    for i, h in enumerate(headers):
        if h.strip().lower() in aliases:
            return i
    return None

def _parse_dob(raw):
    raw = raw.strip()
    for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
                "%d %B %Y", "%B %d, %Y", "%d/%m/%y"]:
        try:
            return date(*time.strptime(raw, fmt)[:3])
        except ValueError:
            continue
    return None

def fetch_all_records():
    """Returns (list_of_records, sheets_count). Each record is a dict."""
    drive, sheets = _build_google_services()

    # List all spreadsheets in folder
    files, page_token = [], None
    while True:
        resp = drive.files().list(
            q=f"'{GOOGLE_DRIVE_FOLDER_ID}' in parents "
              "and mimeType='application/vnd.google-apps.spreadsheet' "
              "and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageSize=100, pageToken=page_token
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    logger.info("Found %d sheet(s) in Drive folder.", len(files))
    records = []

    for f in files:
        try:
            meta = sheets.spreadsheets().get(
                spreadsheetId=f["id"], fields="sheets.properties"
            ).execute()
        except HttpError as e:
            logger.error("Cannot open %s: %s", f["name"], e)
            continue

        for tab in meta.get("sheets", []):
            title = tab["properties"]["title"]
            try:
                resp = sheets.spreadsheets().values().get(
                    spreadsheetId=f["id"], range=title
                ).execute()
            except HttpError as e:
                logger.warning("Cannot read tab %s: %s", title, e)
                continue

            rows = resp.get("values", [])
            if len(rows) < 2:
                continue

            hdrs     = rows[0]
            i_name   = _col_index(hdrs, COL_NAME)
            i_cnt    = _col_index(hdrs, COL_CONTACT)
            i_dob    = _col_index(hdrs, COL_DOB)
            i_chat   = _col_index(hdrs, COL_CHAT_ID)

            if any(x is None for x in [i_name, i_cnt, i_dob]):
                logger.warning("Tab '%s': missing required columns. Headers: %s", title, hdrs)
                continue

            for row in rows[1:]:
                max_i = max(x for x in [i_name, i_cnt, i_dob, i_chat] if x is not None)
                while len(row) <= max_i:
                    row.append("")
                name    = row[i_name].strip()
                contact = row[i_cnt].strip()
                dob_raw = row[i_dob].strip()
                chat_id = row[i_chat].strip() if i_chat is not None else ""
                if not name or not dob_raw:
                    continue
                dob = _parse_dob(dob_raw)
                if dob is None:
                    logger.warning("Cannot parse DOB '%s' for %s", dob_raw, name)
                    continue
                records.append({
                    "name": name, "contact": contact,
                    "dob": dob, "chat_id": chat_id,
                    "sheet_id": f["id"], "sheet_title": title,
                })

    return records, len(files)

def filter_todays(records):
    today = date.today()
    return [r for r in records if r["dob"].month == today.month and r["dob"].day == today.day]

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM SENDER
# ═══════════════════════════════════════════════════════════════
def send_telegram(name: str, chat_id: str) -> bool:
    text = f"🎉 Happy Birthday, <b>{name}</b>! 🎂\nWishing you a wonderful day! 🎊"
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
            if resp.status_code == 200:
                logger.info("✅ Sent to %s (chat %s)", name, chat_id)
                return True
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", RETRY_DELAY_S))
                logger.warning("Rate limited, waiting %ds", wait)
                time.sleep(wait)
                continue
            if 400 <= resp.status_code < 500:
                logger.error("Telegram 4xx %d: %s", resp.status_code, resp.text)
                return False
            time.sleep(RETRY_DELAY_S)
        except requests.RequestException as e:
            logger.warning("Attempt %d failed: %s", attempt, e)
            time.sleep(RETRY_DELAY_S)
    return False

# ═══════════════════════════════════════════════════════════════
#  FLASK APP
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__)
CORS(app)
init_db()

# ── HTML UI ───────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>Birthday Wishes Agent</title>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet"/>
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    :root{
      --bg:#0d0d0f;--card:#1a1a24;--border:rgba(255,255,255,0.07);
      --gold:#f5c842;--gold-dim:#c9a22e;--rose:#ff6b8a;--teal:#40e0c8;
      --text:#f0eee8;--muted:#7a7a8a;
      --ok:#4ade80;--err:#f87171;--warn:#fbbf24;--radius:14px;
    }
    html{scroll-behavior:smooth}
    body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;font-weight:300;min-height:100vh;overflow-x:hidden}
    .bg-orbs{position:fixed;inset:0;pointer-events:none;z-index:0;overflow:hidden}
    .orb{position:absolute;border-radius:50%;filter:blur(90px);opacity:.17;animation:drift 18s ease-in-out infinite alternate}
    .orb-1{width:600px;height:600px;background:#f5c842;top:-200px;left:-150px}
    .orb-2{width:500px;height:500px;background:#ff6b8a;bottom:-150px;right:-100px;animation-delay:-6s}
    .orb-3{width:400px;height:400px;background:#40e0c8;top:40%;left:50%;animation-delay:-12s}
    @keyframes drift{from{transform:translate(0,0) scale(1)}to{transform:translate(40px,30px) scale(1.08)}}
    .wrap{position:relative;z-index:1;max-width:960px;margin:0 auto;padding:48px 24px 96px}
    header{text-align:center;margin-bottom:60px;animation:up .8s ease both}
    .ring{display:inline-flex;align-items:center;justify-content:center;width:88px;height:88px;border-radius:50%;background:linear-gradient(135deg,var(--gold),var(--rose));margin-bottom:24px;box-shadow:0 0 48px rgba(245,200,66,.3);animation:pulse 3s ease-in-out infinite}
    .ring span{font-size:38px}
    @keyframes pulse{0%,100%{box-shadow:0 0 48px rgba(245,200,66,.3)}50%{box-shadow:0 0 80px rgba(245,200,66,.55)}}
    h1{font-family:'Playfair Display',serif;font-size:clamp(2.2rem,5vw,3.6rem);font-weight:900;background:linear-gradient(135deg,var(--gold) 0%,#fff8e0 50%,var(--rose) 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;line-height:1.1}
    .sub{margin-top:12px;color:var(--muted);font-size:1rem;letter-spacing:.04em}
    .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:36px;animation:up .8s .15s ease both}
    .sc{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px 16px;text-align:center;transition:border-color .3s,transform .3s}
    .sc:hover{border-color:rgba(245,200,66,.3);transform:translateY(-2px)}
    .sv{font-family:'Playfair Display',serif;font-size:2rem;font-weight:700;color:var(--gold);line-height:1;margin-bottom:6px}
    .sl{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
    .card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:36px;margin-bottom:28px;animation:up .8s .25s ease both}
    .card-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;flex-wrap:wrap;gap:12px}
    .card-title{font-family:'Playfair Display',serif;font-size:1.35rem;font-weight:700}
    .dbadge{display:inline-flex;align-items:center;gap:6px;background:rgba(245,200,66,.12);border:1px solid rgba(245,200,66,.3);border-radius:100px;padding:6px 14px;font-size:.82rem;color:var(--gold);font-weight:500}
    .tabs{display:flex;gap:8px;margin-bottom:24px;background:rgba(255,255,255,.04);border-radius:12px;padding:5px}
    .tab{flex:1;padding:10px 12px;border:none;border-radius:9px;background:transparent;color:var(--muted);font-family:'DM Sans',sans-serif;font-size:.88rem;font-weight:500;cursor:pointer;transition:all .25s}
    .tab.active{background:var(--gold);color:#0d0d0f}
    .tab:hover:not(.active){color:var(--text)}
    .sbtn{width:100%;padding:20px;border:none;border-radius:var(--radius);background:linear-gradient(135deg,var(--gold),var(--gold-dim));color:#0d0d0f;font-family:'Playfair Display',serif;font-size:1.15rem;font-weight:700;cursor:pointer;position:relative;overflow:hidden;transition:transform .2s,box-shadow .2s;box-shadow:0 8px 32px rgba(245,200,66,.3)}
    .sbtn:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 16px 48px rgba(245,200,66,.45)}
    .sbtn:disabled{opacity:.55;cursor:not-allowed;transform:none}
    .sbtn .bc{display:flex;align-items:center;justify-content:center;gap:10px}
    .sbtn::after{content:'';position:absolute;top:0;left:-100%;width:60%;height:100%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.25),transparent);transform:skewX(-20deg);transition:left .5s}
    .sbtn:hover::after{left:160%}
    .spin{width:20px;height:20px;border:2px solid rgba(0,0,0,.2);border-top-color:#0d0d0f;border-radius:50%;animation:spin .7s linear infinite;display:none}
    .spin.on{display:block}
    @keyframes spin{to{transform:rotate(360deg)}}
    .rp{background:var(--card);border:1px solid var(--border);border-radius:20px;overflow:hidden;margin-bottom:28px;display:none;animation:up .5s ease both}
    .rp.on{display:block}
    .rp-hdr{padding:20px 28px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
    .rp-hdr h3{font-family:'Playfair Display',serif;font-size:1.1rem}
    .rg{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--border)}
    .rc{background:var(--card);padding:20px;text-align:center}
    .rn{font-family:'Playfair Display',serif;font-size:2rem;font-weight:700;line-height:1;margin-bottom:4px}
    .rl{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
    .rl-list{padding:0 28px 24px}
    .ri{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.04);font-size:.88rem}
    .ri:last-child{border-bottom:none}
    .dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
    .d-ok{background:var(--ok);box-shadow:0 0 8px var(--ok)}
    .d-err{background:var(--err);box-shadow:0 0 8px var(--err)}
    .d-sk{background:var(--muted)}
    .ct{background:var(--card);border:1px solid var(--border);border-radius:20px;overflow:hidden;margin-bottom:28px;animation:up .8s .35s ease both}
    .ct-hdr{display:flex;align-items:center;justify-content:space-between;padding:22px 28px;border-bottom:1px solid var(--border);flex-wrap:wrap;gap:12px}
    .ct-title{font-family:'Playfair Display',serif;font-size:1.2rem;font-weight:700}
    .srch{display:flex;align-items:center;gap:8px;background:rgba(255,255,255,.05);border:1px solid var(--border);border-radius:10px;padding:8px 14px;transition:border-color .25s}
    .srch:focus-within{border-color:rgba(245,200,66,.4)}
    .srch input{background:none;border:none;outline:none;color:var(--text);font-family:'DM Sans',sans-serif;font-size:.88rem;width:180px}
    .srch input::placeholder{color:var(--muted)}
    .lbtn{display:flex;align-items:center;gap:8px;padding:10px 18px;border:1px solid var(--border);border-radius:10px;background:transparent;color:var(--text);font-family:'DM Sans',sans-serif;font-size:.88rem;cursor:pointer;transition:all .25s}
    .lbtn:hover{border-color:var(--gold);color:var(--gold)}
    table{width:100%;border-collapse:collapse}
    thead th{padding:12px 20px;text-align:left;font-size:.72rem;font-weight:500;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);border-bottom:1px solid var(--border)}
    tbody tr{border-bottom:1px solid rgba(255,255,255,.04);transition:background .2s}
    tbody tr:hover{background:rgba(255,255,255,.03)}
    tbody tr:last-child{border-bottom:none}
    td{padding:14px 20px;font-size:.9rem}
    .badge{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:100px;font-size:.73rem;font-weight:500}
    .b-bday{background:rgba(245,200,66,.15);color:var(--gold);border:1px solid rgba(245,200,66,.3)}
    .b-sent{background:rgba(74,222,128,.12);color:var(--ok);border:1px solid rgba(74,222,128,.25)}
    .b-notg{background:rgba(248,113,113,.12);color:var(--err);border:1px solid rgba(248,113,113,.25)}
    .b-ok  {background:rgba(64,224,200,.12);color:var(--teal);border:1px solid rgba(64,224,200,.25)}
    .empty{padding:60px 24px;text-align:center;color:var(--muted)}
    .empty .ei{font-size:3rem;margin-bottom:16px}
    #tc{position:fixed;top:24px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:12px;pointer-events:none}
    .toast{display:flex;align-items:flex-start;gap:14px;background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px 20px;min-width:300px;max-width:400px;box-shadow:0 24px 64px rgba(0,0,0,.7);pointer-events:all;position:relative;overflow:hidden;animation:tin .4s cubic-bezier(.34,1.56,.64,1) both}
    .toast.out{animation:tout .3s ease forwards}
    .ti{font-size:1.4rem;flex-shrink:0;margin-top:1px}
    .tb{flex:1}
    .tt{font-weight:500;font-size:.93rem;margin-bottom:4px}
    .tm{font-size:.82rem;color:var(--muted);line-height:1.5}
    .tp{position:absolute;bottom:0;left:0;height:3px;border-radius:0 0 0 14px;animation:prog 4s linear forwards}
    .t-ok .tp{background:var(--ok)}
    .t-err .tp{background:var(--err)}
    .t-inf .tp{background:var(--gold)}
    .tc{background:none;border:none;color:var(--muted);cursor:pointer;font-size:1rem;padding:0;flex-shrink:0;transition:color .2s}
    .tc:hover{color:var(--text)}
    @keyframes tin{from{opacity:0;transform:translateX(60px)}to{opacity:1;transform:translateX(0)}}
    @keyframes tout{to{opacity:0;transform:translateX(60px)}}
    @keyframes prog{from{width:100%}to{width:0%}}
    #cc{position:fixed;inset:0;pointer-events:none;z-index:9998}
    @keyframes up{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
    footer{text-align:center;color:var(--muted);font-size:.8rem;padding-top:24px;border-top:1px solid var(--border)}
    @media(max-width:600px){
      .stats{grid-template-columns:repeat(3,1fr);gap:8px}
      .sv{font-size:1.5rem}
      .card{padding:22px}
      thead{display:none}
      td{display:block;padding:5px 16px}
      tbody tr{display:block;padding:10px 0;border-bottom:1px solid var(--border)}
    }
  </style>
</head>
<body>
<div class="bg-orbs"><div class="orb orb-1"></div><div class="orb orb-2"></div><div class="orb orb-3"></div></div>
<div id="tc"></div>
<canvas id="cc"></canvas>

<div class="wrap">
  <header>
    <div class="ring"><span>🎂</span></div>
    <h1>Birthday Wishes</h1>
    <p class="sub">Automated greetings via Telegram &middot; Powered by Google Sheets</p>
  </header>

  <div class="stats">
    <div class="sc"><div class="sv" id="s-total">—</div><div class="sl">Total Contacts</div></div>
    <div class="sc"><div class="sv" id="s-today" style="color:var(--rose)">—</div><div class="sl">Birthdays Today</div></div>
    <div class="sc"><div class="sv" id="s-sheets" style="color:var(--teal)">—</div><div class="sl">Sheets Scanned</div></div>
  </div>

  <div class="card">
    <div class="card-hdr">
      <h2 class="card-title">Send Birthday Wishes</h2>
      <div class="dbadge">📅 <span id="tdate"></span></div>
    </div>
    <div class="tabs">
      <button class="tab active" onclick="setMode('today',this)">🎂 Today's Birthdays</button>
      <button class="tab" onclick="setMode('all',this)">🌟 All Contacts</button>
    </div>
    <button class="sbtn" id="sbtn" onclick="sendWishes()">
      <span class="bc"><div class="spin" id="sp"></div><span id="sbtxt">🎉 Send Birthday Wishes</span></span>
    </button>
  </div>

  <div class="rp" id="rp">
    <div class="rp-hdr"><span>📊</span><h3>Send Results</h3></div>
    <div class="rg">
      <div class="rc"><div class="rn" id="r-sent" style="color:var(--ok)">0</div><div class="rl">Sent</div></div>
      <div class="rc"><div class="rn" id="r-skip" style="color:var(--warn)">0</div><div class="rl">Skipped</div></div>
      <div class="rc"><div class="rn" id="r-fail" style="color:var(--err)">0</div><div class="rl">Failed</div></div>
    </div>
    <div class="rl-list" id="rlist"></div>
  </div>

  <div class="ct">
    <div class="ct-hdr">
      <h2 class="ct-title">Contact List</h2>
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
        <div class="srch">
          <svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" style="color:var(--muted)"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          <input id="q" type="text" placeholder="Search…" oninput="filter()"/>
        </div>
        <button class="lbtn" id="lbtn" onclick="load()">
          <svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-3.75"/></svg>
          Refresh
        </button>
      </div>
    </div>
    <div id="cbody"><div class="empty"><div class="ei">📋</div><p>Loading contacts…</p></div></div>
  </div>

  <footer>Birthday Agent &middot; Google Sheets &middot; Telegram Bot</footer>
</div>

<script>
let contacts=[], mode='today';

document.addEventListener('DOMContentLoaded',()=>{
  document.getElementById('tdate').textContent=new Date().toLocaleDateString('en-GB',{day:'numeric',month:'long',year:'numeric'});
  load();
});

function setMode(m,btn){
  mode=m;
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('sbtxt').textContent=m==='today'?'🎉 Send Birthday Wishes':'🌟 Send to All Contacts';
}

async function load(){
  const lb=document.getElementById('lbtn');
  lb.disabled=true;
  lb.innerHTML='<div class="spin on" style="width:13px;height:13px;border-color:rgba(255,255,255,.2);border-top-color:var(--gold)"></div> Loading…';
  document.getElementById('cbody').innerHTML='<div class="empty"><div class="ei">⚙️</div><p>Fetching from Google Sheets…</p></div>';
  try{
    const d=await(await fetch('/api/contacts')).json();
    if(!d.success) throw new Error(d.error||'Error');
    contacts=d.contacts;
    document.getElementById('s-total').textContent=d.total;
    document.getElementById('s-today').textContent=d.birthdays_today;
    document.getElementById('s-sheets').textContent=d.sheets_scanned;
    render(contacts);
    toast('inf','📋 Loaded',d.total+' contacts from '+d.sheets_scanned+' sheet(s)');
  }catch(e){
    document.getElementById('cbody').innerHTML='<div class="empty"><div class="ei">⚠️</div><p style="color:var(--err)">'+e.message+'</p></div>';
    toast('err','Load failed',e.message);
  }finally{
    lb.disabled=false;
    lb.innerHTML='<svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-3.75"/></svg> Refresh';
  }
}

function render(data){
  if(!data.length){document.getElementById('cbody').innerHTML='<div class="empty"><div class="ei">🔍</div><p>No contacts found.</p></div>';return;}
  const rows=data.map(c=>{
    let b='';
    if(c.is_birthday_today&&c.already_sent) b='<span class="badge b-sent">✓ Sent</span>';
    else if(c.is_birthday_today) b='<span class="badge b-bday">🎂 Today!</span>';
    else if(!c.has_telegram) b='<span class="badge b-notg">No Telegram</span>';
    else b='<span class="badge b-ok">✓ Ready</span>';
    const dob=new Date(c.dob).toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'});
    return '<tr><td style="font-weight:500">'+x(c.name)+'</td><td style="color:var(--muted)">'+x(c.contact_number)+'</td><td>'+dob+'</td><td>'+(c.telegram_chat_id?'<code style="font-size:.78rem;color:var(--teal)">'+c.telegram_chat_id+'</code>':'<span style="color:var(--muted)">—</span>')+'</td><td>'+b+'</td></tr>';
  }).join('');
  document.getElementById('cbody').innerHTML='<table><thead><tr><th>Name</th><th>Contact</th><th>DOB</th><th>Telegram Chat ID</th><th>Status</th></tr></thead><tbody>'+rows+'</tbody></table>';
}

function filter(){
  const q=document.getElementById('q').value.toLowerCase();
  render(contacts.filter(c=>c.name.toLowerCase().includes(q)||c.contact_number.toLowerCase().includes(q)));
}

async function sendWishes(){
  const btn=document.getElementById('sbtn'),sp=document.getElementById('sp'),txt=document.getElementById('sbtxt');
  btn.disabled=true; sp.classList.add('on'); txt.textContent='Sending…';
  document.getElementById('rp').classList.remove('on');
  try{
    const d=await(await fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})})).json();
    if(!d.success) throw new Error(d.error||'Error');
    document.getElementById('r-sent').textContent=d.sent;
    document.getElementById('r-skip').textContent=d.skipped;
    document.getElementById('r-fail').textContent=d.failed;
    const items=(d.results||[]).map(r=>{
      const cls=r.status==='sent'?'d-ok':r.status==='failed'?'d-err':'d-sk';
      const rsn=r.reason?'<span style="color:var(--muted);font-size:.78rem"> · '+r.reason+'</span>':'';
      return '<div class="ri"><div class="dot '+cls+'"></div><span>'+x(r.name)+'</span><span style="color:var(--muted);font-size:.78rem;text-transform:capitalize">'+r.status+'</span>'+rsn+'</div>';
    }).join('');
    document.getElementById('rlist').innerHTML=items||'<p style="padding:14px 0;color:var(--muted);font-size:.88rem">No results.</p>';
    document.getElementById('rp').classList.add('on');
    if(d.sent>0){toast('ok','🎉 '+d.sent+' wish'+(d.sent>1?'es':'')+' sent!',d.message);confetti();load();}
    else if(d.skipped>0) toast('inf','Nothing new',d.message||'Already sent or no birthdays today.');
    else toast('err','Nothing sent',d.message||'Check config.');
  }catch(e){toast('err','Error',e.message);}
  finally{btn.disabled=false;sp.classList.remove('on');txt.textContent=mode==='today'?'🎉 Send Birthday Wishes':'🌟 Send to All Contacts';}
}

function toast(type,title,msg,dur=4200){
  const icons={ok:'✅',err:'❌',inf:'ℹ️'};
  const t=document.createElement('div');
  t.className='toast t-'+type;
  t.innerHTML='<div class="ti">'+icons[type]+'</div><div class="tb"><div class="tt">'+title+'</div><div class="tm">'+msg+'</div></div><button class="tc" onclick="rmToast(this.parentElement)">✕</button><div class="tp"></div>';
  document.getElementById('tc').appendChild(t);
  setTimeout(()=>rmToast(t),dur);
}
function rmToast(el){if(!el||!el.parentElement)return;el.classList.add('out');setTimeout(()=>el.remove(),300);}

function confetti(){
  const cv=document.getElementById('cc'),ctx=cv.getContext('2d');
  cv.width=window.innerWidth;cv.height=window.innerHeight;
  const cols=['#f5c842','#ff6b8a','#40e0c8','#fff','#ffb347','#c9f0ff'];
  const ps=Array.from({length:130},()=>({x:Math.random()*cv.width,y:Math.random()*-cv.height,r:Math.random()*6+3,d:Math.random()*60+30,c:cols[~~(Math.random()*cols.length)],ta:0,tai:Math.random()*.07+.05,s:Math.random()>.5?'r':'c'}));
  let a=0,fr;
  function draw(){
    ctx.clearRect(0,0,cv.width,cv.height);a+=.01;let done=0;
    ps.forEach(p=>{
      p.ta+=p.tai;p.y+=Math.cos(a+p.d)+2.5;p.x+=Math.sin(a)*.8;
      ctx.fillStyle=p.c;ctx.beginPath();
      if(p.s==='r'){ctx.save();ctx.translate(p.x,p.y);ctx.rotate(p.ta);ctx.fillRect(-p.r,-p.r/2,p.r*2,p.r);ctx.restore();}
      else{ctx.arc(p.x,p.y,p.r,0,2*Math.PI);ctx.fill();}
      if(p.y>cv.height)done++;
    });
    if(done<ps.length)fr=requestAnimationFrame(draw);
    else{ctx.clearRect(0,0,cv.width,cv.height);cancelAnimationFrame(fr);}
  }
  draw();
  setTimeout(()=>{cancelAnimationFrame(fr);ctx.clearRect(0,0,cv.width,cv.height);},4500);
}

function x(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
</script>
</body>
</html>"""

# ── Routes ────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")

@app.route("/api/status")
def status():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

@app.route("/api/contacts")
def get_contacts():
    try:
        records, sheets_count = fetch_all_records()
        today = date.today()
        out = []
        for r in records:
            is_bday = r["dob"].month == today.month and r["dob"].day == today.day
            out.append({
                "name":             r["name"],
                "contact_number":   r["contact"],
                "dob":              r["dob"].isoformat(),
                "telegram_chat_id": r["chat_id"],
                "sheet_title":      r["sheet_title"],
                "is_birthday_today": is_bday,
                "has_telegram":     bool(r["chat_id"]),
                "already_sent":     already_sent(r["name"], r["contact"],
                                        r["dob"].month, r["dob"].day, today.year
                                    ) if is_bday else False,
            })
        return jsonify({
            "success": True, "contacts": out,
            "total": len(out), "sheets_scanned": sheets_count,
            "birthdays_today": sum(1 for c in out if c["is_birthday_today"]),
        })
    except Exception as e:
        logger.exception("get_contacts error")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/send", methods=["POST"])
def send_route():
    body  = request.get_json() or {}
    mode  = body.get("mode", "today")
    today = date.today()
    results = []
    try:
        records, _ = fetch_all_records()
        targets = filter_todays(records) if mode == "today" else records
        if not targets:
            return jsonify({"success": True, "results": [], "message": "No contacts matched.", "sent": 0, "failed": 0, "skipped": 0})

        sent = failed = skipped = 0
        for r in targets:
            if not r["chat_id"]:
                results.append({"name": r["name"], "status": "skipped", "reason": "No Telegram Chat ID"})
                skipped += 1
                continue
            if mode == "today" and already_sent(r["name"], r["contact"], r["dob"].month, r["dob"].day, today.year):
                results.append({"name": r["name"], "status": "skipped", "reason": "Already sent today"})
                skipped += 1
                continue
            ok = send_telegram(r["name"], r["chat_id"])
            if ok:
                if mode == "today":
                    mark_sent(r["name"], r["contact"], r["dob"].month, r["dob"].day, today.year, r["sheet_id"])
                results.append({"name": r["name"], "status": "sent"})
                sent += 1
            else:
                results.append({"name": r["name"], "status": "failed", "reason": "Telegram API error"})
                failed += 1

        log_run(sent, failed, notes=f"web/{mode}")
        return jsonify({"success": True, "results": results, "sent": sent, "failed": failed, "skipped": skipped,
                        "message": f"Done! {sent} message(s) sent successfully."})
    except Exception as e:
        logger.exception("send_route error")
        return jsonify({"success": False, "error": str(e)}), 500

# ── Run ───────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
