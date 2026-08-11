import os
import imaplib
import email
import re
import asyncio
import random
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

# Gmail UID watermark. Messages at or below this UID existed when the
# bot started and are NEVER forwarded by this process.
uid_watermark = 0
bot_started_at = None


def decode_header_value(value):
    if not value:
        return ""
    result = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="ignore"))
        else:
            result.append(part)
    return "".join(result)


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
    labelled = re.search(
        r"(?:verification|security|confirmation|login|one[- ]time|otp)"
        r"(?:\s+code)?\s*[:#-]?\s*([0-9]{4,8})\b",
        text, re.IGNORECASE
    )
    if labelled:
        return labelled.group(1)

    match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", text)
    return match.group(1) if match else None


def make_variation(text):
    # Only letters change. @, ., digits and every other character stay intact.
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

    message_date = message.date
    if message_date.tzinfo is None:
        message_date = message_date.replace(tzinfo=timezone.utc)

    return message_date >= bot_started_at


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

    await query.answer("Generating...")

    lines = [
        f"`{v.replace('`', '')}`"
        for v in create_variations(BASE_TEXT, VARIATION_COUNT)
    ]
    await query.message.reply_text("\n".join(lines), parse_mode="Markdown")


def get_current_uid_watermark():
    """Return the newest UID currently in INBOX."""
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=12)
        mail.login(GMAIL, APP_PASSWORD)
        mail.select("INBOX", readonly=True)

        status, data = mail.uid("search", None, "ALL")
        if status != "OK" or not data[0]:
            return 0

        return int(data[0].split()[-1])
    except Exception as exc:
        print(f"Initial Gmail watermark error: {exc}", flush=True)
        return 0
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


def gmail_check_sync():
    """Process only Gmail UIDs newer than the startup watermark."""
    global uid_watermark
    mail = None

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=12)
        mail.login(GMAIL, APP_PASSWORD)
        mail.select("INBOX", readonly=True)

        # UID search is used instead of UNSEEN. This means an old unread
        # message can never be sent just because it remains unread.
        status, data = mail.uid(
            "search", None, f"UID {uid_watermark + 1}:*"
        )
        if status != "OK":
            return []

        codes = []

        for raw_uid in data[0].split():
            uid = int(raw_uid)

            if uid <= uid_watermark:
                continue

            # Advance watermark even if this message is not an OTP.
            uid_watermark = max(uid_watermark, uid)

            status, msg_data = mail.uid(
                "fetch", raw_uid, "(RFC822)"
            )
            if status != "OK" or not msg_data:
                continue

            raw = next(
                (item[1] for item in msg_data if isinstance(item, tuple)),
                None
            )
            if not raw:
                continue

            msg = email.message_from_bytes(raw)
            subject = decode_header_value(msg.get("Subject", ""))
            body = get_text(msg)
            combined = f"{subject}\n{body}"

            if not any(
                k.lower() in combined.lower()
                for k in OTP_KEYWORDS
            ):
                continue

            code = extract_code(combined)
            if code:
                codes.append(code)

        return codes

    except Exception as exc:
        print(f"Gmail error: {exc}", flush=True)
        return []
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


async def otp_worker(app):
    while True:
        try:
            codes = await asyncio.to_thread(gmail_check_sync)

            for code in codes:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "📋 Copy",
                        copy_text={"text": code}
                    )
                ]])

                await app.bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"Code: {code}",
                    reply_markup=keyboard
                )

        except Exception as exc:
            print(f"OTP worker error: {exc}", flush=True)

        await asyncio.sleep(2)


async def post_init(app):
    global bot_started_at, uid_watermark

    # Drop old Telegram updates first.
    await app.bot.delete_webhook(drop_pending_updates=True)

    # IMPORTANT: establish the Gmail UID baseline BEFORE starting the worker.
    # Therefore all messages already in the inbox are ignored.
    uid_watermark = await asyncio.to_thread(get_current_uid_watermark)

    bot_started_at = datetime.now(timezone.utc)

    print(
        f"Bot ready. Gmail UID watermark={uid_watermark}. "
        f"Telegram cutoff={bot_started_at.isoformat()}",
        flush=True
    )

    app.create_task(otp_worker(app))


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(
        CallbackQueryHandler(
            generate_variations_callback,
            pattern=r"^generate_variations$"
        )
    )

    print("Telegram bot starting...", flush=True)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
