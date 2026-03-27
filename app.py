import os
import asyncio
import time
import aiohttp
import asyncpg
from threading import Thread
from flask import Flask, request, jsonify
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import logging

# -------------------- КОНФИГ --------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_IDS", "").split(",") if id.strip()]

if not BOT_TOKEN:
    logging.error("BOT_TOKEN не установлен!")
    exit(1)

# Кошельки
WALLETS = {
    'ton': os.environ.get("WALLET_TON", "EQD..."),
    'usdt_ton': os.environ.get("WALLET_USDT_TON", "EQD..."),
    'usdt_trc20': os.environ.get("WALLET_USDT_TRC20", "T..."),
}

# Настройки
REFERRAL_PERCENT = 1  # 1% реферальный бонус
COMMISSION_PERCENT = 3  # 3% комиссия (забираем себе)

# Курсы валют (обновляются каждые 30 секунд)
exchange_rates = {
    'ton': 0,
    'usdt': 0,
    'last_update': 0
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------- ПОДКЛЮЧЕНИЕ К POSTGRESQL --------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    logger.error("DATABASE_URL не установлен! Добавьте PostgreSQL в Railway.")
    exit(1)

db_pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    
    async with db_pool.acquire() as conn:
        # Пользователи
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                user_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                balance REAL DEFAULT 0,
                ref_by BIGINT DEFAULT 0,
                ref_bonus REAL DEFAULT 0,
                created_at INTEGER
            )
        ''')
        # Заявки на пополнение
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS deposits (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                crypto TEXT NOT NULL,
                memo TEXT UNIQUE NOT NULL,
                amount REAL DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at INTEGER
            )
        ''')
        # Заявки на вывод
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS withdrawals (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount REAL NOT NULL,
                details TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                admin_comment TEXT,
                created_at INTEGER,
                processed_at INTEGER
            )
        ''')
        # Реферальные начисления
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS referral_earnings (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                from_user_id BIGINT NOT NULL,
                amount REAL NOT NULL,
                created_at INTEGER
            )
        ''')
        
        logger.info("База данных PostgreSQL инициализирована")

async def get_user(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)

async def create_user(user_id: int, username: str, ref_by: int = None):
    async with db_pool.acquire() as conn:
        existing = await conn.fetchval("SELECT user_id FROM users WHERE user_id = $1", user_id)
        if existing:
            return
        await conn.execute(
            "INSERT INTO users (user_id, username, ref_by, created_at) VALUES ($1, $2, $3, $4)",
            user_id, username, ref_by, int(time.time())
        )

async def update_balance(user_id: int, amount: float):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)

async def get_balance(user_id: int) -> float:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
        return row['balance'] if row else 0

async def add_deposit(user_id: int, crypto: str, memo: str):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO deposits (user_id, crypto, memo, status, created_at) VALUES ($1, $2, $3, $4, $5) RETURNING id",
            user_id, crypto, memo, 'pending', int(time.time())
        )

async def complete_deposit(deposit_id: int, amount_crypto: float, rub_amount: float, user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE deposits SET amount = $1, status = 'completed' WHERE id = $2", amount_crypto, deposit_id)
        
        # Реферальный бонус
        ref_by = await conn.fetchval("SELECT ref_by FROM users WHERE user_id = $1", user_id)
        if ref_by and ref_by > 0:
            bonus = rub_amount * REFERRAL_PERCENT / 100
            if bonus > 0:
                await conn.execute("UPDATE users SET balance = balance + $1, ref_bonus = ref_bonus + $1 WHERE user_id = $2", bonus, ref_by)
                await conn.execute(
                    "INSERT INTO referral_earnings (user_id, from_user_id, amount, created_at) VALUES ($1, $2, $3, $4)",
                    ref_by, user_id, bonus, int(time.time())
                )

async def add_withdrawal(user_id: int, amount: float, details: str) -> int:
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO withdrawals (user_id, amount, details, status, created_at) VALUES ($1, $2, $3, $4, $5) RETURNING id",
            user_id, amount, details, 'pending', int(time.time())
        )

