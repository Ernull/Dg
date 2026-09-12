"""
🚀 Nexus Extractor - PRO TELEGRAM BOT (MULTI-ADMIN & AUTO-EXPORT)
"""
import os, json, asyncio, logging, secrets
from io import BytesIO
from datetime import datetime
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ================= Configuration =================
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")

# پشتیبانی از چند ادمین: آیدی‌ها را با کاما جدا کنید (مثال: 123456,7891011)
admin_ids_env = os.environ.get("ADMIN_IDS", "123456789")
ADMIN_IDS = [int(x.strip()) for x in admin_ids_env.split(",") if x.strip().isdigit()]

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://your-domain.com").rstrip('/')
PORT = int(os.environ.get("PORT", "8080"))
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

APP_SECRET_HEADER = "JetApp-Secure-Client"

import redis.asyncio as aioredis
db = aioredis.from_url(REDIS_URL, decode_responses=True)

logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO)

# ================= Helper Functions =================
async def send_links_file(bot, chat_id):
    """تابع کمکی برای تولید و ارسال فایل لینک‌ها"""
    records = await db.hgetall("jet:bulk_accounts")
    if not records:
        await bot.send_message(chat_id=chat_id, text="❌ هیچ لینکی در سیستم موجود نیست.")
        return
        
    text_content = "🔗 لیست تمام اکانت‌های استخراج شده:\n\n"
    for phone, val in records.items():
        data = json.loads(val)
        link = f"{WEBHOOK_URL}/auth/{data['token']}"
        text_content += f"📱 شماره: {phone}\n👤 نام: {data['name']}\n🔗 لینک: {link}\n──────────────────\n"
        
    file_bytes = BytesIO(text_content.encode('utf-8'))
    file_bytes.name = f"Nexus_Links_{datetime.now().strftime('%Y%m%d')}.txt"
    await bot.send_document(chat_id=chat_id, document=file_bytes, caption=f"✅ لیست {len(records)} لینک ورود آماده شد.")

# ================= Telegram Handlers =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    await admin_panel(update, context)

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    
    keyboard = [
        [InlineKeyboardButton("▶️ شروع پردازش همزمان", callback_data="adm_start_bulk")],
        [InlineKeyboardButton("🔗 دریافت فایل تمام لینک‌ها", callback_data="adm_export_links")],
        [InlineKeyboardButton("📦 خروجی دیتابیس JSON", callback_data="adm_export_all")],
        [InlineKeyboardButton("⚠️ پاکسازی کل دیتابیس", callback_data="adm_clear_db_warn")]
    ]
    text = "⚙️ **پنل اتوماسیون مرکزی:**\n\nتولید لینک‌ها به صورت ایزوله در این سرور انجام می‌شود."
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS: return
    await query.answer()

    if query.data == "adm_start_bulk":
        await db.rpush("bot:admin_commands", "START_BULK")
        await query.message.reply_text("🚀 عملیات پردازش آغاز شد. گزارشات به زودی ارسال می‌شوند...", parse_mode="Markdown")

    elif query.data == "adm_export_links":
        msg = await query.message.reply_text("⏳ در حال ساخت فایل لینک‌ها...")
        await send_links_file(context.bot, query.message.chat.id)
        await msg.delete()

    elif query.data == "adm_export_all":
        msg = await query.message.reply_text("⏳ در حال خروجی JSON...")
        keys = await db.keys("jet_session:*")
        all_accounts = {}
        for k in keys:
            data = await db.get(k)
            if data: all_accounts[k] = json.loads(data)
        
        file_bytes = BytesIO(json.dumps(all_accounts, ensure_ascii=False, indent=2).encode('utf-8'))
        file_bytes.name = f"Nexus_Database_{datetime.now().strftime('%Y%m%d')}.json"
        await context.bot.send_document(chat_id=query.message.chat.id, document=file_bytes)
        await msg.delete()

    elif query.data == "adm_clear_db_warn":
        warn_keyboard = [
            [InlineKeyboardButton("✅ بله، دیتابیس فلش شود", callback_data="adm_clear_db_confirm")],
            [InlineKeyboardButton("❌ انصراف", callback_data="adm_cancel_action")]
        ]
        await query.message.reply_text(
            "⚠️ **هشدار امنیتی!**\nآیا از پاکسازی کل دیتابیس اطمینان دارید؟\nاین عملیات تمام لینک‌ها، نشست‌ها و حافظه خطوط استخراج شده را به صورت کامل و غیرقابل بازگشت پاک می‌کند.", 
            reply_markup=InlineKeyboardMarkup(warn_keyboard), 
            parse_mode="Markdown"
        )

    elif query.data == "adm_clear_db_confirm":
        # پاکسازی تمام جداول مرتبط
        await db.delete("jet:processed_phones")
        await db.delete("jet:bulk_accounts")
        keys = await db.keys("jet_session:*")
        if keys:
            await db.delete(*keys)
        await query.message.edit_text("🧹 دیتابیس با موفقیت به صورت کامل فلش شد.")

    elif query.data == "adm_cancel_action":
        await query.message.edit_text("✅ عملیات لغو شد.")

