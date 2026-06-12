"""
EduGrands Marathon Bot — 8-9 graders
Mechanic: invite 2 friends via your referral link -> get the private chat link.

Setup:
  pip install python-telegram-bot==21.*
  Set env vars: BOT_TOKEN, PRIVATE_CHAT_ID, CHANNEL_USERNAMES, ADMIN_ID, ADMIN_PASSWORD
Deploy: Railway worker (same as GrantBek), no port needed (polling).
"""

import os
import io
import csv
import asyncio
import secrets
import sqlite3
import logging
import threading

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    MessageHandler, filters
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
PRIVATE_CHAT_ID = int(os.environ["PRIVATE_CHAT_ID"])  # e.g. -1001234567890; bot must be admin with invite rights
CHANNEL_USERNAMES = [c.strip() for c in os.environ.get("CHANNEL_USERNAMES", "").split(",") if c.strip()]
# e.g. CHANNEL_USERNAMES=@edugrandsuz,@second_channel  (empty = no check)
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
REQUIRED_INVITES = 2
DB_PATH = os.environ.get("DB_PATH", "marathon.db")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")  # required for web panel
PORT = int(os.environ.get("PORT", "8080"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("marathon")

# ---------- DB ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id     INTEGER PRIMARY KEY,
        username    TEXT,
        full_name   TEXT,
        referrer_id INTEGER,
        invites     INTEGER DEFAULT 0,
        unlocked    INTEGER DEFAULT 0,
        invite_link TEXT,
        joined_at   TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    try:
        conn.execute("ALTER TABLE users ADD COLUMN invite_link TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE users ADD COLUMN credited INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    for col in ("phone TEXT", "school TEXT", "reg_step TEXT", "age TEXT", "region TEXT"):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    return conn

def get_user(conn, uid):
    return conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

REGIONS = [
    "Toshkent sh.", "Toshkent vil.", "Andijon", "Farg'ona", "Namangan",
    "Samarqand", "Buxoro", "Navoiy", "Qashqadaryo", "Surxondaryo",
    "Jizzax", "Sirdaryo", "Xorazm", "Qoraqalpog'iston",
]

from urllib.parse import quote

def share_keyboard(link):
    share_text = quote("🎓 A Week of 8-9 Graders marafoniga qo'shil! Nufuzli maktablar va litseylar bilan jonli muloqotlar:")
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Do'stlarga ulashish", url=f"https://t.me/share/url?url={quote(link)}&text={share_text}")
    ]])

# ---------- Texts (Uzbek) ----------

def welcome_text(link):
    return (
        "🎓 *A Week of 8-9 Graders* marafoniga xush kelibsiz!\n\n"
        "Bir hafta davomida nufuzli maktablar, litseylar va dastur vakillari "
        "bilan jonli muloqotlar bo'lib o'tadi.\n\n"
        f"🔐 Maxsus chatga kirish uchun *{REQUIRED_INVITES} ta do'stingizni* "
        "taklif qiling.\n\n"
        f"🔗 Sizning shaxsiy havolangiz:\n`{link}`\n\n"
        "Havolani do'stlaringizga yuboring — ular botga kirishi bilan "
        "hisobingizga qo'shiladi. /status orqali jarayonni kuzating."
    )

def progress_text(invites):
    bar = "✅" * invites + "⬜️" * (REQUIRED_INVITES - invites)
    return f"📊 Takliflar: {invites}/{REQUIRED_INVITES}  {bar}"

