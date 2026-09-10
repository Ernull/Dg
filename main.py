"""
🚀 Nexus Extractor - Cloud Bot & Secure Gateway
- Auto-Registration
- Admin Pause/Resume & DB Flush
- System Error Logs Viewer Added
"""

import os
import json
import uuid
import secrets
import asyncio
import logging
from io import BytesIO
from datetime import datetime, timedelta
from aiohttp import web
import redis.asyncio as aioredis
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# ================= Configuration =================
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://your-domain.com").rstrip('/')
PORT = int(os.environ.get("PORT", "8080"))
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

APP_SECRET_HEADER = "JetApp-Secure-Client"

(ADMIN_BAN, ADMIN_UNBAN, ADMIN_GET_JSON_PHONE) = range(3)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= Redis Manager =================
class Database:
    def __init__(self):
        self.redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    async def init_user(self, user_id: int, first_name: str):
        if not await self.redis.exists(f"user:{user_id}"):
            await self.redis.hset(f"user:{user_id}", mapping={
                "name": first_name,
                "joined": datetime.now().isoformat(),
                "banned": "0",
                "links_created": "0"
            })

    async def is_banned(self, user_id: int) -> bool:
        return await self.redis.hget(f"user:{user_id}", "banned") == "1"

    async def set_ban(self, user_id: int, status: str):
        await self.redis.hset(f"user:{user_id}", "banned", status)

    async def add_link_count(self, user_id: int):
        await self.redis.hincrby(f"user:{user_id}", "links_created", 1)
        
    async def get_user_total_links(self, user_id: int):
        return int(await self.redis.hget(f"user:{user_id}", "links_created") or 0)

    async def is_system_paused(self) -> bool:
        return await self.redis.get("config:paused") == "1"

    async def toggle_system_pause(self) -> bool:
        current = await self.redis.get("config:paused")
        new_val = "0" if current == "1" else "1"
        await self.redis.set("config:paused", new_val)
        return new_val == "1"

    async def save_user_created_link(self, user_id: int, phone: str, url: str, days: int):
        expire_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        link_data = json.dumps({
            "phone": phone, "url": url, "expire": expire_date,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M")
        }, ensure_ascii=False)
        await self.redis.rpush(f"user_links:{user_id}", link_data)

    async def get_user_created_links(self, user_id: int):
        links = await self.redis.lrange(f"user_links:{user_id}", 0, -1)
        return [json.loads(x) for x in links]

    async def get_expiry_days(self) -> int:
        val = await self.redis.get("config:expiry_days")
        return int(val) if val else 30

    async def set_expiry_days(self, days: int):
        await self.redis.set("config:expiry_days", str(days))

    async def save_session(self, token_key: str, data: dict, days: int, phone: str = None):
        expire_seconds = days * 24 * 3600
        payload = json.dumps(data, ensure_ascii=False)
        await self.redis.setex(f"jet_session:{token_key}", expire_seconds, payload)
        if phone:
            await self.redis.setex(f"phone_session:{phone}", expire_seconds, payload)

    async def get_session(self, token_key: str):
        data = await self.redis.get(f"jet_session:{token_key}")
        return json.loads(data) if data else None

    async def get_session_by_phone(self, phone: str):
        direct_data = await self.redis.get(f"phone_session:{phone}")
        if direct_data:
            return json.loads(direct_data)
        keys = await self.redis.keys("jet_session:*")
        for k in keys:
            val = await self.redis.get(k)
            if val and phone in val:
                try:
                    return json.loads(val)
                except Exception:
                    pass
        return None

    # ====== توابع جدید برای لاگ خطا ======
    async def get_recent_errors(self, limit=5):
        errors = await self.redis.lrange("bot:admin_errors", -limit, -1)
        return [json.loads(e) for e in reversed(errors)]
    
    async def clear_errors(self):
        await self.redis.delete("bot:admin_errors")

    async def export_full_db(self) -> dict:
        backup = {
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "configs": {"expiry_days": await self.get_expiry_days()},
            "users": {}, "user_links": {},
            "active_sessions_count": len(await self.redis.keys("jet_session:*"))
        }
        user_keys = await self.redis.keys("user:*")
        for uk in user_keys:
            uid = uk.split(":")[1]
            backup["users"][uid] = await self.redis.hgetall(uk)
        link_keys = await self.redis.keys("user_links:*")
        for lk in link_keys:
            uid = lk.split(":")[1]
            items = await self.redis.lrange(lk, 0, -1)
            backup["user_links"][uid] = [json.loads(x) for x in items]
        return backup

    async def clear_operational_db(self):
        keys_to_delete = []
        patterns = ["jet_session:*", "phone_session:*", "result:*", "user_links:*", "bot:tasks", "bot:admin_errors"]
        for p in patterns:
            if "*" in p:
                found = await self.redis.keys(p)
                keys_to_delete.extend(found)
            else:
                keys_to_delete.append(p)
        if keys_to_delete:
            await self.redis.delete(*keys_to_delete)
        return len(keys_to_delete)

    async def get_stats(self):
        keys = await self.redis.keys("user:*")
        total = len(keys)
        banned = 0
        for k in keys:
            if await self.redis.hget(k, "banned") == "1":
                banned += 1
        active_sessions = len(await self.redis.keys("jet_session:*"))
        return total, banned, active_sessions

