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
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

def clean_text(text):
    return re.sub(r'[_*[\]()~`>#+\-=|{}.!]', lambda m: '\\' + m.group(0), text)

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

async def edit_or_send_new(callback: types.CallbackQuery, text, markup=None):
    """Редактирует сообщение или отправляет новое если не получается отредактировать"""
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        try:
            await callback.message.delete()
        except:
            pass
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=markup)

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
    
    # Отправляем новое сообщение с адресом и memo
    await callback.message.delete()
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

# Остальные функции (withdraw, referrals, history, admin) остаются без изменений
# Они такие же как в предыдущей версии, я их не менял

@dp.callback_query(F.data == "withdraw")
async def withdraw_menu(callback: types.CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user and user[6]:
        await callback.answer("⛔ Ваш аккаунт заблокирован.", show_alert=True)
        return
    
    bal = get_balance(callback.from_user.id)
    min_wd = get_min_withdrawal()
    max_wd = get_max_withdrawal()
    
    if bal < min_wd:
        await edit_or_send_new(callback, f"❌ *У вас нет средств для вывода*\n\nМинимальная сумма вывода: {min_wd:.0f} ₽", back_kb())
        return
    await edit_or_send_new(callback,
        f"💸 *Вывод рублей*\n\n"
        f"💰 Ваш баланс: *{bal:.2f} ₽*\n"
        f"📊 Минимальная сумма: {min_wd:.0f} ₽\n"
        f"📊 Максимальная сумма: {max_wd:.0f} ₽\n\n"
        "Введите сумму, которую хотите вывести (в рублях)",
        cancel_kb())
    await state.set_state(Withdraw.amount)
    await callback.answer()

@dp.message(Withdraw.amount)
async def withdraw_amount(message: types.Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if user and user[6]:
        await message.answer("⛔ Ваш аккаунт заблокирован.")
        return
    
    try:
        amount = float(message.text)
        min_wd = get_min_withdrawal()
        max_wd = get_max_withdrawal()
        
        if amount < min_wd:
            await message.answer(f"❌ Минимальная сумма вывода: {min_wd:.0f} ₽", reply_markup=cancel_kb())
            return
        if amount > max_wd:
            await message.answer(f"❌ Максимальная сумма вывода: {max_wd:.0f} ₽", reply_markup=cancel_kb())
            return
        bal = get_balance(message.from_user.id)
        if amount > bal:
            await message.answer(f"❌ Недостаточно средств. Ваш баланс: {bal:.2f} ₽", reply_markup=cancel_kb())
            return
    except:
        await message.answer("❌ Введите положительное число (например, 500).", reply_markup=cancel_kb())
        return
    
    await state.update_data(amount=amount)
    await message.answer("Введите реквизиты для выплаты (номер карты, счёта или телефона):", reply_markup=cancel_kb())
    await state.set_state(Withdraw.details)

@dp.message(Withdraw.details)
async def withdraw_details(message: types.Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if user and user[6]:
        await message.answer("⛔ Ваш аккаунт заблокирован.")
        return
    
    details = message.text.strip()
    if len(details) < 5:
        await message.answer("❌ Введите корректные реквизиты (минимум 5 символов)", reply_markup=cancel_kb())
        return
    
    data = await state.get_data()
    amount = data['amount']
    update_balance(message.from_user.id, -amount)
    wid = add_withdrawal(message.from_user.id, amount, details)
    await message.answer(
        f"✅ *Заявка на вывод создана*\n\n"
        f"📋 Номер заявки: #{wid}\n"
        f"💰 Сумма: {amount:.2f} ₽\n\n"
        "⏱ Ожидайте подтверждения администратора.\n"
        "Вы получите уведомление, когда заявка будет обработана.",
        parse_mode="Markdown",
        reply_markup=back_kb()
    )
    await state.clear()

@dp.callback_query(F.data == "referrals")
async def referrals_cb(callback: types.CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user and user[6]:
        await callback.answer("⛔ Ваш аккаунт заблокирован.", show_alert=True)
        return
    
    me = await bot.get_me()
    bot_username = me.username if me.username else "CryptoExchanges_robot"
    link = f"https://t.me/{bot_username}?start={callback.from_user.id}"
    c.execute("SELECT COUNT(*) FROM users WHERE ref_by = ?", (callback.from_user.id,))
    invited = c.fetchone()[0]
    c.execute("SELECT ref_bonus FROM users WHERE user_id = ?", (callback.from_user.id,))
    bonus = c.fetchone()[0] or 0
    ref_percent = get_referral_percent()
    await edit_or_send_new(callback,
        f"👥 *Реферальная программа*\n\n"
        f"🔗 Ваша реферальная ссылка:\n`{clean_text(link)}`\n\n"
        f"👤 Приглашено: *{invited}* чел\n"
        f"🎁 Заработано бонусов: *{bonus:.2f} ₽*\n\n"
        f"💡 Вы получаете *{ref_percent}%* от суммы обменов ваших рефералов (после вычета комиссии)\n"
        "Бонусы начисляются автоматически и доступны для вывода.",
        back_kb())
    await callback.answer()

@dp.callback_query(F.data == "history")
async def history_cb(callback: types.CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user and user[6]:
        await callback.answer("⛔ Ваш аккаунт заблокирован.", show_alert=True)
        return
    
    c.execute("SELECT crypto, amount, status, created_at FROM deposits WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (callback.from_user.id,))
    dep = c.fetchall()
    c.execute("SELECT amount, status, created_at FROM withdrawals WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (callback.from_user.id,))
    wd = c.fetchall()
    
    text = "📜 *История операций*\n\n"
    if dep:
        text += "*🔄 Обмены (пополнения)*\n"
        for d in dep:
            date = time.strftime("%d.%m.%Y %H:%M", time.localtime(d[3]))
            text += f"• {d[0].upper()} {d[1]:.4f} - {d[2]} ({date})\n"
    if wd:
        text += "\n*💸 Выводы*\n"
        for w in wd:
            date = time.strftime("%d.%m.%Y %H:%M", time.localtime(w[2]))
            text += f"• {w[0]:.2f} ₽ - {w[1]} ({date})\n"
    if not dep and not wd:
        text += "📭 Операций пока нет."
    await edit_or_send_new(callback, text, back_kb())
    await callback.answer()

@dp.message(Command("admin"))
async def admin_cmd(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("🛡 *Панель администратора*", parse_mode="Markdown", reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery):
    await edit_or_send_new(callback, "🛡 *Панель администратора*", admin_kb())
    await callback.answer()

@dp.callback_query(F.data == "admin_exit")
async def admin_exit(callback: types.CallbackQuery):
    await back_cb(callback, None)

@dp.callback_query(F.data == "admin_requests")
async def admin_requests(callback: types.CallbackQuery):
    c.execute("SELECT id, user_id, amount, details, status FROM withdrawals WHERE status = 'pending' ORDER BY created_at DESC")
    rows = c.fetchall()
    if not rows:
        await edit_or_send_new(callback, "📭 Нет новых заявок", admin_kb())
        return
    await edit_or_send_new(callback, f"📋 Найдено заявок: {len(rows)}", admin_kb())
    for w in rows:
        await callback.message.answer(
            f"📋 *Заявка #{w[0]}*\n"
            f"👤 Пользователь: `{w[1]}`\n"
            f"💰 Сумма: {w[2]:.2f} ₽\n"
            f"📝 Реквизиты: `{clean_text(w[3])}`\n"
            f"🏷 Статус: {w[4]}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_{w[0]}"),
                 InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{w[0]}")]
            ])
        )
    await callback.answer()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_req(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    wid = int(callback.data.split("_")[1])
    w = get_withdrawal(wid)
    if w:
        update_withdrawal_status(wid, 'completed')
        await bot.send_message(w[1], f"✅ *Вывод #{wid} выполнен*", parse_mode="Markdown")
        await callback.message.edit_text(f"✅ Заявка #{wid} подтверждена")
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_"))
async def reject_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    wid = int(callback.data.split("_")[1])
    await state.update_data(wid=wid)
    await edit_or_send_new(callback, "❌ *Отклонение заявки*\n\nВведите причину отклонения:", admin_kb())
    await state.set_state(AdminReject.comment)
    await callback.answer()

@dp.message(AdminReject.comment)
async def reject_comment(message: types.Message, state: FSMContext):
    data = await state.get_data()
    wid = data['wid']
    w = get_withdrawal(wid)
    if w:
        update_withdrawal_status(wid, 'rejected', message.text)
        update_balance(w[1], w[2])
        await bot.send_message(w[1], f"❌ *Вывод #{wid} отклонён*\nПричина: {clean_text(message.text)}", parse_mode="Markdown")
        await message.answer(f"✅ Заявка #{wid} отклонена, средства возвращены.", reply_markup=admin_kb())
    else:
        await message.answer("❌ Заявка не найдена.", reply_markup=admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    users, deposits, withdrawals = get_statistics()
    c.execute("SELECT SUM(amount) FROM referral_earnings")
    ref_bonus = c.fetchone()[0] or 0
    c.execute("SELECT COUNT(*) FROM users WHERE ref_by > 0")
    referred = c.fetchone()[0]
    await edit_or_send_new(callback,
        f"📊 *Статистика*\n\n"
        f"👥 Пользователей: {users}\n"
        f"👥 Приглашённых: {referred}\n"
        f"💰 Депозитов: {deposits:.2f} ₽\n"
        f"💸 Выводов: {withdrawals:.2f} ₽\n"
        f"💵 В обороте: {deposits - withdrawals:.2f} ₽\n"
        f"🎁 Реферальных бонусов: {ref_bonus:.2f} ₽",
        admin_kb())
    await callback.answer()

@dp.callback_query(F.data == "admin_limits")
async def admin_limits_menu(callback: types.CallbackQuery, state: FSMContext):
    min_wd = get_min_withdrawal()
    max_wd = get_max_withdrawal()
    await edit_or_send_new(callback,
        f"💵 *Лимиты вывода*\n\n"
        f"📊 Минимальная сумма: {min_wd:.0f} ₽\n"
        f"📊 Максимальная сумма: {max_wd:.0f} ₽\n\n"
        f"Введите новый минимальный лимит:",
        admin_kb())
    await state.set_state(AdminLimits.min_limit)
    await callback.answer()

@dp.message(AdminLimits.min_limit)
async def admin_limits_min(message: types.Message, state: FSMContext):
    try:
        min_val = float(message.text)
        if min_val <= 0:
            raise ValueError
        await state.update_data(min_limit=min_val)
        await message.answer(f"✅ Мин. лимит: {min_val:.0f} ₽\n\nВведите максимальный лимит:", reply_markup=admin_kb())
        await state.set_state(AdminLimits.max_limit)
    except:
        await message.answer("❌ Введите положительное число", reply_markup=admin_kb())
        await state.clear()

@dp.message(AdminLimits.max_limit)
async def admin_limits_max(message: types.Message, state: FSMContext):
    try:
        max_val = float(message.text)
        data = await state.get_data()
        min_val = data.get('min_limit', 1000)
        
        if max_val <= min_val:
            await message.answer(f"❌ Макс. лимит должен быть больше {min_val:.0f}", reply_markup=admin_kb())
            await state.clear()
            return
        
        set_min_withdrawal(min_val)
        set_max_withdrawal(max_val)
        await message.answer(f"✅ Лимиты обновлены!\nМин: {min_val:.0f} ₽\nМакс: {max_val:.0f} ₽", reply_markup=admin_kb())
        await state.clear()
    except:
        await message.answer("❌ Введите число", reply_markup=admin_kb())
        await state.clear()

@dp.callback_query(F.data == "admin_rates")
async def admin_rates_menu(callback: types.CallbackQuery):
    await edit_or_send_new(callback, "🔧 *Управление курсами*\n\nВыберите валюту:", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="rate_ton")],
        [InlineKeyboardButton(text="💵 USDT", callback_data="rate_usdt")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ]))
    await callback.answer()

@dp.callback_query(F.data.startswith("rate_"))
async def admin_rate_select(callback: types.CallbackQuery, state: FSMContext):
    crypto = callback.data.split("_")[1]
    if crypto == 'usdt':
        crypto = 'usdt'
    await state.update_data(crypto=crypto)
    current = get_exchange_rate(crypto)
    await edit_or_send_new(callback, f"🔧 *Изменение курса {crypto.upper()}*\n\nТекущий курс: {current:.2f} ₽\nВведите новый курс:", admin_kb())
    await state.set_state(AdminRate.rate)
    await callback.answer()

@dp.message(AdminRate.rate)
async def admin_rate_set(message: types.Message, state: FSMContext):
    try:
        rate = float(message.text)
        if rate <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число")
        return
    data = await state.get_data()
    set_exchange_rate(data['crypto'], rate)
    await message.answer(f"✅ Курс {data['crypto'].upper()} = {rate:.2f} ₽", reply_markup=admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_wallets")
async def admin_wallets_menu(callback: types.CallbackQuery):
    text = "💰 *Управление кошельками*\n\n"
    for crypto, wallet in WALLETS.items():
        name = crypto.upper().replace('_', ' ')
        text += f"• {name}: `{wallet[:20]}...`\n"
    await edit_or_send_new(callback, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="wallet_ton")],
        [InlineKeyboardButton(text="💰 USDT TON", callback_data="wallet_usdt_ton")],
        [InlineKeyboardButton(text="💵 USDT TRC20", callback_data="wallet_usdt_trc20")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ]))
    await callback.answer()

@dp.callback_query(F.data.startswith("wallet_"))
async def admin_wallet_select(callback: types.CallbackQuery, state: FSMContext):
    if callback.data == "wallet_ton":
        crypto = "ton"
    elif callback.data == "wallet_usdt_ton":
        crypto = "usdt_ton"
    elif callback.data == "wallet_usdt_trc20":
        crypto = "usdt_trc20"
    else:
        await callback.answer("❌ Ошибка", show_alert=True)
        return
    
    await state.update_data(crypto=crypto)
    current = WALLETS[crypto]
    name = crypto.upper().replace('_', ' ')
    await edit_or_send_new(callback, f"🔧 *Изменение кошелька {name}*\n\nТекущий адрес:\n`{clean_text(current)}`\n\nВведите новый адрес:", admin_kb())
    await state.set_state(AdminWallet.address)
    await callback.answer()

@dp.message(AdminWallet.address)
async def admin_wallet_set(message: types.Message, state: FSMContext):
    address = message.text.strip()
    if len(address) < 20:
        await message.answer("❌ Адрес слишком короткий")
        return
    if not re.match(r'^[A-Za-z0-9_-]+$', address):
        await message.answer("❌ Адрес содержит недопустимые символы")
        return
    
    data = await state.get_data()
    WALLETS[data['crypto']] = address
    name = data['crypto'].upper().replace('_', ' ')
    await message.answer(f"✅ Кошелёк {name} обновлён", reply_markup=admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_commission")
async def admin_commission_menu(callback: types.CallbackQuery, state: FSMContext):
    current = get_commission()
    await edit_or_send_new(callback, f"⚙️ *Комиссия сервиса*\n\nТекущая комиссия: {current}%\nВведите новое значение (0-100):", admin_kb())
    await state.set_state(AdminCommission.percent)
    await callback.answer()

@dp.message(AdminCommission.percent)
async def admin_commission_set(message: types.Message, state: FSMContext):
    try:
        p = float(message.text)
        if 0 <= p <= 100:
            set_commission(p)
            await message.answer(f"✅ Комиссия установлена: {p}%", reply_markup=admin_kb())
        else:
            await message.answer("❌ Введите число от 0 до 100")
    except:
        await message.answer("❌ Введите число")
    await state.clear()

@dp.callback_query(F.data == "admin_referral")
async def admin_referral_menu(callback: types.CallbackQuery, state: FSMContext):
    current = get_referral_percent()
    await edit_or_send_new(callback, f"🎁 *Реферальный процент*\n\nТекущий процент: {current}%\nВведите новое значение (0-50):", admin_kb())
    await state.set_state(AdminReferral.percent)
    await callback.answer()

@dp.message(AdminReferral.percent)
async def admin_referral_set(message: types.Message, state: FSMContext):
    try:
        p = float(message.text)
        if 0 <= p <= 50:
            set_referral_percent(p)
            await message.answer(f"✅ Реферальный процент установлен: {p}%", reply_markup=admin_kb())
        else:
            await message.answer("❌ Введите число от 0 до 50")
    except:
        await message.answer("❌ Введите число")
    await state.clear()

@dp.callback_query(F.data == "admin_balance")
async def admin_balance_start(callback: types.CallbackQuery, state: FSMContext):
    await edit_or_send_new(callback, "👤 *Ручное изменение баланса*\n\nВведите Telegram ID пользователя:", admin_kb())
    await state.set_state(AdminBalance.uid)
    await callback.answer()

@dp.message(AdminBalance.uid)
async def admin_balance_uid(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text)
    except:
        await message.answer("❌ ID должен быть числом")
        return
    user = get_user(uid)
    if not user:
        await message.answer(f"❌ Пользователь с ID {uid} не найден")
        return
    await state.update_data(uid=uid, username=user[2])
    await message.answer(f"👤 Пользователь: {user[2]} (ID: {uid})\n💰 Текущий баланс: {user[3]:.2f} ₽\n\nВведите сумму изменения (+ или -):")
    await state.set_state(AdminBalance.amount)

@dp.message(AdminBalance.amount)
async def admin_balance_amount(message: types.Message, state: FSMContext):
    try:
        amt = float(message.text)
    except:
        await message.answer("❌ Введите число")
        return
    data = await state.get_data()
    update_balance(data['uid'], amt)
    new_bal = get_balance(data['uid'])
    await bot.send_message(data['uid'], f"👤 *Изменение баланса*\n\n💰 Сумма: {amt:+.2f} ₽\n💵 Новый баланс: {new_bal:.2f} ₽\n\n👨‍💻 Администратор изменил ваш баланс.", parse_mode="Markdown")
    await message.answer(f"✅ Баланс пользователя {data['username']} изменён на {amt:+.2f} ₽\n💰 Новый баланс: {new_bal:.2f} ₽", reply_markup=admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_ban")
async def admin_ban_start(callback: types.CallbackQuery, state: FSMContext):
    await edit_or_send_new(callback, "🚫 *Блокировка пользователя*\n\nВведите Telegram ID пользователя:", admin_kb())
    await state.set_state(AdminBan.uid)
    await callback.answer()

@dp.message(AdminBan.uid)
async def admin_ban_uid(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text)
    except:
        await message.answer("❌ ID должен быть числом")
        return
    user = get_user(uid)
    if not user:
        await message.answer(f"❌ Пользователь с ID {uid} не найден")
        return
    c.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (uid,))
    conn.commit()
    await bot.send_message(uid, f"🚫 *Ваш аккаунт заблокирован*\n\nСвяжитесь с администратором для выяснения причин.\n👉 {SUPPORT_LINK}", parse_mode="Markdown")
    await message.answer(f"✅ Пользователь {user[2]} (ID: {uid}) заблокирован", reply_markup=admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_mailing")
async def admin_mailing(callback: types.CallbackQuery, state: FSMContext):
    await edit_or_send_new(callback, "📢 *Рассылка*\n\nВведите текст сообщения:", admin_kb())
    await state.set_state(AdminMailing.text)
    await callback.answer()

@dp.message(AdminMailing.text)
async def mailing_send(message: types.Message, state: FSMContext):
    text = message.text
    users = get_all_users()
    success = 0
    
    semaphore = asyncio.Semaphore(5)
    
    async def send_to_user(uid):
        nonlocal success
        async with semaphore:
            try:
                await bot.send_message(uid, text, parse_mode="Markdown")
                success += 1
            except:
                pass
    
    tasks = [send_to_user(uid) for uid in users]
    await asyncio.gather(*tasks)
    
    await message.answer(f"✅ Рассылка завершена!\nОтправлено: {success}/{len(users)}", reply_markup=admin_kb())
    await state.clear()

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
