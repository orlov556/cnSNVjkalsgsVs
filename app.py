import os
import asyncio
import sqlite3
import time
import aiohttp
from threading import Thread
from flask import Flask, request, jsonify
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import logging

# Включаем логирование для отладки
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------- КОНФИГ --------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_IDS", "").split(",") if id.strip()]

if not BOT_TOKEN:
    logger.error("BOT_TOKEN не установлен!")
    exit(1)

# Кошельки для приёма средств
WALLETS = {
    'ton': os.environ.get("WALLET_TON", "EQD..."),
    'usdt_ton': os.environ.get("WALLET_USDT_TON", "EQD..."),
    'usdt_trc20': os.environ.get("WALLET_USDT_TRC20", "T..."),
}

# -------------------- БАЗА ДАННЫХ --------------------
DB_PATH = os.environ.get("DATABASE_PATH", "exchange.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
c = conn.cursor()

c.execute('''CREATE TABLE IF NOT EXISTS users
             (id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE, username TEXT,
              balance REAL DEFAULT 0, ref_by INTEGER DEFAULT 0, created_at INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS deposits
             (id INTEGER PRIMARY KEY, user_id INTEGER, crypto TEXT, memo TEXT,
              amount REAL DEFAULT 0, status TEXT, created_at INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS withdrawals
             (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL,
              details TEXT, status TEXT, created_at INTEGER, processed_at INTEGER)''')
c.execute("CREATE INDEX IF NOT EXISTS idx_memo ON deposits (memo)")
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

def update_balance(user_id, amount):
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()

def get_balance(user_id):
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    return row[0] if row else 0

def add_deposit(user_id, crypto, memo):
    c.execute("INSERT INTO deposits (user_id, crypto, memo, status, created_at) VALUES (?, ?, ?, ?, ?)",
              (user_id, crypto, memo, 'pending', int(time.time())))
    conn.commit()
    return c.lastrowid

def complete_deposit(deposit_id, amount_crypto):
    c.execute("UPDATE deposits SET amount = ?, status = 'completed' WHERE id = ?", (amount_crypto, deposit_id))
    conn.commit()

def add_withdrawal(user_id, amount, details):
    c.execute("INSERT INTO withdrawals (user_id, amount, details, status, created_at) VALUES (?, ?, ?, ?, ?)",
              (user_id, amount, details, 'pending', int(time.time())))
    conn.commit()

def get_pending_withdrawals():
    c.execute("SELECT id, user_id, amount, details FROM withdrawals WHERE status = 'pending'")
    return c.fetchall()

def approve_withdrawal(withdraw_id):
    c.execute("UPDATE withdrawals SET status = 'completed', processed_at = ? WHERE id = ?",
              (int(time.time()), withdraw_id))
    conn.commit()

# -------------------- КЛАВИАТУРЫ --------------------
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="➕ Пополнить")],
        [KeyboardButton(text="💸 Вывести"), KeyboardButton(text="👥 Рефералы")],
    ],
    resize_keyboard=True
)

admin_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Заявки на вывод")],
        [KeyboardButton(text="🔙 Назад")],
    ],
    resize_keyboard=True
)

# -------------------- FSM --------------------
class WithdrawState(StatesGroup):
    amount = State()
    details = State()

# -------------------- ИНИЦИАЛИЗАЦИЯ БОТА --------------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# -------------------- ХЕНДЛЕРЫ --------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    args = message.text.split()
    ref_by = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    create_user(message.from_user.id, message.from_user.username, ref_by)
    await message.answer(
        f"Добро пожаловать, {message.from_user.first_name}!\n"
        "Обменник криптовалют на рубли.\n\n"
        "➕ Пополнить – отправьте крипту на кошелёк\n"
        "💸 Вывести – закажите выплату рублей\n"
        "👥 Рефералы – ваша реферальная ссылка",
        reply_markup=main_kb
    )

@dp.message(F.text == "💰 Баланс")
async def show_balance(message: types.Message):
    balance = get_balance(message.from_user.id)
    await message.answer(f"Ваш баланс: {balance:.2f} ₽")

