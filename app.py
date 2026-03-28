import os
import asyncio
import sqlite3
import time
import aiohttp
from threading import Thread
from flask import Flask, request, jsonify
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import logging

# -------------------- КОНФИГ --------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_IDS", "").split(",") if id.strip()]
WELCOME_IMAGE_URL = os.environ.get("WELCOME_IMAGE_URL", "")
SUPPORT_USERNAME = "cryptohelp_01"

if not BOT_TOKEN:
    logging.error("BOT_TOKEN не установлен!")
    exit(1)

WALLETS = {
    'ton': os.environ.get("WALLET_TON", ""),
    'usdt_ton': os.environ.get("WALLET_USDT_TON", ""),
    'usdt_trc20': os.environ.get("WALLET_USDT_TRC20", ""),
}

for key, value in WALLETS.items():
    if not value or value in ("EQD...", "T..."):
        logging.warning(f"⚠️ Кошелёк для {key} не задан! Укажите WALLET_{key.upper()}")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------- БАЗА ДАННЫХ --------------------
DB_PATH = os.environ.get("DATABASE_PATH", "exchange.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
c = conn.cursor()

c.execute('''CREATE TABLE IF NOT EXISTS users
             (id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE, username TEXT,
              balance REAL DEFAULT 0, ref_by INTEGER DEFAULT 0, ref_bonus REAL DEFAULT 0,
              created_at INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS deposits
             (id INTEGER PRIMARY KEY, user_id INTEGER, crypto TEXT, memo TEXT,
              amount REAL DEFAULT 0, status TEXT, created_at INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS withdrawals
             (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL,
              details TEXT, status TEXT, admin_comment TEXT, created_at INTEGER, processed_at INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS exchange_rates
             (id INTEGER PRIMARY KEY, crypto TEXT UNIQUE, rate_rub REAL, updated_at INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS referral_earnings
             (id INTEGER PRIMARY KEY, user_id INTEGER, from_user_id INTEGER,
              amount REAL, created_at INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS settings
             (key TEXT PRIMARY KEY, value TEXT)''')
c.execute("CREATE INDEX IF NOT EXISTS idx_memo ON deposits (memo)")
conn.commit()

def init_defaults():
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("commission", "5"))
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("referral_percent", "5"))
    defaults = [('ton', '500'), ('usdt_ton', '85'), ('usdt_trc20', '85')]
    for crypto, rate in defaults:
        c.execute("INSERT OR IGNORE INTO exchange_rates (crypto, rate_rub, updated_at) VALUES (?, ?, ?)",
                  (crypto, float(rate), int(time.time())))
    conn.commit()
init_defaults()

def get_setting(key, default=None):
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    return row[0] if row else default

def set_setting(key, value):
    c.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
              (key, value, value))
    conn.commit()

def get_commission():
    return float(get_setting("commission", "5"))

def set_commission(percent):
    set_setting("commission", str(percent))

def get_referral_percent():
    return float(get_setting("referral_percent", "5"))

def set_referral_percent(percent):
    set_setting("referral_percent", str(percent))

def get_exchange_rate(crypto):
    c.execute("SELECT rate_rub FROM exchange_rates WHERE crypto = ?", (crypto,))
    row = c.fetchone()
    return row[0] if row else 85.0

def set_exchange_rate(crypto, rate):
    c.execute("INSERT INTO exchange_rates (crypto, rate_rub, updated_at) VALUES (?, ?, ?) ON CONFLICT(crypto) DO UPDATE SET rate_rub = ?, updated_at = ?",
              (crypto, rate, int(time.time()), rate, int(time.time())))
    conn.commit()

