import os
import re
import json
import secrets
import string
import logging
from datetime import datetime, timezone

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

# ---------------- Configuration (set these in Render Environment) ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
FIREBASE_DB_URL = os.getenv(
    "FIREBASE_DB_URL",
    "https://kanav-3c195-default-rtdb.firebaseio.com"
).rstrip("/")
# Optional Firebase Realtime Database auth token. A Firebase Web API key is NOT
# a database write credential; secure database rules/authentication are required.
FIREBASE_AUTH_TOKEN = os.getenv("FIREBASE_AUTH_TOKEN", "").strip()
REFERRALS_PER_KEY = int(os.getenv("REFERRALS_PER_KEY", "3"))
KEY_PREFIX = os.getenv("KEY_PREFIX", "RAJIV").strip() or "RAJIV"
KEY_LENGTH = max(8, min(int(os.getenv("KEY_LENGTH", "16")), 32))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "25"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("referral-key-bot")

# ---------------- Firebase REST helpers ----------------
def firebase_url(path: str) -> str:
    url = f"{FIREBASE_DB_URL}/{path.strip('/')}.json"
    if FIREBASE_AUTH_TOKEN:
        url += "?auth=" + requests.utils.quote(FIREBASE_AUTH_TOKEN, safe="")
    return url

def fb_get(path: str):
    r = requests.get(firebase_url(path), timeout=15)
    r.raise_for_status()
    return r.json()

def fb_put(path: str, value):
    r = requests.put(
        firebase_url(path),
        json=value,
        timeout=15,
        headers={"Content-Type": "application/json"}
    )
    r.raise_for_status()
    return r.json()

def fb_patch(path: str, value):
    r = requests.patch(
        firebase_url(path),
        json=value,
        timeout=15,
        headers={"Content-Type": "application/json"}
    )
    r.raise_for_status()
    return r.json()

def safe_fb_get(path: str, default=None):
    try:
        result = fb_get(path)
        return default if result is None else result
    except Exception:
        log.exception("Firebase read failed at %s", path)
        return default

def user_path(user_id: int) -> str:
    return f"referral_key_bot/users/{user_id}"

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def make_key() -> str:
    alphabet = string.ascii_uppercase + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(KEY_LENGTH))
    return f"{KEY_PREFIX}-{body}"

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def load_user(user_id: int, username=None, first_name=None):
    user = safe_fb_get(user_path(user_id), {}) or {}
    if not isinstance(user, dict):
        user = {}
    changed = {}
    if username is not None:
        changed["username"] = username
    if first_name is not None:
        changed["first_name"] = first_name
    changed["last_seen"] = utc_now()
    if "referral_count" not in user:
        changed["referral_count"] = 0
    if "keys_earned" not in user:
        changed["keys_earned"] = 0
    if "referrals" not in user:
        changed["referrals"] = {}
    if changed:
        try:
            fb_patch(user_path(user_id), changed)
        except Exception:
            log.exception("Could not update user record")
        user.update(changed)
    return user

def record_referral(referrer_id: int, new_user_id: int) -> bool:
    """Record one unique referral. Returns True if it was newly recorded."""
    if referrer_id == new_user_id:
        return False
    ref_path = f"referral_key_bot/referrals/{new_user_id}"
    existing = safe_fb_get(ref_path)
    if existing:
        return False
    # The user-ID keyed record prevents the same invite from counting twice.
    fb_put(ref_path, {"referrer_id": referrer_id, "created_at": utc_now()})
    ref_user = safe_fb_get(user_path(referrer_id), {}) or {}
    if not isinstance(ref_user, dict):
        ref_user = {}
    count = int(ref_user.get("referral_count", 0)) + 1
    earned = int(ref_user.get("keys_earned", 0))
    remainder = int(ref_user.get("referral_remainder", 0)) + 1
    patch = {"referral_count": count, "referral_remainder": remainder}
    if remainder >= REFERRALS_PER_KEY:
        remainder -= REFERRALS_PER_KEY
        key = make_key()
        earned += 1
        patch["referral_remainder"] = remainder
        patch["keys_earned"] = earned
        key_record = {
            "key": key,
            "owner_telegram_id": referrer_id,
            "status": "unused",
            "created_at": utc_now(),
            "source": "referral_reward"
        }
        fb_put(f"referral_key_bot/generated_keys/{key}", key_record)
        fb_put(f"referral_key_bot/user_keys/{referrer_id}/{key}", key_record)
    fb_patch(user_path(referrer_id), patch)
    return True