async def get_withdrawal(withdraw_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM withdrawals WHERE id = $1", withdraw_id)

async def update_withdrawal_status(withdraw_id: int, status: str, admin_comment: str = None):
    async with db_pool.acquire() as conn:
        if admin_comment:
            await conn.execute(
                "UPDATE withdrawals SET status = $1, admin_comment = $2, processed_at = $3 WHERE id = $4",
                status, admin_comment, int(time.time()), withdraw_id
            )
        else:
            await conn.execute(
                "UPDATE withdrawals SET status = $1, processed_at = $2 WHERE id = $3",
                status, int(time.time()), withdraw_id
            )

async def get_withdrawals_by_status(status: str = None):
    async with db_pool.acquire() as conn:
        if status == 'pending':
            return await conn.fetch("SELECT id, user_id, amount, details, status, created_at FROM withdrawals WHERE status = 'pending' ORDER BY created_at DESC")
        elif status == 'completed':
            return await conn.fetch("SELECT id, user_id, amount, details, status, created_at FROM withdrawals WHERE status = 'completed' ORDER BY created_at DESC")
        elif status == 'rejected':
            return await conn.fetch("SELECT id, user_id, amount, details, status, created_at FROM withdrawals WHERE status = 'rejected' ORDER BY created_at DESC")
        else:
            return await conn.fetch("SELECT id, user_id, amount, details, status, created_at FROM withdrawals ORDER BY created_at DESC")

async def get_statistics():
    async with db_pool.acquire() as conn:
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_deposits = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM deposits WHERE status = 'completed'")
        total_withdrawals = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE status = 'completed'")
        referred_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE ref_by IS NOT NULL AND ref_by > 0")
        total_ref_bonus = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM referral_earnings")
        return users_count, total_deposits, total_withdrawals, referred_count, total_ref_bonus

async def get_user_history(user_id: int, limit: int = 10):
    async with db_pool.acquire() as conn:
        deposits = await conn.fetch(
            "SELECT crypto, amount, status, created_at FROM deposits WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
            user_id, limit
        )
        withdrawals = await conn.fetch(
            "SELECT amount, status, created_at FROM withdrawals WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
            user_id, limit
        )
        return deposits, withdrawals

async def get_referral_info(user_id: int):
    async with db_pool.acquire() as conn:
        invited_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE ref_by = $1", user_id)
        bonus = await conn.fetchval("SELECT ref_bonus FROM users WHERE user_id = $1", user_id) or 0
        return invited_count, bonus

# -------------------- ФУНКЦИИ ДЛЯ КУРСА ВАЛЮТ --------------------
async def fetch_exchange_rates():
    """Получает курсы TON/USDT и USDT/RUB с Binance"""
    global exchange_rates
    try:
        async with aiohttp.ClientSession() as session:
            # Получаем TON/USDT
            async with session.get("https://api.binance.com/api/v3/ticker/price?symbol=TONUSDT") as resp:
                data = await resp.json()
                ton_usdt = float(data['price'])
            
            # Получаем USDT/RUB (через P2P или другой API, используем курс RUB)
            # Binance не даёт USDT/RUB напрямую, используем примерный курс или другой API
            # Для простоты используем фиксированный курс + динамику через другой источник
            # Здесь используем Binance API для BUSD/RUB как пример
            async with session.get("https://api.binance.com/api/v3/ticker/price?symbol=BUSDRUB") as resp:
                data = await resp.json()
                usdt_rub = float(data['price'])
            
            exchange_rates['ton'] = ton_usdt * usdt_rub  # TON в рублях
            exchange_rates['usdt'] = usdt_rub           # USDT в рублях
            exchange_rates['last_update'] = int(time.time())
            
            logger.info(f"Курсы обновлены: TON = {exchange_rates['ton']:.2f} ₽, USDT = {exchange_rates['usdt']:.2f} ₽")
    except Exception as e:
        logger.error(f"Ошибка получения курсов: {e}")
        # Если не получили курс, оставляем старые значения

async def start_rate_updater():
    """Фоновая задача обновления курсов каждые 30 секунд"""
    while True:
        await fetch_exchange_rates()
        await asyncio.sleep(30)

def get_rate_with_commission(crypto: str) -> float:
    """Возвращает курс покупки с вычетом комиссии 3%"""
    if 'ton' in crypto.lower():
        market_rate = exchange_rates.get('ton', 500)
    else:
        market_rate = exchange_rates.get('usdt', 85)
    
    # Применяем комиссию 3% (забираем себе)
    return market_rate * (1 - COMMISSION_PERCENT / 100)

# -------------------- КЛАВИАТУРЫ --------------------
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Баланс", callback_data="menu_balance")],
        [InlineKeyboardButton(text="🔄 Обмен", callback_data="menu_exchange")],
        [InlineKeyboardButton(text="💸 Вывести", callback_data="menu_withdraw")],
        [InlineKeyboardButton(text="👥 Рефералы", callback_data="menu_referrals")],
        [InlineKeyboardButton(text="📜 История", callback_data="menu_history")]
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

def confirm_withdraw_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, подтвердить", callback_data="confirm_withdraw_yes"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="confirm_withdraw_no")]
    ])

