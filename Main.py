import os
import time
import threading
import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# =====================================================================
# ⚙️ কনফিগারেশন ও সিকিউরিটি সেটআপ (Env Variables)
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SMS_API_KEY = os.getenv("SMS_API_KEY", "").strip()

SERVICE = "cdy"          # Caddy সার্ভিস কোড
TIMEOUT_SECONDS = 130    # ১৩০ সেকেন্ড (২ মিনিট ১০ সেকেন্ড)
BASE_URL = "https://smsbower.page/stubs/handler_api.php"
ITEMS_PER_PAGE = 10      # প্রতি পেজে দেশের সংখ্যা

if not BOT_TOKEN or not SMS_API_KEY:
    print("⚠️ WARNING: BOT_TOKEN or SMS_API_KEY is not set properly in Environment Variables!")

bot = telebot.TeleBot(BOT_TOKEN)

# =====================================================================
# 🛠️ SMSBower API হেলপার ক্লাস (Optimized with requests.Session)
# =====================================================================
class SMSBowerAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        # Session ব্যবহার করলে API কল অনেক দ্রুত হবে
        self.session = requests.Session()

    def get_balance(self) -> float:
        """অ্যাকাউন্ট ব্যালেন্স ফেচ করে"""
        params = {"api_key": self.api_key, "action": "getBalance"}
        try:
            res = self.session.get(BASE_URL, params=params, timeout=10).text.strip()
            if "ACCESS_BALANCE:" in res:
                balance_str = res.split("ACCESS_BALANCE:")[1].strip()
                return float(balance_str)
            elif res == "BAD_KEY":
                print("❌ Error: Invalid API Key")
        except Exception as e:
            print(f"Balance Fetch Error: {e}")
        return 0.0

    def get_country_names(self) -> dict:
        """সব দেশের নাম ও আইডি নিয়ে আসে"""
        params = {"api_key": self.api_key, "action": "getCountries"}
        try:
            res = self.session.get(BASE_URL, params=params, timeout=10).json()
            country_dict = {}
            if isinstance(res, list):
                for c in res:
                    country_dict[str(c.get("id"))] = c.get("eng", f"Country {c.get('id')}")
            elif isinstance(res, dict):
                for cid, c in res.items():
                    if isinstance(c, dict):
                        country_dict[str(cid)] = c.get("eng", f"Country {cid}")
            return country_dict
        except Exception as e:
            print(f"Country Fetch Error: {e}")
            return {}

    def get_available_countries(self) -> list:
        """Caddy স্টকে থাকা দেশ ও প্রাইস লিস্ট আনে"""
        country_names = self.get_country_names()
        params = {"api_key": self.api_key, "action": "getPrices", "service": SERVICE}
        try:
            prices_data = self.session.get(BASE_URL, params=params, timeout=10).json()
        except Exception as e:
            print(f"Prices Fetch Error: {e}")
            return []

        available_list = []
        if isinstance(prices_data, dict):
            for c_id, services in prices_data.items():
                if isinstance(services, dict) and SERVICE in services:
                    cost = services[SERVICE].get("cost", 0)
                    count = services[SERVICE].get("count", 0)
                    if count > 0:
                        c_name = country_names.get(str(c_id), f"Country {c_id}")
                        available_list.append({
                            "id": str(c_id),
                            "name": c_name,
                            "cost": cost,
                            "count": count
                        })
        # দাম অনুযায়ী সর্টিং
        available_list.sort(key=lambda x: x["cost"])
        return available_list

    def get_number(self, country_id: str):
        """নাম্বার কেনা"""
        params = {
            "api_key": self.api_key,
            "action": "getNumber",
            "service": SERVICE,
            "country": country_id
        }
        try:
            res = self.session.get(BASE_URL, params=params, timeout=10).text.strip()
            if res.startswith("ACCESS_NUMBER:"):
                _, act_id, phone = res.split(":", 2)
                return act_id, phone
        except Exception as e:
            print(f"Get Number Error: {e}")
        return None, None

    def set_status(self, act_id: str, status: int) -> str:
        """স্ট্যাটাস পরিবর্তন (6 = Complete, 8 = Cancel)"""
        params = {
            "api_key": self.api_key,
            "action": "setStatus",
            "id": act_id,
            "status": status
        }
        try:
            return self.session.get(BASE_URL, params=params, timeout=10).text.strip()
        except Exception as e:
            print(f"Set Status Error: {e}")
            return ""

    def check_status(self, act_id: str) -> str:
        """OTP স্টেটাস চেক"""
        params = {"api_key": self.api_key, "action": "getStatus", "id": act_id}
        try:
            return self.session.get(BASE_URL, params=params, timeout=10).text.strip()
        except Exception as e:
            print(f"Check Status Error: {e}")
            return ""

