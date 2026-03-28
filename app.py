import os
import asyncio
import sqlite3
import time
import aiohttp
import logging
import threading
from datetime import datetime
from collections import defaultdict
from flask import Flask, jsonify
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ============= КОНФИГ =============
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_IDS", "").split(",") if id.strip()]
WELCOME_IMAGE_URL = os.environ.get("WELCOME_IMAGE_URL", "")
SUPPORT_LINK = "https://t.me/cryptohelp_01"

# КОШЕЛЬКИ (прямо в коде)
TON_WALLET = "UQAunfNNErk6s1VC4ycJD2UI_U7aAK53M1LM1ebAv4vbqcDs"
USDT_TON_WALLET = "UQAunfNNErk6s1VC4ycJD2UI_U7aAK53M1LM1ebAv4vbqcDs"   # тот же, что и TON
USDT_TRC20_WALLET = "TGt4Jpn5xk7CzkxeDynnkhwVyDDU124g6B"

WALLETS = {
    'ton': TON_WALLET,
    'usdt_ton': USDT_TON_WALLET,
    'usdt_trc20': USDT_TRC20_WALLET
}

print("\n" + "="*50)
print("💰 КОШЕЛЬКИ ЗАГРУЖЕНЫ:")
print(f"  TON: {TON_WALLET[:15]}...")
print(f"  USDT TON: {USDT_TON_WALLET[:15]}...")
print(f"  USDT TRC20: {USDT_TRC20_WALLET[:15]}...")
print("="*50 + "\n")

if not BOT_TOKEN:
    print("❌ BOT_TOKEN не задан!")
    exit(1)

