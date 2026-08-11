import os
import imaplib
import email
import re
import asyncio
import random
import time
from email.header import decode_header

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
GMAIL = os.environ["GMAIL"]
APP_PASSWORD = os.environ["APP_PASSWORD"]
CHAT_ID = int(os.environ["CHAT_ID"])

# Fixed text used for capitalization variations.
BASE_TEXT = os.environ.get("BASE_TEXT", "YourTextHere")
VARIATION_COUNT = int(os.environ.get("VARIATION_COUNT", "10"))

OTP_KEYWORDS = (
    "verification", "verify", "verification code", "otp",
    "one-time", "security code", "confirmation code", "login code"
)

# Only the worker touches these.
seen_uids = set()
worker_lock = asyncio.Lock()


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
        text,
        re.IGNORECASE,
    )
    if labelled:
        return labelled.group(1)

    match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", text)
    return match.group(1) if match else None


def make_variation(text):
    return "".join(
        c.upper() if c.isalpha() and random.getrandbits(1) else
        c.lower() if c.isalpha() else c
        for c in text
    )


def generate_variations(text):
    result = []
    seen = set()
    attempts = 0

    while len(result) < VARIATION_COUNT and attempts < VARIATION_COUNT * 50:
        v = make_variation(text)
        if v not in seen:
            seen.add(v)
            result.append(v)
        attempts += 1

    return result


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
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


async def generate_variations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.message.chat_id != CHAT_ID:
        await query.answer()
        return

    await query.answer()

    variations = generate_variations(BASE_TEXT)

    # Telegram Markdown inline-code formatting.
    # One variation per line, no individual buttons.
    text = "\n".join(
        f"`{v.replace('`', '')}`" for v in variations
    )

    await query.message.reply_text(text, parse_mode="Markdown")


def gmail_check_sync():
    """
    Short-lived IMAP connection.
    It runs only in a worker thread, so it cannot block Telegram polling.
    """
    mail = None

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=12)
        mail.login(GMAIL, APP_PASSWORD)
        mail.select("INBOX", readonly=True)

        status, data = mail.uid("search", None, "UNSEEN")
        if status != "OK":
            return []

        results = []

        for raw_uid in data[0].split():
            uid = int(raw_uid)

            if uid in seen_uids:
                continue

            seen_uids.add(uid)

            status, msg_data = mail.uid(
                "fetch", raw_uid, "(RFC822)"
            )
            if status != "OK" or not msg_data:
                continue

            raw = None
            for item in msg_data:
                if isinstance(item, tuple):
                    raw = item[1]
                    break

            if not raw:
                continue

            msg = email.message_from_bytes(raw)

            subject = decode_header_value(
                msg.get("Subject", "")
            )
            body = get_text(msg)
            combined = f"{subject}\n{body}"

            if not any(
                keyword.lower() in combined.lower()
                for keyword in OTP_KEYWORDS
            ):
                continue

            code = extract_code(combined)

            if code:
                results.append(code)

        return results

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
    """
    Runs independently from Telegram update handling.
    Telegram commands remain responsive even if Gmail is slow.
    """
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
            print(f"Worker error: {exc}", flush=True)

        # 2-second check interval.
        await asyncio.sleep(2)


async def post_init(app):
    # Independent asyncio task. Never blocks Telegram polling.
    app.create_task(otp_worker(app))


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        CallbackQueryHandler(
            generate_variations,
            pattern="^generate_variations$"
        )
    )

    print("Telegram bot started.", flush=True)
    print("Gmail worker started.", flush=True)

    # Telegram polling remains the main responsive loop.
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
