import os, time, json, threading, logging
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from functools import wraps

import requests
import psycopg
from psycopg_pool import ConnectionPool
from flask import Flask, jsonify
import pyotp

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('alpha')

BOT_TOKEN = "8943388643:AAFYLrveqsYlmZpGpvsDYsKhiwWQy8LFIe0"
ADMIN_ID = 8067626951
LAMIX_TOKEN = "sNf7xjfQxZOfNLjAgmYOm8xmCcQuHRRa5zpmjAViQeE"
DATABASE_URL = 
LAMIX_BASE = "https://panel.lamix.org/api/v1/"
PORT = int(os.getenv('PORT', '10000'))

TG = f'https://api.telegram.org/bot{BOT_TOKEN}'

# -------------------- Telegram --------------------
def tg(method, payload=None, timeout=35):
    r = requests.post(f'{TG}/{method}', json=payload or {}, timeout=timeout)
    try: data = r.json()
    except Exception: data = {'ok': False, 'description': r.text[:300]}
    if not data.get('ok'):
        log.warning('Telegram %s failed: %s', method, data.get('description'))
    return data

def send(chat_id, text, markup=None):
    p={'chat_id':chat_id,'text':text,'parse_mode':'HTML','disable_web_page_preview':True}
    if markup: p['reply_markup']=markup
    return tg('sendMessage', p)

def edit(chat_id, msg_id, text, markup=None):
    p={'chat_id':chat_id,'message_id':msg_id,'text':text,'parse_mode':'HTML','disable_web_page_preview':True}
    if markup: p['reply_markup']=markup
    return tg('editMessageText', p)

def answer(qid, text='', alert=False):
    return tg('answerCallbackQuery', {'callback_query_id':qid,'text':text,'show_alert':alert})

# -------------------- DB --------------------
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=True)

