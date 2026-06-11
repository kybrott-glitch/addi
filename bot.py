"""
Telegram Ad Bot - Full Featured
Controls multiple Telegram user accounts and broadcasts ad messages to groups.
"""

import asyncio
import logging
import sqlite3
import os
from datetime import datetime
from typing import Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeExpiredError,
    PhoneCodeInvalidError, FloodWaitError, UserBannedInChannelError,
    ChatWriteForbiddenError
)
from telethon.tl.types import Channel, Chat

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── States ──────────────────────────────────────────────────────────────────
(
    MAIN_MENU, DASHBOARD, ADD_ACCOUNT_PHONE, ADD_ACCOUNT_OTP, ADD_ACCOUNT_2FA,
    SET_AD_MESSAGE, SET_INTERVAL, SELECT_ACCOUNT_SETTINGS,
    ACCOUNT_CUSTOM_MSG, ACCOUNT_CUSTOM_INTERVAL, AUTO_REPLY_MSG
) = range(11)

# ╔══════════════════════════════════════════════════════════════════╗
# ║                        CONFIGURATION                            ║
# ╠══════════════════════════════════════════════════════════════════╣
# ║  1. BOT_TOKEN  → @BotFather on Telegram → /newbot               ║
# ║  2. API_ID     → https://my.telegram.org → API Development      ║
# ║  3. API_HASH   → same page as API_ID                            ║
# ║  4. OWNER_ID   → message @userinfobot to get your user ID       ║
# ╚══════════════════════════════════════════════════════════════════╝

BOT_TOKEN    = "8436389782:AAHiL2mYHd89uoGickZu7ZOL-8uS9eBkPWU"
API_ID       = 21752358
API_HASH     = "fb46a136fed4a4de27ab057c7027fec3"
OWNER_ID     = 1899208318

# ── Advanced (optional to change) ────────────────────────────────────────────
MAX_ACCOUNTS  = 50
SESSION_DIR   = "sessions"
DB_PATH       = "data/adbot.db"

# ── Database ─────────────────────────────────────────────────────────────────
def init_db():
    os.makedirs("data", exist_ok=True)
    os.makedirs(SESSION_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT UNIQUE,
            name        TEXT,
            active      INTEGER DEFAULT 1,
            ad_message  TEXT,
            interval       INTEGER DEFAULT 300,
            auto_reply     TEXT,
            added_at       TEXT,
            last_run       TEXT,
            log_channel_id INTEGER DEFAULT NULL,
            log_channel_url TEXT DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS global_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS analytics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT,
            event       TEXT,
            detail      TEXT,
            ts          TEXT
        );
    """)
    # defaults
    for k, v in [("global_message",""), ("global_interval","300"), ("status","paused")]:
        c.execute("INSERT OR IGNORE INTO global_settings VALUES (?,?)", (k, v))
    conn.commit()
    conn.close()

def db():
    return sqlite3.connect(DB_PATH)

def get_setting(key):
    with db() as conn:
        r = conn.execute("SELECT value FROM global_settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

def set_setting(key, value):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO global_settings VALUES (?,?)", (key, str(value)))

def get_accounts():
    with db() as conn:
        return conn.execute("SELECT * FROM accounts").fetchall()

def get_account(phone):
    with db() as conn:
        return conn.execute("SELECT * FROM accounts WHERE phone=?", (phone,)).fetchone()

def add_account(phone, name):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (phone,name,added_at) VALUES (?,?,?)",
            (phone, name, datetime.now().isoformat())
        )

def update_account(phone, **kwargs):
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [phone]
    with db() as conn:
        conn.execute(f"UPDATE accounts SET {sets} WHERE phone=?", vals)

def delete_account_db(phone):
    with db() as conn:
        conn.execute("DELETE FROM accounts WHERE phone=?", (phone,))

def log_event(phone, event, detail=""):
    with db() as conn:
        conn.execute(
            "INSERT INTO analytics (phone,event,detail,ts) VALUES (?,?,?,?)",
            (phone, event, detail, datetime.now().isoformat())
        )

def get_analytics():
    with db() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM analytics WHERE event='sent'").fetchone()[0]
        failed  = conn.execute("SELECT COUNT(*) FROM analytics WHERE event='failed'").fetchone()[0]
        cycles  = conn.execute("SELECT COUNT(*) FROM analytics WHERE event='cycle_done'").fetchone()[0]
        accs    = conn.execute("SELECT COUNT(*) FROM accounts WHERE active=1").fetchone()[0]
        return {"sent": total, "failed": failed, "cycles": cycles, "active": accs}

# ── Telethon client pool ──────────────────────────────────────────────────────
clients: Dict[str, TelegramClient] = {}
pending_phones: Dict[int, str] = {}   # user_id -> phone being added

def session_path(phone):
    safe = phone.replace("+", "").replace(" ", "")
    return os.path.join(SESSION_DIR, safe)

async def get_client(phone) -> TelegramClient:
    if phone not in clients:
        c = TelegramClient(session_path(phone), API_ID, API_HASH)
        await c.connect()
        clients[phone] = c
    return clients[phone]

# ── Log Channel Manager ──────────────────────────────────────────────────────
async def ensure_log_channel(phone: str) -> tuple:
    """Create a log channel for this account if it doesn't exist yet.
    Returns (channel_id, invite_link) or (None, None) on failure."""
    acc = get_account(phone)
    if acc and acc[9]:   # log_channel_id column index 9
        return acc[9], acc[10]
    try:
        from telethon.tl.functions.channels import CreateChannelRequest, ExportChatInviteRequest
        from telethon.tl.functions.messages import ExportChatInviteRequest as MsgExportInvite
        client = await get_client(phone)
        result = await client(CreateChannelRequest(
            title=f"📋 AdBot Logs | {phone}",
            about="Auto-generated log channel for ad broadcast monitoring.",
            megagroup=False,   # False = broadcast channel
        ))
        channel = result.chats[0]
        cid     = channel.id
        # Export invite link
        inv = await client(ExportChatInviteRequest(channel))
        url = inv.link
        update_account(phone, log_channel_id=cid, log_channel_url=url)
        logger.info(f"[{phone}] Log channel created: {url}")
        return cid, url
    except Exception as e:
        logger.error(f"[{phone}] Failed to create log channel: {e}")
        return None, None

async def post_to_log_channel(phone: str, channel_id: int, group_name: str, msg_id: int, group_username: str = None):
    """Post a sent-message log entry to the account's log channel."""
    try:
        client = await get_client(phone)
        now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if group_username:
            link = f"https://t.me/{group_username}/{msg_id}"
            text = (
                f"📤 <b>Ad Sent</b>\n"
                f"🕒 {now}\n"
                f"💬 Group: <b>{group_name}</b>\n"
                f"🔗 <a href='{link}'>View Message</a>"
            )
        else:
            text = (
                f"📤 <b>Ad Sent</b>\n"
                f"🕒 {now}\n"
                f"💬 Group: <b>{group_name}</b>\n"
                f"<i>(Private group — no public link)</i>"
            )
        await client.send_message(channel_id, text, parse_mode='html')
    except Exception as e:
        logger.warning(f"[{phone}] Log channel post failed: {e}")