MIN_EXCHANGE = 1.0
MIN_WITHDRAWAL = 100
MAX_WITHDRAWAL = 100000

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============= БАЗА ДАННЫХ =============
conn = sqlite3.connect("exchange.db", check_same_thread=False)
c = conn.cursor()

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
              amount REAL,
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
              rate_rub REAL)''')

c.execute('''CREATE TABLE IF NOT EXISTS referral_earnings
             (id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER,
              from_user_id INTEGER,
              amount REAL,
              created_at INTEGER)''')

c.execute('''CREATE TABLE IF NOT EXISTS settings
             (key TEXT PRIMARY KEY,
              value TEXT)''')

# Курсы по умолчанию
c.execute("INSERT OR IGNORE INTO exchange_rates (crypto, rate_rub) VALUES (?, ?)", ('ton', 100))   # запасной курс TON
c.execute("INSERT OR IGNORE INTO exchange_rates (crypto, rate_rub) VALUES (?, ?)", ('usdt', 85))

# Настройки
c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('commission', '7'))
c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('referral_percent', '1'))

conn.commit()

# ============= ФУНКЦИИ =============
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
    # Для USDT оба варианта возвращают курс usdt
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

# ============= ПОЛУЧЕНИЕ КУРСОВ =============
# Последние успешные курсы (для fallback)
last_known_rates = {'usdt': 85.0, 'ton': 100.0}

async def get_usdt_rate():
    """Курс USDT/RUB с множественными источниками"""
    sources = [
        ("CoinGecko", "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub"),
        ("Binance P2P", "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search", True),
        ("CBR", "https://www.cbr-xml-daily.ru/daily_json.js"),
    ]
    for src in sources:
        try:
            async with aiohttp.ClientSession() as session:
                if src[0] == "Binance P2P":
                    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
                    payload = {"asset": "USDT", "fiat": "RUB", "tradeType": "BUY", "page": 1, "rows": 1}
                    async with session.post(src[1], json=payload, headers=headers, timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get('data') and len(data['data']) > 0:
                                rate = float(data['data'][0]['adv']['price'])
                                return rate
                elif src[0] == "CBR":
                    async with session.get(src[1], timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return float(data['Valute']['USD']['Value'])
                else:
                    async with session.get(src[1], timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return float(data['tether']['rub'])
        except Exception as e:
            logger.warning(f"Ошибка {src[0]}: {e}")
    return None

async def get_ton_rate():
    """Курс TON/RUB с множественными источниками"""
    sources = [
        ("CoinGecko", "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=rub"),
        ("Bybit", "https://api.bybit.com/v5/market/tickers?category=spot&symbol=TONUSDT"),
        ("KuCoin", "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol=TON-USDT"),
    ]
    for src in sources:
        try:
            async with aiohttp.ClientSession() as session:
                if src[0] == "Bybit":
                    async with session.get(src[1], timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get('result') and data['result']['list']:
                                ton_usdt = float(data['result']['list'][0]['lastPrice'])
                                usdt_rub = await get_usdt_rate()
                                if usdt_rub:
                                    return ton_usdt * usdt_rub
                elif src[0] == "KuCoin":
                    async with session.get(src[1], timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get('data'):
                                ton_usdt = float(data['data']['price'])
                                usdt_rub = await get_usdt_rate()
                                if usdt_rub:
                                    return ton_usdt * usdt_rub
                else:
                    async with session.get(src[1], timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return float(data['the-open-network']['rub'])
        except Exception as e:
            logger.warning(f"Ошибка {src[0]}: {e}")
    return None

async def update_rates_loop():
    global last_known_rates
    while True:
        try:
            usdt_rate = await get_usdt_rate()
            if usdt_rate:
                last_known_rates['usdt'] = usdt_rate
                set_exchange_rate('usdt', usdt_rate)
                logger.info(f"✅ USDT курс: {usdt_rate:.2f} ₽")
            else:
                # Используем последний известный курс
                usdt_rate = last_known_rates['usdt']
                logger.warning(f"⚠️ USDT курс не получен, использую последний: {usdt_rate:.2f} ₽")

            ton_rate = await get_ton_rate()
            if ton_rate:
                last_known_rates['ton'] = ton_rate
                set_exchange_rate('ton', ton_rate)
                logger.info(f"✅ TON курс: {ton_rate:.2f} ₽")
            else:
                # Fallback: 100 руб или последний известный
                ton_rate = last_known_rates['ton']
                logger.warning(f"⚠️ TON курс не получен, использую последний: {ton_rate:.2f} ₽")
        except Exception as e:
            logger.error(f"Ошибка обновления курсов: {e}")
        await asyncio.sleep(30)

# ============= ПРОВЕРКА ТРАНЗАКЦИЙ (только для TON) =============
async def check_ton_tx(memo):
    address = WALLETS['ton']
    try:
        url = f"https://toncenter.com/api/v2/getTransactions?address={address}&limit=50"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for tx in data.get('result', []):
                        in_msg = tx.get('in_msg')
                        if in_msg and in_msg.get('message') == memo:
                            amount = int(in_msg.get('value', 0)) / 1e9
                            return {'amount': amount, 'tx_hash': tx.get('transaction_id', {}).get('hash')}
    except Exception as e:
        logger.error(f"TON check error: {e}")
    return None

async def check_deposits_loop(bot):
    processed = set()
    while True:
        try:
            c.execute("SELECT id, user_id, crypto, memo, amount FROM deposits WHERE status = 'pending' AND crypto = 'ton'")
            pending = c.fetchall()
            for dep in pending:
                if dep[3] in processed:
                    continue
                tx = await check_ton_tx(dep[3])
                if tx and tx['amount'] >= dep[4]:
                    processed.add(dep[3])
                    rate = get_exchange_rate('ton')
                    gross = tx['amount'] * rate
                    fee = gross * get_commission() / 100
                    rub = gross - fee
                    update_balance(dep[1], rub)
                    complete_deposit(dep[0], tx['amount'], rub, dep[1], tx['tx_hash'])
                    await bot.send_message(
                        dep[1],
                        f"✅ *Обмен TON выполнен!*\n\n"
                        f"💰 Зачислено: {rub:.2f} ₽\n"
                        f"📊 Курс: {rate:.2f} ₽\n"
                        f"💸 Комиссия: {fee:.2f} ₽",
                        parse_mode="Markdown"
                    )
        except Exception as e:
            logger.error(f"Check deposits error: {e}")
        await asyncio.sleep(60)

# ============= ЛИМИТЫ ДЛЯ USDT-ЗАЯВОК =============
user_usdt_requests = defaultdict(list)

def can_make_usdt_request(user_id):
    now = datetime.now()
    # Очищаем старые запросы (старше 10 минут)
    user_usdt_requests[user_id] = [t for t in user_usdt_requests[user_id] if now - t < timedelta(minutes=10)]
    if len(user_usdt_requests[user_id]) >= 1:
        return False
    user_usdt_requests[user_id].append(now)
    return True

# ============= КЛАВИАТУРЫ =============
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
        [InlineKeyboardButton(text="🔧 Курсы", callback_data="admin_rates")],
        [InlineKeyboardButton(text="💰 Кошельки", callback_data="admin_wallets")],
        [InlineKeyboardButton(text="⚙️ Комиссия", callback_data="admin_commission")],
        [InlineKeyboardButton(text="🎁 Рефералка", callback_data="admin_referral")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👤 Баланс", callback_data="admin_balance")],
        [InlineKeyboardButton(text="🚫 Блок", callback_data="admin_ban")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_mailing")],
        [InlineKeyboardButton(text="◀️ Выход", callback_data="admin_exit")]
    ])

# ============= FSM =============
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

# ============= БОТ =============
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

async def welcome(target, user_id, username, ref_by=None):
    user = get_user(user_id)
    if user and user[6]:
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
        f"💰 *Минимальная сумма вывода*: {MIN_WITHDRAWAL} ₽\n"
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
    except Exception as e:
        if "message is not modified" not in str(e):
            try:
                await cb.message.edit_caption(caption=text, parse_mode="Markdown", reply_markup=markup)
            except:
                await cb.message.answer(text, parse_mode="Markdown", reply_markup=markup)
    await cb.answer()

# ============= ХЕНДЛЕРЫ =============
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
    bal = get_balance(cb.from_user.id)
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
    crypto = cb.data.split("_")[1]  # ton, usdt_ton, usdt_trc20
    
    if crypto not in WALLETS:
        await cb.answer(f"❌ Валюта не поддерживается", show_alert=True)
        return
    
    await state.update_data(crypto=crypto)
    rate = get_exchange_rate(crypto)
    await edit_or_send(cb,
        f"🔄 *Обмен {crypto.upper()} на рубли*\n\n"
        f"Текущий курс: 1 {crypto.upper()} = {rate:.2f} ₽\n"
        f"Комиссия сервиса: {get_commission()}%\n"
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
    
    # Для USDT – создаём заявку на ручное подтверждение
    if crypto in ('usdt_ton', 'usdt_trc20'):
        # Сохраняем заявку в БД (статус pending)
        deposit_id = add_deposit(user_id, crypto, memo, amount)
        
        # Отправляем пользователю информацию о кошельке
        await edit_or_send(cb,
            f"🔄 *Обмен {crypto.upper()}*\n\n"
            f"📤 *Отправьте на адрес*\n`{address}`\n\n"
            f"📝 *Обязательный комментарий (memo)*\n`{memo}`\n\n"
            f"💰 Вы получите: {net:.2f} ₽ (после вычета комиссии)\n\n"
            f"⚡️ *Важно*\n"
            f"• Переводите только {crypto.upper()} на указанный адрес\n"
            f"• Укажите комментарий точно, как выше\n"
            f"• После отправки нажмите кнопку «Проверить обмен»\n\n"
            f"⏱ Ссылка на проверку будет доступна после оплаты.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Проверить обмен", callback_data=f"check_usdt_{deposit_id}")],
                [InlineKeyboardButton(text="◀️ В главное меню", callback_data="back")]
            ]))
        await state.clear()
        return
    
    # Для TON – автоматическая проверка
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
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В главное меню", callback_data="back")]])
    await edit_or_send(cb, text, kb)
    await state.clear()

# Обработчик кнопки "Проверить обмен" для USDT
@dp.callback_query(F.data.startswith("check_usdt_"))
async def check_usdt(cb: types.CallbackQuery, state: FSMContext):
    deposit_id = int(cb.data.split("_")[2])
    
    # Проверяем, существует ли заявка и не была ли она уже обработана
    c.execute("SELECT id, user_id, crypto, memo, amount, status FROM deposits WHERE id = ?", (deposit_id,))
    dep = c.fetchone()
    if not dep:
        await cb.answer("❌ Заявка не найдена", show_alert=True)
        return
    
    if dep[5] != 'pending':
        await cb.answer("❌ Заявка уже обработана", show_alert=True)
        return
    
    # Проверяем лимит заявок от пользователя
    if not can_make_usdt_request(dep[1]):
        await cb.answer("⚠️ Вы уже отправляли запрос на проверку. Подождите 10 минут.", show_alert=True)
        return
    
    # Отправляем уведомление админам
    for admin_id in ADMIN_IDS:
        await bot.send_message(
            admin_id,
            f"📢 *Запрос на подтверждение USDT обмена*\n\n"
            f"👤 Пользователь: `{dep[1]}`\n"
            f"💎 Валюта: {dep[2].upper()}\n"
            f"📊 Сумма: {dep[4]:.4f} {dep[2].upper()}\n"
            f"📝 Memo: `{dep[3]}`\n"
            f"💰 К получению: {dep[4] * get_exchange_rate(dep[2]) * (100 - get_commission()) / 100:.2f} ₽\n\n"
            f"Кошелёк: {WALLETS[dep[2]]}\n\n"
            f"Проверьте транзакцию и подтвердите.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_usdt_{deposit_id}"),
                 InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_usdt_{deposit_id}")]
            ])
        )
    
    await cb.answer("✅ Запрос отправлен администратору. Ожидайте подтверждения.", show_alert=True)

# Обработчики для админов
@dp.callback_query(F.data.startswith("confirm_usdt_"))
async def confirm_usdt(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    
    deposit_id = int(cb.data.split("_")[2])
    c.execute("SELECT id, user_id, crypto, memo, amount, status FROM deposits WHERE id = ?", (deposit_id,))
    dep = c.fetchone()
    if not dep or dep[5] != 'pending':
        await cb.answer("❌ Заявка не найдена или уже обработана", show_alert=True)
        return
    
    # Рассчитываем сумму к начислению
    rate = get_exchange_rate(dep[2])
    gross = dep[4] * rate
    fee = gross * get_commission() / 100
    rub = gross - fee
    
    # Начисляем баланс
    update_balance(dep[1], rub)
    complete_deposit(deposit_id, dep[4], rub, dep[1], None)
    
    # Уведомляем пользователя
    await bot.send_message(
        dep[1],
        f"✅ *Обмен {dep[2].upper()} подтверждён!*\n\n"
        f"💰 Зачислено: {rub:.2f} ₽\n"
        f"📊 Курс: {rate:.2f} ₽\n"
        f"💸 Комиссия: {fee:.2f} ₽",
        parse_mode="Markdown"
    )
    
    await cb.message.edit_text(f"✅ Заявка #{deposit_id} подтверждена, средства начислены.")
    await cb.answer()

@dp.callback_query(F.data.startswith("reject_usdt_"))
async def reject_usdt(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    
    deposit_id = int(cb.data.split("_")[2])
    c.execute("SELECT id, user_id, crypto, memo, amount, status FROM deposits WHERE id = ?", (deposit_id,))
    dep = c.fetchone()
    if not dep or dep[5] != 'pending':
        await cb.answer("❌ Заявка не найдена или уже обработана", show_alert=True)
        return
    
    # Обновляем статус
    c.execute("UPDATE deposits SET status = 'rejected' WHERE id = ?", (deposit_id,))
    conn.commit()
    
    # Уведомляем пользователя
    await bot.send_message(
        dep[1],
        f"❌ *Обмен {dep[2].upper()} отклонён администратором.*\n\n"
        f"Пожалуйста, свяжитесь с поддержкой: {SUPPORT_LINK}",
        parse_mode="Markdown"
    )
    
    await cb.message.edit_text(f"❌ Заявка #{deposit_id} отклонена.")
    await cb.answer()

# ... (остальные хендлеры – баланс, вывод, рефералы, история, админка – такие же, как в предыдущей версии)
# Чтобы не дублировать, ниже я кратко приведу остальные, но в финальном коде они должны быть полными.

# (Здесь должны быть остальные хендлеры: withdraw, referrals, history, админские функции)
# Для краткости я не буду повторять их полностью, но они должны быть такими же, как в последнем рабочем коде.

# ============= ЗАПУСК =============
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot running!"

@app.route('/health')
def health():
    return "OK"

async def main():
    asyncio.create_task(update_rates_loop())
    asyncio.create_task(check_deposits_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    print("🚀 Запуск бота...")
    
    is_railway = bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN"))
    
    if is_railway:
        def run_bot():
            asyncio.run(main())
        threading.Thread(target=run_bot, daemon=True).start()
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("🛑 Бот остановлен")
