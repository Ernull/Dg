import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import redis
import time
import uuid
import json
import os

# ================= Configuration =================
# خواندن اطلاعات از متغیرهای محیطی Railway برای امنیت بیشتر
BOT_TOKEN = os.getenv("BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")

# خواندن آیدی ادمین‌ها (به صورت اعداد جدا شده با کاما در متغیر محیطی ADMIN_IDS)
admin_ids_env = os.getenv("ADMIN_IDS", "")
ADMINS = [int(aid.strip()) for aid in admin_ids_env.split(",") if aid.strip().isdigit()]

if not BOT_TOKEN or not REDIS_URL:
    raise ValueError("❌ لطفاً متغیرهای محیطی BOT_TOKEN و REDIS_URL را در پنل Railway تنظیم کنید!")

bot = telebot.TeleBot(BOT_TOKEN)

# اتصال امن به ردیس با قابلیت تلاش مجدد
while True:
    try:
        db = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        db.ping()
        print("✅ Nexus Extractor Bot connected to Cloud Redis.")
        break
    except Exception as e:
        print("⏳ Waiting for Cloud Redis connection... (Retrying in 5s)")
        time.sleep(5)

print("🤖 Nexus Extractor Bot is running...")

# ================= Admin Panel (پنل کنترل سیستم) =================
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.chat.id not in ADMINS:
        return
    
    status = db.get("system_status") or "active"
    status_text = "🟢 روشن و آماده کار" if status == "active" else "🔴 متوقف (در حال تعویض سیم‌کارت)"
    
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("🟢 استارت سیستم", callback_data="sys_start"),
        InlineKeyboardButton("🔴 توقف سیستم", callback_data="sys_stop")
    )
    
    bot.send_message(message.chat.id, f"⚙️ **پنل مدیریت Nexus Extractor**\n\nوضعیت فعلی: {status_text}", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data in ["sys_start", "sys_stop"])
def handle_system_status(call):
    if call.message.chat.id not in ADMINS:
        return
        
    if call.data == "sys_start":
        db.set("system_status", "active")
        bot.answer_callback_query(call.id, "سیستم روشن شد! کاربرها میتوانند لینک دریافت کنند.")
        bot.edit_message_text("⚙️ **پنل مدیریت Nexus Extractor**\n\nوضعیت فعلی: 🟢 روشن و آماده کار", call.message.chat.id, call.message.message_id)
    
    elif call.data == "sys_stop":
        db.set("system_status", "paused")
        bot.answer_callback_query(call.id, "سیستم متوقف شد! لطفاً سیم‌کارت‌ها را تعویض کنید.")
        bot.edit_message_text("⚙️ **پنل مدیریت Nexus Extractor**\n\nوضعیت فعلی: 🔴 متوقف (در حال تعویض سیم‌کارت)", call.message.chat.id, call.message.message_id)

# ================= User Request (درخواست لینک توسط مشتری) =================
@bot.message_handler(commands=['getlink', 'start']) 
def request_new_account(message):
    chat_id = message.chat.id
    
    # ۱. چک کردن وضعیت سیستم
    status = db.get("system_status") or "active"
    if status == "paused":
        bot.send_message(chat_id, "⚠️ ظرفیت سیم‌کارت‌های فعلی پر شده و سیستم در حال بروزرسانی شماره‌ها است. لطفاً چند دقیقه دیگر مجدداً تلاش کنید.")
        return

    # ۲. ساخت یک شناسه یکتا برای این درخواست و ارسال به صف
    task_id = str(uuid.uuid4())
    msg = bot.send_message(chat_id, "⏳ در حال ارتباط با سرور و ساخت اکانت دیجی‌کالا... لطفاً صبور باشید.")
    
    # ارسال دستور به فایل اجرایی آقای بیگی
    db.rpush("bot:tasks", task_id)
    
    # ۳. منتظر ماندن برای دریافت نتیجه (هماهنگ با تایم‌اوت ۷۵ ثانیه‌ای واسط)
    wait_time = 0
    final_result = None
    
    while wait_time < 75:
        result_data = db.get(f"result:{task_id}")
        if result_data:
            final_result = json.loads(result_data)
            db.delete(f"result:{task_id}") # پاکسازی دیتابیس
            break
        
        time.sleep(3)
        wait_time += 3

    # ۴. پردازش نتیجه و ارسال به کاربر
    if not final_result:
        bot.edit_message_text("❌ متاسفانه سرور پاسخ نداد. ممکن است اینترنت دستگاه مودم قطع باشد.", chat_id, msg.message_id)
        return
        
    if final_result.get("status") == "error":
        error_msg = final_result.get("msg", "خطای نامشخص")
        bot.edit_message_text(f"❌ خطا در ساخت اکانت:\n`{error_msg}`", chat_id, msg.message_id, parse_mode="Markdown")
        return
        
    if final_result.get("status") == "success":
        phone = final_result.get("phone")
        json_data = final_result.get("data") # دیتای کش و کوکی
        
        success_text = f"✅ **اکانت با موفقیت ساخته شد!**\n\n📱 شماره: `{phone}`\n\n🔗 لینک ورود شما آماده است."
        bot.edit_message_text(success_text, chat_id, msg.message_id, parse_mode="Markdown")

bot.infinity_polling()
