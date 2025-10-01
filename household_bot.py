#!/usr/bin/env python3
"""
household_bot.py

Features:
- Webhook (Flask) for Render.
- Admin commands: /add /del /list /importcsv
- Approval system with expiry days: /approve <telegram_id> [days] (default 30 days)
  and /unapprove, /approved
- Auto-sync DB <-> CSV: accounts.csv and approved.csv (so data persists across redeploys)
- Fetches only household links from OTT emails (Netflix patterns by default)
- Use .env for BOT_TOKEN, ADMIN_ID, DB_FILE, CSV filenames, MAX_EMAILS_CHECK
"""

import os
import re
import csv
import sqlite3
import imaplib
import poplib
import email
from email.message import Message
from email.utils import parseaddr
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timedelta

from dotenv import load_dotenv
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from flask import Flask, request

# ====== CONFIG (env) ======
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_FILE = os.getenv("DB_FILE", "accounts.db")
ACCOUNTS_CSV = os.getenv("ACCOUNTS_CSV", "accounts.csv")
APPROVED_CSV = os.getenv("APPROVED_CSV", "approved.csv")
MAX_EMAILS_CHECK = int(os.getenv("MAX_EMAILS_CHECK", "20"))

if not BOT_TOKEN:
    raise SystemExit("Please set BOT_TOKEN in .env")
if not ADMIN_ID:
    raise SystemExit("Please set ADMIN_ID in .env")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ====== OTT patterns (link-only) ======
OTT_SENDERS = [
    "info@account.netflix.com",
    "no-reply@account.netflix.com",
]
NETFLIX_LINK_PATTERNS = [
    re.compile(r"https://www\.netflix\.com/account/update-primary-location\?nftoken=[^\s\"'<>]+", re.I),
    re.compile(r"https://www\.netflix\.com/account/travel/verify\?nftoken=[^\s\"'<>]+", re.I),
]

# ====== DATA MODEL ======
@dataclass
class Account:
    email: str
    password: str
    protocol: str  # imap / pop3
    server: str
    port: int

# ====== DB helpers ======
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            email TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            protocol TEXT NOT NULL CHECK (protocol IN ('imap','pop3')),
            server TEXT NOT NULL,
            port INTEGER NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS approved_users (
            user_id INTEGER PRIMARY KEY,
            expiry_date TEXT   -- ISO date string yyyy-mm-dd
        )
    """)
    con.commit()
    con.close()

# ====== CSV <-> DB sync helpers ======
def export_accounts_to_csv():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT email,password,protocol,server,port FROM accounts ORDER BY email")
    rows = cur.fetchall()
    con.close()
    if rows:
        with open(ACCOUNTS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["email","password","protocol","server","port"])
            writer.writerows(rows)
    else:
        # write header if empty
        with open(ACCOUNTS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["email","password","protocol","server","port"])

def export_approved_to_csv():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT user_id, expiry_date FROM approved_users ORDER BY user_id")
    rows = cur.fetchall()
    con.close()
    with open(APPROVED_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["user_id","expiry_date"])
        writer.writerows(rows)

def import_accounts_from_csv():
    if not os.path.exists(ACCOUNTS_CSV):
        return 0
    added = 0
    with open(ACCOUNTS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email_addr = (row.get("email") or "").strip()
            if not email_addr:
                continue
            password = (row.get("password") or "").strip()
            protocol = (row.get("protocol") or "pop3").strip().lower() or "pop3"
            server = (row.get("server") or "").strip()
            try:
                port = int(row.get("port") or 0)
            except:
                port = 0
            if email_addr and password and server and port:
                upsert_account_db(Account(email_addr, password, protocol, server, port))
                added += 1
    return added

def import_approved_from_csv():
    if not os.path.exists(APPROVED_CSV):
        return 0
    added = 0
    with open(APPROVED_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            uid_s = (row.get("user_id") or "").strip()
            expiry = (row.get("expiry_date") or "").strip()
            if not uid_s:
                continue
            try:
                uid = int(uid_s)
            except:
                continue
            if expiry:
                # validate date
                try:
                    datetime.strptime(expiry, "%Y-%m-%d")
                except:
                    expiry = ""
            upsert_approved_db(uid, expiry or None)
            added += 1
    return added

# ====== DB CRUD wrappers ======
def upsert_account_db(acc: Account):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""INSERT INTO accounts(email,password,protocol,server,port)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(email) DO UPDATE SET
                     password=excluded.password,
                     protocol=excluded.protocol,
                     server=excluded.server,
                     port=excluded.port
                """, (acc.email, acc.password, acc.protocol, acc.server, acc.port))
    con.commit()
    con.close()
    export_accounts_to_csv()

