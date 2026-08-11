import os
import imaplib
import re
import asyncio
import random
import threading
import select
import socket
import time
from datetime import datetime, timezone
from email.header import decode_header

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
GMAIL = os.environ["GMAIL"]
APP_PASSWORD = os.environ["APP_PASSWORD"]
CHAT_ID = int(os.environ["CHAT_ID"])

BASE_TEXT = os.environ.get("BASE_TEXT", "YourTextHere")
VARIATION_COUNT = int(os.environ.get("VARIATION_COUNT", "10"))

OTP_KEYWORDS = (
    "verification", "verify", "verification code", "otp",
    "one-time", "security code", "confirmation code", "login code"
)

# Pre-compiled fast regexes
RE_OTP_KEYWORD = re.compile(
    r"(?:verification|security|confirmation|login|one[- ]time|otp)"
    r"(?:\s+code)?\s*[:#-]?\s*([0-9]{4,8})\b",
    re.IGNORECASE
)
RE_OTP_FALLBACK = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")

gmail_uid_watermark = 0
bot_started_at = None
telegram_loop = None
application = None


def decode_header_value(value):
    if not value:
        return ""
    out = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="ignore"))
        else:
            out.append(part)
    return "".join(out)


def extract_code(text):
    m = RE_OTP_KEYWORD.search(text)
    if m:
        return m.group(1)
    m = RE_OTP_FALLBACK.search(text)
    return m.group(1) if m else None


def make_variation(text):
    return "".join(
        c.upper() if c.isalpha() and random.getrandbits(1)
        else c.lower() if c.isalpha()
        else c
        for c in text
    )


def create_variations(text, count):
    result, seen = [], set()
    for _ in range(count * 100):
        if len(result) >= count:
            break
        v = make_variation(text)
        if v not in seen:
            seen.add(v)
            result.append(v)
    return result


def is_new_update(update):
    if bot_started_at is None:
        return False
    msg = update.effective_message
    if not msg or not msg.date:
        return False
    d = msg.date
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d >= bot_started_at


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID or not is_new_update(update):
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔤 Generate Variations", callback_data="generate_variations")
    ]])

    await update.message.reply_text("🤖 Gmail Bot\n\nChoose an option:", reply_markup=keyboard)


async def generate_variations_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message.chat_id != CHAT_ID:
        await q.answer()
        return

    await q.answer()
    lines = [f"`{v.replace('`', '')}`" for v in create_variations(BASE_TEXT, VARIATION_COUNT)]
    await q.message.reply_text("\n".join(lines), parse_mode="Markdown")


def startup_uid():
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=10)
        mail.login(GMAIL, APP_PASSWORD)
        mail.select("INBOX", readonly=True)

        status, data = mail.uid("search", None, "ALL")
        if status == "OK" and data[0]:
            return int(data[0].split()[-1])
        return 0
    except Exception as e:
        print("Startup Gmail error:", e, flush=True)
        return 0
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


def fetch_and_process_new(mail):
    global gmail_uid_watermark

    status, data = mail.uid("search", None, f"UID {gmail_uid_watermark + 1}:*")
    if status != "OK" or not data[0]:
        return []

    uids = [int(u) for u in data[0].split() if int(u) > gmail_uid_watermark]
    if not uids:
        return []

    gmail_uid_watermark = max(uids)

    # Optimization: Batch fetch Subject & Text for ALL new UIDs in a SINGLE IMAP roundtrip
    uid_batch_str = ",".join(str(u) for u in uids)
    status, msg_data = mail.uid(
        "fetch", 
        uid_batch_str, 
        "(BODY.PEEK[HEADER.FIELDS (SUBJECT)] BODY.PEEK[TEXT])"
    )
    if status != "OK":
        return []

    codes = []
    
    # Process batch response rapidly
    current_text_block = []
    for item in msg_data:
        if isinstance(item, tuple):
            try:
                chunk = item[1].decode("utf-8", errors="ignore")
                current_text_block.append(chunk)
            except Exception:
                continue
        elif item == b')' or item == b'':
            if current_text_block:
                full_raw_text = "\n".join(current_text_block)
                current_text_block.clear()

                # Fast Keyword Filter
                if not any(k in full_raw_text.lower() for k in OTP_KEYWORDS):
                    continue

                # Extract Code
                code = extract_code(full_raw_text)
                if code:
                    codes.append(code)

    # Residual cleanup if data trailing
    if current_text_block:
        full_raw_text = "\n".join(current_text_block)
        if any(k in full_raw_text.lower() for k in OTP_KEYWORDS):
            code = extract_code(full_raw_text)
            if code:
                codes.append(code)

    return codes


