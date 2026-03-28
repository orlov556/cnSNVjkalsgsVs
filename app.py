import os
import asyncio
import sqlite3
import time
import aiohttp
import logging
import re
import threading
import shutil
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
MIN_EXCHANGE = float(os.environ.get("MIN_EXCHANGE", "1"))  # Минимальная сумма обмена в USD эквиваленте
RATE_UPDATE_INTERVAL = int(os.environ.get("RATE_UPDATE_INTERVAL", "30"))
DEPOSIT_CHECK_INTERVAL = int(os.environ.get("DEPOSIT_CHECK_INTERVAL", "60"))
BACKUP_INTERVAL = int(os.environ.get("BACKUP_INTERVAL", "86400"))  # 24 часа
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

# -------------------- БАЗА ДАННЫХ (с thread-local) --------------------
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
    """Инициализация базы данных со всеми таблицами и индексами"""
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
        
        # Индексы для оптимизации
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_users_user_id ON users (user_id)",
            "CREATE INDEX IF NOT EXISTS idx_users_ref_by ON users (ref_by)",
            "CREATE INDEX IF NOT EXISTS idx_deposits_user_status ON deposits (user_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_deposits_memo ON deposits (memo)",
            "CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals (status)",
            "CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals (user_id)",
            "CREATE INDEX IF NOT EXISTS idx_withdrawals_created ON withdrawals (created_at)",
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
            ("min_exchange", str(MIN_EXCHANGE))
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

# Вызываем инициализацию
init_database()

# -------------------- ФУНКЦИИ РАБОТЫ С БД --------------------
def get_setting(key: str, default: str = None) -> Optional[str]:
    """Получение настройки"""
    with get_db() as (conn, c):
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = c.fetchone()
        return row['value'] if row else default

def set_setting(key: str, value: str) -> None:
    """Установка настройки"""
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
        
        # Начисление реферального бонуса
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
    """Логирование действий администраторов"""
    with get_db() as (conn, c):
        c.execute("INSERT INTO admin_logs (admin_id, action, target_user, details, created_at) VALUES (?, ?, ?, ?, ?)",
                 (admin_id, action, target_user, details, int(time.time())))
        conn.commit()

def backup_database() -> bool:
    """Создание резервной копии базы данных"""
    try:
        backup_dir = "backups"
        os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = f"{backup_dir}/exchange_{timestamp}.db"
        
        shutil.copy2(DB_PATH, backup_path)
        
        # Удаляем старые копии (старше 7 дней)
        for file in os.listdir(backup_dir):
            file_path = os.path.join(backup_dir, file)
            if os.path.isfile(file_path):
                file_time = datetime.fromtimestamp(os.path.getctime(file_path))
                if datetime.now() - file_time > timedelta(days=7):
                    os.remove(file_path)
        
        logger.info(f"✅ Создана резервная копия: {backup_path}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка создания резервной копии: {e}")
        return False

# -------------------- ЛИМИТЫ ДЕЙСТВИЙ --------------------
user_actions = defaultdict(list)

def check_rate_limit(user_id: int, action: str, limit: int = 10, window: int = 60) -> bool:
    """Проверка лимитов на действия пользователя"""
    now = datetime.now()
    key = f"{user_id}:{action}"
    actions = user_actions[key]
    
    # Удаляем старые действия
    actions = [act for act in actions if now - act < timedelta(seconds=window)]
    
    if len(actions) >= limit:
        return False
    
    actions.append(now)
    user_actions[key] = actions
    return True

# -------------------- СООБЩЕНИЯ ОБ ОШИБКАХ --------------------
ERROR_MESSAGES = {
    "insufficient_balance": "❌ Недостаточно средств.\n💰 Ваш баланс: {balance:.2f} ₽",
    "invalid_amount": "❌ Пожалуйста, введите корректную сумму.\n📝 Пример: 0.5 или 100",
    "invalid_crypto": "❌ Выбранная валюта временно недоступна",
    "invalid_wallet": "❌ Кошелёк для этой валюты не настроен.\n👨‍💻 Свяжитесь с администратором",
    "min_withdraw": "❌ Минимальная сумма вывода: {min_amount} ₽",
    "max_withdraw": "❌ Максимальная сумма вывода: {max_amount} ₽",
    "min_exchange": "❌ Минимальная сумма обмена: {min_amount} {crypto}",
    "rate_limit": "❌ Слишком много запросов.\n⏱ Подождите {wait_time} секунд",
    "network_error": "❌ Ошибка сети.\n🔄 Пожалуйста, попробуйте позже",
    "db_error": "❌ Техническая ошибка.\n👨‍💻 Администратор уже уведомлён",
    "invalid_memo": "❌ Неверный комментарий.\n📝 Убедитесь, что скопировали memo полностью",
    "access_denied": "⛔ У вас нет доступа к этой команде",
    "user_banned": "⛔ Ваш аккаунт заблокирован.\n👨‍💻 Обратитесь к администратору",
}

def get_error_message(error_key: str, **kwargs) -> str:
    """Получение локализованного сообщения об ошибке"""
    message = ERROR_MESSAGES.get(error_key, "❌ Произошла ошибка")
    return message.format(**kwargs)

