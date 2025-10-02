import os
import re
import sqlite3
import imaplib, poplib, email
from email.message import Message
from email.utils import parseaddr
from datetime import datetime, timedelta
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

# ---------------------- CONFIG ----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
MAX_EMAILS_CHECK = 20

bot = telebot.TeleBot(BOT_TOKEN)

DB_FILE = "household.db"

# ---------------------- DB SETUP ----------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS accounts (
        email TEXT PRIMARY KEY,
        password TEXT,
        protocol TEXT,
        server TEXT,
        port INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS approved_users (
        user_id INTEGER PRIMARY KEY,
        expiry_date TEXT
    )""")
    conn.commit()
    conn.close()

init_db()

# ---------------------- DB HELPERS ----------------------
def upsert_account(email, password, proto, server, port):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""INSERT INTO accounts (email,password,protocol,server,port)
        VALUES (?,?,?,?,?)
        ON CONFLICT(email) DO UPDATE SET
        password=excluded.password,
        protocol=excluded.protocol,
        server=excluded.server,
        port=excluded.port
    """, (email,password,proto,server,port))
    conn.commit()
    conn.close()

def get_account(email):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT email,password,protocol,server,port FROM accounts WHERE email=?", (email,))
    row = cur.fetchone()
    conn.close()
    return row

