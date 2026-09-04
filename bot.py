import os
import re
import hmac
import hashlib
import logging
import threading
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone, timedelta

import requests
import pyotp
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, request, jsonify

# ============================================================
# Lamix Safe Management Bot
# - Telegram webhook + Flask
# - Firebase/Firestore
# - Lamix Agent API: ranges, numbers, clients, CDRs, messages
# - No automated third-party verification/OTP forwarding
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("lamix-bot")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
LAMIX_TOKEN = os.environ["LAMIX_API_TOKEN"]
LAMIX_BASE_URL = os.getenv("LAMIX_BASE_URL", "https://panel.lamix.org/api/v1").rstrip("/")
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
PORT = int(os.getenv("PORT", "10000"))
NUMBER_REQUEST_COUNT = max(1, min(6, int(os.getenv("NUMBER_REQUEST_COUNT", "1"))))
MESSAGE_POLL_SECONDS = int(os.getenv("MESSAGE_POLL_SECONDS", "20"))

app = Flask(__name__)

# ---------------- Firebase ----------------
if not firebase_admin._apps:
    if os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON"):
        import json
        service = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"])
        firebase_admin.initialize_app(credentials.Certificate(service))
    elif os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        firebase_admin.initialize_app(
            credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
        )
    else:
        firebase_admin.initialize_app()

db = firestore.client()

# ---------------- Telegram ----------------
TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
http = requests.Session()
http.headers.update({"User-Agent": "LamixSafeBot/1.0"})

