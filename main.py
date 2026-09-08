"""
🚀 Advanced Digikala Jet Link Maker Bot & Secure API Gateway
Features:
- Completely Hides Session Data from Browser View (Anti-Inspect)
- Dedicated API Endpoint for Custom Android App
- User Quota / Limit System
- Link History for Users ("لینک‌های من")
- Continuous Link Flow ("ساخت لینک بعدی / پایان")
- Protected Admin Panel with 30/60 Days Expiry Toggle
- Webhook & Redis Ready for Railway Deployment
"""

import os
import json
import uuid
import secrets
import asyncio
import logging
from datetime import datetime, timedelta
import aiohttp
from aiohttp import web
import redis.asyncio as aioredis
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# کلید هدر امنیتی برای ارتباط اپلیکیشن با سرور
APP_SECRET_HEADER = "JetApp-Secure-Client"

# Conversation States
ASK_PHONE, ASK_OTP, ACTION_CHOICE = range(3)
ADMIN_BAN, ADMIN_UNBAN, ADMIN_SET_USER_LIMIT_ID, ADMIN_SET_USER_LIMIT_VAL, ADMIN_SET_DEFAULT_LIMIT = range(3, 8)

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

    async def save_session(self, token_key: str, data: dict, days: int):
        expire_seconds = days * 24 * 3600
        await self.redis.setex(f"jet_session:{token_key}", expire_seconds, json.dumps(data, ensure_ascii=False))

    async def get_session(self, token_key: str):
        data = await self.redis.get(f"jet_session:{token_key}")
        return json.loads(data) if data else None

    async def get_stats(self):
        keys = await self.redis.keys("user:*")
        total = len(keys)
        banned = sum([1 for k in keys if await self.redis.hget(k, "banned") == "1"])
        links = sum([int(await self.redis.hget(k, "links") or 0) for k in keys])
        return total, banned, links

db = Database()

# ================= Digikala API Logic =================
class AsyncJetAuth:
    def __init__(self):
        self.client_id = "FINGERPRINTV2-6a44d158446867e4502af048b412cc7d"
        self.headers = {
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
            'X-Request-UUID': str(uuid.uuid4()),
            'ClientId': self.client_id,
            'ClientOs': 'Android',
            'Client': 'mobile',
            'platform-sso-disable-prod': '1',
            'app-id': '8b62e987-34bf-48bd-bc62-347f38309a36',
            'clientid-v2': self.client_id
        }

    async def request_otp(self, phone: str):
        url = "https://api.digikalajet.ir/user/login-register/?ch=jj"
        self.headers['X-Request-UUID'] = str(uuid.uuid4())
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.post(url, json={"phone": phone}, timeout=10) as res:
                    if res.status == 200:
                        data = await res.json()
                        return True, data.get("data", {}).get("token", "")
                    return False, f"HTTP {res.status}"
            except Exception as e:
                return False, str(e)

    async def confirm_phone(self, phone: str, code: str, otp_token: str):
        url = "https://api.digikalajet.ir/user/confirm-phone/?ch=jj"
        self.headers['X-Request-UUID'] = str(uuid.uuid4())
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.post(url, json={"phone": phone, "code": code, "token": otp_token}, timeout=10) as res:
                    if res.status == 200:
                        return True, await res.json()
                    return False, f"HTTP {res.status}"
            except Exception as e:
                return False, str(e)

def build_json(data: dict, phone: str):
    res_data = data.get("data", {})
    access_token = res_data.get("token", "")
    if not access_token: return None
    
    refresh_token = res_data.get("refresh_token", "")
    uid = res_data.get("user_id", 0)
    user_info = res_data.get("user_info", {})
    
    first_name = user_info.get('first_name', 'کاربر')
    last_name = user_info.get('last_name', 'جت')
    full_name = f"{first_name} {last_name}".strip()

    user_persist_obj = {
        "token": access_token,
        "refreshToken": refresh_token,
        "userId": uid,
        "info": {
            "userFullName": full_name, "userPhone": phone,
            "userFirstName": first_name, "userLastName": last_name,
            "userOngoingOrdersCount": 0, "userHeapId": str(uid),
            "userWelcomeReferralMessage": {}
        },
        "isQuantityTooltipVisited": False, "pendingReferralCodeCheck": "",
        "isBNPLTooltipVisited": False, "isPWABannerVisited": False,
        "isUserInitCompleted": True, "isRegister": False,
        "recentSearches": [], "favoriteSearches": [], "referralMessage": None,
        "appStyleMode": "jet", "externalToken": access_token,
        "plusMinimumPurchaseAmountRial": 1500000
    }
    
    persist_root_dict = {
        "user": json.dumps(user_persist_obj, ensure_ascii=False),
        "_persist": json.dumps({"version": -1, "rehydrated": True})
    }
    
    return {
        "cookies": [
            {"name": "token", "value": access_token, "domain": ".digikalajet.com", "path": "/", "secure": True, "sameSite": "unspecified"},
            {"name": "token", "value": access_token, "domain": ".www.digikalajet.com", "path": "/", "secure": True, "sameSite": "unspecified"}
        ],
        "origins": [{"origin": "https://www.digikalajet.com", "localStorage": [
            {"name": "persist:DKNow", "value": json.dumps(persist_root_dict, ensure_ascii=False)},
            {"name": "jet:app_id", "value": "8b62e987-34bf-48bd-bc62-347f38309a36"},
            {"name": "jet:browser-id-v3", "value": "FINGERPRINTV2-6a44d158446867e4502af048b412cc7d"}
        ]}]
    }