# API ক্লাসের অবজেক্ট তৈরি
sms_api = SMSBowerAPI(SMS_API_KEY)

# =====================================================================
# 🤖 টেলিগ্রাম বোট হ্যান্ডলার
# =====================================================================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    balance = sms_api.get_balance()
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("💰 চেক ব্যালেন্স", callback_data="check_balance"),
        InlineKeyboardButton("📱 Caddy নাম্বার নিন", callback_data="caddypage_0")
    )
    
    welcome_text = (
        f"👋 **স্বাগতম! Caddy OTP বোট-এ।**\n\n"
        f"💳 **আপনার বর্তমান ব্যালেন্স:** `${balance:.4f}`\n\n"
        f"নিচের বাটন চেপে কাজ শুরু করুন:"
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=markup)

def show_country_page(chat_id, message_id, page: int):
    """পেজিনেশন সহ দেশ দেখানোর ফাংশন"""
    bot.edit_message_text("🔍 স্টকে থাকা দেশ ও প্রাইস লিস্ট লোড হচ্ছে...", chat_id, message_id)
    countries = sms_api.get_available_countries()
    
    if not countries:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔄 রিফ্রেশ করুন", callback_data="caddypage_0"))
        markup.add(InlineKeyboardButton("🔙 প্রধান মেনু", callback_data="main_menu"))
        bot.edit_message_text("❌ বর্তমানে Caddy এর জন্য কোনো দেশের নাম্বার স্টকে নেই।", chat_id, message_id, reply_markup=markup)
        return

    total_countries = len(countries)
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    current_page_countries = countries[start_idx:end_idx]

    markup = InlineKeyboardMarkup(row_width=1)
    for c in current_page_countries:
        btn_text = f"{c['name']} - ${c['cost']:.3f} (স্টক: {c['count']}টি)"
        markup.add(InlineKeyboardButton(btn_text, callback_data=f"getnum_{c['id']}"))

    # নেভিগেশন বাটন (Next / Previous)
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ পূর্ববর্তী", callback_data=f"caddypage_{page-1}"))
    if end_idx < total_countries:
        nav_buttons.append(InlineKeyboardButton("পরবর্তী ➡️", callback_data=f"caddypage_{page+1}"))
    
    if nav_buttons:
        markup.row(*nav_buttons)

    markup.add(InlineKeyboardButton("🔙 প্রধান মেনু", callback_data="main_menu"))
    
    msg_text = f"🌎 **কোন দেশের নাম্বার নিতে চান বেছে নিন:**\n*(Page {page+1} of {((total_countries - 1) // ITEMS_PER_PAGE) + 1})*"
    bot.edit_message_text(msg_text, chat_id, message_id, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    
    # ১. ব্যালেন্স বাটন
    if call.data == "check_balance":
        balance = sms_api.get_balance()
        bot.answer_callback_query(call.id, f"আপনার বর্তমান ব্যালেন্স: ${balance:.4f}", show_alert=True)

    # ২. দেশ পেজিনেশন লিস্ট
    elif call.data.startswith("caddypage_"):
        page = int(call.data.split("_")[1])
        show_country_page(chat_id, message_id, page)

    # ৩. নির্দিষ্ট দেশ সিলেক্ট করা
    elif call.data.startswith("getnum_"):
        country_id = call.data.split("_")[1]
        bot.answer_callback_query(call.id, "নাম্বার রিকোয়েস্ট করা হচ্ছে...")
        
        act_id, phone = sms_api.get_number(country_id)
        
        if not act_id:
            bot.send_message(chat_id, "❌ স্টক শেষ হয়ে গেছে বা নাম্বার পাওয়া যায়নি। অন্য দেশ চেষ্টা করুন।")
            return

        balance = sms_api.get_balance()
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("🔄 OTP চেক করুন", callback_data=f"checkotp_{act_id}"),
            InlineKeyboardButton("🚫 ক্যানসেল করুন", callback_data=f"cancel_{act_id}")
        )

        msg_text = (
            f"✅ **নাম্বার নেওয়া হয়েছে!**\n\n"
            f"📱 **নম্বর:** `+{phone}`\n"
            f"🆔 **ID:** `{act_id}`\n"
            f"💰 **অবশিষ্ট ব্যালেন্স:** `${balance:.4f}`\n\n"
            f"⏳ *বোট ব্যাকগ্রাউন্ডে ২ মিনিট ১০ সেকেন্ড OTP এর জন্য অপেক্ষা করছে...*"
        )
        # নতুন মেসেজ পাঠাবে, আগেরটা ডিলিট করে দিতে পারেন চাইলে
        bot.delete_message(chat_id, message_id)
        msg = bot.send_message(chat_id, msg_text, parse_mode="Markdown", reply_markup=markup)
        
        # থ্রেড দিয়ে ব্যাকগ্রাউন্ডে OTP পাওয়ার জন্য ওয়েট করা
        threading.Thread(target=auto_otp_worker, args=(chat_id, msg.message_id, act_id), daemon=True).start()

    # ৪. ম্যানুয়াল OTP চেক
    elif call.data.startswith("checkotp_"):
        act_id = call.data.split("_")[1]
        status = sms_api.check_status(act_id)
        if status.startswith("STATUS_OK:"):
            otp = status.split(":", 1)[1]
            sms_api.set_status(act_id, 6)
            balance = sms_api.get_balance()
            bot.edit_message_text(f"🎉 **OTP পেয়ে গেছেন:** `{otp}`\n\n💰 বর্তমান ব্যালেন্স: `${balance:.4f}`", chat_id, message_id, parse_mode="Markdown")
        else:
            bot.answer_callback_query(call.id, "এখনো OTP আসেনি! অপেক্ষা করুন...", show_alert=True)

    # ৫. ম্যানুয়াল ক্যানসেল
    elif call.data.startswith("cancel_"):
        act_id = call.data.split("_")[1]
        res = sms_api.set_status(act_id, 8)
        if res == "ACCESS_CANCEL":
            balance = sms_api.get_balance()
            bot.edit_message_text(f"🚫 নাম্বারটি ক্যানসেল করা হয়েছে। টাকা রিফান্ড দেওয়া হয়েছে।\n💰 নতুন ব্যালেন্স: `${balance:.4f}`", chat_id, message_id)
        elif res == "EARLY_CANCEL_DENIED":
            bot.answer_callback_query(call.id, "⚠️ নাম্বার নেওয়ার ২ মিনিটের মধ্যে ক্যানসেল করা যায় না!", show_alert=True)

    # ৬. প্রধান মেনু
    elif call.data == "main_menu":
        bot.delete_message(chat_id, message_id)
        send_welcome(call.message)

