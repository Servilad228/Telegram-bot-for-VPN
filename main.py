#!/usr/bin/env python3
"""
Telegram Bot для напоминаний об оплате VPN и регистрации пользователей
MVP версия - максимально простая реализация по принципу KISS
"""

import logging
import sqlite3
import os
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
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

# Интеграция с Amnezia VPN (для будущих версий)
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
        "Для регистрации нажмите /register"
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
        "Через 3 дня ({{due_date}}) истекает срок вашей подписки.\n\n"
        "Пожалуйста, продлите подписку вовремя."
    ),
    'payment_due': (
        "🚨 СРОЧНО: Срок оплаты VPN истек!\n\n"
        "Ваша подписка истекла {{due_date}}.\n\n"
        "Пожалуйста, продлите подписку как можно скорее."
    ),
    'help': (
        f"📋 Доступные команды:\n"
        f"• /start - Приветствие и регистрация\n"
        f"• /register - Регистрация в системе\n"
        f"• /help - Показать эту справку\n\n"
        f"📞 По всем вопросам обращайтесь к администратору: {ADMIN_USERNAME}"
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

# ========================================
# ОБРАБОТЧИКИ КОМАНД БОТА
# ========================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    
    logging.info(f"Start command from user {user.id} (@{user.username})")
    
    await update.message.reply_text(MESSAGES['welcome'])

async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /register"""
    user = update.effective_user
    username = user.username or "Unknown"
    
    logging.info(f"Register command from user {user.id} (@{username})")
    
    if register_user(user.id, username):
        # Новый пользователь
        payment_date = get_user_payment_date(user.id)
        message = MESSAGES['registration_success'].format(
            next_payment_date=payment_date.strftime('%d.%m.%Y')
        )
        await update.message.reply_text(message)
        
    else:
        # Пользователь уже существует
        payment_date = get_user_payment_date(user.id)
        message = MESSAGES['already_registered'].format(
            next_payment_date=payment_date.strftime('%d.%m.%Y')
        )
        await update.message.reply_text(message)
        logging.warning(f"User {user.id} already registered")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    user = update.effective_user
    
    logging.info(f"Help command from user {user.id} (@{user.username})")
    
    await update.message.reply_text(MESSAGES['help'])

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
    
    for telegram_id, username, payment_due_date in users:
        try:
            due_date = datetime.strptime(payment_due_date, '%Y-%m-%d').date()
            message = MESSAGES['payment_reminder'].format(
                due_date=due_date.strftime('%d.%m.%Y')
            )
            
            await context.bot.send_message(
                chat_id=telegram_id,
                text=message
            )
            
            logging.info(f"Payment reminder sent to user {telegram_id} (@{username})")
            
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
    application.add_handler(CommandHandler("register", register_command))
    application.add_handler(CommandHandler("help", help_command))
    
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