ANNOUNCEMENT = (
    "🎓 *8–9-sinfdan keyin: litseymi, xususiy maktabmi yoki Prezident dasturi?*\n\n"
    "Ko'plab o'quvchilar va ota-onalar aynan shu bosqichda muhim tanlov oldida turishadi. "
    "Afsuski, ko'pchilik mavjud imkoniyatlar va foydali loyihalar haqida yetarlicha ma'lumotga ega emas.\n\n"
    "🚀 Shuning uchun biz O'zbekistondagi 8–9-sinflar uchun eng katta bepul marafonni ishga tushirdik!\n\n"
    "Bir hafta davom etadigan bu onlayn marafonimizda Prezident iqtidorli farzandlari dasturi, "
    "Thompson School, Target School, Rahimov School kabi yetakchi xususiy maktablar hamda "
    "INTERHOUSE, ALWIUT (Westminster) va ALUWED (JIDU) kabi TOP litseylarning vakillari va "
    "o'quvchilari qatnashishadi.\n\n"
    "Qatnashuvchilar qabul jarayonlari, grantlar, o'qish tizimi va ta'lim muhiti haqida "
    "birinchi shaxslardan ma'lumot olish hamda savollariga jonli javob olish imkoniyatiga "
    "ega bo'ladilar. ⚡️\n\n"
    "📅 20–27-iyun\n\n"
    "Marafonda qatnashish uchun mening havolam orqali ro'yxatdan o'ting 👇\n"
    "{link}"
)

ANNOUNCEMENT_SHORT = (
    "🎓 *8–9-sinfdan keyin: litseymi, xususiy maktabmi yoki Prezident dasturi?*\n\n"
    "🚀 O'zbekistondagi 8–9-sinflar uchun eng katta bepul marafon!\n\n"
    "Bir hafta davomida Prezident iqtidorli farzandlari dasturi, Thompson School, "
    "Target School, Rahimov School hamda INTERHOUSE, ALWIUT va ALUWED kabi TOP "
    "litseylarning vakillari bilan jonli muloqotlar: qabul, grantlar, o'qish tizimi "
    "va ta'lim muhiti — barchasi birinchi shaxslardan. ⚡️\n\n"
    "📅 20–27-iyun\n\n"
    "Qatnashish uchun mening havolam orqali ro'yxatdan o'ting 👇\n{link}"
)

def get_setting(conn, key):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else None

async def send_forwardable_post(context, user_id, link):
    conn = db()
    poster = get_setting(conn, "poster_file_id")
    conn.close()
    await context.bot.send_message(
        user_id,
        "📨 Quyidagi tayyor postni do'stlaringizga *forward qiling* — "
        "ular sizning havolangiz orqali qo'shilishadi:",
        parse_mode="Markdown",
    )
    if poster:
        await context.bot.send_photo(
            user_id, poster,
            caption=ANNOUNCEMENT_SHORT.format(link=link), parse_mode="Markdown",
        )
    else:
        await context.bot.send_message(user_id, ANNOUNCEMENT.format(link=link),
                                       parse_mode="Markdown")

async def setposter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data["awaiting_poster"] = True
    await update.message.reply_text("🖼 Endi poster rasmini yuboring (photo yoki fayl sifatida).")

async def poster_receiver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch the admin's next image after /setposter (photo or image file)."""
    if update.effective_user.id != ADMIN_ID or not context.user_data.get("awaiting_poster"):
        return
    file_id = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        file_id = update.message.document.file_id
    if not file_id:
        await update.message.reply_text("❗️ Rasm topilmadi, qayta yuboring.")
        return
    conn = db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('poster_file_id', ?)", (file_id,))
    conn.commit()
    conn.close()
    context.user_data["awaiting_poster"] = False
    await update.message.reply_text("✅ Poster saqlandi! Endi /post rasm bilan chiqadi.")

UNLOCK_TEXT = (
    "🎉 *Tabriklaymiz!* Siz {n} ta do'stingizni taklif qildingiz.\n\n"
    "Mana maxsus chatga havola:\n{link}\n\n"
    "Marafonda ko'rishguncha! 🚀"
)

# ---------- Helpers ----------

async def check_subscribed(context, user_id) -> bool:
    """True only if subscribed to ALL required channels."""
    for ch in CHANNEL_USERNAMES:
        try:
            m = await context.bot.get_chat_member(ch, user_id)
            if m.status not in ("member", "administrator", "creator"):
                return False
        except Exception:
            return False
    return True

