import os
import asyncio
import sqlite3
import time
import aiohttp
import logging
import re
import threading
import shutil
import sys
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional, Dict, Tuple, List
from contextlib import contextmanager
from flask import Flask, request, jsonify
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

if not BOT_TOKEN:
    logging.error("❌ BOT_TOKEN не установлен!")
    exit(1)

if not ADMIN_IDS:
    logging.error("❌ ADMIN_IDS не установлен!")
    exit(1)

WALLETS = {
    'ton': os.environ.get("WALLET_TON", ""),
    'usdt_ton': os.environ.get("WALLET_USDT_TON", ""),
    'usdt_trc20': os.environ.get("WALLET_USDT_TRC20", ""),
}

for key, value in WALLETS.items():
    if not value or value in ("EQD...", "T...", ""):
        logging.warning(f"⚠️ Кошелёк для {key} не задан! Укажите WALLET_{key.upper()}")

# Минимальные суммы обмена
MIN_EXCHANGE_AMOUNTS = {
    'ton': 1.0,
    'usdt_ton': 1.0,
    'usdt_trc20': 1.0
}

# Лимиты и настройки
MIN_WITHDRAWAL = float(os.environ.get("MIN_WITHDRAWAL", "100"))
MAX_WITHDRAWAL = float(os.environ.get("MAX_WITHDRAWAL", "100000"))
RATE_UPDATE_INTERVAL = int(os.environ.get("RATE_UPDATE_INTERVAL", "30"))
DEPOSIT_CHECK_INTERVAL = int(os.environ.get("DEPOSIT_CHECK_INTERVAL", "60"))
BACKUP_INTERVAL = int(os.environ.get("BACKUP_INTERVAL", "86400"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# -------------------- БАЗА ДАННЫХ --------------------
DB_PATH = os.environ.get("DATABASE_PATH", "exchange.db")
_local = threading.local()

@contextmanager
def get_db():
    if not hasattr(_local, 'conn'):
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.c = _local.conn.cursor()
    try:
        yield _local.conn, _local.c
    except Exception as e:
        _local.conn.rollback()
        raise e

def init_database():
    with get_db() as (conn, c):
        c.execute('''CREATE TABLE IF NOT EXISTS users
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER UNIQUE,
                      username TEXT,
                      balance REAL DEFAULT 0,
                      ref_by INTEGER DEFAULT 0,
                      ref_bonus REAL DEFAULT 0,
                      is_banned INTEGER DEFAULT 0,
                      created_at INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS deposits
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER,
                      crypto TEXT,
                      memo TEXT,
                      amount REAL DEFAULT 0,
                      status TEXT,
                      tx_hash TEXT,
                      created_at INTEGER,
                      completed_at INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS withdrawals
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER,
                      amount REAL,
                      details TEXT,
                      status TEXT,
                      admin_comment TEXT,
                      created_at INTEGER,
                      processed_at INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS exchange_rates
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      crypto TEXT UNIQUE,
                      rate_rub REAL,
                      updated_at INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS referral_earnings
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER,
                      from_user_id INTEGER,
                      amount REAL,
                      created_at INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS settings
                     (key TEXT PRIMARY KEY,
                      value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admin_logs
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      admin_id INTEGER,
                      action TEXT,
                      target_user INTEGER,
                      details TEXT,
                      created_at INTEGER)''')
        # Индексы
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_users_user_id ON users (user_id)",
            "CREATE INDEX IF NOT EXISTS idx_users_ref_by ON users (ref_by)",
            "CREATE INDEX IF NOT EXISTS idx_deposits_user_status ON deposits (user_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_deposits_memo ON deposits (memo)",
            "CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals (status)",
            "CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals (user_id)",
        ]
        for idx in indexes:
            try:
                c.execute(idx)
            except Exception as e:
                logger.warning(f"Ошибка индекса: {e}")

        defaults = [
            ("commission", "7"),
            ("referral_percent", "1"),
            ("min_withdrawal", str(MIN_WITHDRAWAL)),
            ("max_withdrawal", str(MAX_WITHDRAWAL)),
        ]
        for key, value in defaults:
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

        default_rates = [('ton', '500'), ('usdt_ton', '85'), ('usdt_trc20', '85')]
        for crypto, rate in default_rates:
            c.execute("INSERT OR IGNORE INTO exchange_rates (crypto, rate_rub, updated_at) VALUES (?, ?, ?)",
                      (crypto, float(rate), int(time.time())))

        conn.commit()
        logger.info("✅ База данных инициализирована")

init_database()

# -------------------- ФУНКЦИИ БД --------------------
def get_setting(key: str, default: str = None) -> Optional[str]:
    with get_db() as (conn, c):
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = c.fetchone()
        return row['value'] if row else default

def set_setting(key: str, value: str) -> None:
    with get_db() as (conn, c):
        c.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                  (key, value, value))
        conn.commit()

def get_commission() -> float:
    return float(get_setting("commission", "7"))

def set_commission(percent: float) -> None:
    set_setting("commission", str(percent))

def get_referral_percent() -> float:
    return float(get_setting("referral_percent", "1"))

def set_referral_percent(percent: float) -> None:
    set_setting("referral_percent", str(percent))

def get_exchange_rate(crypto: str) -> float:
    with get_db() as (conn, c):
        c.execute("SELECT rate_rub FROM exchange_rates WHERE crypto = ?", (crypto,))
        row = c.fetchone()
        return row['rate_rub'] if row else 85.0

def set_exchange_rate(crypto: str, rate: float) -> None:
    with get_db() as (conn, c):
        c.execute("INSERT INTO exchange_rates (crypto, rate_rub, updated_at) VALUES (?, ?, ?) ON CONFLICT(crypto) DO UPDATE SET rate_rub = ?, updated_at = ?",
                  (crypto, rate, int(time.time()), rate, int(time.time())))
        conn.commit()

