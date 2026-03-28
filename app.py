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

# Валидация конфигурации
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

# Лимиты и настройки
MIN_WITHDRAWAL = float(os.environ.get("MIN_WITHDRAWAL", "100"))
MAX_WITHDRAWAL = float(os.environ.get("MAX_WITHDRAWAL", "100000"))
MIN_EXCHANGE = float(os.environ.get("MIN_EXCHANGE", "1"))
RATE_UPDATE_INTERVAL = int(os.environ.get("RATE_UPDATE_INTERVAL", "60"))  # Увеличил до 60 секунд
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
    """Получение соединения с БД с thread-local storage"""
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
    """Инициализация базы данных"""
    with get_db() as (conn, c):
        # Таблица пользователей
        c.execute('''CREATE TABLE IF NOT EXISTS users
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER UNIQUE,
                      username TEXT,
                      balance REAL DEFAULT 0,
                      ref_by INTEGER DEFAULT 0,
                      ref_bonus REAL DEFAULT 0,
                      is_banned INTEGER DEFAULT 0,
                      created_at INTEGER)''')
        
        # Таблица депозитов
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
        
        # Таблица выводов
        c.execute('''CREATE TABLE IF NOT EXISTS withdrawals
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER,
                      amount REAL,
                      details TEXT,
                      status TEXT,
                      admin_comment TEXT,
                      created_at INTEGER,
                      processed_at INTEGER)''')
        
        # Таблица курсов
        c.execute('''CREATE TABLE IF NOT EXISTS exchange_rates
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      crypto TEXT UNIQUE,
                      rate_rub REAL,
                      updated_at INTEGER)''')
        
        # Таблица реферальных начислений
        c.execute('''CREATE TABLE IF NOT EXISTS referral_earnings
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER,
                      from_user_id INTEGER,
                      amount REAL,
                      created_at INTEGER)''')
        
        # Таблица настроек
        c.execute('''CREATE TABLE IF NOT EXISTS settings
                     (key TEXT PRIMARY KEY,
                      value TEXT)''')
        
        # Таблица логов администраторов
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
                logger.warning(f"Ошибка создания индекса: {e}")
        
        # Настройки по умолчанию
        defaults = [
            ("commission", "5"),
            ("referral_percent", "5"),
            ("min_withdrawal", str(MIN_WITHDRAWAL)),
            ("max_withdrawal", str(MAX_WITHDRAWAL)),
        ]
        
        for key, value in defaults:
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        
        # Курсы по умолчанию
        default_rates = [
            ('ton', '500'),
            ('usdt_ton', '85'),
            ('usdt_trc20', '85')
        ]
        
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
    return float(get_setting("commission", "5"))

def set_commission(percent: float) -> None:
    set_setting("commission", str(percent))