def schedule_telegram_code(code):
    if telegram_loop and not telegram_loop.is_closed():
        asyncio.run_coroutine_threadsafe(send_telegram_code(code), telegram_loop)


async def send_telegram_code(code):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Copy", copy_text={"text": code})
    ]])
    await application.bot.send_message(
        chat_id=CHAT_ID,
        text=f"Code: {code}",
        reply_markup=keyboard
    )


def gmail_idle_worker():
    global gmail_uid_watermark

    while True:
        mail = None
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=15)
            
            # Disable Nagle's algorithm for immediate packet flushing
            mail.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            mail.login(GMAIL, APP_PASSWORD)
            mail.select("INBOX", readonly=True)

            print("Gmail IMAP connected (TCP_NODELAY active) — IDLE active", flush=True)

            while True:
                # Process any pending mail immediately
                for code in fetch_and_process_new(mail):
                    schedule_telegram_code(code)

                # Enter IMAP IDLE
                tag = mail._new_tag()
                mail.send(tag + b" IDLE\r\n")

                # Read continuation '+'
                response = mail.readline()
                if not response or not response.startswith(b"+"):
                    raise ConnectionError("IDLE response error")

                idle_until = time.monotonic() + 25 * 60

                while time.monotonic() < idle_until:
                    # Instant check on internal Python buffer
                    if hasattr(mail, '_file') and mail._file and mail._file.peek():
                        ready = True
                    else:
                        r, _, _ = select.select([mail.sock], [], [], 10)
                        ready = bool(r)

                    if not ready:
                        continue

                    line = mail.readline()
                    if not line:
                        raise ConnectionError("IMAP socket closed")

                    if b" EXISTS" in line or b" RECENT" in line:
                        # Exit IDLE instantly
                        mail.send(b"DONE\r\n")
                        
                        while True:
                            done_line = mail.readline()
                            if not done_line or done_line.startswith(tag):
                                break

                        # Instantly batch-fetch and process new code(s)
                        for code in fetch_and_process_new(mail):
                            schedule_telegram_code(code)

                        break  # Resume IDLE loop

                else:
                    # Send DONE before 25-min server timeout
                    mail.send(b"DONE\r\n")
                    while True:
                        done_line = mail.readline()
                        if not done_line or done_line.startswith(tag):
                            break

        except Exception as e:
            print(f"Gmail connection error: {e}. Reconnecting...", flush=True)
            time.sleep(0.5)

        finally:
            if mail:
                try:
                    mail.logout()
                except Exception:
                    pass


async def post_init(app):
    global bot_started_at, gmail_uid_watermark, telegram_loop

    await app.bot.delete_webhook(drop_pending_updates=True)
    gmail_uid_watermark = await asyncio.to_thread(startup_uid)

    bot_started_at = datetime.now(timezone.utc)
    telegram_loop = asyncio.get_running_loop()

    print(f"READY | Gmail UID baseline: {gmail_uid_watermark}", flush=True)

    threading.Thread(
        target=gmail_idle_worker,
        daemon=True,
        name="gmail-idle"
    ).start()


def main():
    global application

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(
        CallbackQueryHandler(
            generate_variations_callback,
            pattern=r"^generate_variations$"
        )
    )

    print("Telegram bot starting...", flush=True)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
