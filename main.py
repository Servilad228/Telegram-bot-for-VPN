#!/usr/bin/env python3
"""
Telegram Bot для напоминаний об оплате VPN и регистрации пользователей
MVP версия - максимально простая реализация по принципу KISS
"""

import logging
import sqlite3
import os
import secrets
import string
import subprocess
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from logging.handlers import RotatingFileHandler

# ========================================
# КОНФИГУРАЦИЯ MVP (константы в коде)
# ========================================

# ВАЖНО: Замените эти значения на свои!
BOT_TOKEN = "8244782584:AAG5UOUK-X12MoLMfBkKE53yj-hNTur3IkY"  # Получить от @BotFather
ADMIN_USERNAME = "@pervld"  # Ваш логин в Telegram

# Настройки бота
DATABASE_FILE = "users.db"
PAYMENT_REMINDER_DAYS = 3  # За сколько дней до оплаты напоминать
CHECK_TIME_HOUR = 10  # Время ежедневной проверки (10:00)
PAYMENT_PERIOD_DAYS = 30  # Период оплаты (30 дней)

# Настройки оплаты
PAYMENT_AMOUNT = "50 рублей"
PAYMENT_LINK = "https://www.tbank.ru/cf/UbKjn3J4eD"  # Замените на вашу ссылку для оплаты
ADMIN_CHAT_ID = 526829525  # Ваш Telegram ID для уведомлений
AMNEZIA_BASE_DIR = "/opt/amnezia"
OPENVPN_CONFIG_DIR = "/opt/amnezia/openvpn"  
XRAY_CONFIG_DIR = "/opt/amnezia/xray"

# ========================================
# ПРЕДОПРЕДЕЛЕННЫЕ СООБЩЕНИЯ (без LLM)
# ========================================

MESSAGES = {
    'welcome': (
        "🤖 Добро пожаловать в VPN Payment Bot!\n\n"
        "Этот бот поможет вам не забывать об оплате VPN сервиса.\n\n"
        "Используйте кнопки ниже для навигации по боту."
    ),
    'registration_success': (
        "✅ Вы успешно зарегистрированы!\n\n"
        "Теперь вы будете получать напоминания об оплате за 3 дня до истечения срока.\n"
        f"Следующая оплата: {{next_payment_date}}"
    ),
    'already_registered': (
        "ℹ️ Вы уже зарегистрированы в системе.\n"
        f"Следующая оплата: {{next_payment_date}}"
    ),
    'payment_reminder': (
        "⏰ Напоминание об оплате VPN!\n\n"
        "Через 3 дня ({due_date}) истекает срок вашей подписки.\n\n"
        "💰 Пожалуйста, продлите подписку вовремя.\n"
        "После оплаты вы будете получать следующее напоминание через месяц."
    ),
    'payment_due': (
        "🚨 СРОЧНО: Срок оплаты VPN истек!\n\n"
        "Ваша подписка истекла {{due_date}}.\n\n"
        "Пожалуйста, продлите подписку как можно скорее."
    ),
    'help': (
        f"📋 Доступные команды:\n"
        f"• /start - Приветствие и автоматическая регистрация\n"
        f"• /my_profile - Мой профиль и информация о подписке\n"
        f"• /extend - Продлить подписку (получить инструкцию по оплате)\n"
        f"• /help - Показать эту справку\n\n"
        f"📞 По всем вопросам обращайтесь к администратору: {ADMIN_USERNAME}"
    ),
    'payment_instruction': (
        "💳 Инструкция по оплате:\n\n"
        "💰 Сумма: {amount}\n"
        "🔗 Ссылка для оплаты: {payment_link}\n\n"
        "⚠️ ОБЯЗАТЕЛЬНО укажите в комментарии к переводу:\n"
        "📝 \"{payment_comment}\"\n\n"
        "🕐 После оплаты ваша подписка будет автоматически продлена."
    ),
    'admin_payment_notification': (
        "🔔 Новый запрос на продление VPN!\n\n"
        "👤 Пользователь: @{username} (ID: {user_id})\n"
        "📅 Текущая дата оплаты: {current_due_date}\n"
        "📋 Ожидаемый комментарий: \"{payment_comment}\"\n\n"
        "ℹ️ После подтверждения оплаты продлите подписку командой:\n"
        "/confirm_payment {user_id}"
    ),
    'not_registered': (
        "⚠️ Ошибка доступа к профилю.\n"
        "Нажмите /start для повторной регистрации."
    )
}