# -------------------- ПРОВЕРКА ТРАНЗАКЦИЙ (с retry) --------------------
async def check_ton_transaction(memo: str) -> Optional[Dict]:
    """Проверка транзакций TON через TON Center API"""
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
        logger.error(f"Ошибка проверки TON транзакции: {e}")
        return None

async def check_trc20_transaction(memo: str) -> Optional[Dict]:
    """Проверка транзакций USDT TRC20 через TronGrid API"""
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
                    if tx.get('contractType') == 31:  # TRC20 transfer
                        extra = tx.get('extra_param', {})
                        if extra.get('memo') == memo:
                            amount_sun = int(tx.get('amount', 0))
                            amount_usdt = amount_sun / 1e6
                            tx_hash = tx.get('hash')
                            return {'amount': amount_usdt, 'tx_hash': tx_hash}
        return None
    except Exception as e:
        logger.error(f"Ошибка проверки TRC20 транзакции: {e}")
        return None

async def check_transaction_with_retry(crypto: str, memo: str, max_retries: int = MAX_RETRIES) -> Optional[Dict]:
    """Проверка транзакции с повторными попытками"""
    for attempt in range(max_retries):
        try:
            if crypto in ('ton', 'usdt_ton'):
                result = await check_ton_transaction(memo)
            elif crypto == 'usdt_trc20':
                result = await check_trc20_transaction(memo)
            else:
                return None
            
            if result:
                return result
            
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # Экспоненциальная задержка
        except Exception as e:
            logger.error(f"Попытка {attempt + 1} не удалась: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
    
    return None

# -------------------- АВТООБНОВЛЕНИЕ КУРСОВ --------------------
async def fetch_real_rate(crypto: str) -> Optional[float]:
    """Получение реального курса"""
    try:
        async with aiohttp.ClientSession() as session:
            if crypto == 'ton':
                # Получаем TON/USDT с Binance
                async with session.get("https://api.binance.com/api/v3/ticker/price?symbol=TONUSDT", timeout=10) as resp:
                    if resp.status != 200:
                        logger.warning(f"TON/USDT недоступен, статус {resp.status}")
                        return None
                    data = await resp.json()
                    ton_usdt = float(data['price'])
                
                # Получаем USDT/RUB с CoinGecko
                async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub", timeout=10) as resp:
                    if resp.status != 200:
                        logger.warning(f"USDT/RUB недоступен, статус {resp.status}")
                        return None
                    data = await resp.json()
                    usdt_rub = data['tether']['rub']
                
                return ton_usdt * usdt_rub
            
            elif crypto in ('usdt_ton', 'usdt_trc20'):
                # USDT/RUB с CoinGecko
                async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub", timeout=10) as resp:
                    if resp.status != 200:
                        logger.warning(f"USDT/RUB недоступен, статус {resp.status}")
                        return None
                    data = await resp.json()
                    return data['tether']['rub']
    
    except Exception as e:
        logger.error(f"Ошибка получения курса {crypto}: {e}")
    
    return None

async def update_rates_periodically():
    """Обновление курсов с повторными попытками"""
    while True:
        for crypto in ['ton', 'usdt_ton', 'usdt_trc20']:
            rate = await fetch_real_rate(crypto)
            
            if rate is not None:
                set_exchange_rate(crypto, rate)
                logger.info(f"Курс {crypto} обновлён: {rate:.2f} ₽")
            else:
                logger.warning(f"Не удалось обновить курс {crypto}")
                # Попытка с задержкой
                await asyncio.sleep(2)
                rate = await fetch_real_rate(crypto)
                if rate is not None:
                    set_exchange_rate(crypto, rate)
                    logger.info(f"Курс {crypto} обновлён после повтора: {rate:.2f} ₽")
        
        await asyncio.sleep(RATE_UPDATE_INTERVAL)

# -------------------- ПРОВЕРКА ДЕПОЗИТОВ --------------------
processed_transactions = set()
MAX_PROCESSED_SIZE = 10000

async def check_deposits():
    """Проверка неподтверждённых депозитов"""
    while True:
        try:
            # Очищаем старые записи
            if len(processed_transactions) > MAX_PROCESSED_SIZE:
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
                
                # Пропускаем уже обработанные
                if memo in processed_transactions:
                    continue
                
                # Проверяем транзакцию
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
                    
                    # Обновляем баланс и статус депозита
                    update_balance(user_id, rub_amount)
                    complete_deposit(dep_id, amount_crypto, rub_amount, user_id, tx_hash)
                    
                    # Уведомляем пользователя
                    await bot.send_message(
                        user_id,
                        f"✅ *Обмен выполнен*\n\n"
                        f"💰 Зачислено: {rub_amount:.2f} ₽\n"
                        f"📊 Курс: {rate:.2f} ₽ за {crypto.upper()}\n"
                        f"💸 Комиссия: {fee:.2f} ₽\n"
                        f"🔗 Хэш транзакции: `{tx_hash}`",
                        parse_mode="Markdown"
                    )
                    
                    # Уведомляем админов
                    await notify_admin(
                        f"✅ *Обмен завершён*\n"
                        f"👤 Пользователь: {user_id}\n"
                        f"💎 Валюта: {crypto.upper()}\n"
                        f"📊 Сумма: {amount_crypto:.2f} {crypto.upper()}\n"
                        f"💰 Зачислено: {rub_amount:.2f} ₽"
                    )
        
        except Exception as e:
            logger.error(f"Ошибка в check_deposits: {e}")
        
        await asyncio.sleep(DEPOSIT_CHECK_INTERVAL)

# -------------------- ПРОВЕРКА ЗДОРОВЬЯ API --------------------
async def check_apis_health() -> Dict[str, bool]:
    """Проверка доступности всех внешних API"""
    apis = {
        "binance": "https://api.binance.com/api/v3/ping",
        "coingecko": "https://api.coingecko.com/api/v3/ping",
        "toncenter": "https://toncenter.com/api/v2/getBlockchainConfig",
        "tronscan": "https://apilist.tronscan.org/api/system/status"
    }
    
    status = {}
    for name, url in apis.items():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5) as resp:
                    status[name] = resp.status == 200
        except Exception as e:
            status[name] = False
            logger.warning(f"API {name} недоступен: {e}")
    
    # Уведомляем админов о проблемах
    if not all(status.values()):
        problem_apis = [name for name, ok in status.items() if not ok]
        await notify_admin(f"⚠️ Проблемы с API:\n{', '.join(problem_apis)}")
    
    return status