# ── Ad Broadcast Engine ───────────────────────────────────────────────────────
broadcasting = False          # global start/stop (dashboard)
broadcast_task = None
active_broadcasts: dict = {}  # phone -> asyncio.Task (per-account)

def is_group(entity):
    """Return True if the entity is a group or supergroup (not a channel)."""
    if isinstance(entity, Chat):
        return True
    if isinstance(entity, Channel) and entity.megagroup:
        return True
    return False

async def broadcast_loop(app):
    global broadcasting
    logger.info("Broadcast loop started")
    while broadcasting:
        accounts = get_accounts()
        if not accounts:
            logger.warning("No accounts in DB, waiting 30s")
            await asyncio.sleep(30)
            continue

        for acc in accounts:
            if not broadcasting:
                break
            phone    = acc[1]
            active   = acc[3]
            msg      = acc[4] or get_setting("global_message")
            interval = int(acc[5] or get_setting("global_interval") or 300)

            if not active:
                logger.info(f"[{phone}] Skipped (inactive)")
                continue
            if not msg:
                logger.info(f"[{phone}] Skipped (no message set)")
                continue

            try:
                client = await get_client(phone)
                if not await client.is_user_authorized():
                    logger.warning(f"[{phone}] Not authorized, skipping")
                    continue

                dialogs = await client.get_dialogs()
                groups  = [d for d in dialogs if is_group(d.entity)]
                logger.info(f"[{phone}] Found {len(groups)} groups, sending...")

                sent = 0
                for dialog in groups:
                    if not broadcasting:
                        break
                    try:
                        await client.send_message(dialog.entity, msg)
                        sent += 1
                        log_event(phone, "sent", dialog.name)
                        logger.info(f"[{phone}] Sent to {dialog.name}")
                        await asyncio.sleep(5)   # anti-flood delay between each message
                    except FloodWaitError as e:
                        logger.warning(f"[{phone}] FloodWait {e.seconds}s")
                        log_event(phone, "failed", f"FloodWait {e.seconds}s")
                        await asyncio.sleep(e.seconds)
                    except (UserBannedInChannelError, ChatWriteForbiddenError) as e:
                        log_event(phone, "failed", str(e))
                    except Exception as e:
                        logger.error(f"[{phone}] Send error: {e}")
                        log_event(phone, "failed", str(e))

                log_event(phone, "cycle_done", f"sent={sent}")
                update_account(phone, last_run=datetime.now().isoformat())
                logger.info(f"[{phone}] Cycle done. Sent={sent}. Sleeping {interval}s")
                # Wait the interval before processing next account / next cycle
                await asyncio.sleep(interval)

            except Exception as e:
                logger.error(f"[{phone}] Outer broadcast error: {e}")
                await asyncio.sleep(30)

    logger.info("Broadcast loop stopped")