# ========================================
# НАСТРОЙКА ЛОГГИРОВАНИЯ
# ========================================

def setup_logging():
    """Настройка логгирования согласно техническому видению"""
    
    # Создаем логгер
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Формат логов
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Ротация логов (10MB, 3 файла)
    file_handler = RotatingFileHandler(
        'bot.log', 
        maxBytes=10*1024*1024,  # 10MB
        backupCount=3,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    
    # Вывод в консоль
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    # Добавляем обработчики
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

# ========================================
# РАБОТА С БАЗОЙ ДАННЫХ
# ========================================

def init_database():
    """Создание таблицы users при первом запуске"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            registration_date DATE NOT NULL,
            payment_due_date DATE NOT NULL,
            is_active BOOLEAN DEFAULT 1
        )
    ''')
    
    conn.commit()
    conn.close()
    logging.info("Database initialized successfully")

def register_user(telegram_id: int, username: str) -> bool:
    """
    Регистрация нового пользователя
    Возвращает True если пользователь новый, False если уже существует
    """
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    try:
        # Проверяем, существует ли пользователь
        cursor.execute('SELECT id FROM users WHERE telegram_id = ?', (telegram_id,))
        if cursor.fetchone():
            conn.close()
            return False
        
        # Регистрируем нового пользователя
        registration_date = datetime.now().date()
        payment_due_date = registration_date + timedelta(days=PAYMENT_PERIOD_DAYS)
        
        cursor.execute('''
            INSERT INTO users (telegram_id, username, registration_date, payment_due_date)
            VALUES (?, ?, ?, ?)
        ''', (telegram_id, username, registration_date, payment_due_date))
        
        conn.commit()
        conn.close()
        
        logging.info(f"New user registered: {telegram_id} (@{username})")
        return True
        
    except sqlite3.Error as e:
        logging.error(f"Database error during registration: {e}")
        conn.close()
        return False

def get_user_payment_date(telegram_id: int) -> Optional[datetime]:
    """Получить дату следующей оплаты пользователя"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    cursor.execute('SELECT payment_due_date FROM users WHERE telegram_id = ?', (telegram_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        return datetime.strptime(result[0], '%Y-%m-%d').date()
    return None

def get_users_for_reminder() -> list:
    """Получить пользователей, которым нужно отправить напоминания"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    # Дата через PAYMENT_REMINDER_DAYS дней
    reminder_date = (datetime.now().date() + timedelta(days=PAYMENT_REMINDER_DAYS)).strftime('%Y-%m-%d')
    
    cursor.execute('''
        SELECT telegram_id, username, payment_due_date 
        FROM users 
        WHERE payment_due_date = ? AND is_active = 1
    ''', (reminder_date,))
    
    users = cursor.fetchall()
    conn.close()
    
    return users

def generate_payment_comment(telegram_id: int) -> str:
    """Генерация комментария для оплаты с автоматическими датами"""
    current_payment_date = get_user_payment_date(telegram_id)
    if not current_payment_date:
        return "VPN подписка, договоренность от {}".format(datetime.now().date().strftime('%d.%m.%Y'))
    
    # Рассчитываем период
    period_start = current_payment_date
    period_end = current_payment_date + timedelta(days=PAYMENT_PERIOD_DAYS)
    
    # Дата договоренности (дата запроса)
    agreement_date = datetime.now().date()
    
    comment = "VPN подписка [{}-{}], договоренность от [{}]".format(
        period_start.strftime('%d.%m.%Y'),
        period_end.strftime('%d.%m.%Y'),
        agreement_date.strftime('%d.%m.%Y')
    )
    
    return comment

def get_all_users_with_status() -> list:
    """Получить список всех пользователей с информацией о статусе оплаты"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT telegram_id, username, registration_date, payment_due_date, is_active 
        FROM users 
        ORDER BY payment_due_date ASC
    ''')
    
    users = cursor.fetchall()
    conn.close()
    
    users_data = []
    
    for telegram_id, username, reg_date, payment_date_str, is_active in users:
        from datetime import datetime, timedelta
        
        try:
            payment_date = datetime.strptime(payment_date_str, '%Y-%m-%d').date()
            days_until_payment = (payment_date - datetime.now().date()).days
            
            # Определяем статус оплаты
            if not is_active:
                status_emoji = "⚫"
                status_text = "Неактивен"
            elif days_until_payment <= 0:
                status_emoji = "🔴"
                status_text = f"Просрочено на {abs(days_until_payment)} дн."
            elif days_until_payment <= 3:
                status_emoji = "🟡"
                status_text = f"Осталось {days_until_payment} дн."
            else:
                status_emoji = "🟢"
                status_text = f"Осталось {days_until_payment} дн."
            
            users_data.append({
                'telegram_id': telegram_id,
                'username': username or '',
                'registration_date': reg_date,
                'payment_date': payment_date.strftime('%d.%m.%Y'),
                'days_until_payment': days_until_payment,
                'status_emoji': status_emoji,
                'status_text': status_text,
                'is_active': is_active
            })
            
        except ValueError as e:
            logging.error(f"Error parsing payment date for user {telegram_id}: {e}")
            users_data.append({
                'telegram_id': telegram_id,
                'username': username or '',
                'registration_date': reg_date,
                'payment_date': payment_date_str,
                'days_until_payment': 0,
                'status_emoji': "❌",
                'status_text': "Ошибка даты",
                'is_active': is_active
            })
    
    return users_data

def get_user_profile_info(telegram_id: int) -> dict:
    """Получить информацию о профиле пользователя"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT username, registration_date, payment_due_date, is_active 
        FROM users 
        WHERE telegram_id = ?
    ''', (telegram_id,))
    
    result = cursor.fetchone()
    conn.close()
    
    if result:
        return {
            'username': result[0],
            'registration_date': result[1],
            'payment_due_date': result[2],
            'is_active': bool(result[3])
        }
    return None

