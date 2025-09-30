import os
import sqlite3
import telebot
from telebot import types
from dotenv import load_dotenv
import imaplib, poplib, email
from typing import List
from flask import Flask, request

# ===========================
# Load ENV
# ===========================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DB_FILE = os.getenv("DB_FILE", "accounts.db")
CSV_BOOTSTRAP = os.getenv("CSV_BOOTSTRAP", "accounts.csv")
MAX_EMAILS_CHECK = int(os.getenv("MAX_EMAILS_CHECK", 20))

bot = telebot.TeleBot(BOT_TOKEN)

# ===========================
# DB Setup
# ===========================
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    # Accounts table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            email TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            protocol TEXT NOT NULL CHECK (protocol IN ('imap','pop3')),
            server TEXT NOT NULL,
            port INTEGER NOT NULL
        )
    """)
    # Approved users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS approved_users (
            user_id INTEGER PRIMARY KEY
        )
    """)
    con.commit()
    con.close()

init_db()

# ===========================
# Helper Functions
# ===========================
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

def unapprove_user(user_id: int):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DELETE FROM approved_users WHERE user_id=?", (user_id,))
    con.commit()
    con.close()

def list_approved() -> List[int]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT user_id FROM approved_users")
    rows = [r[0] for r in cur.fetchall()]
    con.close()
    return rows

# ===========================
# Mail Fetcher
# ===========================
def fetch_link(email_addr, password, protocol, server, port):
    try:
        if protocol == "imap":
            mail = imaplib.IMAP4_SSL(server, port)
            mail.login(email_addr, password)
            mail.select("inbox")
            typ, data = mail.search(None, "ALL")
            ids = data[0].split()
            ids = ids[-MAX_EMAILS_CHECK:]
            for eid in reversed(ids):
                typ, msg_data = mail.fetch(eid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                if "http" in msg.get_payload(decode=True).decode(errors="ignore"):
                    for word in msg.get_payload(decode=True).decode(errors="ignore").split():
                        if word.startswith("http"):
                            return word
            return None
        else:
            mail = poplib.POP3_SSL(server, port)
            mail.user(email_addr)
            mail.pass_(password)
            num, _ = mail.stat()
            for i in range(max(1, num - MAX_EMAILS_CHECK), num + 1):
                _, lines, _ = mail.retr(i)
                msg = b"\n".join(lines)
                msg = email.message_from_bytes(msg)
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(errors="ignore")
                            for word in body.split():
                                if word.startswith("http"):
                                    return word
                else:
                    body = msg.get_payload(decode=True).decode(errors="ignore")
                    for word in body.split():
                        if word.startswith("http"):
                            return word
            return None
    except Exception as e:
        return f"Error: {str(e)}"

# ===========================
# Commands
# ===========================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    uid = message.from_user.id
    if not is_approved(uid):
        bot.reply_to(message, "❌ You are not approved to use this bot.\nPlease contact the admin.")
        return
    bot.reply_to(message, "👋 Hi, Household Bot this side.\nLooks like you faced a household issue.\nEnter Yes/yes to get the code or Exit/exit to exit.")

@bot.message_handler(commands=['approve'])
def cmd_approve(message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, uid_str = message.text.split()
        uid = int(uid_str)
        approve_user(uid)
        bot.reply_to(message, f"✅ Approved user {uid}")
    except:
        bot.reply_to(message, "Usage: /approve <telegram_id>")

@bot.message_handler(commands=['unapprove'])
def cmd_unapprove(message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, uid_str = message.text.split()
        uid = int(uid_str)
        unapprove_user(uid)
        bot.reply_to(message, f"🗑️ Unapproved user {uid}")
    except:
        bot.reply_to(message, "Usage: /unapprove <telegram_id>")

@bot.message_handler(commands=['approved'])
def cmd_list_approved(message):
    if not is_admin(message.from_user.id):
        return
    rows = list_approved()
    if not rows:
        bot.reply_to(message, "No approved users yet.")
    else:
        pretty = "\n".join([f"• {uid}" for uid in rows])
        bot.reply_to(message, "✅ Approved users:\n" + pretty)

# ===========================
# Text Handler
# ===========================
@bot.message_handler(func=lambda m: True)
def text_router(message):
    uid = message.from_user.id
    if not is_approved(uid):
        bot.reply_to(message, "❌ You are not approved to use this bot.\nPlease contact the admin.")
        return

    txt = message.text.strip().lower()
    if txt in ["yes", "y"]:
        bot.reply_to(message, "✅ Enter the mail ID:")
    elif "@" in message.text:
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        cur.execute("SELECT * FROM accounts WHERE email=?", (message.text.strip(),))
        row = cur.fetchone()
        con.close()
        if row:
            email_addr, pw, proto, srv, port = row
            link = fetch_link(email_addr, pw, proto, srv, port)
            if link:
                bot.reply_to(message, f"Here’s your link:\n{link}")
            else:
                bot.reply_to(message, "No link found in recent mails.")
        else:
            bot.reply_to(message, "❌ Email not in database.")
    elif txt in ["exit", "cancel"]:
        bot.reply_to(message, "👋 Exiting. Have a nice day!")
    else:
        bot.reply_to(message, "❓ Please reply Yes/yes to continue or Exit/exit to quit.")

# ===========================
# Webhook Setup
# ===========================
app = Flask(__name__)

@app.route("/" + BOT_TOKEN, methods=['POST'])
def getMessage():
    json_str = request.stream.read().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/")
def webhook():
    bot.remove_webhook()
    bot.set_webhook(url="https://household-bot.onrender.com/" + BOT_TOKEN)
    return "Webhook set", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
