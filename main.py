"""
🚀 Nexus Extractor - ADMIN AUTOMATION & WEB SERVER
"""
import os, json, asyncio, logging
from io import BytesIO
from datetime import datetime
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://your-domain.com").rstrip('/')
PORT = int(os.environ.get("PORT", "8080"))
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

APP_SECRET_HEADER = "JetApp-Secure-Client"

import redis.asyncio as aioredis
db = aioredis.from_url(REDIS_URL, decode_responses=True)

logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await admin_panel(update, context)

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    keyboard = [
        [InlineKeyboardButton("▶️ شروع رگباری (کل مودم)", callback_data="adm_start_bulk")],
        [InlineKeyboardButton("🔗 دریافت فایل تمام لینک‌ها", callback_data="adm_export_links")],
        [InlineKeyboardButton("📦 خروجی دیتابیس JSON", callback_data="adm_export_all")],
        [InlineKeyboardButton("🧹 فرمت شماره‌های تکراری", callback_data="adm_clear_history")]
    ]
    text = "⚙️ **پنل اتوماسیون و درگاه لینک‌ساز:**\n\nسرور وب فعال است. پس از پایان استخراج، فایل لینک‌ها را دریافت کنید."
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return
    await query.answer()

    if query.data == "adm_start_bulk":
        await db.rpush("bot:admin_commands", "START_BULK")
        await query.message.reply_text("🚀 عملیات رگباری آغاز شد. منتظر دریافت گزارش از سیستم محلی باشید...", parse_mode="Markdown")

    elif query.data == "adm_export_links":
        msg = await query.message.reply_text("⏳ در حال ساخت فایل لینک‌ها...")
        records = await db.hgetall("jet:bulk_accounts")
        if not records:
            await msg.edit_text("❌ هیچ لینکی در سیستم موجود نیست.")
            return
            
        text_content = "🔗 لیست تمام اکانت‌های استخراج شده:\n\n"
        for phone, val in records.items():
            data = json.loads(val)
            link = f"{WEBHOOK_URL}/auth/{data['token']}"
            text_content += f"📱 شماره: {phone}\n👤 نام: {data['name']}\n🔗 لینک: {link}\n──────────────────\n"
            
        file_bytes = BytesIO(text_content.encode('utf-8'))
        file_bytes.name = f"Jet_Links_{datetime.now().strftime('%Y%m%d')}.txt"
        await context.bot.send_document(chat_id=query.message.chat.id, document=file_bytes, caption=f"✅ لیست {len(records)} لینک ورود آماده شد.")
        await msg.delete()

    elif query.data == "adm_export_all":
        msg = await query.message.reply_text("⏳ در حال خروجی JSON...")
        keys = await db.keys("jet_session:*")
        all_accounts = {}
        for k in keys:
            data = await db.get(k)
            if data: all_accounts[k] = json.loads(data)
        
        file_bytes = BytesIO(json.dumps(all_accounts, ensure_ascii=False, indent=2).encode('utf-8'))
        file_bytes.name = f"Jet_Database_{datetime.now().strftime('%Y%m%d')}.json"
        await context.bot.send_document(chat_id=query.message.chat.id, document=file_bytes)
        await msg.delete()

    elif query.data == "adm_clear_history":
        await db.delete("jet:processed_phones")
        await db.delete("jet:bulk_accounts")
        await query.message.reply_text("🧹 حافظه شماره‌های تکراری و لینک‌ها برای دوره جدید پاک شد.")

async def alert_listener(app: Application):
    while True:
        try:
            alert = await db.lpop("bot:admin_alerts")
            if alert:
                await app.bot.send_message(chat_id=ADMIN_ID, text=alert, parse_mode="Markdown")
        except Exception:
            pass
        await asyncio.sleep(2)

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
    
    asyncio.create_task(alert_listener(bot_app))
    
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