def update_user_payment_date(telegram_id: int) -> bool:
    """Обновить дату следующей оплаты пользователя на следующий месяц"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    try:
        # Получаем текущую дату оплаты
        cursor.execute('SELECT payment_due_date FROM users WHERE telegram_id = ?', (telegram_id,))
        result = cursor.fetchone()
        
        if not result:
            conn.close()
            return False
        
        # Вычисляем следующую дату оплаты (через 30 дней)
        current_due_date = datetime.strptime(result[0], '%Y-%m-%d').date()
        next_due_date = current_due_date + timedelta(days=PAYMENT_PERIOD_DAYS)
        
        # Обновляем дату в базе
        cursor.execute('''
            UPDATE users 
            SET payment_due_date = ? 
            WHERE telegram_id = ?
        ''', (next_due_date.strftime('%Y-%m-%d'), telegram_id))
        
        conn.commit()
        conn.close()
        
        logging.info(f"Updated payment date for user {telegram_id}: {current_due_date} -> {next_due_date}")
        return True
        
    except sqlite3.Error as e:
        logging.error(f"Database error updating payment date for {telegram_id}: {e}")
        conn.close()
        return False

# ========================================
# ОБРАБОТЧИКИ КОМАНД БОТА
# ========================================

def get_main_keyboard():
    """Создание основной клавиатуры с командами"""
    keyboard = [
        [InlineKeyboardButton("👤 Мой профиль", callback_data="my_profile")],
        [InlineKeyboardButton("💳 Продлить подписку", callback_data="extend")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
        [InlineKeyboardButton("🆔 Мой ID", callback_data="getid")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start - автоматическая регистрация"""
    user = update.effective_user
    username = user.username or "Unknown"
    
    logging.info(f"Start command from user {user.id} (@{username})")
    
    # Автоматическая регистрация пользователя
    is_new_user = register_user(user.id, username)
    
    keyboard = get_main_keyboard()
    
    if is_new_user:
        # Новый пользователь - показываем приветствие и дату платежа
        payment_date = get_user_payment_date(user.id)
        welcome_message = (
            f"{MESSAGES['welcome']}\n\n"
            f"✅ Вы успешно зарегистрированы!\n"
            f"📅 Следующая оплата: {payment_date.strftime('%d.%m.%Y')}"
        )
        await update.message.reply_text(welcome_message, reply_markup=keyboard)
        logging.info(f"New user auto-registered: {user.id} (@{username})")
    else:
        # Существующий пользователь - обычное приветствие
        await update.message.reply_text(MESSAGES['welcome'], reply_markup=keyboard)