def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with get_db() as (conn, c):
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return c.fetchone()

def create_user(user_id: int, username: str, ref_by: int = None) -> None:
    with get_db() as (conn, c):
        if get_user(user_id):
            return
        c.execute("INSERT INTO users (user_id, username, ref_by, created_at) VALUES (?, ?, ?, ?)",
                  (user_id, username, ref_by, int(time.time())))
        conn.commit()

def update_balance(user_id: int, amount: float) -> None:
    with get_db() as (conn, c):
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()

def get_balance(user_id: int) -> float:
    with get_db() as (conn, c):
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        return row['balance'] if row else 0

def add_deposit(user_id: int, crypto: str, memo: str, amount_crypto: float) -> int:
    with get_db() as (conn, c):
        c.execute("INSERT INTO deposits (user_id, crypto, memo, amount, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (user_id, crypto, memo, amount_crypto, 'pending', int(time.time())))
        conn.commit()
        return c.lastrowid

def complete_deposit(deposit_id: int, amount_crypto: float, rub_amount: float, user_id: int, tx_hash: str = None) -> None:
    with get_db() as (conn, c):
        c.execute("UPDATE deposits SET amount = ?, status = 'completed', completed_at = ?, tx_hash = ? WHERE id = ?",
                  (amount_crypto, int(time.time()), tx_hash, deposit_id))
        conn.commit()

        ref_percent = get_referral_percent()
        c.execute("SELECT ref_by FROM users WHERE user_id = ?", (user_id,))
        ref_by_row = c.fetchone()
        if ref_by_row and ref_by_row['ref_by']:
            ref_by = ref_by_row['ref_by']
            bonus = rub_amount * ref_percent / 100
            if bonus > 0:
                update_balance(ref_by, bonus)
                c.execute("UPDATE users SET ref_bonus = ref_bonus + ? WHERE user_id = ?", (bonus, ref_by))
                c.execute("INSERT INTO referral_earnings (user_id, from_user_id, amount, created_at) VALUES (?, ?, ?, ?)",
                          (ref_by, user_id, bonus, int(time.time())))
                conn.commit()

def add_withdrawal(user_id: int, amount: float, details: str) -> int:
    with get_db() as (conn, c):
        c.execute("INSERT INTO withdrawals (user_id, amount, details, status, created_at) VALUES (?, ?, ?, ?, ?)",
                  (user_id, amount, details, 'pending', int(time.time())))
        conn.commit()
        return c.lastrowid

def get_withdrawal(withdraw_id: int) -> Optional[sqlite3.Row]:
    with get_db() as (conn, c):
        c.execute("SELECT * FROM withdrawals WHERE id = ?", (withdraw_id,))
        return c.fetchone()

def update_withdrawal_status(withdraw_id: int, status: str, admin_comment: str = None) -> None:
    with get_db() as (conn, c):
        if admin_comment:
            c.execute("UPDATE withdrawals SET status = ?, admin_comment = ?, processed_at = ? WHERE id = ?",
                      (status, admin_comment, int(time.time()), withdraw_id))
        else:
            c.execute("UPDATE withdrawals SET status = ?, processed_at = ? WHERE id = ?",
                      (status, int(time.time()), withdraw_id))
        conn.commit()

def get_withdrawals_paginated(status: str = None, page: int = 1, per_page: int = 5) -> Tuple[List[sqlite3.Row], int]:
    with get_db() as (conn, c):
        offset = (page - 1) * per_page
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
        withdrawals = c.fetchall()
        total = c.fetchone()[0]
        return withdrawals, total

def get_statistics() -> Tuple:
    with get_db() as (conn, c):
        c.execute("SELECT COUNT(*) FROM users WHERE is_banned = 0")
        users_count = c.fetchone()[0]
        c.execute("SELECT SUM(amount) FROM deposits WHERE status = 'completed'")
        total_deposits = c.fetchone()[0] or 0
        c.execute("SELECT SUM(amount) FROM withdrawals WHERE status = 'completed'")
        total_withdrawals = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM users WHERE ref_by IS NOT NULL AND ref_by > 0")
        referred_count = c.fetchone()[0]
        c.execute("SELECT SUM(amount) FROM referral_earnings")
        total_ref_bonus = c.fetchone()[0] or 0
        return users_count, total_deposits, total_withdrawals, referred_count, total_ref_bonus

def get_user_history(user_id: int, limit: int = 10) -> Tuple[List, List]:
    with get_db() as (conn, c):
        c.execute("SELECT crypto, amount, status, created_at FROM deposits WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
        deposits = c.fetchall()
        c.execute("SELECT amount, status, created_at FROM withdrawals WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
        withdrawals = c.fetchall()
        return deposits, withdrawals

def get_referral_info(user_id: int) -> Tuple[int, float]:
    with get_db() as (conn, c):
        c.execute("SELECT COUNT(*) FROM users WHERE ref_by = ?", (user_id,))
        invited_count = c.fetchone()[0]
        c.execute("SELECT ref_bonus FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        bonus = row['ref_bonus'] if row else 0
        return invited_count, bonus

def log_admin_action(admin_id: int, action: str, target_user: int = None, details: str = "") -> None:
    with get_db() as (conn, c):
        c.execute("INSERT INTO admin_logs (admin_id, action, target_user, details, created_at) VALUES (?, ?, ?, ?, ?)",
                  (admin_id, action, target_user, details, int(time.time())))
        conn.commit()

def backup_database() -> bool:
    try:
        backup_dir = "backups"
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = f"{backup_dir}/exchange_{timestamp}.db"
        shutil.copy2(DB_PATH, backup_path)
        for file in os.listdir(backup_dir):
            file_path = os.path.join(backup_dir, file)
            if os.path.isfile(file_path):
                file_time = datetime.fromtimestamp(os.path.getctime(file_path))
                if datetime.now() - file_time > timedelta(days=7):
                    os.remove(file_path)
        logger.info(f"✅ Резервная копия: {backup_path}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка резервного копирования: {e}")
        return False

# -------------------- ЛИМИТЫ --------------------
user_actions = defaultdict(list)

def check_rate_limit(user_id: int, action: str, limit: int = 10, window: int = 60) -> bool:
    now = datetime.now()
    key = f"{user_id}:{action}"
    actions = user_actions[key]
    actions = [act for act in actions if now - act < timedelta(seconds=window)]
    if len(actions) >= limit:
        return False
    actions.append(now)
    user_actions[key] = actions
    return True

# -------------------- ESCAPE MARKDOWN --------------------
def escape_markdown(text: str) -> str:
    if not text:
        return text
    special_chars = r'_*[]()~`>#+-=|{}.!'
    result = []
    for ch in text:
        if ch in special_chars:
            result.append('\\' + ch)
        else:
            result.append(ch)
    return ''.join(result)

# -------------------- ПОЛУЧЕНИЕ КУРСОВ --------------------
async def fetch_usdt_rub_crypto_bot() -> Optional[float]:
    try:
        url = "https://api.crypt.bot/v1/getExchangeRate"
        params = {"from": "USDT", "to": "RUB"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('ok') and data.get('result'):
                        return float(data['result']['rate'])
    except Exception as e:
        logger.warning(f"Crypto Bot API ошибка: {e}")
    return None

async def fetch_usdt_rub_binance() -> Optional[float]:
    try:
        url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        payload = {"asset": "USDT", "fiat": "RUB", "tradeType": "BUY", "page": 1, "rows": 1}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('data') and len(data['data']) > 0:
                        return float(data['data'][0]['adv']['price'])
    except Exception as e:
        logger.warning(f"Binance P2P ошибка: {e}")
    return None

async def fetch_usdt_rub_coingecko() -> Optional[float]:
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data['tether']['rub'])
    except Exception as e:
        logger.warning(f"CoinGecko ошибка: {e}")
    return None

async def fetch_usdt_rub_cbr() -> Optional[float]:
    try:
        url = "https://www.cbr-xml-daily.ru/daily_json.js"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data['Valute']['USD']['Value'])
    except Exception as e:
        logger.warning(f"ЦБ РФ ошибка: {e}")
    return None

async def fetch_ton_usdt_multiple() -> Optional[float]:
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
                            return float(data['result']['list'][0]['lastPrice'])
                        elif name == "KuCoin" and data.get('data'):
                            return float(data['data']['price'])
                        elif name == "Gate.io" and data and len(data) > 0:
                            return float(data[0]['last'])
                        elif name == "MEXC" and data.get('price'):
                            return float(data['price'])
        except Exception as e:
            logger.warning(f"{name} TON/USDT ошибка: {e}")
    return None

async def fetch_ton_rub_direct() -> Optional[float]:
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=rub"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data['the-open-network']['rub'])
    except Exception as e:
        logger.warning(f"TON/RUB direct error: {e}")
    return None

async def fetch_real_rate(crypto: str) -> Optional[float]:
    if crypto == 'ton':
        rate = await fetch_ton_rub_direct()
        if rate:
            return rate
        ton_usdt = await fetch_ton_usdt_multiple()
        if ton_usdt:
            usdt_rub = await fetch_usdt_rub_crypto_bot() or await fetch_usdt_rub_binance() or await fetch_usdt_rub_coingecko() or await fetch_usdt_rub_cbr()
            if usdt_rub:
                return ton_usdt * usdt_rub
        return None
    elif crypto in ('usdt_ton', 'usdt_trc20'):
        return await fetch_usdt_rub_crypto_bot() or await fetch_usdt_rub_binance() or await fetch_usdt_rub_coingecko() or await fetch_usdt_rub_cbr()
    return None

async def update_rates_periodically():
    while True:
        for crypto in ['ton', 'usdt_ton', 'usdt_trc20']:
            rate = await fetch_real_rate(crypto)
            if rate and rate > 0:
                set_exchange_rate(crypto, rate)
                logger.info(f"✅ {crypto} курс: {rate:.2f} ₽")
            else:
                logger.warning(f"❌ Не удалось обновить {crypto}, оставляем старый")
        await asyncio.sleep(RATE_UPDATE_INTERVAL)

# -------------------- ПРОВЕРКА ТРАНЗАКЦИЙ --------------------
async def check_ton_transaction(memo: str) -> Optional[Dict]:
    address = WALLETS.get('ton')
    if not address:
        return None
    url = "https://toncenter.com/api/v2/getTransactions"
    params = {'address': address, 'limit': 50}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                for tx in data.get('result', []):
                    in_msg = tx.get('in_msg')
                    if in_msg and in_msg.get('message') == memo:
                        value_nano = int(in_msg.get('value', 0))
                        amount = value_nano / 1e9
                        tx_hash = tx.get('transaction_id', {}).get('hash')
                        return {'amount': amount, 'tx_hash': tx_hash}
    except Exception as e:
        logger.error(f"TON check error: {e}")
    return None

async def check_usdt_ton_transaction(memo: str) -> Optional[Dict]:
    address = WALLETS.get('usdt_ton')
    if not address:
        return None
    url = "https://toncenter.com/api/v2/getTransactions"
    params = {'address': address, 'limit': 50}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                for tx in data.get('result', []):
                    in_msg = tx.get('in_msg')
                    if in_msg and in_msg.get('message') == memo:
                        value = int(in_msg.get('value', 0))
                        if value > 0:
                            amount = value / 1e9
                            tx_hash = tx.get('transaction_id', {}).get('hash')
                            return {'amount': amount, 'tx_hash': tx_hash}
    except Exception as e:
        logger.error(f"USDT TON check error: {e}")
    return None

async def check_trc20_transaction(memo: str) -> Optional[Dict]:
    address = WALLETS.get('usdt_trc20')
    if not address:
        return None
    url = "https://apilist.tronscan.org/api/transaction"
    params = {'address': address, 'limit': 50, 'sort': '-timestamp'}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                for tx in data.get('data', []):
                    if tx.get('contractType') == 31:
                        extra = tx.get('extra_param', {})
                        if extra.get('memo') == memo:
                            amount_sun = int(tx.get('amount', 0))
                            amount = amount_sun / 1e6
                            tx_hash = tx.get('hash')
                            return {'amount': amount, 'tx_hash': tx_hash}
    except Exception as e:
        logger.error(f"TRC20 check error: {e}")
    return None

async def check_transaction_with_retry(crypto: str, memo: str) -> Optional[Dict]:
    for attempt in range(MAX_RETRIES):
        try:
            if crypto == 'ton':
                result = await check_ton_transaction(memo)
            elif crypto == 'usdt_ton':
                result = await check_usdt_ton_transaction(memo)
            elif crypto == 'usdt_trc20':
                result = await check_trc20_transaction(memo)
            else:
                return None
            if result:
                return result
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"Попытка {attempt+1} ошибка: {e}")
    return None

# -------------------- ПРОВЕРКА ДЕПОЗИТОВ --------------------
processed_memos = set()

async def check_deposits():
    while True:
        try:
            if len(processed_memos) > 10000:
                processed_memos.clear()
            with get_db() as (conn, c):
                c.execute("SELECT id, user_id, crypto, memo, amount FROM deposits WHERE status = 'pending'")
                pending = c.fetchall()
            for dep in pending:
                if dep['memo'] in processed_memos:
                    continue
                tx = await check_transaction_with_retry(dep['crypto'], dep['memo'])
                if tx and tx.get('amount', 0) >= dep['amount']:
                    processed_memos.add(dep['memo'])
                    amount_crypto = tx['amount']
                    tx_hash = tx.get('tx_hash')
                    rate = get_exchange_rate(dep['crypto'])
                    gross = amount_crypto * rate
                    commission = get_commission()
                    fee = gross * commission / 100
                    rub = gross - fee
                    update_balance(dep['user_id'], rub)
                    complete_deposit(dep['id'], amount_crypto, rub, dep['user_id'], tx_hash)
                    await bot.send_message(
                        dep['user_id'],
                        f"✅ *Обмен выполнен*\n\n💰 Зачислено: {rub:.2f} ₽\n📊 Курс: {rate:.2f} ₽ за {dep['crypto'].upper()}\n💸 Комиссия: {fee:.2f} ₽",
                        parse_mode="Markdown"
                    )
                    for admin in ADMIN_IDS:
                        await bot.send_message(
                            admin,
                            f"✅ Обмен завершён\n👤 {dep['user_id']}\n💎 {dep['crypto'].upper()}\n📊 {amount_crypto:.4f}\n💰 {rub:.2f} ₽",
                            parse_mode="Markdown"
                        )
        except Exception as e:
            logger.error(f"check_deposits: {e}")
        await asyncio.sleep(DEPOSIT_CHECK_INTERVAL)

# -------------------- КЛАВИАТУРЫ --------------------
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Баланс", callback_data="menu_balance")],
        [InlineKeyboardButton(text="🔄 Обмен", callback_data="menu_exchange")],
        [InlineKeyboardButton(text="💸 Вывести", callback_data="menu_withdraw")],
        [InlineKeyboardButton(text="👥 Рефералы", callback_data="menu_referrals")],
        [InlineKeyboardButton(text="📜 История", callback_data="menu_history")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="menu_support")]
    ])

