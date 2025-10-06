import os
import logging
import asyncio
import time
from datetime import datetime, timedelta
from collections import defaultdict
from fastapi import FastAPI
from pymongo import MongoClient
from bson import ObjectId
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)
import aiohttp
from keepAlive import keep_alive


# --- Load environment variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
SUPERADMIN_ID = int(os.getenv("SUPERADMIN_ID", "0"))
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "placementreminderbot")
SERVER_URL = os.getenv("SERVER_URL", "http://0.0.0.0:5000")  # Fallback to local URL

if not all([BOT_TOKEN, CHANNEL_ID, SUPERADMIN_ID, MONGO_URI]):
    raise EnvironmentError("❌ Missing one or more required environment variables.")

# --- Setup ---
logging.basicConfig(level=logging.INFO)
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
admins = db["admins"]
reminders = db["reminders"]
blocked = db["blocked_users"]

scheduler = AsyncIOScheduler()
scheduler.start()

# --- Ensure superadmin exists ---
if not admins.find_one({"role": "superadmin"}):
    admins.insert_one({"user_id": SUPERADMIN_ID, "role": "superadmin"})
    logging.info("✅ Superadmin inserted into DB")

# --- Helpers ---
def is_superadmin(user_id: int) -> bool:
    return admins.find_one({"user_id": user_id, "role": "superadmin"}) is not None

def is_admin_or_superadmin(user_id: int) -> bool:
    return admins.find_one({"user_id": user_id}) is not None

async def send_reminder(context: ContextTypes.DEFAULT_TYPE, message: str):
    await context.bot.send_message(chat_id=CHANNEL_ID, text=message)

def _get_intervals():
    return [
        (timedelta(hours=2), "⏰ Reminder in 2 hours:"),
        (timedelta(hours=1), "⏰ Reminder in 1 hour:"),
        (timedelta(minutes=30), "⏰ Reminder in 30 minutes:"),
        (timedelta(minutes=15), "⏰ Reminder in 15 minutes:"),
        (timedelta(), "🔔 It's time!"),
    ]

def schedule_reminder_jobs(context: ContextTypes.DEFAULT_TYPE, reminder_id: str, reminder_time: datetime, message: str):
    intervals = _get_intervals()
    now = datetime.now()
    for i, (offset, prefix) in enumerate(intervals):
        run_time = reminder_time - offset
        if run_time > now:
            job_id = f"{reminder_id}_{i}"
            try:
                scheduler.remove_job(job_id)
            except Exception:
                pass
            scheduler.add_job(
                send_reminder,
                "date",
                run_date=run_time,
                args=[context, f"{prefix} {message}"],
                id=job_id
            )

def remove_reminder_jobs(reminder_id: str):
    for job in scheduler.get_jobs():
        if job.id.startswith(f"{reminder_id}_"):
            try:
                scheduler.remove_job(job.id)
            except Exception:
                pass

# --- Spam Protection ---
user_requests = defaultdict(list)
REQUEST_LIMIT = 5
TIME_WINDOW = 10

def is_blocked(user_id: int) -> bool:
    return blocked.find_one({"user_id": user_id}) is not None

def block_user(user_id: int, reason="Spam detected"):
    blocked.update_one(
        {"user_id": user_id},
        {"$set": {"reason": reason, "blocked_at": time.time()}},
        upsert=True
    )

def unblock_user(user_id: int):
    blocked.delete_one({"user_id": user_id})

def rate_limit(user_id: int) -> bool:
    now = time.time()
    user_requests[user_id] = [t for t in user_requests[user_id] if now - t < TIME_WINDOW]
    user_requests[user_id].append(now)
    if len(user_requests[user_id]) > REQUEST_LIMIT:
        block_user(user_id)
        return False
    return True

async def check_spam(update: Update) -> bool:
    user_id = update.effective_user.id
    if is_admin_or_superadmin(user_id):
        return True
    if is_blocked(user_id):
        await update.message.reply_text("⛔ You are blocked for spamming. Contact the superadmin to be unblocked.")
        return False
    if not rate_limit(user_id):
        await update.message.reply_text("⛔ You have been blocked for spamming. Contact the superadmin to be unblocked.")
        return False
    return True

