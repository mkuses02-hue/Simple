import os
import imaplib
import email
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

RE_OTP = re.compile(
    r"(?:verification|security|confirmation|login|one[- ]time|otp)"
    r"(?:\s+code)?\s*[:#-]?\s*([0-9]{4,8})\b",
    re.IGNORECASE,
)
RE_DIGITS = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
OTP_WORDS = re.compile(
    r"verification|verify|otp|one[- ]time|security|confirmation|login",
    re.IGNORECASE,
)

gmail_uid_watermark = 0
bot_started_at = None
telegram_loop = None
application = None


def decode_subject(value):
    if not value:
        return ""
    out = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="ignore"))
        else:
            out.append(part)
    return "".join(out)


def extract_text(msg):
    parts = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() != "text/plain":
                continue
            payload = part.get_payload(decode=True)
            if payload:
                parts.append(
                    payload.decode(
                        part.get_content_charset() or "utf-8",
                        errors="ignore",
                    )
                )
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            parts.append(
                payload.decode(
                    msg.get_content_charset() or "utf-8",
                    errors="ignore",
                )
            )

    return "\n".join(parts)


def extract_code(text):
    match = RE_OTP.search(text)
    if match:
        return match.group(1)

    match = RE_DIGITS.search(text)
    return match.group(1) if match else None


def make_variation(text):
    # Only letters change. @, ., digits and punctuation stay unchanged.
    return "".join(
        c.upper() if c.isalpha() and random.getrandbits(1)
        else c.lower() if c.isalpha()
        else c
        for c in text
    )


def create_variations(text, count):
    result = []
    seen = set()

    for _ in range(count * 100):
        if len(result) >= count:
            break

        value = make_variation(text)

        if value not in seen:
            seen.add(value)
            result.append(value)

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
    if update.effective_chat.id != CHAT_ID:
        return

    if not is_new_update(update):
        return

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "🔤 Generate Variations",
                callback_data="generate_variations",
            )
        ]]
    )

    await update.message.reply_text(
        "🤖 Gmail Bot\n\nChoose an option:",
        reply_markup=keyboard,
    )


async def generate_variations_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query.message.chat_id != CHAT_ID:
        await query.answer()
        return

    await query.answer()

    lines = [
        f"`{value.replace('`', '')}`"
        for value in create_variations(
            BASE_TEXT,
            VARIATION_COUNT,
        )
    ]

    await query.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


def get_startup_uid():
    mail = None

    try:
        mail = imaplib.IMAP4_SSL(
            "imap.gmail.com",
            timeout=10,
        )

        mail.sock.setsockopt(
            socket.IPPROTO_TCP,
            socket.TCP_NODELAY,
            1,
        )

        mail.login(GMAIL, APP_PASSWORD)
        mail.select("INBOX", readonly=True)

        status, data = mail.uid("search", None, "ALL")

        if status == "OK" and data[0]:
            return int(data[0].split()[-1])

        return 0

    except Exception as exc:
        print(
            f"Startup Gmail error: {exc}",
            flush=True,
        )
        return 0

    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


def fetch_new_messages(mail):
    """
    One UID SEARCH followed by one batched BODY.PEEK FETCH.
    We intentionally fetch headers + text for all new UIDs together.
    """
    global gmail_uid_watermark

    status, data = mail.uid(
        "search",
        None,
        f"UID {gmail_uid_watermark + 1}:*",
    )

    if status != "OK" or not data[0]:
        return []

    uids = [
        int(value)
        for value in data[0].split()
        if int(value) > gmail_uid_watermark
    ]

    if not uids:
        return []

    # Advance watermark immediately so a reconnect cannot duplicate them.
    gmail_uid_watermark = max(uids)

    # Single batched FETCH round-trip.
    uid_set = ",".join(map(str, uids))

    status, response = mail.uid(
        "fetch",
        uid_set,
        "(BODY.PEEK[])",
    )

    if status != "OK":
        return []

    codes = []

    # Each tuple returned by imaplib corresponds to a fetched message body.
    for item in response:
        if not isinstance(item, tuple) or len(item) < 2:
            continue

        raw = item[1]

        if not isinstance(raw, bytes):
            continue

        try:
            msg = email.message_from_bytes(raw)
        except Exception:
            continue

        subject = decode_subject(
            msg.get("Subject", "")
        )

        # Cheap subject-first filter avoids parsing full body when possible.
        if OTP_WORDS.search(subject):
            body = extract_text(msg)
            combined = subject + "\n" + body
        else:
            body = extract_text(msg)
            combined = subject + "\n" + body

            if not OTP_WORDS.search(combined):
                continue

        code = extract_code(combined)

        if code:
            codes.append(code)

    return codes