def ref_link(bot_username, user_id):
    return f"https://t.me/{bot_username}?start=ref{user_id}"

async def get_or_create_invite(context, conn, user_id) -> str:
    """One-time (member_limit=1) invite link, generated once per user."""
    row = conn.execute("SELECT invite_link FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row and row[0]:
        return row[0]
    invite = await context.bot.create_chat_invite_link(
        chat_id=PRIVATE_CHAT_ID,
        member_limit=1,
        name=f"marathon-{user_id}",
    )
    conn.execute("UPDATE users SET invite_link=? WHERE user_id=?", (invite.invite_link, user_id))
    conn.commit()
    return invite.invite_link

async def credit_referrer_if_due(context, conn, user_id):
    """Credit the referrer once, only after this user has passed the subscription gate."""
    row = conn.execute(
        "SELECT referrer_id, credited FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    if not row or row[1] or not row[0]:
        return
    referrer_id = row[0]
    if not get_user(conn, referrer_id):
        conn.execute("UPDATE users SET credited=1 WHERE user_id=?", (user_id,))
        conn.commit()
        return
    conn.execute("UPDATE users SET credited=1 WHERE user_id=?", (user_id,))
    conn.execute("UPDATE users SET invites = invites + 1 WHERE user_id=?", (referrer_id,))
    conn.commit()
    r = get_user(conn, referrer_id)
    invites, unlocked = r[4], r[5]
    try:
        if invites >= REQUIRED_INVITES and not unlocked:
            conn.execute("UPDATE users SET unlocked=1 WHERE user_id=?", (referrer_id,))
            conn.commit()
            one_time = await get_or_create_invite(context, conn, referrer_id)
            await context.bot.send_message(
                referrer_id,
                UNLOCK_TEXT.format(n=invites, link=one_time),
                parse_mode="Markdown",
            )
        else:
            await context.bot.send_message(
                referrer_id,
                f"➕ Yangi do'stingiz qo'shildi!\n{progress_text(min(invites, REQUIRED_INVITES))}",
            )
    except Exception as e:
        log.warning(f"notify referrer failed: {e}")


import re as _re

async def start_registration(update_or_msg, context, conn, user_id):
    """Ask for phone with a share-contact button."""
    conn.execute("UPDATE users SET reg_step='phone' WHERE user_id=?", (user_id,))
    conn.commit()
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Raqamni yuborish", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await context.bot.send_message(
        user_id,
        "📋 *Ro'yxatdan o'tish*\n\nTelefon raqamingizni yuboring "
        "(tugmani bosing yoki +998... ko'rinishida yozing):",
        parse_mode="Markdown", reply_markup=kb,
    )

async def registration_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = db()
    row = conn.execute("SELECT reg_step FROM users WHERE user_id=?", (user.id,)).fetchone()
    if not row or not row[0]:
        conn.close()
        return  # not in registration flow

    step = row[0]
    if step == "phone":
        phone = None
        if update.message.contact and update.message.contact.user_id == user.id:
            phone = update.message.contact.phone_number
        elif update.message.text:
            cand = _re.sub(r"[^\d+]", "", update.message.text)
            if _re.fullmatch(r"\+?\d{9,15}", cand):
                phone = cand
        if not phone:
            await update.message.reply_text("❗️ Raqam noto'g'ri. +998901234567 ko'rinishida yuboring.")
            conn.close()
            return
        conn.execute("UPDATE users SET phone=?, reg_step='age' WHERE user_id=?", (phone, user.id))
        conn.commit()
        await update.message.reply_text(
            "🎂 Yoshingiz nechada? (raqamda yozing)",
            reply_markup=ReplyKeyboardRemove(),
        )
        conn.close()
        return

    if step == "age":
        age = (update.message.text or "").strip()
        if not age.isdigit() or not (8 <= int(age) <= 25):
            await update.message.reply_text("❗️ Yoshingizni raqamda yozing (masalan: 14).")
            conn.close()
            return
        conn.execute("UPDATE users SET age=?, reg_step='region' WHERE user_id=?", (age, user.id))
        conn.commit()
        rows, row = [], []
        for i, r in enumerate(REGIONS):
            row.append(InlineKeyboardButton(r, callback_data=f"reg:{i}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
        await update.message.reply_text(
            "📍 Qaysi hududdansiz?", reply_markup=InlineKeyboardMarkup(rows)
        )
        conn.close()
        return
    conn.close()

async def region_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    idx = int(q.data.split(":")[1])
    region = REGIONS[idx]
    conn = db()
    conn.execute("UPDATE users SET region=?, reg_step='done' WHERE user_id=?", (region, q.from_user.id))
    conn.commit()
    await credit_referrer_if_due(context, conn, q.from_user.id)
    conn.close()
    link = ref_link(context.bot.username, q.from_user.id)
    await q.message.edit_text(f"📍 {region} ✅")
    await q.message.reply_text(
        welcome_text(link), parse_mode="Markdown",
        reply_markup=share_keyboard(link),
    )
    await send_forwardable_post(context, q.from_user.id, link)

def is_registered(conn, user_id) -> bool:
    r = conn.execute("SELECT reg_step FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(r and r[0] == "done")

# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = db()

    is_new = get_user(conn, user.id) is None

    # parse referrer from deep link: /start ref12345
    referrer_id = None
    if context.args and context.args[0].startswith("ref"):
        try:
            referrer_id = int(context.args[0][3:])
        except ValueError:
            pass
    if referrer_id == user.id:
        referrer_id = None

    if is_new:
        conn.execute(
            "INSERT INTO users (user_id, username, full_name, referrer_id) VALUES (?,?,?,?)",
            (user.id, user.username, user.full_name, referrer_id),
        )
        conn.commit()

    # channel subscription gate — referral is credited only after passing it
    if not await check_subscribed(context, user.id):
        buttons = [
            [InlineKeyboardButton(f"📢 {ch} ga obuna bo'lish", url=f"https://t.me/{ch.lstrip('@')}")]
            for ch in CHANNEL_USERNAMES
        ]
        buttons.append([InlineKeyboardButton("✅ Obuna bo'ldim", callback_data="check_sub")])
        await update.message.reply_text(
            "Davom etish uchun avval kanallarimizga obuna bo'ling 👇",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        conn.close()
        return

    if not is_registered(conn, user.id):
        await start_registration(update, context, conn, user.id)
        conn.close()
        return

    await credit_referrer_if_due(context, conn, user.id)
    link = ref_link(context.bot.username, user.id)
    await update.message.reply_text(welcome_text(link), parse_mode="Markdown",
                                    reply_markup=share_keyboard(link))
    conn.close()

async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if await check_subscribed(context, q.from_user.id):
        conn = db()
        if not is_registered(conn, q.from_user.id):
            await start_registration(q, context, conn, q.from_user.id)
            conn.close()
            return
        await credit_referrer_if_due(context, conn, q.from_user.id)
        conn.close()
        link = ref_link(context.bot.username, q.from_user.id)
        await q.message.reply_text(welcome_text(link), parse_mode="Markdown",
                                   reply_markup=share_keyboard(link))
    else:
        await q.answer("Hali obuna bo'lmagansiz 🙂", show_alert=True)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    row = get_user(conn, update.effective_user.id)
    if not row:
        conn.close()
        await update.message.reply_text("Avval /start bosing.")
        return
    invites, unlocked = row[4], row[5]
    link = ref_link(context.bot.username, update.effective_user.id)
    msg = progress_text(min(invites, REQUIRED_INVITES)) + f"\n\n🔗 Havolangiz:\n`{link}`"
    if unlocked:
        one_time = await get_or_create_invite(context, conn, update.effective_user.id)
        msg += f"\n\n🔓 Sizning shaxsiy chat havolangiz (faqat 1 marta ishlaydi):\n{one_time}"
    conn.close()
    await update.message.reply_text(msg, parse_mode="Markdown",
                                    reply_markup=share_keyboard(link))

async def post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    row = get_user(conn, update.effective_user.id)
    conn.close()
    if not row:
        await update.message.reply_text("Avval /start bosing.")
        return
    link = ref_link(context.bot.username, update.effective_user.id)
    await send_forwardable_post(context, update.effective_user.id, link)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    unlocked = conn.execute("SELECT COUNT(*) FROM users WHERE unlocked=1").fetchone()[0]
    top = conn.execute(
        "SELECT full_name, username, invites FROM users ORDER BY invites DESC LIMIT 10"
    ).fetchall()
    conn.close()
    lines = [f"👥 Jami: {total}", f"🔓 Chatga kirganlar: {unlocked}", "", "🏆 Top 10:"]
    for name, uname, inv in top:
        lines.append(f"• {name} (@{uname}) — {inv}")
    await update.message.reply_text("\n".join(lines))

# ================= WEB ADMIN PANEL =================

web = FastAPI()
security = HTTPBasic()

def auth(creds: HTTPBasicCredentials = Depends(security)):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "Set ADMIN_PASSWORD env var to enable the panel")
    ok = secrets.compare_digest(creds.username, ADMIN_USER) and \
         secrets.compare_digest(creds.password, ADMIN_PASSWORD)
    if not ok:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return True

PANEL_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Marathon Admin</title><style>
body{font-family:system-ui,sans-serif;background:#0d1b2e;color:#eef;margin:0;padding:24px}
h1{font-size:20px;margin:0 0 16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px}
.card{background:#15294a;border-radius:10px;padding:14px}
.card b{display:block;font-size:24px}.card span{font-size:12px;opacity:.7}
input,textarea,button{font:inherit;border-radius:8px;border:1px solid #2a4a7a;background:#10223f;color:#eef;padding:8px}
button{background:#2563eb;border:none;cursor:pointer;padding:8px 16px}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:14px}
th,td{text-align:left;padding:8px;border-bottom:1px solid #1e3a5f}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
textarea{width:100%;min-height:70px}
.ok{color:#4ade80}.muted{opacity:.6}
</style></head><body>
<h1>🎓 Marathon Admin</h1>
<div class="cards" id="cards"></div>
<div class="row">
<input id="q" placeholder="Search name / username / region / ID" style="flex:1;min-width:200px">
<button onclick="load()">Search</button>
<button onclick="location.href='/admin/export.csv'">⬇ Export CSV</button>
</div>
<div><textarea id="bcast" placeholder="Broadcast message to all users..."></textarea>
<div class="row" style="margin-top:8px"><button onclick="sendB()">📣 Send broadcast</button>
<span id="bres" class="muted"></span></div></div>
<table><thead><tr><th>User</th><th>Username</th><th>Phone</th><th>Age</th><th>Region</th><th>Invites</th><th>Status</th><th>Joined</th></tr></thead>
<tbody id="tb"></tbody></table>
<script>
async function load(){
  const q=document.getElementById('q').value;
  const s=await (await fetch('/admin/api/stats')).json();
  document.getElementById('cards').innerHTML=
    `<div class=card><b>${s.total}</b><span>Total users</span></div>
     <div class=card><b>${s.credited}</b><span>Passed gate</span></div>
     <div class=card><b>${s.unlocked}</b><span>Unlocked chat</span></div>
     <div class=card><b>${s.today}</b><span>Joined today</span></div>`;
  const u=await (await fetch('/admin/api/users?q='+encodeURIComponent(q))).json();
  document.getElementById('tb').innerHTML=u.users.map(r=>
    `<tr><td>${r.full_name||''}</td><td>${r.username?'@'+r.username:''}</td>
     <td>${r.phone||''}</td><td>${r.age||''}</td><td>${r.region||''}</td><td>${r.invites}</td>
     <td>${r.unlocked?'<span class=ok>✅ unlocked</span>':(r.credited?'subscribed':'pending')}</td>
     <td class=muted>${(r.joined_at||'').slice(0,16)}</td></tr>`).join('');
}
async function sendB(){
  const t=document.getElementById('bcast').value.trim();
  if(!t)return alert('Empty message');
  if(!confirm('Send to ALL users?'))return;
  document.getElementById('bres').textContent='Sending...';
  const r=await (await fetch('/admin/api/broadcast',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})})).json();
  document.getElementById('bres').textContent=`Sent ${r.sent}/${r.total}, failed ${r.failed}`;
}
load();
</script></body></html>"""

@web.get("/admin", response_class=HTMLResponse)
def panel(_: bool = Depends(auth)):
    return PANEL_HTML

@web.get("/admin/api/stats")
def api_stats(_: bool = Depends(auth)):
    conn = db()
    out = {
        "total": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "credited": conn.execute("SELECT COUNT(*) FROM users WHERE credited=1").fetchone()[0],
        "unlocked": conn.execute("SELECT COUNT(*) FROM users WHERE unlocked=1").fetchone()[0],
        "today": conn.execute(
            "SELECT COUNT(*) FROM users WHERE DATE(joined_at)=DATE('now')").fetchone()[0],
    }
    conn.close()
    return out

@web.get("/admin/api/users")
def api_users(q: str = "", _: bool = Depends(auth)):
    conn = db()
    if q:
        like = f"%{q}%"
        rows = conn.execute(
            """SELECT user_id, username, full_name, phone, age, region, invites, unlocked, credited, joined_at
               FROM users WHERE full_name LIKE ? OR username LIKE ? OR region LIKE ? OR CAST(user_id AS TEXT) LIKE ?
               ORDER BY invites DESC LIMIT 200""", (like, like, like, like)).fetchall()
    else:
        rows = conn.execute(
            """SELECT user_id, username, full_name, phone, age, region, invites, unlocked, credited, joined_at
               FROM users ORDER BY invites DESC LIMIT 200""").fetchall()
    conn.close()
    keys = ["user_id", "username", "full_name", "phone", "age", "region", "invites", "unlocked", "credited", "joined_at"]
    return {"users": [dict(zip(keys, r)) for r in rows]}

@web.get("/admin/export.csv")
def export_csv(_: bool = Depends(auth)):
    conn = db()
    rows = conn.execute(
        "SELECT user_id, username, full_name, phone, age, region, referrer_id, invites, credited, unlocked, joined_at FROM users"
    ).fetchall()
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "username", "full_name", "phone", "age", "region", "referrer_id", "invites", "registered", "unlocked", "joined_at"])
    w.writerows(rows)
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=marathon_users.csv"})

@web.post("/admin/api/broadcast")
async def api_broadcast(req: Request, _: bool = Depends(auth)):
    body = await req.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "empty"}, status_code=400)
    conn = db()
    ids = [r[0] for r in conn.execute("SELECT user_id FROM users").fetchall()]
    conn.close()
    bot = Bot(BOT_TOKEN)
    sent = failed = 0
    async with bot:
        for uid in ids:
            try:
                await bot.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.07)  # ~14 msg/s, under Telegram's 30/s limit
    return {"total": len(ids), "sent": sent, "failed": failed}

def run_web():
    uvicorn.run(web, host="0.0.0.0", port=PORT, log_level="warning")

def main():
    threading.Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("post", post_cmd))
    app.add_handler(CommandHandler("setposter", setposter_cmd))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, poster_receiver))
    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(region_callback, pattern="^reg:"))
    app.add_handler(MessageHandler((filters.TEXT | filters.CONTACT) & ~filters.COMMAND, registration_handler))
    log.info(f"Marathon bot running, admin panel on :{PORT}/admin")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