# --- Pending deletions ---
pending_deletes = {}

# --- Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_spam(update): return
    await update.message.reply_text("👋 Hi! I'm your reminder bot.\nUse /remind to set reminders.")

async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_spam(update): return
    if not is_superadmin(update.effective_user.id):
        return await update.message.reply_text("❌ Only superadmin can add admins.")

    try:
        user_id = int(context.args[0])
        if not admins.find_one({"user_id": user_id}):
            admins.insert_one({"user_id": user_id, "role": "admin"})
            await update.message.reply_text(f"✅ Added {user_id} as admin.")
        else:
            await update.message.reply_text("⚠️ That user is already an admin.")
    except Exception as e:
        await update.message.reply_text(f"Usage: /addadmin <user_id>\nError: {e}")

async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_spam(update): return
    if not is_superadmin(update.effective_user.id):
        return await update.message.reply_text("❌ Only superadmin can remove admins.")

    try:
        user_id = int(context.args[0])
        result = admins.delete_one({"user_id": user_id, "role": "admin"})
        if result.deleted_count > 0:
            await update.message.reply_text(f"✅ Removed {user_id} from admins.")
        else:
            await update.message.reply_text("⚠️ User is not an admin.")
    except Exception as e:
        await update.message.reply_text(f"Usage: /removeadmin <user_id>\nError: {e}")

async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_spam(update): return
    if not is_superadmin(update.effective_user.id):
        return await update.message.reply_text("❌ Only superadmin can list admins.")

    all_admins = [f"{a['user_id']} ({a['role']})" for a in admins.find()]
    await update.message.reply_text("👮 Admins:\n" + "\n".join(all_admins))

async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_spam(update): return
    if not is_admin_or_superadmin(update.effective_user.id):
        return await update.message.reply_text("❌ You are not an admin.")

    try:
        dt_str = context.args[0] + " " + context.args[1]
        reminder_time = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        message = " ".join(context.args[2:])

        if not message:
            return await update.message.reply_text("⚠️ Reminder message cannot be empty.")

        res = reminders.insert_one({
            "time": reminder_time,
            "message": message,
            "created_by": update.effective_user.id,
            "created_at": datetime.utcnow()
        })
        rid = str(res.inserted_id)

        schedule_reminder_jobs(context, rid, reminder_time, message)

        await update.message.reply_text(
            f"✅ Reminder set (ID: {rid}) for {reminder_time}:\n{message}\n\n"
            "The bot will notify at 2h, 1h, 30m, 15m before, and on time."
        )

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=f"📌 A new reminder has been set for {reminder_time}:\n{message}"
        )

        try:
            await context.bot.send_message(
                chat_id=SUPERADMIN_ID,
                text=f"🔔 New reminder created by {update.effective_user.id} (ID: {rid}):\n{reminder_time}\n{message}"
            )
        except Exception:
            logging.exception("Failed to notify superadmin about new reminder.")

    except Exception as e:
        await update.message.reply_text(f"Usage: /remind YYYY-MM-DD HH:MM message\nError: {e}")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_spam(update): return
    if not is_superadmin(update.effective_user.id):
        return await update.message.reply_text("❌ Only superadmin can broadcast messages.")

    message = " ".join(context.args)
    if not message:
        return await update.message.reply_text("⚠️ Usage: /broadcast <message>")

    await context.bot.send_message(chat_id=CHANNEL_ID, text=f"📢 {message}")
    await update.message.reply_text("✅ Message broadcasted to the channel.")

async def list_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_spam(update): return
    if not is_superadmin(update.effective_user.id):
        return await update.message.reply_text("❌ Only superadmin can list blocked users.")

    users = list(blocked.find())
    if len(users) == 0:
        return await update.message.reply_text("✅ No users are currently blocked.")

    lines = []
    for u in users:
        blocked_time = datetime.fromtimestamp(u.get("blocked_at", 0)).strftime("%Y-%m-%d %H:%M:%S")
        reason = u.get("reason", "No reason")
        lines.append(f"🚫 {u['user_id']} | Reason: {reason} | Blocked at: {blocked_time}")

    await update.message.reply_text("🔒 Blocked Users:\n" + "\n".join(lines))