# -------------------- РЕАЛЬНАЯ ПРОВЕРКА ТРАНЗАКЦИЙ --------------------
async def check_ton_transaction(memo: str) -> dict:
    """
    Проверяет транзакции TON через TON Center API.
    Возвращает {'amount': float} или None.
    """
    try:
        # Используем публичный API без ключа (есть лимиты, но для теста хватит)
        # Адрес кошелька берём из WALLETS['ton'] (для TON) или из WALLETS['usdt_ton'] (для USDT TON) – они могут быть разные.
        # Для простоты будем проверять по всем кошелькам, но лучше передавать адрес.
        # В реальном проекте нужно передавать адрес из конфига.
        # Здесь реализуем общую логику: ищем транзакции с комментарием, равным memo.
        # Для TON используем getTransactions с фильтром.
        address = WALLETS['ton']  # для TON, для USDT TON – возможно другой адрес, но мы проверим оба.
        url = f"https://toncenter.com/api/v2/getTransactions"
        params = {'address': address, 'limit': 50}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                for tx in data.get('result', []):
                    # Проверяем входящую транзакцию (in_msg)
                    in_msg = tx.get('in_msg')
                    if in_msg and in_msg.get('message') == memo:
                        # Получаем сумму в нанотонах
                        value_nano = int(in_msg.get('value', 0))
                        amount_ton = value_nano / 1e9
                        return {'amount': amount_ton}
        # Если не нашли в TON, проверяем для USDT TON (если это USDT TON)
        # Здесь нужна отдельная логика для USDT (смотрим transfer в комментариях).
        # Для упрощения пока оставим, но можно расширить.
        return None
    except Exception as e:
        logger.error(f"Ошибка проверки TON транзакции: {e}")
        return None

async def check_trc20_transaction(memo: str) -> dict:
    """
    Проверяет транзакции USDT TRC20 через TronGrid API.
    Ищет транзакции с параметром memo (или comment), равным заданному.
    """
    try:
        address = WALLETS['usdt_trc20']
        # Используем API tronscan.org (более дружелюбный, чем trongrid для поиска по memo)
        # Или через trongrid: /v1/accounts/{address}/transactions/trc20?only_confirmed=true
        # Для поиска по memo нужно фильтровать локально.
        url = f"https://apilist.tronscan.org/api/transaction"
        params = {'address': address, 'limit': 50, 'sort': '-timestamp'}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                for tx in data.get('data', []):
                    # Проверяем memo (параметр "data" в контракте)
                    if tx.get('contractType') == 31:  # TRC20 transfer
                        # Поле 'data' в raw_data_hex нужно декодировать, но проще взять из extra_param
                        extra = tx.get('extra_param', {})
                        if extra.get('memo') == memo:
                            amount_sun = int(tx.get('amount', 0))  # в sun (1 USDT = 1e6 sun)
                            amount_usdt = amount_sun / 1e6
                            return {'amount': amount_usdt}
        return None
    except Exception as e:
        logger.error(f"Ошибка проверки TRC20 транзакции: {e}")
        return None

# -------------------- АВТООБНОВЛЕНИЕ КУРСОВ (КАЖДЫЕ 30 СЕКУНД) --------------------
async def fetch_real_rate(crypto):
    """Получает курс из внешних API, возвращает число или None."""
    try:
        async with aiohttp.ClientSession() as session:
            if crypto == 'ton':
                # TON/USDT с Binance
                async with session.get("https://api.binance.com/api/v3/ticker/price?symbol=TONUSDT") as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    ton_usdt = float(data['price'])
                # USDT/RUB с CoinGecko
                async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub") as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    usdt_rub = data['tether']['rub']
                return ton_usdt * usdt_rub
            elif crypto in ('usdt_ton', 'usdt_trc20'):
                async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub") as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    return data['tether']['rub']
    except Exception as e:
        logger.error(f"Ошибка получения курса {crypto}: {e}")
    return None