def get_top_users(limit=10):
    users = safe_fb_get("referral_key_bot/users", {}) or {}
    if not isinstance(users, dict):
        return []
    rows = []
    for uid, info in users.items():
        if not isinstance(info, dict):
            continue
        rows.append((int(info.get("referral_count", 0)), int(uid),
                     info.get("first_name") or "User",
                     info.get("username") or ""))
    rows.sort(key=lambda x: (-x[0], x[1]))
    return rows[:limit]

# ---------------- User-facing commands ----------------
async def user_blocked_by_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user and not is_admin(user.id):
        paused = bool(safe_fb_get("referral_key_bot/settings/paused", False))
        if paused:
            if update.callback_query:
                await update.callback_query.answer("Bot is temporarily paused.", show_alert=True)
            elif update.message:
                await update.message.reply_text("⏸ Bot is temporarily paused by the admin.")
            return True
    return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await user_blocked_by_pause(update, context):
        return
    u = update.effective_user
    if not u or not update.message:
        return
    existing_user = safe_fb_get(user_path(u.id))
    is_first_start = not isinstance(existing_user, dict)
    load_user(u.id, u.username, u.first_name)
    if is_first_start and context.args:
        arg = context.args[0]
        if arg.startswith("ref_") and arg[4:].isdigit():
            referrer_id = int(arg[4:])
            if referrer_id != u.id:
                try:
                    if record_referral(referrer_id, u.id):
                        ref_user = safe_fb_get(user_path(referrer_id), {}) or {}
                        if int(ref_user.get("referral_count", 0)) % REFERRALS_PER_KEY == 0:
                            await context.bot.send_message(
                                chat_id=referrer_id,
                                text=f"🎉 Congratulations! You reached {REFERRALS_PER_KEY} new referrals and earned a key. Use /mykeys to view it."
                            )
                except Exception:
                    log.exception("Referral processing failed")
                    await update.message.reply_text(
                        "Welcome! Referral was not recorded because the database could not be updated. Please try again later."
                    )
    keyboard = [
        [InlineKeyboardButton("🔗 My Referral Link", callback_data="myref")],
        [InlineKeyboardButton("🔑 My Keys", callback_data="mykeys"),
         InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")],
        [InlineKeyboardButton("📊 My Stats", callback_data="stats")]
    ]
    if is_admin(u.id):
        keyboard.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin")])
    await update.message.reply_text(
        f"👋 Welcome, {u.first_name or 'there'}!\n\n"
        f"Invite friends using your referral link. For every {REFERRALS_PER_KEY} valid new users, "
        "you earn one unique key.\n\n"
        "Commands: /referral /mykeys /stats /leaderboard /help",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await user_blocked_by_pause(update, context):
        return
    await update.message.reply_text(
        "📘 Help\n"
        "/start — Open menu\n"
        "/referral — Get your personal referral link\n"
        "/mykeys — Show your generated keys\n"
        "/stats — Referral stats\n"
        "/leaderboard — Top inviters\n\n"
        f"🎁 Reward: {REFERRALS_PER_KEY} unique referrals = 1 key."
    )

async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await user_blocked_by_pause(update, context):
        return
    u = update.effective_user
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{u.id}"
    user = load_user(u.id, u.username, u.first_name)
    count = int(user.get("referral_count", 0))
    remainder = int(user.get("referral_remainder", count % REFERRALS_PER_KEY))
    await update.message.reply_text(
        f"🔗 Your referral link:\n{link}\n\n"
        f"👥 Successful referrals: {count}\n"
        f"🔑 Keys earned: {int(user.get('keys_earned', 0))}\n"
        f"👣 Referrals toward next key: {remainder}/{REFERRALS_PER_KEY}\n\n"
        "Only users who start the bot for the first time count."
    )

async def keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await user_blocked_by_pause(update, context):
        return
    uid = update.effective_user.id
    keys = safe_fb_get(f"referral_key_bot/user_keys/{uid}", {}) or {}
    if not isinstance(keys, dict) or not keys:
        await update.message.reply_text("You have no keys yet. Invite friends with /referral.")
        return
    lines = ["🔑 Your keys:"]
    for key, record in list(keys.items())[-50:]:
        status = record.get("status", "unused") if isinstance(record, dict) else "unused"
        lines.append(f"• <code>{key}</code> — {status}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await user_blocked_by_pause(update, context):
        return
    u = update.effective_user
    user = load_user(u.id, u.username, u.first_name)
    count = int(user.get("referral_count", 0))
    remainder = int(user.get("referral_remainder", count % REFERRALS_PER_KEY))
    await update.message.reply_text(
        f"📊 Your stats\n👥 Referrals: {count}\n🔑 Keys earned: {user.get('keys_earned', 0)}\n"
        f"🎯 Progress: {remainder}/{REFERRALS_PER_KEY} toward next key"
    )

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await user_blocked_by_pause(update, context):
        return
    rows = get_top_users(10)
    if not rows:
        await update.message.reply_text("Leaderboard is empty so far.")
        return
    lines = ["🏆 Referral Leaderboard"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (count, uid, name, username) in enumerate(rows, 1):
        label = f"@{username}" if username else name
        lines.append(f"{medals[i-1] if i <= 3 else str(i) + '.'} {label} — {count}")
    await update.message.reply_text("\n".join(lines))

# ---------------- Admin panel ----------------
def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📈 Bot Stats", callback_data="admin_stats"),
         InlineKeyboardButton("🔑 Generate Key", callback_data="admin_genkey")],
        [InlineKeyboardButton("⏸ Pause/Resume Bot", callback_data="admin_toggle")],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")]
    ])

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    await update.message.reply_text("🛠 Admin Panel", reply_markup=admin_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id
    if not is_admin(uid) and await user_blocked_by_pause(update, context):
        return
    if data == "home":
        await query.edit_message_text("Use /start to open the main menu.")
        return
    if data == "myref":
        me = await context.bot.get_me()
        user = load_user(uid, query.from_user.username, query.from_user.first_name)
        count = int(user.get("referral_count", 0))
        await query.edit_message_text(
            f"🔗 https://t.me/{me.username}?start=ref_{uid}\n\n"
            f"Referrals: {count}\nKeys earned: {user.get('keys_earned', 0)}"
        )
    elif data == "mykeys":
        keys = safe_fb_get(f"referral_key_bot/user_keys/{uid}", {}) or {}
        if not keys:
            await query.edit_message_text("No keys yet. Invite friends with /referral.")
        else:
            lines = ["🔑 Your keys:"]
            for key, rec in list(keys.items())[-30:]:
                status = rec.get("status", "unused") if isinstance(rec, dict) else "unused"
                lines.append(f"• <code>{key}</code> — {status}")
            await query.edit_message_text("\n".join(lines), parse_mode="HTML")
    elif data == "leaderboard":
        rows = get_top_users(10)
        lines = ["🏆 Referral Leaderboard"]
        for i, (count, _, name, username) in enumerate(rows, 1):
            lines.append(f"{i}. {'@' + username if username else name} — {count}")
        await query.edit_message_text("\n".join(lines) if rows else "Leaderboard is empty.")
    elif data == "stats":
        user = load_user(uid, query.from_user.username, query.from_user.first_name)
        count = int(user.get("referral_count", 0))
        rem = int(user.get("referral_remainder", count % REFERRALS_PER_KEY))
        await query.edit_message_text(
            f"📊 Referrals: {count}\n🔑 Keys earned: {user.get('keys_earned', 0)}\n"
            f"🎯 Progress: {rem}/{REFERRALS_PER_KEY}"
        )
    elif data == "admin":
        if not is_admin(uid):
            await query.edit_message_text("⛔ Admin only.")
        else:
            await query.edit_message_text("🛠 Admin Panel", reply_markup=admin_keyboard())
    elif data.startswith("admin_"):
        if not is_admin(uid):
            await query.edit_message_text("⛔ Admin only.")
            return
        if data == "admin_broadcast":
            context.user_data["awaiting_broadcast"] = True
            await query.edit_message_text("Send the broadcast message now. Use /cancel to cancel.")
        elif data == "admin_stats":
            users = safe_fb_get("referral_key_bot/users", {}) or {}
            keys = safe_fb_get("referral_key_bot/generated_keys", {}) or {}
            await query.edit_message_text(
                f"📈 Bot Stats\nUsers: {len(users) if isinstance(users, dict) else 0}\n"
                f"Generated keys: {len(keys) if isinstance(keys, dict) else 0}\n"
                f"Admins configured: {len(ADMIN_IDS)}"
            )
        elif data == "admin_genkey":
            key = make_key()
            record = {"key": key, "owner_telegram_id": uid, "status": "unused",
                      "created_at": utc_now(), "source": "admin"}
            try:
                fb_put(f"referral_key_bot/generated_keys/{key}", record)
                fb_put(f"referral_key_bot/user_keys/{uid}/{key}", record)
                await query.edit_message_text(f"✅ Key generated:\n<code>{key}</code>", parse_mode="HTML",
                                              reply_markup=admin_keyboard())
            except Exception:
                log.exception("Admin key generation failed")
                await query.edit_message_text("Firebase error. Check database rules/auth token.")
        elif data == "admin_toggle":
            current = bool(safe_fb_get("referral_key_bot/settings/paused", False))
            try:
                fb_put("referral_key_bot/settings/paused", not current)
                await query.edit_message_text(
                    f"Bot is now {'⏸ PAUSED' if not current else '▶️ ACTIVE'}.",
                    reply_markup=admin_keyboard()
                )
            except Exception:
                await query.edit_message_text("Could not update Firebase settings.")
    else:
        await query.edit_message_text("Unknown action. Use /start.")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_broadcast", None)
    await update.message.reply_text("Cancelled.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id
    if not is_admin(uid) or not context.user_data.get("awaiting_broadcast"):
        return
    text = update.message.text or ""
    context.user_data.pop("awaiting_broadcast", None)
    users = safe_fb_get("referral_key_bot/users", {}) or {}
    sent = failed = 0
    if isinstance(users, dict):
        await update.message.reply_text(f"Broadcast started to {len(users)} users…")
        for target in users.keys():
            try:
                await context.bot.send_message(chat_id=int(target), text=text)
                sent += 1
            except Exception:
                failed += 1
    await update.message.reply_text(f"📣 Broadcast complete.\nSent: {sent}\nFailed: {failed}")

# ---------------- Startup ----------------
async def post_init(app: Application):
    me = await app.bot.get_me()
    log.info("Bot started as @%s", me.username)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled Telegram error", exc_info=context.error)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty. Admin panel will be unavailable.")
    if not FIREBASE_DB_URL.startswith("https://"):
        raise RuntimeError("FIREBASE_DB_URL must be an HTTPS Realtime Database URL.")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("referral", referral_command))
    app.add_handler(CommandHandler("mykeys", keys_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