# =====================================================================
# 🔄 ব্যাকগ্রাউন্ড অটো থ্রেড (OTP Polling) - With Exception Handling
# =====================================================================
def auto_otp_worker(chat_id, message_id, act_id):
    try:
        start_time = time.time()
        while time.time() - start_time < TIMEOUT_SECONDS:
            status = sms_api.check_status(act_id)
            if status.startswith("STATUS_OK:"):
                otp = status.split(":", 1)[1]
                sms_api.set_status(act_id, 6)
                balance = sms_api.get_balance()
                bot.edit_message_text(
                    f"🎉 **OTP রিসিভ হয়েছে!**\n\n🔑 **OTP:** `{otp}`\n💰 **বর্তমান ব্যালেন্স:** `${balance:.4f}`",
                    chat_id, message_id, parse_mode="Markdown"
                )
                return
            elif status == "STATUS_CANCEL":
                return
            time.sleep(5)

        # সময় শেষ হলে ক্যানসেল করা
        cancel_res = sms_api.set_status(act_id, 8)
        balance = sms_api.get_balance()
        if cancel_res == "ACCESS_CANCEL":
            bot.edit_message_text(
                f"⏰ **টাইমআউট!** নির্দিষ্ট সময়ের মধ্যে OTP না আসায় নাম্বারটি ক্যানসেল করা হয়েছে।\n💰 **বর্তমান ব্যালেন্স:** `${balance:.4f}`",
                chat_id, message_id, parse_mode="Markdown"
            )
    except Exception as e:
        print(f"Background Thread Error: {e}")

# =====================================================================
# 🚀 বোট স্টার্ট
# =====================================================================
if __name__ == "__main__":
    print("🤖 Telegram Bot is starting...")
    bot.infinity_polling(timeout=20, long_polling_timeout=10)