async def broadcast_account_loop(phone: str):
    """Broadcast loop for a single account."""
    logger.info(f"[{phone}] Per-account broadcast started")

    # Ensure log channel exists before starting
    log_cid, log_url = await ensure_log_channel(phone)

    while phone in active_broadcasts:
        acc = get_account(phone)
        if not acc or not acc[3]:
            logger.info(f"[{phone}] Account inactive or deleted, stopping")
            break
        msg      = acc[4] or get_setting("global_message")
        interval = int(acc[5] or get_setting("global_interval") or 300)
        if not msg:
            logger.warning(f"[{phone}] No message set, waiting 30s")
            await asyncio.sleep(30)
            continue
        try:
            client = await get_client(phone)
            if not await client.is_user_authorized():
                logger.warning(f"[{phone}] Not authorized")
                break
            dialogs = await client.get_dialogs()
            groups  = [d for d in dialogs if is_group(d.entity)]
            logger.info(f"[{phone}] Found {len(groups)} groups")
            sent = 0
            for dialog in groups:
                if phone not in active_broadcasts:
                    break
                try:
                    result = await client.send_message(dialog.entity, msg)
                    sent += 1
                    log_event(phone, "sent", dialog.name)
                    logger.info(f"[{phone}] Sent to {dialog.name}")
                    # Post to log channel
                    if log_cid:
                        group_username = getattr(dialog.entity, 'username', None)
                        await post_to_log_channel(phone, log_cid, dialog.name, result.id, group_username)
                    await asyncio.sleep(5)
                except FloodWaitError as e:
                    logger.warning(f"[{phone}] FloodWait {e.seconds}s")
                    log_event(phone, "failed", f"FloodWait {e.seconds}s")
                    if log_cid:
                        await post_to_log_channel(phone, log_cid, dialog.name, 0, None)
                    await asyncio.sleep(e.seconds)
                except (UserBannedInChannelError, ChatWriteForbiddenError) as e:
                    log_event(phone, "failed", str(e))
                except Exception as e:
                    logger.error(f"[{phone}] Send error: {e}")
                    log_event(phone, "failed", str(e))
            log_event(phone, "cycle_done", f"sent={sent}")
            update_account(phone, last_run=datetime.now().isoformat())
            logger.info(f"[{phone}] Cycle done. Sent={sent}. Sleeping {interval}s")
            await asyncio.sleep(interval)
        except Exception as e:
            logger.error(f"[{phone}] Outer error: {e}")
            await asyncio.sleep(30)
    active_broadcasts.pop(phone, None)
    logger.info(f"[{phone}] Per-account broadcast stopped")

# ── Keyboards ─────────────────────────────────────────────────────────────────
def kb_main():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Dashboard", callback_data="dashboard")
    ],[
        InlineKeyboardButton("🔄 Updates", callback_data="updates"),
        InlineKeyboardButton("💬 Support", callback_data="support")
    ],[
        InlineKeyboardButton("📖 How To Use", callback_data="howto")
    ]])

def kb_dashboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Account",   callback_data="add_account"),
         InlineKeyboardButton("📋 My Accounts",   callback_data="my_accounts")],
        [InlineKeyboardButton("▶️ Start Ads",     callback_data="start_ads"),
         InlineKeyboardButton("⏸ Stop Ads",       callback_data="stop_ads")],
        [InlineKeyboardButton("🗑 Delete Account", callback_data="delete_accounts"),
         InlineKeyboardButton("📈 Analytics",      callback_data="analytics")],
        [InlineKeyboardButton("🔙 Back",           callback_data="back_main")],
    ])

def kb_interval():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("20min 🟢", callback_data="iv_1200"),
        InlineKeyboardButton("5min 🔴",  callback_data="iv_300"),
        InlineKeyboardButton("10min 🟡", callback_data="iv_600"),
    ],[
        InlineKeyboardButton("🔙 Back", callback_data="dashboard"),
    ]])

def kb_back_dashboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="dashboard")]])

