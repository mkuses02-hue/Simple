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

# Example:
# BASE_TEXT=AsocksAvi01@gmail.com
BASE_TEXT = os.environ.get("BASE_TEXT", "YourTextHere")
VARIATION_COUNT = int(os.environ.get("VARIATION_COUNT", "10"))

OTP_KEYWORDS = (
    "verification", "verify", "verification code", "otp",
    "one-time", "security code", "confirmation code", "login code"
)

seen_uids = set()
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
                    chunks.append(
                        payload.decode(
                            part.get_content_charset() or "utf-8",
                            errors="ignore"
                        )
                    )
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            chunks.append(
                payload.decode(
                    msg.get_content_charset() or "utf-8",
                    errors="ignore"
                )
            )

    return "\n".join(chunks)


def extract_code(text):
    labelled = re.search(
        r"(?:verification|security|confirmation|login|one[- ]time|otp)"
        r"(?:\s+code)?\s*[:#-]?\s*([0-9]{4,8})\b",
        text,
        re.IGNORECASE
    )

    if labelled:
        return labelled.group(1)

    match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", text)
    return match.group(1) if match else None


# IMPORTANT:
# Only alphabetic characters change case.
# Digits, dots, @, spaces, hyphens, etc. NEVER change.
def make_text_variation(text):
    output = []

    for char in text:
        if char.isalpha():
            output.append(
                char.upper()
                if random.getrandbits(1)
                else char.lower()
            )
        else:
            output.append(char)

    return "".join(output)


def create_variations(text, count):
    variations = []
    seen = set()
    attempts = 0

    while len(variations) < count and attempts < count * 100:
        variation = make_text_variation(text)

        if variation not in seen:
            seen.add(variation)
            variations.append(variation)

        attempts += 1

    return variations


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


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if update.effective_chat.id != CHAT_ID:
        return

    if not is_new_update(update):
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔤 Generate Variations",
                callback_data="generate_variations"
            )
        ]
    ])

    await update.message.reply_text(
        "🤖 Gmail Bot\n\nChoose an option:",
        reply_markup=keyboard
    )


async def generate_variations_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if query.message.chat_id != CHAT_ID:
        await query.answer()
        return

    await query.answer("Generating...")

    variations = create_variations(
        BASE_TEXT,
        VARIATION_COUNT
    )

    # One variation per line.
    # Backticks only format the text; there are no individual buttons.
    lines = []

    for variation in variations:
        safe = variation.replace("`", "")
        lines.append(f"`{safe}`")

    await query.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown"
    )


def gmail_check_sync():
    mail = None

    try:
        mail = imaplib.IMAP4_SSL(
            "imap.gmail.com",
            timeout=12
        )

        mail.login(GMAIL, APP_PASSWORD)
        mail.select("INBOX", readonly=True)

        status, data = mail.uid(
            "search",
            None,
            "UNSEEN"
        )

        if status != "OK":
            return []

        codes = []

        for raw_uid in data[0].split():

            uid = int(raw_uid)

            if uid in seen_uids:
                continue

            seen_uids.add(uid)

            status, msg_data = mail.uid(
                "fetch",
                raw_uid,
                "(RFC822)"
            )

            if status != "OK" or not msg_data:
                continue

            raw = next(
                (
                    item[1]
                    for item in msg_data
                    if isinstance(item, tuple)
                ),
                None
            )

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
                codes.append(code)

        return codes

    except Exception as exc:
        print(
            f"Gmail error: {exc}",
            flush=True
        )
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
            codes = await asyncio.to_thread(
                gmail_check_sync
            )

            for code in codes:

                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "📋 Copy",
                            copy_text={"text": code}
                        )
                    ]
                ])

                await app.bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"Code: {code}",
                    reply_markup=keyboard
                )

        except Exception as exc:
            print(
                f"OTP worker error: {exc}",
                flush=True
            )

        # Gmail checker is independent from Telegram polling.
        await asyncio.sleep(2)


async def post_init(app):

    global bot_started_at

    # Remove updates that accumulated while the bot was offline.
    await app.bot.delete_webhook(
        drop_pending_updates=True
    )

    # Only messages received after this moment are accepted.
    bot_started_at = datetime.now(timezone.utc)

    app.create_task(
        otp_worker(app)
    )

    print(
        f"Bot ready: {bot_started_at.isoformat()}",
        flush=True
    )


def main():

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            generate_variations_callback,
            pattern=r"^generate_variations$"
        )
    )

    print(
        "Telegram bot starting...",
        flush=True
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
