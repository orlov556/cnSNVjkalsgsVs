import os
import asyncio
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
import aiosqlite
from contextlib import asynccontextmanager

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

db_path = "exchange.db"

@asynccontextmanager
async def get_db():
    async with aiosqlite.connect(db_path) as conn:
        yield conn

async def init_db():
    async with get_db() as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            username TEXT,
            balance REAL DEFAULT 0,
            ref_by INTEGER DEFAULT 0,
            ref_bonus REAL DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            created_at INTEGER)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            crypto TEXT,
            memo TEXT,
            amount REAL,
            status TEXT,
            tx_hash TEXT,
            created_at INTEGER,
            completed_at INTEGER)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            details TEXT,
            status TEXT,
            admin_comment TEXT,
            created_at INTEGER,
            processed_at INTEGER)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS exchange_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            crypto TEXT UNIQUE,
            rate_rub REAL)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS referral_earnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            from_user_id INTEGER,
            amount REAL,
            created_at INTEGER)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT)''')
        await conn.execute("INSERT OR IGNORE INTO exchange_rates (crypto, rate_rub) VALUES (?, ?)", ('ton', 100))
        await conn.execute("INSERT OR IGNORE INTO exchange_rates (crypto, rate_rub) VALUES (?, ?)", ('usdt', 85))
        await conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('commission', '7'))
        await conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('referral_percent', '1'))
        await conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('min_withdrawal', '1000'))
        await conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('max_withdrawal', '100000'))
        await conn.commit()

async def get_user(user_id):
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def create_user(user_id, username, ref_by=None):
    async with get_db() as conn:
        if await get_user(user_id):
            return
        await conn.execute("INSERT INTO users (user_id, username, ref_by, created_at) VALUES (?, ?, ?, ?)",
                          (user_id, username, ref_by, int(time.time())))
        await conn.commit()

async def get_balance(user_id):
    async with get_db() as conn:
        async with conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def update_balance(user_id, amount):
    async with get_db() as conn:
        await conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await conn.commit()

async def add_deposit(user_id, crypto, memo, amount):
    async with get_db() as conn:
        cursor = await conn.execute(
            "INSERT INTO deposits (user_id, crypto, memo, amount, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, crypto, memo, amount, 'pending', int(time.time()))
        )
        await conn.commit()
        return cursor.lastrowid

async def complete_deposit(deposit_id, amount_crypto, rub_amount, user_id, tx_hash=None):
    async with get_db() as conn:
        await conn.execute(
            "UPDATE deposits SET amount = ?, status = 'completed', completed_at = ?, tx_hash = ? WHERE id = ?",
            (amount_crypto, int(time.time()), tx_hash, deposit_id)
        )
        ref_row = await conn.execute("SELECT ref_by FROM users WHERE user_id = ?", (user_id,))
        ref = await ref_row.fetchone()
        if ref and ref[0]:
            ref_pct_row = await conn.execute("SELECT value FROM settings WHERE key = 'referral_percent'")
            ref_pct = float((await ref_pct_row.fetchone())[0])
            bonus = rub_amount * ref_pct / 100
            if bonus > 0:
                await conn.execute("UPDATE users SET balance = balance + ?, ref_bonus = ref_bonus + ? WHERE user_id = ?",
                                   (bonus, bonus, ref[0]))
                await conn.execute("INSERT INTO referral_earnings (user_id, from_user_id, amount, created_at) VALUES (?, ?, ?, ?)",
                                   (ref[0], user_id, bonus, int(time.time())))
        await conn.commit()

async def add_withdrawal(user_id, amount, details):
    async with get_db() as conn:
        cursor = await conn.execute(
            "INSERT INTO withdrawals (user_id, amount, details, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, amount, details, 'pending', int(time.time()))
        )
        await conn.commit()
        return cursor.lastrowid

async def get_withdrawal(wid):
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM withdrawals WHERE id = ?", (wid,)) as cursor:
            return await cursor.fetchone()

async def update_withdrawal_status(wid, status, comment=None):
    async with get_db() as conn:
        if comment:
            await conn.execute("UPDATE withdrawals SET status = ?, admin_comment = ?, processed_at = ? WHERE id = ?",
                               (status, comment, int(time.time()), wid))
        else:
            await conn.execute("UPDATE withdrawals SET status = ?, processed_at = ? WHERE id = ?",
                               (status, int(time.time()), wid))
        await conn.commit()

async def get_exchange_rate(crypto):
    if crypto in ('usdt_ton', 'usdt_trc20'):
        crypto = 'usdt'
    async with get_db() as conn:
        async with conn.execute("SELECT rate_rub FROM exchange_rates WHERE crypto = ?", (crypto,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else (100 if crypto == 'ton' else 85)

async def set_exchange_rate(crypto, rate):
    if crypto in ('usdt_ton', 'usdt_trc20'):
        crypto = 'usdt'
    async with get_db() as conn:
        await conn.execute("UPDATE exchange_rates SET rate_rub = ? WHERE crypto = ?", (rate, crypto))
        await conn.commit()

async def get_commission():
    async with get_db() as conn:
        async with conn.execute("SELECT value FROM settings WHERE key = 'commission'") as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row else 7

async def set_commission(value):
    async with get_db() as conn:
        await conn.execute("UPDATE settings SET value = ? WHERE key = 'commission'", (str(value),))
        await conn.commit()

async def get_referral_percent():
    async with get_db() as conn:
        async with conn.execute("SELECT value FROM settings WHERE key = 'referral_percent'") as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row else 1

async def set_referral_percent(value):
    async with get_db() as conn:
        await conn.execute("UPDATE settings SET value = ? WHERE key = 'referral_percent'", (str(value),))
        await conn.commit()

async def get_min_withdrawal():
    async with get_db() as conn:
        async with conn.execute("SELECT value FROM settings WHERE key = 'min_withdrawal'") as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row else 1000

async def set_min_withdrawal(value):
    async with get_db() as conn:
        await conn.execute("UPDATE settings SET value = ? WHERE key = 'min_withdrawal'", (str(value),))
        await conn.commit()

async def get_max_withdrawal():
    async with get_db() as conn:
        async with conn.execute("SELECT value FROM settings WHERE key = 'max_withdrawal'") as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row else 100000

async def set_max_withdrawal(value):
    async with get_db() as conn:
        await conn.execute("UPDATE settings SET value = ? WHERE key = 'max_withdrawal'", (str(value),))
        await conn.commit()

async def get_statistics():
    async with get_db() as conn:
        users = await conn.execute("SELECT COUNT(*) FROM users WHERE is_banned = 0")
        users = (await users.fetchone())[0]
        deposits = await conn.execute("SELECT SUM(amount) FROM deposits WHERE status = 'completed'")
        deposits = (await deposits.fetchone())[0] or 0
        withdrawals = await conn.execute("SELECT SUM(amount) FROM withdrawals WHERE status = 'completed'")
        withdrawals = (await withdrawals.fetchone())[0] or 0
        return users, deposits, withdrawals

async def get_all_users():
    async with get_db() as conn:
        cursor = await conn.execute("SELECT user_id FROM users WHERE is_banned = 0")
        return [row[0] for row in await cursor.fetchall()]

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
                await set_exchange_rate('usdt', usdt)
                logger.info(f"USDT курс: {usdt:.2f} ₽")
            
            ton = await get_ton_rate()
            if ton:
                last_known_rates['ton'] = ton
                await set_exchange_rate('ton', ton)
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
            async with get_db() as conn:
                cursor = await conn.execute("SELECT id, user_id, crypto, memo, amount FROM deposits WHERE status = 'pending' AND crypto = 'ton'")
                pending = await cursor.fetchall()
            
            for dep in pending:
                deposit_id, user_id, crypto, memo, required_amount = dep
                
                if memo in processed:
                    continue
                
                tx = await check_ton_tx(memo, required_amount)
                
                if tx and tx['amount'] >= required_amount:
                    processed.add(memo)
                    
                    rate = await get_exchange_rate('ton')
                    gross = tx['amount'] * rate
                    fee = gross * await get_commission() / 100
                    rub = gross - fee
                    
                    await update_balance(user_id, rub)
                    await complete_deposit(deposit_id, tx['amount'], rub, user_id, tx['tx_hash'])
                    
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

async def check_ban_middleware(handler, event, data):
    user_id = None
    if isinstance(event, types.Message):
        user_id = event.from_user.id
    elif isinstance(event, types.CallbackQuery):
        user_id = event.from_user.id
    
    if user_id:
        user = await get_user(user_id)
        if user and user[6]:
            if isinstance(event, types.Message):
                await event.answer("⛔ Ваш аккаунт заблокирован. Обратитесь к администратору.")
            else:
                await event.answer("⛔ Ваш аккаунт заблокирован.", show_alert=True)
            return
    return await handler(event, data)

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
dp.callback_query.middleware(check_ban_middleware)
dp.message.middleware(check_ban_middleware)

async def welcome(target, user_id, username, ref_by=None):
    user = await get_user(user_id)
    if user and user[6]:
        await target.answer("⛔ Ваш аккаунт заблокирован.")
        return
    await create_user(user_id, username, ref_by)
    
    commission = await get_commission()
    ref_percent = await get_referral_percent()
    min_wd = await get_min_withdrawal()
    max_wd = await get_max_withdrawal()
    
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

async def edit_or_send(cb, text, markup=None):
    try:
        await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    except:
        await cb.message.answer(text, parse_mode="Markdown", reply_markup=markup)
    await cb.answer()

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
    try:
        await cb.message.delete()
    except:
        pass

@dp.callback_query(F.data == "balance")
async def balance_cb(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    bal = await get_balance(cb.from_user.id)
    await edit_or_send(cb, f"💰 *Ваш баланс*\n\n{bal:.2f} ₽\n\nЗдесь отображаются рубли, полученные за обмен криптовалюты.", back_kb())

@dp.callback_query(F.data == "exchange")
async def exchange_menu(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_or_send(cb, 
        "🔄 *Обмен криптовалюты на рубли*\n\n"
        "Выберите криптовалюту, которую хотите обменять\n\n"
        "💎 *TON* - нативный токен сети TON (мин. 1 TON)\n"
        "💰 *USDT (TON)* - стейблкоин в сети TON (мин. 1 USDT)\n"
        "💵 *USDT (TRC20)* - стейблкоин в сети TRON (мин. 1 USDT)",
        exchange_kb())

@dp.callback_query(F.data.startswith("exch_"))
async def exch_select(cb: types.CallbackQuery, state: FSMContext):
    crypto = cb.data[5:]
    
    if crypto not in WALLETS:
        await cb.answer(f"❌ Ошибка: валюта {crypto} не найдена", show_alert=True)
        return
    
    await state.update_data(crypto=crypto)
    rate = await get_exchange_rate(crypto)
    commission = await get_commission()
    await edit_or_send(cb,
        f"🔄 *Обмен {crypto.upper()} на рубли*\n\n"
        f"Текущий курс: 1 {crypto.upper()} = {rate:.2f} ₽\n"
        f"Комиссия сервиса: {commission}%\n"
        f"Минимальная сумма: {MIN_EXCHANGE} {crypto.upper()}\n\n"
        "Введите сумму, которую хотите обменять (в криптовалюте)",
        cancel_kb())
    await state.set_state(Exchange.amount)

@dp.message(Exchange.amount)
async def exch_amount(m: types.Message, state: FSMContext):
    try:
        amount = float(m.text)
        if amount < MIN_EXCHANGE:
            await m.answer(f"❌ Минимальная сумма обмена: {MIN_EXCHANGE}", reply_markup=cancel_kb())
            return
    except:
        await m.answer("❌ Введите положительное число (например, 0.5).", reply_markup=cancel_kb())
        return
    
    data = await state.get_data()
    crypto = data['crypto']
    rate = await get_exchange_rate(crypto)
    commission = await get_commission()
    gross = amount * rate
    fee = gross * commission / 100
    net = gross - fee
    
    await state.update_data(amount=amount, net=net)
    await m.answer(
        f"🔄 *Обмен {crypto.upper()}*\n\n"
        f"Сумма: {amount:.4f} {crypto.upper()}\n"
        f"Курс: 1 {crypto.upper()} = {rate:.2f} ₽\n"
        f"Комиссия ({commission}%): -{fee:.2f} ₽\n"
        f"Вы получите: *{net:.2f} ₽*\n\n"
        "Подтверждаете обмен?",
        parse_mode="Markdown",
        reply_markup=confirm_kb())

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
    deposit_id = await add_deposit(user_id, crypto, memo, amount)
    
    safe_address = escape_markdown(address)
    safe_memo = escape_markdown(memo)
    commission = await get_commission()
    
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
    
    try:
        await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except:
        await cb.message.answer(text, parse_mode="Markdown", reply_markup=kb)
    
    await state.clear()
    await cb.answer()

@dp.callback_query(F.data.startswith("check_ton_"))
async def check_ton_manual(cb: types.CallbackQuery):
    deposit_id = int(cb.data.split("_")[2])
    
    async with get_db() as conn:
        cursor = await conn.execute("SELECT id, user_id, crypto, memo, amount, status FROM deposits WHERE id = ?", (deposit_id,))
        dep = await cursor.fetchone()
    
    if not dep:
        await cb.answer("❌ Заявка не найдена", show_alert=True)
        return
    
    if dep[5] != 'pending':
        await cb.answer("❌ Заявка уже обработана", show_alert=True)
        return
    
    if not can_make_usdt_request(dep[1]):
        await cb.answer("⚠️ Подождите 10 минут перед повторной проверкой.", show_alert=True)
        return
    
    await cb.answer("🔍 Проверяем транзакцию...")
    
    tx = await check_ton_tx(dep[3], dep[4], retries=3)
    
    if tx and tx['amount'] >= dep[4]:
        rate = await get_exchange_rate('ton')
        gross = tx['amount'] * rate
        fee = gross * await get_commission() / 100
        rub = gross - fee
        
        await update_balance(dep[1], rub)
        await complete_deposit(deposit_id, tx['amount'], rub, dep[1], tx['tx_hash'])
        
        await bot.send_message(
            dep[1],
            f"✅ *Обмен TON подтверждён!*\n\n"
            f"💰 Зачислено: {rub:.2f} ₽\n"
            f"📊 Курс: {rate:.2f} ₽\n"
            f"💸 Комиссия: {fee:.2f} ₽",
            parse_mode="Markdown"
        )
        
        await cb.message.edit_text("✅ Транзакция найдена! Средства зачислены.")
    else:
        await cb.answer("❌ Транзакция не найдена. Проверьте правильность memo и попробуйте позже.", show_alert=True)

@dp.callback_query(F.data.startswith("check_usdt_"))
async def check_usdt(cb: types.CallbackQuery):
    deposit_id = int(cb.data.split("_")[2])
    async with get_db() as conn:
        cursor = await conn.execute("SELECT id, user_id, crypto, memo, amount, status FROM deposits WHERE id = ?", (deposit_id,))
        dep = await cursor.fetchone()
    if not dep:
        await cb.answer("❌ Заявка не найдена", show_alert=True)
        return
    if dep[5] != 'pending':
        await cb.answer("❌ Заявка уже обработана", show_alert=True)
        return
    if not can_make_usdt_request(dep[1]):
        await cb.answer("⚠️ Вы уже отправляли запрос на проверку. Подождите 10 минут.", show_alert=True)
        return
    
    rate = await get_exchange_rate(dep[2])
    commission = await get_commission()
    gross = dep[4] * rate
    net = gross - gross * commission / 100
    
    for admin_id in ADMIN_IDS:
        await bot.send_message(
            admin_id,
            f"📢 *Запрос на подтверждение USDT обмена*\n\n"
            f"👤 Пользователь: `{dep[1]}`\n"
            f"💎 Валюта: {dep[2].upper()}\n"
            f"📊 Сумма: {dep[4]:.4f} {dep[2].upper()}\n"
            f"📝 Memo: `{escape_markdown(dep[3])}`\n"
            f"💰 К получению: {net:.2f} ₽\n\n"
            f"💰 Кошелёк для проверки: `{escape_markdown(WALLETS[dep[2]])}`\n\n"
            f"Проверьте транзакцию и подтвердите.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_usdt_{deposit_id}"),
                 InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_usdt_{deposit_id}")]
            ])
        )
    
    try:
        await cb.message.edit_text(
            f"🔄 *Обмен {dep[2].upper()}*\n\n"
            f"✅ Запрос на проверку отправлен администратору.\n"
            f"Пожалуйста, ожидайте подтверждения.\n\n"
            f"Статус заявки: ожидает проверки.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ В главное меню", callback_data="back")]
            ])
        )
    except:
        pass
    
    await cb.answer("✅ Запрос отправлен администратору. Ожидайте подтверждения.", show_alert=True)

@dp.callback_query(F.data.startswith("confirm_usdt_"))
async def confirm_usdt(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    
    deposit_id = int(cb.data.split("_")[2])
    async with get_db() as conn:
        cursor = await conn.execute("SELECT id, user_id, crypto, memo, amount, status FROM deposits WHERE id = ?", (deposit_id,))
        dep = await cursor.fetchone()
    
    if not dep or dep[5] != 'pending':
        await cb.answer("❌ Заявка не найдена или уже обработана", show_alert=True)
        return
    
    rate = await get_exchange_rate(dep[2])
    commission = await get_commission()
    gross = dep[4] * rate
    rub = gross - gross * commission / 100
    await update_balance(dep[1], rub)
    await complete_deposit(deposit_id, dep[4], rub, dep[1], None)
    
    await bot.send_message(
        dep[1],
        f"✅ *Обмен {dep[2].upper()} подтверждён!*\n\n"
        f"💰 Зачислено: {rub:.2f} ₽\n"
        f"📊 Курс: {rate:.2f} ₽\n"
        f"💸 Комиссия: {gross * commission / 100:.2f} ₽\n\n"
        f"💵 Теперь вы можете вывести средства на карту через меню «Вывести».",
        parse_mode="Markdown"
    )
    
    await cb.message.edit_text(f"✅ Заявка #{deposit_id} подтверждена, средства начислены пользователю.")
    await cb.answer()

@dp.callback_query(F.data.startswith("reject_usdt_"))
async def reject_usdt(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    
    deposit_id = int(cb.data.split("_")[2])
    async with get_db() as conn:
        cursor = await conn.execute("SELECT id, user_id, crypto, memo, amount, status FROM deposits WHERE id = ?", (deposit_id,))
        dep = await cursor.fetchone()
    
    if not dep or dep[5] != 'pending':
        await cb.answer("❌ Заявка не найдена или уже обработана", show_alert=True)
        return
    
    await conn.execute("UPDATE deposits SET status = 'rejected' WHERE id = ?", (deposit_id,))
    await conn.commit()
    
    await bot.send_message(
        dep[1],
        f"❌ *Обмен {dep[2].upper()} отклонён администратором.*\n\n"
        f"Пожалуйста, свяжитесь с поддержкой: {SUPPORT_LINK}",
        parse_mode="Markdown"
    )
    
    await cb.message.edit_text(f"❌ Заявка #{deposit_id} отклонена.")
    await cb.answer()

@dp.callback_query(F.data == "withdraw")
async def withdraw_menu(cb: types.CallbackQuery, state: FSMContext):
    bal = await get_balance(cb.from_user.id)
    min_wd = await get_min_withdrawal()
    max_wd = await get_max_withdrawal()
    
    if bal < min_wd:
        await edit_or_send(cb, f"❌ *У вас нет средств для вывода*\n\nМинимальная сумма вывода: {min_wd:.0f} ₽", back_kb())
        return
    await edit_or_send(cb,
        f"💸 *Вывод рублей*\n\n"
        f"💰 Ваш баланс: *{bal:.2f} ₽*\n"
        f"📊 Минимальная сумма: {min_wd:.0f} ₽\n"
        f"📊 Максимальная сумма: {max_wd:.0f} ₽\n\n"
        "Введите сумму, которую хотите вывести (в рублях)",
        cancel_kb())
    await state.set_state(Withdraw.amount)

@dp.message(Withdraw.amount)
async def withdraw_amount(m: types.Message, state: FSMContext):
    try:
        amount = float(m.text)
        min_wd = await get_min_withdrawal()
        max_wd = await get_max_withdrawal()
        
        if amount < min_wd:
            await m.answer(f"❌ Минимальная сумма вывода: {min_wd:.0f} ₽", reply_markup=cancel_kb())
            return
        if amount > max_wd:
            await m.answer(f"❌ Максимальная сумма вывода: {max_wd:.0f} ₽", reply_markup=cancel_kb())
            return
        bal = await get_balance(m.from_user.id)
        if amount > bal:
            await m.answer(f"❌ Недостаточно средств. Ваш баланс: {bal:.2f} ₽", reply_markup=cancel_kb())
            return
    except:
        await m.answer("❌ Введите положительное число (например, 500).", reply_markup=cancel_kb())
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
    await update_balance(m.from_user.id, -amount)
    wid = await add_withdrawal(m.from_user.id, amount, details)
    await m.answer(
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
async def referrals_cb(cb: types.CallbackQuery, state: FSMContext):
    me = await bot.get_me()
    bot_username = me.username if me.username else "CryptoExchanges_robot"
    link = f"https://t.me/{bot_username}?start={cb.from_user.id}"
    async with get_db() as conn:
        invited = await conn.execute("SELECT COUNT(*) FROM users WHERE ref_by = ?", (cb.from_user.id,))
        invited = (await invited.fetchone())[0]
        bonus = await conn.execute("SELECT ref_bonus FROM users WHERE user_id = ?", (cb.from_user.id,))
        bonus = (await bonus.fetchone())[0] or 0
    ref_percent = await get_referral_percent()
    await edit_or_send(cb,
        f"👥 *Реферальная программа*\n\n"
        f"🔗 Ваша реферальная ссылка:\n`{escape_markdown(link)}`\n\n"
        f"👤 Приглашено: *{invited}* чел\n"
        f"🎁 Заработано бонусов: *{bonus:.2f} ₽*\n\n"
        f"💡 Вы получаете *{ref_percent}%* от суммы обменов ваших рефералов (после вычета комиссии)\n"
        "Бонусы начисляются автоматически и доступны для вывода.",
        back_kb())

@dp.callback_query(F.data == "history")
async def history_cb(cb: types.CallbackQuery, state: FSMContext):
    async with get_db() as conn:
        dep = await conn.execute("SELECT crypto, amount, status, created_at FROM deposits WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (cb.from_user.id,))
        dep = await dep.fetchall()
        wd = await conn.execute("SELECT amount, status, created_at FROM withdrawals WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (cb.from_user.id,))
        wd = await wd.fetchall()
    
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
    await edit_or_send(cb, text, back_kb())

@dp.message(Command("admin"))
async def admin_cmd(m: types.Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    await m.answer("🛡 *Панель администратора*", parse_mode="Markdown", reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_back")
async def admin_back(cb: types.CallbackQuery):
    await edit_or_send(cb, "🛡 *Панель администратора*", admin_kb())

@dp.callback_query(F.data == "admin_exit")
async def admin_exit(cb: types.CallbackQuery):
    await back_cb(cb, None)

@dp.callback_query(F.data == "admin_requests")
async def admin_requests(cb: types.CallbackQuery):
    async with get_db() as conn:
        rows = await conn.execute("SELECT id, user_id, amount, details, status FROM withdrawals WHERE status = 'pending' ORDER BY created_at DESC")
        rows = await rows.fetchall()
    if not rows:
        await edit_or_send(cb, "📭 Нет новых заявок", admin_kb())
        return
    for w in rows:
        await cb.message.answer(
            f"📋 *Заявка #{w[0]}*\n"
            f"👤 Пользователь: `{w[1]}`\n"
            f"💰 Сумма: {w[2]:.2f} ₽\n"
            f"📝 Реквизиты: `{escape_markdown(w[3])}`\n"
            f"🏷 Статус: {w[4]}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_{w[0]}"),
                 InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{w[0]}")]
            ])
        )
    await cb.answer()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_req(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    wid = int(cb.data.split("_")[1])
    w = await get_withdrawal(wid)
    if w:
        await update_withdrawal_status(wid, 'completed')
        await bot.send_message(w[1], f"✅ *Вывод #{wid} выполнен*", parse_mode="Markdown")
        await cb.message.edit_text(f"✅ Заявка #{wid} подтверждена")
    await cb.answer()

@dp.callback_query(F.data.startswith("reject_"))
async def reject_start(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    wid = int(cb.data.split("_")[1])
    await state.update_data(wid=wid)
    await edit_or_send(cb, "❌ *Отклонение заявки*\n\nВведите причину отклонения:", admin_kb())
    await state.set_state(AdminReject.comment)

@dp.message(AdminReject.comment)
async def reject_comment(m: types.Message, state: FSMContext):
    data = await state.get_data()
    wid = data['wid']
    w = await get_withdrawal(wid)
    if w:
        await update_withdrawal_status(wid, 'rejected', m.text)
        await update_balance(w[1], w[2])
        await bot.send_message(w[1], f"❌ *Вывод #{wid} отклонён*\nПричина: {escape_markdown(m.text)}", parse_mode="Markdown")
        await m.answer(f"✅ Заявка #{wid} отклонена, средства возвращены.", reply_markup=admin_kb())
    else:
        await m.answer("❌ Заявка не найдена.", reply_markup=admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cb: types.CallbackQuery):
    users, deposits, withdrawals = await get_statistics()
    async with get_db() as conn:
        ref_bonus = await conn.execute("SELECT SUM(amount) FROM referral_earnings")
        ref_bonus = (await ref_bonus.fetchone())[0] or 0
        referred = await conn.execute("SELECT COUNT(*) FROM users WHERE ref_by > 0")
        referred = (await referred.fetchone())[0]
    await edit_or_send(cb,
        f"📊 *Статистика*\n\n"
        f"👥 Пользователей: {users}\n"
        f"👥 Приглашённых: {referred}\n"
        f"💰 Депозитов: {deposits:.2f} ₽\n"
        f"💸 Выводов: {withdrawals:.2f} ₽\n"
        f"💵 В обороте: {deposits - withdrawals:.2f} ₽\n"
        f"🎁 Реферальных бонусов: {ref_bonus:.2f} ₽",
        admin_kb())

@dp.callback_query(F.data == "admin_limits")
async def admin_limits_menu(cb: types.CallbackQuery, state: FSMContext):
    min_wd = await get_min_withdrawal()
    max_wd = await get_max_withdrawal()
    await edit_or_send(cb,
        f"💵 *Лимиты вывода*\n\n"
        f"📊 Минимальная сумма: {min_wd:.0f} ₽\n"
        f"📊 Максимальная сумма: {max_wd:.0f} ₽\n\n"
        f"Введите новый минимальный лимит:",
        admin_kb())
    await state.set_state(AdminLimits.min_limit)

@dp.message(AdminLimits.min_limit)
async def admin_limits_min(m: types.Message, state: FSMContext):
    try:
        min_val = float(m.text)
        if min_val <= 0:
            raise ValueError
        await state.update_data(min_limit=min_val)
        await m.answer(f"✅ Мин. лимит: {min_val:.0f} ₽\n\nВведите максимальный лимит:", reply_markup=admin_kb())
        await state.set_state(AdminLimits.max_limit)
    except:
        await m.answer("❌ Введите положительное число", reply_markup=admin_kb())
        await state.clear()

@dp.message(AdminLimits.max_limit)
async def admin_limits_max(m: types.Message, state: FSMContext):
    try:
        max_val = float(m.text)
        data = await state.get_data()
        min_val = data.get('min_limit', 1000)
        
        if max_val <= min_val:
            await m.answer(f"❌ Макс. лимит должен быть больше {min_val:.0f}", reply_markup=admin_kb())
            await state.clear()
            return
        
        await set_min_withdrawal(min_val)
        await set_max_withdrawal(max_val)
        await m.answer(f"✅ Лимиты обновлены!\nМин: {min_val:.0f} ₽\nМакс: {max_val:.0f} ₽", reply_markup=admin_kb())
        await state.clear()
    except:
        await m.answer("❌ Введите число", reply_markup=admin_kb())
        await state.clear()

@dp.callback_query(F.data == "admin_rates")
async def admin_rates_menu(cb: types.CallbackQuery):
    await edit_or_send(cb, "🔧 *Управление курсами*\n\nВыберите валюту:", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="rate_ton")],
        [InlineKeyboardButton(text="💵 USDT", callback_data="rate_usdt")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ]))

@dp.callback_query(F.data.startswith("rate_"))
async def admin_rate_select(cb: types.CallbackQuery, state: FSMContext):
    crypto = cb.data.split("_")[1]
    if crypto == 'usdt':
        crypto = 'usdt'
    await state.update_data(crypto=crypto)
    current = await get_exchange_rate(crypto)
    await edit_or_send(cb, f"🔧 *Изменение курса {crypto.upper()}*\n\nТекущий курс: {current:.2f} ₽\nВведите новый курс:", admin_kb())
    await state.set_state(AdminRate.rate)

@dp.message(AdminRate.rate)
async def admin_rate_set(m: types.Message, state: FSMContext):
    try:
        rate = float(m.text)
        if rate <= 0:
            raise ValueError
    except:
        await m.answer("❌ Введите положительное число")
        return
    data = await state.get_data()
    await set_exchange_rate(data['crypto'], rate)
    await m.answer(f"✅ Курс {data['crypto'].upper()} = {rate:.2f} ₽", reply_markup=admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_wallets")
async def admin_wallets_menu(cb: types.CallbackQuery):
    text = "💰 *Управление кошельками*\n\n"
    for crypto, wallet in WALLETS.items():
        name = crypto.upper().replace('_', ' ')
        text += f"• {name}: `{wallet[:20]}...`\n"
    await edit_or_send(cb, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="wallet_ton")],
        [InlineKeyboardButton(text="💰 USDT TON", callback_data="wallet_usdt_ton")],
        [InlineKeyboardButton(text="💵 USDT TRC20", callback_data="wallet_usdt_trc20")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ]))

@dp.callback_query(F.data.startswith("wallet_"))
async def admin_wallet_select(cb: types.CallbackQuery, state: FSMContext):
    if cb.data == "wallet_ton":
        crypto = "ton"
    elif cb.data == "wallet_usdt_ton":
        crypto = "usdt_ton"
    elif cb.data == "wallet_usdt_trc20":
        crypto = "usdt_trc20"
    else:
        await cb.answer("❌ Ошибка", show_alert=True)
        return
    
    await state.update_data(crypto=crypto)
    current = WALLETS[crypto]
    name = crypto.upper().replace('_', ' ')
    await edit_or_send(cb, f"🔧 *Изменение кошелька {name}*\n\nТекущий адрес:\n`{escape_markdown(current)}`\n\nВведите новый адрес:", admin_kb())
    await state.set_state(AdminWallet.address)

@dp.message(AdminWallet.address)
async def admin_wallet_set(m: types.Message, state: FSMContext):
    address = m.text.strip()
    if len(address) < 20:
        await m.answer("❌ Адрес слишком короткий")
        return
    if not re.match(r'^[A-Za-z0-9_-]+$', address):
        await m.answer("❌ Адрес содержит недопустимые символы")
        return
    
    data = await state.get_data()
    WALLETS[data['crypto']] = address
    name = data['crypto'].upper().replace('_', ' ')
    await m.answer(f"✅ Кошелёк {name} обновлён", reply_markup=admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_commission")
async def admin_commission_menu(cb: types.CallbackQuery, state: FSMContext):
    current = await get_commission()
    await edit_or_send(cb, f"⚙️ *Комиссия сервиса*\n\nТекущая комиссия: {current}%\nВведите новое значение (0-100):", admin_kb())
    await state.set_state(AdminCommission.percent)

@dp.message(AdminCommission.percent)
async def admin_commission_set(m: types.Message, state: FSMContext):
    try:
        p = float(m.text)
        if 0 <= p <= 100:
            await set_commission(p)
            await m.answer(f"✅ Комиссия установлена: {p}%", reply_markup=admin_kb())
        else:
            await m.answer("❌ Введите число от 0 до 100")
    except:
        await m.answer("❌ Введите число")
    await state.clear()

@dp.callback_query(F.data == "admin_referral")
async def admin_referral_menu(cb: types.CallbackQuery, state: FSMContext):
    current = await get_referral_percent()
    await edit_or_send(cb, f"🎁 *Реферальный процент*\n\nТекущий процент: {current}%\nВведите новое значение (0-50):", admin_kb())
    await state.set_state(AdminReferral.percent)

@dp.message(AdminReferral.percent)
async def admin_referral_set(m: types.Message, state: FSMContext):
    try:
        p = float(m.text)
        if 0 <= p <= 50:
            await set_referral_percent(p)
            await m.answer(f"✅ Реферальный процент установлен: {p}%", reply_markup=admin_kb())
        else:
            await m.answer("❌ Введите число от 0 до 50")
    except:
        await m.answer("❌ Введите число")
    await state.clear()

@dp.callback_query(F.data == "admin_balance")
async def admin_balance_start(cb: types.CallbackQuery, state: FSMContext):
    await edit_or_send(cb, "👤 *Ручное изменение баланса*\n\nВведите Telegram ID пользователя:", admin_kb())
    await state.set_state(AdminBalance.uid)

@dp.message(AdminBalance.uid)
async def admin_balance_uid(m: types.Message, state: FSMContext):
    try:
        uid = int(m.text)
    except:
        await m.answer("❌ ID должен быть числом")
        return
    user = await get_user(uid)
    if not user:
        await m.answer(f"❌ Пользователь с ID {uid} не найден")
        return
    await state.update_data(uid=uid, username=user[2])
    await m.answer(f"👤 Пользователь: {user[2]} (ID: {uid})\n💰 Текущий баланс: {user[3]:.2f} ₽\n\nВведите сумму изменения (+ или -):")
    await state.set_state(AdminBalance.amount)

@dp.message(AdminBalance.amount)
async def admin_balance_amount(m: types.Message, state: FSMContext):
    try:
        amt = float(m.text)
    except:
        await m.answer("❌ Введите число")
        return
    data = await state.get_data()
    await update_balance(data['uid'], amt)
    new_bal = await get_balance(data['uid'])
    await bot.send_message(data['uid'], f"👤 *Изменение баланса*\n\n💰 Сумма: {amt:+.2f} ₽\n💵 Новый баланс: {new_bal:.2f} ₽\n\n👨‍💻 Администратор изменил ваш баланс.", parse_mode="Markdown")
    await m.answer(f"✅ Баланс пользователя {data['username']} изменён на {amt:+.2f} ₽\n💰 Новый баланс: {new_bal:.2f} ₽", reply_markup=admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_ban")
async def admin_ban_start(cb: types.CallbackQuery, state: FSMContext):
    await edit_or_send(cb, "🚫 *Блокировка пользователя*\n\nВведите Telegram ID пользователя:", admin_kb())
    await state.set_state(AdminBan.uid)

@dp.message(AdminBan.uid)
async def admin_ban_uid(m: types.Message, state: FSMContext):
    try:
        uid = int(m.text)
    except:
        await m.answer("❌ ID должен быть числом")
        return
    user = await get_user(uid)
    if not user:
        await m.answer(f"❌ Пользователь с ID {uid} не найден")
        return
    async with get_db() as conn:
        await conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (uid,))
        await conn.commit()
    await bot.send_message(uid, f"🚫 *Ваш аккаунт заблокирован*\n\nСвяжитесь с администратором для выяснения причин.\n👉 {SUPPORT_LINK}", parse_mode="Markdown")
    await m.answer(f"✅ Пользователь {user[2]} (ID: {uid}) заблокирован", reply_markup=admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_mailing")
async def admin_mailing(cb: types.CallbackQuery, state: FSMContext):
    await edit_or_send(cb, "📢 *Рассылка*\n\nВведите текст сообщения:", admin_kb())
    await state.set_state(AdminMailing.text)

@dp.message(AdminMailing.text)
async def mailing_send(m: types.Message, state: FSMContext):
    text = m.text
    users = await get_all_users()
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
    
    await m.answer(f"✅ Рассылка завершена!\nОтправлено: {success}/{len(users)}", reply_markup=admin_kb())
    await state.clear()

app = Flask(__name__)

@app.route('/')
def index():
    return "Bot running!"

@app.route('/health')
def health():
    return "OK"

async def main():
    await init_db()
    asyncio.create_task(update_rates_loop())
    asyncio.create_task(check_deposits_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    is_railway = bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN"))
    if is_railway:
        def run_bot():
            asyncio.run(main())
        threading.Thread(target=run_bot, daemon=True).start()
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