# ── Helpers ───────────────────────────────────────────────────────────────────
def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if uid != OWNER_ID:
            await update.effective_message.reply_text("⛔ Unauthorized.")
            return
        return await func(update, ctx)
    return wrapper

async def send_or_edit(update: Update, text: str, reply_markup=None, parse_mode="HTML"):
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

# ── Handlers ──────────────────────────────────────────────────────────────────
@owner_only
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 <b>Welcome to Free Ads Bot</b> — The Future of Telegram Automation\n\n"
        "• Premium Ad Broadcasting\n"
        "• Smart Delays\n"
        "• Multi-Account Support (up to 50)\n"
        "• Per-Account Custom Settings\n\n"
        "For support contact: @YourSupport"
    )
    await send_or_edit(update, text, kb_main())
    return MAIN_MENU

@owner_only
async def dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    accs    = get_accounts()
    msg     = get_setting("global_message") or "Not Set"
    iv      = get_setting("global_interval") or "300"
    status  = get_setting("status") or "paused"
    stat_icon = "▶️ Running" if status == "running" else "⏸ Paused"
    text = (
        f"📊 <b>Ads DASHBOARD</b>\n\n"
        f"• Hosted Accounts: <b>{len(accs)}/{MAX_ACCOUNTS}</b>\n"
        f"• Advertising Status: <b>{stat_icon}</b>\n\n"
        "Choose an action below to continue:"
    )
    await send_or_edit(update, text, kb_dashboard())
    return DASHBOARD

async def add_account_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    accs = get_accounts()
    if len(accs) >= MAX_ACCOUNTS:
        await send_or_edit(update, f"⚠️ Max {MAX_ACCOUNTS} accounts reached.", kb_back_dashboard())
        return DASHBOARD
    await send_or_edit(update,
        "🔐 <b>HOST NEW ACCOUNT</b>\n\nSecure Account Hosting\n\n"
        "Enter your phone number with country code:\n"
        "<i>Example: +1234567890</i>\n\n"
        "🔒 Your data is encrypted and secure",
        kb_back_dashboard()
    )
    return ADD_ACCOUNT_PHONE

async def add_account_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone.startswith("+"):
        await update.message.reply_text("❌ Invalid format. Use +CountryCodeNumber")
        return ADD_ACCOUNT_PHONE
    pending_phones[update.effective_user.id] = phone
    try:
        client = await get_client(phone)
        await client.send_code_request(phone)
        await update.message.reply_text(
            f"✅ OTP sent to <b>{phone}</b>\n\n"
            "Enter the OTP (no spaces needed):\n"
            "<i>Valid for: 5 minutes</i>",
            parse_mode="HTML"
        )
        return ADD_ACCOUNT_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return ADD_ACCOUNT_PHONE

async def add_account_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    otp   = update.message.text.strip()
    uid   = update.effective_user.id
    phone = pending_phones.get(uid)
    if not phone:
        await update.message.reply_text("❌ Session expired. Start again.")
        return DASHBOARD
    try:
        client = await get_client(phone)
        await client.sign_in(phone, otp)
        me = await client.get_me()
        name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        add_account(phone, name)
        log_event(phone, "added")
        await update.message.reply_text(
            f"✅ Account <b>{name}</b> ({phone}) added successfully!",
            parse_mode="HTML", reply_markup=kb_back_dashboard()
        )
        return DASHBOARD
    except SessionPasswordNeededError:
        await update.message.reply_text("🔐 2FA enabled. Enter your Telegram password:")
        ctx.user_data["2fa_phone"] = phone
        return ADD_ACCOUNT_2FA
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        await update.message.reply_text(f"❌ {e}. Try again:")
        return ADD_ACCOUNT_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return DASHBOARD

async def add_account_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    phone    = ctx.user_data.get("2fa_phone")
    try:
        client = await get_client(phone)
        await client.sign_in(password=password)
        me = await client.get_me()
        name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        add_account(phone, name)
        log_event(phone, "added")
        await update.message.reply_text(
            f"✅ Account <b>{name}</b> added (2FA OK)!",
            parse_mode="HTML", reply_markup=kb_back_dashboard()
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 2FA Error: {e}")
    return DASHBOARD

async def my_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    accs = get_accounts()
    if not accs:
        await send_or_edit(update, "📭 No accounts added yet.", kb_back_dashboard())
        return DASHBOARD
    lines = ["📋 <b>My Accounts</b>\n"]
    buttons = []
    for a in accs:
        phone, name, active = a[1], a[2], a[3]
        status = "✅" if active else "❌"
        lines.append(f"{status} <b>{name}</b> | {phone}")
        buttons.append([
            InlineKeyboardButton(f"⚙️ {phone}", callback_data=f"accsettings_{phone}"),
            InlineKeyboardButton("🗑", callback_data=f"delacc_{phone}")
        ])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="dashboard")])
    await send_or_edit(update, "\n".join(lines), InlineKeyboardMarkup(buttons))
    return DASHBOARD