async def health_check_loop():
    """Циклическая проверка здоровья API"""
    while True:
        await check_apis_health()
        await asyncio.sleep(300)  # 5 минут

# -------------------- РЕЗЕРВНОЕ КОПИРОВАНИЕ --------------------
async def backup_loop():
    """Циклическое резервное копирование"""
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        backup_database()

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

def escape_markdown(text: str) -> str:
    """Экранирование спецсимволов Markdown"""
    special_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{char}' if char in special_chars else char for char in text)

async def send_welcome_message(target, user_id: int, username: str, ref_by: int = None):
    """Отправка приветственного сообщения"""
    # Проверка на бан
    user = get_user(user_id)
    if user and user['is_banned']:
        await target.answer(get_error_message("user_banned"))
        return
    
    create_user(user_id, username, ref_by)
    
    text = (
        "✨ *Добро пожаловать в CryptoExchangeBot* ✨\n\n"
        "Этот бот поможет вам обменять криптовалюту на рубли по выгодному курсу.\n\n"
        "📌 *Как это работает*\n"
        "1️⃣ Выберите криптовалюту (TON, USDT TON или USDT TRC20)\n"
        "2️⃣ Введите сумму, которую хотите обменять\n"
        "3️⃣ Отправьте криптовалюту на указанный кошелёк с обязательным комментарием (memo)\n"
        "4️⃣ После зачисления рубли поступят на ваш баланс автоматически\n"
        "5️⃣ Вы можете вывести рубли на карту или счёт\n\n"
        f"💸 *Комиссия сервиса* {get_commission()}%\n"
        f"🎁 *Реферальная программа* {get_referral_percent()}% от суммы обменов ваших рефералов\n\n"
        f"💰 *Минимальная сумма вывода*: {MIN_WITHDRAWAL} ₽\n"
        f"💎 *Минимальная сумма обмена*: {MIN_EXCHANGE} USD\n\n"
        "⬇️ *Выберите действие*"
    )
    
    if WELCOME_IMAGE_URL:
        try:
            if isinstance(target, types.Message):
                await target.answer_photo(photo=WELCOME_IMAGE_URL, caption=text, parse_mode="Markdown", reply_markup=main_menu_kb())
            else:
                await target.message.answer_photo(photo=WELCOME_IMAGE_URL, caption=text, parse_mode="Markdown", reply_markup=main_menu_kb())
        except Exception as e:
            logger.error(f"Ошибка отправки фото: {e}")
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
    """Редактирование или отправка сообщения"""
    try:
        if callback.message.text:
            await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        else:
            await callback.message.edit_caption(caption=text, parse_mode="Markdown", reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"Не удалось отредактировать сообщение: {e}")
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=reply_markup)
    await callback.answer()

async def notify_admin(text: str):
    """Уведомление администраторов"""
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

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
    
    # Проверка на бан
    user = get_user(callback.from_user.id)
    if user and user['is_banned']:
        await callback.answer(get_error_message("user_banned"), show_alert=True)
        return
    
    balance = get_balance(callback.from_user.id)
    text = f"💰 *Ваш баланс*\n\n{balance:.2f} ₽\n\nЗдесь отображаются рубли, полученные за обмен криптовалюты."
    await edit_or_send_message(callback, text, back_to_main_kb())

@dp.callback_query(F.data == "menu_exchange")
async def exchange_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    
    # Проверка на бан
    user = get_user(callback.from_user.id)
    if user and user['is_banned']:
        await callback.answer(get_error_message("user_banned"), show_alert=True)
        return
    
    text = (
        "🔄 *Обмен криптовалюты на рубли*\n\n"
        "Выберите криптовалюту, которую хотите обменять\n\n"
        "💎 *TON* - нативный токен сети TON\n"
        "💰 *USDT (TON)* - стейблкоин в сети TON\n"
        "💵 *USDT (TRC20)* - стейблкоин в сети TRON\n\n"
        f"💰 *Минимальная сумма*: {MIN_EXCHANGE} USD"
    )
    await edit_or_send_message(callback, text, exchange_menu_kb())