# -------------------- АДМИНКА --------------------
def admin_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Заявки на вывод", callback_data="admin_withdrawals")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👤 Изменить баланс", callback_data="admin_balance")],
        [InlineKeyboardButton(text="📈 Текущий курс", callback_data="admin_rates")],
        [InlineKeyboardButton(text="◀️ Выход", callback_data="admin_exit")]
    ])

def admin_back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])

def withdrawal_action_kb(withdraw_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_{withdraw_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{withdraw_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])

def admin_withdrawals_filter_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Все", callback_data="admin_withdrawals_all"),
         InlineKeyboardButton(text="🟡 Новые", callback_data="admin_withdrawals_pending")],
        [InlineKeyboardButton(text="✅ Выполненные", callback_data="admin_withdrawals_completed"),
         InlineKeyboardButton(text="❌ Отклонённые", callback_data="admin_withdrawals_rejected")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])

# -------------------- FSM --------------------
class WithdrawFSM(StatesGroup):
    waiting_amount = State()
    waiting_details = State()
    confirm = State()

class AdminBalanceFSM(StatesGroup):
    waiting_user_id = State()
    waiting_amount = State()

class AdminRejectFSM(StatesGroup):
    waiting_comment = State()

# -------------------- БОТ --------------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# -------------------- ХЕНДЛЕРЫ --------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    args = message.text.split()
    ref_by = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    await create_user(message.from_user.id, message.from_user.username, ref_by)
    
    ton_rate = get_rate_with_commission('ton')
    usdt_rate = get_rate_with_commission('usdt')
    
    text = (
        f"✨ *Добро пожаловать в CryptoExchangeBot!* ✨\n\n"
        f"💱 *Актуальные курсы обмена (с комиссией {COMMISSION_PERCENT}%):*\n"
        f"💎 TON: *{ton_rate:.2f} ₽*\n"
        f"💰 USDT: *{usdt_rate:.2f} ₽*\n\n"
        f"📌 *Как это работает:*\n"
        f"1️⃣ Выберите криптовалюту (TON, USDT TON или USDT TRC20)\n"
        f"2️⃣ Отправьте криптовалюту на указанный кошелёк с комментарием (memo)\n"
        f"3️⃣ После зачисления рубли поступят на ваш баланс\n"
        f"4️⃣ Вы можете вывести рубли на карту или счёт\n\n"
        f"🎁 *Реферальная программа:* Приглашайте друзей и получайте {REFERRAL_PERCENT}% от суммы их обменов!\n\n"
        f"⬇️ *Выберите действие:*"
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=main_menu_kb())

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery):
    ton_rate = get_rate_with_commission('ton')
    usdt_rate = get_rate_with_commission('usdt')
    text = (
        f"✨ *Главное меню* ✨\n\n"
        f"💱 *Текущие курсы:*\n"
        f"💎 TON: *{ton_rate:.2f} ₽*\n"
        f"💰 USDT: *{usdt_rate:.2f} ₽*\n\n"
        f"⬇️ *Выберите действие:*"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())
    await callback.answer()

@dp.callback_query(F.data == "menu_balance")
async def show_balance(callback: types.CallbackQuery):
    balance = await get_balance(callback.from_user.id)
    text = f"💰 *Ваш баланс:*\n\n{balance:.2f} ₽\n\n_Средства, полученные за обмен криптовалюты._"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_to_main_kb())
    await callback.answer()