# ================= Background Workers =================
async def alert_listener(app: Application):
    """شنود لاگ‌ها و ارسال به ادمین‌ها + ارسال خودکار لینک در پایان کار"""
    while True:
        try:
            alert = await db.lpop("bot:admin_alerts")
            if alert:
                for admin_id in ADMIN_IDS:
                    try:
                        await app.bot.send_message(chat_id=admin_id, text=alert, parse_mode="Markdown")
                        
                        # تشخیص پایان کار و ارسال خودکار فایل لینک‌ها
                        if "گزارش نهایی" in alert:
                            await app.bot.send_message(chat_id=admin_id, text="⏳ در حال آماده‌سازی خودکار فایل لینک‌ها...")
                            await send_links_file(app.bot, admin_id)
                    except Exception as e:
                        pass
        except Exception:
            pass
        await asyncio.sleep(2)

async def token_generator_worker():
    """تولید توکن و لینک در سرور ابری (کاملاً ایزوله از سیستم محلی)"""
    while True:
        try:
            raw_data = await db.lpop("bot:new_accounts")
            if raw_data:
                acc = json.loads(raw_data)
                phone, name, final_json = acc["phone"], acc["name"], acc["data"]
                
                session_token = secrets.token_urlsafe(14)
                
                await db.setex(f"jet_session:{session_token}", 30 * 24 * 3600, json.dumps(final_json, ensure_ascii=False))
                record = {"phone": phone, "token": session_token, "name": name}
                await db.hset("jet:bulk_accounts", phone, json.dumps(record, ensure_ascii=False))
        except Exception:
            pass
        await asyncio.sleep(1)

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
    session_data_str = await db.get(f"jet_session:{token_key}")

    if not session_data_str:
        return web.Response(text="پیوند منقضی یا نامعتبر است.", status=404, content_type="text/plain;charset=utf-8")

    user_agent = request.headers.get("User-Agent", "")
    app_header = request.headers.get("X-Client-App", "")

    if app_header == APP_SECRET_HEADER or "JetAppClient" in user_agent:
        return web.json_response({"status": "success", "session": json.loads(session_data_str)})

    html_blocked = '<html dir="rtl" lang="fa"><meta charset="UTF-8"><body style="font-family:Tahoma;text-align:center;margin-top:50px;"><h3>دسترسی مسدود است</h3><p>لینک فقط در اپلیکیشن باز می‌شود.</p></body></html>'
    return web.Response(text=html_blocked, content_type="text/html")

async def main():
    bot_app = Application.builder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("admin", admin_panel))
    bot_app.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm_"))
    
    web_app = web.Application()
    web_app["bot_app"] = bot_app
    web_app.router.add_post(f"/webhook/{TOKEN}", web_telegram_webhook)
    web_app.router.add_get("/auth/{token}", web_secure_gateway)
    
    await bot_app.initialize()
    await bot_app.start()
    
    webhook_endpoint = f"{WEBHOOK_URL}/webhook/{TOKEN}"
    await bot_app.bot.set_webhook(url=webhook_endpoint)
    
    # اجرای همزمان فرآیندهای پس‌زمینه
    asyncio.create_task(alert_listener(bot_app))
    asyncio.create_task(token_generator_worker())
    
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await bot_app.stop()
        await bot_app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
