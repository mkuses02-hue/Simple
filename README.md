# Gmail → Telegram OTP notifier (Railway)

Use this only with a Gmail account and Telegram chat that you control.

## Railway Variables

Add these in Railway → Variables:

BOT_TOKEN=your_bot_token
GMAIL=Asocksavi01@gmail.com
APP_PASSWORD=your_gmail_app_password
CHAT_ID=your_telegram_chat_id

Do NOT commit the App Password or Bot Token to GitHub.

## Deploy

1. Create a GitHub repository.
2. Upload `bot.py` and `requirements.txt`.
3. Create a Railway project and deploy the GitHub repository.
4. Add the four Variables above.
5. Railway will install the requirements and run the bot.

If Railway asks for a start command, use:

python bot.py

The bot checks Gmail every 5 seconds and, when it finds a new unread verification/OTP email, sends:

Code: 123456

with a Copy button.
