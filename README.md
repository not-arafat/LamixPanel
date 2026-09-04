# Lamix Safe Firebase + Render Webhook Bot

This project is intentionally scoped to safe/authorized management features:
- Telegram webhook on Flask/Gunicorn
- Firebase/Firestore persistence
- Lamix Agent API integration for ranges, numbers, clients, CDR/traffic and message-management metadata
- Admin settings
- Referral join bonus accounting
- Wallet/transaction ledger
- TOTP/2FA utility for secrets the user is authorized to use
- Masked number inventory in the user UI
- No automated third-party account verification, OTP harvesting, or OTP forwarding

## Deploy on Render

1. Put these files in a Git repository.
2. Create a Render Web Service from the repository.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn -w 1 -b 0.0.0.0:$PORT bot:app`
5. Set all variables from `.env.example` in Render Environment.
6. Use a Firebase service account with only the permissions needed by the Firestore database.
7. Deploy. The app calls Telegram `setWebhook` at startup using `PUBLIC_URL`.

Render web services must listen on `0.0.0.0` and the `PORT` environment variable. The included Gunicorn command follows that requirement.

## Firebase

Enable Firestore in your Firebase/GCP project. Create a service account and put its JSON in `FIREBASE_SERVICE_ACCOUNT_JSON` on Render.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# export the variables from .env using your preferred tool
python bot.py
```

Do not commit `.env`, service-account JSON, or API tokens.

## Important

Rotate any Lamix or Telegram token that has previously been pasted into chat, source code, screenshots, logs, or public repositories.
