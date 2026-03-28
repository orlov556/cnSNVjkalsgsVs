import os
import asyncio
import sqlite3
import time
import aiohttp
import logging
import threading
import shutil
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional, Dict, Tuple, List
from contextlib import contextmanager
from flask import Flask, jsonify
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# -------------------- КОНФИГ --------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_IDS", "").split(",") if id.strip()]
WELCOME_IMAGE_URL = os.environ.get("WELCOME_IMAGE_URL", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "cryptohelp_01")

# Кошельки
WALLETS = {
    'ton': os.environ.get("WALLET_TON", ""),
    'usdt_ton': os.environ.get("WALLET_USDT_TON", ""),
    'usdt_trc20': os.environ.get("WALLET_USDT_TRC20", ""),
}

if not BOT_TOKEN:
    logging.error("❌ BOT_TOKEN не задан!")
    exit(1)

# Минимальные суммы
MIN_EXCHANGE_AMOUNTS = {
    'ton': 1.0,
    'usdt_ton': 1.0,
    'usdt_trc20': 1.0
}

MIN_WITHDRAWAL = float(os.environ.get("MIN_WITHDRAWAL", "100"))
MAX_WITHDRAWAL = float(os.environ.get("MAX_WITHDRAWAL", "100000"))
RATE_UPDATE_INTERVAL = 30
DEPOSIT_CHECK_INTERVAL = 60
BACKUP_INTERVAL = 86400

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# -------------------- БАЗА ДАННЫХ --------------------
DB_PATH = "exchange.db"
_local = threading.local()

@contextmanager
def get_db():
    if not hasattr(_local, 'conn'):
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.c = _local.conn.cursor()
    try:
        yield _local.conn, _local.c
    except Exception:
        _local.conn.rollback()
        raise

def init_db():
    with get_db() as (conn, c):
        c.execute('''CREATE TABLE IF NOT EXISTS users (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     user_id INTEGER UNIQUE,
                     username TEXT,
                     balance REAL DEFAULT 0,
                     ref_by INTEGER DEFAULT 0,
                     ref_bonus REAL DEFAULT 0,
                     is_banned INTEGER DEFAULT 0,
                     created_at INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS deposits (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     user_id INTEGER,
                     crypto TEXT,
                     memo TEXT,
                     amount REAL DEFAULT 0,
                     status TEXT,
                     tx_hash TEXT,
                     created_at INTEGER,
                     completed_at INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS withdrawals (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     user_id INTEGER,
                     amount REAL,
                     details TEXT,
                     status TEXT,
                     admin_comment TEXT,
                     created_at INTEGER,
                     processed_at INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS exchange_rates (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     crypto TEXT UNIQUE,
                     rate_rub REAL,
                     updated_at INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS referral_earnings (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     user_id INTEGER,
                     from_user_id INTEGER,
                     amount REAL,
                     created_at INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS settings (
                     key TEXT PRIMARY KEY,
                     value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admin_logs (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     admin_id INTEGER,
                     action TEXT,
                     target_user INTEGER,
                     details TEXT,
                     created_at INTEGER)''')
        
        for idx in [
            "CREATE INDEX IF NOT EXISTS idx_users_user_id ON users (user_id)",
            "CREATE INDEX IF NOT EXISTS idx_deposits_memo ON deposits (memo)",
            "CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals (status)",
        ]:
            try:
                c.execute(idx)
            except:
                pass
        
        defaults = [("commission", "7"), ("referral_percent", "1"),
                    ("min_withdrawal", str(MIN_WITHDRAWAL)), ("max_withdrawal", str(MAX_WITHDRAWAL))]
        for k, v in defaults:
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        
        for crypto, rate in [('ton', 500), ('usdt_ton', 85), ('usdt_trc20', 85)]:
            c.execute("INSERT OR IGNORE INTO exchange_rates (crypto, rate_rub, updated_at) VALUES (?, ?, ?)",
                      (crypto, float(rate), int(time.time())))
        
        conn.commit()
        logger.info("✅ База данных готова")

init_db()

# -------------------- ФУНКЦИИ БД --------------------
def get_setting(key, default=None):
    with get_db() as (_, c):
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = c.fetchone()
        return row['value'] if row else default

def set_setting(key, value):
    with get_db() as (conn, c):
        c.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                  (key, value, value))
        conn.commit()

def get_commission(): return float(get_setting("commission", "7"))
def set_commission(p): set_setting("commission", str(p))
def get_referral_percent(): return float(get_setting("referral_percent", "1"))
def set_referral_percent(p): set_setting("referral_percent", str(p))

def get_exchange_rate(crypto):
    with get_db() as (_, c):
        c.execute("SELECT rate_rub FROM exchange_rates WHERE crypto = ?", (crypto,))
        row = c.fetchone()
        return row['rate_rub'] if row else 85.0

def set_exchange_rate(crypto, rate):
    with get_db() as (conn, c):
        c.execute("INSERT INTO exchange_rates (crypto, rate_rub, updated_at) VALUES (?, ?, ?) ON CONFLICT(crypto) DO UPDATE SET rate_rub = ?, updated_at = ?",
                  (crypto, rate, int(time.time()), rate, int(time.time())))
        conn.commit()

def get_user(user_id):
    with get_db() as (_, c):
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return c.fetchone()

def create_user(user_id, username, ref_by=None):
    with get_db() as (conn, c):
        if get_user(user_id):
            return
        c.execute("INSERT INTO users (user_id, username, ref_by, created_at) VALUES (?, ?, ?, ?)",
                  (user_id, username, ref_by, int(time.time())))
        conn.commit()

def update_balance(user_id, amount):
    with get_db() as (conn, c):
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()

def get_balance(user_id):
    with get_db() as (_, c):
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        return row['balance'] if row else 0

