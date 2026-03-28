import os
import asyncio
import sqlite3
import time
import aiohttp
import logging
import threading
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional
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

# ВАШИ КОШЕЛЬКИ (ВСТАВЛЕНЫ ПРЯМО СЮДА)
WALLETS = {
    'ton': "UQAunfNNErk6s1VC4ycJD2UI_U7aAK53M1LM1ebAv4vbqcDs",           # TON
    'usdt_ton': "UQAunfNNErk6s1VC4ycJD2UI_U7aAK53M1LM1ebAv4vbqcDs",    # USDT TON (тот же)
    'usdt_trc20': "TGt4Jpn5xk7CzkxeDynnkhwVyDDU124g6B"                  # USDT TRC20
}

# Проверяем загрузку
print("\n" + "="*50)
print("💰 ЗАГРУЖЕНЫ КОШЕЛЬКИ:")
print(f"  TON: {WALLETS['ton'][:15]}...")
print(f"  USDT TON: {WALLETS['usdt_ton'][:15]}...")
print(f"  USDT TRC20: {WALLETS['usdt_trc20'][:15]}...")
print("="*50 + "\n")

if not BOT_TOKEN:
    print("❌ BOT_TOKEN не задан!")
    exit(1)

# Настройки
MIN_EXCHANGE = 1.0  # Минимум 1 TON или 1 USDT
MIN_WITHDRAWAL = 100
MAX_WITHDRAWAL = 100000
COMMISSION = 7  # 7%
REFERRAL_PERCENT = 1  # 1%

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============= БАЗА ДАННЫХ =============
conn = sqlite3.connect("exchange.db", check_same_thread=False)
c = conn.cursor()

# Создаем таблицы
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

# Добавляем курсы по умолчанию
c.execute("INSERT OR IGNORE INTO exchange_rates (crypto, rate_rub) VALUES (?, ?)", ('ton', 500))
c.execute("INSERT OR IGNORE INTO exchange_rates (crypto, rate_rub) VALUES (?, ?)", ('usdt_ton', 85))
c.execute("INSERT OR IGNORE INTO exchange_rates (crypto, rate_rub) VALUES (?, ?)", ('usdt_trc20', 85))

# Настройки
c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('commission', str(COMMISSION)))
c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('referral_percent', str(REFERRAL_PERCENT)))

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
    
    # Реферальный бонус
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
    c.execute("SELECT rate_rub FROM exchange_rates WHERE crypto = ?", (crypto,))
    row = c.fetchone()
    return row[0] if row else 85.0

def set_exchange_rate(crypto, rate):
    c.execute("UPDATE exchange_rates SET rate_rub = ? WHERE crypto = ?", (rate, crypto))
    conn.commit()

def get_commission():
    c.execute("SELECT value FROM settings WHERE key = 'commission'")
    return float(c.fetchone()[0])

def get_referral_percent():
    c.execute("SELECT value FROM settings WHERE key = 'referral_percent'")
    return float(c.fetchone()[0])

def get_statistics():
    c.execute("SELECT COUNT(*) FROM users WHERE is_banned = 0")
    users = c.fetchone()[0]
    c.execute("SELECT SUM(amount) FROM deposits WHERE status = 'completed'")
    deposits = c.fetchone()[0] or 0
    c.execute("SELECT SUM(amount) FROM withdrawals WHERE status = 'completed'")
    withdrawals = c.fetchone()[0] or 0
    return users, deposits, withdrawals

# ============= ПОЛУЧЕНИЕ КУРСОВ =============
async def get_usdt_rub():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub", 
                                  timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data['tether']['rub'])
    except Exception as e:
        logger.error(f"Ошибка курса: {e}")
    return 85.0

async def get_ton_rub():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=rub",
                                  timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data['the-open-network']['rub'])
    except Exception as e:
        logger.error(f"Ошибка TON курса: {e}")
    return 500.0