def upsert_approved(uid:int, days:int=None):
    expiry = None
    if days:
        expiry = (datetime.utcnow() + timedelta(days=days)).date().isoformat()
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""INSERT INTO approved_users (user_id,expiry_date)
        VALUES (?,?)
        ON CONFLICT(user_id) DO UPDATE SET expiry_date=excluded.expiry_date
    """, (uid,expiry))
    conn.commit()
    conn.close()

def delete_approved(uid:int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM approved_users WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def list_approved():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id,expiry_date FROM approved_users")
    rows = cur.fetchall()
    conn.close()
    return rows

def is_approved(uid:int):
    if uid == ADMIN_ID:
        return True
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT expiry_date FROM approved_users WHERE user_id=?", (uid,))
    row = cur.fetchone()
    conn.close()
    if not row: return False
    expiry=row[0]
    if not expiry: return True
    try:
        return datetime.strptime(expiry,"%Y-%m-%d").date() >= datetime.utcnow().date()
    except:
        return False

# ---------------------- EMAIL HELPERS ----------------------
def message_from_bytes_safe(raw): return email.message_from_bytes(raw)

def get_text_from_message(msg:Message)->str:
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() in ("text/plain","text/html"):
                try: return p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8","ignore")
                except: continue
    else:
        try: return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8","ignore")
        except: return ""
    return ""

def extract_links_from_text(text):
    patterns = [
        re.compile(r"https://www\.netflix\.com/account/update-primary-location\?nftoken=[^\s\"'<>]+", re.I),
        re.compile(r"https://www\.netflix\.com/account/travel/verify\?nftoken=[^\s\"'<>]+", re.I),
    ]
    links=[]
    for pat in patterns: links.extend(pat.findall(text))
    return list(dict.fromkeys(links))

def fetch_via_pop3(server,port,email_addr,password):
    conn=poplib.POP3_SSL(server,port,timeout=30); conn.user(email_addr); conn.pass_(password)
    num=len(conn.list()[1]); start=max(1,num-MAX_EMAILS_CHECK+1); results=[]
    for i in range(start,num+1):
        raw=b"\n".join(conn.retr(i)[1]); msg=message_from_bytes_safe(raw)
        if not msg: continue
        text=get_text_from_message(msg); links=extract_links_from_text(text)
        if links: results.extend(links)
    conn.quit(); return results

def fetch_via_imap(server,port,email_addr,password):
    m=imaplib.IMAP4_SSL(server,port); m.login(email_addr,password); m.select("INBOX",readonly=True)
    typ,data=m.search(None,"ALL"); results=[]
    if typ=="OK":
        ids=data[0].split()
        for i in ids[-MAX_EMAILS_CHECK:]:
            typ,msg_data=m.fetch(i,"(RFC822)")
            if typ=="OK" and msg_data and isinstance(msg_data[0],tuple):
                msg=message_from_bytes_safe(msg_data[0][1]); text=get_text_from_message(msg)
                links=extract_links_from_text(text)
                if links: results.extend(links)
    m.logout(); return results

def fetch_links(email, password, proto, server, port):
    if proto=="imap": return fetch_via_imap(server,port,email,password)
    if proto=="pop3": return fetch_via_pop3(server,port,email,password)
    return []

# ---------------------- BOT STATES ----------------------
user_state = {}

# ---------------------- COMMANDS ----------------------
@bot.message_handler(commands=['start'])
def cmd_start(m):
    if not is_approved(m.from_user.id):
        bot.reply_to(m,"❌ You are not approved.")
        return
    kb=ReplyKeyboardMarkup(resize_keyboard=True); kb.row(KeyboardButton("Yes"),KeyboardButton("Exit"))
    bot.reply_to(m,"👋 Hi, Household Bot this side\nLooks like you have faced a household issue...\nEnter Yes/yes to get the code or Exit/exit to exit.",reply_markup=kb)
    user_state[m.from_user.id]="awaiting_yes"

@bot.message_handler(commands=['add'])
def cmd_add(m):
    if m.from_user.id!=ADMIN_ID: return
    try:
        _,email,pw,proto,server,port=m.text.split()
        upsert_account(email,pw,proto.lower(),server,int(port))
        bot.reply_to(m,f"✅ Saved {email}")
    except:
        bot.reply_to(m,"Usage: /add <email> <pass> <imap|pop3> <server> <port>")

@bot.message_handler(commands=['approve'])
def cmd_approve(m):
    if m.from_user.id!=ADMIN_ID: return
    try:
        parts=m.text.split()
        uid=int(parts[1]); days=int(parts[2]) if len(parts)>2 else None
        upsert_approved(uid,days)
        bot.reply_to(m,f"✅ Approved {uid} {'for '+str(days)+' days' if days else 'permanently'}")
    except:
        bot.reply_to(m,"Usage: /approve <id> [days]")

@bot.message_handler(commands=['unapprove'])
def cmd_unapprove(m):
    if m.from_user.id!=ADMIN_ID: return
    try:
        _,uid=m.text.split(); delete_approved(int(uid)); bot.reply_to(m,f"Unapproved {uid}")
    except:
        bot.reply_to(m,"Usage: /unapprove <id>")

@bot.message_handler(commands=['approved'])
def cmd_listapproved(m):
    if m.from_user.id!=ADMIN_ID: return
    rows=list_approved()
    if not rows: bot.reply_to(m,"No approved users."); return
    txt="\n".join([f"{u} → {d or 'permanent'}" for u,d in rows])
    bot.reply_to(m,"✅ Approved users:\n"+txt)

# ---------------------- TEXT FLOW ----------------------
@bot.message_handler(func=lambda msg: True)
def text_router(m):
    uid=m.from_user.id; txt=(m.text or "").strip()
    if not is_approved(uid):
        bot.reply_to(m,"❌ You are not approved."); return

    if user_state.get(uid)=="awaiting_yes":
        if txt.lower()=="yes":
            bot.reply_to(m,"✍️ Enter the mail ID now.",reply_markup=ReplyKeyboardRemove())
            user_state[uid]="awaiting_email"
        else:
            bot.reply_to(m,"Exited.",reply_markup=ReplyKeyboardRemove())
            user_state.pop(uid,None)

    elif user_state.get(uid)=="awaiting_email":
        acc=get_account(txt)
        if not acc:
            bot.reply_to(m,"❌ Email not in DB."); user_state.pop(uid,None); return
        email,pw,proto,server,port=acc
        try:
            links=fetch_links(email,pw,proto,server,port)
        except Exception as e:
            bot.reply_to(m,f"⚠️ Error: {e}"); user_state.pop(uid,None); return
        if not links:
            bot.reply_to(m,"❌ No household links found."); user_state.pop(uid,None); return
        bot.reply_to(m,"📬 "+email+"\n"+"\n".join("🔗 "+l for l in links))
        user_state.pop(uid,None)

# ---------------------- START ----------------------
if __name__=="__main__":
    print("🤖 Household Bot running...")
    bot.infinity_polling()