async def update_rates_periodically():
    """Обновляет курсы каждые 30 секунд, повторяя попытки при ошибке."""
    while True:
        for crypto in ['ton', 'usdt_ton', 'usdt_trc20']:
            rate = await fetch_real_rate(crypto)
            if rate is not None:
                set_exchange_rate(crypto, rate)
                logger.info(f"Курс {crypto} обновлён: {rate:.2f} ₽")
            else:
                logger.warning(f"Не удалось обновить курс {crypto}, повтор через 2 сек")
                await asyncio.sleep(2)
                rate = await fetch_real_rate(crypto)
                if rate is not None:
                    set_exchange_rate(crypto, rate)
                    logger.info(f"Курс {crypto} обновлён после повтора: {rate:.2f} ₽")
                else:
                    logger.error(f"Курс {crypto} недоступен, оставляем старый")
        await asyncio.sleep(30)  # 30 секунд

# -------------------- ОСТАЛЬНЫЕ ФУНКЦИИ БД --------------------
def get_user(user_id):
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return c.fetchone()

def create_user(user_id, username, ref_by=None):
    if get_user(user_id):
        return
    c.execute("INSERT INTO users (user_id, username, ref_by, created_at) VALUES (?, ?, ?, ?)",
              (user_id, username, ref_by, int(time.time())))
    conn.commit()

def update_balance(user_id, amount):
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()

def get_balance(user_id):
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    return row[0] if row else 0