@dp.message(F.text == "➕ Пополнить")
async def deposit_menu(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="deposit_ton")],
        [InlineKeyboardButton(text="💰 USDT (TON)", callback_data="deposit_usdt_ton")],
        [InlineKeyboardButton(text="💵 USDT (TRC20)", callback_data="deposit_usdt_trc20")],
    ])
    await message.answer("Выберите криптовалюту для пополнения:", reply_markup=kb)

@dp.callback_query(F.data.startswith("deposit_"))
async def process_deposit(callback: types.CallbackQuery):
    crypto_type = callback.data.split("_")[1]
    user_id = callback.from_user.id
    memo = f"dep_{user_id}_{int(time.time())}"
    address = WALLETS[crypto_type]
    add_deposit(user_id, crypto_type, memo)

    if crypto_type == 'ton':
        text = (f"Отправьте **TON** на адрес:\n`{address}`\n\n"
                f"**Обязательно** укажите комментарий (memo): `{memo}`\n\n"
                f"После зачисления сумма будет автоматически конвертирована в рубли и зачислена.")
    elif crypto_type == 'usdt_ton':
        text = (f"Отправьте **USDT (TON)** на адрес:\n`{address}`\n\n"
                f"**Обязательно** укажите комментарий (memo): `{memo}`\n\n"
                f"После зачисления средства будут зачислены в рублях.")
    else:
        text = (f"Отправьте **USDT (TRC20)** на адрес:\n`{address}`\n\n"
                f"**Обязательно** укажите комментарий (memo): `{memo}`\n\n"
                f"После зачисления средства будут зачислены в рублях.")
    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer()

@dp.message(F.text == "💸 Вывести")
async def withdraw_start(message: types.Message, state: FSMContext):
    balance = get_balance(message.from_user.id)
    if balance <= 0:
        await message.answer("У вас нет средств для вывода.")
        return
    await message.answer("Введите сумму в рублях:")
    await state.set_state(WithdrawState.amount)

@dp.message(WithdrawState.amount)
async def withdraw_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
    except:
        await message.answer("Введите число.")
        return
    balance = get_balance(message.from_user.id)
    if amount > balance:
        await message.answer(f"Недостаточно средств. Ваш баланс: {balance:.2f} ₽")
        return
    await state.update_data(amount=amount)
    await message.answer("Введите реквизиты для выплаты (номер карты/счёта/телефон):")
    await state.set_state(WithdrawState.details)

@dp.message(WithdrawState.details)
async def withdraw_details(message: types.Message, state: FSMContext):
    data = await state.get_data()
    amount = data['amount']
    details = message.text
    update_balance(message.from_user.id, -amount)
    add_withdrawal(message.from_user.id, amount, details)
    await message.answer(f"Заявка на вывод {amount:.2f} ₽ создана. Ожидайте подтверждения.")
    await state.clear()

@dp.message(F.text == "👥 Рефералы")
async def referrals(message: types.Message):
    user_id = message.from_user.id
    c.execute("SELECT COUNT(*) FROM users WHERE ref_by = ?", (user_id,))
    count = c.fetchone()[0]
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={user_id}"
    await message.answer(f"Ваша реферальная ссылка:\n{link}\n\nПриглашено: {count} чел.")

# -------------------- АДМИНКА --------------------
@dp.message(F.text == "/admin")
async def admin_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Нет доступа.")
        return
    await message.answer("Панель администратора", reply_markup=admin_kb)

@dp.message(F.text == "📋 Заявки на вывод")
async def show_withdrawals(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    withdrawals = get_pending_withdrawals()
    if not withdrawals:
        await message.answer("Нет активных заявок.")
        return
    for w in withdrawals:
        withdraw_id, user_id, amount, details = w
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_{withdraw_id}")]
        ])
        await message.answer(
            f"Заявка #{withdraw_id}\nПользователь: {user_id}\nСумма: {amount} ₽\nРеквизиты: {details}",
            reply_markup=kb
        )