def delete_account_db(email_addr: str):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DELETE FROM accounts WHERE email=?", (email_addr,))
    con.commit()
    con.close()
    export_accounts_to_csv()

def get_account_db(email_addr: str) -> Optional[Account]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT email,password,protocol,server,port FROM accounts WHERE email=?", (email_addr,))
    row = cur.fetchone()
    con.close()
    if row:
        return Account(row[0], row[1], row[2], row[3], int(row[4]))
    return None

def list_accounts_db() -> List[Tuple[str,str,int]]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT email, server, port FROM accounts ORDER BY email")
    rows = cur.fetchall()
    con.close()
    return rows

# ====== Approved users DB wrappers (expiry support) ======
def upsert_approved_db(uid: int, expiry_iso: Optional[str]):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""INSERT INTO approved_users(user_id, expiry_date)
                   VALUES(?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET expiry_date=excluded.expiry_date""",
                (uid, expiry_iso))
    con.commit()
    con.close()
    export_approved_to_csv()

def delete_approved_db(uid: int):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DELETE FROM approved_users WHERE user_id=?", (uid,))
    con.commit()
    con.close()
    export_approved_to_csv()

def get_approved_db(uid: int) -> Optional[str]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT expiry_date FROM approved_users WHERE user_id=?", (uid,))
    row = cur.fetchone()
    con.close()
    if row:
        return row[0]  # may be None
    return None

def list_approved_db() -> List[Tuple[int, Optional[str]]]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT user_id, expiry_date FROM approved_users ORDER BY user_id")
    rows = cur.fetchall()
    con.close()
    return rows

# ====== Approval check (considers expiry) ======
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def is_approved(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    expiry = get_approved_db(user_id)
    if not expiry:
        return False
    try:
        exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
    except:
        return False
    if exp_date >= datetime.utcnow().date():
        return True
    # expired -> auto-remove
    delete_approved_db(user_id)
    return False

# ====== Email parsing helpers ======
def normalize_link(u: str) -> str:
    u = u.strip()
    if "<" in u:
        u = u.split("<",1)[0]
    return u.rstrip(').,;\'"')

def extract_links_from_text(text: str) -> List[str]:
    links: List[str] = []
    for pat in NETFLIX_LINK_PATTERNS:
        links.extend(pat.findall(text))
    seen = set()
    clean_links = []
    for u in links:
        nu = normalize_link(u)
        if nu and nu not in seen:
            seen.add(nu)
            clean_links.append(nu)
    return clean_links

def message_from_bytes_safe(raw: bytes) -> Message:
    return email.message_from_bytes(raw)

def get_text_from_message(msg: Message) -> str:
    if msg.is_multipart():
        parts = []
        for p in msg.walk():
            ctype = p.get_content_type()
            if ctype in ("text/plain","text/html"):
                try:
                    parts.append(p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8","ignore"))
                except Exception:
                    try:
                        parts.append(p.get_payload(decode=True).decode("utf-8","ignore"))
                    except Exception:
                        pass
        return "\n".join(parts)
    else:
        try:
            return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8","ignore")
        except Exception:
            try:
                return msg.get_payload(decode=True).decode("utf-8","ignore")
            except Exception:
                return ""

# ====== Fetch via POP3 / IMAP (search OTT senders and extract links) ======
def fetch_via_pop3(server: str, port: int, email_addr: str, password: str) -> List[List[str]]:
    out: List[List[str]] = []
    try:
        conn = poplib.POP3_SSL(server, port, timeout=30)
        conn.user(email_addr)
        conn.pass_(password)
        num_messages = len(conn.list()[1])
        if num_messages == 0:
            conn.quit()
            return out
        start = max(1, num_messages - MAX_EMAILS_CHECK + 1)
        for i in range(start, num_messages + 1):
            try:
                raw_msg = b"\n".join(conn.retr(i)[1])
                msg = message_from_bytes_safe(raw_msg)
                frm = parseaddr(msg.get("From",""))[1].lower()
                if frm not in [s.lower() for s in OTT_SENDERS]:
                    continue
                text = get_text_from_message(msg)
                links = extract_links_from_text(text)
                if links:
                    out.append(links)
            except Exception:
                continue
        conn.quit()
    except Exception as e:
        raise e
    return out

def fetch_via_imap(server: str, port: int, email_addr: str, password: str) -> List[List[str]]:
    out: List[List[str]] = []
    try:
        m = imaplib.IMAP4_SSL(server, port)
        m.login(email_addr, password)
        m.select("INBOX", readonly=True)
        ids = set()
        for s in OTT_SENDERS:
            try:
                typ, data = m.search(None, f'(FROM "{s}")')
                if typ == "OK" and data and data[0]:
                    for i in data[0].split():
                        ids.add(i)
            except Exception:
                continue
        if not ids:
            m.logout()
            return out
        id_list = sorted(list(ids), key=lambda x: int(x), reverse=True)[:MAX_EMAILS_CHECK]
        for i in id_list:
            try:
                typ, msg_data = m.fetch(i, "(RFC822)")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                msg = message_from_bytes_safe(msg_data[0][1])
                text = get_text_from_message(msg)
                links = extract_links_from_text(text)
                if links:
                    out.append(links)
            except Exception:
                continue
        m.logout()
    except Exception as e:
        raise e
    return out

def fetch_household_info(acc: Account) -> List[List[str]]:
    if acc.protocol == "imap":
        return fetch_via_imap(acc.server, acc.port, acc.email, acc.password)
    else:
        return fetch_via_pop3(acc.server, acc.port, acc.email, acc.password)

# ====== Conversation state ======
user_state: Dict[int, str] = {}   # user_id -> "awaiting_yes" | "awaiting_email"

def greet_text() -> str:
    return ("Hi, Household Bot this side\n"
            "Looks like you have faced an household issue on your OTT platform (Enter Yes/yes to get the link or Exit/exit to exit)")

# ====== Bot Handlers ======
@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    uid = message.from_user.id
    if not is_approved(uid):
        bot.reply_to(message, "❌ You are not approved to use this bot.\nPlease contact the admin.")
        return
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row(KeyboardButton("Yes"), KeyboardButton("Exit"))
    bot.reply_to(message, greet_text(), reply_markup=kb)
    user_state[uid] = "awaiting_yes"

def admin_only(message) -> bool:
    return message.from_user.id == ADMIN_ID

# ====== Admin account management ======
@bot.message_handler(commands=['add'])
def cmd_add(message):
    if not admin_only(message):
        return
    try:
        parts = message.text.split()
        if len(parts) != 6:
            raise ValueError
        _, email_addr, password, protocol, server, port = parts
        protocol = protocol.lower()
        if protocol not in ("imap","pop3"):
            raise ValueError("protocol must be imap or pop3")
        upsert_account_db(Account(email_addr, password, protocol, server, int(port)))
        bot.reply_to(message, f"✅ Saved {email_addr} ({protocol} {server}:{port})")
    except Exception:
        bot.reply_to(message, "Usage:\n/add <email> <password> <imap|pop3> <server> <port>")

@bot.message_handler(commands=['del'])
def cmd_del(message):
    if not admin_only(message):
        return
    try:
        _, email_addr = message.text.split()
        delete_account_db(email_addr)
        bot.reply_to(message, f"🗑️ Deleted {email_addr}")
    except Exception:
        bot.reply_to(message, "Usage:\n/del <email>")

@bot.message_handler(commands=['list'])
def cmd_list(message):
    if not admin_only(message):
        return
    rows = list_accounts_db()
    if not rows:
        bot.reply_to(message, "📭 Database is empty.")
    else:
        pretty = "\n".join([f"• {e} — {s}:{p}" for e,s,p in rows])
        bot.reply_to(message, "📋 Accounts:\n" + pretty)

@bot.message_handler(commands=['importcsv'])
def cmd_importcsv(message):
    if not admin_only(message):
        return
    added = import_accounts_from_csv()
    bot.reply_to(message, f"📥 Imported {added} account(s) from {ACCOUNTS_CSV}")

# ====== Approval commands (with expiry days) ======
@bot.message_handler(commands=['approve'])
def cmd_approve(message):
    if not admin_only(message):
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            raise ValueError
        uid = int(parts[1])
        days = int(parts[2]) if len(parts) >= 3 else 30  # default 30 days
        expiry = (datetime.utcnow().date() + timedelta(days=days)).isoformat()
        upsert_approved_db(uid, expiry)
        bot.reply_to(message, f"✅ Approved {uid} for {days} days (expires {expiry})")
    except Exception:
        bot.reply_to(message, "Usage: /approve <telegram_id> [days]")

@bot.message_handler(commands=['unapprove'])
def cmd_unapprove(message):
    if not admin_only(message):
        return
    try:
        _, uid_str = message.text.split()
        uid = int(uid_str)
        delete_approved_db(uid)
        bot.reply_to(message, f"🗑️ Unapproved user {uid}")
    except Exception:
        bot.reply_to(message, "Usage: /unapprove <telegram_id>")

@bot.message_handler(commands=['approved'])
def cmd_list_approved(message):
    if not admin_only(message):
        return
    rows = list_approved_db()
    if not rows:
        bot.reply_to(message, "No approved users yet.")
    else:
        lines = []
        for uid, expiry in rows:
            if expiry:
                lines.append(f"• {uid} — expires {expiry}")
            else:
                lines.append(f"• {uid} — no expiry")
        bot.reply_to(message, "✅ Approved users:\n" + "\n".join(lines))

# ====== Text router for Yes/email flow ======
@bot.message_handler(func=lambda m: True, content_types=['text'])
def text_router(message):
    uid = message.from_user.id
    txt = (message.text or "").strip()

    if not is_approved(uid):
        bot.reply_to(message, "❌ You are not approved to use this bot.\nPlease contact the admin.")
        return

    if user_state.get(uid) == "awaiting_yes":
        if txt.lower() == "yes":
            bot.reply_to(message, "Enter the mail ID", reply_markup=ReplyKeyboardRemove())
            user_state[uid] = "awaiting_email"
        else:
            bot.reply_to(message, "Okay. Type /start anytime to try again.", reply_markup=ReplyKeyboardRemove())
            user_state.pop(uid, None)
        return

    if user_state.get(uid) == "awaiting_email":
        email_addr = txt
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_addr):
            bot.reply_to(message, "That doesn't look like a valid email. Please send a correct mail ID.")
            return
        acc = get_account_db(email_addr)
        if not acc:
            bot.reply_to(message, "❌ This mail ID is not in the database. Please contact admin.")
            user_state.pop(uid, None)
            return
        # acc is tuple(email,password,protocol,server,port)
        account = Account(acc[0], acc[1], acc[2], acc[3], int(acc[4]))
        bot.send_chat_action(message.chat.id, "typing")
        try:
            results = fetch_household_info(account)
        except Exception as e:
            bot.reply_to(message, f"⚠️ Couldn't read mailbox: {e}")
            user_state.pop(uid, None)
            return
        if not results:
            bot.reply_to(message, "❌ No household emails found recently. Try again later.")
            user_state.pop(uid, None)
            return
        reply_lines = [f"📬 Results for <b>{email_addr}</b>"]
        for links in results:
            for ln in links:
                reply_lines.append(f"🔗 <code>{ln}</code>")
            reply_lines.append("— — — — —")
        bot.reply_to(message, "\n".join(reply_lines))
        user_state.pop(uid, None)
        return

    # fallback / restart
    if txt.lower() in ("yes","start"):
        kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.row(KeyboardButton("Yes"), KeyboardButton("Exit"))
        bot.reply_to(message, greet_text(), reply_markup=kb)
        user_state[uid] = "awaiting_yes"
        return
    if txt.lower() in ("exit","cancel"):
        bot.reply_to(message, "Exited. Type /start whenever you need me.")
        user_state.pop(uid, None)
        return

# ====== WEBHOOK (Flask) ======
@app.route("/" + BOT_TOKEN, methods=['POST'])
def webhook_receive():
    json_str = request.stream.read().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/")
def webhook_set():
    # prefer Render-provided external URL env var if present
    render_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("RENDER_APP_URL") or "https://household-bot.onrender.com"
    webhook_url = render_url.rstrip("/") + "/" + BOT_TOKEN
    try:
        bot.remove_webhook()
        bot.set_webhook(url=webhook_url)
        return "Webhook set", 200
    except Exception as e:
        return f"Webhook error: {e}", 500

# ====== MAIN ======
if __name__ == "__main__":
    init_db()
    # import CSVs into DB (bootstrap)
    import_accounts_from_csv()
    import_approved_from_csv()
    # ensure CSVs reflect DB state right after import
    export_accounts_to_csv()
    export_approved_to_csv()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