async def my_profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /my_profile - информация о профиле"""
    user = update.effective_user
    
    logging.info(f"My profile command from user {user.id} (@{user.username})")
    
    # Проверяем, зарегистрирован ли пользователь
    payment_date = get_user_payment_date(user.id)
    keyboard = get_main_keyboard()
    
    if not payment_date:
        await update.message.reply_text(MESSAGES['not_registered'], reply_markup=keyboard)
        return
    
    # Получаем информацию о пользователе
    user_info = get_user_profile_info(user.id)
    
    if user_info:
        # Рассчитываем дни до оплаты
        from datetime import datetime, timedelta
        days_until_payment = (payment_date - datetime.now().date()).days
        
        if days_until_payment <= 0:
            status_emoji = "🔴"
            status_text = "Оплата просрочена"
        elif days_until_payment <= 3:
            status_emoji = "🟡"
            status_text = f"Оплата через {days_until_payment} дн."
        else:
            status_emoji = "🟢"
            status_text = f"Оплата через {days_until_payment} дн."
        
        profile_message = (
            f"👤 **Мой профиль**\n\n"
            f"🆔 **ID:** {user.id}\n"
            f"👤 **Логин:** @{user.username or 'Не указан'}\n"
            f"📅 **Дата регистрации:** {user_info['registration_date']}\n"
            f"💳 **Следующая оплата:** {payment_date.strftime('%d.%m.%Y')}\n"
            f"{status_emoji} **Статус:** {status_text}\n\n"
            f"💰 **Сумма оплаты:** {PAYMENT_AMOUNT}"
        )
        
        await update.message.reply_text(profile_message, reply_markup=keyboard, parse_mode='Markdown')
    else:
        await update.message.reply_text(
            "❌ Ошибка получения информации о профиле", 
            reply_markup=keyboard
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    user = update.effective_user
    
    logging.info(f"Help command from user {user.id} (@{user.username})")
    
    keyboard = get_main_keyboard()
    await update.message.reply_text(MESSAGES['help'], reply_markup=keyboard)

async def test_admin_notification_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тестовая команда для проверки уведомлений админа"""
    user = update.effective_user
    
    # Проверяем, что команду выполняет администратор
    if user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ Доступ запрещён. Команда доступна только администратору.")
        return
    
    try:
        test_message = (
            "🔔 **Тест уведомлений**\n\n"
            "✅ Связь с админом работает!\n"
            f"🆔 Ваш ID: {user.id}\n"
            f"👤 Username: @{user.username}\n\n"
            "💳 Теперь вы будете получать уведомления о запросах на оплату."
        )
        
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=test_message,
            parse_mode='Markdown'
        )
        
        logging.info(f"Admin notification test successful for admin {user.id}")
        
    except Exception as e:
        await update.message.reply_text(
            f"❌ Ошибка отправки уведомления: {e}\n\n"
            "💡 Пожалуйста, убедитесь, что вы начали диалог с ботом через /start"
        )
        logging.error(f"Failed to send admin test notification: {e}")