@dp.callback_query(F.data.startswith("approve_"))
async def approve_withdrawal_cb(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав", show_alert=True)
        return
    withdraw_id = int(callback.data.split("_")[1])
    approve_withdrawal(withdraw_id)
    await callback.message.edit_text(f"Заявка #{withdraw_id} подтверждена.")
    await callback.answer()

@dp.message(F.text == "🔙 Назад")
async def back_to_main(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("Главное меню", reply_markup=main_kb)

# -------------------- ФОНОВАЯ ПРОВЕРКА ПОПОЛНЕНИЙ --------------------
async def get_crypto_rate(crypto_type: str) -> float:
    """Получение курса криптовалюты к рублю"""
    try:
        if 'ton' in crypto_type:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.binance.com/api/v3/ticker/price?symbol=TONUSDT") as resp:
                    data = await resp.json()
                    ton_usdt = float(data['price'])
                    usdt_rub = 95.0
                    return ton_usdt * usdt_rub
        else:
            return 95.0
    except Exception as e:
        logger.error(f"Ошибка получения курса: {e}")
        return 95.0

async def check_ton_transaction(memo: str) -> dict:
    """Проверка транзакций TON (заглушка)"""
    # TODO: Реализовать API запрос к TON Center
    return None

async def check_trc20_transaction(memo: str) -> dict:
    """Проверка транзакций TRC20 (заглушка)"""
    # TODO: Реализовать API запрос к TronGrid
    return None

async def check_deposits():
    """Фоновая задача проверки пополнений"""
    while True:
        try:
            c.execute("SELECT id, user_id, crypto, memo FROM deposits WHERE status = 'pending'")
            pending = c.fetchall()
            for dep_id, user_id, crypto, memo in pending:
                tx = None
                if crypto == 'ton' or crypto == 'usdt_ton':
                    tx = await check_ton_transaction(memo)
                elif crypto == 'usdt_trc20':
                    tx = await check_trc20_transaction(memo)
                
                if tx and tx.get('amount', 0) > 0:
                    amount_crypto = tx['amount']
                    rate = await get_crypto_rate(crypto)
                    rub_amount = amount_crypto * rate
                    update_balance(user_id, rub_amount)
                    complete_deposit(dep_id, amount_crypto)
                    await bot.send_message(user_id, f"✅ Пополнение на {rub_amount:.2f} ₽ зачислено!")
        except Exception as e:
            logger.error(f"Ошибка в check_deposits: {e}")
        await asyncio.sleep(60)

# -------------------- FLASK (для вебхука) --------------------
app = Flask(__name__)
webhook_configured = False

@app.route('/webhook', methods=['POST'])
async def webhook():
    """Эндпоинт для вебхука Telegram"""
    try:
        if request.headers.get('content-type') == 'application/json':
            update = types.Update(**request.json)
            await dp.feed_update(bot, update)
            return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Ошибка в вебхуке: {e}")
    return jsonify({'error': 'Invalid request'}), 400

@app.route('/')
def index():
    return "Bot is running on Railway!"

@app.route('/health')
def health():
    return "OK"

@app.route('/set_webhook', methods=['GET'])
def set_webhook_manual():
    """Ручная установка вебхука"""
    global webhook_configured
    try:
        # Получаем URL из переменной окружения Railway
        railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        if not railway_url:
            # Если нет Railway URL, используем localhost
            railway_url = request.host
        
        webhook_url = f"https://{railway_url}/webhook"
        
        # Устанавливаем вебхук синхронно
        import requests
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            json={"url": webhook_url}
        )
        
        if response.status_code == 200:
            webhook_configured = True
            return jsonify({"status": "success", "webhook_url": webhook_url})
        else:
            return jsonify({"status": "error", "message": response.text}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# -------------------- ЗАПУСК --------------------
def run_bot_polling():
    """Запуск бота через polling (альтернатива вебхуку)"""
    async def start_polling():
        logger.info("Запуск бота через polling...")
        # Запускаем фоновую задачу
        asyncio.create_task(check_deposits())
        # Запускаем polling
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
    # Определяем, запускаем ли мы в Railway или локально
    is_railway = bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN"))
    
    if is_railway:
        # В Railway используем polling (проще и стабильнее)
        logger.info("Запуск на Railway с polling...")
        
        # Запускаем бота в отдельном потоке
        bot_thread = Thread(target=run_bot_polling, daemon=True)
        bot_thread.start()
        
        # Запускаем Flask для health checks
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        # Локально используем polling
        run_bot_polling()