async def account_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    raw = update.callback_query.data
    if raw.startswith("accsettings_"):
        phone = raw.replace("accsettings_", "")
        ctx.user_data["selected_acc"] = phone
    else:
        phone = ctx.user_data.get("selected_acc")
    if not phone:
        await send_or_edit(update, "❌ Session lost. Go back and select the account again.", kb_back_dashboard())
        return DASHBOARD
    acc = get_account(phone)
    if not acc:
        await send_or_edit(update, "❌ Account not found.", kb_back_dashboard())
        return DASHBOARD
    iv      = acc[5] or "global"
    log_url = acc[10] if len(acc) > 10 else None
    log_line = f"\n• Log Channel: <a href='{log_url}'>📋 Open Logs</a>" if log_url else "\n• Log Channel: <i>Created on first Start Ads</i>"
    text = (
        f"⚙️ <b>Settings: {acc[2]}</b>\n<code>{phone}</code>\n\n"
        f"• Custom Message: <b>{'Set ✅' if acc[4] else 'Not Set'}</b>\n"
        f"• Interval: <b>{iv}s</b>\n"
        f"• Auto Reply: <b>{'Set ✅' if acc[6] else 'Not Set'}</b>\n"
        f"• Status: <b>{'Active ✅' if acc[3] else 'Paused ⏸'}</b>"
        f"{log_line}"
    )
    is_running = phone in active_broadcasts
    kb_rows = [
        [InlineKeyboardButton("📝 Custom Message", callback_data="acc_custmsg"),
         InlineKeyboardButton("⏱ Custom Interval", callback_data="acc_custiv")],
        [InlineKeyboardButton("🤖 Auto Reply",     callback_data="acc_autoreply"),
         InlineKeyboardButton("⏸ Toggle Active",   callback_data=f"acc_toggle_{phone}")],
        [InlineKeyboardButton("⏹ Stop Ads" if is_running else "▶️ Start Ads",
                              callback_data=f"acc_stop_{phone}" if is_running else f"acc_start_{phone}")],
    ]
    if log_url:
        kb_rows.append([InlineKeyboardButton("📋 Open Log Channel", url=log_url)])
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="my_accounts")])
    await send_or_edit(update, text, InlineKeyboardMarkup(kb_rows))
    return SELECT_ACCOUNT_SETTINGS

async def acc_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.callback_query.data.replace("acc_toggle_", "")
    ctx.user_data["selected_acc"] = phone
    acc = get_account(phone)
    if not acc:
        await update.callback_query.answer("❌ Account not found.", show_alert=True)
        return SELECT_ACCOUNT_SETTINGS
    new = 0 if acc[3] else 1
    update_account(phone, active=new)
    await update.callback_query.answer(f"Account {'activated ✅' if new else 'paused ⏸'}!", show_alert=True)
    iv      = acc[5] or "global"
    log_url = acc[10] if len(acc) > 10 else None
    log_line = f"\n• Log Channel: <a href='{log_url}'>📋 Open Logs</a>" if log_url else "\n• Log Channel: <i>Created on first Start Ads</i>"
    text = (
        f"⚙️ <b>Settings: {acc[2]}</b>\n<code>{phone}</code>\n\n"
        f"• Custom Message: <b>{'Set ✅' if acc[4] else 'Not Set'}</b>\n"
        f"• Interval: <b>{iv}s</b>\n"
        f"• Auto Reply: <b>{'Set ✅' if acc[6] else 'Not Set'}</b>\n"
        f"• Status: <b>{'Active ✅' if new else 'Paused ⏸'}</b>"
        f"{log_line}"
    )
    is_running = phone in active_broadcasts
    kb_rows = [
        [InlineKeyboardButton("📝 Custom Message", callback_data="acc_custmsg"),
         InlineKeyboardButton("⏱ Custom Interval", callback_data="acc_custiv")],
        [InlineKeyboardButton("🤖 Auto Reply",     callback_data="acc_autoreply"),
         InlineKeyboardButton("⏸ Toggle Active",   callback_data=f"acc_toggle_{phone}")],
        [InlineKeyboardButton("⏹ Stop Ads" if is_running else "▶️ Start Ads",
                              callback_data=f"acc_stop_{phone}" if is_running else f"acc_start_{phone}")],
    ]
    if log_url:
        kb_rows.append([InlineKeyboardButton("📋 Open Log Channel", url=log_url)])
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="my_accounts")])
    await send_or_edit(update, text, InlineKeyboardMarkup(kb_rows))
    return SELECT_ACCOUNT_SETTINGS