def add_deposit(user_id, crypto, memo, amount):
    with get_db() as (conn, c):
        c.execute("INSERT INTO deposits (user_id, crypto, memo, amount, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (user_id, crypto, memo, amount, 'pending', int(time.time())))
        conn.commit()
        return c.lastrowid

def complete_deposit(deposit_id, amount_crypto, rub_amount, user_id, tx_hash=None):
    with get_db() as (conn, c):
        c.execute("UPDATE deposits SET amount = ?, status = 'completed', completed_at = ?, tx_hash = ? WHERE id = ?",
                  (amount_crypto, int(time.time()), tx_hash, deposit_id))
        conn.commit()
        
        ref_pct = get_referral_percent()
        c.execute("SELECT ref_by FROM users WHERE user_id = ?", (user_id,))
        ref = c.fetchone()
        if ref and ref['ref_by']:
            bonus = rub_amount * ref_pct / 100
            if bonus > 0:
                update_balance(ref['ref_by'], bonus)
                c.execute("UPDATE users SET ref_bonus = ref_bonus + ? WHERE user_id = ?", (bonus, ref['ref_by']))
                c.execute("INSERT INTO referral_earnings (user_id, from_user_id, amount, created_at) VALUES (?, ?, ?, ?)",
                          (ref['ref_by'], user_id, bonus, int(time.time())))
                conn.commit()

def add_withdrawal(user_id, amount, details):
    with get_db() as (conn, c):
        c.execute("INSERT INTO withdrawals (user_id, amount, details, status, created_at) VALUES (?, ?, ?, ?, ?)",
                  (user_id, amount, details, 'pending', int(time.time())))
        conn.commit()
        return c.lastrowid

def get_withdrawal(wid):
    with get_db() as (_, c):
        c.execute("SELECT * FROM withdrawals WHERE id = ?", (wid,))
        return c.fetchone()

def update_withdrawal_status(wid, status, comment=None):
    with get_db() as (conn, c):
        if comment:
            c.execute("UPDATE withdrawals SET status = ?, admin_comment = ?, processed_at = ? WHERE id = ?",
                      (status, comment, int(time.time()), wid))
        else:
            c.execute("UPDATE withdrawals SET status = ?, processed_at = ? WHERE id = ?",
                      (status, int(time.time()), wid))
        conn.commit()

def get_withdrawals_paginated(status, page=1, per_page=5):
    with get_db() as (conn, c):
        offset = (page-1)*per_page
        if status == 'pending':
            c.execute("SELECT id, user_id, amount, details, status, created_at FROM withdrawals WHERE status = 'pending' ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset))
            c.execute("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending'")
        elif status == 'completed':
            c.execute("SELECT id, user_id, amount, details, status, created_at FROM withdrawals WHERE status = 'completed' ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset))
            c.execute("SELECT COUNT(*) FROM withdrawals WHERE status = 'completed'")
        elif status == 'rejected':
            c.execute("SELECT id, user_id, amount, details, status, created_at FROM withdrawals WHERE status = 'rejected' ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset))
            c.execute("SELECT COUNT(*) FROM withdrawals WHERE status = 'rejected'")
        else:
            c.execute("SELECT id, user_id, amount, details, status, created_at FROM withdrawals ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset))
            c.execute("SELECT COUNT(*) FROM withdrawals")
        rows = c.fetchall()
        total = c.fetchone()[0]
        return rows, total

def get_statistics():
    with get_db() as (_, c):
        c.execute("SELECT COUNT(*) FROM users WHERE is_banned = 0")
        users = c.fetchone()[0]
        c.execute("SELECT SUM(amount) FROM deposits WHERE status = 'completed'")
        deposits = c.fetchone()[0] or 0
        c.execute("SELECT SUM(amount) FROM withdrawals WHERE status = 'completed'")
        withdrawals = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM users WHERE ref_by IS NOT NULL AND ref_by > 0")
        referred = c.fetchone()[0]
        c.execute("SELECT SUM(amount) FROM referral_earnings")
        bonus = c.fetchone()[0] or 0
        return users, deposits, withdrawals, referred, bonus

def get_user_history(user_id, limit=10):
    with get_db() as (_, c):
        c.execute("SELECT crypto, amount, status, created_at FROM deposits WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
        dep = c.fetchall()
        c.execute("SELECT amount, status, created_at FROM withdrawals WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
        wd = c.fetchall()
        return dep, wd

def get_referral_info(user_id):
    with get_db() as (_, c):
        c.execute("SELECT COUNT(*) FROM users WHERE ref_by = ?", (user_id,))
        invited = c.fetchone()[0]
        c.execute("SELECT ref_bonus FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        bonus = row['ref_bonus'] if row else 0
        return invited, bonus

def log_admin_action(admin_id, action, target_user=None, details=""):
    with get_db() as (conn, c):
        c.execute("INSERT INTO admin_logs (admin_id, action, target_user, details, created_at) VALUES (?, ?, ?, ?, ?)",
                  (admin_id, action, target_user, details, int(time.time())))
        conn.commit()

def backup_db():
    try:
        os.makedirs("backups", exist_ok=True)
        shutil.copy2(DB_PATH, f"backups/exchange_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
        logger.info("✅ Бэкап создан")
    except Exception as e:
        logger.error(f"Ошибка бэкапа: {e}")

# -------------------- ПОЛУЧЕНИЕ КУРСОВ --------------------
async def get_usdt_rub():
    """Получение курса USDT/RUB из разных источников"""
    sources = [
        ("CoinGecko", "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub"),
        ("Binance P2P", "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search", True),
        ("CBR", "https://www.cbr-xml-daily.ru/daily_json.js"),
    ]
    
    for source in sources:
        try:
            async with aiohttp.ClientSession() as session:
                if source[0] == "Binance P2P":
                    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
                    payload = {"asset": "USDT", "fiat": "RUB", "tradeType": "BUY", "page": 1, "rows": 1}
                    async with session.post(source[1], json=payload, headers=headers, timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get('data') and len(data['data']) > 0:
                                rate = float(data['data'][0]['adv']['price'])
                                logger.info(f"USDT/RUB from {source[0]}: {rate:.2f} ₽")
                                return rate
                elif source[0] == "CBR":
                    async with session.get(source[1], timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            rate = float(data['Valute']['USD']['Value'])
                            logger.info(f"USD/RUB from {source[0]}: {rate:.2f} ₽")
                            return rate
                else:
                    async with session.get(source[1], timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            rate = float(data['tether']['rub'])
                            logger.info(f"USDT/RUB from {source[0]}: {rate:.2f} ₽")
                            return rate
        except Exception as e:
            logger.warning(f"Ошибка {source[0]}: {e}")
    
    return 85.0

async def get_ton_usdt():
    """Получение курса TON/USDT"""
    sources = [
        ("Bybit", "https://api.bybit.com/v5/market/tickers?category=spot&symbol=TONUSDT"),
        ("KuCoin", "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol=TON-USDT"),
        ("Gate.io", "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=TON_USDT"),
        ("MEXC", "https://api.mexc.com/api/v3/ticker/price?symbol=TONUSDT"),
    ]
    
    for name, url in sources:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if name == "Bybit" and data.get('result') and data['result']['list']:
                            rate = float(data['result']['list'][0]['lastPrice'])
                            logger.info(f"TON/USDT from {name}: {rate}")
                            return rate
                        elif name == "KuCoin" and data.get('data'):
                            rate = float(data['data']['price'])
                            logger.info(f"TON/USDT from {name}: {rate}")
                            return rate
                        elif name == "Gate.io" and data and len(data) > 0:
                            rate = float(data[0]['last'])
                            logger.info(f"TON/USDT from {name}: {rate}")
                            return rate
                        elif name == "MEXC" and data.get('price'):
                            rate = float(data['price'])
                            logger.info(f"TON/USDT from {name}: {rate}")
                            return rate
        except Exception as e:
            logger.warning(f"Ошибка {name}: {e}")
    
    return None

async def get_ton_rub():
    """Прямой курс TON/RUB"""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=rub"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    rate = float(data['the-open-network']['rub'])
                    logger.info(f"TON/RUB direct: {rate:.2f} ₽")
                    return rate
    except Exception as e:
        logger.warning(f"Ошибка TON/RUB direct: {e}")
    return None

async def update_rates():
    """Обновление курсов"""
    # USDT/RUB для всех USDT
    usdt_rub = await get_usdt_rub()
    if usdt_rub:
        set_exchange_rate('usdt_ton', usdt_rub)
        set_exchange_rate('usdt_trc20', usdt_rub)
        logger.info(f"✅ USDT курс: {usdt_rub:.2f} ₽")
    
    # TON/RUB
    ton_rub = await get_ton_rub()
    if ton_rub:
        set_exchange_rate('ton', ton_rub)
        logger.info(f"✅ TON курс: {ton_rub:.2f} ₽")
    else:
        ton_usdt = await get_ton_usdt()
        if ton_usdt and usdt_rub:
            ton_rub = ton_usdt * usdt_rub
            set_exchange_rate('ton', ton_rub)
            logger.info(f"✅ TON курс (рассчитан): {ton_rub:.2f} ₽")

async def update_rates_loop():
    while True:
        await update_rates()
        await asyncio.sleep(RATE_UPDATE_INTERVAL)

# -------------------- ПРОВЕРКА ТРАНЗАКЦИЙ --------------------
async def check_ton_tx(memo):
    address = WALLETS.get('ton')
    if not address:
        return None
    try:
        url = "https://toncenter.com/api/v2/getTransactions"
        params = {'address': address, 'limit': 50}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                for tx in data.get('result', []):
                    in_msg = tx.get('in_msg')
                    if in_msg and in_msg.get('message') == memo:
                        amount = int(in_msg.get('value', 0)) / 1e9
                        tx_hash = tx.get('transaction_id', {}).get('hash')
                        return {'amount': amount, 'tx_hash': tx_hash}
    except Exception as e:
        logger.error(f"TON check error: {e}")
    return None

async def check_usdt_ton_tx(memo):
    address = WALLETS.get('usdt_ton')
    if not address:
        return None
    try:
        url = "https://toncenter.com/api/v2/getTransactions"
        params = {'address': address, 'limit': 50}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                for tx in data.get('result', []):
                    in_msg = tx.get('in_msg')
                    if in_msg and in_msg.get('message') == memo:
                        amount = int(in_msg.get('value', 0)) / 1e9
                        tx_hash = tx.get('transaction_id', {}).get('hash')
                        return {'amount': amount, 'tx_hash': tx_hash}
    except Exception as e:
        logger.error(f"USDT TON check error: {e}")
    return None

async def check_trc20_tx(memo):
    address = WALLETS.get('usdt_trc20')
    if not address:
        return None
    try:
        url = "https://apilist.tronscan.org/api/transaction"
        params = {'address': address, 'limit': 50, 'sort': '-timestamp'}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                for tx in data.get('data', []):
                    if tx.get('contractType') == 31:
                        extra = tx.get('extra_param', {})
                        if extra.get('memo') == memo:
                            amount = int(tx.get('amount', 0)) / 1e6
                            tx_hash = tx.get('hash')
                            return {'amount': amount, 'tx_hash': tx_hash}
    except Exception as e:
        logger.error(f"TRC20 check error: {e}")
    return None

async def check_deposits_loop():
    processed = set()
    while True:
        try:
            with get_db() as (conn, c):
                c.execute("SELECT id, user_id, crypto, memo, amount FROM deposits WHERE status = 'pending'")
                pending = c.fetchall()
            
            for dep in pending:
                if dep['memo'] in processed:
                    continue
                
                tx = None
                if dep['crypto'] == 'ton':
                    tx = await check_ton_tx(dep['memo'])
                elif dep['crypto'] == 'usdt_ton':
                    tx = await check_usdt_ton_tx(dep['memo'])
                elif dep['crypto'] == 'usdt_trc20':
                    tx = await check_trc20_tx(dep['memo'])
                
                if tx and tx['amount'] >= dep['amount']:
                    processed.add(dep['memo'])
                    rate = get_exchange_rate(dep['crypto'])
                    gross = tx['amount'] * rate
                    fee = gross * get_commission() / 100
                    rub = gross - fee
                    update_balance(dep['user_id'], rub)
                    complete_deposit(dep['id'], tx['amount'], rub, dep['user_id'], tx['tx_hash'])
                    
                    await bot.send_message(
                        dep['user_id'],
                        f"✅ *Обмен выполнен!*\n\n"
                        f"💰 Зачислено: *{rub:.2f} ₽*\n"
                        f"📊 Курс: {rate:.2f} ₽ за {dep['crypto'].upper()}\n"
                        f"💸 Комиссия ({get_commission()}%): -{fee:.2f} ₽\n"
                        f"🔗 Хэш: `{tx['tx_hash'][:20]}...`",
                        parse_mode="Markdown"
                    )
                    
                    for admin in ADMIN_IDS:
                        await bot.send_message(
                            admin,
                            f"✅ *Обмен завершён*\n👤 {dep['user_id']}\n💎 {dep['crypto'].upper()}\n📊 {tx['amount']:.4f}\n💰 {rub:.2f} ₽",
                            parse_mode="Markdown"
                        )
        except Exception as e:
            logger.error(f"Check deposits error: {e}")
        await asyncio.sleep(DEPOSIT_CHECK_INTERVAL)

# -------------------- ЛИМИТЫ --------------------
user_actions = defaultdict(list)

def rate_limit(user_id, action, limit=5, window=60):
    now = datetime.now()
    key = f"{user_id}:{action}"
    actions = [t for t in user_actions[key] if now - t < timedelta(seconds=window)]
    if len(actions) >= limit:
        return False
    actions.append(now)
    user_actions[key] = actions
    return True

# -------------------- КЛАВИАТУРЫ --------------------
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Баланс", callback_data="balance")],
        [InlineKeyboardButton(text="🔄 Обмен", callback_data="exchange")],
        [InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw")],
        [InlineKeyboardButton(text="👥 Рефералы", callback_data="referrals")],
        [InlineKeyboardButton(text="📜 История", callback_data="history")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")]
    ])

def exchange_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="exch_ton")],
        [InlineKeyboardButton(text="💰 USDT (TON)", callback_data="exch_usdt_ton")],
        [InlineKeyboardButton(text="💵 USDT (TRC20)", callback_data="exch_usdt_trc20")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В главное меню", callback_data="back")]])

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="back")]])

def confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="back")]
    ])

def withdraw_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, подтвердить", callback_data="withdraw_yes"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="back")]
    ])

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Заявки на вывод", callback_data="admin_requests")],
        [InlineKeyboardButton(text="🔧 Управление курсами", callback_data="admin_rates")],
        [InlineKeyboardButton(text="⚙️ Комиссия", callback_data="admin_commission")],
        [InlineKeyboardButton(text="🎁 Реферальный %", callback_data="admin_referral")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👤 Изменить баланс", callback_data="admin_balance")],
        [InlineKeyboardButton(text="🚫 Заблокировать", callback_data="admin_ban")],
        [InlineKeyboardButton(text="◀️ Выход", callback_data="admin_exit")]
    ])