@dp.callback_query(F.data.startswith("exchange_"))
async def exchange_select_crypto(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    
    # Проверка на бан
    user = get_user(callback.from_user.id)
    if user and user['is_banned']:
        await callback.answer(get_error_message("user_banned"), show_alert=True)
        return
    
    crypto = callback.data.split("_")[1]
    
    if crypto not in WALLETS:
        await callback.answer("Ошибка: выбранная валюта не поддерживается", show_alert=True)
        return
    
    if not WALLETS[crypto]:
        await callback.answer(get_error_message("invalid_wallet"), show_alert=True)
        return
    
    await state.update_data(crypto=crypto)
    rate = get_exchange_rate(crypto)
    
    text = (
        f"🔄 *Обмен {crypto.upper()} на рубли*\n\n"
        f"Текущий курс: 1 {crypto.upper()} = {rate:.2f} ₽\n"
        f"Комиссия сервиса: {get_commission()}%\n"
        f"Минимальная сумма: {MIN_EXCHANGE} USD\n\n"
        "Введите сумму, которую хотите обменять (в криптовалюте)"
    )
    
    await edit_or_send_message(callback, text, cancel_kb())
    await state.set_state(ExchangeFSM.waiting_amount)

@dp.message(ExchangeFSM.waiting_amount)
async def exchange_amount(message: types.Message, state: FSMContext):
    # Проверка лимитов
    if not check_rate_limit(message.from_user.id, "exchange", limit=5, window=60):
        await message.answer(get_error_message("rate_limit", wait_time=60))
        return
    
    try:
        amount_crypto = float(message.text)
        if amount_crypto <= 0:
            raise ValueError
    except:
        await message.answer(get_error_message("invalid_amount"), reply_markup=cancel_kb())
        return
    
    data = await state.get_data()
    crypto = data['crypto']
    rate = get_exchange_rate(crypto)
    
    # Проверка минимальной суммы
    min_amount_usd = MIN_EXCHANGE
    amount_usd = amount_crypto * rate / get_exchange_rate('usdt_trc20')
    if amount_usd < min_amount_usd:
        await message.answer(
            get_error_message("min_exchange", min_amount=min_amount_usd, crypto=crypto.upper()),
            reply_markup=cancel_kb()
        )
        return
    
    commission = get_commission()
    gross_rub = amount_crypto * rate
    fee = gross_rub * commission / 100
    net_rub = gross_rub - fee
    
    text = (
        f"🔄 *Обмен {crypto.upper()}*\n\n"
        f"Сумма: {amount_crypto:.4f} {crypto.upper()}\n"
        f"Курс: 1 {crypto.upper()} = {rate:.2f} ₽\n"
        f"Комиссия ({commission}%): -{fee:.2f} ₽\n"
        f"Вы получите: *{net_rub:.2f} ₽*\n\n"
        "Подтверждаете обмен?"
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
        f"📤 *Отправьте на адрес*\n`{address}`\n\n"
        f"📝 *Обязательный комментарий (memo)*\n`{memo}`\n\n"
        f"💰 Вы получите: {net_rub:.2f} ₽ (после вычета комиссии)\n\n"
        f"⚡️ *Важно*\n"
        f"• Переводите только {crypto.upper()} на указанный адрес\n"
        f"• Укажите комментарий точно, как выше\n"
        f"• После зачисления рубли поступят на ваш баланс автоматически\n\n"
        f"⏱ Обычно зачисление занимает 1-5 минут."
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ В главное меню", callback_data="back_to_main")]
    ])
    
    await edit_or_send_message(callback, text, kb)
    await state.clear()
    
    await notify_admin(
        f"🔄 *Новая заявка на обмен*\n"
        f"👤 Пользователь: {user_id}\n"
        f"💎 Валюта: {crypto.upper()}\n"
        f"📊 Сумма: {amount_crypto:.4f} {crypto.upper()}\n"
        f"💰 К получению: {net_rub:.2f} ₽"
    )

@dp.callback_query(ExchangeFSM.confirm, F.data == "exchange_cancel")
async def exchange_cancel(callback: types.CallbackQuery, state: FSMContext):
    await back_to_main(callback, state)
    await state.clear()

@dp.callback_query(F.data == "menu_withdraw")
async def withdraw_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    
    # Проверка на бан
    user = get_user(callback.from_user.id)
    if user and user['is_banned']:
        await callback.answer(get_error_message("user_banned"), show_alert=True)
        return
    
    balance = get_balance(callback.from_user.id)
    
    if balance <= 0:
        text = "❌ *У вас нет средств для вывода*\n\nПополните баланс через обмен криптовалюты."
        await edit_or_send_message(callback, text, back_to_main_kb())
        return
    
    text = (
        "💸 *Вывод рублей*\n\n"
        f"💰 Ваш баланс: *{balance:.2f} ₽*\n"
        f"📊 Минимальная сумма: {MIN_WITHDRAWAL} ₽\n"
        f"📊 Максимальная сумма: {MAX_WITHDRAWAL} ₽\n\n"
        "Введите сумму, которую хотите вывести (в рублях)"
    )
    
    await edit_or_send_message(callback, text, cancel_kb())
    await state.set_state(WithdrawFSM.waiting_amount)