async def acc_start_ads(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Start ads for a single account."""
    phone = update.callback_query.data.replace("acc_start_", "")
    ctx.user_data["selected_acc"] = phone
    acc = get_account(phone)
    if not acc:
        await update.callback_query.answer("❌ Account not found.", show_alert=True)
        return SELECT_ACCOUNT_SETTINGS
    msg = acc[4] or get_setting("global_message")
    if not msg:
        await update.callback_query.answer("❌ Set a message for this account first!", show_alert=True)
        return SELECT_ACCOUNT_SETTINGS
    if phone in active_broadcasts:
        await update.callback_query.answer("⚠️ Already running!", show_alert=True)
        return SELECT_ACCOUNT_SETTINGS
    loop = asyncio.get_event_loop()
    active_broadcasts[phone] = loop.create_task(broadcast_account_loop(phone))
    await update.callback_query.answer(f"▶️ Ads started for {phone}!", show_alert=True)
    # Refresh the settings page to show Stop button
    return await account_settings(update, ctx)

async def acc_stop_ads(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Stop ads for a single account."""
    phone = update.callback_query.data.replace("acc_stop_", "")
    ctx.user_data["selected_acc"] = phone
    if phone in active_broadcasts:
        active_broadcasts[phone].cancel()
        active_broadcasts.pop(phone, None)
        await update.callback_query.answer(f"⏹ Ads stopped for {phone}!", show_alert=True)
    else:
        await update.callback_query.answer("Already stopped.", show_alert=True)
    return await account_settings(update, ctx)

async def acc_custmsg_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await send_or_edit(update,
        "📝 Send the custom ad message for this account:\n<i>(or /skip to use global)</i>",
        kb_back_dashboard()
    )
    return ACCOUNT_CUSTOM_MSG

async def acc_custmsg_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = ctx.user_data.get("selected_acc")
    msg   = update.message.text.strip()
    if msg != "/skip":
        update_account(phone, ad_message=msg)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Account Settings", callback_data="acc_back_settings")]])
    await update.message.reply_text(
        "✅ Custom message set!" if msg != "/skip" else "✅ Using global message.",
        reply_markup=kb
    )
    return SELECT_ACCOUNT_SETTINGS

async def acc_custiv_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await send_or_edit(update,
        "⏱ Send custom interval in seconds for this account:\n<i>Example: 600</i>",
        kb_back_dashboard()
    )
    return ACCOUNT_CUSTOM_INTERVAL

async def acc_custiv_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = ctx.user_data.get("selected_acc")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Account Settings", callback_data="acc_back_settings")]])
    try:
        iv = int(update.message.text.strip())
        update_account(phone, interval=iv)
        await update.message.reply_text(f"✅ Interval set to {iv}s", reply_markup=kb)
    except ValueError:
        await update.message.reply_text("❌ Invalid number. Send a number like 600", reply_markup=kb)
    return SELECT_ACCOUNT_SETTINGS

async def set_ad_message_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    cur = get_setting("global_message") or "No message set yet."
    await send_or_edit(update,
        f"📝 <b>SET YOUR AD MESSAGE</b>\n\n"
        f"Current Ad Message:\n<i>{cur[:100]}</i>\n\n"
        "Tips for effective ads:\n"
        "• Keep it concise and engaging\n"
        "• Use premium emojis for flair\n"
        "• Include clear call-to-action\n"
        "• Avoid excessive caps or spam words\n\n"
        "Send your ad message now:",
        kb_back_dashboard()
    )
    return SET_AD_MESSAGE

async def set_ad_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip()
    set_setting("global_message", msg)
    await update.message.reply_text("✅ Global ad message saved!", reply_markup=kb_back_dashboard())
    return DASHBOARD

async def set_interval_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    cur = get_setting("global_interval") or "300"
    await send_or_edit(update,
        f"⏱ <b>SET BROADCAST CYCLE INTERVAL</b>\n\n"
        f"Current Interval: <b>{cur} seconds</b>\n\n"
        "Recommended Intervals:\n"
        "• 300s - Aggressive (5 min) 🔴\n"
        "• 600s - Safe & Balanced (10 min) 🟡\n"
        "• 1200s - Conservative (20 min) 🟢\n\n"
        "⚠️ Using short intervals can get your account banned.\n\n"
        "Or send a custom number in seconds:",
        kb_interval()
    )
    return SET_INTERVAL

async def set_interval_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    iv = int(update.callback_query.data.replace("iv_", ""))
    set_setting("global_interval", iv)
    await update.callback_query.answer(f"✅ Interval set to {iv}s", show_alert=True)
    return await dashboard(update, ctx)

async def set_interval_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        iv = int(update.message.text.strip())
        set_setting("global_interval", iv)
        await update.message.reply_text(f"✅ Global interval set to {iv}s", reply_markup=kb_back_dashboard())
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number.")
    return DASHBOARD

async def start_ads(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global broadcasting, broadcast_task
    await update.callback_query.answer()
    if broadcasting:
        await update.callback_query.answer("⚠️ Already running!", show_alert=True)
        return DASHBOARD
    accs = get_accounts()
    if not accs:
        await update.callback_query.answer("❌ Add at least one account first!", show_alert=True)
        return DASHBOARD
    msg = get_setting("global_message")
    # Check if at least one account has a message (global or custom)
    has_msg = bool(msg) or any(a[4] for a in accs)
    if not has_msg:
        await update.callback_query.answer("❌ Set an ad message first!", show_alert=True)
        return DASHBOARD
    broadcasting = True
    set_setting("status", "running")
    loop = asyncio.get_event_loop()
    broadcast_task = loop.create_task(broadcast_loop(ctx.application))
    active_count = sum(1 for a in accs if a[3])
    await send_or_edit(update,
        f"▶️ <b>Ads Started!</b>\n\n"
        f"• Active accounts: <b>{active_count}</b>\n"
        f"• Interval: <b>{get_setting('global_interval') or 300}s</b>\n"
        f"• Message: <b>Set ✅</b>\n\n"
        "Broadcasting to all groups...",
        kb_back_dashboard()
    )
    return DASHBOARD

async def stop_ads(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global broadcasting, broadcast_task
    await update.callback_query.answer()
    broadcasting = False
    set_setting("status", "paused")
    if broadcast_task:
        broadcast_task.cancel()
        broadcast_task = None
    await send_or_edit(update, "⏸ <b>Ads Stopped.</b>", kb_back_dashboard())
    return DASHBOARD

async def delete_accounts_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    accs = get_accounts()
    if not accs:
        await send_or_edit(update, "No accounts to delete.", kb_back_dashboard())
        return DASHBOARD
    buttons = [[InlineKeyboardButton(f"🗑 {a[2]} ({a[1]})", callback_data=f"delacc_{a[1]}")] for a in accs]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="dashboard")])
    await send_or_edit(update, "🗑 <b>Select account to delete:</b>", InlineKeyboardMarkup(buttons))
    return DASHBOARD