def requests_filter_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Все", callback_data="req_all")],
        [InlineKeyboardButton(text="🟡 Новые", callback_data="req_pending")],
        [InlineKeyboardButton(text="✅ Выполненные", callback_data="req_completed")],
        [InlineKeyboardButton(text="❌ Отклонённые", callback_data="req_rejected")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])

def req_pagination_kb(typ, page, total):
    btns = []
    if page > 1:
        btns.append(InlineKeyboardButton(text="◀️", callback_data=f"req_{typ}_page_{page-1}"))
    if page < total:
        btns.append(InlineKeyboardButton(text="▶️", callback_data=f"req_{typ}_page_{page+1}"))
    btns.append(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_requests"))
    return InlineKeyboardMarkup(inline_keyboard=[btns])

def req_action_kb(wid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_{wid}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{wid}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_requests")]
    ])

def admin_back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]])

# -------------------- FSM --------------------
class Exchange(StatesGroup):
    amount = State()

class Withdraw(StatesGroup):
    amount = State()
    details = State()

class AdminBalance(StatesGroup):
    uid = State()
    amount = State()

class AdminRate(StatesGroup):
    crypto = State()
    rate = State()

class AdminReject(StatesGroup):
    comment = State()

class AdminCommission(StatesGroup):
    percent = State()