@dp.message(WithdrawFSM.waiting_amount)
async def withdraw_amount(message: types.Message, state: FSMContext):
    # Проверка лимитов
    if not check_rate_limit(message.from_user.id, "withdraw", limit=3, window=300):
        await message.answer(get_error_message("rate_limit", wait_time=300))
        return
    
    try:
        amount = float(message.text)
        if amount <= 0:
            raise ValueError
    except:
        await message.answer(get_error_message("invalid_amount"), reply_markup=cancel_kb())
        return
    
    balance = get_balance(message.from_user.id)
    
    if amount < MIN_WITHDRAWAL:
        await message.answer(get_error_message("min_withdraw", min_amount=MIN_WITHDRAWAL), reply_markup=cancel_kb())
        return
    
    if amount > MAX_WITHDRAWAL:
        await message.answer(get_error_message("max_withdraw", max_amount=MAX_WITHDRAWAL), reply_markup=cancel_kb())
        return
    
    if amount > balance:
        await message.answer(get_error_message("insufficient_balance", balance=balance), reply_markup=cancel_kb())
        return
    
    await state.update_data(amount=amount)
    
    text = (
        "💸 *Вывод рублей*\n\n"
        f"💰 Сумма вывода: *{amount:.2f} ₽*\n\n"
        "Введите реквизиты для выплаты (номер карты, счёта или телефона)"
    )
    
    await message.answer(text, parse_mode="Markdown", reply_markup=cancel_kb())
    await state.set_state(WithdrawFSM.waiting_details)

