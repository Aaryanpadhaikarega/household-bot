#!/usr/bin/env python3
"""
household_bot.py
- Admin commands: /add /del /list /importcsv
- Approvals with expiry: /approve <id> [days], /unapprove, /approved
- Auto-sync DB <-> CSV (accounts.csv, approved.csv)
- Expired approvals are cleaned and do not come back
"""

import os, re, csv, sqlite3, imaplib, poplib, email
from email.message import Message
from email.utils import parseaddr
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timedelta

from dotenv import load_dotenv
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from flask import Flask, request

# ===== CONFIG =====
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_FILE = os.getenv("DB_FILE", "accounts.db")
ACCOUNTS_CSV = os.getenv("ACCOUNTS_CSV", "accounts.csv")
APPROVED_CSV = os.getenv("APPROVED_CSV", "approved.csv")
MAX_EMAILS_CHECK = int(os.getenv("MAX_EMAILS_CHECK", "20"))

if not BOT_TOKEN: raise SystemExit("BOT_TOKEN missing")
if not ADMIN_ID: raise SystemExit("ADMIN_ID missing")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ===== OTT patterns =====
OTT_SENDERS = ["info@account.netflix.com", "no-reply@account.netflix.com"]
NETFLIX_LINK_PATTERNS = [
    re.compile(r"https://www\.netflix\.com/account/update-primary-location\?nftoken=[^\s\"'<>]+", re.I),
    re.compile(r"https://www\.netflix\.com/account/travel/verify\?nftoken=[^\s\"'<>]+", re.I),
]

# ===== Data model =====
@dataclass
class Account:
    email: str
    password: str
    protocol: str
    server: str
    port: int