def get_referral_percent() -> float:
    return float(get_setting("referral_percent", "5"))

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
            c.execute("SELECT id, user_id, amount, details, status, created_at FROM withdrawals WHERE status = 'pending' ORDER BY created_at DESC LIMIT ? OFFSET ?",
                     (per_page, offset))
            c.execute("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending'")
        elif status == 'completed':
            c.execute("SELECT id, user_id, amount, details, status, created_at FROM withdrawals WHERE status = 'completed' ORDER BY created_at DESC LIMIT ? OFFSET ?",
                     (per_page, offset))
            c.execute("SELECT COUNT(*) FROM withdrawals WHERE status = 'completed'")
        elif status == 'rejected':
            c.execute("SELECT id, user_id, amount, details, status, created_at FROM withdrawals WHERE status = 'rejected' ORDER BY created_at DESC LIMIT ? OFFSET ?",
                     (per_page, offset))
            c.execute("SELECT COUNT(*) FROM withdrawals WHERE status = 'rejected'")
        else:
            c.execute("SELECT id, user_id, amount, details, status, created_at FROM withdrawals ORDER BY created_at DESC LIMIT ? OFFSET ?",
                     (per_page, offset))
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
        c.execute("SELECT crypto, amount, status, created_at FROM deposits WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                 (user_id, limit))
        deposits = c.fetchall()
        c.execute("SELECT amount, status, created_at FROM withdrawals WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                 (user_id, limit))
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
        
        logger.info(f"✅ Создана резервная копия: {backup_path}")
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
    """Экранирование спецсимволов для Telegram Markdown"""
    if not text:
        return text
    
    special_chars = r'_*[]()~`>#+-=|{}.!'
    result = []
    for char in text:
        if char in special_chars:
            result.append('\\' + char)
        else:
            result.append(char)
    
    return ''.join(result)

# -------------------- ПОЛУЧЕНИЕ КУРСОВ (МНОЖЕСТВО API) --------------------
async def fetch_usdt_rub_multiple() -> Optional[float]:
    """Получение курса USDT/RUB из нескольких источников"""
    sources = [
        # Bybit
        {
            "name": "Bybit",
            "url": "https://api.bybit.com/v5/market/tickers?category=spot&symbol=USDTUSD",
            "parser": lambda data: float(data['result']['list'][0]['lastPrice']) if data.get('result') and data['result']['list'] else None
        },
        # Huobi
        {
            "name": "Huobi",
            "url": "https://api.huobi.pro/market/detail/merged?symbol=usdtusdt",
            "parser": lambda data: float(data['tick']['close']) if data.get('tick') else None
        },
        # KuCoin
        {
            "name": "KuCoin",
            "url": "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol=USDT-USDT",
            "parser": lambda data: float(data['data']['price']) if data.get('data') else None
        },
        # Gate.io
        {
            "name": "Gate.io",
            "url": "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=USDT_USDT",
            "parser": lambda data: float(data[0]['last']) if data and len(data) > 0 else None
        }
    ]
    
    for source in sources:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(source["url"], timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        usdt_usd = source["parser"](data)
                        if usdt_usd and usdt_usd > 0:
                            logger.info(f"USDT/USD from {source['name']}: {usdt_usd}")
                            # Получаем курс USD/RUB
                            usd_rub = await fetch_usd_rub()
                            if usd_rub:
                                return usdt_usd * usd_rub
        except Exception as e:
            logger.warning(f"Ошибка получения USDT/USD из {source['name']}: {e}")
    
    return None

async def fetch_usd_rub() -> Optional[float]:
    """Получение курса USD/RUB из различных источников"""
    sources = [
        {
            "name": "Central Bank of Russia",
            "url": "https://www.cbr-xml-daily.ru/daily_json.js",
            "parser": lambda data: float(data['Valute']['USD']['Value'])
        },
        {
            "name": "Currency API",
            "url": "https://api.exchangerate-api.com/v4/latest/USD",
            "parser": lambda data: float(data['rates']['RUB'])
        },
        {
            "name": "Free Currency API",
            "url": "https://api.freecurrencyapi.com/v1/latest?apikey=fca_live_6YpCQiHFz9DwHd0EBIz5CzrUdUcvNszjUBSWPLZP&base_currency=USD&currencies=RUB",
            "parser": lambda data: float(data['data']['RUB']) if data.get('data') else None
        }
    ]
    
    for source in sources:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(source["url"], timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rate = source["parser"](data)
                        if rate and rate > 0:
                            logger.info(f"USD/RUB from {source['name']}: {rate}")
                            return rate
        except Exception as e:
            logger.warning(f"Ошибка получения USD/RUB из {source['name']}: {e}")
    
    return None

async def fetch_ton_usdt_multiple() -> Optional[float]:
    """Получение курса TON/USDT из нескольких источников"""
    sources = [
        {
            "name": "Bybit",
            "url": "https://api.bybit.com/v5/market/tickers?category=spot&symbol=TONUSDT",
            "parser": lambda data: float(data['result']['list'][0]['lastPrice']) if data.get('result') and data['result']['list'] else None
        },
        {
            "name": "KuCoin",
            "url": "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol=TON-USDT",
            "parser": lambda data: float(data['data']['price']) if data.get('data') else None
        },
        {
            "name": "Gate.io",
            "url": "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=TON_USDT",
            "parser": lambda data: float(data[0]['last']) if data and len(data) > 0 else None
        },
        {
            "name": "MEXC",
            "url": "https://api.mexc.com/api/v3/ticker/price?symbol=TONUSDT",
            "parser": lambda data: float(data['price']) if data.get('price') else None
        }
    ]
    
    for source in sources:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(source["url"], timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rate = source["parser"](data)
                        if rate and rate > 0:
                            logger.info(f"TON/USDT from {source['name']}: {rate}")
                            return rate
        except Exception as e:
            logger.warning(f"Ошибка получения TON/USDT из {source['name']}: {e}")
    
    return None

async def fetch_real_rate(crypto: str) -> Optional[float]:
    """Получение реального курса с использованием множества API"""
    try:
        if crypto == 'ton':
            # Получаем TON/USDT
            ton_usdt = await fetch_ton_usdt_multiple()
            if not ton_usdt:
                logger.warning("Не удалось получить TON/USDT")
                return None
            
            # Получаем USD/RUB
            usd_rub = await fetch_usd_rub()
            if not usd_rub:
                logger.warning("Не удалось получить USD/RUB")
                return None
            
            rate = ton_usdt * usd_rub
            logger.info(f"TON/RUB рассчитан: {rate:.2f} (TON/USDT: {ton_usdt}, USD/RUB: {usd_rub})")
            return rate
        
        elif crypto in ('usdt_ton', 'usdt_trc20'):
            # USDT/RUB
            # Пробуем получить USDT/USD * USD/RUB
            usdt_usd = await fetch_usdt_usd_multiple()
            if usdt_usd:
                usd_rub = await fetch_usd_rub()
                if usd_rub:
                    rate = usdt_usd * usd_rub
                    logger.info(f"USDT/RUB рассчитан: {rate:.2f} (USDT/USD: {usdt_usd}, USD/RUB: {usd_rub})")
                    return rate
            
            # Альтернатива: прямой USDT/RUB
            return await fetch_usdt_rub_direct()
    
    except Exception as e:
        logger.error(f"Ошибка получения курса {crypto}: {e}")
    
    return None

async def fetch_usdt_usd_multiple() -> Optional[float]:
    """Получение курса USDT/USD"""
    sources = [
        {
            "name": "Bybit",
            "url": "https://api.bybit.com/v5/market/tickers?category=spot&symbol=USDTUSD",
            "parser": lambda data: float(data['result']['list'][0]['lastPrice']) if data.get('result') and data['result']['list'] else None
        },
        {
            "name": "KuCoin",
            "url": "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol=USDT-USDT",
            "parser": lambda data: float(data['data']['price']) if data.get('data') else None
        },
        {
            "name": "Gate.io",
            "url": "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=USDT_USDT",
            "parser": lambda data: float(data[0]['last']) if data and len(data) > 0 else None
        }
    ]
    
    for source in sources:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(source["url"], timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rate = source["parser"](data)
                        if rate and rate > 0:
                            logger.info(f"USDT/USD from {source['name']}: {rate}")
                            return rate
        except Exception as e:
            logger.warning(f"Ошибка получения USDT/USD из {source['name']}: {e}")
    
    return 1.0  # Возвращаем 1 если не удалось получить

async def fetch_usdt_rub_direct() -> Optional[float]:
    """Прямое получение USDT/RUB из обменников"""
    sources = [
        {
            "name": "BestChange",
            "url": "https://api.bestchange.ru/info.php?p=usdt-rub",
            "parser": lambda data: float(data['rate']) if data.get('rate') else None
        },
        {
            "name": "Coingecko",
            "url": "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub",
            "parser": lambda data: float(data['tether']['rub']) if data.get('tether') else None
        }
    ]
    
    for source in sources:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(source["url"], timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rate = source["parser"](data)
                        if rate and rate > 0:
                            logger.info(f"USDT/RUB direct from {source['name']}: {rate}")
                            return rate
        except Exception as e:
            logger.warning(f"Ошибка получения USDT/RUB из {source['name']}: {e}")
    
    return None

async def update_rates_periodically():
    """Обновление курсов"""
    while True:
        for crypto in ['ton', 'usdt_ton', 'usdt_trc20']:
            rate = await fetch_real_rate(crypto)
            
            if rate is not None:
                set_exchange_rate(crypto, rate)
                logger.info(f"✅ Курс {crypto} обновлён: {rate:.2f} ₽")
            else:
                logger.warning(f"❌ Не удалось обновить курс {crypto}")
                # Сохраняем старый курс из БД
                old_rate = get_exchange_rate(crypto)
                logger.info(f"📊 Используем старый курс {crypto}: {old_rate:.2f} ₽")
        
        await asyncio.sleep(RATE_UPDATE_INTERVAL)

# -------------------- ПРОВЕРКА ТРАНЗАКЦИЙ --------------------
async def check_ton_transaction(memo: str) -> Optional[Dict]:
    """Проверка транзакций TON"""
    try:
        address = WALLETS['ton']
        if not address or address in ("EQD...", ""):
            return None
        
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
                        value_nano = int(in_msg.get('value', 0))
                        amount_ton = value_nano / 1e9
                        tx_hash = tx.get('transaction_id', {}).get('hash')
                        return {'amount': amount_ton, 'tx_hash': tx_hash}
        return None
    except Exception as e:
        logger.error(f"Ошибка проверки TON: {e}")
        return None

async def check_trc20_transaction(memo: str) -> Optional[Dict]:
    """Проверка транзакций USDT TRC20"""
    try:
        address = WALLETS['usdt_trc20']
        if not address or address in ("T...", ""):
            return None
        
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
                            amount_sun = int(tx.get('amount', 0))
                            amount_usdt = amount_sun / 1e6
                            tx_hash = tx.get('hash')
                            return {'amount': amount_usdt, 'tx_hash': tx_hash}
        return None
    except Exception as e:
        logger.error(f"Ошибка проверки TRC20: {e}")
        return None

async def check_transaction_with_retry(crypto: str, memo: str) -> Optional[Dict]:
    """Проверка транзакции с повторными попытками"""
    for attempt in range(MAX_RETRIES):
        try:
            if crypto in ('ton', 'usdt_ton'):
                result = await check_ton_transaction(memo)
            elif crypto == 'usdt_trc20':
                result = await check_trc20_transaction(memo)
            else:
                return None
            
            if result:
                return result
            
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"Попытка {attempt + 1} не удалась: {e}")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
    
    return None

# -------------------- ПРОВЕРКА ДЕПОЗИТОВ --------------------
processed_transactions = set()

async def check_deposits():
    """Проверка депозитов"""
    while True:
        try:
            if len(processed_transactions) > 10000:
                processed_transactions.clear()
            
            with get_db() as (conn, c):
                c.execute("SELECT id, user_id, crypto, memo, amount FROM deposits WHERE status = 'pending'")
                pending = c.fetchall()
            
            for dep in pending:
                dep_id = dep['id']
                user_id = dep['user_id']
                crypto = dep['crypto']
                memo = dep['memo']
                expected_amount = dep['amount']
                
                if memo in processed_transactions:
                    continue
                
                tx = await check_transaction_with_retry(crypto, memo)
                
                if tx and tx.get('amount', 0) >= expected_amount:
                    processed_transactions.add(memo)
                    
                    amount_crypto = tx['amount']
                    tx_hash = tx.get('tx_hash')
                    rate = get_exchange_rate(crypto)
                    gross_rub = amount_crypto * rate
                    commission = get_commission()
                    fee = gross_rub * commission / 100
                    rub_amount = gross_rub - fee
                    
                    update_balance(user_id, rub_amount)
                    complete_deposit(dep_id, amount_crypto, rub_amount, user_id, tx_hash)
                    
                    await bot.send_message(
                        user_id,
                        f"✅ *Обмен выполнен*\n\n"
                        f"💰 Зачислено: {rub_amount:.2f} ₽\n"
                        f"📊 Курс: {rate:.2f} ₽ за {crypto.upper()}\n"
                        f"💸 Комиссия: {fee:.2f} ₽",
                        parse_mode="Markdown"
                    )
                    
                    for admin_id in ADMIN_IDS:
                        await bot.send_message(
                            admin_id,
                            f"✅ *Обмен завершён*\n"
                            f"👤 Пользователь: {user_id}\n"
                            f"💎 Валюта: {crypto.upper()}\n"
                            f"📊 Сумма: {amount_crypto:.4f} {crypto.upper()}\n"
                            f"💰 Зачислено: {rub_amount:.2f} ₽",
                            parse_mode="Markdown"
                        )
        
        except Exception as e:
            logger.error(f"Ошибка в check_deposits: {e}")
        
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

# -------------------- БОТ --------------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

async def send_welcome_message(target, user_id: int, username: str, ref_by: int = None):
    user = get_user(user_id)
    if user and user['is_banned']:
        await target.answer("⛔ Ваш аккаунт заблокирован")
        return
    
    create_user(user_id, username, ref_by)
    
    text = (
        "✨ *Добро пожаловать в CryptoExchangeBot* ✨\n\n"
        "Этот бот поможет вам обменять криптовалюту на рубли\n\n"
        f"💸 *Комиссия* {get_commission()}%\n"
        f"🎁 *Рефералка* {get_referral_percent()}%\n\n"
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
        logger.warning(f"Не удалось отредактировать: {e}")
        try:
            await callback.message.answer(text, parse_mode="Markdown", reply_markup=reply_markup)
        except:
            await callback.message.answer(text, reply_markup=reply_markup)
    await callback.answer()

# -------------------- ХЕНДЛЕРЫ --------------------
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
    balance = get_balance(callback.from_user.id)
    text = f"💰 *Баланс*\n\n{balance:.2f} ₽"
    await edit_or_send_message(callback, text, back_to_main_kb())

@dp.callback_query(F.data == "menu_exchange")
async def exchange_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = "🔄 *Обмен*\n\nВыберите криптовалюту"
    await edit_or_send_message(callback, text, exchange_menu_kb())

@dp.callback_query(F.data.startswith("exchange_"))
async def exchange_select_crypto(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    crypto = callback.data.split("_")[1]
    
    if crypto not in WALLETS or not WALLETS[crypto]:
        await callback.answer("Кошелёк не настроен", show_alert=True)
        return
    
    await state.update_data(crypto=crypto)
    rate = get_exchange_rate(crypto)
    
    text = (
        f"🔄 *Обмен {crypto.upper()}*\n\n"
        f"Курс: 1 {crypto.upper()} = {rate:.2f} ₽\n"
        f"Комиссия: {get_commission()}%\n\n"
        "Введите сумму"
    )
    
    await edit_or_send_message(callback, text, cancel_kb())
    await state.set_state(ExchangeFSM.waiting_amount)

@dp.message(ExchangeFSM.waiting_amount)
async def exchange_amount(message: types.Message, state: FSMContext):
    try:
        amount_crypto = float(message.text)
        if amount_crypto <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите число", reply_markup=cancel_kb())
        return
    
    data = await state.get_data()
    crypto = data['crypto']
    rate = get_exchange_rate(crypto)
    commission = get_commission()
    gross_rub = amount_crypto * rate
    fee = gross_rub * commission / 100
    net_rub = gross_rub - fee
    
    text = (
        f"🔄 *Обмен*\n\n"
        f"Сумма: {amount_crypto:.4f} {crypto.upper()}\n"
        f"Курс: {rate:.2f} ₽\n"
        f"Комиссия ({commission}%): -{fee:.2f} ₽\n"
        f"Вы получите: *{net_rub:.2f} ₽*\n\n"
        "Подтверждаете?"
    )
    
    await state.update_data(amount_crypto=amount_crypto, net_rub=net_rub)
    await message.answer(text, parse_mode="Markdown", reply_markup=confirm_exchange_kb())
    await state.set_state(ExchangeFSM.confirm)

@dp.callback_query(ExchangeFSM.confirm, F.data == "exchange_confirm")
async def exchange_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    crypto = data['crypto']
    amount_crypto = data['amount_crypto']
    net_rub = data['net_rub']
    user_id = callback.from_user.id
    memo = f"dep_{user_id}_{int(time.time())}"
    address = WALLETS[crypto]
    
    add_deposit(user_id, crypto, memo, amount_crypto)
    
    text = (
        f"🔄 *Обмен {crypto.upper()}*\n\n"
        f"📤 *Отправьте*\n`{address}`\n\n"
        f"📝 *Memo*\n`{memo}`\n\n"
        f"💰 Вы получите: {net_rub:.2f} ₽"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ В главное меню", callback_data="back_to_main")]
    ])
    
    await edit_or_send_message(callback, text, kb)
    await state.clear()

@dp.callback_query(ExchangeFSM.confirm, F.data == "exchange_cancel")
async def exchange_cancel(callback: types.CallbackQuery, state: FSMContext):
    await back_to_main(callback, state)

@dp.callback_query(F.data == "menu_withdraw")
async def withdraw_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    balance = get_balance(callback.from_user.id)
    
    if balance <= 0:
        await edit_or_send_message(callback, "❌ Нет средств", back_to_main_kb())
        return
    
    text = f"💸 *Вывод*\n\nБаланс: {balance:.2f} ₽\n\nВведите сумму"
    await edit_or_send_message(callback, text, cancel_kb())
    await state.set_state(WithdrawFSM.waiting_amount)

@dp.message(WithdrawFSM.waiting_amount)
async def withdraw_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите число", reply_markup=cancel_kb())
        return
    
    balance = get_balance(message.from_user.id)
    
    if amount > balance:
        await message.answer(f"❌ Недостаточно средств. Баланс: {balance:.2f} ₽", reply_markup=cancel_kb())
        return
    
    if amount < MIN_WITHDRAWAL:
        await message.answer(f"❌ Минимум {MIN_WITHDRAWAL} ₽", reply_markup=cancel_kb())
        return
    
    await state.update_data(amount=amount)
    await message.answer("Введите реквизиты", reply_markup=cancel_kb())
    await state.set_state(WithdrawFSM.waiting_details)

@dp.message(WithdrawFSM.waiting_details)
async def withdraw_details(message: types.Message, state: FSMContext):
    await state.update_data(details=message.text)
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
    withdraw_id = add_withdrawal(callback.from_user.id, amount, details)
    
    text = f"✅ Заявка #{withdraw_id} создана"
    await edit_or_send_message(callback, text, back_to_main_kb())
    await state.clear()
    
    for admin_id in ADMIN_IDS:
        await bot.send_message(admin_id, f"💸 Заявка #{withdraw_id}\nПользователь: {callback.from_user.id}\nСумма: {amount} ₽")

@dp.callback_query(WithdrawFSM.confirm, F.data == "confirm_withdraw_no")
async def withdraw_cancel(callback: types.CallbackQuery, state: FSMContext):
    await back_to_main(callback, state)

@dp.callback_query(F.data == "menu_referrals")
async def show_referrals(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    invited, bonus = get_referral_info(callback.from_user.id)
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={callback.from_user.id}"
    
    text = (
        f"👥 *Рефералы*\n\n"
        f"🔗 {link}\n"
        f"👤 Приглашено: {invited}\n"
        f"🎁 Бонусов: {bonus:.2f} ₽"
    )
    
    await edit_or_send_message(callback, text, back_to_main_kb())

@dp.callback_query(F.data == "menu_history")
async def show_history(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    deposits, withdrawals = get_user_history(callback.from_user.id)
    
    text = "📜 *История*\n\n"
    
    if deposits:
        text += "*Обмены*\n"
        for d in deposits:
            text += f"• {d['crypto']} {d['amount']:.4f} - {d['status']}\n"
    
    if withdrawals:
        text += "\n*Выводы*\n"
        for w in withdrawals:
            text += f"• {w['amount']:.2f} ₽ - {w['status']}\n"
    
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
    await state.clear()
    await edit_or_send_message(callback, "🛡 *Админ панель*", admin_main_kb())

@dp.callback_query(F.data == "admin_exit")
async def admin_exit(callback: types.CallbackQuery, state: FSMContext):
    await back_to_main(callback, state)

@dp.callback_query(F.data == "admin_withdrawals")
async def admin_withdrawals(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await edit_or_send_message(callback, "📋 *Заявки*", admin_withdrawals_filter_kb())

@dp.callback_query(F.data.startswith("admin_withdrawals_"))
async def admin_show_withdrawals(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
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
            f"📋 Заявка #{w['id']}\n"
            f"👤 {w['user_id']}\n"
            f"💰 {w['amount']:.2f} ₽\n"
            f"📝 {w['details']}\n"
            f"🏷 {w['status']}",
            reply_markup=withdrawal_action_kb(w['id'])
        )
    
    total_pages = (total + 4) // 5
    await callback.message.answer("📄 Страница", reply_markup=withdrawal_pagination_kb(filter_type, page, total_pages))
    await callback.answer()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_withdrawal(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    withdraw_id = int(callback.data.split("_")[1])
    w = get_withdrawal(withdraw_id)
    
    if w:
        update_withdrawal_status(withdraw_id, 'completed')
        await bot.send_message(w['user_id'], f"✅ Вывод #{withdraw_id} выполнен")
        await callback.message.edit_text(f"✅ Заявка #{withdraw_id} подтверждена", reply_markup=back_to_admin_kb())
    
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_"))
async def reject_withdrawal(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    withdraw_id = int(callback.data.split("_")[1])
    await state.update_data(withdraw_id=withdraw_id)
    await edit_or_send_message(callback, "Введите причину отказа", back_to_admin_kb())
    await state.set_state(AdminRejectFSM.waiting_comment)

@dp.message(AdminRejectFSM.waiting_comment)
async def reject_comment(message: types.Message, state: FSMContext):
    data = await state.get_data()
    withdraw_id = data['withdraw_id']
    w = get_withdrawal(withdraw_id)
    
    if w:
        update_withdrawal_status(withdraw_id, 'rejected', message.text)
        update_balance(w['user_id'], w['amount'])
        await bot.send_message(w['user_id'], f"❌ Вывод #{withdraw_id} отклонён\nПричина: {message.text}")
        await message.answer(f"✅ Заявка #{withdraw_id} отклонена", reply_markup=back_to_admin_kb())
    
    await state.clear()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    users, deposits, withdrawals, referred, bonuses = get_statistics()
    text = (
        f"📊 *Статистика*\n\n"
        f"👥 Пользователей: {users}\n"
        f"💰 Депозитов: {deposits:.2f} ₽\n"
        f"💸 Выводов: {withdrawals:.2f} ₽\n"
        f"👥 Рефералов: {referred}\n"
        f"🎁 Бонусов: {bonuses:.2f} ₽"
    )
    
    await edit_or_send_message(callback, text, back_to_admin_kb())

@dp.callback_query(F.data == "admin_rates")
async def admin_rates(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="rate_ton")],
        [InlineKeyboardButton(text="💰 USDT (TON)", callback_data="rate_usdt_ton")],
        [InlineKeyboardButton(text="💵 USDT (TRC20)", callback_data="rate_usdt_trc20")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])
    
    await edit_or_send_message(callback, "🔧 *Курсы*", kb)

@dp.callback_query(F.data.startswith("rate_"))
async def admin_set_rate(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
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
        await message.answer("❌ Введите число")
        return
    
    data = await state.get_data()
    set_exchange_rate(data['crypto'], rate)
    await message.answer(f"✅ Курс установлен: {rate:.2f} ₽", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_commission")
async def admin_commission(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
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
        await message.answer("❌ Введите 0-100")
        return
    
    set_commission(percent)
    await message.answer(f"✅ Комиссия: {percent}%", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_referral")
async def admin_referral(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
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
        await message.answer("❌ Введите 0-50")
        return
    
    set_referral_percent(percent)
    await message.answer(f"✅ Реферальный %: {percent}%", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_balance")
async def admin_balance(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
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
    
    if not get_user(user_id):
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
    
    await message.answer(f"✅ Баланс изменён на {amount:+.2f} ₽\nНовый баланс: {new_balance:.2f} ₽", reply_markup=back_to_admin_kb())
    await state.clear()

# -------------------- FLASK --------------------
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is running!"

@app.route('/health')
def health():
    return "OK"

# -------------------- ЗАПУСК --------------------
async def start_background_tasks():
    asyncio.create_task(update_rates_periodically())
    asyncio.create_task(check_deposits())
    asyncio.create_task(backup_loop())

async def backup_loop():
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        backup_database()

def run_bot():
    async def main():
        await start_background_tasks()
        await dp.start_polling(bot)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    finally:
        loop.close()

if __name__ == "__main__":
    logger.info("🚀 Запуск бота...")
    
    is_railway = bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN"))
    
    if is_railway:
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        run_bot()