async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для получения списка всех пользователей (только для админа)"""
    user = update.effective_user
    
    # Проверяем, что команду выполняет администратор
    if user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ Доступ запрещён. Команда доступна только администратору.")
        return
    
    try:
        users_data = get_all_users_with_status()
        
        if not users_data:
            await update.message.reply_text(
                "📊 **Список пользователей**\n\n"
                "😅 Пользователей пока нет!",
                parse_mode='Markdown'
            )
            return
        
        # Формируем сообщение со списком пользователей
        message = "📊 **Список пользователей**\n\n"
        
        total_users = len(users_data)
        active_users = sum(1 for user in users_data if user['status_emoji'] == '🟢')
        warning_users = sum(1 for user in users_data if user['status_emoji'] == '🟡')
        overdue_users = sum(1 for user in users_data if user['status_emoji'] == '🔴')
        
        message += (
            f"📈 **Статистика:**\n"
            f"• Всего пользователей: {total_users}\n"
            f"• 🟢 Активные: {active_users}\n"
            f"• 🟡 Нужно оплатить: {warning_users}\n"
            f"• 🔴 Просрочено: {overdue_users}\n\n"
        )
        
        message += "👥 **Пользователи:**\n"
        
        for i, user_data in enumerate(users_data, 1):
            username_display = f"@{user_data['username']}" if user_data['username'] else "Не указан"
            
            message += (
                f"{i}. {user_data['status_emoji']} `{user_data['telegram_id']}` {username_display}\n"
                f"   📅 Оплата: {user_data['payment_date']} ({user_data['status_text']})\n\n"
            )
        
        # Разбиваем на части если сообщение слишком длинное
        if len(message) > 4000:
            # Отправляем статистику отдельно
            stats_message = (
                f"📊 **Список пользователей**\n\n"
                f"📈 **Статистика:**\n"
                f"• Всего пользователей: {total_users}\n"
                f"• 🟢 Активные: {active_users}\n"
                f"• 🟡 Нужно оплатить: {warning_users}\n"
                f"• 🔴 Просрочено: {overdue_users}"
            )
            await update.message.reply_text(stats_message, parse_mode='Markdown')
            
            # Отправляем список пользователей по частям
            user_chunks = [users_data[i:i + 10] for i in range(0, len(users_data), 10)]
            
            for chunk_num, chunk in enumerate(user_chunks, 1):
                chunk_message = f"👥 **Пользователи (часть {chunk_num}/{len(user_chunks)}):**\n\n"
                
                start_index = (chunk_num - 1) * 10
                for j, user_data in enumerate(chunk, start_index + 1):
                    username_display = f"@{user_data['username']}" if user_data['username'] else "Не указан"
                    
                    chunk_message += (
                        f"{j}. {user_data['status_emoji']} `{user_data['telegram_id']}` {username_display}\n"
                        f"   📅 Оплата: {user_data['payment_date']} ({user_data['status_text']})\n\n"
                    )
                
                await update.message.reply_text(chunk_message, parse_mode='Markdown')
        else:
            await update.message.reply_text(message, parse_mode='Markdown')
        
        logging.info(f"Admin {user.id} requested user list - {total_users} users displayed")
        
    except Exception as e:
        await update.message.reply_text(
            f"❌ Ошибка получения списка пользователей: {e}"
        )
        logging.error(f"Failed to get user list for admin {user.id}: {e}")

async def get_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Временная команда для получения Telegram ID"""
    user = update.effective_user
    
    admin_status = "🔴 Обычный пользователь"
    if user.id == ADMIN_CHAT_ID:
        admin_status = "🟢 Администратор"
    
    message = (
        f"🆔 Ваш Telegram ID: `{user.id}`\n"
        f"👤 Username: @{user.username or 'Не указан'}\n"
        f"🔑 Статус: {admin_status}\n\n"
        f"📝 Для настройки админа скопируйте: `{user.id}`"
    )
    
    if user.id == ADMIN_CHAT_ID:
        message += "\n\n🔔 Чтобы протестировать уведомления, используйте /test_admin"
    
    keyboard = get_main_keyboard()
    await update.message.reply_text(message, reply_markup=keyboard, parse_mode='Markdown')
    logging.info(f"ID request from user {user.id} (@{user.username})")   