# ===== DB setup =====
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS accounts(
        email TEXT PRIMARY KEY, password TEXT, protocol TEXT, server TEXT, port INTEGER)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS approved_users(
        user_id INTEGER PRIMARY KEY, expiry_date TEXT)""")
    con.commit(); con.close()

# ===== CSV <-> DB Sync =====
def export_accounts_to_csv():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor(); cur.execute("SELECT email,password,protocol,server,port FROM accounts ORDER BY email")
    rows = cur.fetchall(); con.close()
    with open(ACCOUNTS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f); writer.writerow(["email","password","protocol","server","port"]); writer.writerows(rows)

def export_approved_to_csv():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor(); cur.execute("SELECT user_id,expiry_date FROM approved_users ORDER BY user_id")
    rows = cur.fetchall(); con.close()
    with open(APPROVED_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f); writer.writerow(["user_id","expiry_date"]); writer.writerows(rows)

def import_accounts_from_csv():
    if not os.path.exists(ACCOUNTS_CSV): return 0
    added=0
    with open(ACCOUNTS_CSV, newline="", encoding="utf-8") as f:
        reader=csv.DictReader(f)
        for row in reader:
            if not row.get("email"): continue
            acc=Account(row["email"], row["password"], row["protocol"].lower(), row["server"], int(row["port"]))
            upsert_account_db(acc); added+=1
    export_accounts_to_csv(); return added

def import_approved_from_csv():
    if not os.path.exists(APPROVED_CSV): return 0
    today=datetime.utcnow().date(); added=0
    with open(APPROVED_CSV, newline="", encoding="utf-8") as f:
        reader=csv.DictReader(f)
        for row in reader:
            if not row.get("user_id"): continue
            try: uid=int(row["user_id"])
            except: continue
            expiry=row.get("expiry_date") or ""
            if expiry:
                try:
                    exp=datetime.strptime(expiry,"%Y-%m-%d").date()
                    if exp<today: continue
                except: continue
            upsert_approved_db(uid, expiry if expiry else None); added+=1
    export_approved_to_csv(); return added

# ===== DB ops =====
def upsert_account_db(acc:Account):
    con=sqlite3.connect(DB_FILE); cur=con.cursor()
    cur.execute("""INSERT INTO accounts VALUES(?,?,?,?,?)
        ON CONFLICT(email) DO UPDATE SET password=excluded.password,
        protocol=excluded.protocol, server=excluded.server, port=excluded.port""",
        (acc.email,acc.password,acc.protocol,acc.server,acc.port))
    con.commit(); con.close(); export_accounts_to_csv()

def delete_account_db(email):
    con=sqlite3.connect(DB_FILE); cur=con.cursor()
    cur.execute("DELETE FROM accounts WHERE email=?",(email,))
    con.commit(); con.close(); export_accounts_to_csv()

def get_account_db(email)->Optional[Account]:
    con=sqlite3.connect(DB_FILE); cur=con.cursor()
    cur.execute("SELECT email,password,protocol,server,port FROM accounts WHERE email=?",(email,))
    row=cur.fetchone(); con.close()
    return Account(*row) if row else None

def list_accounts_db():
    con=sqlite3.connect(DB_FILE); cur=con.cursor()
    cur.execute("SELECT email,server,port FROM accounts ORDER BY email")
    rows=cur.fetchall(); con.close(); return rows

def upsert_approved_db(uid:int, expiry:str):
    con=sqlite3.connect(DB_FILE); cur=con.cursor()
    cur.execute("""INSERT INTO approved_users VALUES(?,?)
        ON CONFLICT(user_id) DO UPDATE SET expiry_date=excluded.expiry_date""",(uid,expiry))
    con.commit(); con.close(); export_approved_to_csv()

def delete_approved_db(uid:int):
    con=sqlite3.connect(DB_FILE); cur=con.cursor()
    cur.execute("DELETE FROM approved_users WHERE user_id=?",(uid,))
    con.commit(); con.close(); export_approved_to_csv()

def get_approved_db(uid:int)->Optional[str]:
    con=sqlite3.connect(DB_FILE); cur=con.cursor()
    cur.execute("SELECT expiry_date FROM approved_users WHERE user_id=?",(uid,))
    row=cur.fetchone(); con.close(); return row[0] if row else None

def list_approved_db():
    con=sqlite3.connect(DB_FILE); cur=con.cursor()
    cur.execute("SELECT user_id,expiry_date FROM approved_users ORDER BY user_id")
    rows=cur.fetchall(); con.close(); return rows

# ===== Approval check =====
def is_admin(uid): return uid==ADMIN_ID
def is_approved(uid):
    if is_admin(uid): return True
    expiry=get_approved_db(uid)
    if not expiry: return False
    try: return datetime.strptime(expiry,"%Y-%m-%d").date()>=datetime.utcnow().date()
    except: return False

# ===== Email fetch helpers =====
def message_from_bytes_safe(raw): return email.message_from_bytes(raw)
def get_text_from_message(msg:Message)->str:
    if msg.is_multipart():
        parts=[]
        for p in msg.walk():
            if p.get_content_type() in ("text/plain","text/html"):
                try: parts.append(p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8","ignore"))
                except: continue
        return "\n".join(parts)
    else:
        try: return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8","ignore")
        except: return ""

def extract_links_from_text(text):
    links=[]
    for pat in NETFLIX_LINK_PATTERNS: links.extend(pat.findall(text))
    seen=set(); clean=[]
    for u in links:
        u=u.strip().rstrip(').,;\'"')
        if u not in seen: seen.add(u); clean.append(u)
    return clean

def fetch_via_pop3(server,port,email_addr,password):
    out=[]
    conn=poplib.POP3_SSL(server,port,timeout=30); conn.user(email_addr); conn.pass_(password)
    num=len(conn.list()[1]); start=max(1,num-MAX_EMAILS_CHECK+1)
    for i in range(start,num+1):
        raw=b"\n".join(conn.retr(i)[1]); msg=message_from_bytes_safe(raw)
        if parseaddr(msg.get("From",""))[1].lower() not in [s.lower() for s in OTT_SENDERS]: continue
        text=get_text_from_message(msg); links=extract_links_from_text(text)
        if links: out.append(links)
    conn.quit(); return out

def fetch_via_imap(server,port,email_addr,password):
    out=[]; m=imaplib.IMAP4_SSL(server,port); m.login(email_addr,password); m.select("INBOX",readonly=True)
    ids=set()
    for s in OTT_SENDERS:
        typ,data=m.search(None,f'(FROM "{s}")')
        if typ=="OK" and data and data[0]: ids.update(data[0].split())
    if not ids: m.logout(); return out
    for i in sorted(list(ids),key=lambda x:int(x),reverse=True)[:MAX_EMAILS_CHECK]:
        typ,msg_data=m.fetch(i,"(RFC822)")
        if typ=="OK" and msg_data and isinstance(msg_data[0],tuple):
            msg=message_from_bytes_safe(msg_data[0][1]); text=get_text_from_message(msg)
            links=extract_links_from_text(text); 
            if links: out.append(links)
    m.logout(); return out

def fetch_household_info(acc:Account):
    return fetch_via_imap(acc.server,acc.port,acc.email,acc.password) if acc.protocol=="imap" else fetch_via_pop3(acc.server,acc.port,acc.email,acc.password)

# ===== Bot State =====
user_state:Dict[int,str]={}
def greet_text(): return "Hi, Household Bot this side\nLooks like you have faced an household issue... (Yes/Exit)"

# ===== Handlers =====
@bot.message_handler(commands=['start'])
def cmd_start(m):
    if not is_approved(m.from_user.id): bot.reply_to(m,"❌ Not approved"); return
    kb=ReplyKeyboardMarkup(resize_keyboard=True); kb.row(KeyboardButton("Yes"),KeyboardButton("Exit"))
    bot.reply_to(m,greet_text(),reply_markup=kb); user_state[m.from_user.id]="awaiting_yes"

@bot.message_handler(commands=['add'])
def cmd_add(m):
    if not is_admin(m.from_user.id): return
    try:
        _,email_addr,pw,proto,server,port=m.text.split()
        upsert_account_db(Account(email_addr,pw,proto.lower(),server,int(port)))
        bot.reply_to(m,f"✅ Saved {email_addr}")
    except: bot.reply_to(m,"Usage: /add <email> <pass> <imap|pop3> <server> <port>")

@bot.message_handler(commands=['del'])
def cmd_del(m):
    if not is_admin(m.from_user.id): return
    try: _,email_addr=m.text.split(); delete_account_db(email_addr); bot.reply_to(m,f"🗑️ Deleted {email_addr}")
    except: bot.reply_to(m,"Usage: /del <email>")

@bot.message_handler(commands=['list'])
def cmd_list(m):
    if not is_admin(m.from_user.id): return
    rows=list_accounts_db(); bot.reply_to(m,"📋 Accounts:\n"+"\n".join([f"{e} {s}:{p}" for e,s,p in rows]) if rows else "None")

@bot.message_handler(commands=['importcsv'])
def cmd_importcsv(m):
    if not is_admin(m.from_user.id): return
    bot.reply_to(m,f"Imported {import_accounts_from_csv()} accounts, {import_approved_from_csv()} approvals")

@bot.message_handler(commands=['approve'])
def cmd_approve(m):
    if not is_admin(m.from_user.id): return
    try:
        parts=m.text.split(); uid=int(parts[1]); days=int(parts[2]) if len(parts)>2 else 30
        expiry=(datetime.utcnow().date()+timedelta(days=days)).isoformat()
        upsert_approved_db(uid,expiry); bot.reply_to(m,f"✅ Approved {uid} until {expiry}")
    except: bot.reply_to(m,"Usage: /approve <id> [days]")

@bot.message_handler(commands=['unapprove'])
def cmd_unapprove(m):
    if not is_admin(m.from_user.id): return
    try: _,uid=m.text.split(); delete_approved_db(int(uid)); bot.reply_to(m,f"Unapproved {uid}")
    except: bot.reply_to(m,"Usage: /unapprove <id>")

@bot.message_handler(commands=['approved'])
def cmd_list_approved(m):
    if not is_admin(m.from_user.id): return
    rows=list_approved_db()
    bot.reply_to(m,"✅ Approved:\n"+"\n".join([f"{u} exp {d}" for u,d in rows]) if rows else "None")

@bot.message_handler(func=lambda m: True)
def text_router(m):
    uid=m.from_user.id; txt=(m.text or "").strip()
    if not is_approved(uid): bot.reply_to(m,"❌ Not approved"); return
    if user_state.get(uid)=="awaiting_yes":
        if txt.lower()=="yes": bot.reply_to(m,"Enter mail ID",reply_markup=ReplyKeyboardRemove()); user_state[uid]="awaiting_email"
        else: bot.reply_to(m,"Exited",reply_markup=ReplyKeyboardRemove()); user_state.pop(uid,None)
        return
    if user_state.get(uid)=="awaiting_email":
        acc=get_account_db(txt)
        if not acc: bot.reply_to(m,"❌ Not in DB"); user_state.pop(uid,None); return
        try: results=fetch_household_info(acc)
        except Exception as e: bot.reply_to(m,f"⚠️ Error: {e}"); user_state.pop(uid,None); return
        if not results: bot.reply_to(m,"❌ No household emails"); user_state.pop(uid,None); return
        reply=[f"📬 {acc.email}"]
        for links in results: [reply.append(f"🔗 {ln}") for ln in links]; reply.append("——")
        bot.reply_to(m,"\n".join(reply)); user_state.pop(uid,None); return

# ===== Webhook =====
@app.route("/"+BOT_TOKEN,methods=['POST'])
def webhook(): update=telebot.types.Update.de_json(request.stream.read().decode("utf-8")); bot.process_new_updates([update]); return "OK",200
@app.route("/")
def home():
    render_url=os.getenv("RENDER_EXTERNAL_URL") or "https://household-bot.onrender.com"
    bot.remove_webhook(); bot.set_webhook(url=render_url.rstrip("/")+"/"+BOT_TOKEN); return "Webhook set",200

# ===== Main =====
if __name__=="__main__":
    init_db(); import_accounts_from_csv(); import_approved_from_csv()
    export_accounts_to_csv(); export_approved_to_csv()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)))
