import os
import asyncio
import sqlite3
import time
import aiohttp
import logging
import threading
import base64
import re
from datetime import datetime, timedelta
from collections import defaultdict
from flask import Flask
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_IDS", "").split(",") if id.strip()]
WELCOME_IMAGE_URL = os.environ.get("WELCOME_IMAGE_URL", "")
SUPPORT_LINK = "https://t.me/cryptohelp_01"

TON_WALLET = "UQAunfNNErk6s1VC4ycJD2UI_U7aAK53M1LM1ebAv4vbqcDs"
USDT_TON_WALLET = "UQAunfNNErk6s1VC4ycJD2UI_U7aAK53M1LM1ebAv4vbqcDs"
USDT_TRC20_WALLET = "TGt4Jpn5xk7CzkxeDynnkhwVyDDU124g6B"

WALLETS = {
    'ton': TON_WALLET,
    'usdt_ton': USDT_TON_WALLET,
    'usdt_trc20': USDT_TRC20_WALLET
}

if not BOT_TOKEN:
    exit(1)

MIN_EXCHANGE = 1.0

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def escape_markdown(text):
    """Экранирует специальные символы Markdown"""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

def clean_text(text):
    """Очищает текст для безопасного отображения в Markdown"""
    if not text:
        return text
    # Экранируем все специальные символы
    special_chars = r'_*[]()~`>#+-=|{}.!'
    for char in special_chars:
        text = text.replace(char, '\\' + char)
    return text

def format_address(address):
    """Форматирует адрес для отображения в Markdown"""
    return f"`{address}`" if address else "Не указан"

def format_memo(memo):
    """Форматирует memo для отображения в Markdown"""
    return f"`{memo}`" if memo else "Не указан"

