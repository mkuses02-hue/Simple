import os
import imaplib
import email
import re
import asyncio
import random
import threading
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

bot_started_at = None
gmail_uid_watermark = 0
telegram_loop = None


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


def get_text(msg):
    chunks = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    chunks.append(payload.decode(
                        part.get_content_charset() or "utf-8",
                        errors="ignore"
                    ))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            chunks.append(payload.decode(
                msg.get_content_charset() or "utf-8",
                errors="ignore"
            ))
    return "\n".join(chunks)


def extract_code(text):
    # Prefer a code associated with an OTP/verification label.
    m = re.search(
        r"(?:verification|security|confirmation|login|one[- ]time|otp)"
        r"(?:\s+code)?\s*[:#-]?\s*([0-9]{4,8})\b",
        text,
        re.IGNORECASE
    )
    if m:
        return m.group(1)

    m = re.search(r"(?<!\d)(\d{4,8})(?!\d)", text)
    return m.group(1) if m else None


def make_variation(text):
    # Only letters change. @, ., digits and punctuation stay unchanged.
    return "".join(
        c.upper() if c.isalpha() and random.getrandbits(1)
        else c.lower() if c.isalpha()
        else c
        for c in text
    )


def create_variations(text, count):
    result, seen = [], set()
    attempts = 0
    while len(result) < count and attempts < count * 100:
        v = make_variation(text)
        if v not in seen:
            seen.add(v)
            result.append(v)
        attempts += 1
    return result


def is_new_update(update):
    if bot_started_at is None:
        return False
    message = update.effective_message
    if not message or not message.date:
        return False
    d = message.date
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d >= bot_started_at


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID or not is_new_update(update):
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🔤 Generate Variations",
            callback_data="generate_variations"
        )
    ]])

    await update.message.reply_text(
        "🤖 Gmail Bot\n\nChoose an option:",
        reply_markup=keyboard
    )


async def generate_variations_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.message.chat_id != CHAT_ID:
        await query.answer()
        return

    await query.answer()
    lines = [
        f"`{v.replace('`', '')}`"
        for v in create_variations(BASE_TEXT, VARIATION_COUNT)
    ]
    await query.message.reply_text("\n".join(lines), parse_mode="Markdown")


def get_startup_uid():
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=15)
        mail.login(GMAIL, APP_PASSWORD)
        mail.select("INBOX", readonly=True)
        status, data = mail.uid("search", None, "ALL")
        if status == "OK" and data[0]:
            return int(data[0].split()[-1])
        return 0
    except Exception as exc:
        print(f"Startup Gmail UID error: {exc}", flush=True)
        return 0
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


def fetch_uid(mail, uid):
    status, data = mail.uid("fetch", str(uid), "(RFC822)")
    if status != "OK" or not data:
        return None

    raw = next(
        (item[1] for item in data if isinstance(item, tuple)),
        None
    )
    if not raw:
        return None

    return email.message_from_bytes(raw)


def process_new_uids(mail):
    global gmail_uid_watermark

    status, data = mail.uid("search", None, f"UID {gmail_uid_watermark + 1}:*")
    if status != "OK":
        return []

    codes = []

    for raw_uid in data[0].split():
        uid = int(raw_uid)
        if uid <= gmail_uid_watermark:
            continue

        # Advance the watermark for every new message, so each UID is handled once.
        gmail_uid_watermark = max(gmail_uid_watermark, uid)

        msg = fetch_uid(mail, uid)
        if not msg:
            continue

        subject = decode_header_value(msg.get("Subject", ""))
        body = get_text(msg)
        combined = f"{subject}\n{body}"

        if not any(k.lower() in combined.lower() for k in OTP_KEYWORDS):
            continue

        code = extract_code(combined)
        if code:
            codes.append(code)

    return codes


async def send_otp(code):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "📋 Copy",
            copy_text={"text": code}
        )
    ]])

    await application.bot.send_message(
        chat_id=CHAT_ID,
        text=f"Code: {code}",
        reply_markup=keyboard
    )


def schedule_code(code):
    if telegram_loop and not telegram_loop.is_closed():
        asyncio.run_coroutine_threadsafe(send_otp(code), telegram_loop)


def imap_idle_worker():
    """
    Dedicated blocking thread for IMAP.
    Telegram's asyncio loop is never blocked by Gmail.
    Gmail IDLE wakes this thread when INBOX changes.
    """
    global gmail_uid_watermark

    while True:
        mail = None
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
            mail.login(GMAIL, APP_PASSWORD)
            mail.select("INBOX", readonly=True)

            print("Gmail IMAP connected; waiting for new mail...", flush=True)

            # Establish the watermark only on first startup.
            if gmail_uid_watermark == 0:
                status, data = mail.uid("search", None, "ALL")
                if status == "OK" and data[0]:
                    gmail_uid_watermark = int(data[0].split()[-1])
                    print(
                        f"Gmail startup watermark: {gmail_uid_watermark}",
                        flush=True
                    )

            while True:
                # First check for anything that arrived between IDLE cycles.
                for code in process_new_uids(mail):
                    schedule_code(code)

                # Enter IMAP IDLE. This blocks ONLY this dedicated thread.
                tag = mail._new_tag()
                mail.send(tag + b" IDLE\r\n")

                # Wait for Gmail to confirm IDLE.
                response = mail.readline()
                if not response:
                    raise ConnectionError("IMAP IDLE connection closed")

                # Gmail may keep IDLE open for about 29 minutes max.
                # Use a 25-minute cycle so we can cleanly refresh it.
                deadline = time.monotonic() + (25 * 60)

                while time.monotonic() < deadline:
                    mail.sock.settimeout(30)

                    try:
                        line = mail.readline()
                    except TimeoutError:
                        # Send a harmless NOOP while remaining in IDLE.
                        continue

                    if not line:
                        raise ConnectionError("IMAP connection closed")

                    # Any EXISTS/RECENT notification means the inbox changed.
                    if b" EXISTS" in line or b" RECENT" in line:
                        # Exit IDLE and fetch the new UID(s).
                        try:
                            mail.send(b"DONE\r\n")
                            mail.readline()
                        except Exception:
                            pass

                        # Tiny delay gives Gmail time to expose the new UID.
                        time.sleep(0.15)

                        for code in process_new_uids(mail):
                            schedule_code(code)

                        break

                else:
                    # Refresh IDLE before Gmail's timeout.
                    try:
                        mail.send(b"DONE\r\n")
                        mail.readline()
                    except Exception:
                        pass

        except Exception as exc:
            print(f"IMAP worker reconnecting: {exc}", flush=True)
            time.sleep(1)

        finally:
            if mail:
                try:
                    mail.logout()
                except Exception:
                    pass


async def post_init(app):
    global bot_started_at, telegram_loop

    # Discard Telegram updates queued before this process started.
    await app.bot.delete_webhook(drop_pending_updates=True)

    # Establish the Gmail baseline BEFORE starting the worker.
    gmail_uid_watermark = get_startup_uid()

    bot_started_at = datetime.now(timezone.utc)
    telegram_loop = asyncio.get_running_loop()

    print(
        f"Bot ready. Gmail UID watermark={gmail_uid_watermark}",
        flush=True
    )

    # Dedicated Gmail thread: never blocks Telegram polling.
    threading.Thread(
        target=imap_idle_worker,
        daemon=True,
        name="gmail-imap-idle"
    ).start()


def main():
    global application
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )
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