@dp.callback_query(F.data == "menu_exchange")
async def exchange_menu(callback: types.CallbackQuery):
    ton_rate = get_rate_with_commission('ton')
    usdt_rate = get_rate_with_commission('usdt')
    text = (
        "🔄 *Обмен криптовалюты на рубли*\n\n"
        f"💎 *TON* — {ton_rate:.2f} ₽\n"
        f"💰 *USDT (TON)* — {usdt_rate:.2f} ₽\n"
        f"💵 *USDT (TRC20)* — {usdt_rate:.2f} ₽\n\n"
        f"⚡️ *Комиссия:* {COMMISSION_PERCENT}% за обмен\n\n"
        f"Выберите криптовалюту для обмена:"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=exchange_menu_kb())
    await callback.answer()

@dp.callback_query(F.data.startswith("exchange_"))
async def process_exchange(callback: types.CallbackQuery):
    crypto = callback.data.split("_")[1]
    user_id = callback.from_user.id
    memo = f"dep_{user_id}_{int(time.time())}"
    address = WALLETS[crypto]
    await add_deposit(user_id, crypto, memo)
    
    if crypto == 'ton':
        name = "TON"
        rate = get_rate_with_commission('ton')
    else:
        name = "USDT"
        rate = get_rate_with_commission('usdt')

    text = (
        f"🔄 *Обмен {name} на рубли*\n\n"
        f"📤 *Отправьте:*\n`{address}`\n\n"
        f"📝 *Обязательный комментарий (memo):*\n`{memo}`\n\n"
        f"💱 *Курс обмена:* 1 {name} = {rate:.2f} ₽ (включая комиссию {COMMISSION_PERCENT}%)\n\n"
        f"⚡️ *Важно:*\n"
        f"• Переводите только {name} на указанный адрес\n"
        f"• Укажите комментарий *точно*, как выше\n"
        f"• После зачисления рубли поступят на баланс автоматически\n\n"
        f"_Обычно зачисление занимает 1-5 минут._"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_exchange")]
    ])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "back_to_exchange")
async def back_to_exchange(callback: types.CallbackQuery):
    await exchange_menu(callback)

@dp.callback_query(F.data == "menu_withdraw")
async def withdraw_start(callback: types.CallbackQuery, state: FSMContext):
    balance = await get_balance(callback.from_user.id)
    if balance <= 0:
        text = "❌ *У вас нет средств для вывода.*\n\nПополните баланс через обмен криптовалюты."
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_to_main_kb())
        await callback.answer()
        return
    text = (
        "💸 *Вывод рублей*\n\n"
        f"💰 Ваш баланс: *{balance:.2f} ₽*\n\n"
        "Введите сумму, которую хотите вывести (в рублях):"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_to_main_kb())
    await state.set_state(WithdrawFSM.waiting_amount)
    await callback.answer()

@dp.message(WithdrawFSM.waiting_amount)
async def withdraw_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
    except:
        await message.answer("❌ Введите число (например, 500).")
        return
    balance = await get_balance(message.from_user.id)
    if amount > balance:
        await message.answer(f"❌ Недостаточно средств. Ваш баланс: {balance:.2f} ₽\nВведите меньшую сумму.")
        return
    await state.update_data(amount=amount)
    text = (
        "💸 *Вывод рублей*\n\n"
        f"💰 Сумма вывода: *{amount:.2f} ₽*\n\n"
        "Введите реквизиты для выплаты (номер карты, счёта или телефона):"
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=back_to_main_kb())
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
    await update_balance(callback.from_user.id, -amount)
    withdraw_id = await add_withdrawal(callback.from_user.id, amount, details)
    text = (
        f"✅ *Заявка на вывод создана!*\n\n"
        f"Номер заявки: #{withdraw_id}\n"
        f"Сумма: {amount:.2f} ₽\n\n"
        "Ожидайте подтверждения администратора."
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_to_main_kb())
    await state.clear()
    await callback.answer()

@dp.callback_query(WithdrawFSM.confirm, F.data == "confirm_withdraw_no")
async def withdraw_cancel(callback: types.CallbackQuery, state: FSMContext):
    text = "❌ Вывод отменён."
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_to_main_kb())
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data == "menu_referrals")
async def show_referrals(callback: types.CallbackQuery):
    invited, bonus = await get_referral_info(callback.from_user.id)
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={callback.from_user.id}"
    text = (
        "👥 *Реферальная программа*\n\n"
        f"🔗 Ваша реферальная ссылка:\n`{link}`\n\n"
        f"👤 Приглашено: *{invited}* чел.\n"
        f"🎁 Заработано бонусов: *{bonus:.2f} ₽*\n\n"
        f"💡 Вы получаете *{REFERRAL_PERCENT}%* от суммы обменов ваших рефералов.\n"
        "Бонусы начисляются автоматически и доступны для вывода."
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_to_main_kb())
    await callback.answer()