async def extend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /extend - запрос на продление подписки"""
    user = update.effective_user
    
    logging.info(f"Extend command from user {user.id} (@{user.username})")
    
    # Проверяем, зарегистрирован ли пользователь
    current_payment_date = get_user_payment_date(user.id)
    if not current_payment_date:
        keyboard = get_main_keyboard()
        await update.message.reply_text(MESSAGES['not_registered'], reply_markup=keyboard)
        return
    
    # Генерируем комментарий для оплаты
    payment_comment = generate_payment_comment(user.id)
    
    # Отправляем инструкцию пользователю
    user_message = MESSAGES['payment_instruction'].format(
        amount=PAYMENT_AMOUNT,
        payment_link=PAYMENT_LINK,
        payment_comment=payment_comment
    )
    keyboard = get_main_keyboard()
    await update.message.reply_text(user_message, reply_markup=keyboard)
    
    # Отправляем уведомление администратору
    try:
        admin_message = MESSAGES['admin_payment_notification'].format(
            username=user.username or "Unknown",
            user_id=user.id,
            current_due_date=current_payment_date.strftime('%d.%m.%Y'),
            payment_comment=payment_comment
        )
        
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_message
        )
        
        logging.info(f"Payment request from user {user.id}: {payment_comment}")
        logging.info(f"Admin notification sent for user {user.id}")
        
    except Exception as e:
        logging.error(f"Failed to send admin notification for user {user.id}: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на инлайн кнопки"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    command = query.data
    
    logging.info(f"Button pressed: {command} from user {user.id} (@{user.username})")
    
    # Обрабатываем команды
    if command == "my_profile":
        await handle_my_profile_button(query, context)
    elif command == "extend":
        await handle_extend_button(query, context)
    elif command == "help":
        await handle_help_button(query, context)
    elif command == "getid":
        await handle_getid_button(query, context)

async def handle_my_profile_button(query, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопки моего профиля"""
    user = query.from_user
    
    # Проверяем, зарегистрирован ли пользователь
    payment_date = get_user_payment_date(user.id)
    keyboard = get_main_keyboard()
    
    if not payment_date:
        await query.edit_message_text(MESSAGES['not_registered'], reply_markup=keyboard)
        return
    
    # Получаем информацию о пользователе
    user_info = get_user_profile_info(user.id)
    
    if user_info:
        # Рассчитываем дни до оплаты
        from datetime import datetime, timedelta
        days_until_payment = (payment_date - datetime.now().date()).days
        
        if days_until_payment <= 0:
            status_emoji = "🔴"
            status_text = "Оплата просрочена"
        elif days_until_payment <= 3:
            status_emoji = "🟡"
            status_text = f"Оплата через {days_until_payment} дн."
        else:
            status_emoji = "🟢"
            status_text = f"Оплата через {days_until_payment} дн."
        
        profile_message = (
            f"👤 **Мой профиль**\n\n"
            f"🆔 **ID:** {user.id}\n"
            f"👤 **Логин:** @{user.username or 'Не указан'}\n"
            f"📅 **Дата регистрации:** {user_info['registration_date']}\n"
            f"💳 **Следующая оплата:** {payment_date.strftime('%d.%m.%Y')}\n"
            f"{status_emoji} **Статус:** {status_text}\n\n"
            f"💰 **Сумма оплаты:** {PAYMENT_AMOUNT}"
        )
        
        await query.edit_message_text(profile_message, reply_markup=keyboard, parse_mode='Markdown')
    else:
        await query.edit_message_text(
            "❌ Ошибка получения информации о профиле", 
            reply_markup=keyboard
        )