async def update_rates():
    usdt_rate = await get_usdt_rub()
    ton_rate = await get_ton_rub()
    set_exchange_rate('usdt_ton', usdt_rate)
    set_exchange_rate('usdt_trc20', usdt_rate)
    set_exchange_rate('ton', ton_rate)
    logger.info(f"✅ Курсы: USDT={usdt_rate:.2f}₽, TON={ton_rate:.2f}₽")

# ============= ПРОВЕРКА ТРАНЗАКЦИЙ =============
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

async def check_trc20_tx(memo):
    address = WALLETS['usdt_trc20']
    try:
        url = f"https://apilist.tronscan.org/api/transaction?address={address}&limit=50&sort=-timestamp"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for tx in data.get('data', []):
                        if tx.get('contractType') == 31:
                            extra = tx.get('extra_param', {})
                            if extra.get('memo') == memo:
                                amount = int(tx.get('amount', 0)) / 1e6
                                return {'amount': amount, 'tx_hash': tx.get('hash')}
    except Exception as e:
        logger.error(f"TRC20 check error: {e}")
    return None

async def check_deposits_loop():
    processed = set()
    while True:
        try:
            c.execute("SELECT id, user_id, crypto, memo, amount FROM deposits WHERE status = 'pending'")
            pending = c.fetchall()
            
            for dep in pending:
                if dep[3] in processed:
                    continue
                
                tx = None
                if dep[2] == 'ton':
                    tx = await check_ton_tx(dep[3])
                elif dep[2] == 'usdt_trc20':
                    tx = await check_trc20_tx(dep[3])
                
                if tx and tx['amount'] >= dep[4]:
                    processed.add(dep[3])
                    rate = get_exchange_rate(dep[2])
                    gross = tx['amount'] * rate
                    fee = gross * get_commission() / 100
                    rub = gross - fee
                    update_balance(dep[1], rub)
                    complete_deposit(dep[0], tx['amount'], rub, dep[1], tx['tx_hash'])
                    
                    await bot.send_message(
                        dep[1],
                        f"✅ *Обмен выполнен!*\n\n💰 Зачислено: {rub:.2f} ₽\n📊 Курс: {rate:.2f} ₽",
                        parse_mode="Markdown"
                    )
        except Exception as e:
            logger.error(f"Check deposits error: {e}")
        await asyncio.sleep(60)

# ============= КЛАВИАТУРЫ =============
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Баланс", callback_data="balance")],
        [InlineKeyboardButton(text="🔄 Обмен", callback_data="exchange")],
        [InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw")],
        [InlineKeyboardButton(text="👥 Рефералы", callback_data="referrals")],
        [InlineKeyboardButton(text="📜 История", callback_data="history")],
        [InlineKeyboardButton(text="🆘 Поддержка", url="https://t.me/cryptohelp_01")]
    ])

def exchange_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="exch_ton")],
        [InlineKeyboardButton(text="💰 USDT (TON)", callback_data="exch_usdt_ton")],
        [InlineKeyboardButton(text="💵 USDT (TRC20)", callback_data="exch_usdt_trc20")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back")]])

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="back")]])

def confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="back")]
    ])

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Заявки", callback_data="admin_requests")],
        [InlineKeyboardButton(text="🔧 Курсы", callback_data="admin_rates")],
        [InlineKeyboardButton(text="⚙️ Комиссия", callback_data="admin_commission")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👤 Баланс", callback_data="admin_balance")],
        [InlineKeyboardButton(text="🚫 Блок", callback_data="admin_ban")],
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

# ============= БОТ =============
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

async def welcome(target, user_id, username, ref_by=None):
    user = get_user(user_id)
    if user and user[6]:  # is_banned
        await target.answer("⛔ Аккаунт заблокирован")
        return
    create_user(user_id, username, ref_by)
    
    text = (
        "✨ *Добро пожаловать в CryptoExchangeBot* ✨\n\n"
        "Обмен TON и USDT на рубли по выгодному курсу.\n\n"
        "📌 *Как это работает*\n"
        "1️⃣ Выберите криптовалюту\n"
        "2️⃣ Введите сумму (мин. 1)\n"
        "3️⃣ Отправьте крипту на кошелек с комментарием\n"
        "4️⃣ После зачисления рубли поступят на баланс\n"
        "5️⃣ Выведите на карту\n\n"
        f"💸 *Комиссия*: {get_commission()}%\n"
        f"🎁 *Рефералка*: {get_referral_percent()}%\n\n"
        f"💰 *Мин. вывод*: {MIN_WITHDRAWAL} ₽\n\n"
        "⬇️ *Выберите действие*"
    )
    
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
    await edit_or_send(cb, f"💰 *Баланс*\n\n{bal:.2f} ₽", back_kb())

@dp.callback_query(F.data == "exchange")
async def exchange_menu(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_or_send(cb, "🔄 *Выберите валюту*", exchange_kb())

@dp.callback_query(F.data.startswith("exch_"))
async def exch_select(cb: types.CallbackQuery, state: FSMContext):
    crypto = cb.data.split("_")[1]  # ton, usdt_ton, usdt_trc20
    
    # ПРЯМАЯ ПРОВЕРКА - ЕСЛИ КЛЮЧ ЕСТЬ В WALLETS, ВСЕ РАБОТАЕТ
    if crypto not in WALLETS:
        await cb.answer(f"❌ Валюта {crypto} не найдена!", show_alert=True)
        return
    
    await state.update_data(crypto=crypto)
    rate = get_exchange_rate(crypto)
    await edit_or_send(cb,
        f"🔄 *Обмен {crypto.upper()}*\n\n"
        f"Курс: 1 {crypto.upper()} = {rate:.2f} ₽\n"
        f"Комиссия: {get_commission()}%\n"
        f"Мин. сумма: {MIN_EXCHANGE} {crypto.upper()}\n\n"
        "Введите сумму:",
        cancel_kb())
    await state.set_state(Exchange.amount)

@dp.message(Exchange.amount)
async def exch_amount(m: types.Message, state: FSMContext):
    try:
        amount = float(m.text)
        if amount < MIN_EXCHANGE:
            await m.answer(f"❌ Минимум {MIN_EXCHANGE}", reply_markup=cancel_kb())
            return
    except:
        await m.answer("❌ Введите число", reply_markup=cancel_kb())
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
        f"Курс: {rate:.2f} ₽\n"
        f"Комиссия: -{fee:.2f} ₽\n"
        f"Вы получите: *{net:.2f} ₽*\n\n"
        "Подтверждаете?",
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
    address = WALLETS[crypto]  # БЕРЕМ КОШЕЛЕК ИЗ СЛОВАРЯ
    
    add_deposit(user_id, crypto, memo, amount)
    
    text = (
        f"🔄 *Обмен {crypto.upper()}*\n\n"
        f"📤 *Отправьте на адрес*\n`{address}`\n\n"
        f"📝 *Комментарий (memo)*\n`{memo}`\n\n"
        f"💰 Вы получите: {net:.2f} ₽\n\n"
        f"⏱ Зачисление 1-5 минут"
    )
    await edit_or_send(cb, text, InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="back")]]))
    await state.clear()

@dp.callback_query(F.data == "withdraw")
async def withdraw_menu(cb: types.CallbackQuery, state: FSMContext):
    bal = get_balance(cb.from_user.id)
    if bal < MIN_WITHDRAWAL:
        await edit_or_send(cb, f"❌ Недостаточно. Мин. {MIN_WITHDRAWAL} ₽", back_kb())
        return
    await edit_or_send(cb,
        f"💸 *Вывод*\n\nБаланс: {bal:.2f} ₽\nМин: {MIN_WITHDRAWAL} ₽\n\nВведите сумму:",
        cancel_kb())
    await state.set_state(Withdraw.amount)

@dp.message(Withdraw.amount)
async def withdraw_amount(m: types.Message, state: FSMContext):
    try:
        amount = float(m.text)
        if amount < MIN_WITHDRAWAL:
            await m.answer(f"❌ Минимум {MIN_WITHDRAWAL} ₽", reply_markup=cancel_kb())
            return
        bal = get_balance(m.from_user.id)
        if amount > bal:
            await m.answer(f"❌ Недостаточно. Баланс: {bal:.2f} ₽", reply_markup=cancel_kb())
            return
    except:
        await m.answer("❌ Введите число", reply_markup=cancel_kb())
        return
    
    await state.update_data(amount=amount)
    await m.answer("Введите реквизиты:", reply_markup=cancel_kb())
    await state.set_state(Withdraw.details)