def exchange_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="exchange_ton")],
        [InlineKeyboardButton(text="💰 USDT (TON)", callback_data="exchange_usdt_ton")],
        [InlineKeyboardButton(text="💵 USDT (TRC20)", callback_data="exchange_usdt_trc20")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]
    ])

def back_to_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ В главное меню", callback_data="back_to_main")]
    ])

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main")]
    ])

def confirm_exchange_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="exchange_confirm"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="exchange_cancel")]
    ])

def confirm_withdraw_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, подтвердить", callback_data="confirm_withdraw_yes"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="confirm_withdraw_no")]
    ])

def admin_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Заявки на вывод", callback_data="admin_withdrawals")],
        [InlineKeyboardButton(text="🔧 Управление курсами", callback_data="admin_rates")],
        [InlineKeyboardButton(text="⚙️ Комиссия", callback_data="admin_commission")],
        [InlineKeyboardButton(text="🎁 Реферальный %", callback_data="admin_referral")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👤 Изменить баланс", callback_data="admin_balance")],
        [InlineKeyboardButton(text="🚫 Заблокировать", callback_data="admin_ban")],
        [InlineKeyboardButton(text="◀️ Выход", callback_data="admin_exit")]
    ])

def admin_withdrawals_filter_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Все", callback_data="admin_withdrawals_all")],
        [InlineKeyboardButton(text="🟡 Новые", callback_data="admin_withdrawals_pending")],
        [InlineKeyboardButton(text="✅ Выполненные", callback_data="admin_withdrawals_completed")],
        [InlineKeyboardButton(text="❌ Отклонённые", callback_data="admin_withdrawals_rejected")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])