async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_spam(update): return
    if not is_superadmin(update.effective_user.id):
        return await update.message.reply_text("❌ Only superadmin can unblock users.")

    try:
        user_id = int(context.args[0])
        if is_blocked(user_id):
            unblock_user(user_id)
            await update.message.reply_text(f"✅ User {user_id} has been unblocked.")
        else:
            await update.message.reply_text("⚠️ That user is not blocked.")
    except Exception as e:
        await update.message.reply_text(f"Usage: /unblock <user_id>\nError: {e}")

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_spam(update): return

    all_reminders = list(reminders.find().sort("time", 1))
    if not all_reminders:
        return await update.message.reply_text("📭 No reminders found.")

    lines = []
    for r in all_reminders:
        rid = str(r["_id"])
        created_by = r.get("created_by", "unknown")
        time_str = r["time"].strftime("%Y-%m-%d %H:%M")
        lines.append(f"🆔 {rid}\n⏰ {time_str}\n📌 {r['message']}\n👤 {created_by}\n---")

    msg = "\n".join(lines)
    if len(msg) < 3900:
        await update.message.reply_text(msg)
    else:
        chunk = []
        cur_len = 0
        for line in lines:
            if cur_len + len(line) > 3500:
                await update.message.reply_text("\n".join(chunk))
                chunk = []
                cur_len = 0
            chunk.append(line)
            cur_len += len(line)
        if chunk:
            await update.message.reply_text("\n".join(chunk))

async def edit_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_spam(update): return
    if not is_superadmin(update.effective_user.id):
        return await update.message.reply_text("❌ Only superadmin can edit reminders.")

    try:
        rid = context.args[0]
        dt_str = context.args[1] + " " + context.args[2]
        new_time = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        new_message = " ".join(context.args[3:])

        if not new_message:
            return await update.message.reply_text("⚠️ New message cannot be empty.")

        reminder = reminders.find_one({"_id": ObjectId(rid)})
        if not reminder:
            return await update.message.reply_text("❌ Reminder not found.")

        reminders.update_one({"_id": ObjectId(rid)}, {"$set": {"time": new_time, "message": new_message, "edited_by": update.effective_user.id, "edited_at": datetime.utcnow()}})

        remove_reminder_jobs(rid)
        schedule_reminder_jobs(context, rid, new_time, new_message)

        await update.message.reply_text("✅ Reminder updated successfully.")

        try:
            await context.bot.send_message(chat_id=SUPERADMIN_ID, text=f"✏️ Reminder {rid} edited by {update.effective_user.id}. New time: {new_time}\n{new_message}")
        except Exception:
            logging.exception("Failed to notify superadmin about edited reminder.")

    except Exception as e:
        await update.message.reply_text(f"Usage: /editreminder <id> <YYYY-MM-DD> <HH:MM> <new message>\nError: {e}")

async def delete_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_spam(update): return

    try:
        rid = context.args[0]
        reminder = reminders.find_one({"_id": ObjectId(rid)})
        if not reminder:
            return await update.message.reply_text("❌ Reminder not found.")

        user_id = update.effective_user.id

        if is_superadmin(user_id):
            reminders.delete_one({"_id": ObjectId(rid)})
            remove_reminder_jobs(rid)
            await update.message.reply_text("✅ Reminder deleted successfully.")
            try:
                await context.bot.send_message(chat_id=SUPERADMIN_ID, text=f"🗑️ Reminder {rid} was deleted by superadmin {user_id}.")
            except Exception:
                pass
            await context.bot.send_message(chat_id=CHANNEL_ID, text=f"🗑️ A reminder (ID: {rid}) was deleted by the superadmin.")
        elif is_admin_or_superadmin(user_id):
            pending_deletes[rid] = {"requested_by": user_id, "requested_at": time.time()}
            await update.message.reply_text("⌛ Deletion request sent to superadmin.")
            try:
                await context.bot.send_message(
                    chat_id=SUPERADMIN_ID,
                    text=f"⚠️ Admin {user_id} requested deletion of reminder {rid}.\nUse /approve {rid} to confirm or /reject {rid} to deny."
                )
            except Exception:
                logging.exception("Failed to notify superadmin about delete request.")
        else:
            await update.message.reply_text("❌ You are not allowed to delete reminders.")
    except Exception as e:
        await update.message.reply_text(f"Usage: /deletereminder <id>\nError: {e}")