@dp.message(WithdrawFSM.waiting_details)
async def withdraw_details(message: types.Message, state: FSMContext):
    details = message.text.strip()
    
    if not details or len(details) < 5:
        await message.answer("❌ Введите корректные реквизиты (минимум 5 символов)", reply_markup=cancel_kb())
        return
    
    await state.update_data(details=details)
    data = await state.get_data()
    
    text = (
        "💸 *Подтверждение вывода*\n\n"
        f"💰 Сумма: *{data['amount']:.2f} ₽*\n"
        f"📝 Реквизиты: `{escape_markdown(data['details'])}`\n\n"
        "Проверьте данные. Подтверждаете?"
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
    
    text = (
        f"✅ *Заявка на вывод создана*\n\n"
        f"📋 Номер заявки: #{withdraw_id}\n"
        f"💰 Сумма: {amount:.2f} ₽\n\n"
        "⏱ Ожидайте подтверждения администратора.\n"
        "Вы получите уведомление, когда заявка будет обработана."
    )
    
    await edit_or_send_message(callback, text, back_to_main_kb())
    await state.clear()
    
    await notify_admin(
        f"💸 *Новая заявка на вывод*\n"
        f"👤 Пользователь: {callback.from_user.id}\n"
        f"💰 Сумма: {amount:.2f} ₽\n"
        f"📝 Реквизиты: {details}"
    )

@dp.callback_query(WithdrawFSM.confirm, F.data == "confirm_withdraw_no")
async def withdraw_cancel(callback: types.CallbackQuery, state: FSMContext):
    await back_to_main(callback, state)
    await state.clear()

@dp.callback_query(F.data == "menu_referrals")
async def show_referrals(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    
    # Проверка на бан
    user = get_user(callback.from_user.id)
    if user and user['is_banned']:
        await callback.answer(get_error_message("user_banned"), show_alert=True)
        return
    
    invited, bonus = get_referral_info(callback.from_user.id)
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={callback.from_user.id}"
    ref_percent = get_referral_percent()
    
    text = (
        "👥 *Реферальная программа*\n\n"
        f"🔗 Ваша реферальная ссылка\n`{link}`\n\n"
        f"👤 Приглашено: *{invited}* чел\n"
        f"🎁 Заработано бонусов: *{bonus:.2f} ₽*\n\n"
        f"💡 Вы получаете *{ref_percent}%* от суммы обменов ваших рефералов (после вычета комиссии)\n"
        "Бонусы начисляются автоматически и доступны для вывода."
    )
    
    await edit_or_send_message(callback, text, back_to_main_kb())

@dp.callback_query(F.data == "menu_history")
async def show_history(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    
    # Проверка на бан
    user = get_user(callback.from_user.id)
    if user and user['is_banned']:
        await callback.answer(get_error_message("user_banned"), show_alert=True)
        return
    
    deposits, withdrawals = get_user_history(callback.from_user.id, limit=10)
    
    text = "📜 *История операций*\n\n"
    
    if deposits:
        text += "*🔄 Обмены (пополнения)*\n"
        for d in deposits:
            crypto, amount, status, ts = d
            date = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
            status_emoji = "✅" if status == "completed" else "⏳"
            text += f"{status_emoji} {crypto} {amount:.4f} - {status} ({date})\n"
    
    if withdrawals:
        text += "\n*💸 Выводы*\n"
        for w in withdrawals:
            amount, status, ts = w
            date = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
            status_emoji = "✅" if status == "completed" else "❌" if status == "rejected" else "⏳"
            text += f"{status_emoji} {amount:.2f} ₽ - {status} ({date})\n"
    
    if not deposits and not withdrawals:
        text += "📭 Операций пока нет."
    
    await edit_or_send_message(callback, text, back_to_main_kb())

@dp.callback_query(F.data == "menu_support")
async def support(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    
    text = f"🆘 *Поддержка*\n\nЕсли у вас возникли вопросы или проблемы, свяжитесь с нашим специалистом\n\n👉 @{SUPPORT_USERNAME}"
    await edit_or_send_message(callback, text, back_to_main_kb())

# -------------------- АДМИНКА --------------------
@dp.message(Command("admin"))
async def admin_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer(get_error_message("access_denied"))
        return
    
    await state.clear()
    text = "🛡 *Панель администратора*\n\nВыберите действие"
    await message.answer(text, parse_mode="Markdown", reply_markup=admin_main_kb())

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = "🛡 *Панель администратора*\n\nВыберите действие"
    await edit_or_send_message(callback, text, admin_main_kb())

@dp.callback_query(F.data == "admin_exit")
async def admin_exit(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await back_to_main(callback, state)

@dp.callback_query(F.data == "admin_withdrawals")
async def admin_withdrawals_filter(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer(get_error_message("access_denied"), show_alert=True)
        return
    
    await state.clear()
    text = "📋 *Заявки на вывод*\n\nВыберите фильтр"
    await edit_or_send_message(callback, text, admin_withdrawals_filter_kb())

@dp.callback_query(F.data.startswith("admin_withdrawals_"))
async def admin_show_withdrawals(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer(get_error_message("access_denied"), show_alert=True)
        return
    
    parts = callback.data.split("_")
    
    if len(parts) == 3:
        filter_type = parts[2]
        page = 1
    elif len(parts) == 5:
        filter_type = parts[2]
        page = int(parts[4])
    else:
        return
    
    withdrawals, total = get_withdrawals_paginated(filter_type if filter_type != "all" else None, page, per_page=5)
    
    if not withdrawals:
        text = "📭 Нет заявок по выбранному фильтру."
        await edit_or_send_message(callback, text, admin_withdrawals_filter_kb())
        return
    
    # Отправляем заявки
    for w in withdrawals:
        w_id, user_id, amount, details, status, ts = w
        date = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
        
        await callback.message.answer(
            f"📋 *Заявка #{w_id}*\n"
            f"👤 Пользователь: `{user_id}`\n"
            f"💰 Сумма: {amount:.2f} ₽\n"
            f"📝 Реквизиты: `{escape_markdown(details)}`\n"
            f"🏷 Статус: {status}\n"
            f"📅 Дата: {date}",
            parse_mode="Markdown",
            reply_markup=withdrawal_action_kb(w_id)
        )
    
    total_pages = (total + 4) // 5
    pagination_kb = withdrawal_pagination_kb(filter_type, page, total_pages)
    await callback.message.answer("📄 Страница", reply_markup=pagination_kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_withdrawal(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer(get_error_message("access_denied"), show_alert=True)
        return
    
    withdraw_id = int(callback.data.split("_")[1])
    w = get_withdrawal(withdraw_id)
    
    if w:
        # Логируем действие
        log_admin_action(
            admin_id=callback.from_user.id,
            action="approve_withdrawal",
            target_user=w['user_id'],
            details=f"Заявка #{withdraw_id} на сумму {w['amount']:.2f} ₽"
        )
        
        update_withdrawal_status(withdraw_id, 'completed')
        
        # Уведомляем пользователя
        await bot.send_message(
            w['user_id'],
            f"✅ *Заявка на вывод #{withdraw_id}*\n"
            f"💰 Сумма: {w['amount']:.2f} ₽\n\n"
            f"✅ Заявка подтверждена и выполнена.",
            parse_mode="Markdown"
        )
        
        await callback.message.edit_text(f"✅ Заявка #{withdraw_id} подтверждена.", reply_markup=back_to_admin_kb())
    else:
        await callback.message.edit_text(f"❌ Заявка #{withdraw_id} не найдена.", reply_markup=back_to_admin_kb())
    
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_"))
async def reject_withdrawal_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer(get_error_message("access_denied"), show_alert=True)
        return
    
    withdraw_id = int(callback.data.split("_")[1])
    await state.update_data(withdraw_id=withdraw_id)
    
    text = f"❌ *Отклонение заявки #{withdraw_id}*\n\nВведите причину отклонения (будет отправлена пользователю)"
    await edit_or_send_message(callback, text, back_to_admin_kb())
    await state.set_state(AdminRejectFSM.waiting_comment)

@dp.message(AdminRejectFSM.waiting_comment)
async def reject_withdrawal_comment(message: types.Message, state: FSMContext):
    data = await state.get_data()
    withdraw_id = data['withdraw_id']
    comment = message.text
    
    w = get_withdrawal(withdraw_id)
    
    if w:
        # Логируем действие
        log_admin_action(
            admin_id=message.from_user.id,
            action="reject_withdrawal",
            target_user=w['user_id'],
            details=f"Заявка #{withdraw_id}, причина: {comment}"
        )
        
        update_withdrawal_status(withdraw_id, 'rejected', comment)
        
        # Возвращаем средства
        update_balance(w['user_id'], w['amount'])
        
        # Уведомляем пользователя
        await bot.send_message(
            w['user_id'],
            f"❌ *Заявка на вывод #{withdraw_id}*\n"
            f"💰 Сумма: {w['amount']:.2f} ₽\n\n"
            f"❌ Заявка отклонена.\n"
            f"📝 Причина: {comment}\n\n"
            f"💰 Средства возвращены на баланс.",
            parse_mode="Markdown"
        )
        
        await message.answer(f"✅ Заявка #{withdraw_id} отклонена, средства возвращены.", reply_markup=back_to_admin_kb())
    else:
        await message.answer(f"❌ Заявка #{withdraw_id} не найдена.", reply_markup=back_to_admin_kb())
    
    await state.clear()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer(get_error_message("access_denied"), show_alert=True)
        return
    
    users_count, total_deposits, total_withdrawals, referred_count, total_ref_bonus = get_statistics()
    
    text = (
        "📊 *Статистика*\n\n"
        f"👥 Пользователей: {users_count}\n"
        f"👥 Приглашённых: {referred_count}\n"
        f"💰 Всего пополнений (обменов): {total_deposits:.2f} ₽\n"
        f"💸 Всего выводов: {total_withdrawals:.2f} ₽\n"
        f"💵 В обороте: {total_deposits - total_withdrawals:.2f} ₽\n"
        f"🎁 Выплачено реферальных бонусов: {total_ref_bonus:.2f} ₽"
    )
    
    await edit_or_send_message(callback, text, back_to_admin_kb())

@dp.callback_query(F.data == "admin_rates")
async def admin_rates_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer(get_error_message("access_denied"), show_alert=True)
        return
    
    text = "🔧 *Управление курсами*\n\nВыберите криптовалюту для изменения курса"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="rate_ton")],
        [InlineKeyboardButton(text="💰 USDT (TON)", callback_data="rate_usdt_ton")],
        [InlineKeyboardButton(text="💵 USDT (TRC20)", callback_data="rate_usdt_trc20")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])
    
    await edit_or_send_message(callback, text, kb)

@dp.callback_query(F.data.startswith("rate_"))
async def admin_set_rate_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer(get_error_message("access_denied"), show_alert=True)
        return
    
    crypto = callback.data.split("_")[1]
    await state.update_data(crypto=crypto)
    current = get_exchange_rate(crypto)
    
    text = f"🔧 *Изменение курса {crypto.upper()}*\n\nТекущий курс: {current:.2f} ₽\nВведите новый курс (рублей за единицу)"
    await edit_or_send_message(callback, text, back_to_admin_kb())
    await state.set_state(AdminRateFSM.waiting_rate)

@dp.message(AdminRateFSM.waiting_rate)
async def admin_set_rate_value(message: types.Message, state: FSMContext):
    try:
        rate = float(message.text)
        if rate <= 0:
            raise ValueError
    except:
        await message.answer(get_error_message("invalid_amount"))
        return
    
    data = await state.get_data()
    crypto = data['crypto']
    
    set_exchange_rate(crypto, rate)
    
    # Логируем действие
    log_admin_action(
        admin_id=message.from_user.id,
        action="set_rate",
        details=f"{crypto}: {rate:.2f} ₽"
    )
    
    await message.answer(f"✅ Курс для {crypto.upper()} установлен: {rate:.2f} ₽", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_commission")
async def admin_commission_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer(get_error_message("access_denied"), show_alert=True)
        return
    
    current = get_commission()
    text = f"⚙️ *Комиссия сервиса*\n\nТекущая комиссия: {current}%\nВведите новое значение (процент от суммы обмена)"
    await edit_or_send_message(callback, text, back_to_admin_kb())
    await state.set_state(AdminCommissionFSM.waiting_percent)

@dp.message(AdminCommissionFSM.waiting_percent)
async def admin_set_commission(message: types.Message, state: FSMContext):
    try:
        percent = float(message.text)
        if percent < 0 or percent > 100:
            raise ValueError
    except:
        await message.answer("❌ Введите число от 0 до 100.")
        return
    
    set_commission(percent)
    
    # Логируем действие
    log_admin_action(
        admin_id=message.from_user.id,
        action="set_commission",
        details=f"{percent}%"
    )
    
    await message.answer(f"✅ Комиссия установлена: {percent}%", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_referral")
async def admin_referral_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer(get_error_message("access_denied"), show_alert=True)
        return
    
    current = get_referral_percent()
    text = f"🎁 *Реферальный процент*\n\nТекущий процент: {current}%\nВведите новое значение (процент от суммы обмена реферала)"
    await edit_or_send_message(callback, text, back_to_admin_kb())
    await state.set_state(AdminReferralFSM.waiting_percent)

@dp.message(AdminReferralFSM.waiting_percent)
async def admin_set_referral(message: types.Message, state: FSMContext):
    try:
        percent = float(message.text)
        if percent < 0 or percent > 50:
            raise ValueError
    except:
        await message.answer("❌ Введите число от 0 до 50.")
        return
    
    set_referral_percent(percent)
    
    # Логируем действие
    log_admin_action(
        admin_id=message.from_user.id,
        action="set_referral_percent",
        details=f"{percent}%"
    )
    
    await message.answer(f"✅ Реферальный процент установлен: {percent}%", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_balance")
async def admin_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer(get_error_message("access_denied"), show_alert=True)
        return
    
    text = "👤 *Ручное изменение баланса*\n\nВведите Telegram ID пользователя"
    await edit_or_send_message(callback, text, back_to_admin_kb())
    await state.set_state(AdminBalanceFSM.waiting_user_id)

@dp.message(AdminBalanceFSM.waiting_user_id)
async def admin_balance_user(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
    except:
        await message.answer("❌ ID должен быть числом.")
        return
    
    # Проверяем существование пользователя
    user = get_user(user_id)
    if not user:
        await message.answer(f"❌ Пользователь с ID {user_id} не найден.")
        return
    
    await state.update_data(user_id=user_id, username=user['username'])
    await message.answer("Введите сумму изменения (положительная - зачисление, отрицательная - списание)")
    await state.set_state(AdminBalanceFSM.waiting_amount)

@dp.message(AdminBalanceFSM.waiting_amount)
async def admin_balance_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
    except:
        await message.answer(get_error_message("invalid_amount"))
        return
    
    data = await state.get_data()
    user_id = data['user_id']
    username = data.get('username', str(user_id))
    
    update_balance(user_id, amount)
    new_balance = get_balance(user_id)
    
    # Логируем действие
    log_admin_action(
        admin_id=message.from_user.id,
        action="manual_balance_change",
        target_user=user_id,
        details=f"Изменение на {amount:.2f} ₽, новый баланс: {new_balance:.2f} ₽"
    )
    
    # Уведомляем пользователя
    try:
        await bot.send_message(
            user_id,
            f"👤 *Изменение баланса*\n\n"
            f"💰 Сумма: {amount:+.2f} ₽\n"
            f"💵 Новый баланс: {new_balance:.2f} ₽\n\n"
            f"👨‍💻 Администратор изменил ваш баланс.",
            parse_mode="Markdown"
        )
    except:
        pass
    
    await message.answer(
        f"✅ Баланс пользователя {username} (ID: {user_id}) изменён на {amount:+.2f} ₽.\n"
        f"💰 Новый баланс: {new_balance:.2f} ₽",
        reply_markup=back_to_admin_kb()
    )
    await state.clear()

@dp.callback_query(F.data == "admin_ban")
async def admin_ban_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer(get_error_message("access_denied"), show_alert=True)
        return
    
    text = "🚫 *Блокировка пользователя*\n\nВведите Telegram ID пользователя для блокировки"
    await edit_or_send_message(callback, text, back_to_admin_kb())
    await state.set_state(AdminBanFSM.waiting_user_id)

@dp.message(AdminBanFSM.waiting_user_id)
async def admin_ban_user(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
    except:
        await message.answer("❌ ID должен быть числом.")
        return
    
    user = get_user(user_id)
    if not user:
        await message.answer(f"❌ Пользователь с ID {user_id} не найден.")
        return
    
    with get_db() as (conn, c):
        c.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
    
    # Логируем действие
    log_admin_action(
        admin_id=message.from_user.id,
        action="ban_user",
        target_user=user_id,
        details=f"Заблокирован пользователь {user['username']}"
    )
    
    # Уведомляем пользователя
    try:
        await bot.send_message(
            user_id,
            "🚫 *Ваш аккаунт заблокирован*\n\n"
            "Свяжитесь с администратором для выяснения причин.\n"
            f"👉 @{SUPPORT_USERNAME}",
            parse_mode="Markdown"
        )
    except:
        pass
    
    await message.answer(
        f"✅ Пользователь {user['username']} (ID: {user_id}) заблокирован.",
        reply_markup=back_to_admin_kb()
    )
    await state.clear()

# -------------------- FLASK (для health check) --------------------
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is running on Railway!"

@app.route('/health')
def health():
    return "OK"

@app.route('/stats')
def stats():
    """Простая статистика для мониторинга"""
    users_count, total_deposits, total_withdrawals, _, _ = get_statistics()
    return jsonify({
        "status": "ok",
        "users": users_count,
        "total_deposits": total_deposits,
        "total_withdrawals": total_withdrawals,
        "balance": total_deposits - total_withdrawals
    })

# -------------------- ЗАПУСК --------------------
def run_bot_polling():
    async def start_polling():
        logger.info("🚀 Запуск бота через polling...")
        
        # Запускаем фоновые задачи
        asyncio.create_task(update_rates_periodically())
        asyncio.create_task(check_deposits())
        asyncio.create_task(health_check_loop())
        asyncio.create_task(backup_loop())
        
        await dp.start_polling(bot)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(start_polling())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен")
    finally:
        loop.close()

if __name__ == "__main__":
    # Валидация конфигурации
    logger.info("🔧 Проверка конфигурации...")
    
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не установлен!")
        exit(1)
    
    if not ADMIN_IDS:
        logger.error("❌ ADMIN_IDS не установлен!")
        exit(1)
    
    # Проверка кошельков
    for crypto, wallet in WALLETS.items():
        if not wallet or wallet in ("EQD...", "T...", ""):
            logger.warning(f"⚠️ Кошелёк для {crypto} не настроен")
    
    logger.info("✅ Конфигурация проверена")
    logger.info(f"🤖 Бот запущен с ID админов: {ADMIN_IDS}")
    
    is_railway = bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN"))
    
    if is_railway:
        logger.info("🚂 Запуск на Railway с polling...")
        bot_thread = Thread(target=run_bot_polling, daemon=True)
        bot_thread.start()
        
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        run_bot_polling()