def withdrawal_pagination_kb(filter_type: str, page: int, total_pages: int):
    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_withdrawals_{filter_type}_page_{page-1}"))
    if page < total_pages:
        buttons.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"admin_withdrawals_{filter_type}_page_{page+1}"))
    buttons.append(InlineKeyboardButton(text="◀️ Назад к фильтрам", callback_data="admin_withdrawals"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])

def withdrawal_action_kb(withdraw_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_{withdraw_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{withdraw_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_withdrawals")]
    ])

def back_to_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])

# -------------------- FSM --------------------
class ExchangeFSM(StatesGroup):
    waiting_amount = State()
    confirm = State()

class WithdrawFSM(StatesGroup):
    waiting_amount = State()
    waiting_details = State()
    confirm = State()

class AdminBalanceFSM(StatesGroup):
    waiting_user_id = State()
    waiting_amount = State()

class AdminRateFSM(StatesGroup):
    waiting_crypto = State()
    waiting_rate = State()

class AdminRejectFSM(StatesGroup):
    waiting_comment = State()

class AdminCommissionFSM(StatesGroup):
    waiting_percent = State()

class AdminReferralFSM(StatesGroup):
    waiting_percent = State()

class AdminBanFSM(StatesGroup):
    waiting_user_id = State()

# -------------------- БОТ --------------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