# ================= User Handlers =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.init_user(user.id, user.first_name or "کاربر")
    
    if await db.is_banned(user.id):
        return ConversationHandler.END

    used, max_l = await db.get_user_quota(user.id)
    rem = max(0, max_l - used)

    keyboard = [
        [InlineKeyboardButton("🔗 ساخت لینک اکانت", callback_data="btn_make_link")],
        [InlineKeyboardButton("📋 لینک‌های من", callback_data="btn_my_links"),
         InlineKeyboardButton("👤 حساب کاربری", callback_data="btn_my_account")]
    ]

    await update.message.reply_text(
        f"سلام {user.first_name} عزیز 🛒\n\n"
        f"به سیستم صدور پیوند ورود اختصاصی دیجی‌کالا جت خوش آمدید.\n"
        f"🔹 سهمیه باقی‌مانده شما: **{rem}** از **{max_l}** لینک\n\n"
        f"جهت ادامه یکی از گزینه‌های زیر را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

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
                f"❌ **سهمیه ساخت لینک شما به اتمام رسیده است!** ({used}/{max_l})\n"
                f"جهت شارژ مجدد سهمیه با پشتیبانی در ارتباط باشید.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        context.user_data.clear()
        await query.message.reply_text(
            "📱 **لطفاً شماره موبایل را وارد کنید:**\n(مثال: 09123456789)\n\n"
            "🔙 لغو: /cancel",
            parse_mode="Markdown"
        )
        return ASK_PHONE

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

        text = "📋 **پیوندهای ثبت شده توسط شما:**\n\n"
        for idx, item in enumerate(reversed(links[-10:]), 1):
            text += f"{idx}. شماره: `{item['phone']}`\n"
            text += f"🔗 پیوند: `{item['url']}`\n"
            text += f"⏳ انقضا: `{item['expire']}`\n"
            text += "──────────────────\n"

        await query.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if len(phone) != 11 or not phone.startswith("09") or not phone.isdigit():
        await update.message.reply_text("❌ فرمت شماره نامعتبر است. لطفاً شماره ۱۱ رقمی صحیح وارد کنید:")
        return ASK_PHONE

    msg = await update.message.reply_text("⏳ در حال ارسال پیامک...")
    jet = AsyncJetAuth()
    success, otp_token = await jet.request_otp(phone)
    
    if not success or not otp_token:
        await msg.edit_text(f"❌ خطا در ارسال پیامک:\n{otp_token}\n\nمجدداً شماره را وارد کنید:")
        return ASK_PHONE

    context.user_data['phone'] = phone
    context.user_data['otp_token'] = otp_token
    await msg.edit_text(f"✅ کد پیامک شد به `{phone}`.\n🔢 کد تایید را وارد کنید:", parse_mode="Markdown")
    return ASK_OTP

async def get_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    phone = context.user_data.get('phone')
    otp_token = context.user_data.get('otp_token')
    user_id = update.effective_user.id
    
    if not phone or not otp_token:
        await update.message.reply_text("❌ نشست منقضی شد. لطفاً از ابتدا اقدام کنید: /start")
        return ConversationHandler.END

    msg = await update.message.reply_text("⏳ در حال ساخت پیوند...")
    jet = AsyncJetAuth()
    success, response = await jet.confirm_phone(phone, code, otp_token)
    
    if not success:
        await msg.edit_text(f"❌ کد اشتباه یا منقضی است:\n{response}\n\nمجدداً کد را ارسال کنید:")
        return ASK_OTP

    result_json = build_json(response, phone)
    if not result_json:
        await msg.edit_text("❌ خطایی رخ داد. شماره در دیجی‌کالا ثبت‌نام نشده است.")
        return ConversationHandler.END

    days = await db.get_expiry_days()
    session_token = secrets.token_urlsafe(14)
    await db.save_session(session_token, result_json, days=days)

    login_url = f"{WEBHOOK_URL}/auth/{session_token}"
    
    # ذخیره و اعمال کسر از سهمیه
    await db.add_link_count(user_id)
    await db.add_log(user_id, phone)
    await db.save_user_created_link(user_id, phone, login_url, days)

    used, max_l = await db.get_user_quota(user_id)
    rem = max(0, max_l - used)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 ساخت لینک بعدی", callback_data="flow_next")],
        [InlineKeyboardButton("❌ پایان ساخت لینک", callback_data="flow_finish")]
    ])

    await msg.delete()
    await update.message.reply_text(
        f"🎉 **پیوند اختصاصی با موفقیت ساخته شد!**\n\n"
        f"📱 شماره: `{phone}`\n"
        f"⏳ **اعتبار پیوند:** {days} روز ({'۱ ماه' if days == 30 else '۲ ماه'})\n"
        f"📊 **سهمیه باقیمانده:** {rem} لینک\n\n"
        f"🔗 **پیوند اختصاصی جهت ورود:**\n`{login_url}`\n\n"
        f"*(پیوند بالا را در اپلیکیشن اختصاصی کپی و پیست کنید)*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    return ACTION_CHOICE

async def handle_action_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "flow_next":
        used, max_l = await db.get_user_quota(user_id)
        if used >= max_l:
            await query.message.reply_text(
                f"❌ **سقف مجاز ساخت لینک شما به پایان رسید!** ({used}/{max_l})\n"
                f"امکان ساخت لینک بیشتر وجود ندارد.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        context.user_data.clear()
        await query.message.reply_text(
            "📱 **شماره موبایل بعدی را وارد کنید:**\n(مثال: 09123456789)",
            parse_mode="Markdown"
        )
        return ASK_PHONE
        
    elif query.data == "flow_finish":
        context.user_data.clear()
        await query.message.reply_text(
            "✅ **فرآیند به پایان رسید.**\nبا دستور /start به منوی اصلی بازگردید.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ عملیات لغو شد. برای شروع: /start")
    return ConversationHandler.END

# ================= Admin Panel (Protected) =================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # کاملاً مخفی برای افراد غیر از ادمین
    if update.effective_user.id != ADMIN_ID:
        return

    days = await db.get_expiry_days()
    expiry_text = "۱ ماهه (۳۰ روز)" if days == 30 else "۲ ماهه (۶۰ روز)"
    default_l = await db.get_default_limit()
    
    keyboard = [
        [InlineKeyboardButton("📊 آمار کلی ربات", callback_data="adm_stats")],
        [InlineKeyboardButton(f"⏳ اعتبار پیش‌فرض: {expiry_text} (تغییر)", callback_data="adm_toggle_exp")],
        [InlineKeyboardButton(f"🌐 سهمیه پیش‌فرض: {default_l} لینک (تغییر)", callback_data="adm_set_def_limit")],
        [InlineKeyboardButton("⚙️ تنظیم سهمیه یک کاربر خاص", callback_data="adm_set_user_limit")],
        [InlineKeyboardButton("🚫 مسدود کردن کاربر", callback_data="adm_ban"), InlineKeyboardButton("✅ رفع مسدودی کاربر", callback_data="adm_unban")]
    ]
    await update.message.reply_text("⚙️ **پنل مدیریت پیشرفته ربات:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        return
    await query.answer()
    
    if query.data == "adm_stats":
        total, banned, links = await db.get_stats()
        await query.edit_message_text(f"📊 **آمار سیستم:**\n\n👥 کاربران: {total}\n🚫 مسدود شده‌ها: {banned}\n🔗 کل پیوندهای تولید شده: {links}", parse_mode="Markdown")
    elif query.data == "adm_toggle_exp":
        current = await db.get_expiry_days()
        new_days = 60 if current == 30 else 30
        await db.set_expiry_days(new_days)
        new_text = "۱ ماهه (۳۰ روز)" if new_days == 30 else "۲ ماهه (۶۰ روز)"
        await query.edit_message_text(f"✅ اعتبار پیوندهای جدید به **{new_text}** تغییر یافت.", parse_mode="Markdown")
    elif query.data == "adm_set_def_limit":
        await query.message.reply_text("🔢 سهمیه پیش‌فرض جدید برای تمام کاربران جدید را وارد کنید:")
        return ADMIN_SET_DEFAULT_LIMIT
    elif query.data == "adm_set_user_limit":
        await query.message.reply_text("👤 آیدی عددی (Chat ID) کاربر را بفرستید:")
        return ADMIN_SET_USER_LIMIT_ID
    elif query.data == "adm_ban":
        await query.message.reply_text("🚫 آیدی عددی کاربر برای مسدود شدن را بفرستید:")
        return ADMIN_BAN
    elif query.data == "adm_unban":
        await query.message.reply_text("✅ آیدی عددی کاربر برای رفع مسدودی را بفرستید:")
        return ADMIN_UNBAN

# هندلرهای تنظیمات ادمین
async def adm_save_default_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.isdigit():
        await db.set_default_limit(int(text))
        await update.message.reply_text(f"✅ سهمیه پیش‌فرض کاربران به {text} تغییر یافت.")
    else:
        await update.message.reply_text("❌ مقدار باید عددی باشد.")
    return ConversationHandler.END

async def adm_get_user_for_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.isdigit():
        context.user_data['target_uid'] = text
        await update.message.reply_text(f"🔢 اکنون سقف لینک مجاز برای کاربر {text} را وارد کنید:")
        return ADMIN_SET_USER_LIMIT_VAL
    await update.message.reply_text("❌ آیدی باید عددی باشد.")
    return ConversationHandler.END

async def adm_save_user_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    uid = context.user_data.get('target_uid')
    if val.isdigit() and uid:
        await db.set_user_limit(int(uid), int(val))
        await update.message.reply_text(f"✅ سقف لینک کاربر {uid} به {val} لینک تغییر کرد.")
    else:
        await update.message.reply_text("❌ ورودی نامعتبر.")
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
        await update.message.reply_text(f"✅ کاربر {uid} رفع مسدودی شد.")
    return ConversationHandler.END

# ================= Secure Gateway Web Route =================
async def web_telegram_webhook(request: web.Request):
    app = request.app["bot_app"]
    try:
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.update_queue.put(update)
    except Exception as e:
        logger.error(f"Webhook Error: {e}")
    return web.Response(text="OK")

async def web_secure_gateway(request: web.Request):
    """
    دروازه امن تحویل سشن:
    - اگر از طریق مرورگر عادی باز شود: هیچ اطلاعاتی نمایش داده نمی‌شود.
    - اگر از طریق اپلیکیشن درخواست داده شود: دیتای JSON تحویل داده می‌شود.
    """
    token_key = request.match_info.get("token")
    session_data = await db.get_session(token_key)

    if not session_data:
        # نمایش صفحه خطای رسمی بدون هیچ ردپایی
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

    # بررسی هدرهای کلاینت: آیا درخواست از اپلیکیشن است یا مرورگر؟
    user_agent = request.headers.get("User-Agent", "")
    app_header = request.headers.get("X-Client-App", "")

    # اگر درخواست از اپلیکیشن رسمی ما باشد (ارسال دیتای JSON خالص)
    if app_header == APP_SECRET_HEADER or "JetAppClient" in user_agent:
        return web.json_response({"status": "success", "session": session_data})

    # اگر کاربر لینک را در مرورگر (کروم، فایرفاکس، سافاری و...) باز کند:
    # یک صفحه مسدودساز شیک بدون حتی ۱ بایت از اطلاعات سشن نمایش داده می‌شود
    html_browser_blocked = """
    <!DOCTYPE html>
    <html dir="rtl" lang="fa">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>دسترسی فقط از طریق اپلیکیشن</title>
        <style>
            body { font-family: Tahoma, -apple-system, sans-serif; background-color: #f1f5f9; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
            .box { background: #ffffff; padding: 35px 25px; border-radius: 16px; box-shadow: 0 10px 25px rgba(0, 0, 0, 0.05); text-align: center; max-width: 400px; width: 85%; }
            .icon { font-size: 45px; margin-bottom: 15px; }
            h3 { color: #0f172a; margin: 0 0 12px; font-size: 17px; }
            p { color: #64748b; font-size: 14px; line-height: 1.7; margin: 0; }
        </style>
    </head>
    <body>
        <div class="box">
            <div class="icon">🔒</div>
            <h3>دسترسی مستقیم مسدود است</h3>
            <p>این پیوند صرفاً برای اجرا در <b>اپلیکیشن اختصاصی</b> طراحی شده است و امکان مشاهده مستقیم آن در مرورگر وجود ندارد.<br><br>لطفاً پیوند را کپی کرده و در اپلیکیشن وارد نمایید.</p>
        </div>
    </body>
    </html>
    """
    return web.Response(text=html_browser_blocked, content_type="text/html")

# ================= Runner =================
async def main():
    bot_app = Application.builder().token(TOKEN).build()

    user_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(user_menu_callback, pattern="^btn_make_link$")],
        states={
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
            ASK_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_otp)],
            ACTION_CHOICE: [CallbackQueryHandler(handle_action_choice, pattern="^flow_")]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern="^adm_")],
        states={
            ADMIN_SET_DEFAULT_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_save_default_limit)],
            ADMIN_SET_USER_LIMIT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_get_user_for_limit)],
            ADMIN_SET_USER_LIMIT_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_save_user_limit)],
            ADMIN_BAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_handle_ban)],
            ADMIN_UNBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_handle_unban)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("admin", admin_panel))
    bot_app.add_handler(user_conv)
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
    logger.info(f"🚀 Gateway Server started on port {PORT}")

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