@dp.callback_query(F.data == "menu_history")
async def show_history(callback: types.CallbackQuery):
    deposits, withdrawals = await get_user_history(callback.from_user.id, 10)
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
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_to_main_kb())
    await callback.answer()

# -------------------- АДМИНКА --------------------
@dp.message(Command("admin"))
async def admin_start(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return
    text = "🛡 *Панель администратора*\n\nВыберите действие:"
    await message.answer(text, parse_mode="Markdown", reply_markup=admin_main_kb())

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    text = "🛡 *Панель администратора*\n\nВыберите действие:"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_main_kb())
    await callback.answer()

@dp.callback_query(F.data == "admin_exit")
async def admin_exit(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    await back_to_main(callback)

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    users_count, total_deposits, total_withdrawals, referred_count, total_ref_bonus = await get_statistics()
    text = (
        "📊 *Статистика*\n\n"
        f"👥 Пользователей: {users_count}\n"
        f"👥 Приглашённых: {referred_count}\n"
        f"💰 Всего пополнений (обменов): {total_deposits:.2f} ₽\n"
        f"💸 Всего выводов: {total_withdrawals:.2f} ₽\n"
        f"💵 В обороте: {total_deposits - total_withdrawals:.2f} ₽\n"
        f"🎁 Выплачено реферальных бонусов: {total_ref_bonus:.2f} ₽"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_back_kb())
    await callback.answer()

@dp.callback_query(F.data == "admin_rates")
async def admin_rates(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    ton_market = exchange_rates.get('ton', 0)
    usdt_market = exchange_rates.get('usdt', 0)
    ton_buy = ton_market * (1 - COMMISSION_PERCENT / 100)
    usdt_buy = usdt_market * (1 - COMMISSION_PERCENT / 100)
    text = (
        "📈 *Текущие курсы*\n\n"
        f"💎 *TON:*\n"
        f"  Рыночный: {ton_market:.2f} ₽\n"
        f"  Покупка: {ton_buy:.2f} ₽ (комиссия {COMMISSION_PERCENT}%)\n\n"
        f"💰 *USDT:*\n"
        f"  Рыночный: {usdt_market:.2f} ₽\n"
        f"  Покупка: {usdt_buy:.2f} ₽ (комиссия {COMMISSION_PERCENT}%)\n\n"
        f"🔄 Обновление: каждые 30 секунд"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_back_kb())
    await callback.answer()

@dp.callback_query(F.data == "admin_withdrawals")
async def admin_withdrawals_menu(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    text = "📋 *Заявки на вывод*\n\nВыберите фильтр:"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_withdrawals_filter_kb())
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_withdrawals_"))
async def admin_show_withdrawals(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    filter_type = callback.data.split("_")[2]
    if filter_type == "all":
        withdrawals = await get_withdrawals_by_status()
    elif filter_type == "pending":
        withdrawals = await get_withdrawals_by_status('pending')
    elif filter_type == "completed":
        withdrawals = await get_withdrawals_by_status('completed')
    elif filter_type == "rejected":
        withdrawals = await get_withdrawals_by_status('rejected')
    else:
        withdrawals = []
    
    if not withdrawals:
        text = "📭 Нет заявок по выбранному фильтру."
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_withdrawals_filter_kb())
        await callback.answer()
        return
    
    # Показываем все заявки (можно доработать с пагинацией)
    for w in withdrawals:
        w_id, user_id, amount, details, status, ts = w
        date = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
        text = (
            f"📋 *Заявка #{w_id}*\n"
            f"👤 Пользователь: `{user_id}`\n"
            f"💰 Сумма: {amount:.2f} ₽\n"
            f"📝 Реквизиты: `{details}`\n"
            f"📅 Дата: {date}\n"
            f"🔖 Статус: {status}"
        )
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=withdrawal_action_kb(w_id))
    await callback.message.delete()
    await callback.answer()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_withdrawal(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    withdraw_id = int(callback.data.split("_")[1])
    await update_withdrawal_status(withdraw_id, 'completed')
    w = await get_withdrawal(withdraw_id)
    if w:
        user_id = w['user_id']
        amount = w['amount']
        await bot.send_message(user_id, f"✅ *Заявка на вывод #{withdraw_id}* на сумму {amount:.2f} ₽ подтверждена и выполнена.", parse_mode="Markdown")
    await callback.message.edit_text(f"✅ Заявка #{withdraw_id} подтверждена.", reply_markup=admin_back_kb())
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_"))
async def reject_withdrawal_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    withdraw_id = int(callback.data.split("_")[1])
    await state.update_data(withdraw_id=withdraw_id)
    text = f"❌ Отклонение заявки #{withdraw_id}\n\nВведите причину отклонения:"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_back_kb())
    await state.set_state(AdminRejectFSM.waiting_comment)
    await callback.answer()

@dp.message(AdminRejectFSM.waiting_comment)
async def reject_withdrawal_comment(message: types.Message, state: FSMContext):
    data = await state.get_data()
    withdraw_id = data['withdraw_id']
    comment = message.text
    await update_withdrawal_status(withdraw_id, 'rejected', comment)
    w = await get_withdrawal(withdraw_id)
    if w:
        user_id = w['user_id']
        amount = w['amount']
        await update_balance(user_id, amount)  # возвращаем средства
        await bot.send_message(user_id, f"❌ *Заявка на вывод #{withdraw_id}* на сумму {amount:.2f} ₽ отклонена.\nПричина: {comment}", parse_mode="Markdown")
    await message.answer(f"✅ Заявка #{withdraw_id} отклонена, средства возвращены.", reply_markup=admin_back_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_balance")
async def admin_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    text = "👤 *Ручное изменение баланса*\n\nВведите Telegram ID пользователя:"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_back_kb())
    await state.set_state(AdminBalanceFSM.waiting_user_id)
    await callback.answer()

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
    await update_balance(user_id, amount)
    new_balance = await get_balance(user_id)
    await message.answer(f"✅ Баланс пользователя {user_id} изменён на {amount:.2f} ₽.\nНовый баланс: {new_balance:.2f} ₽", reply_markup=admin_back_kb())
    await state.clear()

# -------------------- ФОНОВАЯ ПРОВЕРКА ПОПОЛНЕНИЙ --------------------
async def check_ton_transaction(memo: str) -> dict:
    """Проверка транзакций TON (заглушка)"""
    # TODO: реализовать API запрос к TON Center
    return None

async def check_trc20_transaction(memo: str) -> dict:
    """Проверка транзакций TRC20 (заглушка)"""
    # TODO: реализовать API запрос к TronGrid
    return None

async def check_deposits():
    while True:
        try:
            async with db_pool.acquire() as conn:
                pending = await conn.fetch("SELECT id, user_id, crypto, memo FROM deposits WHERE status = 'pending'")
            
            for dep_id, user_id, crypto, memo in pending:
                tx = None
                if crypto == 'ton' or crypto == 'usdt_ton':
                    tx = await check_ton_transaction(memo)
                elif crypto == 'usdt_trc20':
                    tx = await check_trc20_transaction(memo)
                
                if tx and tx.get('amount', 0) > 0:
                    amount_crypto = tx['amount']
                    # Получаем курс с комиссией
                    if 'ton' in crypto:
                        rate = get_rate_with_commission('ton')
                    else:
                        rate = get_rate_with_commission('usdt')
                    rub_amount = amount_crypto * rate
                    await update_balance(user_id, rub_amount)
                    await complete_deposit(dep_id, amount_crypto, rub_amount, user_id)
                    await bot.send_message(user_id, f"✅ *Обмен выполнен!*\n\nЗачислено {rub_amount:.2f} ₽ по курсу {rate:.2f} ₽ за {crypto}.", parse_mode="Markdown")
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
async def main():
    await init_db()
    asyncio.create_task(start_rate_updater())
    asyncio.create_task(check_deposits())
    
    # Удаляем вебхук, используем polling
    await bot.delete_webhook()
    await dp.start_polling(bot)

def run_bot():
    asyncio.run(main())

if __name__ == "__main__":
    is_railway = bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN"))
    if is_railway:
        logger.info("Запуск на Railway с polling...")
        bot_thread = Thread(target=run_bot, daemon=True)
        bot_thread.start()
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        run_bot()