async def delete_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    phone = update.callback_query.data.replace("delacc_", "")
    delete_account_db(phone)
    if phone in clients:
        await clients[phone].disconnect()
        del clients[phone]
    sess = session_path(phone) + ".session"
    if os.path.exists(sess):
        os.remove(sess)
    log_event(phone, "deleted")
    await update.callback_query.answer(f"✅ {phone} deleted.", show_alert=True)
    return await dashboard(update, ctx)

async def analytics_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    a = get_analytics()
    rate = round(a["sent"] / max(a["sent"] + a["failed"], 1) * 100)
    text = (
        "📈 <b>ANALYTICS</b>\n\n"
        f"• Broadcast Cycles Completed: <b>{a['cycles']}</b>\n"
        f"• Messages Sent: <b>{a['sent']}</b>\n"
        f"• Failed Sends: <b>{a['failed']}</b>\n"
        f"• Active Accounts: <b>{a['active']}</b>\n"
        f"• Success Rate: <b>{rate}%</b>"
    )
    await send_or_edit(update, text, kb_back_dashboard())
    return DASHBOARD

async def auto_reply_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await send_or_edit(update,
        "🤖 <b>AUTO REPLY</b>\n\nSend the message to auto-reply when someone DMs your hosted accounts:\n"
        "<i>Or /skip to disable</i>",
        kb_back_dashboard()
    )
    return AUTO_REPLY_MSG

async def auto_reply_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip()
    phone = ctx.user_data.get("selected_acc")
    if phone:
        # Per-account auto reply
        update_account(phone, auto_reply=None if msg == "/skip" else msg)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Account Settings", callback_data="acc_back_settings")]])
        await update.message.reply_text(
            "✅ Auto reply set for this account!" if msg != "/skip" else "✅ Auto reply disabled.",
            reply_markup=kb
        )
        return SELECT_ACCOUNT_SETTINGS
    else:
        # Global auto reply
        for acc in get_accounts():
            update_account(acc[1], auto_reply=None if msg == "/skip" else msg)
        await update.message.reply_text(
            "✅ Auto reply set for all accounts!" if msg != "/skip" else "✅ Auto reply disabled.",
            reply_markup=kb_back_dashboard()
        )
        return DASHBOARD