conn = sqlite3.connect("exchange.db", check_same_thread=False)
c = conn.cursor()

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
    amount REAL,
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
    rate_rub REAL)''')

c.execute('''CREATE TABLE IF NOT EXISTS referral_earnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    from_user_id INTEGER,
    amount REAL,
    created_at INTEGER)''')

c.execute('''CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT)''')

c.execute("INSERT OR IGNORE INTO exchange_rates (crypto, rate_rub) VALUES (?, ?)", ('ton', 100))
c.execute("INSERT OR IGNORE INTO exchange_rates (crypto, rate_rub) VALUES (?, ?)", ('usdt', 85))
c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('commission', '7'))
c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('referral_percent', '1'))
c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('min_withdrawal', '1000'))
c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('max_withdrawal', '100000'))
conn.commit()

def get_user(user_id):
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return c.fetchone()

def create_user(user_id, username, ref_by=None):
    if get_user(user_id):
        return
    c.execute("INSERT INTO users (user_id, username, ref_by, created_at) VALUES (?, ?, ?, ?)",
              (user_id, username, ref_by, int(time.time())))
    conn.commit()

def get_balance(user_id):
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    return row[0] if row else 0

def update_balance(user_id, amount):
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()

def add_deposit(user_id, crypto, memo, amount):
    c.execute("INSERT INTO deposits (user_id, crypto, memo, amount, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (user_id, crypto, memo, amount, 'pending', int(time.time())))
    conn.commit()
    return c.lastrowid

def complete_deposit(deposit_id, amount_crypto, rub_amount, user_id, tx_hash=None):
    c.execute("UPDATE deposits SET amount = ?, status = 'completed', completed_at = ?, tx_hash = ? WHERE id = ?",
              (amount_crypto, int(time.time()), tx_hash, deposit_id))
    conn.commit()
    
    c.execute("SELECT ref_by FROM users WHERE user_id = ?", (user_id,))
    ref = c.fetchone()
    if ref and ref[0]:
        c.execute("SELECT value FROM settings WHERE key = 'referral_percent'")
        ref_pct = float(c.fetchone()[0])
        bonus = rub_amount * ref_pct / 100
        if bonus > 0:
            update_balance(ref[0], bonus)
            c.execute("UPDATE users SET ref_bonus = ref_bonus + ? WHERE user_id = ?", (bonus, ref[0]))
            c.execute("INSERT INTO referral_earnings (user_id, from_user_id, amount, created_at) VALUES (?, ?, ?, ?)",
                      (ref[0], user_id, bonus, int(time.time())))
            conn.commit()

def add_withdrawal(user_id, amount, details):
    c.execute("INSERT INTO withdrawals (user_id, amount, details, status, created_at) VALUES (?, ?, ?, ?, ?)",
              (user_id, amount, details, 'pending', int(time.time())))
    conn.commit()
    return c.lastrowid

def get_withdrawal(wid):
    c.execute("SELECT * FROM withdrawals WHERE id = ?", (wid,))
    return c.fetchone()

def update_withdrawal_status(wid, status, comment=None):
    if comment:
        c.execute("UPDATE withdrawals SET status = ?, admin_comment = ?, processed_at = ? WHERE id = ?",
                  (status, comment, int(time.time()), wid))
    else:
        c.execute("UPDATE withdrawals SET status = ?, processed_at = ? WHERE id = ?",
                  (status, int(time.time()), wid))
    conn.commit()

def get_exchange_rate(crypto):
    if crypto in ('usdt_ton', 'usdt_trc20'):
        crypto = 'usdt'
    c.execute("SELECT rate_rub FROM exchange_rates WHERE crypto = ?", (crypto,))
    row = c.fetchone()
    return row[0] if row else (100 if crypto == 'ton' else 85)

def set_exchange_rate(crypto, rate):
    if crypto in ('usdt_ton', 'usdt_trc20'):
        crypto = 'usdt'
    c.execute("UPDATE exchange_rates SET rate_rub = ? WHERE crypto = ?", (rate, crypto))
    conn.commit()

def get_commission():
    c.execute("SELECT value FROM settings WHERE key = 'commission'")
    return float(c.fetchone()[0])

def set_commission(value):
    c.execute("UPDATE settings SET value = ? WHERE key = 'commission'", (str(value),))
    conn.commit()

def get_referral_percent():
    c.execute("SELECT value FROM settings WHERE key = 'referral_percent'")
    return float(c.fetchone()[0])

def set_referral_percent(value):
    c.execute("UPDATE settings SET value = ? WHERE key = 'referral_percent'", (str(value),))
    conn.commit()

def get_min_withdrawal():
    c.execute("SELECT value FROM settings WHERE key = 'min_withdrawal'")
    return float(c.fetchone()[0])

def set_min_withdrawal(value):
    c.execute("UPDATE settings SET value = ? WHERE key = 'min_withdrawal'", (str(value),))
    conn.commit()

def get_max_withdrawal():
    c.execute("SELECT value FROM settings WHERE key = 'max_withdrawal'")
    return float(c.fetchone()[0])

def set_max_withdrawal(value):
    c.execute("UPDATE settings SET value = ? WHERE key = 'max_withdrawal'", (str(value),))
    conn.commit()

def get_statistics():
    c.execute("SELECT COUNT(*) FROM users WHERE is_banned = 0")
    users = c.fetchone()[0]
    c.execute("SELECT SUM(amount) FROM deposits WHERE status = 'completed'")
    deposits = c.fetchone()[0] or 0
    c.execute("SELECT SUM(amount) FROM withdrawals WHERE status = 'completed'")
    withdrawals = c.fetchone()[0] or 0
    return users, deposits, withdrawals

def get_all_users():
    c.execute("SELECT user_id FROM users WHERE is_banned = 0")
    return [row[0] for row in c.fetchall()]

last_known_rates = {'usdt': 85.0, 'ton': 100.0}

async def get_usdt_rate(retries=3):
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub",
                                      timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return float(data['tether']['rub'])
        except:
            pass
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return float(data['Valute']['USD']['Value'])
        except:
            pass
        await asyncio.sleep(2)
    return last_known_rates['usdt']

async def get_ton_rate(retries=3):
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=rub",
                                      timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return float(data['the-open-network']['rub'])
        except:
            pass
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.bybit.com/v5/market/tickers?category=spot&symbol=TONUSDT", timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get('result') and data['result']['list']:
                            ton_usdt = float(data['result']['list'][0]['lastPrice'])
                            usdt_rate = await get_usdt_rate()
                            return ton_usdt * usdt_rate
        except:
            pass
        await asyncio.sleep(2)
    return last_known_rates['ton']

async def update_rates_loop():
    global last_known_rates
    while True:
        try:
            usdt = await get_usdt_rate()
            if usdt:
                last_known_rates['usdt'] = usdt
                set_exchange_rate('usdt', usdt)
                logger.info(f"USDT курс: {usdt:.2f} ₽")
            
            ton = await get_ton_rate()
            if ton:
                last_known_rates['ton'] = ton
                set_exchange_rate('ton', ton)
                logger.info(f"TON курс: {ton:.2f} ₽")
        except Exception as e:
            logger.error(f"Ошибка обновления курсов: {e}")
        await asyncio.sleep(30)

async def check_ton_tx(memo, required_amount, retries=3):
    address = WALLETS['ton']
    apis = [
        f"https://toncenter.com/api/v2/getTransactions?address={address}&limit=100",
        f"https://tonapi.io/v2/accounts/{address}/events?limit=100",
    ]
    
    for attempt in range(retries):
        for api_url in apis:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(api_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            
                            if 'result' in data:
                                for tx in data.get('result', []):
                                    in_msg = tx.get('in_msg')
                                    if in_msg:
                                        tx_memo = None
                                        if in_msg.get('message'):
                                            tx_memo = in_msg.get('message')
                                        elif in_msg.get('msg_data') and in_msg['msg_data'].get('text'):
                                            try:
                                                tx_memo = base64.b64decode(in_msg['msg_data']['text']).decode('utf-8')
                                            except:
                                                tx_memo = None
                                        
                                        if tx_memo and tx_memo == memo:
                                            amount = int(in_msg.get('value', 0)) / 1e9
                                            tx_hash = tx.get('transaction_id', {}).get('hash')
                                            return {'amount': amount, 'tx_hash': tx_hash, 'source': in_msg.get('source')}
                            
                            elif 'events' in data:
                                for event in data.get('events', []):
                                    for action in event.get('actions', []):
                                        if action.get('type') == 'TonTransfer':
                                            transfer = action.get('TonTransfer', {})
                                            comment = transfer.get('comment', '')
                                            if comment == memo:
                                                amount = int(transfer.get('amount', 0)) / 1e9
                                                return {'amount': amount, 'tx_hash': event.get('event_id'), 'source': transfer.get('sender', {}).get('address')}
            except Exception as e:
                logger.error(f"API error {api_url}: {e}")
                continue
        await asyncio.sleep(2)
    return None

async def check_deposits_loop(bot):
    processed = set()
    
    while True:
        try:
            c.execute("SELECT id, user_id, crypto, memo, amount FROM deposits WHERE status = 'pending' AND crypto = 'ton'")
            pending = c.fetchall()
            
            for dep in pending:
                deposit_id, user_id, crypto, memo, required_amount = dep
                
                if memo in processed:
                    continue
                
                tx = await check_ton_tx(memo, required_amount)
                
                if tx and tx['amount'] >= required_amount:
                    processed.add(memo)
                    
                    rate = get_exchange_rate('ton')
                    gross = tx['amount'] * rate
                    fee = gross * get_commission() / 100
                    rub = gross - fee
                    
                    update_balance(user_id, rub)
                    complete_deposit(deposit_id, tx['amount'], rub, user_id, tx['tx_hash'])
                    
                    await bot.send_message(
                        user_id,
                        f"✅ *Обмен TON выполнен!*\n\n"
                        f"💰 Зачислено: {rub:.2f} ₽\n"
                        f"📊 Курс: {rate:.2f} ₽\n"
                        f"💸 Комиссия: {fee:.2f} ₽",
                        parse_mode="Markdown"
                    )
                    
                    for admin_id in ADMIN_IDS:
                        await bot.send_message(
                            admin_id,
                            f"✅ *Новый обмен TON*\n\n"
                            f"👤 Пользователь: `{user_id}`\n"
                            f"💰 Сумма: {tx['amount']:.4f} TON\n"
                            f"💵 Выплачено: {rub:.2f} ₽",
                            parse_mode="Markdown"
                        )
                    
                    logger.info(f"Депозит #{deposit_id}: пользователь {user_id}, сумма {tx['amount']} TON -> {rub} RUB")
            
            if int(time.time()) % 3600 < 60:
                processed.clear()
                
        except Exception as e:
            logger.error(f"Check deposits error: {e}")
        
        await asyncio.sleep(30)

user_usdt_requests = defaultdict(list)

def can_make_usdt_request(user_id):
    now = datetime.now()
    user_usdt_requests[user_id] = [t for t in user_usdt_requests[user_id] if now - t < timedelta(minutes=10)]
    if len(user_usdt_requests[user_id]) >= 1:
        return False
    user_usdt_requests[user_id].append(now)
    return True

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Баланс", callback_data="balance")],
        [InlineKeyboardButton(text="🔄 Обмен", callback_data="exchange")],
        [InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw")],
        [InlineKeyboardButton(text="👥 Рефералы", callback_data="referrals")],
        [InlineKeyboardButton(text="📜 История", callback_data="history")],
        [InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_LINK)]
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

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Заявки на вывод", callback_data="admin_requests")],
        [InlineKeyboardButton(text="🔧 Управление курсами", callback_data="admin_rates")],
        [InlineKeyboardButton(text="💰 Управление кошельками", callback_data="admin_wallets")],
        [InlineKeyboardButton(text="⚙️ Комиссия сервиса", callback_data="admin_commission")],
        [InlineKeyboardButton(text="🎁 Реферальный процент", callback_data="admin_referral")],
        [InlineKeyboardButton(text="💵 Лимиты вывода", callback_data="admin_limits")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👤 Изменить баланс", callback_data="admin_balance")],
        [InlineKeyboardButton(text="🚫 Блокировка", callback_data="admin_ban")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_mailing")],
        [InlineKeyboardButton(text="◀️ Выход", callback_data="admin_exit")]
    ])

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

class AdminWallet(StatesGroup):
    crypto = State()
    address = State()

class AdminMailing(StatesGroup):
    text = State()

class AdminCommission(StatesGroup):
    percent = State()

class AdminReferral(StatesGroup):
    percent = State()

class AdminBan(StatesGroup):
    uid = State()

class AdminReject(StatesGroup):
    comment = State()

class AdminLimits(StatesGroup):
    min_limit = State()
    max_limit = State()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

async def safe_send_message(target, text, parse_mode="Markdown", reply_markup=None):
    """Безопасная отправка сообщения с обработкой ошибок Markdown"""
    try:
        if hasattr(target, 'message'):
            await target.message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
        else:
            await target.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        # Если ошибка в Markdown, отправляем без форматирования
        try:
            if hasattr(target, 'message'):
                await target.message.answer(text, reply_markup=reply_markup)
            else:
                await target.answer(text, reply_markup=reply_markup)
        except:
            pass

async def edit_or_send_new(callback: types.CallbackQuery, text, markup=None):
    """Редактирует сообщение или отправляет новое если не получается"""
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        try:
            await callback.message.delete()
        except:
            pass
        try:
            await callback.message.answer(text, parse_mode="Markdown", reply_markup=markup)
        except:
            await callback.message.answer(text, reply_markup=markup)

async def welcome(target, user_id, username, ref_by=None):
    user = get_user(user_id)
    if user and user[6]:
        await target.answer("⛔ Ваш аккаунт заблокирован.")
        return
    create_user(user_id, username, ref_by)
    
    commission = get_commission()
    ref_percent = get_referral_percent()
    min_wd = get_min_withdrawal()
    max_wd = get_max_withdrawal()
    
    text = (
        "✨ *Добро пожаловать в CryptoExchangeBot* ✨\n\n"
        "Этот бот поможет вам обменять криптовалюту на рубли по выгодному курсу.\n\n"
        "📌 *Как это работает*\n"
        "1️⃣ Выберите криптовалюту (TON, USDT TON или USDT TRC20)\n"
        "2️⃣ Введите сумму, которую хотите обменять (мин. 1 TON или 1 USDT)\n"
        "3️⃣ Отправьте криптовалюту на указанный кошелёк с обязательным комментарием (memo)\n"
        "4️⃣ После зачисления рубли поступят на ваш баланс автоматически\n"
        "5️⃣ Вы можете вывести рубли на карту или счёт\n\n"
        f"💸 *Комиссия сервиса*: {commission}%\n"
        f"🎁 *Реферальная программа*: {ref_percent}% от суммы обменов ваших рефералов\n\n"
        f"💰 *Минимальная сумма вывода*: {min_wd:.0f} ₽\n"
        f"💰 *Максимальная сумма вывода*: {max_wd:.0f} ₽\n"
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
            if isinstance(target, types.Message):
                await target.answer(text, parse_mode="Markdown", reply_markup=main_kb())
            else:
                await target.message.answer(text, parse_mode="Markdown", reply_markup=main_kb())
    else:
        if isinstance(target, types.Message):
            await target.answer(text, parse_mode="Markdown", reply_markup=main_kb())
        else:
            await target.message.answer(text, parse_mode="Markdown", reply_markup=main_kb())

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if user and user[6]:
        await message.answer("⛔ Ваш аккаунт заблокирован.")
        return
    await state.clear()
    args = message.text.split()
    ref = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    await welcome(message, message.from_user.id, message.from_user.username, ref)

@dp.callback_query(F.data == "back")
async def back_cb(callback: types.CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user and user[6]:
        await callback.answer("⛔ Ваш аккаунт заблокирован.", show_alert=True)
        return
    await state.clear()
    await welcome(callback, callback.from_user.id, callback.from_user.username)
    await callback.answer()

@dp.callback_query(F.data == "balance")
async def balance_cb(callback: types.CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user and user[6]:
        await callback.answer("⛔ Ваш аккаунт заблокирован.", show_alert=True)
        return
    await state.clear()
    bal = get_balance(callback.from_user.id)
    await edit_or_send_new(callback, f"💰 *Ваш баланс*\n\n{bal:.2f} ₽\n\nЗдесь отображаются рубли, полученные за обмен криптовалюты.", back_kb())
    await callback.answer()

@dp.callback_query(F.data == "exchange")
async def exchange_menu(callback: types.CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user and user[6]:
        await callback.answer("⛔ Ваш аккаунт заблокирован.", show_alert=True)
        return
    await state.clear()
    await edit_or_send_new(callback, 
        "🔄 *Обмен криптовалюты на рубли*\n\n"
        "Выберите криптовалюту, которую хотите обменять\n\n"
        "💎 *TON* - нативный токен сети TON (мин. 1 TON)\n"
        "💰 *USDT (TON)* - стейблкоин в сети TON (мин. 1 USDT)\n"
        "💵 *USDT (TRC20)* - стейблкоин в сети TRON (мин. 1 USDT)",
        exchange_kb())
    await callback.answer()

@dp.callback_query(F.data.startswith("exch_"))
async def exch_select(callback: types.CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user and user[6]:
        await callback.answer("⛔ Ваш аккаунт заблокирован.", show_alert=True)
        return
    
    crypto = callback.data[5:]
    
    if crypto not in WALLETS:
        await callback.answer(f"❌ Ошибка: валюта {crypto} не найдена", show_alert=True)
        return
    
    await state.update_data(crypto=crypto)
    rate = get_exchange_rate(crypto)
    commission = get_commission()
    await edit_or_send_new(callback,
        f"🔄 *Обмен {crypto.upper()} на рубли*\n\n"
        f"Текущий курс: 1 {crypto.upper()} = {rate:.2f} ₽\n"
        f"Комиссия сервиса: {commission}%\n"
        f"Минимальная сумма: {MIN_EXCHANGE} {crypto.upper()}\n\n"
        "Введите сумму, которую хотите обменять (в криптовалюте)",
        cancel_kb())
    await state.set_state(Exchange.amount)
    await callback.answer()

@dp.message(Exchange.amount)
async def exch_amount(message: types.Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if user and user[6]:
        await message.answer("⛔ Ваш аккаунт заблокирован.")
        return
    
    try:
        amount = float(message.text)
        if amount < MIN_EXCHANGE:
            await message.answer(f"❌ Минимальная сумма обмена: {MIN_EXCHANGE}", reply_markup=cancel_kb())
            return
    except:
        await message.answer("❌ Введите положительное число (например, 0.5).", reply_markup=cancel_kb())
        return
    
    data = await state.get_data()
    crypto = data['crypto']
    rate = get_exchange_rate(crypto)
    commission = get_commission()
    gross = amount * rate
    fee = gross * commission / 100
    net = gross - fee
    
    await state.update_data(amount=amount, net=net)
    await message.answer(
        f"🔄 *Обмен {crypto.upper()}*\n\n"
        f"Сумма: {amount:.4f} {crypto.upper()}\n"
        f"Курс: 1 {crypto.upper()} = {rate:.2f} ₽\n"
        f"Комиссия ({commission}%): -{fee:.2f} ₽\n"
        f"Вы получите: *{net:.2f} ₽*\n\n"
        "Подтверждаете обмен?",
        parse_mode="Markdown",
        reply_markup=confirm_kb())

@dp.callback_query(F.data == "confirm")
async def exch_confirm(callback: types.CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user and user[6]:
        await callback.answer("⛔ Ваш аккаунт заблокирован.", show_alert=True)
        return
    
    data = await state.get_data()
    if not data:
        await back_cb(callback, state)
        return
    
    crypto = data['crypto']
    amount = data['amount']
    net = data['net']
    user_id = callback.from_user.id
    memo = f"dep_{user_id}_{int(time.time())}"
    address = WALLETS[crypto]
    deposit_id = add_deposit(user_id, crypto, memo, amount)
    
    safe_address = clean_text(address)
    safe_memo = clean_text(memo)
    commission = get_commission()
    
    if crypto in ('usdt_ton', 'usdt_trc20'):
        text = (
            f"🔄 *Обмен {crypto.upper()}*\n\n"
            f"📤 *Отправьте на адрес:*\n"
            f"`{safe_address}`\n\n"
            f"📝 *Обязательный комментарий (memo):*\n"
            f"`{safe_memo}`\n\n"
            f"💰 *Вы получите:* {net:.2f} ₽ (после вычета комиссии {commission}%)\n\n"
            f"⚡️ *ВАЖНО!*\n"
            f"• Переводите ТОЛЬКО {crypto.upper()} на указанный адрес\n"
            f"• Обязательно укажите комментарий (memo) ТОЧНО как написано выше\n"
            f"• Без правильного комментария средства не будут зачислены!\n\n"
            f"✅ *После отправки нажмите кнопку «Проверить обмен»*\n\n"
            f"👨‍💼 Администратор проверит транзакцию и зачислит средства на баланс."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Проверить обмен", callback_data=f"check_usdt_{deposit_id}")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="back")]
        ])
    else:
        text = (
            f"🔄 *Обмен {crypto.upper()}*\n\n"
            f"📤 *Отправьте на адрес:*\n"
            f"`{safe_address}`\n\n"
            f"📝 *Обязательный комментарий (memo):*\n"
            f"`{safe_memo}`\n\n"
            f"💰 *Вы получите:* {net:.2f} ₽ (после вычета комиссии {commission}%)\n\n"
            f"⚡️ *ВАЖНО!*\n"
            f"• Переводите ТОЛЬКО {crypto.upper()} на указанный адрес\n"
            f"• Обязательно укажите комментарий (memo) ТОЧНО как написано выше\n"
            f"• Без правильного комментария средства не будут зачислены!\n\n"
            f"🤖 *Автоматическое зачисление*\n"
            f"• После отправки рубли поступят на баланс автоматически\n"
            f"• Обычно это занимает 1-5 минут\n\n"
            f"⚠️ *Если прошло больше 10 минут - нажмите кнопку «Проверить»*"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Проверить", callback_data=f"check_ton_{deposit_id}")],
            [InlineKeyboardButton(text="◀️ В главное меню", callback_data="back")]
        ])
    
    # Удаляем старое сообщение и отправляем новое
    try:
        await callback.message.delete()
    except:
        pass
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data.startswith("check_ton_"))
async def check_ton_manual(callback: types.CallbackQuery):
    user = get_user(callback.from_user.id)
    if user and user[6]:
        await callback.answer("⛔ Ваш аккаунт заблокирован.", show_alert=True)
        return
    
    deposit_id = int(callback.data.split("_")[2])
    
    c.execute("SELECT id, user_id, crypto, memo, amount, status FROM deposits WHERE id = ?", (deposit_id,))
    dep = c.fetchone()
    
    if not dep:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return
    
    if dep[5] != 'pending':
        await callback.answer("❌ Заявка уже обработана", show_alert=True)
        return
    
    if not can_make_usdt_request(dep[1]):
        await callback.answer("⚠️ Подождите 10 минут перед повторной проверкой.", show_alert=True)
        return
    
    await callback.answer("🔍 Проверяем транзакцию...")
    
    tx = await check_ton_tx(dep[3], dep[4], retries=3)
    
    if tx and tx['amount'] >= dep[4]:
        rate = get_exchange_rate('ton')
        gross = tx['amount'] * rate
        fee = gross * get_commission() / 100
        rub = gross - fee
        
        update_balance(dep[1], rub)
        complete_deposit(deposit_id, tx['amount'], rub, dep[1], tx['tx_hash'])
        
        await bot.send_message(
            dep[1],
            f"✅ *Обмен TON подтверждён!*\n\n"
            f"💰 Зачислено: {rub:.2f} ₽\n"
            f"📊 Курс: {rate:.2f} ₽\n"
            f"💸 Комиссия: {fee:.2f} ₽",
            parse_mode="Markdown"
        )
        
        await edit_or_send_new(callback, "✅ Транзакция найдена! Средства зачислены.", back_kb())
    else:
        await edit_or_send_new(callback, "❌ Транзакция не найдена. Проверьте правильность memo и попробуйте позже.", 
                           InlineKeyboardMarkup(inline_keyboard=[
                               [InlineKeyboardButton(text="🔍 Проверить снова", callback_data=f"check_ton_{deposit_id}")],
                               [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
                           ]))
    await callback.answer()

@dp.callback_query(F.data.startswith("check_usdt_"))
async def check_usdt(callback: types.CallbackQuery):
    user = get_user(callback.from_user.id)
    if user and user[6]:
        await callback.answer("⛔ Ваш аккаунт заблокирован.", show_alert=True)
        return
    
    deposit_id = int(callback.data.split("_")[2])
    c.execute("SELECT id, user_id, crypto, memo, amount, status FROM deposits WHERE id = ?", (deposit_id,))
    dep = c.fetchone()
    
    if not dep:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return
    
    if dep[5] != 'pending':
        await callback.answer("❌ Заявка уже обработана", show_alert=True)
        return
    
    if not can_make_usdt_request(dep[1]):
        await callback.answer("⚠️ Вы уже отправляли запрос на проверку. Подождите 10 минут.", show_alert=True)
        return
    
    await callback.answer("🔄 Отправляем запрос администратору...")
    
    rate = get_exchange_rate(dep[2])
    commission = get_commission()
    gross = dep[4] * rate
    net = gross - gross * commission / 100
    
    admin_sent = False
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"📢 *Запрос на подтверждение USDT обмена*\n\n"
                f"👤 Пользователь: `{dep[1]}`\n"
                f"💎 Валюта: {dep[2].upper()}\n"
                f"📊 Сумма: {dep[4]:.4f} {dep[2].upper()}\n"
                f"📝 Memo: `{clean_text(dep[3])}`\n"
                f"💰 К получению: {net:.2f} ₽\n\n"
                f"💰 Кошелёк для проверки: `{clean_text(WALLETS[dep[2]])}`\n\n"
                f"Проверьте транзакцию и подтвердите.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_usdt_{deposit_id}"),
                     InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_usdt_{deposit_id}")]
                ])
            )
            admin_sent = True
        except Exception as e:
            logger.error(f"Error sending to admin {admin_id}: {e}")
    
    if admin_sent:
        text = f"🔄 *Обмен {dep[2].upper()}*\n\n✅ Запрос на проверку отправлен администратору.\nПожалуйста, ожидайте подтверждения.\n\nСтатус заявки: ожидает проверки."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ В главное меню", callback_data="back")]
        ])
        await edit_or_send_new(callback, text, kb)
        await callback.answer("✅ Запрос отправлен администратору. Ожидайте подтверждения.", show_alert=True)
    else:
        await edit_or_send_new(callback, "❌ Не удалось отправить запрос администраторам. Пожалуйста, свяжитесь с поддержкой.", back_kb())
        await callback.answer("❌ Ошибка отправки запроса", show_alert=True)

@dp.callback_query(F.data.startswith("confirm_usdt_"))
async def confirm_usdt(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    
    deposit_id = int(callback.data.split("_")[2])
    c.execute("SELECT id, user_id, crypto, memo, amount, status FROM deposits WHERE id = ?", (deposit_id,))
    dep = c.fetchone()
    
    if not dep or dep[5] != 'pending':
        await callback.answer("❌ Заявка не найдена или уже обработана", show_alert=True)
        return
    
    rate = get_exchange_rate(dep[2])
    commission = get_commission()
    gross = dep[4] * rate
    rub = gross - gross * commission / 100
    
    update_balance(dep[1], rub)
    complete_deposit(deposit_id, dep[4], rub, dep[1], None)
    
    await bot.send_message(
        dep[1],
        f"✅ *Обмен {dep[2].upper()} подтверждён!*\n\n"
        f"💰 Зачислено: {rub:.2f} ₽\n"
        f"📊 Курс: {rate:.2f} ₽\n"
        f"💸 Комиссия: {gross * commission / 100:.2f} ₽\n\n"
        f"💵 Теперь вы можете вывести средства на карту через меню «Вывести».",
        parse_mode="Markdown"
    )
    
    await edit_or_send_new(callback, f"✅ Заявка #{deposit_id} подтверждена, средства начислены пользователю.", admin_kb())
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_usdt_"))
async def reject_usdt(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    
    deposit_id = int(callback.data.split("_")[2])
    c.execute("SELECT id, user_id, crypto, memo, amount, status FROM deposits WHERE id = ?", (deposit_id,))
    dep = c.fetchone()
    
    if not dep or dep[5] != 'pending':
        await callback.answer("❌ Заявка не найдена или уже обработана", show_alert=True)
        return
    
    c.execute("UPDATE deposits SET status = 'rejected' WHERE id = ?", (deposit_id,))
    conn.commit()
    
    await bot.send_message(
        dep[1],
        f"❌ *Обмен {dep[2].upper()} отклонён администратором.*\n\n"
        f"Пожалуйста, свяжитесь с поддержкой: {SUPPORT_LINK}",
        parse_mode="Markdown"
    )
    
    await edit_or_send_new(callback, f"❌ Заявка #{deposit_id} отклонена.", admin_kb())
    await callback.answer()

# Остальные функции (withdraw, referrals, history, admin и т.д.) остаются без изменений
# ... (продолжение следует, но они такие же как в предыдущей версии)

app = Flask(__name__)

@app.route('/')
def index():
    return "Bot running!"

@app.route('/health')
def health():
    return "OK"

async def main():
    await asyncio.sleep(1)
    asyncio.create_task(update_rates_loop())
    asyncio.create_task(check_deposits_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    is_railway = bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN"))
    if is_railway:
        def run_bot():
            asyncio.run(main())
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