async def handle_extend_button(query, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопки продления подписки"""
    user = query.from_user
    
    current_payment_date = get_user_payment_date(user.id)
    if not current_payment_date:
        keyboard = get_main_keyboard()
        await query.edit_message_text(MESSAGES['not_registered'], reply_markup=keyboard)
        return
    
    payment_comment = generate_payment_comment(user.id)
    
    user_message = MESSAGES['payment_instruction'].format(
        amount=PAYMENT_AMOUNT,
        payment_link=PAYMENT_LINK,
        payment_comment=payment_comment
    )
    
    keyboard = get_main_keyboard()
    await query.edit_message_text(user_message, reply_markup=keyboard)
    
    # Отправляем уведомление администратору
    try:
        admin_message = MESSAGES['admin_payment_notification'].format(
            username=user.username or "Unknown",
            user_id=user.id,
            current_due_date=current_payment_date.strftime('%d.%m.%Y'),
            payment_comment=payment_comment
        )
        
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_message
        )
        
        logging.info(f"Payment request from user {user.id}: {payment_comment}")
        logging.info(f"Admin notification sent for user {user.id}")
        
    except Exception as e:
        logging.error(f"Failed to send admin notification for user {user.id}: {e}")

async def handle_help_button(query, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопки помощи"""
    keyboard = get_main_keyboard()
    await query.edit_message_text(MESSAGES['help'], reply_markup=keyboard)

async def handle_getid_button(query, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопки получения ID"""
    user = query.from_user
    
    admin_status = "🔴 Обычный пользователь"
    if user.id == ADMIN_CHAT_ID:
        admin_status = "🟢 Администратор"
    
    message = (
        f"🆔 Ваш Telegram ID: `{user.id}`\n"
        f"👤 Username: @{user.username or 'Не указан'}\n"
        f"🔑 Статус: {admin_status}\n\n"
        f"📝 Для настройки админа скопируйте: `{user.id}`"
    )
    
    if user.id == ADMIN_CHAT_ID:
        message += "\n\n🔔 Чтобы протестировать уведомления, используйте /test_admin"
    
    keyboard = get_main_keyboard()
    await query.edit_message_text(message, reply_markup=keyboard, parse_mode='Markdown')

async def confirm_payment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /confirm_payment - подтверждение оплаты (только для админа)"""
    user = update.effective_user
    
    # Проверяем, что команду выполняет администратор
    if user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ Доступ запрещён. Команда доступна только администратору.")
        return
    
    # Проверяем аргументы команды
    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "⚠️ Неправильный формат команды.\n"
            "Используйте: /confirm_payment USER_ID"
        )
        return
    
    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Неправильный ID пользователя. Используйте число.")
        return
    
    # Проверяем, существует ли пользователь
    current_payment_date = get_user_payment_date(target_user_id)
    if not current_payment_date:
        await update.message.reply_text(f"❌ Пользователь {target_user_id} не найден в системе.")
        return
    
    # Продлеваем подписку
    if update_user_payment_date(target_user_id):
        new_payment_date = get_user_payment_date(target_user_id)
        
        # Уведомляем админа
        await update.message.reply_text(
            f"✅ Подписка продлена!\n"
            f"👤 Пользователь: {target_user_id}\n"
            f"📅 Новая дата оплаты: {new_payment_date.strftime('%d.%m.%Y')}",
            reply_markup=keyboard
        )
        
        keyboard = get_main_keyboard()
        
        # Уведомляем пользователя
        try:
            user_notification = (
                "✅ Оплата подтверждена!\n\n"
                f"📅 Новая дата оплаты: {new_payment_date.strftime('%d.%m.%Y')}\n"
                "Следующее напоминание будет отправлено за 3 дня до этой даты."
            )
            
            await context.bot.send_message(
                chat_id=target_user_id,
                text=user_notification,
                reply_markup=keyboard
            )
            
            logging.info(f"Admin {user.id} confirmed payment for user {target_user_id}: {current_payment_date} -> {new_payment_date}")
            
        except Exception as e:
            await update.message.reply_text(f"⚠️ Подписка продлена, но не удалось уведомить пользователя: {e}")
            logging.error(f"Failed to notify user {target_user_id} about payment confirmation: {e}")
    
    else:
        await update.message.reply_text(f"❌ Ошибка при продлении подписки для пользователя {target_user_id}.")
        logging.error(f"Failed to extend subscription for user {target_user_id}")

# ========================================
# ПЛАНИРОВЩИК НАПОМИНАНИЙ
# ========================================

async def send_payment_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Отправка напоминаний об оплате (вызывается планировщиком)"""
    logging.info("Checking for users who need payment reminders...")
    
    users = get_users_for_reminder()
    
    if not users:
        logging.info("No users need payment reminders today")
        return
    
    logging.info(f"Found {len(users)} users who need payment reminders")
    
    for telegram_id, username, payment_due_date in users:
        try:
            due_date = datetime.strptime(payment_due_date, '%Y-%m-%d').date()
            message = MESSAGES['payment_reminder'].format(
                due_date=due_date.strftime('%d.%m.%Y')
            )
            
            # Отправляем напоминание
            await context.bot.send_message(
                chat_id=telegram_id,
                text=message
            )
            
            logging.info(f"Payment reminder sent to user {telegram_id} (@{username}) for due date {due_date}")
            
            # Обновляем дату следующей оплаты (на следующий месяц)
            if update_user_payment_date(telegram_id):
                next_payment_date = due_date + timedelta(days=PAYMENT_PERIOD_DAYS)
                logging.info(f"Next payment date for user {telegram_id}: {next_payment_date}")
            else:
                logging.error(f"Failed to update payment date for user {telegram_id}")
            
        except Exception as e:
            logging.error(f"Failed to send reminder to {telegram_id}: {e}")

def setup_scheduler(application: Application):
    """Настройка планировщика задач"""
    scheduler = AsyncIOScheduler()
    
    # Ежедневная проверка в указанное время
    scheduler.add_job(
        send_payment_reminders,
        trigger=CronTrigger(hour=CHECK_TIME_HOUR, minute=0),
        args=[application],
        id='daily_payment_check',
        name='Daily Payment Reminder Check'
    )
    
    scheduler.start()
    logging.info(f"Scheduler started, checking reminders daily at {CHECK_TIME_HOUR}:00")
    
    return scheduler

# ========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (для будущих версий)
# ========================================

def check_vpn_user_exists(telegram_id: int) -> bool:
    """
    Проверка наличия VPN конфигурации пользователя
    Пока не используется в MVP, добавлено для будущих версий
    """
    openvpn_file = f"{OPENVPN_CONFIG_DIR}/user_{telegram_id}.ovpn"
    xray_file = f"{XRAY_CONFIG_DIR}/user_{telegram_id}.json"
    
    return os.path.exists(openvpn_file) or os.path.exists(xray_file)

# ========================================
# ОСНОВНАЯ ФУНКЦИЯ
# ========================================

def main():
    """Основная функция запуска бота"""
    
    # Настройка логгирования
    setup_logging()
    
    # Проверка конфигурации
    if BOT_TOKEN == "your_bot_token_here":
        logging.error("❌ BOT_TOKEN not configured! Please set your bot token in main.py")
        print("❌ Ошибка: Не настроен BOT_TOKEN!")
        print("Получите токен от @BotFather и укажите его в файле main.py")
        return
    
    if ADMIN_USERNAME == "@your_telegram_username":
        logging.warning("⚠️ ADMIN_USERNAME not configured, using default")
    
    logging.info("🚀 Starting VPN Payment Reminder Bot...")
    
    # Инициализация базы данных
    init_database()
    
    # Создание приложения бота
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Регистрация обработчиков команд
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("my_profile", my_profile_command))
    application.add_handler(CommandHandler("extend", extend_command))
    application.add_handler(CommandHandler("confirm_payment", confirm_payment_command))
    application.add_handler(CommandHandler("getid", get_id_command))  # Временная команда
    application.add_handler(CommandHandler("test_admin", test_admin_notification_command))  # Тест админ уведомлений
    application.add_handler(CommandHandler("list", list_users_command))  # Список пользователей
    application.add_handler(CommandHandler("help", help_command))
    
    # Регистрация обработчика инлайн кнопок
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Настройка планировщика
    scheduler = setup_scheduler(application)
    
    # Запуск бота
    try:
        logging.info("✅ Bot started successfully! Press Ctrl+C to stop.")
        application.run_polling()
        
    except KeyboardInterrupt:
        logging.info("🛑 Bot stopped by user")
        
    except Exception as e:
        logging.error(f"❌ Bot crashed: {e}")
        
    finally:
        if 'scheduler' in locals():
            scheduler.shutdown()
        logging.info("🔄 Bot shutdown complete")

if __name__ == '__main__':
    main()