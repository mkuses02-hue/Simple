import os
import imaplib
import email
import re
import asyncio
import random
from email.header import decode_header

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

BOT_TOKEN = os.environ["BOT_TOKEN"]
GMAIL = os.environ["GMAIL"]
APP_PASSWORD = os.environ["APP_PASSWORD"]
CHAT_ID = int(os.environ["CHAT_ID"])

# Put the fixed text you want variations of here.
BASE_TEXT = os.environ.get("BASE_TEXT", "YourTextHere")

OTP_KEYWORDS = (
    "verification", "verify", "verification code", "otp",
    "one-time", "security code", "confirmation code", "login code"
)

# How many capitalization variations to show.
VARIATION_COUNT = 10

last_uid = 0


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
    labelled = re.search(
        r"(?:verification|security|confirmation|login|one[- ]time|otp)"
        r"(?:\s+code)?\s*[:#-]?\s*([0-9]{4,8})\b",
        text, re.IGNORECASE
    )
    if labelled:
        return labelled.group(1)

    m = re.search(r"(?<!\d)(\d{4,8})(?!\d)", text)
    return m.group(1) if m else None


def make_variation(text):
    chars = []
    for c in text:
        if c.isalpha():
            chars.append(c.upper() if random.choice([True, False]) else c.lower())
        else:
            chars.append(c)
    return "".join(chars)


def generate_variations(text, count=VARIATION_COUNT):
    seen = set()
    attempts = 0
    while len(seen) < count and attempts < count * 20:
        seen.add(make_variation(text))
        attempts += 1
    return list(seen)


def variation_message():
    variations = generate_variations(BASE_TEXT)
    # Each variation is on its own line and wrapped in backticks.
    # No separate copy buttons.
    return "\n".join(f"`{v}`" for v in variations)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔤 Generate Variations", callback_data="variations")
    ]])

    await update.message.reply_text(
        "🤖 Gmail Bot\n\nChoose an option:",
        reply_markup=keyboard
    )


async def variations_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.message.chat_id != CHAT_ID:
        return

    await query.message.reply_text(
        variation_message(),
        parse_mode="Markdown"
    )


def fetch_unseen_once():
    global last_uid

    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL, APP_PASSWORD)
    mail.select("INBOX")

    status, data = mail.uid("search", None, "UNSEEN")
    if status != "OK":
        mail.logout()
        return []

    found = []

    for uid in data[0].split():
        uid_num = int(uid)
        if uid_num <= last_uid:
            continue

        status, msg_data = mail.uid("fetch", uid, "(RFC822)")
        if status != "OK":
            continue

        msg = email.message_from_bytes(msg_data[0][1])
        subject = decode_header_value(msg.get("Subject", ""))
        body = get_text(msg)
        combined = f"{subject}\n{body}"

        last_uid = max(last_uid, uid_num)

        if any(k.lower() in combined.lower() for k in OTP_KEYWORDS):
            code = extract_code(combined)
            if code:
                found.append(code)

    mail.logout()
    return found


async def otp_worker(app):
    """
    Fast mailbox checker. It uses a persistent IMAP connection and IDLE
    when the server supports it, with a short fallback poll.
    """
    global last_uid

    while True:
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(GMAIL, APP_PASSWORD)
            mail.select("INBOX")

            # Establish the current UID baseline so old unread messages
            # are not dumped into Telegram on startup.
            status, data = mail.uid("search", None, "ALL")
            if status == "OK" and data[0]:
                last_uid = int(data[0].split()[-1])

            while True:
                # Gmail IMAP IDLE: wake quickly when the mailbox changes.
                try:
                    tag = mail._new_tag()
                    mail.send(tag + b" IDLE\r\n")
                    mail.readline()  # + idling response

                    loop = asyncio.get_running_loop()
                    changed = await asyncio.wait_for(
                        loop.run_in_executor(None, mail.readline),
                        timeout=25
                    )

                    mail.send(b"DONE\r\n")
                    mail.readline()

                    if changed:
                        # Give Gmail a moment to finish indexing the message.
                        await asyncio.sleep(0.5)

                except asyncio.TimeoutError:
                    try:
                        mail.send(b"DONE\r\n")
                        mail.readline()
                    except Exception:
                        pass

                # Process newly arrived unread messages.
                codes = await asyncio.to_thread(fetch_unseen_once)
                for code in codes:
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("📋 Copy", copy_text={"text": code})
                    ]])
                    await app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"Code: {code}",
                        reply_markup=keyboard
                    )

        except Exception as exc:
            print("IMAP worker error:", exc, flush=True)
            await asyncio.sleep(2)


async def post_init(app):
    app.create_task(otp_worker(app))


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(
        variations_callback, pattern="^variations$"
    ))

    print("Bot started.", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
