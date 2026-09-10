"""
🚀 Nexus Extractor - ADMIN AUTOMATION & LIVE ALERTS
"""
import os, json, asyncio, logging
from io import BytesIO
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

import redis.asyncio as aioredis
db = aioredis.from_url(REDIS_URL, decode_responses=True)

logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await admin_panel(update, context)

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    keyboard = [
        [InlineKeyboardButton("▶️ شروع استخراج رگباری (کل مودم)", callback_data="adm_start_bulk")],
        [InlineKeyboardButton("📦 خروجی یکجای تمام اکانت‌ها (JSON)", callback_data="adm_export_all")],
        [InlineKeyboardButton("🧹 پاکسازی لیست تکراری‌ها", callback_data="adm_clear_history")]
    ]
    text = "⚙️ **پنل مدیریت اتوماسیون جت:**\n\nآماده دریافت دستور. گزارشات به صورت خودکار همینجا ارسال خواهند شد."
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
        await query.message.reply_text("🚀 فرمان رگباری ارسال شد. منتظر دریافت گزارش اولیه از سیستم آقای بیگی باشید...", parse_mode="Markdown")

    elif query.data == "adm_export_all":
        msg = await query.message.reply_text("⏳ در حال جمع‌آوری...")
        keys = await db.keys("phone_session:*")
        all_accounts = {}
        for k in keys:
            phone = k.split(":")[1]
            data = await db.get(k)
            if data: all_accounts[phone] = json.loads(data)
            
        if not all_accounts:
            await msg.edit_text("❌ هیچ اکانتی یافت نشد.")
            return
            
        file_bytes = BytesIO(json.dumps(all_accounts, ensure_ascii=False, indent=2).encode('utf-8'))
        file_bytes.name = f"Nexus_All_{datetime.now().strftime('%Y%m%d')}.json"
        
        await context.bot.send_document(chat_id=query.message.chat.id, document=file_bytes, caption=f"📦 تعداد {len(all_accounts)} اکانت استخراج شد.")
        await msg.delete()

    elif query.data == "adm_clear_history":
        count = await db.scard("jet:processed_phones")
        await db.delete("jet:processed_phones")
        await query.message.reply_text(f"🧹 لیست ضدتکرار پاک شد ({count} شماره).")

async def alert_listener(app: Application):
    """ناظر پس‌زمینه: خواندن آلارم‌ها از ردیس و ارسال فوری به ادمین"""
    while True:
        try:
            alert = await db.lpop("bot:admin_alerts")
            if alert:
                await app.bot.send_message(chat_id=ADMIN_ID, text=alert, parse_mode="Markdown")
        except Exception:
            pass
        await asyncio.sleep(2)

async def main():
    bot_app = Application.builder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("admin", admin_panel))
    bot_app.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm_"))
    
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    
    # اجرای ناظر آلارم‌ها به صورت همزمان با ربات
    asyncio.create_task(alert_listener(bot_app))
    
    try:
        await asyncio.Event().wait()
    finally:
        await bot_app.stop()

if __name__ == "__main__":
    asyncio.run(main())
