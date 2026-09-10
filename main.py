"""
🚀 Nexus Extractor - ADMIN AUTOMATION
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
        await update.message.reply_text("⛔️ این ربات به حالت تمام خودکار درآمده و فقط برای ادمین در دسترس است.")
        return
    await admin_panel(update, context)

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    keyboard = [
        [InlineKeyboardButton("▶️ شروع ثبت‌نام خودکار (تمام مودم)", callback_data="adm_start_bulk")],
        [InlineKeyboardButton("📦 خروجی یکجای تمام اکانت‌ها (JSON)", callback_data="adm_export_all")],
        [InlineKeyboardButton("📋 مشاهده وضعیت و لاگ‌ها", callback_data="adm_view_logs")],
        [InlineKeyboardButton("🧹 پاکسازی لیست تکراری‌ها", callback_data="adm_clear_history")]
    ]
    text = "⚙️ **پنل مدیریت اتوماسیون جت:**\n\nبا فشردن دکمه استارت، سیستم خودش شماره‌ها را خوانده و با رعایت پروکسی اکانت‌ها را می‌سازد."
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
        await query.message.reply_text("🚀 دستور **اجرای خودکار** به سرور مودم ارسال شد.\nلطفاً از بخش لاگ‌ها وضعیت را بررسی کنید.", parse_mode="Markdown")

    elif query.data == "adm_export_all":
        msg = await query.message.reply_text("⏳ در حال جمع‌آوری تمامی اکانت‌ها...")
        keys = await db.keys("phone_session:*")
        all_accounts = {}
        for k in keys:
            phone = k.split(":")[1]
            data = await db.get(k)
            if data: all_accounts[phone] = json.loads(data)
            
        if not all_accounts:
            await msg.edit_text("❌ هیچ اکانتی در دیتابیس یافت نشد.")
            return
            
        file_bytes = BytesIO(json.dumps(all_accounts, ensure_ascii=False, indent=2).encode('utf-8'))
        file_bytes.name = f"Nexus_All_Accounts_{datetime.now().strftime('%Y%m%d')}.json"
        
        await context.bot.send_document(chat_id=query.message.chat.id, document=file_bytes, caption=f"📦 خروجی کامل:\n✅ تعداد {len(all_accounts)} اکانت معتبر استخراج شد.")
        await msg.delete()

    elif query.data == "adm_view_logs":
        logs = await db.lrange("bot:admin_logs", -7, -1)
        if not logs:
            await query.message.reply_text("✅ لاگ جدیدی وجود ندارد.")
            return
        text = "📋 **آخرین وضعیت سیستم محلی:**\n\n"
        for log_str in reversed(logs):
            l = json.loads(log_str)
            text += f"▪️ `[{l['time']}]` | {l['phone']}\nوضعیت: **{l['status']}**\nجزئیات: {l['details']}\n──────────────\n"
        await query.message.reply_text(text, parse_mode="Markdown")

    elif query.data == "adm_clear_history":
        count = await db.scard("jet:processed_phones")
        await db.delete("jet:processed_phones")
        await query.message.reply_text(f"🧹 لیست شماره‌های تکراری پاک شد.\n(تعداد {count} شماره از حافظه ردیس حذف گردید)")

async def main():
    bot_app = Application.builder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("admin", admin_panel))
    bot_app.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm_"))
    
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    
    try:
        await asyncio.Event().wait()
    finally:
        await bot_app.stop()

if __name__ == "__main__":
    asyncio.run(main())