def db_init():
    with pool.connection() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS users(
            telegram_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
            balance NUMERIC(18,4) NOT NULL DEFAULT 0, referred_by BIGINT,
            referral_paid BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
        c.execute('''CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS services(
            id BIGSERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, enabled BOOLEAN NOT NULL DEFAULT TRUE,
            price NUMERIC(18,4) NOT NULL DEFAULT 0, quantity INTEGER NOT NULL DEFAULT 1)''')
        c.execute('''CREATE TABLE IF NOT EXISTS orders(
            id BIGSERIAL PRIMARY KEY, telegram_id BIGINT NOT NULL, msisdn TEXT NOT NULL,
            range_id BIGINT, service TEXT, price NUMERIC(18,4) NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            released_at TIMESTAMPTZ)''')
        c.execute('''CREATE TABLE IF NOT EXISTS wallet_transactions(
            id BIGSERIAL PRIMARY KEY, telegram_id BIGINT NOT NULL, amount NUMERIC(18,4) NOT NULL,
            type TEXT NOT NULL, note TEXT, ref TEXT UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
        c.execute('''CREATE TABLE IF NOT EXISTS referrals(
            id BIGSERIAL PRIMARY KEY, referrer BIGINT NOT NULL, referred BIGINT UNIQUE NOT NULL,
            bonus NUMERIC(18,4) NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(telegram_id,status)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_wallet_user ON wallet_transactions(telegram_id,created_at DESC)''')
        for k,v in {
            'support_id':'Not configured', 'main_channel':'', 'referral_bonus':'0',
            'commission_percent':'0', 'default_quantity':'1'
        }.items():
            c.execute('INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING',(k,v))

def setting(k, default=''):
    with pool.connection() as c:
        r=c.execute('SELECT value FROM settings WHERE key=%s',(k,)).fetchone()
        return r[0] if r else default

def set_setting(k,v):
    with pool.connection() as c:
        c.execute('INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',(k,str(v)))

def user_upsert(u, ref=None):
    uid=int(u['id']); username=u.get('username'); first=u.get('first_name','User')
    with pool.connection() as c:
        exists=c.execute('SELECT referred_by FROM users WHERE telegram_id=%s',(uid,)).fetchone()
        if exists is None:
            rb = ref if ref and ref != uid else None
            c.execute('INSERT INTO users(telegram_id,username,first_name,referred_by) VALUES(%s,%s,%s,%s)',(uid,username,first,rb))
            if rb:
                bonus=Decimal(setting('referral_bonus','0'))
                c.execute('INSERT INTO referrals(referrer,referred,bonus) VALUES(%s,%s,%s) ON CONFLICT(referred) DO NOTHING',(rb,uid,bonus))
                if bonus>0:
                    refstr=f'signup:{uid}'
                    try:
                        c.execute('INSERT INTO wallet_transactions(telegram_id,amount,type,note,ref) VALUES(%s,%s,%s,%s,%s)',(rb,bonus,'referral','Signup referral bonus',refstr))
                        c.execute('UPDATE users SET balance=balance+%s WHERE telegram_id=%s',(bonus,rb))
                    except psycopg.errors.UniqueViolation: c.rollback()
        else:
            c.execute('UPDATE users SET username=%s,first_name=%s,updated_at=now() WHERE telegram_id=%s',(username,first,uid))

def balance(uid):
    with pool.connection() as c:
        r=c.execute('SELECT balance FROM users WHERE telegram_id=%s',(uid,)).fetchone()
        return Decimal(r[0]) if r else Decimal('0')

def add_balance(uid, amount, typ, note='', ref=None):
    amount=Decimal(str(amount))
    with pool.connection() as c:
        if ref:
            old=c.execute('SELECT 1 FROM wallet_transactions WHERE ref=%s',(ref,)).fetchone()
            if old: return False
        c.execute('UPDATE users SET balance=balance+%s,updated_at=now() WHERE telegram_id=%s',(amount,uid))
        c.execute('INSERT INTO wallet_transactions(telegram_id,amount,type,note,ref) VALUES(%s,%s,%s,%s,%s)',(uid,amount,typ,note,ref))
        return True

# -------------------- Lamix --------------------
class Lamix:
    def __init__(self):
        self.s=requests.Session(); self.s.headers.update({'Authorization':f'Bearer {LAMIX_TOKEN}','Accept':'application/json'})
        self.lock=threading.Lock(); self.last=0.0
    def request(self, method, path, **kwargs):
        with self.lock:
            wait=1.05-(time.monotonic()-self.last)
            if wait>0: time.sleep(wait)
            self.last=time.monotonic()
        url=LAMIX_BASE+'/'+path.lstrip('/')
        for attempt in range(4):
            r=self.s.request(method,url,timeout=30,**kwargs)
            if r.status_code==429:
                time.sleep(min(int(r.headers.get('Retry-After','2')),10)); continue
            if r.status_code>=500:
                time.sleep(2**attempt); continue
            if not r.ok:
                try: detail=r.json()
                except Exception: detail=r.text[:500]
                raise RuntimeError(f'Lamix HTTP {r.status_code}: {detail}')
            return r.json()
        raise RuntimeError('Lamix API unavailable after retries')
    def ranges(self): return self.request('GET','ranges')
    def numbers(self, **params): return self.request('GET','numbers',params=params)
    def messages(self, **params): return self.request('GET','messages',params=params)
    def cdrs(self, **params): return self.request('GET','cdrs',params=params)
    def clients(self, **params): return self.request('GET','clients',params=params)
    def create_client(self, payload): return self.request('POST','clients',json=payload)
    def assign(self, payload): return self.request('POST','numbers/assign',json=payload)
    def unassign(self, payload): return self.request('POST','numbers/unassign',json=payload)
lamix=Lamix()

# -------------------- UI --------------------
def main_kb(admin=False):
    rows=[[{'text':'🟢 Get Number'},{'text':'🟢 2FA'}],[{'text':'🔵 Traffic'},{'text':'🔵 Wallet'}],[{'text':'🔴 Invite'},{'text':'🔴 Support'}]]
    if admin: rows.append([{'text':'🟢 AdminPanel'}])
    return {'keyboard':rows,'resize_keyboard':True}

def inline(rows): return {'inline_keyboard':rows}

def admin_kb():
    return inline([
        [{'text':'📱 Numbers','callback_data':'a_numbers'},{'text':'🌎 Ranges','callback_data':'a_ranges'}],
        [{'text':'💵 CDR','callback_data':'a_cdr'},{'text':'👥 Clients','callback_data':'a_clients'}],
        [{'text':'📊 Statistics','callback_data':'a_stats'},{'text':'⚙️ Settings','callback_data':'a_settings'}],
    ])

# -------------------- Helpers --------------------
def short(v,n=3800):
    s=str(v); return s if len(s)<=n else s[:n]+'…'

def is_admin(uid): return int(uid)==ADMIN_ID

def safe_decimal(s):
    try:return Decimal(str(s))
    except InvalidOperation:return Decimal('0')

# -------------------- Handlers --------------------
def show_ranges(chat_id, mid=None):
    try:
        data=lamix.ranges(); rows=[]
        for r in data.get('ranges', data if isinstance(data,list) else []):
            rid=r.get('id'); name=r.get('name',rid); avail=r.get('unassignedNumbers',r.get('numbers','?'))
            rows.append([{'text':f'{name} • {avail}', 'callback_data':f'range:{rid}'}])
        text='🌎 <b>Available Ranges</b>\n\nSelect a range.' if rows else '❌ No ranges returned.'
        if mid: edit(chat_id,mid,text,inline(rows))
        else: send(chat_id,text,inline(rows))
    except Exception as e: send(chat_id,f'❌ Lamix error: <code>{short(e,500)}</code>')

def show_numbers(chat_id, mid=None, range_id=None):
    try:
        p={'limit':50,'assigned':'false'}
        if range_id is not None:p['rangeId']=range_id
        data=lamix.numbers(**p); nums=data.get('numbers', data if isinstance(data,list) else [])
        text='📱 <b>Numbers</b>\n\n'+('\n'.join(f'• <code>{n.get("msisdn",n.get("number","?"))}</code>' for n in nums[:80]) or 'No unassigned numbers.')
        if mid: edit(chat_id,mid,short(text),inline([[{'text':'🔄 Refresh','callback_data':'a_numbers'}],[{'text':'⬅️ Admin','callback_data':'a_home'}]]))
        else: send(chat_id,short(text),inline([[{'text':'🔄 Refresh','callback_data':'a_numbers'}]]))
    except Exception as e: send(chat_id,f'❌ Lamix error: <code>{short(e,500)}</code>')

def traffic(chat_id):
    try:
        data=lamix.messages(limit=20)
        msgs=data.get('messages',data if isinstance(data,list) else [])
        # Do not expose authentication codes. Only metadata is shown.
        lines=[]
        for m in msgs[:20]:
            num=m.get('number') or m.get('to') or m.get('from') or '?'
            ts=m.get('createdAt') or m.get('timestamp') or ''
            lines.append(f'• <code>{num}</code> — {ts}')
        send(chat_id,'🔵 <b>Traffic</b>\n\n'+('\n'.join(lines) if lines else 'No recent traffic.'))
    except Exception as e: send(chat_id,f'❌ Traffic error: <code>{short(e,500)}</code>')

def wallet(chat_id,uid):
    with pool.connection() as c:
        rows=c.execute('SELECT amount,type,note,created_at FROM wallet_transactions WHERE telegram_id=%s ORDER BY created_at DESC LIMIT 10',(uid,)).fetchall()
    lines=[f'• {r[1]}: {r[0]} — {r[2] or ""}' for r in rows]
    send(chat_id,f'💰 <b>Wallet</b>\n\nBalance: <code>{balance(uid):.4f}</code>\n\n'+'\n'.join(lines))

def admin_stats(chat_id):
    with pool.connection() as c:
        users=c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        orders=c.execute("SELECT COUNT(*) FROM orders WHERE status='active'").fetchone()[0]
        bal=c.execute('SELECT COALESCE(SUM(balance),0) FROM users').fetchone()[0]
    send(chat_id,f'📊 <b>Statistics</b>\n\nUsers: <code>{users}</code>\nActive orders: <code>{orders}</code>\nUser balances: <code>{bal}</code>',admin_kb())

def handle_message(m):
    if m.get('chat',{}).get('type')!='private': return
    uid=int(m['from']['id']); chat=m['chat']['id']; text=(m.get('text') or '').strip()
    if text.startswith('/start'):
        p=text.split(); ref=int(p[1]) if len(p)>1 and p[1].isdigit() else None
        user_upsert(m['from'],ref)
        send(chat,'👋 <b>Welcome to AlphaWorkers</b>\n\nChoose an option:',main_kb(is_admin(uid))); return
    user_upsert(m['from'])
    if text=='🟢 Get Number':
        send(chat,'📱 <b>Get Number</b>\n\nChoose a country/range:',inline([[{'text':'🌎 Browse Ranges','callback_data':'a_ranges'}]]))
    elif text=='🟢 2FA':
        send(chat,'🔐 <b>2FA</b>\n\nSend your TOTP secret (Base32) as a message. This is for accounts you are authorized to access.')
        states[uid]='2fa'
    elif text=='🔵 Traffic': traffic(chat)
    elif text=='🔵 Wallet': wallet(chat,uid)
    elif text=='🔴 Invite':
        me=tg('getMe').get('result',{}).get('username',''); send(chat,f'🔗 <b>Your referral link</b>\n<code>https://t.me/{me}?start={uid}</code>')
    elif text=='🔴 Support': send(chat,f'🆘 <b>Support</b>\n{setting("support_id","Not configured")}')
    elif text=='🟢 AdminPanel' and is_admin(uid): send(chat,'🛠 <b>Admin Panel</b>',admin_kb())
    elif states.get(uid)=='2fa':
        try:
            secret=text.replace(' ','').upper(); code=pyotp.TOTP(secret).now()
            send(chat,f'🔐 <b>Current TOTP</b>\n\n<code>{code}</code>\n\nValid for the current TOTP window.')
        except Exception: send(chat,'❌ Invalid TOTP secret. Please send a valid Base32 secret.')
        states.pop(uid,None)

def callback(q):
    uid=int(q['from']['id']); chat=q['message']['chat']['id']; mid=q['message']['message_id']; d=q.get('data','')
    if d.startswith('a_') or d in ('a_home',):
        if not is_admin(uid): answer(q['id'],'Not authorized.',True); return
    answer(q['id'])
    if d=='a_home': edit(chat,mid,'🛠 <b>Admin Panel</b>',admin_kb())
    elif d=='a_numbers': show_numbers(chat,mid)
    elif d=='a_ranges': show_ranges(chat,mid)
    elif d=='a_stats': admin_stats(chat)
    elif d=='a_clients':
        try:
            data=lamix.clients(limit=50); clients=data.get('clients',data if isinstance(data,list) else [])
            txt='👥 <b>Clients</b>\n\n'+('\n'.join(f'• {c.get("username","?")} — {c.get("status","")}' for c in clients) or 'No clients.')
            edit(chat,mid,short(txt),admin_kb())
        except Exception as e: edit(chat,mid,f'❌ <code>{short(e,500)}</code>',admin_kb())
    elif d=='a_cdr':
        try:
            data=lamix.cdrs(limit=20); rows=data.get('cdrs',data if isinstance(data,list) else [])
            txt='💵 <b>CDR</b>\n\n'+('\n'.join(f'• {r.get("number", "?")} — {r.get("duration", "?")} — {r.get("rate", "?")}' for r in rows) or 'No CDR records.')
            edit(chat,mid,short(txt),admin_kb())
        except Exception as e: edit(chat,mid,f'❌ <code>{short(e,500)}</code>',admin_kb())
    elif d=='a_settings':
        txt='⚙️ <b>Settings</b>\n\n'+ '\n'.join(f'• {k}: <code>{setting(k)}</code>' for k in ['support_id','main_channel','referral_bonus','commission_percent','default_quantity'])
        edit(chat,mid,txt,admin_kb())
    elif d.startswith('range:'):
        rid=d.split(':',1)[1]
        show_numbers(chat,mid,rid)

states={}

# -------------------- Polling + health --------------------
app=Flask(__name__)
@app.get('/')
def root(): return jsonify(ok=True,service='alpha-lamix-bot')
@app.get('/health')
def health(): return jsonify(ok=True,db=True,lamix=True)

def polling():
    offset=None
    while True:
        try:
            p={'timeout':25,'allowed_updates':['message','callback_query']}
            if offset is not None:p['offset']=offset
            r=requests.get(f'{TG}/getUpdates',params=p,timeout=35).json()
            for u in r.get('result',[]):
                offset=u['update_id']+1
                try:
                    if 'message' in u: handle_message(u['message'])
                    elif 'callback_query' in u: callback(u['callback_query'])
                except Exception: log.exception('update failed')
        except Exception: log.exception('polling failed'); time.sleep(3)

def main():
    db_init(); log.info('Bot starting')
    threading.Thread(target=polling,daemon=True).start()
    app.run(host='0.0.0.0',port=PORT,debug=False,use_reloader=False)

if __name__=='__main__': main()