def tg(method, payload=None, timeout=20):
    r = http.post(f"{TG_BASE}/{method}", json=payload or {}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data.get("result")

def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("sendMessage", payload)

def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
               "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("editMessageText", payload)

def answer_callback(callback_id, text=None, alert=False):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = alert
    try:
        return tg("answerCallbackQuery", payload)
    except Exception:
        return None

def set_webhook():
    payload = {"url": f"{PUBLIC_URL}/telegram/webhook"}
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET
    result = tg("setWebhook", payload)
    log.info("Webhook configured: %s", result)
    return result

# ---------------- Firestore helpers ----------------
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def dec(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")

def user_ref(uid):
    return db.collection("users").document(str(uid))

def get_user(uid):
    snap = user_ref(uid).get()
    return snap.to_dict() if snap.exists else None

def ensure_user(tg_user, referrer=None):
    uid = str(tg_user["id"])
    ref = user_ref(uid)
    snap = ref.get()
    if snap.exists:
        ref.set({
            "first_name": tg_user.get("first_name", ""),
            "last_name": tg_user.get("last_name", ""),
            "username": tg_user.get("username", ""),
            "updated_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)
        return snap.to_dict()

    data = {
        "telegram_id": int(uid),
        "first_name": tg_user.get("first_name", ""),
        "last_name": tg_user.get("last_name", ""),
        "username": tg_user.get("username", ""),
        "balance": "0.0000",
        "lifetime_earned": "0.0000",
        "referral_bonus_earned": "0.0000",
        "referral_commission_earned": "0.0000",
        "referred_by": str(referrer) if referrer and str(referrer) != uid else None,
        "created_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP,
        "blocked": False,
    }
    ref.set(data)
    if referrer and str(referrer) != uid:
        award_referral_join_bonus(str(referrer), uid)
    return data

def settings_ref():
    return db.collection("settings").document("main")

def get_settings():
    snap = settings_ref().get()
    if snap.exists:
        return snap.to_dict()
    defaults = {
        "main_channel": "",
        "support_id": "",
        "forward_group": "",
        "force_join_enabled": False,
        "force_join_channels": [],
        "withdraw_enabled": True,
        "min_withdraw": "10.0000",
        "withdraw_methods": ["bKash", "Nagad"],
        "referral_join_bonus": "0.0000",
        "referral_commission_percent": "0.0",
        "number_request_count": NUMBER_REQUEST_COUNT,
        "service_enabled": True,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    settings_ref().set(defaults)
    return defaults

def set_setting(key, value):
    settings_ref().set({key: value, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)

def transaction_ref():
    return db.collection("transactions")

def add_transaction(uid, kind, amount, meta=None):
    amount_s = f"{dec(amount):.4f}"
    transaction_ref().add({
        "user_id": str(uid),
        "kind": kind,
        "amount": amount_s,
        "meta": meta or {},
        "created_at": firestore.SERVER_TIMESTAMP,
    })

def change_balance(uid, amount, reason, meta=None):
    amount_d = dec(amount)
    ref = user_ref(uid)
    snap = ref.get()
    if not snap.exists:
        return False
    data = snap.to_dict()
    new_bal = dec(data.get("balance", "0")) + amount_d
    if new_bal < 0:
        return False
    updates = {"balance": f"{new_bal:.4f}", "updated_at": firestore.SERVER_TIMESTAMP}
    if amount_d > 0:
        lifetime = dec(data.get("lifetime_earned", "0")) + amount_d
        updates["lifetime_earned"] = f"{lifetime:.4f}"
    ref.set(updates, merge=True)
    add_transaction(uid, reason, amount_d, meta)
    return True

def award_referral_join_bonus(referrer_id, referred_id):
    s = get_settings()
    bonus = dec(s.get("referral_join_bonus", "0"))
    if bonus <= 0:
        return
    ref = user_ref(referrer_id)
    snap = ref.get()
    if not snap.exists:
        return
    data = snap.to_dict()
    already = dec(data.get("referral_bonus_earned", "0"))
    # Join bonus is one-time per referred user; transaction query is avoided
    # by a dedicated referral edge document.
    edge = db.collection("referrals").document(f"{referrer_id}_{referred_id}")
    if edge.get().exists:
        return
    edge.set({
        "referrer_id": str(referrer_id),
        "referred_id": str(referred_id),
        "type": "join_bonus",
        "amount": f"{bonus:.4f}",
        "created_at": firestore.SERVER_TIMESTAMP,
    })
    new_bonus = already + bonus
    user_ref(referrer_id).set({"referral_bonus_earned": f"{new_bonus:.4f}"}, merge=True)
    change_balance(referrer_id, bonus, "referral_join_bonus", {"referred_user": str(referred_id)})

# ---------------- Lamix API ----------------
class LamixAPI:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def get(self, path, params=None):
        r = self.s.get(f"{LAMIX_BASE_URL}/{path.lstrip('/')}", params=params, timeout=20)
        if r.status_code == 429:
            raise RuntimeError("Lamix rate limit reached; retry later.")
        if r.status_code >= 400:
            try:
                err = r.json().get("error", "unknown_error")
            except Exception:
                err = "http_error"
            raise RuntimeError(f"Lamix API error: {err}")
        return r.json()

    def post(self, path, body):
        r = self.s.post(f"{LAMIX_BASE_URL}/{path.lstrip('/')}", json=body, timeout=20)
        if r.status_code == 429:
            raise RuntimeError("Lamix rate limit reached; retry later.")
        if r.status_code >= 400:
            try:
                err = r.json().get("error", "unknown_error")
            except Exception:
                err = "http_error"
            raise RuntimeError(f"Lamix API error: {err}")
        return r.json()

    def messages(self, limit=50, **params):
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params["limit"] = min(1000, max(1, int(limit)))
        return self.get("/messages", params)

    def ranges(self):
        return self.get("/ranges")

    def numbers(self, limit=100, after=None, range_id=None, assigned=None, search=None):
        params = {"limit": min(500, max(1, int(limit)))}
        if after:
            params["after"] = after
        if range_id:
            params["rangeId"] = range_id
        if assigned is not None:
            params["assigned"] = str(bool(assigned)).lower()
        if search:
            params["search"] = search
        return self.get("/numbers", params)

    def cdrs(self, limit=100, **params):
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params["limit"] = min(500, max(1, int(limit)))
        return self.get("/cdrs", params)

    def clients(self, limit=100):
        return self.get("/clients", {"limit": min(500, max(1, int(limit)))})

    def assign(self, client, numbers, client_payout_rate):
        return self.post("/numbers/assign", {
            "client": client,
            "numbers": numbers[:1000],
            "clientPayoutRate": f"{dec(client_payout_rate):.4f}",
        })

    def unassign(self, numbers):
        return self.post("/numbers/unassign", {"numbers": numbers[:1000]})

lamix = LamixAPI(LAMIX_TOKEN)

# ---------------- Safe message redaction ----------------
CODE_RE = re.compile(r"(?<!\d)\d{3,8}(?!\d)")

def redact_sensitive_digits(text):
    # Do not expose likely verification codes in bot/group UI.
    def repl(m):
        token = m.group(0)
        if len(token) >= 4:
            return "•" * len(token)
        return token
    return CODE_RE.sub(repl, str(text or ""))

def mask_number(number):
    n = str(number).lstrip("+")
    if len(n) <= 6:
        return "*" * len(n)
    return f"{n[:3]}***{n[-3:]}"

# ---------------- Keyboards ----------------
def button(text, callback_data=None, url=None, style=None):
    b = {"text": text}
    if callback_data:
        b["callback_data"] = callback_data
    if url:
        b["url"] = url
    if style:
        b["style"] = style
    return b

def main_keyboard(uid):
    rows = [
        [button("Get Number", "user_numbers", style="success"),
         button("2FA", "twofa", style="success")],
        [button("Traffic", "traffic", style="primary"),
         button("Wallet", "wallet", style="primary")],
        [button("Invite", "invite", style="danger"),
         button("Support", "support", style="danger")],
    ]
    if uid == ADMIN_ID:
        rows.append([button("AdminPanel", "admin", style="success")])
    return {"inline_keyboard": rows}

def back_kb():
    return {"inline_keyboard": [[button("BACK", "home", style="danger")]]}

def admin_keyboard():
    return {"inline_keyboard": [
        [button("Dashboard", "adm_dashboard", style="primary"),
         button("Lamix Sync", "adm_sync", style="primary")],
        [button("Ranges", "adm_ranges", style="primary"),
         button("Numbers", "adm_numbers", style="primary")],
        [button("Clients", "adm_clients", style="primary"),
         button("Traffic", "adm_traffic", style="primary")],
        [button("Users", "adm_users", style="primary"),
         button("Wallet", "adm_wallet", style="primary")],
        [button("Settings", "adm_settings", style="success")],
        [button("Force Join", "adm_forcejoin", style="success"),
         button("Broadcast", "adm_broadcast", style="success")],
        [button("Home", "home", style="danger")],
    ]}

def settings_keyboard():
    return {"inline_keyboard": [
        [button("Main Channel", "set_main_channel", style="primary"),
         button("Support ID", "set_support", style="primary")],
        [button("Forward Group", "set_group", style="primary"),
         button("Min Withdraw", "set_min_withdraw", style="primary")],
        [button("Referral Bonus", "set_ref_bonus", style="primary"),
         button("Referral %", "set_ref_pct", style="primary")],
        [button("Number Count", "set_num_count", style="primary")],
        [button("Force Join", "adm_forcejoin", style="success")],
        [button("BACK", "admin", style="danger")],
    ]}

# ---------------- In-memory admin states ----------------
states = {}
state_lock = threading.Lock()

def set_state(uid, state):
    with state_lock:
        states[str(uid)] = state

def pop_state(uid):
    with state_lock:
        return states.pop(str(uid), None)

# ---------------- UI ----------------
def home_text(user):
    s = get_settings()
    name = user.get("first_name", "User")
    return (
        f"<b>Welcome, {name}!</b>\n\n"
        "Use the menu below.\n\n"
        f"📢 Channel: {'Configured' if s.get('main_channel') else 'Not configured'}\n"
        f"🛟 Support: {'Configured' if s.get('support_id') else 'Not configured'}"
    )

def handle_start(message):
    tg_user = message["from"]
    args = (message.get("text") or "").split(maxsplit=1)
    ref = None
    if len(args) == 2 and args[1].startswith("ref_"):
        ref = args[1][4:].strip()
        if not ref.isdigit():
            ref = None
    user = ensure_user(tg_user, ref)
    if user.get("blocked"):
        send_message(message["chat"]["id"], "Your account is blocked.")
        return
    send_message(message["chat"]["id"], home_text(user), main_keyboard(int(tg_user["id"])))

def handle_text(message):
    uid = int(message["from"]["id"])
    text = (message.get("text") or "").strip()
    st = pop_state(uid)
    if not st:
        return
    action = st.get("action")
    if uid != ADMIN_ID:
        return

    if action == "main_channel":
        set_setting("main_channel", text)
        send_message(uid, "Main channel updated.", settings_keyboard())
    elif action == "support":
        set_setting("support_id", text)
        send_message(uid, "Support ID updated.", settings_keyboard())
    elif action == "group":
        set_setting("forward_group", text)
        send_message(uid, "Forward group updated.", settings_keyboard())
    elif action == "min_withdraw":
        value = dec(text)
        if value < 0:
            send_message(uid, "Invalid amount.", settings_keyboard())
        else:
            set_setting("min_withdraw", f"{value:.4f}")
            send_message(uid, "Minimum withdrawal updated.", settings_keyboard())
    elif action == "ref_bonus":
        value = dec(text)
        if value < 0:
            send_message(uid, "Invalid amount.", settings_keyboard())
        else:
            set_setting("referral_join_bonus", f"{value:.4f}")
            send_message(uid, "Referral join bonus updated.", settings_keyboard())
    elif action == "ref_pct":
        value = dec(text)
        if value < 0 or value > 100:
            send_message(uid, "Enter a percentage from 0 to 100.", settings_keyboard())
        else:
            set_setting("referral_commission_percent", str(value))
            send_message(uid, "Referral commission percentage updated.", settings_keyboard())
    elif action == "num_count":
        try:
            n = int(text)
            if not 1 <= n <= 6:
                raise ValueError
            set_setting("number_request_count", n)
            send_message(uid, f"Default number count set to {n}.", settings_keyboard())
        except Exception:
            send_message(uid, "Enter an integer from 1 to 6.", settings_keyboard())
    elif action == "broadcast":
        # Admin can broadcast ordinary informational text.
        sent = 0
        for snap in db.collection("users").stream():
            d = snap.to_dict()
            if d.get("blocked"):
                continue
            try:
                send_message(int(snap.id), text)
                sent += 1
            except Exception:
                pass
        send_message(uid, f"Broadcast finished. Sent: {sent}", admin_keyboard())

def show_numbers(chat_id):
    try:
        data = lamix.numbers(limit=100)
        records = data.get("records", [])
        if not records:
            send_message(chat_id, "No Lamix numbers are currently visible.", back_kb())
            return
        # Safe inventory view: masked numbers only, grouped by range.
        groups = {}
        for r in records[:60]:
            groups.setdefault(r.get("range", "Unknown"), []).append(r)
        lines = ["<b>Available Lamix Inventory</b>", ""]
        for rng, nums in groups.items():
            lines.append(f"<b>{rng}</b> — {len(nums)} visible")
            for n in nums[:10]:
                lines.append(f"• {mask_number(n.get('number',''))} — {n.get('status','unknown')}")
        lines.append("")
        lines.append("Numbers are masked here; assignment controls are available to admins.")
        send_message(chat_id, "\n".join(lines), back_kb())
    except Exception as e:
        log.exception("numbers")
        send_message(chat_id, f"Could not load numbers: {e}", back_kb())

def show_traffic(chat_id):
    try:
        data = lamix.cdrs(limit=100)
        records = data.get("records", [])
        total = sum((dec(x.get("payout")) for x in records), Decimal("0"))
        cleared = sum(1 for x in records if x.get("status") == "cleared")
        send_message(
            chat_id,
            "<b>Traffic</b>\n\n"
            f"Records: {len(records)}\n"
            f"Cleared: {cleared}\n"
            f"Own payout in sample: {total:.4f}\n\n"
            "Detailed upstream earnings are never exposed to users.",
            back_kb(),
        )
    except Exception as e:
        send_message(chat_id, f"Traffic unavailable: {e}", back_kb())

def show_wallet(chat_id, uid):
    user = get_user(uid) or {}
    send_message(
        chat_id,
        "<b>Wallet</b>\n\n"
        f"Balance: <code>{dec(user.get('balance','0')):.4f}</code>\n"
        f"Lifetime earned: <code>{dec(user.get('lifetime_earned','0')):.4f}</code>\n"
        f"Referral bonus: <code>{dec(user.get('referral_bonus_earned','0')):.4f}</code>\n"
        f"Referral commission: <code>{dec(user.get('referral_commission_earned','0')):.4f}</code>",
        back_kb(),
    )

def show_invite(chat_id, uid):
    me = tg("getMe")
    username = me.get("username", "")
    link = f"https://t.me/{username}?start=ref_{uid}"
    send_message(
        chat_id,
        "<b>Invite</b>\n\n"
        f"Your referral link:\n<code>{link}</code>\n\n"
        "Referral join bonus and commission are controlled by the admin.",
        back_kb(),
    )

def show_support(chat_id):
    s = get_settings()
    target = s.get("support_id") or "Not configured"
    send_message(chat_id, f"<b>Support</b>\n\nContact: {target}", back_kb())

def show_2fa(chat_id):
    send_message(
        chat_id,
        "<b>2FA / TOTP</b>\n\n"
        "Send a TOTP secret that you are authorized to use. "
        "The secret is processed only for the current calculation and is not stored.\n\n"
        "Example format: <code>JBSWY3DPEHPK3PXP</code>",
        back_kb(),
    )

def calculate_totp(secret):
    secret = re.sub(r"\s+", "", secret).upper()
    if not re.fullmatch(r"[A-Z2-7]+=*", secret):
        raise ValueError("Invalid Base32 TOTP secret.")
    return pyotp.TOTP(secret).now()

def show_admin(chat_id):
    if chat_id != ADMIN_ID:
        return
    send_message(chat_id, "<b>AdminPanel</b>\n\nSelect an action.", admin_keyboard())

def admin_dashboard(chat_id):
    users = sum(1 for _ in db.collection("users").stream())
    s = get_settings()
    try:
        ranges = lamix.ranges().get("records", [])
        numbers = lamix.numbers(limit=1).get("count", 0)
    except Exception:
        ranges, numbers = [], "API error"
    send_message(
        chat_id,
        "<b>Dashboard</b>\n\n"
        f"Users: {users}\n"
        f"Ranges: {len(ranges)}\n"
        f"Numbers count: {numbers}\n"
        f"Default number count: {s.get('number_request_count', 1)}\n"
        f"Min withdrawal: {s.get('min_withdraw','0')}",
        admin_keyboard(),
    )

def admin_sync(chat_id):
    try:
        ranges = lamix.ranges().get("records", [])
        db.collection("cache").document("ranges").set({
            "records": ranges,
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
        # Pull all number pages safely.
        all_records, cursor = [], None
        for _ in range(100):
            page = lamix.numbers(limit=500, after=cursor)
            all_records.extend(page.get("records", []))
            cursor = page.get("nextCursor")
            if not cursor:
                break
        db.collection("cache").document("numbers").set({
            "records": all_records,
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
        send_message(chat_id, f"Sync complete.\nRanges: {len(ranges)}\nNumbers: {len(all_records)}", admin_keyboard())
    except Exception as e:
        send_message(chat_id, f"Sync failed: {e}", admin_keyboard())

def admin_ranges(chat_id):
    try:
        records = lamix.ranges().get("records", [])
        lines = ["<b>Ranges</b>", ""]
        for r in records[:50]:
            rates = ", ".join(
                f"{x.get('plan')}: {x.get('payoutRate')}"
                for x in r.get("rates", [])
            )
            lines.append(
                f"• <b>{r.get('name','Unknown')}</b> | "
                f"numbers={r.get('numbers',0)} | rates={rates or 'none'}"
            )
        send_message(chat_id, "\n".join(lines) or "No ranges.", admin_keyboard())
    except Exception as e:
        send_message(chat_id, f"Ranges unavailable: {e}", admin_keyboard())

def admin_numbers(chat_id):
    try:
        data = lamix.numbers(limit=50)
        lines = ["<b>Numbers</b>", ""]
        for r in data.get("records", []):
            lines.append(
                f"• {mask_number(r.get('number',''))} | {r.get('range','')} | "
                f"{r.get('status','')} | {r.get('plan','')}"
            )
        lines.append("")
        lines.append("Use Lamix assignment controls from your authorized admin tooling.")
        send_message(chat_id, "\n".join(lines), admin_keyboard())
    except Exception as e:
        send_message(chat_id, f"Numbers unavailable: {e}", admin_keyboard())

def admin_clients(chat_id):
    try:
        records = lamix.clients(limit=100).get("records", [])
        lines = ["<b>Clients</b>", ""]
        for c in records[:50]:
            lines.append(
                f"• {c.get('username','')} | {c.get('name','')} | "
                f"numbers={c.get('numbers',0)} | disabled={c.get('disabled',False)}"
            )
        send_message(chat_id, "\n".join(lines) or "No clients.", admin_keyboard())
    except Exception as e:
        send_message(chat_id, f"Clients unavailable: {e}", admin_keyboard())

def admin_traffic(chat_id):
    try:
        data = lamix.cdrs(limit=100)
        records = data.get("records", [])
        total = sum((dec(x.get("payout")) for x in records), Decimal("0"))
        cleared = sum(1 for x in records if x.get("status") == "cleared")
        send_message(
            chat_id,
            "<b>Admin Traffic</b>\n\n"
            f"Sample records: {len(records)}\n"
            f"Cleared: {cleared}\n"
            f"Own payout total in sample: {total:.4f}",
            admin_keyboard(),
        )
    except Exception as e:
        send_message(chat_id, f"Traffic unavailable: {e}", admin_keyboard())

def admin_users(chat_id):
    snaps = list(db.collection("users").limit(50).stream())
    lines = ["<b>Users</b>", ""]
    for snap in snaps:
        d = snap.to_dict()
        lines.append(
            f"• <code>{snap.id}</code> @{d.get('username','-')} "
            f"| balance={d.get('balance','0')}"
        )
    send_message(chat_id, "\n".join(lines), admin_keyboard())

def admin_wallet(chat_id):
    total = Decimal("0")
    count = 0
    for snap in db.collection("users").stream():
        count += 1
        total += dec(snap.to_dict().get("balance", "0"))
    send_message(chat_id, f"<b>Wallet Overview</b>\n\nUsers: {count}\nLiability: {total:.4f}", admin_keyboard())

def admin_settings(chat_id):
    s = get_settings()
    send_message(
        chat_id,
        "<b>Settings</b>\n\n"
        f"Main channel: {s.get('main_channel') or 'Not set'}\n"
        f"Support: {s.get('support_id') or 'Not set'}\n"
        f"Forward group: {s.get('forward_group') or 'Not set'}\n"
        f"Min withdraw: {s.get('min_withdraw')}\n"
        f"Referral bonus: {s.get('referral_join_bonus')}\n"
        f"Referral commission: {s.get('referral_commission_percent')}%\n"
        f"Number count: {s.get('number_request_count')}",
        settings_keyboard(),
    )

def admin_forcejoin(chat_id):
    s = get_settings()
    send_message(
        chat_id,
        "<b>Force Join</b>\n\n"
        f"Enabled: {s.get('force_join_enabled', False)}\n"
        f"Channels: {', '.join(s.get('force_join_channels', [])) or 'None'}",
        {"inline_keyboard": [
            [button(
                "Disable" if s.get("force_join_enabled") else "Enable",
                "toggle_forcejoin",
                style="danger" if s.get("force_join_enabled") else "success"
            )],
            [button("BACK", "admin", style="danger")]
        ]}
    )

# ---------------- Callback router ----------------
def callback_router(q):
    uid = int(q["from"]["id"])
    data = q.get("data", "")
    chat_id = q["message"]["chat"]["id"]
    message_id = q["message"]["message_id"]
    answer_callback(q["id"])

    if data == "home":
        user = get_user(uid) or {"first_name": q["from"].get("first_name", "User")}
        edit_message(chat_id, message_id, home_text(user), main_keyboard(uid))
    elif data == "user_numbers":
        show_numbers(chat_id)
    elif data == "twofa":
        show_2fa(chat_id)
        set_state(uid, {"action": "totp"})
    elif data == "traffic":
        show_traffic(chat_id)
    elif data == "wallet":
        show_wallet(chat_id, uid)
    elif data == "invite":
        show_invite(chat_id, uid)
    elif data == "support":
        show_support(chat_id)
    elif data == "admin" and uid == ADMIN_ID:
        show_admin(chat_id)
    elif uid == ADMIN_ID:
        if data == "adm_dashboard": admin_dashboard(chat_id)
        elif data == "adm_sync": admin_sync(chat_id)
        elif data == "adm_ranges": admin_ranges(chat_id)
        elif data == "adm_numbers": admin_numbers(chat_id)
        elif data == "adm_clients": admin_clients(chat_id)
        elif data == "adm_traffic": admin_traffic(chat_id)
        elif data == "adm_users": admin_users(chat_id)
        elif data == "adm_wallet": admin_wallet(chat_id)
        elif data == "adm_settings": admin_settings(chat_id)
        elif data == "adm_forcejoin": admin_forcejoin(chat_id)
        elif data == "adm_broadcast":
            set_state(uid, {"action": "broadcast"})
            send_message(chat_id, "Send the informational broadcast text.", back_kb())
        elif data == "toggle_forcejoin":
            s = get_settings()
            set_setting("force_join_enabled", not bool(s.get("force_join_enabled")))
            admin_forcejoin(chat_id)
        elif data == "set_main_channel":
            set_state(uid, {"action": "main_channel"})
            send_message(chat_id, "Send the main channel username/link.", settings_keyboard())
        elif data == "set_support":
            set_state(uid, {"action": "support"})
            send_message(chat_id, "Send support ID/link.", settings_keyboard())
        elif data == "set_group":
            set_state(uid, {"action": "group"})
            send_message(chat_id, "Send forward group chat ID.", settings_keyboard())
        elif data == "set_min_withdraw":
            set_state(uid, {"action": "min_withdraw"})
            send_message(chat_id, "Send minimum withdrawal amount.", settings_keyboard())
        elif data == "set_ref_bonus":
            set_state(uid, {"action": "ref_bonus"})
            send_message(chat_id, "Send referral join bonus amount.", settings_keyboard())
        elif data == "set_ref_pct":
            set_state(uid, {"action": "ref_pct"})
            send_message(chat_id, "Send referral commission percentage (0-100).", settings_keyboard())
        elif data == "set_num_count":
            set_state(uid, {"action": "num_count"})
            send_message(chat_id, "Send default number count (1-6).", settings_keyboard())

# ---------------- Update handling ----------------
def handle_update(update):
    if "callback_query" in update:
        callback_router(update["callback_query"])
        return
    message = update.get("message")
    if not message:
        return
    uid = int(message["from"]["id"])
    if message.get("text", "").startswith("/start"):
        handle_start(message)
        return
    with state_lock:
        st = states.get(str(uid))
    if st and st.get("action") == "totp":
        pop_state(uid)
        try:
            code = calculate_totp(message.get("text", ""))
            send_message(message["chat"]["id"], f"Current TOTP: <code>{code}</code>", back_kb())
        except Exception as e:
            send_message(message["chat"]["id"], f"TOTP error: {e}", back_kb())
        return
    if message.get("text"):
        handle_text(message)

# ---------------- Web endpoints ----------------
@app.get("/")
def index():
    return "Lamix Safe Bot OK"

@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "lamix-safe-bot", "time": now_iso()})

@app.post("/telegram/webhook")
def telegram_webhook():
    if WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied, WEBHOOK_SECRET):
            return jsonify({"ok": False}), 403
    update = request.get_json(silent=True)
    if not update:
        return jsonify({"ok": False}), 400
    try:
        handle_update(update)
    except Exception:
        log.exception("Update handling failed")
    return jsonify({"ok": True})

@app.post("/internal/set-webhook")
def internal_set_webhook():
    # Protect this endpoint with the same secret if enabled.
    if WEBHOOK_SECRET:
        supplied = request.headers.get("X-Webhook-Admin-Secret", "")
        if not hmac.compare_digest(supplied, WEBHOOK_SECRET):
            return jsonify({"ok": False}), 403
    try:
        return jsonify({"ok": True, "result": set_webhook()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def startup():
    try:
        get_settings()
        set_webhook()
    except Exception:
        log.exception("Startup initialization failed")

if __name__ == "__main__":
    startup()
    app.run(host="0.0.0.0", port=PORT)