async def approve_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_spam(update): return
    if not is_superadmin(update.effective_user.id):
        return await update.message.reply_text("❌ Only superadmin can approve deletions.")

    try:
        rid = context.args[0]
        if rid not in pending_deletes:
            return await update.message.reply_text("❌ No pending request for this reminder.")

        reminders.delete_one({"_id": ObjectId(rid)})
        remove_reminder_jobs(rid)
        requester = pending_deletes[rid]["requested_by"]
        del pending_deletes[rid]
        await update.message.reply_text(f"✅ Reminder {rid} deleted after approval.")
        try:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=f"🗑️ A reminder (ID: {rid}) was deleted by the superadmin after approval.")
            await context.bot.send_message(chat_id=requester, text=f"✅ Your deletion request for reminder {rid} was approved and it has been deleted.")
        except Exception:
            pass
    except Exception as e:
        await update.message.reply_text(f"Usage: /approve <id>\nError: {e}")

async def reject_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_spam(update): return
    if not is_superadmin(update.effective_user.id):
        return await update.message.reply_text("❌ Only superadmin can reject deletions.")

    try:
        rid = context.args[0]
        if rid not in pending_deletes:
            return await update.message.reply_text("❌ No pending request for this reminder.")

        requester = pending_deletes[rid]["requested_by"]
        del pending_deletes[rid]
        await update.message.reply_text(f"🚫 Deletion of reminder {rid} has been rejected.")
        try:
            await context.bot.send_message(chat_id=requester, text=f"🚫 Your deletion request for reminder {rid} was rejected by the superadmin.")
        except Exception:
            pass
    except Exception as e:
        await update.message.reply_text(f"Usage: /reject <id>\nError: {e}")

# --- FastAPI setup ---
app = FastAPI()
telegram_app = None  # Global variable to hold the Telegram application instance

async def run_bot():
    global telegram_app
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Add all handlers
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("addadmin", add_admin))
    telegram_app.add_handler(CommandHandler("removeadmin", remove_admin))
    telegram_app.add_handler(CommandHandler("listadmins", list_admins))
    telegram_app.add_handler(CommandHandler("remind", remind))
    telegram_app.add_handler(CommandHandler("listreminders", list_reminders))
    telegram_app.add_handler(CommandHandler("editreminder", edit_reminder))
    telegram_app.add_handler(CommandHandler("deletereminder", delete_reminder))
    telegram_app.add_handler(CommandHandler("approve", approve_delete))
    telegram_app.add_handler(CommandHandler("reject", reject_delete))
    telegram_app.add_handler(CommandHandler("broadcast", broadcast))
    telegram_app.add_handler(CommandHandler("unblock", unblock_cmd))
    telegram_app.add_handler(CommandHandler("listblocked", list_blocked))

    logging.info("🤖 Telegram bot starting in background...")
    await telegram_app.initialize()
    await telegram_app.start()
    # ✅ NEW — use `run_polling()` in a task-safe way
    asyncio.create_task(telegram_app.run_polling())

@app.on_event("startup")
async def on_startup():
    # Start the Telegram bot
    asyncio.create_task(run_bot())
    # Schedule keep-alive pings to /health endpoint
    await keep_alive(f"{SERVER_URL}/health", scheduler)
    logging.info("🌐 FastAPI server started on port 5000")

@app.on_event("shutdown")
async def on_shutdown():
    global telegram_app
    if telegram_app:
        logging.info("🛑 Stopping Telegram bot...")
        await telegram_app.stop()
        await telegram_app.shutdown()
        logging.info("🛑 Telegram bot stopped.")

    if scheduler.running:
        logging.info("🛑 Stopping APScheduler...")
        scheduler.shutdown()
        logging.info("🛑 APScheduler stopped.")

@app.get("/")
async def root():
    return {"status": "ok", "message": "Placement Reminder Bot is running!"}

@app.get("/health")
async def health():
    """Health check endpoint (for Render)"""
    return {"status": "healthy"}

# --- Run FastAPI (for local testing only) ---
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 5000))  # Use Render's PORT or default to 5000
    uvicorn.run(app, host="0.0.0.0", port=port)