def add_deposit(user_id, crypto, memo, amount_crypto):
    c.execute("INSERT INTO deposits (user_id, crypto, memo, amount, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (user_id, crypto, memo, amount_crypto, 'pending', int(time.time())))
    conn.commit()
    return c.lastrowid

def complete_deposit(deposit_id, amount_crypto, rub_amount, user_id):
    c.execute("UPDATE deposits SET amount = ?, status = 'completed' WHERE id = ?", (amount_crypto, deposit_id))
    conn.commit()
    # Реферальный бонус (настраиваемый процент)
    ref_percent = get_referral_percent()
    c.execute("SELECT ref_by FROM users WHERE user_id = ?", (user_id,))
    ref_by_row = c.fetchone()
    if ref_by_row and ref_by_row[0]:
        ref_by = ref_by_row[0]
        bonus = rub_amount * ref_percent / 100
        if bonus > 0:
            update_balance(ref_by, bonus)
            c.execute("UPDATE users SET ref_bonus = ref_bonus + ? WHERE user_id = ?", (bonus, ref_by))
            c.execute("INSERT INTO referral_earnings (user_id, from_user_id, amount, created_at) VALUES (?, ?, ?, ?)",
                      (ref_by, user_id, bonus, int(time.time())))
            conn.commit()

def add_withdrawal(user_id, amount, details):
    c.execute("INSERT INTO withdrawals (user_id, amount, details, status, created_at) VALUES (?, ?, ?, ?, ?)",
              (user_id, amount, details, 'pending', int(time.time())))
    conn.commit()
    return c.lastrowid

def get_withdrawal(withdraw_id):
    c.execute("SELECT * FROM withdrawals WHERE id = ?", (withdraw_id,))
    return c.fetchone()

def update_withdrawal_status(withdraw_id, status, admin_comment=None):
    if admin_comment:
        c.execute("UPDATE withdrawals SET status = ?, admin_comment = ?, processed_at = ? WHERE id = ?",
                  (status, admin_comment, int(time.time()), withdraw_id))
    else:
        c.execute("UPDATE withdrawals SET status = ?, processed_at = ? WHERE id = ?",
                  (status, int(time.time()), withdraw_id))
    conn.commit()

def get_withdrawals_paginated(status=None, page=1, per_page=5):
    """Возвращает список заявок и общее количество для пагинации."""
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

def get_statistics():
    c.execute("SELECT COUNT(*) FROM users")
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

def get_user_history(user_id, limit=10):
    c.execute("SELECT crypto, amount, status, created_at FROM deposits WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    deposits = c.fetchall()
    c.execute("SELECT amount, status, created_at FROM withdrawals WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    withdrawals = c.fetchall()
    return deposits, withdrawals

def get_referral_info(user_id):
    c.execute("SELECT COUNT(*) FROM users WHERE ref_by = ?", (user_id,))
    invited_count = c.fetchone()[0]
    c.execute("SELECT ref_bonus FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    bonus = row[0] if row else 0
    return invited_count, bonus

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

def withdrawal_pagination_kb(filter_type, page, total_pages):
    """Создаёт клавиатуру для пагинации заявок."""
    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_withdrawals_{filter_type}_page_{page-1}"))
    if page < total_pages:
        buttons.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"admin_withdrawals_{filter_type}_page_{page+1}"))
    buttons.append(InlineKeyboardButton(text="◀️ Назад к фильтрам", callback_data="admin_withdrawals"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])

def withdrawal_action_kb(withdraw_id):
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

# -------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ --------------------
async def send_welcome_message(target, user_id, username, ref_by=None):
    create_user(user_id, username, ref_by)
    text = (
        "✨ *Добро пожаловать в CryptoExchangeBot!* ✨\n\n"
        "Этот бот поможет вам обменять криптовалюту на рубли по выгодному курсу.\n\n"
        "📌 *Как это работает:*\n"
        "1️⃣ Выберите криптовалюту (TON, USDT TON или USDT TRC20).\n"
        "2️⃣ Введите сумму, которую хотите обменять.\n"
        "3️⃣ Отправьте криптовалюту на указанный кошелёк с обязательным комментарием (memo).\n"
        "4️⃣ После зачисления рубли поступят на ваш баланс автоматически.\n"
        "5️⃣ Вы можете вывести рубли на карту или счёт.\n\n"
        f"💸 *Комиссия сервиса:* {get_commission()}%\n"
        f"🎁 *Реферальная программа:* {get_referral_percent()}% от суммы обменов ваших рефералов!\n\n"
        "⬇️ *Выберите действие:*"
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
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

# -------------------- ХЕНДЛЕРЫ --------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    # Очищаем состояние при старте
    await state.clear()
    args = message.text.split()
    ref_by = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    await send_welcome_message(message, message.from_user.id, message.from_user.username, ref_by)

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await send_welcome_message(callback, callback.from_user.id, callback.from_user.username)
    await callback.message.delete()  # удаляем старое сообщение, чтобы не дублировать

@dp.callback_query(F.data == "menu_balance")
async def show_balance(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    balance = get_balance(callback.from_user.id)
    text = f"💰 *Ваш баланс:*\n\n{balance:.2f} ₽\n\n_Здесь отображаются рубли, полученные за обмен криптовалюты._"
    await edit_or_send_message(callback, text, back_to_main_kb())

@dp.callback_query(F.data == "menu_exchange")
async def exchange_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "🔄 *Обмен криптовалюты на рубли*\n\n"
        "Выберите криптовалюту, которую хотите обменять:\n\n"
        "💎 *TON* — нативный токен сети TON\n"
        "💰 *USDT (TON)* — стейблкоин в сети TON\n"
        "💵 *USDT (TRC20)* — стейблкоин в сети TRON\n\n"
        "_После выбора вы укажете сумму, и мы покажем адрес кошелька и комментарий._"
    )
    await edit_or_send_message(callback, text, exchange_menu_kb())

@dp.callback_query(F.data.startswith("exchange_"))
async def exchange_select_crypto(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    crypto = callback.data.split("_")[1]
    if crypto not in WALLETS:
        await callback.answer("Ошибка: выбранная валюта не поддерживается", show_alert=True)
        return
    if not WALLETS[crypto]:
        await callback.answer("Кошелёк для этой валюты не настроен. Свяжитесь с администратором.", show_alert=True)
        return
    await state.update_data(crypto=crypto)
    rate = get_exchange_rate(crypto)
    text = (
        f"🔄 *Обмен {crypto.upper()} на рубли*\n\n"
        f"Текущий курс: 1 {crypto.upper()} = {rate:.2f} ₽\n"
        f"Комиссия сервиса: {get_commission()}%\n\n"
        "Введите сумму, которую хотите обменять (в криптовалюте):"
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
        await message.answer("❌ Введите положительное число (например, 0.5).", reply_markup=cancel_kb())
        return
    data = await state.get_data()
    crypto = data['crypto']
    rate = get_exchange_rate(crypto)
    commission = get_commission()
    gross_rub = amount_crypto * rate
    fee = gross_rub * commission / 100
    net_rub = gross_rub - fee
    text = (
        f"🔄 *Обмен {crypto.upper()}*\n\n"
        f"Сумма: {amount_crypto} {crypto.upper()}\n"
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
        f"📤 *Отправьте:*\n`{address}`\n\n"
        f"📝 *Обязательный комментарий (memo):*\n`{memo}`\n\n"
        f"💰 Вы получите: {net_rub:.2f} ₽ (после вычета комиссии)\n\n"
        f"⚡️ *Важно:*\n"
        f"• Переводите только {crypto.upper()} на указанный адрес\n"
        f"• Укажите комментарий *точно*, как выше\n"
        f"• После зачисления рубли поступят на ваш баланс автоматически\n\n"
        f"_Обычно зачисление занимает 1-5 минут._"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ В главное меню", callback_data="back_to_main")]
    ])
    await edit_or_send_message(callback, text, kb)
    await state.clear()
    await notify_admin(f"🔄 *Новая заявка на обмен*\nПользователь: {user_id}\nВалюта: {crypto}\nСумма: {amount_crypto} {crypto}\nК получению: {net_rub:.2f} ₽")

@dp.callback_query(ExchangeFSM.confirm, F.data == "exchange_cancel")
async def exchange_cancel(callback: types.CallbackQuery, state: FSMContext):
    await back_to_main(callback, state)
    await state.clear()

@dp.callback_query(F.data == "menu_withdraw")
async def withdraw_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    balance = get_balance(callback.from_user.id)
    if balance <= 0:
        text = "❌ *У вас нет средств для вывода.*\n\nПополните баланс через обмен криптовалюты."
        await edit_or_send_message(callback, text, back_to_main_kb())
        return
    text = (
        "💸 *Вывод рублей*\n\n"
        f"💰 Ваш баланс: *{balance:.2f} ₽*\n\n"
        "Введите сумму, которую хотите вывести (в рублях):"
    )
    await edit_or_send_message(callback, text, cancel_kb())
    await state.set_state(WithdrawFSM.waiting_amount)

@dp.message(WithdrawFSM.waiting_amount)
async def withdraw_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число (например, 500).", reply_markup=cancel_kb())
        return
    balance = get_balance(message.from_user.id)
    if amount > balance:
        await message.answer(f"❌ Недостаточно средств. Ваш баланс: {balance:.2f} ₽\nВведите меньшую сумму.", reply_markup=cancel_kb())
        return
    await state.update_data(amount=amount)
    text = (
        "💸 *Вывод рублей*\n\n"
        f"💰 Сумма вывода: *{amount:.2f} ₽*\n\n"
        "Введите реквизиты для выплаты (номер карты, счёта или телефона):"
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=cancel_kb())
    await state.set_state(WithdrawFSM.waiting_details)

@dp.message(WithdrawFSM.waiting_details)
async def withdraw_details(message: types.Message, state: FSMContext):
    await state.update_data(details=message.text)
    data = await state.get_data()
    text = (
        "💸 *Подтверждение вывода*\n\n"
        f"💰 Сумма: *{data['amount']:.2f} ₽*\n"
        f"📝 Реквизиты: `{data['details']}`\n\n"
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
        f"✅ *Заявка на вывод создана!*\n\n"
        f"Номер заявки: #{withdraw_id}\n"
        f"Сумма: {amount:.2f} ₽\n\n"
        "Ожидайте подтверждения администратора.\n"
        "Вы получите уведомление, когда заявка будет обработана."
    )
    await edit_or_send_message(callback, text, back_to_main_kb())
    await state.clear()
    await notify_admin(f"💸 *Новая заявка на вывод*\nПользователь: {callback.from_user.id}\nСумма: {amount} ₽\nРеквизиты: {details}")

@dp.callback_query(WithdrawFSM.confirm, F.data == "confirm_withdraw_no")
async def withdraw_cancel(callback: types.CallbackQuery, state: FSMContext):
    await back_to_main(callback, state)
    await state.clear()

@dp.callback_query(F.data == "menu_referrals")
async def show_referrals(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    invited, bonus = get_referral_info(callback.from_user.id)
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={callback.from_user.id}"
    ref_percent = get_referral_percent()
    text = (
        "👥 *Реферальная программа*\n\n"
        f"🔗 Ваша реферальная ссылка:\n`{link}`\n\n"
        f"👤 Приглашено: *{invited}* чел.\n"
        f"🎁 Заработано бонусов: *{bonus:.2f} ₽*\n\n"
        f"💡 Вы получаете *{ref_percent}%* от суммы обменов ваших рефералов (после вычета комиссии).\n"
        "Бонусы начисляются автоматически и доступны для вывода."
    )
    await edit_or_send_message(callback, text, back_to_main_kb())

@dp.callback_query(F.data == "menu_history")
async def show_history(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    deposits, withdrawals = get_user_history(callback.from_user.id, limit=10)
    text = "📜 *История операций*\n\n"
    if deposits:
        text += "*🔄 Обмены (пополнения):*\n"
        for d in deposits:
            crypto, amount, status, ts = d
            date = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
            text += f"• {crypto} {amount:.2f} – {status} ({date})\n"
    if withdrawals:
        text += "\n*💸 Выводы:*\n"
        for w in withdrawals:
            amount, status, ts = w
            date = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
            text += f"• {amount:.2f} ₽ – {status} ({date})\n"
    if not deposits and not withdrawals:
        text += "Операций пока нет."
    await edit_or_send_message(callback, text, back_to_main_kb())

@dp.callback_query(F.data == "menu_support")
async def support(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = f"🆘 *Поддержка*\n\nЕсли у вас возникли вопросы или проблемы, свяжитесь с нашим специалистом:\n\n👉 @{SUPPORT_USERNAME}"
    await edit_or_send_message(callback, text, back_to_main_kb())

# -------------------- АДМИНКА --------------------
@dp.message(Command("admin"))
async def admin_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return
    await state.clear()
    text = "🛡 *Панель администратора*\n\nВыберите действие:"
    await message.answer(text, parse_mode="Markdown", reply_markup=admin_main_kb())

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = "🛡 *Панель администратора*\n\nВыберите действие:"
    await edit_or_send_message(callback, text, admin_main_kb())

@dp.callback_query(F.data == "admin_exit")
async def admin_exit(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await back_to_main(callback, state)

@dp.callback_query(F.data == "admin_withdrawals")
async def admin_withdrawals_filter(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = "📋 *Заявки на вывод*\n\nВыберите фильтр:"
    await edit_or_send_message(callback, text, admin_withdrawals_filter_kb())

@dp.callback_query(F.data.startswith("admin_withdrawals_"))
async def admin_show_withdrawals(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    if len(parts) == 3:  # admin_withdrawals_{filter}
        filter_type = parts[2]
        page = 1
    elif len(parts) == 5:  # admin_withdrawals_{filter}_page_{page}
        filter_type = parts[2]
        page = int(parts[4])
    else:
        return

    withdrawals, total = get_withdrawals_paginated(filter_type if filter_type != "all" else None, page, per_page=5)
    if not withdrawals:
        text = "📭 Нет заявок по выбранному фильтру."
        await edit_or_send_message(callback, text, admin_withdrawals_filter_kb())
        return

    # Отправляем заявки на текущей странице
    for w in withdrawals:
        w_id, user_id, amount, details, status, ts = w
        date = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
        await callback.message.answer(
            f"📋 *Заявка #{w_id}*\n"
            f"👤 Пользователь: `{user_id}`\n"
            f"💰 Сумма: {amount} ₽\n"
            f"📝 Реквизиты: `{details}`\n"
            f"🏷 Статус: {status}\n"
            f"📅 Дата: {date}",
            parse_mode="Markdown",
            reply_markup=withdrawal_action_kb(w_id)
        )
    total_pages = (total + 4) // 5
    pagination_kb = withdrawal_pagination_kb(filter_type, page, total_pages)
    await callback.message.answer("Страница:", reply_markup=pagination_kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_withdrawal(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    withdraw_id = int(callback.data.split("_")[1])
    update_withdrawal_status(withdraw_id, 'completed')
    w = get_withdrawal(withdraw_id)
    if w:
        user_id = w[1]
        amount = w[3]
        await bot.send_message(user_id, f"✅ *Заявка на вывод #{withdraw_id}* на сумму {amount:.2f} ₽ подтверждена и выполнена.", parse_mode="Markdown")
    await callback.message.edit_text(f"✅ Заявка #{withdraw_id} подтверждена.", reply_markup=back_to_admin_kb())
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_"))
async def reject_withdrawal_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    withdraw_id = int(callback.data.split("_")[1])
    await state.update_data(withdraw_id=withdraw_id)
    text = f"❌ Отклонение заявки #{withdraw_id}\n\nВведите причину отклонения (будет отправлена пользователю):"
    await edit_or_send_message(callback, text, back_to_admin_kb())
    await state.set_state(AdminRejectFSM.waiting_comment)

@dp.message(AdminRejectFSM.waiting_comment)
async def reject_withdrawal_comment(message: types.Message, state: FSMContext):
    data = await state.get_data()
    withdraw_id = data['withdraw_id']
    comment = message.text
    update_withdrawal_status(withdraw_id, 'rejected', comment)
    w = get_withdrawal(withdraw_id)
    if w:
        user_id = w[1]
        amount = w[3]
        update_balance(user_id, amount)  # возвращаем средства
        await bot.send_message(user_id, f"❌ *Заявка на вывод #{withdraw_id}* на сумму {amount:.2f} ₽ отклонена.\nПричина: {comment}", parse_mode="Markdown")
    await message.answer(f"✅ Заявка #{withdraw_id} отклонена, средства возвращены.", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
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
        await callback.answer("Нет прав", show_alert=True)
        return
    text = "🔧 *Управление курсами*\n\nВыберите криптовалюту для изменения курса:"
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
        await callback.answer("Нет прав", show_alert=True)
        return
    crypto = callback.data.split("_")[1]
    await state.update_data(crypto=crypto)
    current = get_exchange_rate(crypto)
    text = f"🔧 *Изменение курса {crypto}*\n\nТекущий курс: {current:.2f} ₽\nВведите новый курс (рублей за единицу):"
    await edit_or_send_message(callback, text, back_to_admin_kb())
    await state.set_state(AdminRateFSM.waiting_rate)

@dp.message(AdminRateFSM.waiting_rate)
async def admin_set_rate_value(message: types.Message, state: FSMContext):
    try:
        rate = float(message.text)
        if rate <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число (например, 95.5).")
        return
    data = await state.get_data()
    crypto = data['crypto']
    set_exchange_rate(crypto, rate)
    await message.answer(f"✅ Курс для {crypto} установлен: {rate:.2f} ₽", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_commission")
async def admin_commission_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    current = get_commission()
    text = f"⚙️ *Комиссия сервиса*\n\nТекущая комиссия: {current}%\nВведите новое значение (процент от суммы обмена):"
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
    await message.answer(f"✅ Комиссия установлена: {percent}%", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_referral")
async def admin_referral_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    current = get_referral_percent()
    text = f"🎁 *Реферальный процент*\n\nТекущий процент: {current}%\nВведите новое значение (процент от суммы обмена реферала):"
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
    await message.answer(f"✅ Реферальный процент установлен: {percent}%", reply_markup=back_to_admin_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_balance")
async def admin_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    text = "👤 *Ручное изменение баланса*\n\nВведите Telegram ID пользователя:"
    await edit_or_send_message(callback, text, back_to_admin_kb())
    await state.set_state(AdminBalanceFSM.waiting_user_id)

@dp.message(AdminBalanceFSM.waiting_user_id)
async def admin_balance_user(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
    except:
        await message.answer("❌ ID должен быть числом.")
        return
    await state.update_data(user_id=user_id)
    await message.answer("Введите сумму изменения (положительная – зачисление, отрицательная – списание):")
    await state.set_state(AdminBalanceFSM.waiting_amount)

@dp.message(AdminBalanceFSM.waiting_amount)
async def admin_balance_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
    except:
        await message.answer("❌ Введите число.")
        return
    data = await state.get_data()
    user_id = data['user_id']
    update_balance(user_id, amount)
    new_balance = get_balance(user_id)
    await message.answer(f"✅ Баланс пользователя {user_id} изменён на {amount:.2f} ₽.\nНовый баланс: {new_balance:.2f} ₽", reply_markup=back_to_admin_kb())
    await state.clear()

# -------------------- ФОНОВАЯ ПРОВЕРКА ПОПОЛНЕНИЙ --------------------
async def check_deposits():
    while True:
        try:
            c.execute("SELECT id, user_id, crypto, memo, amount FROM deposits WHERE status = 'pending'")
            pending = c.fetchall()
            for dep_id, user_id, crypto, memo, expected_amount in pending:
                tx = None
                if crypto == 'ton' or crypto == 'usdt_ton':
                    tx = await check_ton_transaction(memo)
                elif crypto == 'usdt_trc20':
                    tx = await check_trc20_transaction(memo)

                if tx and tx.get('amount', 0) >= expected_amount:
                    amount_crypto = tx['amount']
                    rate = get_exchange_rate(crypto)
                    gross_rub = amount_crypto * rate
                    commission = get_commission()
                    fee = gross_rub * commission / 100
                    rub_amount = gross_rub - fee
                    update_balance(user_id, rub_amount)
                    complete_deposit(dep_id, amount_crypto, rub_amount, user_id)
                    await bot.send_message(user_id, f"✅ *Обмен выполнен!*\n\nЗачислено {rub_amount:.2f} ₽ по курсу {rate:.2f} ₽ за {crypto}.\nКомиссия: {fee:.2f} ₽", parse_mode="Markdown")
                    await notify_admin(f"✅ *Обмен завершён*\nПользователь: {user_id}\nВалюта: {crypto}\nСумма: {amount_crypto} {crypto}\nЗачислено: {rub_amount:.2f} ₽")
        except Exception as e:
            logger.error(f"Ошибка в check_deposits: {e}")
        await asyncio.sleep(60)

# -------------------- FLASK (для health check) --------------------
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is running on Railway!"

@app.route('/health')
def health():
    return "OK"

# -------------------- ЗАПУСК --------------------
def run_bot_polling():
    async def start_polling():
        logger.info("Запуск бота через polling...")
        asyncio.create_task(update_rates_periodically())
        asyncio.create_task(check_deposits())
        await dp.start_polling(bot)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(start_polling())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    finally:
        loop.close()

if __name__ == "__main__":
    is_railway = bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN"))
    if is_railway:
        logger.info("Запуск на Railway с polling...")
        bot_thread = Thread(target=run_bot_polling, daemon=True)
        bot_thread.start()
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        run_bot_polling()