async def send_welcome_message(target, user_id: int, username: str, ref_by: int = None):
    user = get_user(user_id)
    if user and user['is_banned']:
        await target.answer("⛔ Аккаунт заблокирован")
        return
    create_user(user_id, username, ref_by)
    text = (
        "✨ *Добро пожаловать в CryptoExchangeBot* ✨\n\n"
        "Обмен криптовалюты на рубли по выгодному курсу.\n\n"
        f"💸 *Комиссия*: {get_commission()}%\n"
        f"🎁 *Рефералка*: {get_referral_percent()}%\n\n"
        f"💰 *Мин. вывод*: {MIN_WITHDRAWAL:.0f} ₽\n"
        f"💎 *Мин. обмен TON*: 1 TON\n"
        f"💵 *Мин. обмен USDT*: 1 USDT\n\n"
        "⬇️ *Выберите действие*"
    )
    if WELCOME_IMAGE_URL:
        try:
            if isinstance(target, types.Message):
                await target.answer_photo(photo=WELCOME_IMAGE_URL, caption=text, parse_mode="Markdown", reply_markup=main_menu_kb())
            else:
                await target.message.answer_photo(photo=WELCOME_IMAGE_URL, caption=text, parse_mode="Markdown", reply_markup=main_menu_kb())
        except:
            if isinstance(target, types.Message):
                await target.answer(text, parse_mode="Markdown", reply_markup=main_menu_kb())
            else:
                await target.message.answer(text, parse_mode="Markdown", reply_markup=main_menu_kb())
    else:
        if isinstance(target, types.Message):
            await target.answer(text, parse_mode="Markdown", reply_markup=main_menu_kb())
        else:
            await target.message.answer(text, parse_mode="Markdown", reply_markup=main_menu_kb())

async def edit_or_send_message(callback: types.CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup = None):
    try:
        if callback.message.text:
            await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        else:
            await callback.message.edit_caption(caption=text, parse_mode="Markdown", reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"edit error: {e}")
        try:
            await callback.message.answer(text, parse_mode="Markdown", reply_markup=reply_markup)
        except:
            await callback.message.answer(text, reply_markup=reply_markup)
    await callback.answer()

async def notify_admin(text: str):
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"notify admin {admin}: {e}")

# -------------------- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЕЙ --------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    args = message.text.split()
    ref_by = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    await send_welcome_message(message, message.from_user.id, message.from_user.username, ref_by)

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await send_welcome_message(callback, callback.from_user.id, callback.from_user.username)
    await callback.message.delete()