class AdminReferral(StatesGroup):
    percent = State()

class AdminBan(StatesGroup):
    uid = State()

# -------------------- БОТ --------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

async def welcome(target, user_id, username, ref_by=None):
    user = get_user(user_id)
    if user and user['is_banned']:
        await target.answer("⛔ Ваш аккаунт заблокирован. Обратитесь к администратору.")
        return
    create_user(user_id, username, ref_by)
    
    text = (
        "✨ *Добро пожаловать в CryptoExchangeBot* ✨\n\n"
        "Этот бот поможет вам обменять криптовалюту на рубли по выгодному курсу.\n\n"
        "📌 *Как это работает*\n"
        "1️⃣ Выберите криптовалюту (TON, USDT TON или USDT TRC20)\n"
        "2️⃣ Введите сумму, которую хотите обменять (мин. 1 TON или 1 USDT)\n"
        "3️⃣ Отправьте криптовалюту на указанный кошелёк с обязательным комментарием (memo)\n"
        "4️⃣ После зачисления рубли поступят на ваш баланс автоматически\n"
        "5️⃣ Вы можете вывести рубли на карту или счёт\n\n"
        f"💸 *Комиссия сервиса*: {get_commission()}%\n"
        f"🎁 *Реферальная программа*: {get_referral_percent()}% от суммы обменов ваших рефералов\n\n"
        f"💰 *Минимальная сумма вывода*: {MIN_WITHDRAWAL:.0f} ₽\n"
        f"💎 *Минимальная сумма обмена*: 1 TON / 1 USDT\n\n"
        "⬇️ *Выберите действие*"
    )
    
    if WELCOME_IMAGE_URL:
        try:
            if isinstance(target, types.Message):
                await target.answer_photo(WELCOME_IMAGE_URL, caption=text, parse_mode="Markdown", reply_markup=main_kb())
            else:
                await target.message.answer_photo(WELCOME_IMAGE_URL, caption=text, parse_mode="Markdown", reply_markup=main_kb())
        except:
            await target.answer(text, parse_mode="Markdown", reply_markup=main_kb())
    else:
        await target.answer(text, parse_mode="Markdown", reply_markup=main_kb())

