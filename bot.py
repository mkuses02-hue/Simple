import os
import imaplib
import email
import re
import asyncio
from email.header import decode_header

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
GMAIL = os.environ["GMAIL"]
APP_PASSWORD = os.environ["APP_PASSWORD"]
CHAT_ID = int(os.environ["CHAT_ID"])

IMAP_SERVER = "imap.gmail.com"

# Only process messages that look like verification/OTP emails.
OTP_KEYWORDS = (
    "verification",
    "verify",
    "verification code",
    "otp",
    "one-time",
    "security code",
    "confirmation code",
    "login code",
)

last_uid = 0


def decode_header_value(value):
    if not value:
        return ""
    parts = decode_header(value)
    result = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            result.append(part.decode(encoding or "utf-8", errors="ignore"))
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
                            errors="ignore",
                        )
                    )
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            chunks.append(
                payload.decode(
                    msg.get_content_charset() or "utf-8",
                    errors="ignore",
                )
            )

    return "\n".join(chunks)


def extract_code(text):
    # Prefer codes explicitly labelled as code/OTP.
    labelled = re.search(
        r"(?:verification|security|confirmation|login|one[- ]time|otp)"
        r"(?:\s+code)?\s*[:#-]?\s*([0-9]{4,8})\b",
        text,
        re.IGNORECASE,
    )
    if labelled:
        return labelled.group(1)

    # Fallback: a standalone 4-8 digit number.
    match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", text)
    return match.group(1) if match else None


def check_gmail():
    global last_uid

    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(GMAIL, APP_PASSWORD)
    mail.select("INBOX")

    status, data = mail.uid("search", None, "UNSEEN")
    if status != "OK":
        mail.logout()
        return None

    uids = data[0].split()

    for uid in reversed(uids):
        uid_num = int(uid)
        if uid_num <= last_uid:
            continue

        status, msg_data = mail.uid("fetch", uid, "(RFC822)")
        if status != "OK":
            continue

        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        subject = decode_header_value(msg.get("Subject", ""))
        body = get_text(msg)
        combined = f"{subject}\n{body}"

        last_uid = max(last_uid, uid_num)

        if any(keyword.lower() in combined.lower() for keyword in OTP_KEYWORDS):
            code = extract_code(combined)
            if code:
                mail.logout()
                return code

    mail.logout()
    return None


async def send_code(bot, code):
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "📋 Copy",
                copy_text=CopyTextButton(text=code),
            )
        ]]
    )

    await bot.send_message(
        chat_id=CHAT_ID,
        text=f"Code: {code}",
        reply_markup=keyboard,
    )


async def check_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        code = await asyncio.to_thread(check_gmail)
        if code:
            await send_code(context.bot, code)
    except Exception as exc:
        print("Gmail check error:", exc, flush=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    await update.message.reply_text("✅ OTP monitor is running.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))

    # Check the inbox every 5 seconds.
    app.job_queue.run_repeating(check_job, interval=5, first=2)

    print("Bot started.", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