@dp.callback_query(F.data == "menu_balance")
async def show_balance(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user(callback.from_user.id) and get_user(callback.from_user.id)['is_banned']:
        await callback.answer("⛔ Заблокирован", show_alert=True)
        return
    balance = get_balance(callback.from_user.id)
    text = f"💰 *Ваш баланс*\n\n{balance:.2f} ₽"
    await edit_or_send_message(callback, text, back_to_main_kb())

@dp.callback_query(F.data == "menu_exchange")
async def exchange_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user(callback.from_user.id) and get_user(callback.from_user.id)['is_banned']:
        await callback.answer("⛔ Заблокирован", show_alert=True)
        return
    text = "🔄 *Обмен*\n\nВыберите криптовалюту"
    await edit_or_send_message(callback, text, exchange_menu_kb())

@dp.callback_query(F.data.startswith("exchange_"))
async def exchange_select_crypto(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user(callback.from_user.id) and get_user(callback.from_user.id)['is_banned']:
        await callback.answer("⛔ Заблокирован", show_alert=True)
        return
    crypto = callback.data.split("_")[1]   # exchange_ton -> ton
    if crypto not in WALLETS:
        await callback.answer("❌ Валюта не поддерживается", show_alert=True)
        return
    if not WALLETS[crypto]:
        await callback.answer("Кошелёк не настроен", show_alert=True)
        return
    await state.update_data(crypto=crypto)
    rate = get_exchange_rate(crypto)
    min_amt = MIN_EXCHANGE_AMOUNTS[crypto]
    text = (
        f"🔄 *Обмен {crypto.upper()}*\n\n"
        f"Курс: 1 {crypto.upper()} = {rate:.2f} ₽\n"
        f"Комиссия: {get_commission()}%\n"
        f"Мин. сумма: {min_amt} {crypto.upper()}\n\n"
        "Введите сумму"
    )
    await edit_or_send_message(callback, text, cancel_kb())
    await state.set_state(ExchangeFSM.waiting_amount)

@dp.message(ExchangeFSM.waiting_amount)
async def exchange_amount(message: types.Message, state: FSMContext):
    if not check_rate_limit(message.from_user.id, "exchange"):
        await message.answer("❌ Слишком много запросов", reply_markup=cancel_kb())
        return
    try:
        amount = float(message.text)
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число", reply_markup=cancel_kb())
        return
    data = await state.get_data()
    crypto = data['crypto']
    min_amt = MIN_EXCHANGE_AMOUNTS[crypto]
    if amount < min_amt:
        await message.answer(f"❌ Минимальная сумма: {min_amt} {crypto.upper()}", reply_markup=cancel_kb())
        return
    rate = get_exchange_rate(crypto)
    commission = get_commission()
    gross = amount * rate
    fee = gross * commission / 100
    net = gross - fee
    text = (
        f"🔄 *Обмен {crypto.upper()}*\n\n"
        f"Сумма: {amount:.4f} {crypto.upper()}\n"
        f"Курс: {rate:.2f} ₽\n"
        f"Комиссия ({commission}%): -{fee:.2f} ₽\n"
        f"Вы получите: *{net:.2f} ₽*\n\n"
        "Подтверждаете?"
    )
    await state.update_data(amount=amount, net=net)
    await message.answer(text, parse_mode="Markdown", reply_markup=confirm_exchange_kb())
    await state.set_state(ExchangeFSM.confirm)

@dp.callback_query(ExchangeFSM.confirm, F.data == "exchange_confirm")
async def exchange_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    crypto = data['crypto']
    amount = data['amount']
    net = data['net']
    user_id = callback.from_user.id
    memo = f"dep_{user_id}_{int(time.time())}"
    address = WALLETS[crypto]
    add_deposit(user_id, crypto, memo, amount)
    text = (
        f"🔄 *Обмен {crypto.upper()}*\n\n"
        f"📤 *Отправьте*\n`{address}`\n\n"
        f"📝 *Memo*\n`{memo}`\n\n"
        f"💰 Вы получите: {net:.2f} ₽\n\n"
        "⏱ Обычно зачисление занимает 1-5 минут."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_main")]])
    await edit_or_send_message(callback, text, kb)
    await state.clear()
    await notify_admin(f"🔄 Новая заявка\nПользователь: {user_id}\nВалюта: {crypto.upper()}\nСумма: {amount:.4f}")

@dp.callback_query(ExchangeFSM.confirm, F.data == "exchange_cancel")
async def exchange_cancel(callback: types.CallbackQuery, state: FSMContext):
    await back_to_main(callback, state)

@dp.callback_query(F.data == "menu_withdraw")
async def withdraw_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user(callback.from_user.id) and get_user(callback.from_user.id)['is_banned']:
        await callback.answer("⛔ Заблокирован", show_alert=True)
        return
    balance = get_balance(callback.from_user.id)
    if balance <= 0:
        await edit_or_send_message(callback, "❌ Нет средств", back_to_main_kb())
        return
    text = f"💸 *Вывод*\n\nБаланс: {balance:.2f} ₽\nМин: {MIN_WITHDRAWAL:.0f} ₽\nМакс: {MAX_WITHDRAWAL:.0f} ₽\n\nВведите сумму"
    await edit_or_send_message(callback, text, cancel_kb())
    await state.set_state(WithdrawFSM.waiting_amount)

@dp.message(WithdrawFSM.waiting_amount)
async def withdraw_amount(message: types.Message, state: FSMContext):
    if not check_rate_limit(message.from_user.id, "withdraw", limit=3, window=300):
        await message.answer("❌ Слишком много заявок, подождите 5 минут", reply_markup=cancel_kb())
        return
    try:
        amount = float(message.text)
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите число", reply_markup=cancel_kb())
        return
    balance = get_balance(message.from_user.id)
    if amount < MIN_WITHDRAWAL:
        await message.answer(f"❌ Минимум {MIN_WITHDRAWAL:.0f} ₽", reply_markup=cancel_kb())
        return
    if amount > MAX_WITHDRAWAL:
        await message.answer(f"❌ Максимум {MAX_WITHDRAWAL:.0f} ₽", reply_markup=cancel_kb())
        return
    if amount > balance:
        await message.answer(f"❌ Недостаточно средств. Баланс: {balance:.2f} ₽", reply_markup=cancel_kb())
        return
    await state.update_data(amount=amount)
    await message.answer("Введите реквизиты", reply_markup=cancel_kb())
    await state.set_state(WithdrawFSM.waiting_details)

@dp.message(WithdrawFSM.waiting_details)
async def withdraw_details(message: types.Message, state: FSMContext):
    details = message.text.strip()
    if not details or len(details) < 5:
        await message.answer("❌ Введите корректные реквизиты (мин 5 символов)", reply_markup=cancel_kb())
        return
    await state.update_data(details=details)
    data = await state.get_data()
    text = (
        f"💸 *Подтверждение*\n\n"
        f"Сумма: {data['amount']:.2f} ₽\n"
        f"Реквизиты: `{escape_markdown(data['details'])}`\n\n"
        "Подтверждаете?"
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=confirm_withdraw_kb())
    await state.set_state(WithdrawFSM.confirm)

@dp.callback_query(WithdrawFSM.confirm, F.data == "confirm_withdraw_yes")
async def withdraw_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    amount = data['amount']
    details = data['details']
    update_balance(callback.from_user.id, -amount)
    wid = add_withdrawal(callback.from_user.id, amount, details)
    await edit_or_send_message(callback, f"✅ Заявка #{wid} создана", back_to_main_kb())
    await state.clear()
    await notify_admin(f"💸 Новая заявка #{wid}\nПользователь: {callback.from_user.id}\nСумма: {amount:.2f} ₽")

@dp.callback_query(WithdrawFSM.confirm, F.data == "confirm_withdraw_no")
async def withdraw_cancel(callback: types.CallbackQuery, state: FSMContext):
    await back_to_main(callback, state)

@dp.callback_query(F.data == "menu_referrals")
async def show_referrals(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user(callback.from_user.id) and get_user(callback.from_user.id)['is_banned']:
        await callback.answer("⛔ Заблокирован", show_alert=True)
        return
    invited, bonus = get_referral_info(callback.from_user.id)
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={callback.from_user.id}"
    ref_percent = get_referral_percent()
    text = (
        f"👥 *Рефералы*\n\n"
        f"🔗 {link}\n"
        f"👤 Приглашено: {invited}\n"
        f"🎁 Бонусов: {bonus:.2f} ₽\n"
        f"💡 Вы получаете {ref_percent}% от обменов рефералов"
    )
    await edit_or_send_message(callback, text, back_to_main_kb())

@dp.callback_query(F.data == "menu_history")
async def show_history(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user(callback.from_user.id) and get_user(callback.from_user.id)['is_banned']:
        await callback.answer("⛔ Заблокирован", show_alert=True)
        return
    deposits, withdrawals = get_user_history(callback.from_user.id, limit=10)
    text = "📜 *История*\n\n"
    if deposits:
        text += "*Обмены*\n"
        for d in deposits:
            date = time.strftime("%d.%m.%Y %H:%M", time.localtime(d['created_at']))
            text += f"• {d['crypto']} {d['amount']:.4f} - {d['status']} ({date})\n"
    if withdrawals:
        text += "\n*Выводы*\n"
        for w in withdrawals:
            date = time.strftime("%d.%m.%Y %H:%M", time.localtime(w['created_at']))
            text += f"• {w['amount']:.2f} ₽ - {w['status']} ({date})\n"
    if not deposits and not withdrawals:
        text += "Операций нет"
    await edit_or_send_message(callback, text, back_to_main_kb())

@dp.callback_query(F.data == "menu_support")
async def support(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = f"🆘 *Поддержка*\n\n👉 @{escape_markdown(SUPPORT_USERNAME)}"
    await edit_or_send_message(callback, text, back_to_main_kb())

# -------------------- АДМИНКА --------------------
@dp.message(Command("admin"))
async def admin_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет доступа")
        return
    await state.clear()
    await message.answer("🛡 *Админ панель*", parse_mode="Markdown", reply_markup=admin_main_kb())

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True)
        return
    await state.clear()
    await edit_or_send_message(callback, "🛡 *Админ панель*", admin_main_kb())

@dp.callback_query(F.data == "admin_exit")
async def admin_exit(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await back_to_main(callback, state)

@dp.callback_query(F.data == "admin_withdrawals")
async def admin_withdrawals_filter(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True)
        return
    await state.clear()
    await edit_or_send_message(callback, "📋 *Заявки*", admin_withdrawals_filter_kb())

@dp.callback_query(F.data.startswith("admin_withdrawals_"))
async def admin_show_withdrawals(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True)
        return
    parts = callback.data.split("_")
    filter_type = parts[2] if len(parts) >= 3 else "all"
    page = int(parts[4]) if len(parts) >= 5 else 1
    withdrawals, total = get_withdrawals_paginated(filter_type if filter_type != "all" else None, page)
    if not withdrawals:
        await edit_or_send_message(callback, "📭 Нет заявок", admin_withdrawals_filter_kb())
        return
    for w in withdrawals:
        await callback.message.answer(
            f"📋 Заявка #{w['id']}\n👤 {w['user_id']}\n💰 {w['amount']:.2f} ₽\n📝 {w['details']}\n🏷 {w['status']}",
            reply_markup=withdrawal_action_kb(w['id'])
        )
    total_pages = (total + 4) // 5
    await callback.message.answer("📄 Страница", reply_markup=withdrawal_pagination_kb(filter_type, page, total_pages))
    await callback.answer()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_withdrawal(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True)
        return
    w_id = int(callback.data.split("_")[1])
    w = get_withdrawal(w_id)
    if w:
        log_admin_action(callback.from_user.id, "approve_withdrawal", w['user_id'], f"Заявка #{w_id}")
        update_withdrawal_status(w_id, 'completed')
        await bot.send_message(w['user_id'], f"✅ Вывод #{w_id} выполнен")
        await callback.message.edit_text(f"✅ Заявка #{w_id} подтверждена", reply_markup=back_to_admin_kb())
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_"))
async def reject_withdrawal_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True)
        return
    w_id = int(callback.data.split("_")[1])
    await state.update_data(withdraw_id=w_id)
    await edit_or_send_message(callback, "Введите причину отказа", back_to_admin_kb())
    await state.set_state(AdminRejectFSM.waiting_comment)

@dp.message(AdminRejectFSM.waiting_comment)
async def reject_comment(message: types.Message, state: FSMContext):
    data = await state.get_data()
    w_id = data['withdraw_id']
    w = get_withdrawal(w_id)
    if w:
        log_admin_action(message.from_user.id, "reject_withdrawal", w['user_id'], f"Заявка #{w_id}, причина: {message.text}")
        update_withdrawal_status(w_id, 'rejected', message.text)
        update_balance(w['user_id'], w['amount'])
        await bot.send_message(w['user_id'], f"❌ Вывод #{w_id} отклонён\nПричина: {message.text}")
        await message.answer(f"✅ Заявка #{w_id} отклонена", reply_markup=back_to_admin_kb())
    else:
        await message.answer("❌ Заявка не найдена", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True)
        return
    users, deposits, withdrawals, ref_count, ref_bonus = get_statistics()
    text = (
        f"📊 *Статистика*\n\n"
        f"👥 Пользователей: {users}\n"
        f"💰 Депозитов: {deposits:.2f} ₽\n"
        f"💸 Выводов: {withdrawals:.2f} ₽\n"
        f"👥 Рефералов: {ref_count}\n"
        f"🎁 Бонусов: {ref_bonus:.2f} ₽"
    )
    await edit_or_send_message(callback, text, back_to_admin_kb())

@dp.callback_query(F.data == "admin_rates")
async def admin_rates_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="rate_ton")],
        [InlineKeyboardButton(text="💰 USDT (TON)", callback_data="rate_usdt_ton")],
        [InlineKeyboardButton(text="💵 USDT (TRC20)", callback_data="rate_usdt_trc20")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])
    await edit_or_send_message(callback, "🔧 *Курсы*", kb)

@dp.callback_query(F.data.startswith("rate_"))
async def admin_set_rate_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True)
        return
    crypto = callback.data.split("_")[1]
    await state.update_data(crypto=crypto)
    current = get_exchange_rate(crypto)
    await edit_or_send_message(callback, f"Текущий курс {crypto}: {current:.2f} ₽\nВведите новый курс", back_to_admin_kb())
    await state.set_state(AdminRateFSM.waiting_rate)

@dp.message(AdminRateFSM.waiting_rate)
async def admin_set_rate_value(message: types.Message, state: FSMContext):
    try:
        rate = float(message.text)
        if rate <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число")
        return
    data = await state.get_data()
    set_exchange_rate(data['crypto'], rate)
    log_admin_action(message.from_user.id, "set_rate", details=f"{data['crypto']}: {rate:.2f}")
    await message.answer(f"✅ Курс {data['crypto']} установлен: {rate:.2f} ₽", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_commission")
async def admin_commission_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True)
        return
    current = get_commission()
    await edit_or_send_message(callback, f"Текущая комиссия: {current}%\nВведите новую", back_to_admin_kb())
    await state.set_state(AdminCommissionFSM.waiting_percent)

@dp.message(AdminCommissionFSM.waiting_percent)
async def admin_set_commission(message: types.Message, state: FSMContext):
    try:
        percent = float(message.text)
        if percent < 0 or percent > 100:
            raise ValueError
    except:
        await message.answer("❌ Введите число от 0 до 100")
        return
    set_commission(percent)
    log_admin_action(message.from_user.id, "set_commission", details=f"{percent}%")
    await message.answer(f"✅ Комиссия: {percent}%", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_referral")
async def admin_referral_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True)
        return
    current = get_referral_percent()
    await edit_or_send_message(callback, f"Реферальный %: {current}%\nВведите новый", back_to_admin_kb())
    await state.set_state(AdminReferralFSM.waiting_percent)

@dp.message(AdminReferralFSM.waiting_percent)
async def admin_set_referral(message: types.Message, state: FSMContext):
    try:
        percent = float(message.text)
        if percent < 0 or percent > 50:
            raise ValueError
    except:
        await message.answer("❌ Введите число от 0 до 50")
        return
    set_referral_percent(percent)
    log_admin_action(message.from_user.id, "set_referral_percent", details=f"{percent}%")
    await message.answer(f"✅ Реферальный %: {percent}%", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_balance")
async def admin_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True)
        return
    await edit_or_send_message(callback, "Введите ID пользователя", back_to_admin_kb())
    await state.set_state(AdminBalanceFSM.waiting_user_id)

@dp.message(AdminBalanceFSM.waiting_user_id)
async def admin_balance_user(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
    except:
        await message.answer("❌ Введите число")
        return
    user = get_user(user_id)
    if not user:
        await message.answer("❌ Пользователь не найден")
        return
    await state.update_data(user_id=user_id)
    await message.answer("Введите сумму (+ или -)")
    await state.set_state(AdminBalanceFSM.waiting_amount)

@dp.message(AdminBalanceFSM.waiting_amount)
async def admin_balance_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
    except:
        await message.answer("❌ Введите число")
        return
    data = await state.get_data()
    update_balance(data['user_id'], amount)
    new_balance = get_balance(data['user_id'])
    log_admin_action(message.from_user.id, "manual_balance_change", target_user=data['user_id'], details=f"{amount:+.2f}")
    await message.answer(f"✅ Баланс изменён на {amount:+.2f} ₽\nНовый: {new_balance:.2f} ₽", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_ban")
async def admin_ban_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True)
        return
    await edit_or_send_message(callback, "Введите ID пользователя для блокировки", back_to_admin_kb())
    await state.set_state(AdminBanFSM.waiting_user_id)

@dp.message(AdminBanFSM.waiting_user_id)
async def admin_ban_user(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
    except:
        await message.answer("❌ Введите число")
        return
    user = get_user(user_id)
    if not user:
        await message.answer("❌ Пользователь не найден")
        return
    with get_db() as (conn, c):
        c.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
    log_admin_action(message.from_user.id, "ban_user", target_user=user_id)
    await bot.send_message(user_id, "🚫 Ваш аккаунт заблокирован\nСвяжитесь с администратором")
    await message.answer(f"✅ Пользователь {user['username']} (ID {user_id}) заблокирован", reply_markup=back_to_admin_kb())
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
async def background_tasks():
    asyncio.create_task(update_rates_periodically())
    asyncio.create_task(check_deposits())
    asyncio.create_task(backup_loop())

async def backup_loop():
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        backup_database()

def run_bot():
    async def main():
        await background_tasks()
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
    async def delete_webhook():
        try:
            async with Bot(token=BOT_TOKEN) as temp_bot:
                await temp_bot.delete_webhook(drop_pending_updates=True)
                logger.info("✅ Вебхук удалён")
        except Exception as e:
            logger.error(f"Ошибка удаления вебхука: {e}")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(delete_webhook())
    loop.close()

    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        run_bot()