@dp.message(Withdraw.details)
async def withdraw_details(m: types.Message, state: FSMContext):
    details = m.text.strip()
    if len(details) < 5:
        await m.answer("❌ Слишком коротко", reply_markup=cancel_kb())
        return
    
    data = await state.get_data()
    amount = data['amount']
    update_balance(m.from_user.id, -amount)
    wid = add_withdrawal(m.from_user.id, amount, details)
    await m.answer(f"✅ Заявка #{wid} создана", reply_markup=back_kb())
    await state.clear()

@dp.callback_query(F.data == "referrals")
async def referrals_cb(cb: types.CallbackQuery, state: FSMContext):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={cb.from_user.id}"
    c.execute("SELECT COUNT(*) FROM users WHERE ref_by = ?", (cb.from_user.id,))
    invited = c.fetchone()[0]
    c.execute("SELECT ref_bonus FROM users WHERE user_id = ?", (cb.from_user.id,))
    bonus = c.fetchone()[0] or 0
    await edit_or_send(cb,
        f"👥 *Рефералы*\n\n🔗 {link}\n👤 Приглашено: {invited}\n🎁 Бонусов: {bonus:.2f} ₽",
        back_kb())

@dp.callback_query(F.data == "history")
async def history_cb(cb: types.CallbackQuery, state: FSMContext):
    c.execute("SELECT crypto, amount, status, created_at FROM deposits WHERE user_id = ? ORDER BY created_at DESC LIMIT 5", (cb.from_user.id,))
    dep = c.fetchall()
    c.execute("SELECT amount, status, created_at FROM withdrawals WHERE user_id = ? ORDER BY created_at DESC LIMIT 5", (cb.from_user.id,))
    wd = c.fetchall()
    
    text = "📜 *История*\n\n"
    if dep:
        text += "*Обмены*\n"
        for d in dep:
            text += f"• {d[0].upper()} {d[1]:.4f} - {d[2]}\n"
    if wd:
        text += "\n*Выводы*\n"
        for w in wd:
            text += f"• {w[0]:.2f} ₽ - {w[1]}\n"
    if not dep and not wd:
        text += "Операций нет"
    await edit_or_send(cb, text, back_kb())

# ============= АДМИНКА =============
@dp.message(Command("admin"))
async def admin_cmd(m: types.Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        await m.answer("⛔ Нет доступа")
        return
    await m.answer("🛡 *Админ панель*", parse_mode="Markdown", reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_back")
async def admin_back(cb: types.CallbackQuery, state: FSMContext):
    await edit_or_send(cb, "🛡 *Админ панель*", admin_kb())

@dp.callback_query(F.data == "admin_exit")
async def admin_exit(cb: types.CallbackQuery, state: FSMContext):
    await back_cb(cb, state)

@dp.callback_query(F.data == "admin_requests")
async def admin_requests(cb: types.CallbackQuery, state: FSMContext):
    c.execute("SELECT id, user_id, amount, details, status FROM withdrawals WHERE status = 'pending'")
    rows = c.fetchall()
    if not rows:
        await edit_or_send(cb, "📭 Нет заявок", admin_kb())
        return
    for w in rows:
        await cb.message.answer(
            f"📋 *Заявка #{w[0]}*\n👤 {w[1]}\n💰 {w[2]:.2f} ₽\n📝 {w[3]}\n🏷 {w[4]}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_{w[0]}"),
                 InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{w[0]}")]
            ])
        )
    await cb.answer()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_req(cb: types.CallbackQuery):
    wid = int(cb.data.split("_")[1])
    w = get_withdrawal(wid)
    if w:
        update_withdrawal_status(wid, 'completed')
        await bot.send_message(w[1], f"✅ Вывод #{wid} выполнен")
        await cb.message.edit_text(f"✅ Заявка #{wid} подтверждена")
    await cb.answer()