async def howto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await send_or_edit(update,
        "📖 <b>HOW TO USE</b>\n\n"
        "1️⃣ Go to Dashboard → Add Account\n"
        "2️⃣ Enter your phone with country code (+91...)\n"
        "3️⃣ Enter the OTP sent to your Telegram\n"
        "4️⃣ Set your Ad Message in Dashboard\n"
        "5️⃣ Set your time interval\n"
        "6️⃣ Press ▶️ Start Ads — done!\n\n"
        "⚙️ <b>Per-account settings:</b>\n"
        "Go to My Accounts → tap ⚙️ next to any account to set custom message/interval\n\n"
        "⚠️ <b>Tips:</b>\n"
        "• Use 10min+ intervals to stay safe\n"
        "• Don't host too many accounts on same IP\n"
        "• Keep messages relevant to groups",
        kb_back_dashboard()
    )
    return DASHBOARD

async def fallback_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data if update.callback_query else ""
    await update.callback_query.answer()
    if data == "back_main":
        return await start(update, ctx)
    if data == "dashboard":
        return await dashboard(update, ctx)
    if data == "my_accounts":
        return await my_accounts(update, ctx)
    if data == "updates":
        await send_or_edit(update, "🔄 No new updates.", kb_back_dashboard())
    if data == "support":
        await send_or_edit(update, "💬 Support: @YourSupportChannel", kb_back_dashboard())
    return DASHBOARD

# ── Build App ─────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [CallbackQueryHandler(dashboard, pattern="^dashboard$"),
                        CallbackQueryHandler(fallback_cb)],
            DASHBOARD: [
                CallbackQueryHandler(add_account_start,    pattern="^add_account$"),
                CallbackQueryHandler(my_accounts,          pattern="^my_accounts$"),
                CallbackQueryHandler(set_ad_message_start, pattern="^set_ad_message$"),
                CallbackQueryHandler(set_interval_menu,    pattern="^set_interval$"),
                CallbackQueryHandler(start_ads,            pattern="^start_ads$"),
                CallbackQueryHandler(stop_ads,             pattern="^stop_ads$"),
                CallbackQueryHandler(delete_accounts_menu, pattern="^delete_accounts$"),
                CallbackQueryHandler(analytics_menu,       pattern="^analytics$"),
                CallbackQueryHandler(auto_reply_start,     pattern="^auto_reply$"),
                CallbackQueryHandler(account_settings,     pattern="^accsettings_"),
                CallbackQueryHandler(delete_account,       pattern="^delacc_"),
                CallbackQueryHandler(howto,                pattern="^howto$"),
                CallbackQueryHandler(fallback_cb),
            ],
            ADD_ACCOUNT_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_phone),
                CallbackQueryHandler(dashboard, pattern="^dashboard$"),
            ],
            ADD_ACCOUNT_OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_otp),
            ],
            ADD_ACCOUNT_2FA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_2fa),
            ],
            SET_AD_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_ad_message),
                CallbackQueryHandler(dashboard, pattern="^dashboard$"),
            ],
            SET_INTERVAL: [
                CallbackQueryHandler(set_interval_btn, pattern="^iv_"),
                CallbackQueryHandler(dashboard, pattern="^dashboard$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_interval_text),
            ],
            SELECT_ACCOUNT_SETTINGS: [
                CallbackQueryHandler(acc_custmsg_start,  pattern="^acc_custmsg$"),
                CallbackQueryHandler(acc_custiv_start,   pattern="^acc_custiv$"),
                CallbackQueryHandler(auto_reply_start,   pattern="^acc_autoreply$"),
                CallbackQueryHandler(acc_toggle,         pattern="^acc_toggle_"),
                CallbackQueryHandler(acc_start_ads,      pattern="^acc_start_"),
                CallbackQueryHandler(acc_stop_ads,       pattern="^acc_stop_"),
                CallbackQueryHandler(my_accounts,        pattern="^my_accounts$"),
                CallbackQueryHandler(account_settings,   pattern="^acc_back_settings$"),
                CallbackQueryHandler(dashboard,          pattern="^dashboard$"),
            ],
            ACCOUNT_CUSTOM_MSG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, acc_custmsg_set),
                CallbackQueryHandler(account_settings, pattern="^acc_back_settings$"),
            ],
            ACCOUNT_CUSTOM_INTERVAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, acc_custiv_set),
                CallbackQueryHandler(account_settings, pattern="^acc_back_settings$"),
            ],
            AUTO_REPLY_MSG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, auto_reply_set),
                CallbackQueryHandler(account_settings, pattern="^acc_back_settings$"),
                CallbackQueryHandler(dashboard, pattern="^dashboard$"),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        per_user=True,
        allow_reentry=True,
    )

    app.add_handler(conv)
    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