async def edit_or_send(cb, text, markup=None):
    try:
        if cb.message.text:
            await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        else:
            await cb.message.edit_caption(caption=text, parse_mode="Markdown", reply_markup=markup)
    except:
        await cb.message.answer(text, parse_mode="Markdown", reply_markup=markup)
    await cb.answer()

async def notify_admin(msg):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, msg, parse_mode="Markdown")
        except:
            pass

# -------------------- ХЕНДЛЕРЫ --------------------
@dp.message(Command("start"))
async def start_cmd(m: types.Message, state: FSMContext):
    await state.clear()
    args = m.text.split()
    ref = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    await welcome(m, m.from_user.id, m.from_user.username, ref)

@dp.callback_query(F.data == "back")
async def back_cb(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await welcome(cb, cb.from_user.id, cb.from_user.username)
    await cb.message.delete()

@dp.callback_query(F.data == "balance")
async def balance_cb(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user(cb.from_user.id) and get_user(cb.from_user.id)['is_banned']:
        await cb.answer("⛔ Аккаунт заблокирован", show_alert=True)
        return
    bal = get_balance(cb.from_user.id)
    await edit_or_send(cb, f"💰 *Ваш баланс*\n\n{bal:.2f} ₽\n\nЗдесь отображаются рубли, полученные за обмен криптовалюты.", back_kb())

@dp.callback_query(F.data == "exchange")
async def exchange_menu(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user(cb.from_user.id) and get_user(cb.from_user.id)['is_banned']:
        await cb.answer("⛔ Аккаунт заблокирован", show_alert=True)
        return
    await edit_or_send(cb, 
        "🔄 *Обмен криптовалюты на рубли*\n\n"
        "Выберите криптовалюту, которую хотите обменять\n\n"
        "💎 *TON* - нативный токен сети TON (мин. 1 TON)\n"
        "💰 *USDT (TON)* - стейблкоин в сети TON (мин. 1 USDT)\n"
        "💵 *USDT (TRC20)* - стейблкоин в сети TRON (мин. 1 USDT)",
        exchange_kb())

@dp.callback_query(F.data.startswith("exch_"))
async def exch_select(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user(cb.from_user.id) and get_user(cb.from_user.id)['is_banned']:
        await cb.answer("⛔ Аккаунт заблокирован", show_alert=True)
        return
    crypto = cb.data.split("_")[1]
    if crypto not in WALLETS or not WALLETS[crypto]:
        await cb.answer("❌ Кошелёк для этой валюты не настроен", show_alert=True)
        return
    await state.update_data(crypto=crypto)
    rate = get_exchange_rate(crypto)
    min_amt = MIN_EXCHANGE_AMOUNTS[crypto]
    await edit_or_send(cb,
        f"🔄 *Обмен {crypto.upper()} на рубли*\n\n"
        f"Текущий курс: 1 {crypto.upper()} = {rate:.2f} ₽\n"
        f"Комиссия сервиса: {get_commission()}%\n"
        f"Минимальная сумма: {min_amt} {crypto.upper()}\n\n"
        "Введите сумму, которую хотите обменять (в криптовалюте)",
        cancel_kb())
    await state.set_state(Exchange.amount)

@dp.message(Exchange.amount)
async def exch_amount(m: types.Message, state: FSMContext):
    if not rate_limit(m.from_user.id, "exchange"):
        await m.answer("❌ Слишком много запросов. Подождите 60 секунд.", reply_markup=cancel_kb())
        return
    try:
        amount = float(m.text)
        if amount <= 0:
            raise ValueError
    except:
        await m.answer("❌ Введите положительное число (например, 0.5).", reply_markup=cancel_kb())
        return
    data = await state.get_data()
    crypto = data['crypto']
    min_amt = MIN_EXCHANGE_AMOUNTS[crypto]
    if amount < min_amt:
        await m.answer(f"❌ Минимальная сумма обмена: {min_amt} {crypto.upper()}", reply_markup=cancel_kb())
        return
    rate = get_exchange_rate(crypto)
    gross = amount * rate
    fee = gross * get_commission() / 100
    net = gross - fee
    await state.update_data(amount=amount, net=net)
    await m.answer(
        f"🔄 *Обмен {crypto.upper()}*\n\n"
        f"Сумма: {amount:.4f} {crypto.upper()}\n"
        f"Курс: 1 {crypto.upper()} = {rate:.2f} ₽\n"
        f"Комиссия ({get_commission()}%): -{fee:.2f} ₽\n"
        f"Вы получите: *{net:.2f} ₽*\n\n"
        "Подтверждаете обмен?",
        parse_mode="Markdown",
        reply_markup=confirm_kb())
    await state.set_state(Exchange.amount)

@dp.callback_query(F.data == "confirm")
async def exch_confirm(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data:
        await back_cb(cb, state)
        return
    crypto = data['crypto']
    amount = data['amount']
    net = data['net']
    user_id = cb.from_user.id
    memo = f"dep_{user_id}_{int(time.time())}"
    address = WALLETS[crypto]
    add_deposit(user_id, crypto, memo, amount)
    
    text = (
        f"🔄 *Обмен {crypto.upper()}*\n\n"
        f"📤 *Отправьте на адрес*\n`{address}`\n\n"
        f"📝 *Обязательный комментарий (memo)*\n`{memo}`\n\n"
        f"💰 Вы получите: {net:.2f} ₽ (после вычета комиссии)\n\n"
        f"⚡️ *Важно*\n"
        f"• Переводите только {crypto.upper()} на указанный адрес\n"
        f"• Укажите комментарий точно, как выше\n"
        f"• После зачисления рубли поступят на ваш баланс автоматически\n\n"
        f"⏱ Обычно зачисление занимает 1-5 минут."
    )
    await edit_or_send(cb, text, InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В главное меню", callback_data="back")]]))
    await state.clear()
    await notify_admin(f"🔄 *Новая заявка*\n👤 {user_id}\n💎 {crypto.upper()}\n📊 {amount:.4f}\n💰 К получению: {net:.2f} ₽")

@dp.callback_query(F.data == "withdraw")
async def withdraw_menu(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user(cb.from_user.id) and get_user(cb.from_user.id)['is_banned']:
        await cb.answer("⛔ Аккаунт заблокирован", show_alert=True)
        return
    bal = get_balance(cb.from_user.id)
    if bal <= 0:
        await edit_or_send(cb, "❌ *У вас нет средств для вывода*\n\nПополните баланс через обмен криптовалюты.", back_kb())
        return
    await edit_or_send(cb,
        f"💸 *Вывод рублей*\n\n"
        f"💰 Ваш баланс: *{bal:.2f} ₽*\n"
        f"📊 Минимальная сумма: {MIN_WITHDRAWAL:.0f} ₽\n"
        f"📊 Максимальная сумма: {MAX_WITHDRAWAL:.0f} ₽\n\n"
        "Введите сумму, которую хотите вывести (в рублях)",
        cancel_kb())
    await state.set_state(Withdraw.amount)

@dp.message(Withdraw.amount)
async def withdraw_amount(m: types.Message, state: FSMContext):
    if not rate_limit(m.from_user.id, "withdraw", limit=3, window=300):
        await m.answer("❌ Слишком много заявок. Подождите 5 минут.", reply_markup=cancel_kb())
        return
    try:
        amount = float(m.text)
        if amount <= 0:
            raise ValueError
    except:
        await m.answer("❌ Введите положительное число (например, 500).", reply_markup=cancel_kb())
        return
    bal = get_balance(m.from_user.id)
    if amount < MIN_WITHDRAWAL:
        await m.answer(f"❌ Минимальная сумма вывода: {MIN_WITHDRAWAL:.0f} ₽", reply_markup=cancel_kb())
        return
    if amount > MAX_WITHDRAWAL:
        await m.answer(f"❌ Максимальная сумма вывода: {MAX_WITHDRAWAL:.0f} ₽", reply_markup=cancel_kb())
        return
    if amount > bal:
        await m.answer(f"❌ Недостаточно средств. Ваш баланс: {bal:.2f} ₽", reply_markup=cancel_kb())
        return
    await state.update_data(amount=amount)
    await m.answer("Введите реквизиты для выплаты (номер карты, счёта или телефона):", reply_markup=cancel_kb())
    await state.set_state(Withdraw.details)

@dp.message(Withdraw.details)
async def withdraw_details(m: types.Message, state: FSMContext):
    details = m.text.strip()
    if len(details) < 5:
        await m.answer("❌ Введите корректные реквизиты (минимум 5 символов)", reply_markup=cancel_kb())
        return
    data = await state.get_data()
    amount = data['amount']
    await state.update_data(details=details)
    await m.answer(
        f"💸 *Подтверждение вывода*\n\n"
        f"💰 Сумма: *{amount:.2f} ₽*\n"
        f"📝 Реквизиты: `{details}`\n\n"
        "Проверьте данные. Подтверждаете?",
        parse_mode="Markdown",
        reply_markup=withdraw_confirm_kb())

@dp.callback_query(F.data == "withdraw_yes")
async def withdraw_confirm(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data:
        await back_cb(cb, state)
        return
    amount = data['amount']
    details = data['details']
    update_balance(cb.from_user.id, -amount)
    wid = add_withdrawal(cb.from_user.id, amount, details)
    await edit_or_send(cb, f"✅ *Заявка на вывод создана*\n\n📋 Номер заявки: #{wid}\n💰 Сумма: {amount:.2f} ₽\n\n⏱ Ожидайте подтверждения администратора.", back_kb())
    await state.clear()
    await notify_admin(f"💸 *Новая заявка #{wid}*\n👤 {cb.from_user.id}\n💰 {amount:.2f} ₽\n📝 {details}")

@dp.callback_query(F.data == "referrals")
async def referrals_cb(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user(cb.from_user.id) and get_user(cb.from_user.id)['is_banned']:
        await cb.answer("⛔ Аккаунт заблокирован", show_alert=True)
        return
    invited, bonus = get_referral_info(cb.from_user.id)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={cb.from_user.id}"
    await edit_or_send(cb,
        f"👥 *Реферальная программа*\n\n"
        f"🔗 Ваша реферальная ссылка\n`{link}`\n\n"
        f"👤 Приглашено: *{invited}* чел\n"
        f"🎁 Заработано бонусов: *{bonus:.2f} ₽*\n\n"
        f"💡 Вы получаете *{get_referral_percent()}%* от суммы обменов ваших рефералов (после вычета комиссии)\n"
        "Бонусы начисляются автоматически и доступны для вывода.",
        back_kb())

@dp.callback_query(F.data == "history")
async def history_cb(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user(cb.from_user.id) and get_user(cb.from_user.id)['is_banned']:
        await cb.answer("⛔ Аккаунт заблокирован", show_alert=True)
        return
    dep, wd = get_user_history(cb.from_user.id)
    text = "📜 *История операций*\n\n"
    if dep:
        text += "*🔄 Обмены (пополнения)*\n"
        for d in dep:
            dt = time.strftime("%d.%m.%Y %H:%M", time.localtime(d['created_at']))
            text += f"• {d['crypto'].upper()} {d['amount']:.4f} - {d['status']} ({dt})\n"
    if wd:
        text += "\n*💸 Выводы*\n"
        for w in wd:
            dt = time.strftime("%d.%m.%Y %H:%M", time.localtime(w['created_at']))
            text += f"• {w['amount']:.2f} ₽ - {w['status']} ({dt})\n"
    if not dep and not wd:
        text += "📭 Операций пока нет."
    await edit_or_send(cb, text, back_kb())

@dp.callback_query(F.data == "support")
async def support_cb(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_or_send(cb, f"🆘 *Поддержка*\n\nЕсли у вас возникли вопросы или проблемы, свяжитесь с нашим специалистом\n\n👉 @{SUPPORT_USERNAME}", back_kb())

# -------------------- АДМИНКА --------------------
@dp.message(Command("admin"))
async def admin_cmd(m: types.Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        await m.answer("⛔ У вас нет доступа к этой команде.")
        return
    await state.clear()
    await m.answer("🛡 *Панель администратора*\n\nВыберите действие", parse_mode="Markdown", reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_back")
async def admin_back_cb(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔", show_alert=True)
        return
    await state.clear()
    await edit_or_send(cb, "🛡 *Панель администратора*\n\nВыберите действие", admin_kb())

@dp.callback_query(F.data == "admin_exit")
async def admin_exit_cb(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await back_cb(cb, state)

@dp.callback_query(F.data == "admin_requests")
async def admin_requests(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔", show_alert=True)
        return
    await state.clear()
    await edit_or_send(cb, "📋 *Заявки на вывод*\n\nВыберите фильтр", requests_filter_kb())

@dp.callback_query(F.data.startswith("req_"))
async def admin_show_requests(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔", show_alert=True)
        return
    parts = cb.data.split("_")
    filter_type = parts[1] if len(parts) > 1 else "all"
    page = int(parts[3]) if len(parts) > 3 else 1
    if filter_type == "all":
        rows, total = get_withdrawals_paginated(None, page)
    else:
        rows, total = get_withdrawals_paginated(filter_type, page)
    if not rows:
        await edit_or_send(cb, "📭 Нет заявок по выбранному фильтру.", requests_filter_kb())
        return
    for w in rows:
        await cb.message.answer(
            f"📋 *Заявка #{w['id']}*\n"
            f"👤 Пользователь: `{w['user_id']}`\n"
            f"💰 Сумма: {w['amount']:.2f} ₽\n"
            f"📝 Реквизиты: `{w['details']}`\n"
            f"🏷 Статус: {w['status']}\n"
            f"📅 Дата: {time.strftime('%d.%m.%Y %H:%M', time.localtime(w['created_at']))}",
            parse_mode="Markdown",
            reply_markup=req_action_kb(w['id'])
        )
    pages = (total + 4) // 5
    await cb.message.answer("📄 Страница", reply_markup=req_pagination_kb(filter_type, page, pages))
    await cb.answer()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_req(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔", show_alert=True)
        return
    wid = int(cb.data.split("_")[1])
    w = get_withdrawal(wid)
    if w:
        log_admin_action(cb.from_user.id, "approve_withdrawal", w['user_id'], f"Заявка #{wid}")
        update_withdrawal_status(wid, 'completed')
        await bot.send_message(w['user_id'], f"✅ *Заявка на вывод #{wid}* на сумму {w['amount']:.2f} ₽ подтверждена и выполнена.", parse_mode="Markdown")
        await cb.message.edit_text(f"✅ Заявка #{wid} подтверждена.", reply_markup=admin_back_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("reject_"))
async def reject_start(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔", show_alert=True)
        return
    wid = int(cb.data.split("_")[1])
    await state.update_data(wid=wid)
    await edit_or_send(cb, f"❌ *Отклонение заявки #{wid}*\n\nВведите причину отклонения (будет отправлена пользователю)", admin_back_kb())
    await state.set_state(AdminReject.comment)

@dp.message(AdminReject.comment)
async def reject_comment(m: types.Message, state: FSMContext):
    data = await state.get_data()
    wid = data['wid']
    w = get_withdrawal(wid)
    if w:
        log_admin_action(m.from_user.id, "reject_withdrawal", w['user_id'], f"Заявка #{wid}, причина: {m.text}")
        update_withdrawal_status(wid, 'rejected', m.text)
        update_balance(w['user_id'], w['amount'])
        await bot.send_message(w['user_id'], f"❌ *Заявка на вывод #{wid}* на сумму {w['amount']:.2f} ₽ отклонена.\nПричина: {m.text}", parse_mode="Markdown")
        await m.answer(f"✅ Заявка #{wid} отклонена, средства возвращены.", reply_markup=admin_back_kb())
    else:
        await m.answer("❌ Заявка не найдена.", reply_markup=admin_back_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats_cb(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔", show_alert=True)
        return
    users, deposits, withdrawals, referred, bonus = get_statistics()
    await edit_or_send(cb,
        f"📊 *Статистика*\n\n"
        f"👥 Пользователей: {users}\n"
        f"👥 Приглашённых: {referred}\n"
        f"💰 Всего пополнений (обменов): {deposits:.2f} ₽\n"
        f"💸 Всего выводов: {withdrawals:.2f} ₽\n"
        f"💵 В обороте: {deposits - withdrawals:.2f} ₽\n"
        f"🎁 Выплачено реферальных бонусов: {bonus:.2f} ₽",
        admin_back_kb())

@dp.callback_query(F.data == "admin_rates")
async def admin_rates_menu(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔", show_alert=True)
        return
    await edit_or_send(cb, "🔧 *Управление курсами*\n\nВыберите криптовалюту", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="rate_ton")],
        [InlineKeyboardButton(text="💰 USDT (TON)", callback_data="rate_usdt_ton")],
        [InlineKeyboardButton(text="💵 USDT (TRC20)", callback_data="rate_usdt_trc20")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ]))

@dp.callback_query(F.data.startswith("rate_"))
async def admin_rate_select(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔", show_alert=True)
        return
    crypto = cb.data.split("_")[1]
    await state.update_data(crypto=crypto)
    current = get_exchange_rate(crypto)
    await edit_or_send(cb, f"🔧 *Изменение курса {crypto.upper()}*\n\nТекущий курс: {current:.2f} ₽\nВведите новый курс (рублей за единицу)", admin_back_kb())
    await state.set_state(AdminRate.rate)

@dp.message(AdminRate.rate)
async def admin_rate_set(m: types.Message, state: FSMContext):
    try:
        rate = float(m.text)
        if rate <= 0:
            raise ValueError
    except:
        await m.answer("❌ Введите положительное число (например, 95.5).")
        return
    data = await state.get_data()
    set_exchange_rate(data['crypto'], rate)
    log_admin_action(m.from_user.id, "set_rate", details=f"{data['crypto']}: {rate:.2f}")
    await m.answer(f"✅ Курс для {data['crypto'].upper()} установлен: {rate:.2f} ₽", reply_markup=admin_back_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_commission")
async def admin_commission_menu(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔", show_alert=True)
        return
    await edit_or_send(cb, f"⚙️ *Комиссия сервиса*\n\nТекущая комиссия: {get_commission()}%\nВведите новое значение (процент от суммы обмена)", admin_back_kb())
    await state.set_state(AdminCommission.percent)

@dp.message(AdminCommission.percent)
async def admin_commission_set(m: types.Message, state: FSMContext):
    try:
        p = float(m.text)
        if p < 0 or p > 100:
            raise ValueError
    except:
        await m.answer("❌ Введите число от 0 до 100.")
        return
    set_commission(p)
    log_admin_action(m.from_user.id, "set_commission", details=f"{p}%")
    await m.answer(f"✅ Комиссия установлена: {p}%", reply_markup=admin_back_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_referral")
async def admin_referral_menu(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔", show_alert=True)
        return
    await edit_or_send(cb, f"🎁 *Реферальный процент*\n\nТекущий процент: {get_referral_percent()}%\nВведите новое значение (процент от суммы обмена реферала)", admin_back_kb())
    await state.set_state(AdminReferral.percent)

@dp.message(AdminReferral.percent)
async def admin_referral_set(m: types.Message, state: FSMContext):
    try:
        p = float(m.text)
        if p < 0 or p > 50:
            raise ValueError
    except:
        await m.answer("❌ Введите число от 0 до 50.")
        return
    set_referral_percent(p)
    log_admin_action(m.from_user.id, "set_referral_percent", details=f"{p}%")
    await m.answer(f"✅ Реферальный процент установлен: {p}%", reply_markup=admin_back_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_balance")
async def admin_balance_start(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔", show_alert=True)
        return
    await edit_or_send(cb, "👤 *Ручное изменение баланса*\n\nВведите Telegram ID пользователя", admin_back_kb())
    await state.set_state(AdminBalance.uid)

@dp.message(AdminBalance.uid)
async def admin_balance_uid(m: types.Message, state: FSMContext):
    try:
        uid = int(m.text)
    except:
        await m.answer("❌ ID должен быть числом.")
        return
    user = get_user(uid)
    if not user:
        await m.answer(f"❌ Пользователь с ID {uid} не найден.")
        return
    await state.update_data(uid=uid, username=user['username'])
    await m.answer("Введите сумму изменения (положительная - зачисление, отрицательная - списание)")
    await state.set_state(AdminBalance.amount)

@dp.message(AdminBalance.amount)
async def admin_balance_amount(m: types.Message, state: FSMContext):
    try:
        amt = float(m.text)
    except:
        await m.answer("❌ Введите число.")
        return
    data = await state.get_data()
    update_balance(data['uid'], amt)
    new_bal = get_balance(data['uid'])
    log_admin_action(m.from_user.id, "manual_balance_change", target_user=data['uid'], details=f"{amt:+.2f}")
    await bot.send_message(data['uid'], f"👤 *Изменение баланса*\n\n💰 Сумма: {amt:+.2f} ₽\n💵 Новый баланс: {new_bal:.2f} ₽\n\n👨‍💻 Администратор изменил ваш баланс.", parse_mode="Markdown")
    await m.answer(f"✅ Баланс пользователя {data['username']} (ID: {data['uid']}) изменён на {amt:+.2f} ₽.\n💰 Новый баланс: {new_bal:.2f} ₽", reply_markup=admin_back_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_ban")
async def admin_ban_start(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔", show_alert=True)
        return
    await edit_or_send(cb, "🚫 *Блокировка пользователя*\n\nВведите Telegram ID пользователя для блокировки", admin_back_kb())
    await state.set_state(AdminBan.uid)

@dp.message(AdminBan.uid)
async def admin_ban_uid(m: types.Message, state: FSMContext):
    try:
        uid = int(m.text)
    except:
        await m.answer("❌ ID должен быть числом.")
        return
    user = get_user(uid)
    if not user:
        await m.answer(f"❌ Пользователь с ID {uid} не найден.")
        return
    with get_db() as (conn, c):
        c.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (uid,))
        conn.commit()
    log_admin_action(m.from_user.id, "ban_user", target_user=uid)
    await bot.send_message(uid, "🚫 *Ваш аккаунт заблокирован*\n\nСвяжитесь с администратором для выяснения причин.\n" + f"👉 @{SUPPORT_USERNAME}", parse_mode="Markdown")
    await m.answer(f"✅ Пользователь {user['username']} (ID: {uid}) заблокирован.", reply_markup=admin_back_kb())
    await state.clear()

# -------------------- FLASK --------------------
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is running!"

@app.route('/health')
def health():
    return "OK"

@app.route('/stats')
def stats():
    users, deposits, withdrawals, _, _ = get_statistics()
    return jsonify({
        "status": "ok",
        "users": users,
        "total_deposits": deposits,
        "total_withdrawals": withdrawals,
        "balance": deposits - withdrawals
    })

# -------------------- ЗАПУСК --------------------
async def start_background():
    asyncio.create_task(update_rates_loop())
    asyncio.create_task(check_deposits_loop())
    asyncio.create_task(backup_loop())

async def backup_loop():
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        backup_db()

def run_bot():
    async def main():
        await start_background()
        await dp.start_polling(bot)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен")
    finally:
        loop.close()

if __name__ == "__main__":
    logger.info("🚀 Запуск бота...")
    
    async def del_wh():
        try:
            async with Bot(token=BOT_TOKEN) as tmp:
                await tmp.delete_webhook(drop_pending_updates=True)
                logger.info("✅ Вебхук удалён")
        except:
            pass
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(del_wh())
    loop.close()
    
    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        threading.Thread(target=run_bot, daemon=True).start()
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    else:
        run_bot()
