"""
🚀 Nexus Extractor - Cloud Bot & Secure Gateway
Integrated with Hardware Modem Queue System
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

# Conversation States (Only for Admin now, User flow is automated)
(
    ADMIN_BAN,
    ADMIN_UNBAN,
    ADMIN_SET_USER_LIMIT_ID,
    ADMIN_SET_USER_LIMIT_VAL,
    ADMIN_SET_DEFAULT_LIMIT,
    ADMIN_GET_JSON_PHONE
) = range(6)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= Redis Manager =================
class Database:
    def __init__(self):
        self.redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    async def init_user(self, user_id: int, first_name: str):
        if not await self.redis.exists(f"user:{user_id}"):
            default_limit = await self.get_default_limit()
            await self.redis.hset(f"user:{user_id}", mapping={
                "name": first_name,
                "joined": datetime.now().isoformat(),
                "banned": "0",
                "links": "0",
                "max_links": str(default_limit)
            })

    async def is_banned(self, user_id: int) -> bool:
        return await self.redis.hget(f"user:{user_id}", "banned") == "1"

    async def set_ban(self, user_id: int, status: str):
        await self.redis.hset(f"user:{user_id}", "banned", status)

    async def get_user_quota(self, user_id: int):
        used = int(await self.redis.hget(f"user:{user_id}", "links") or 0)
        max_l = await self.redis.hget(f"user:{user_id}", "max_links")
        max_links = int(max_l) if max_l else await self.get_default_limit()
        return used, max_links

    async def set_user_limit(self, user_id: int, limit: int):
        await self.redis.hset(f"user:{user_id}", "max_links", str(limit))

    async def get_default_limit(self) -> int:
        val = await self.redis.get("config:default_limit")
        return int(val) if val else 5

    async def set_default_limit(self, limit: int):
        await self.redis.set("config:default_limit", str(limit))

    async def add_link_count(self, user_id: int):
        await self.redis.hincrby(f"user:{user_id}", "links", 1)

    async def add_log(self, user_id: int, phone: str):
        log = json.dumps({"uid": user_id, "phone": phone, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        await self.redis.rpush("bot:logs", log)

    async def save_user_created_link(self, user_id: int, phone: str, url: str, days: int):
        expire_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        link_data = json.dumps({
            "phone": phone,
            "url": url,
            "expire": expire_date,
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

    async def export_full_db(self) -> dict:
        backup = {
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "configs": {
                "expiry_days": await self.get_expiry_days(),
                "default_limit": await self.get_default_limit()
            },
            "users": {},
            "user_links": {},
            "logs": [],
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
        raw_logs = await self.redis.lrange("bot:logs", 0, -1)
        backup["logs"] = [json.loads(x) for x in raw_logs]
        return backup

    async def get_stats(self):
        keys = await self.redis.keys("user:*")
        total = len(keys)
        banned = 0
        links = 0
        for k in keys:
            user_info = await self.redis.hmget(k, ["banned", "links"])
            if user_info[0] == "1":
                banned += 1
            links += int(user_info[1] or 0)
        return total, banned, links

db = Database()

# ================= User Handlers =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.init_user(user.id, user.first_name or "کاربر")

    if await db.is_banned(user.id):
        return

    used, max_l = await db.get_user_quota(user.id)
    rem = max(0, max_l - used)

    keyboard = [
        [InlineKeyboardButton("🔗 استخراج و ساخت اکانت جدید", callback_data="btn_make_link")],
        [InlineKeyboardButton("📋 لینک‌های من", callback_data="btn_my_links"),
         InlineKeyboardButton("👤 حساب کاربری", callback_data="btn_my_account")]
    ]

    await update.message.reply_text(
        f"سلام {user.first_name} عزیز 🛒\n\n"
        f"به سیستم استخراج خودکار اکانت دیجی‌کالا جت خوش آمدید.\n"
        f"🔹 سهمیه باقی‌مانده شما: **{rem}** از **{max_l}** لینک\n\n"
        f"جهت دریافت اکانت جدید، روی دکمه زیر کلیک کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def user_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if await db.is_banned(user_id):
        return

    if query.data == "btn_make_link":
        used, max_l = await db.get_user_quota(user_id)
        if used >= max_l:
            await query.message.reply_text(
                f"❌ **سهمیه ساخت اکانت شما به اتمام رسیده است!** ({used}/{max_l})\n",
                parse_mode="Markdown"
            )
            return

        msg = await query.message.reply_text(
            "⏳ **در حال ارتباط با سرور مودم...**\n"
            "ربات در حال استخراج یک شماره آزاد، ارسال پیامک و ساخت نشست اختصاصی است. این فرآیند ممکن است ۱ تا ۲ دقیقه زمان ببرد. لطفاً صبور باشید...",
            parse_mode="Markdown"
        )

        # 1. Generate Task ID and Push to Queue
        task_id = str(uuid.uuid4())
        await db.redis.rpush("bot:tasks", task_id)

        # 2. Wait for Bridge.py to process and return result
        result = None
        for _ in range(60): # 120 seconds max timeout
            res_str = await db.redis.get(f"result:{task_id}")
            if res_str:
                result = json.loads(res_str)
                await db.redis.delete(f"result:{task_id}")
                break
            await asyncio.sleep(2)

        # 3. Handle Timeouts or Errors from Hardware Agent
        if not result:
            await msg.edit_text("❌ تایم‌اوت! سرور مودم محلی پاسخ نداد یا در حال حاضر تمامی سیم‌کارت‌ها درگیر پردازش هستند. لطفاً چند دقیقه دیگر تلاش کنید.")
            return

        if result.get("status") == "error":
            err_msg = result.get("msg", "خطای نامشخص در سیستم محلی")
            await msg.edit_text(f"❌ خطا در ساخت اکانت:\n{err_msg}")
            return

        # 4. Process Successful Extraction
        phone = result["phone"]
        final_json = result["data"]

        days = await db.get_expiry_days()
        session_token = secrets.token_urlsafe(14)
        await db.save_session(session_token, final_json, days=days, phone=phone)

        login_url = f"{WEBHOOK_URL}/auth/{session_token}"

        await db.add_link_count(user_id)
        await db.add_log(user_id, phone)
        await db.save_user_created_link(user_id, phone, login_url, days)

        rem = max(0, max_l - used - 1)

        await msg.edit_text(
            f"🎉 **اکانت اختصاصی با موفقیت استخراج شد!**\n\n"
            f"📱 شماره لاگین شده: `{phone}`\n"
            f"⏳ اعتبار پیوند: {days} روز\n"
            f"📊 سهمیه باقیمانده شما: {rem} لینک\n\n"
            f"🔗 **پیوند اختصاصی (جهت باز کردن در اپلیکیشن):**\n`{login_url}`\n\n",
            parse_mode="Markdown"
        )

    elif query.data == "btn_my_account":
        used, max_l = await db.get_user_quota(user_id)
        rem = max(0, max_l - used)
        await query.message.reply_text(
            f"👤 **وضعیت اشتراک شما:**\n\n"
            f"🔹 سهمیه کل: **{max_l}**\n"
            f"🔹 استفاده شده: **{used}**\n"
            f"🔹 سهمیه باقیمانده: **{rem}**",
            parse_mode="Markdown"
        )

    elif query.data == "btn_my_links":
        links = await db.get_user_created_links(user_id)
        if not links:
            await query.message.reply_text("❌ شما هنوز هیچ پیوندی ثبت نکرده‌اید.")
            return

        text = "📋 **پیوندهای اخیر شما:**\n\n"
        for idx, item in enumerate(reversed(links[-10:]), 1):
            text += f"{idx}. شماره: `{item['phone']}`\n"
            text += f"🔗 پیوند: `{item['url']}`\n"
            text += f"⏳ انقضا: `{item['expire']}`\n"
            text += "──────────────────\n"

        await query.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ عملیات لغو شد. برای شروع: /start")
    return ConversationHandler.END

# ================= Admin Panel (Protected) =================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    days = await db.get_expiry_days()
    expiry_text = "۱ ماهه (۳۰ روز)" if days == 30 else "۲ ماهه (۶۰ روز)"
    default_l = await db.get_default_limit()

    keyboard = [
        [InlineKeyboardButton("📊 آمار کلی ربات", callback_data="adm_stats")],
        [InlineKeyboardButton("💾 استخراج کامل دیتابیس", callback_data="adm_export_db")],
        [InlineKeyboardButton("🔍 دریافت JSON با شماره", callback_data="adm_get_json")],
        [InlineKeyboardButton(f"⏳ اعتبار پیش‌فرض: {expiry_text} (تغییر)", callback_data="adm_toggle_exp")],
        [InlineKeyboardButton(f"🌐 سهمیه پیش‌فرض: {default_l} لینک (تغییر)", callback_data="adm_set_def_limit")],
        [InlineKeyboardButton("⚙️ تنظیم سهمیه یک کاربر خاص", callback_data="adm_set_user_limit")],
        [InlineKeyboardButton("🚫 مسدود کردن کاربر", callback_data="adm_ban"), InlineKeyboardButton("✅ رفع مسدودی کاربر", callback_data="adm_unban")]
    ]
    await update.message.reply_text("⚙️ **پنل مدیریت پیشرفته ربات:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        return ConversationHandler.END
    await query.answer()

    if query.data == "adm_stats":
        total, banned, links = await db.get_stats()
        try:
            await query.edit_message_text(
                f"📊 **آمار سیستم:**\n\n👥 کاربران: {total}\n🚫 مسدود شده‌ها: {banned}\n🔗 کل پیوندهای تولید شده: {links}",
                parse_mode="Markdown"
            )
        except BadRequest:
            pass

    elif query.data == "adm_export_db":
        msg = await query.message.reply_text("⏳ در حال استخراج دیتابیس...")
        try:
            db_data = await db.export_full_db()
            file_bytes = BytesIO(json.dumps(db_data, ensure_ascii=False, indent=2).encode('utf-8'))
            file_bytes.name = f"backup_database_{datetime.now().strftime('%Y%m%d_%H%M')}.json"

            await context.bot.send_document(
                chat_id=query.message.chat.id,
                document=file_bytes,
                caption=f"💾 **بکاپ کامل دیتابیس Redis**\n\n👥 کاربران: {len(db_data['users'])}\n📝 لاگ‌ها: {len(db_data['logs'])}\n🔑 نشست‌های فعال: {db_data['active_sessions_count']}",
                parse_mode="Markdown"
            )
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"❌ خطا در استخراج دیتابیس: {e}")

    elif query.data == "adm_get_json":
        await query.message.reply_text("📱 لطفاً شماره موبایل مورد نظر را ارسال کنید:\n(مثال: 09123456789)\n\n🔙 لغو: /cancel")
        return ADMIN_GET_JSON_PHONE

    elif query.data == "adm_toggle_exp":
        current = await db.get_expiry_days()
        new_days = 60 if current == 30 else 30
        await db.set_expiry_days(new_days)
        new_text = "۱ ماهه (۳۰ روز)" if new_days == 30 else "۲ ماهه (۶۰ روز)"
        try:
            await query.edit_message_text(f"✅ اعتبار پیوندهای جدید به **{new_text}** تغییر یافت.", parse_mode="Markdown")
        except BadRequest:
            pass

    elif query.data == "adm_set_def_limit":
        await query.message.reply_text("🔢 سهمیه پیش‌فرض جدید را وارد کنید:\n(برای لغو: /cancel)")
        return ADMIN_SET_DEFAULT_LIMIT

    elif query.data == "adm_set_user_limit":
        await query.message.reply_text("👤 آیدی عددی (Chat ID) کاربر را بفرستید:\n(برای لغو: /cancel)")
        return ADMIN_SET_USER_LIMIT_ID

    elif query.data == "adm_ban":
        await query.message.reply_text("🚫 آیدی عددی کاربر برای مسدود شدن را بفرستید:\n(برای لغو: /cancel)")
        return ADMIN_BAN

    elif query.data == "adm_unban":
        await query.message.reply_text("✅ آیدی عددی کاربر برای رفع مسدودی را بفرستید:\n(برای لغو: /cancel)")
        return ADMIN_UNBAN

async def adm_handle_get_json_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    msg = await update.message.reply_text("🔍 در حال جستجوی اکانت...")
    session_data = await db.get_session_by_phone(phone)

    if not session_data:
        await msg.edit_text(f"❌ هیچ سشن فعالی برای شماره `{phone}` یافت نشد.", parse_mode="Markdown")
        return ConversationHandler.END

    file_bytes = BytesIO(json.dumps(session_data, ensure_ascii=False, indent=2).encode('utf-8'))
    file_bytes.name = f"jet_account_{phone}.json"

    await msg.delete()
    await update.message.reply_document(
        document=file_bytes,
        caption=f"✅ **اطلاعات نشست استخراج شد!**\n\n📱 شماره: `{phone}`",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def adm_save_default_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.isdigit():
        await db.set_default_limit(int(text))
        await update.message.reply_text(f"✅ سهمیه پیش‌فرض به {text} تغییر یافت.")
    return ConversationHandler.END

async def adm_get_user_for_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.isdigit():
        context.user_data['target_uid'] = text
        await update.message.reply_text(f"🔢 سقف لینک مجاز برای کاربر {text} را وارد کنید:")
        return ADMIN_SET_USER_LIMIT_VAL
    return ConversationHandler.END

async def adm_save_user_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    uid = context.user_data.get('target_uid')
    if val.isdigit() and uid:
        await db.set_user_limit(int(uid), int(val))
        await update.message.reply_text(f"✅ سقف لینک کاربر {uid} به {val} تغییر کرد.")
    context.user_data.clear()
    return ConversationHandler.END

async def adm_handle_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text.strip()
    if uid.isdigit():
        await db.set_ban(int(uid), "1")
        await update.message.reply_text(f"✅ کاربر {uid} مسدود شد.")
    return ConversationHandler.END

async def adm_handle_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text.strip()
    if uid.isdigit():
        await db.set_ban(int(uid), "0")
        await update.message.reply_text(f"✅ کاربر {uid} آزاد شد.")
    return ConversationHandler.END

# ================= Secure Gateway Web Route =================
async def web_telegram_webhook(request: web.Request):
    app = request.app["bot_app"]
    try:
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
    except Exception as e:
        logger.error(f"Webhook Error: {e}")
    return web.Response(text="OK")

async def web_secure_gateway(request: web.Request):
    token_key = request.match_info.get("token")
    session_data = await db.get_session(token_key)

    if not session_data:
        html_not_found = """
        <!DOCTYPE html>
        <html dir="rtl" lang="fa">
        <head><meta charset="UTF-8"><title>پیوند نامعتبر</title>
        <style>body{font-family:Tahoma,sans-serif;background:#f8fafc;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}
        .card{background:#fff;padding:35px;border-radius:14px;box-shadow:0 4px 20px rgba(0,0,0,0.06);text-align:center;max-width:380px;border:1px solid #e2e8f0;}
        h3{color:#e11d48;margin-top:0;}p{color:#64748b;font-size:14px;line-height:1.7;}</style></head>
        <body><div class="card"><h3>پیوند منقضی یا نامعتبر است</h3><p>این پیوند در سیستم یافت نشد یا مدت اعتبار آن به پایان رسیده است.</p></div></body></html>
        """
        return web.Response(text=html_not_found, content_type="text/html", status=404)

    user_agent = request.headers.get("User-Agent", "")
    app_header = request.headers.get("X-Client-App", "")

    if app_header == APP_SECRET_HEADER or "JetAppClient" in user_agent:
        return web.json_response({"status": "success", "session": session_data})

    html_browser_blocked = """
    <!DOCTYPE html>
    <html dir="rtl" lang="fa">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>دسترسی فقط از طریق اپلیکیشن</title>
    <style>body { font-family: Tahoma, -apple-system, sans-serif; background-color: #f1f5f9; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .box { background: #ffffff; padding: 35px 25px; border-radius: 16px; box-shadow: 0 10px 25px rgba(0, 0, 0, 0.05); text-align: center; max-width: 400px; width: 85%; }
    .icon { font-size: 45px; margin-bottom: 15px; } h3 { color: #0f172a; margin: 0 0 12px; font-size: 17px; } p { color: #64748b; font-size: 14px; line-height: 1.7; margin: 0; }</style>
    </head>
    <body><div class="box"><div class="icon">🔒</div><h3>دسترسی مستقیم مسدود است</h3><p>این پیوند صرفاً برای اجرا در <b>اپلیکیشن اختصاصی</b> طراحی شده است و امکان مشاهده مستقیم آن در مرورگر وجود ندارد.</p></div></body></html>
    """
    return web.Response(text=html_browser_blocked, content_type="text/html")

# ================= Runner =================
async def main():
    bot_app = Application.builder().token(TOKEN).build()

    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern="^adm_")],
        states={
            ADMIN_SET_DEFAULT_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_save_default_limit)],
            ADMIN_SET_USER_LIMIT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_get_user_for_limit)],
            ADMIN_SET_USER_LIMIT_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_save_user_limit)],
            ADMIN_GET_JSON_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_handle_get_json_phone)],
            ADMIN_BAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_handle_ban)],
            ADMIN_UNBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_handle_unban)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("admin", admin_panel))
    bot_app.add_handler(admin_conv)
    
    # Handlers User Clicks directly (No conversation needed anymore)
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
    logger.info(f"🚀 Combined Cloud Server started on port {PORT}")

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