async def send_telegram_code(code):
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "📋 Copy",
                copy_text={"text": code},
            )
        ]]
    )

    await application.bot.send_message(
        chat_id=CHAT_ID,
        text=f"Code: {code}",
        reply_markup=keyboard,
    )


def schedule_code(code):
    loop = telegram_loop

    if loop and not loop.is_closed():
        asyncio.run_coroutine_threadsafe(
            send_telegram_code(code),
            loop,
        )


def process_new_mail(mail):
    try:
        codes = fetch_new_messages(mail)

        for code in codes:
            schedule_code(code)

    except Exception as exc:
        print(
            f"Fetch error: {exc}",
            flush=True,
        )


def gmail_idle_worker():
    """
    Persistent IMAP IDLE worker.
    Telegram asyncio loop is never blocked here.
    """
    global gmail_uid_watermark

    while True:
        mail = None

        try:
            mail = imaplib.IMAP4_SSL(
                "imap.gmail.com",
                timeout=20,
            )

            mail.sock.setsockopt(
                socket.IPPROTO_TCP,
                socket.TCP_NODELAY,
                1,
            )

            mail.login(
                GMAIL,
                APP_PASSWORD,
            )

            mail.select(
                "INBOX",
                readonly=True,
            )

            print(
                "Gmail IMAP connected — optimized IDLE active",
                flush=True,
            )

            while True:
                # Catch anything that arrived between cycles.
                process_new_mail(mail)

                tag = mail._new_tag()

                mail.send(
                    tag + b" IDLE\r\n"
                )

                response = mail.readline()

                if not response or not response.startswith(b"+"):
                    raise ConnectionError(
                        "IMAP IDLE handshake failed"
                    )

                # Refresh before Gmail's IDLE limit.
                deadline = time.monotonic() + (
                    24 * 60
                )

                while time.monotonic() < deadline:
                    ready, _, _ = select.select(
                        [mail.sock],
                        [],
                        [],
                        5,
                    )

                    if not ready:
                        continue

                    line = mail.readline()

                    if not line:
                        raise ConnectionError(
                            "IMAP connection closed"
                        )

                    if (
                        b" EXISTS" in line
                        or b" RECENT" in line
                    ):
                        # Leave IDLE immediately.
                        mail.send(
                            b"DONE\r\n"
                        )

                        while True:
                            done_line = mail.readline()

                            if not done_line:
                                raise ConnectionError(
                                    "IMAP DONE response missing"
                                )

                            if done_line.startswith(tag):
                                break

                        # No sleep here.
                        process_new_mail(mail)

                        break

                else:
                    # Periodic IDLE refresh.
                    mail.send(
                        b"DONE\r\n"
                    )

                    while True:
                        done_line = mail.readline()

                        if not done_line:
                            raise ConnectionError(
                                "IMAP refresh response missing"
                            )

                        if done_line.startswith(tag):
                            break

        except Exception as exc:
            print(
                f"Gmail connection error: {exc}",
                flush=True,
            )

            time.sleep(0.25)

        finally:
            if mail:
                try:
                    mail.logout()
                except Exception:
                    pass


async def post_init(app):
    global bot_started_at
    global gmail_uid_watermark
    global telegram_loop

    # Discard Telegram updates accumulated while offline.
    await app.bot.delete_webhook(
        drop_pending_updates=True
    )

    # Ignore all Gmail messages that existed before startup.
    gmail_uid_watermark = await asyncio.to_thread(
        get_startup_uid
    )

    bot_started_at = datetime.now(
        timezone.utc
    )

    telegram_loop = asyncio.get_running_loop()

    print(
        f"READY | Gmail UID baseline: "
        f"{gmail_uid_watermark}",
        flush=True,
    )

    threading.Thread(
        target=gmail_idle_worker,
        daemon=True,
        name="gmail-idle",
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
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            generate_variations_callback,
            pattern=r"^generate_variations$",
        )
    )

    print(
        "Telegram bot starting...",
        flush=True,
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