@dp.callback_query(F.data.startswith("reject_"))
async def reject_req(cb: types.CallbackQuery):
    wid = int(cb.data.split("_")[1])
    w = get_withdrawal(wid)
    if w:
        update_withdrawal_status(wid, 'rejected')
        update_balance(w[1], w[2])
        await bot.send_message(w[1], f"❌ Вывод #{wid} отклонён")
        await cb.message.edit_text(f"❌ Заявка #{wid} отклонена")
    await cb.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cb: types.CallbackQuery):
    users, deposits, withdrawals = get_statistics()
    await edit_or_send(cb,
        f"📊 *Статистика*\n\n👥 Пользователей: {users}\n💰 Депозитов: {deposits:.2f} ₽\n💸 Выводов: {withdrawals:.2f} ₽",
        admin_kb())

@dp.callback_query(F.data == "admin_rates")
async def admin_rates_menu(cb: types.CallbackQuery):
    await edit_or_send(cb, "🔧 *Выберите валюту*", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="TON", callback_data="rate_ton")],
        [InlineKeyboardButton(text="USDT", callback_data="rate_usdt")]
    ]))

@dp.callback_query(F.data.startswith("rate_"))
async def admin_rate_select(cb: types.CallbackQuery, state: FSMContext):
    crypto = cb.data.split("_")[1]
    await state.update_data(crypto=crypto)
    await edit_or_send(cb, f"Введите курс для {crypto.upper()}:", admin_kb())
    await state.set_state(AdminRate.rate)

@dp.message(AdminRate.rate)
async def admin_rate_set(m: types.Message, state: FSMContext):
    try:
        rate = float(m.text)
    except:
        await m.answer("❌ Введите число")
        return
    data = await state.get_data()
    set_exchange_rate(data['crypto'], rate)
    await m.answer(f"✅ Курс {data['crypto']} = {rate:.2f} ₽", reply_markup=admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_commission")
async def admin_commission_menu(cb: types.CallbackQuery, state: FSMContext):
    current = get_commission()
    await edit_or_send(cb, f"Текущая комиссия: {current}%\nВведите новую:", admin_kb())
    await state.set_state(AdminRate.rate)  # переиспользуем

@dp.callback_query(F.data == "admin_balance")
async def admin_balance_start(cb: types.CallbackQuery, state: FSMContext):
    await edit_or_send(cb, "Введите ID пользователя:", admin_kb())
    await state.set_state(AdminBalance.uid)

@dp.message(AdminBalance.uid)
async def admin_balance_uid(m: types.Message, state: FSMContext):
    try:
        uid = int(m.text)
    except:
        await m.answer("❌ Введите число")
        return
    user = get_user(uid)
    if not user:
        await m.answer("❌ Пользователь не найден")
        return
    await state.update_data(uid=uid)
    await m.answer("Введите сумму (+ или -):")
    await state.set_state(AdminBalance.amount)

@dp.message(AdminBalance.amount)
async def admin_balance_amount(m: types.Message, state: FSMContext):
    try:
        amt = float(m.text)
    except:
        await m.answer("❌ Введите число")
        return
    data = await state.get_data()
    update_balance(data['uid'], amt)
    new_bal = get_balance(data['uid'])
    await m.answer(f"✅ Баланс изменён на {amt:+.2f} ₽\nНовый: {new_bal:.2f} ₽", reply_markup=admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_ban")
async def admin_ban_start(cb: types.CallbackQuery, state: FSMContext):
    await edit_or_send(cb, "Введите ID пользователя для блокировки:", admin_kb())
    await state.set_state(AdminBalance.uid)

# ============= ЗАПУСК =============
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot running!"

@app.route('/health')
def health():
    return "OK"

async def start_background():
    asyncio.create_task(update_rates())
    asyncio.create_task(check_deposits_loop())

def run_bot():
    async def main():
        await start_background()
        await dp.start_polling(bot)
    asyncio.run(main())

if __name__ == "__main__":
    print("🚀 Запуск бота...")
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
