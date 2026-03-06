import os
from dotenv import load_dotenv
from pathlib import Path
import logging

# Загружаем переменные окружения из .env файла
load_dotenv()

# Базовая директория проекта
BASE_DIR = Path(__file__).resolve().parent

class Config:
    """Класс конфигурации бота"""
    
    # Токен бота (обязательный параметр)
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не установлен в .env файле!")
    
    # ID чатов
    ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '-1003549862438'))
    SUPER_ADMIN_ID = int(os.getenv('SUPER_ADMIN_ID', '5166185821'))
    
    # Настройки базы данных
    DB_NAME = os.getenv('DB_NAME', 'tickets.db')
    DB_PATH = BASE_DIR / DB_NAME
    DB_BACKUP_DIR = BASE_DIR / os.getenv('DB_BACKUP_DIR', 'backups')
    DB_BACKUP_DAYS = int(os.getenv('DB_BACKUP_DAYS', '30'))
    
    # Настройки бота
    BOT_LANGUAGE = os.getenv('BOT_LANGUAGE', 'ru')
    BOT_TIMEZONE = os.getenv('BOT_TIMEZONE', 'Europe/Moscow')
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    
    # Настройки уведомлений
    NOTIFY_NEW_TICKETS = os.getenv('NOTIFY_NEW_TICKETS', 'true').lower() == 'true'
    NOTIFY_STATUS_CHANGE = os.getenv('NOTIFY_STATUS_CHANGE', 'true').lower() == 'true'
    NOTIFY_RESPONSES = os.getenv('NOTIFY_RESPONSES', 'true').lower() == 'true'
    
    # Ограничения
    MAX_MEDIA_PER_TICKET = int(os.getenv('MAX_MEDIA_PER_TICKET', '10'))
    MAX_TICKET_TEXT_LENGTH = int(os.getenv('MAX_TICKET_TEXT_LENGTH', '5000'))
    TICKETS_PER_PAGE = int(os.getenv('TICKETS_PER_PAGE', '10'))
    
    # Безопасность
    ENABLE_RATE_LIMIT = os.getenv('ENABLE_RATE_LIMIT', 'true').lower() == 'true'
    MAX_REQUESTS_PER_MINUTE = int(os.getenv('MAX_REQUESTS_PER_MINUTE', '30'))
    BLOCK_DURATION_HOURS = int(os.getenv('BLOCK_DURATION_HOURS', '24'))
    
    @classmethod
    def create_directories(cls):
        """Создает необходимые директории"""
        cls.DB_BACKUP_DIR.mkdir(exist_ok=True)
        (BASE_DIR / 'logs').mkdir(exist_ok=True)
    
    @classmethod
    def setup_logging(cls):
        """Настройка логирования"""
        log_level = getattr(logging, cls.LOG_LEVEL.upper(), logging.INFO)
        
        logging.basicConfig(
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            level=log_level,
            handlers=[
                logging.FileHandler(BASE_DIR / 'logs' / 'bot.log', encoding='utf-8'),
                logging.StreamHandler()
            ]
        )

# Создаем экземпляр конфигурации
config = Config()

# Создаем необходимые директории
config.create_directories()

# Настраиваем логирование
config.setup_logging()