#!/usr/bin/env python3
"""
Full household_bot.py — webhook-ready, includes admin commands, approval system with expiry.
Replace your current file with this one.
"""

import os
import re
import csv
import ssl
import imaplib
import poplib
import email
import sqlite3
from email.message import Message
from email.utils import parseaddr
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from datetime import datetime

from dotenv import load_dotenv
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from flask import Flask, request

# ====== CONFIG ======
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_FILE = os.getenv("DB_FILE", "accounts.db")
CSV_BOOTSTRAP = os.getenv("CSV_BOOTSTRAP", "accounts.csv")
MAX_EMAILS_CHECK = int(os.getenv("MAX_EMAILS_CHECK", "20"))

if not BOT_TOKEN:
    raise SystemExit("Please set BOT_TOKEN in .env")
if not ADMIN_ID:
    raise SystemExit("Please set ADMIN_ID in .env (your numeric Telegram ID)")

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
    protocol: str  # "imap" or "pop3"
    server: str
    port: int

# ====== DATABASE HELPERS ======
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
            expiry TEXT  -- YYYY-MM-DD format
        )
    """)
    con.commit()
    con.close()

def upsert_account(acc: Account):
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

def delete_account(email_addr: str):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DELETE FROM accounts WHERE email=?", (email_addr,))
    con.commit()
    con.close()

def get_account(email_addr: str) -> Optional[Account]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT email,password,protocol,server,port FROM accounts WHERE email=?", (email_addr,))
    row = cur.fetchone()
    con.close()
    if row:
        return Account(row[0], row[1], row[2], row[3], int(row[4]))
    return None

def list_accounts() -> List[Tuple[str,str,int]]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT email, server, port FROM accounts ORDER BY email")
    rows = cur.fetchall()
    con.close()
    return rows

def bootstrap_from_csv(path: str):
    if not os.path.exists(path):
        return 0
    added = 0
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            email_addr = (row.get("email") or "").strip()
            password = (row.get("password") or "").strip()
            protocol = (row.get("protocol") or "pop3").strip().lower() or "pop3"
            server = (row.get("server") or "").strip()
            try:
                port = int(row.get("port") or 0)
            except:
                port = 0
            if email_addr and password and server and port:
                try:
                    upsert_account(Account(email_addr, password, protocol, server, port))
                    added += 1
                except Exception:
                    pass
    return added

# ====== APPROVAL HELPERS ======
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def is_approved(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT expiry FROM approved_users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    if not row:
        return False
    expiry = row[0]
    if expiry:
        today = datetime.utcnow().date()
        try:
            expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
            if today > expiry_date:
                return False
        except Exception:
            pass
    return True

def approve_user(user_id: int, expiry: Optional[str] = None):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO approved_users(user_id, expiry) VALUES(?,?)", (user_id, expiry))
    con.commit()
    con.close()

def unapprove_user(user_id: int):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DELETE FROM approved_users WHERE user_id=?", (user_id,))
    con.commit()
    con.close()

def list_approved() -> List[Tuple[int, Optional[str]]]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT user_id, expiry FROM approved_users")
    rows = cur.fetchall()
    con.close()
    return rows

# ====== EMAIL PARSING ======
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

# ====== FETCH via POP3/IMAP ======
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

# ====== CONVERSATION STATE ======
user_state: Dict[int, str] = {}   # user_id -> "awaiting_yes" | "awaiting_email"

def greet_text() -> str:
    return ("Hi, Household Bot this side\n"
            "Looks like you have faced an household issue on your OTT platform (Enter Yes/yes to get the link or Exit/exit to exit)")

# ====== BOT HANDLERS (admin + approve + user) ======
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

# Admin-only helpers
def admin_only(message) -> bool:
    return message.from_user.id == ADMIN_ID

# Admin account management
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
        upsert_account(Account(email_addr, password, protocol, server, int(port)))
        bot.reply_to(message, f"✅ Saved {email_addr} ({protocol} {server}:{port})")
    except Exception:
        bot.reply_to(message, "Usage:\n/add <email> <password> <imap|pop3> <server> <port>")

@bot.message_handler(commands=['del'])
def cmd_del(message):
    if not admin_only(message):
        return
    try:
        _, email_addr = message.text.split()
        delete_account(email_addr)
        bot.reply_to(message, f"🗑️ Deleted {email_addr}")
    except Exception:
        bot.reply_to(message, "Usage:\n/del <email>")

@bot.message_handler(commands=['list'])
def cmd_list(message):
    if not admin_only(message):
        return
    rows = list_accounts()
    if not rows:
        bot.reply_to(message, "📭 Database is empty.")
    else:
        pretty = "\n".join([f"• {e} — {s}:{p}" for e,s,p in rows])
        bot.reply_to(message, "📋 Accounts:\n" + pretty)

@bot.message_handler(commands=['importcsv'])
def cmd_importcsv(message):
    if not admin_only(message):
        return
    added = bootstrap_from_csv(CSV_BOOTSTRAP)
    bot.reply_to(message, f"📥 Imported {added} account(s) from {CSV_BOOTSTRAP}")

# Approval admin commands
@bot.message_handler(commands=['approve'])
def cmd_approve(message):
    if not admin_only(message):
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            raise ValueError
        uid = int(parts[1])
        expiry = parts[2] if len(parts) > 2 else None
        if expiry:
            datetime.strptime(expiry, "%Y-%m-%d")
        approve_user(uid, expiry)
        bot.reply_to(message, f"✅ Approved user {uid}" + (f" until {expiry}" if expiry else ""))
    except Exception:
        bot.reply_to(message, "Usage: /approve <telegram_id> [YYYY-MM-DD]")

@bot.message_handler(commands=['unapprove'])
def cmd_unapprove(message):
    if not admin_only(message):
        return
    try:
        _, uid_str = message.text.split()
        uid = int(uid_str)
        unapprove_user(uid)
        bot.reply_to(message, f"🗑️ Unapproved user {uid}")
    except Exception:
        bot.reply_to(message, "Usage: /unapprove <telegram_id>")

@bot.message_handler(commands=['approved'])
def cmd_list_approved(message):
    if not admin_only(message):
        return
    rows = list_approved()
    if not rows:
        bot.reply_to(message, "No approved users yet.")
    else:
        pretty = "\n".join([f"• {uid} — expires: {expiry or 'Never'}" for uid,expiry in rows])
        bot.reply_to(message, "✅ Approved users:\n" + pretty)

# Text router for Yes/email flow
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
        acc = get_account(email_addr)
        if not acc:
            bot.reply_to(message, "❌ This mail ID is not in the database. Please contact admin.")
            user_state.pop(uid, None)
            return
        bot.send_chat_action(message.chat.id, "typing")
        try:
            results = fetch_household_info(acc)
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
    render_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("RENDER_APP_URL") or os.getenv("RENDER_INTERNAL_URL")
    if not render_url:
        render_url
