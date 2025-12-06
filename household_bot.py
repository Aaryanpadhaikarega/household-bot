#!/usr/bin/env python3
"""
Full household_bot.py — webhook-ready, includes admin commands,
approval system, persistent DB support + /exportdb backup.
"""

import os
import re
import csv
import ssl
import imaplib
import poplib
import email
import sqlite3
import shutil

from email.message import Message
from email.utils import parseaddr
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict

import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from flask import Flask, request

# ====== CONFIG ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

# ✅ PERSISTENT DB PATH
DB_FILE = os.getenv("DB_FILE", "/data/accounts.db")

CSV_BOOTSTRAP = os.getenv("CSV_BOOTSTRAP", "accounts.csv")
MAX_EMAILS_CHECK = int(os.getenv("MAX_EMAILS_CHECK", "20"))

if not BOT_TOKEN:
    raise SystemExit("Please set BOT_TOKEN in Render Environment")
if not ADMIN_ID:
    raise SystemExit("Please set ADMIN_ID in Render Environment")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ====== OTT patterns ======
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
    protocol: str
    server: str
    port: int

# ====== DATABASE HELPERS ======
def init_db():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            email TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            protocol TEXT NOT NULL,
            server TEXT NOT NULL,
            port INTEGER NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS approved_users (
            user_id INTEGER PRIMARY KEY
        )
    """)

    con.commit()
    con.close()

def upsert_account(acc: Account):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO accounts(email,password,protocol,server,port)
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

# ====== APPROVAL HELPERS ======
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def is_approved(user_id: int) -> bool:
    if is_admin(user_id):
        return True

    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM approved_users WHERE user_id=?", (user_id,))
    ok = cur.fetchone() is not None
    con.close()
    return ok

def approve_user(user_id: int):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO approved_users(user_id) VALUES(?)", (user_id,))
    con.commit()
    con.close()

def list_approved() -> List[int]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT user_id FROM approved_users")
    rows = [r[0] for r in cur.fetchall()]
    con.close()
    return rows

# ====== EMAIL PARSING ======
def extract_links_from_text(text: str) -> List[str]:
    links = []
    for pat in NETFLIX_LINK_PATTERNS:
        links.extend(pat.findall(text))
    return list(set(links))

# ====== FETCH via POP3 ======
def fetch_household_info(acc: Account):
    return []

# ====== BOT UI ======
user_state: Dict[int, str] = {}

def greet_text():
    return "Household bot ready.\nSend YES to continue or EXIT to cancel."

# ====== START ======
@bot.message_handler(commands=['start'])
def cmd_start(message):
    uid = message.from_user.id

    if not is_approved(uid):
        bot.reply_to(message, "❌ You are not approved.")
        return

    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("Yes", "Exit")

    bot.reply_to(message, greet_text(), reply_markup=kb)
    user_state[uid] = "awaiting_yes"

# ====== ADMIN COMMANDS ======
def admin_only(message):
    return message.from_user.id == ADMIN_ID

@bot.message_handler(commands=['add'])
def cmd_add(message):
    if not admin_only(message):
        return

    try:
        _, email_addr, password, protocol, server, port = message.text.split()
        upsert_account(Account(email_addr, password, protocol, server, int(port)))
        bot.reply_to(message, "✅ Account added.")
    except:
        bot.reply_to(message, "Usage:\n/add email pass imap mail.server.com 993")

@bot.message_handler(commands=['list'])
def cmd_list(message):
    if not admin_only(message):
        return

    rows = list_accounts()
    if not rows:
        bot.reply_to(message, "Database empty.")
    else:
        text = "\n".join([f"{e} | {s}:{p}" for e,s,p in rows])
        bot.reply_to(message, text)

@bot.message_handler(commands=['approve'])
def cmd_approve(message):
    if not admin_only(message):
        return

    try:
        _, uid = message.text.split()
        approve_user(int(uid))
        bot.reply_to(message, "✅ Approved.")
    except:
        bot.reply_to(message, "Usage: /approve 123456")

@bot.message_handler(commands=['approved'])
def cmd_approved(message):
    if not admin_only(message):
        return

    rows = list_approved()
    bot.reply_to(message, "\n".join(map(str, rows)))

# ====== ✅ EXPORT DB (ADMIN ONLY) ======
@bot.message_handler(commands=['exportdb'])
def cmd_exportdb(message):
    if not admin_only(message):
        return

    try:
        if not os.path.exists(DB_FILE):
            bot.reply_to(message, "❌ DB not found.")
            return

        backup_path = "/tmp/accounts_backup.db"
        shutil.copy(DB_FILE, backup_path)

        with open(backup_path, "rb") as f:
            bot.send_document(message.chat.id, f, caption="✅ Database Backup")

        os.remove(backup_path)

    except Exception as e:
        bot.reply_to(message, f"⚠️ Export failed: {e}")

# ====== MESSAGE FLOW ======
@bot.message_handler(func=lambda m: True)
def text_router(message):
    uid = message.from_user.id
    txt = (message.text or "").strip()

    if not is_approved(uid):
        bot.reply_to(message, "❌ Not approved.")
        return

    # ✅ STEP 1: Waiting for YES
    if user_state.get(uid) == "awaiting_yes":
        if txt.lower() == "yes":
            bot.reply_to(message, "Enter the mail ID", reply_markup=ReplyKeyboardRemove())
            user_state[uid] = "awaiting_email"
        else:
            bot.reply_to(message, "Exited. Type /start again.", reply_markup=ReplyKeyboardRemove())
            user_state.pop(uid, None)
        return

    # ✅ STEP 2: Waiting for Email
    if user_state.get(uid) == "awaiting_email":
        email_addr = txt

        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_addr):
            bot.reply_to(message, "❌ Invalid email. Try again.")
            return

        acc = get_account(email_addr)
        if not acc:
            bot.reply_to(message, "❌ This email is not in the database.")
            user_state.pop(uid, None)
            return

        bot.reply_to(message, "✅ Email accepted. Processing...")
        user_state.pop(uid, None)
        return

    # ✅ EXIT command
    if txt.lower() == "exit":
        bot.reply_to(message, "Exited.")
        user_state.pop(uid, None)
        return


# ====== WEBHOOK ======
@app.route("/" + BOT_TOKEN, methods=['POST'])
def webhook_receive():
    json_str = request.stream.read().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/")
def webhook_set():
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    webhook_url = render_url.rstrip("/") + "/" + BOT_TOKEN
    bot.remove_webhook()
    bot.set_webhook(url=webhook_url)
    return "Webhook set", 200

# ====== MAIN ======
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