db = Database()

# ================= User Handlers =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.init_user(user.id, user.first_name or "کاربر")
    if await db.is_banned(user.id):
        return

    keyboard = [
        [InlineKeyboardButton("🔗 استخراج اکانت جدید", callback_data="btn_make_link")],
        [InlineKeyboardButton("📋 لینک‌های من", callback_data="btn_my_links"), InlineKeyboardButton("👤 پروفایل", callback_data="btn_my_account")]
    ]
    await update.message.reply_text(
        f"سلام {user.first_name} عزیز 🛒\n\n"
        f"به سیستم استخراج خودکار اکانت دیجی‌کالا جت خوش آمدید.\n"
        f"جهت دریافت اکانت جدید، روی دکمه زیر کلیک کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def user_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if await db.is_banned(user_id):
        return

    if query.data == "btn_make_link":
        if await db.is_system_paused():
            await query.message.reply_text("⛔️ **سیستم موقتاً متوقف شده است.**\n\nسیستم در حال سرویس است. لطفاً دقایقی دیگر تلاش کنید.", parse_mode="Markdown")
            return

        msg = await query.message.reply_text("⏳ **در حال ارتباط با سرور مودم...**\nمنتظر استخراج شماره آزاد و دریافت پیامک...", parse_mode="Markdown")

        task_id = str(uuid.uuid4())
        await db.redis.rpush("bot:tasks", task_id)

        result = None
        for _ in range(60): # 120s timeout
            res_str = await db.redis.get(f"result:{task_id}")
            if res_str:
                result = json.loads(res_str)
                await db.redis.delete(f"result:{task_id}")
                break
            await asyncio.sleep(2)

        if not result:
            await msg.edit_text("❌ تایم‌اوت! سرور مودم پاسخ نداد.")
            return

        if result.get("status") == "error":
            err_msg = result.get("msg", "خطای نامشخص در سیستم محلی")
            await msg.edit_text(f"❌ خطا:\n{err_msg}")
            return

        phone = result["phone"]
        final_json = result["data"]

        days = await db.get_expiry_days()
        session_token = secrets.token_urlsafe(14)
        await db.save_session(session_token, final_json, days=days, phone=phone)

        login_url = f"{WEBHOOK_URL}/auth/{session_token}"

        await db.add_link_count(user_id)
        await db.save_user_created_link(user_id, phone, login_url, days)

        await msg.edit_text(
            f"🎉 **اکانت با موفقیت استخراج شد!**\n\n"
            f"📱 شماره: `{phone}`\n"
            f"⏳ اعتبار: {days} روز\n\n"
            f"🔗 **لینک ورود:**\n`{login_url}`\n\n", parse_mode="Markdown"
        )

    elif query.data == "btn_my_account":
        total_created = await db.get_user_total_links(user_id)
        await query.message.reply_text(f"👤 **تعداد کل اکانت‌های شما:** **{total_created}** عدد", parse_mode="Markdown")

    elif query.data == "btn_my_links":
        links = await db.get_user_created_links(user_id)
        if not links:
            await query.message.reply_text("❌ شما هنوز هیچ اکانتی ندارید.")
            return
        text = "📋 **پیوندهای اخیر شما:**\n\n"
        for idx, item in enumerate(reversed(links[-10:]), 1):
            text += f"{idx}. شماره: `{item['phone']}`\n🔗 پیوند: `{item['url']}`\n──────────────────\n"
        await query.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ لغو شد.")
    return ConversationHandler.END

# ================= Admin Panel =================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    days = await db.get_expiry_days()
    expiry_text = "۱ ماهه" if days == 30 else "۲ ماهه"
    is_paused = await db.is_system_paused()
    pause_btn_text = "▶️ شروع سیستم" if is_paused else "⏸ توقف موقت"

    keyboard = [
        [InlineKeyboardButton(pause_btn_text, callback_data="adm_toggle_pause")],
        [InlineKeyboardButton("🚨 مشاهده لاگ خطاها", callback_data="adm_view_errors")],
        [InlineKeyboardButton("📊 آمار کلی", callback_data="adm_stats"), InlineKeyboardButton("🧹 پاکسازی DB", callback_data="adm_clear_db")],
        [InlineKeyboardButton("💾 استخراج بکاپ", callback_data="adm_export_db"), InlineKeyboardButton("🔍 دریافت JSON", callback_data="adm_get_json")],
        [InlineKeyboardButton(f"⏳ اعتبار پیوندها: {expiry_text}", callback_data="adm_toggle_exp")],
        [InlineKeyboardButton("🚫 مسدود کردن", callback_data="adm_ban"), InlineKeyboardButton("✅ رفع مسدودی", callback_data="adm_unban")]
    ]
    await update.message.reply_text("⚙️ **پنل مدیریت ربات:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        return ConversationHandler.END
    await query.answer()

    if query.data == "adm_toggle_pause":
        is_paused = await db.toggle_system_pause()
        await query.message.reply_text(f"سیستم {'🛑 متوقف' if is_paused else '✅ فعال'} شد.", parse_mode="Markdown")
        await admin_panel(update, context)

    elif query.data == "adm_clear_db":
        deleted = await db.clear_operational_db()
        await query.message.reply_text(f"🧹 `{deleted}` رکورد (سشن، خطا، صف) پاکسازی شد.", parse_mode="Markdown")

    elif query.data == "adm_view_errors":
        errors = await db.get_recent_errors(5)
        if not errors:
            await query.message.reply_text("✅ سیستم در حال حاضر هیچ خطایی ندارد.")
            return
        
        text = "🚨 **۵ خطای اخیر سیستم محلی:**\n\n"
        for i, err in enumerate(errors, 1):
            text += f"*{i}. زمان:* {err['time']}\n"
            text += f"*📱 شماره:* `{err['phone']}`\n"
            text += f"*⚠️ نوع خطا:* {err['error_type']}\n"
            text += f"*📝 شرح:* `{err['details']}`\n"
            text += "────────────────\n"
        
        kb = [[InlineKeyboardButton("🗑 پاکسازی لیست خطاها", callback_data="adm_clear_errors")]]
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data == "adm_clear_errors":
        await db.clear_errors()
        await query.edit_message_text("✅ لیست خطاها با موفقیت پاکسازی شد.")

    elif query.data == "adm_stats":
        total, banned, active = await db.get_stats()
        try:
            await query.edit_message_text(f"📊 **آمار:**\n👥 کاربران: {total}\n🚫 مسدود: {banned}\n🔗 سشن‌های فعال: {active}", parse_mode="Markdown")
        except BadRequest:
            pass

    elif query.data == "adm_export_db":
        msg = await query.message.reply_text("⏳ در حال استخراج...")
        db_data = await db.export_full_db()
        file_bytes = BytesIO(json.dumps(db_data, ensure_ascii=False, indent=2).encode('utf-8'))
        file_bytes.name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
        await context.bot.send_document(chat_id=query.message.chat.id, document=file_bytes)
        await msg.delete()

    elif query.data == "adm_get_json":
        await query.message.reply_text("📱 شماره موبایل را ارسال کنید:")
        return ADMIN_GET_JSON_PHONE

    elif query.data == "adm_toggle_exp":
        new_days = 60 if await db.get_expiry_days() == 30 else 30
        await db.set_expiry_days(new_days)
        try:
            await query.edit_message_text(f"✅ اعتبار پیوندها تغییر یافت.", parse_mode="Markdown")
        except BadRequest: pass

    elif query.data == "adm_ban":
        await query.message.reply_text("🚫 آیدی کاربر برای مسدودی:")
        return ADMIN_BAN

    elif query.data == "adm_unban":
        await query.message.reply_text("✅ آیدی کاربر برای رفع مسدودی:")
        return ADMIN_UNBAN

async def adm_handle_get_json_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    session_data = await db.get_session_by_phone(phone)
    if not session_data:
        await update.message.reply_text("❌ یافت نشد.")
        return ConversationHandler.END
    file_bytes = BytesIO(json.dumps(session_data, ensure_ascii=False, indent=2).encode('utf-8'))
    file_bytes.name = f"jet_{phone}.json"
    await update.message.reply_document(document=file_bytes)
    return ConversationHandler.END

async def adm_handle_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.set_ban(int(update.message.text.strip()), "1")
    await update.message.reply_text("✅ مسدود شد.")
    return ConversationHandler.END

async def adm_handle_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.set_ban(int(update.message.text.strip()), "0")
    await update.message.reply_text("✅ رفع مسدودی شد.")
    return ConversationHandler.END

# ================= Secure Gateway Web Route =================
async def web_telegram_webhook(request: web.Request):
    app = request.app["bot_app"]
    try:
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
    except Exception:
        pass
    return web.Response(text="OK")

async def web_secure_gateway(request: web.Request):
    token_key = request.match_info.get("token")
    session_data = await db.get_session(token_key)
    if not session_data:
        return web.Response(text="Not Found", status=404)

    user_agent = request.headers.get("User-Agent", "")
    app_header = request.headers.get("X-Client-App", "")
    if app_header == APP_SECRET_HEADER or "JetAppClient" in user_agent:
        return web.json_response({"status": "success", "session": session_data})

    return web.Response(text="دسترسی فقط از اپلیکیشن مجاز است.", content_type="text/plain;charset=utf-8")

# ================= Runner =================
async def main():
    bot_app = Application.builder().token(TOKEN).build()

    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern="^adm_")],
        states={
            ADMIN_GET_JSON_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_handle_get_json_phone)],
            ADMIN_BAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_handle_ban)],
            ADMIN_UNBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_handle_unban)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("admin", admin_panel))
    bot_app.add_handler(admin_conv)
    bot_app.add_handler(CallbackQueryHandler(user_menu_callback, pattern="^btn_"))

    web_app = web.Application()
    web_app["bot_app"] = bot_app
    web_app.router.add_post(f"/webhook/{TOKEN}", web_telegram_webhook)
    web_app.router.add_get("/auth/{token}", web_secure_gateway)

    await bot_app.initialize()
    await bot_app.start()

    webhook_endpoint = f"{WEBHOOK_URL}/webhook/{TOKEN}"
    await bot_app.bot.set_webhook(url=webhook_endpoint, allowed_updates=Update.ALL_TYPES)
    logger.info(f"Webhook set to: {webhook_endpoint}")

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🚀 Cloud Server started on port {PORT}")

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await bot_app.stop()
        await bot_app.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
