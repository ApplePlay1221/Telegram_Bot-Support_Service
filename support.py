import sqlite3
import logging
import json
import os
import shutil
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from enum import Enum
from contextlib import contextmanager
from pathlib import Path

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)

# ==================== КОНФИГУРАЦИЯ ====================

from dotenv import load_dotenv
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
config.create_directories()
config.setup_logging()

logger = logging.getLogger(__name__)

# Константы из конфига
ADMIN_CHAT_ID = config.ADMIN_CHAT_ID
SUPER_ADMIN_ID = config.SUPER_ADMIN_ID

# ==================== СОСТОЯНИЯ ====================

(
    SELECTING_ACTION,
    TYPING_PROBLEM,
    TYPING_COMPLAINT,
    TYPING_QUESTION,
    TYPING_SUGGESTION,
    WAITING_PHONE,
    WAITING_MEDIA,
    ADMIN_SELECTING_TICKET,
    ADMIN_TYPING_RESPONSE,
    ADMIN_MANAGING_USERS,
    ADMIN_SEARCH,
) = range(11)

# ==================== ENUMS ====================

class TicketType(Enum):
    PROBLEM = "Проблема"
    COMPLAINT = "Жалоба"
    QUESTION = "Вопрос"
    SUGGESTION = "Предложение"

class TicketStatus(Enum):
    PENDING = "В рассмотрении"
    ACCEPTED = "Заявка принята"
    FORWARDED = "Запрос передан в руководство"
    REJECTED = "Заявка отклонена"
    COMPLETED = "Завершена"

class TicketPriority(Enum):
    LOW = "Низкий"
    MEDIUM = "Средний"
    HIGH = "Высокий"
    URGENT = "Срочный"

class AdminRole(Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    MODERATOR = "moderator"

# ==================== БАЗА ДАННЫХ ====================

class Database:
    def __init__(self):
        self.db_name = str(config.DB_PATH)
        self.backup_dir = str(config.DB_BACKUP_DIR)
        self.cache = {}
        self.cache_ttl = 300
        
        os.makedirs(self.backup_dir, exist_ok=True)
        
        self.conn = sqlite3.connect(self.db_name, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.row_factory = sqlite3.Row
        
        self.create_tables()
        self.create_indexes()
        self.init_super_admin()
        self.init_default_settings()
    
    @contextmanager
    def transaction(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Database transaction error: {e}")
            raise
    
    def create_tables(self):
        with self.transaction() as conn:
            cursor = conn.cursor()
            
            # Таблица пользователей
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    phone TEXT,
                    language_code TEXT,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_tickets INTEGER DEFAULT 0,
                    is_blocked BOOLEAN DEFAULT 0,
                    block_reason TEXT,
                    blocked_at TIMESTAMP
                )
            ''')
            
            # Таблица заявок
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    ticket_number TEXT UNIQUE,
                    type TEXT NOT NULL,
                    status TEXT DEFAULT 'PENDING',
                    priority TEXT DEFAULT 'MEDIUM',
                    description TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP,
                    assigned_to INTEGER,
                    response_time INTEGER,
                    resolution_time INTEGER,
                    rating INTEGER CHECK(rating >= 1 AND rating <= 5),
                    feedback TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id),
                    FOREIGN KEY (assigned_to) REFERENCES admins(user_id)
                )
            ''')
            
            # Таблица медиафайлов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    file_size INTEGER,
                    file_name TEXT,
                    mime_type TEXT,
                    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
                )
            ''')
            
            # Таблица ответов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS responses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    admin_id INTEGER,
                    user_id INTEGER,
                    response_text TEXT NOT NULL,
                    response_type TEXT DEFAULT 'public',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_read BOOLEAN DEFAULT 0,
                    FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE,
                    FOREIGN KEY (admin_id) REFERENCES admins(user_id),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
            
            # Таблица администраторов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    role TEXT DEFAULT 'moderator',
                    added_by INTEGER,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP,
                    total_responses INTEGER DEFAULT 0,
                    total_assigned INTEGER DEFAULT 0,
                    can_manage_admins BOOLEAN DEFAULT 0,
                    can_ban_users BOOLEAN DEFAULT 0,
                    can_export_data BOOLEAN DEFAULT 0,
                    FOREIGN KEY (added_by) REFERENCES admins(user_id)
                )
            ''')
            
            # Таблица черного списка
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS blacklist (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    reason TEXT,
                    banned_by INTEGER,
                    banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    is_permanent BOOLEAN DEFAULT 0,
                    FOREIGN KEY (banned_by) REFERENCES admins(user_id)
                )
            ''')
            
            # Таблица уведомлений
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    ticket_id INTEGER,
                    type TEXT NOT NULL,
                    title TEXT,
                    message TEXT NOT NULL,
                    is_read BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id),
                    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
                )
            ''')
            
            # Таблица статистики
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS statistics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date DATE UNIQUE,
                    new_tickets INTEGER DEFAULT 0,
                    closed_tickets INTEGER DEFAULT 0,
                    avg_response_time REAL,
                    avg_resolution_time REAL,
                    satisfaction_rate REAL
                )
            ''')
            
            # Таблица логов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS action_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    admin_id INTEGER,
                    action TEXT NOT NULL,
                    entity_type TEXT,
                    entity_id INTEGER,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица настроек
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    description TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            logger.info("✅ Таблицы созданы")
    
    def create_indexes(self):
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_tickets_user_id ON tickets(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_tickets_created_at ON tickets(created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_tickets_ticket_number ON tickets(ticket_number)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_responses_ticket_id ON responses(ticket_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)')
            logger.info("✅ Индексы созданы")
    
    def init_super_admin(self):
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM admins WHERE role = ?', (AdminRole.SUPER_ADMIN.value,))
            if not cursor.fetchone():
                cursor.execute('''
                    INSERT OR REPLACE INTO admins 
                    (user_id, username, first_name, role, added_by, can_manage_admins, can_ban_users, can_export_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    SUPER_ADMIN_ID, 
                    'super_admin', 
                    'СуперАдмин', 
                    AdminRole.SUPER_ADMIN.value,
                    SUPER_ADMIN_ID,
                    1, 1, 1
                ))
                logger.info("✅ Суперадмин инициализирован")
    
    def init_default_settings(self):
        with self.transaction() as conn:
            cursor = conn.cursor()
            default_settings = {
                'max_media_per_ticket': str(config.MAX_MEDIA_PER_TICKET),
                'max_ticket_text_length': str(config.MAX_TICKET_TEXT_LENGTH),
                'tickets_per_page': str(config.TICKETS_PER_PAGE),
                'notify_new_tickets': str(config.NOTIFY_NEW_TICKETS).lower(),
                'notify_status_change': str(config.NOTIFY_STATUS_CHANGE).lower(),
                'notify_responses': str(config.NOTIFY_RESPONSES).lower(),
                'bot_language': config.BOT_LANGUAGE,
            }
            for key, value in default_settings.items():
                cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
    
    def generate_ticket_number(self, ticket_id: int) -> str:
        year = datetime.now().year
        return f"TICKET-{year}-{ticket_id:04d}"
    
    def get_or_create_user(self, user_id: int, username: str = None, first_name: str = None, last_name: str = None) -> Dict:
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
            user = cursor.fetchone()
            if not user:
                cursor.execute('''
                    INSERT INTO users (user_id, username, first_name, last_name)
                    VALUES (?, ?, ?, ?)
                ''', (user_id, username, first_name, last_name))
                cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
                user = cursor.fetchone()
            return dict(user) if user else None
    
    def update_user_activity(self, user_id: int):
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE user_id = ?', (user_id,))
    
    def is_user_blocked(self, user_id: int) -> bool:
        cursor = self.conn.cursor()
        cursor.execute('SELECT is_blocked FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return bool(result and result[0])
    
    def create_ticket(self, user_id: int, ticket_type: str, description: str, priority: str = 'MEDIUM') -> int:
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO tickets (user_id, type, description, priority)
                VALUES (?, ?, ?, ?)
            ''', (user_id, ticket_type, description, priority))
            ticket_id = cursor.lastrowid
            ticket_number = self.generate_ticket_number(ticket_id)
            cursor.execute('UPDATE tickets SET ticket_number = ? WHERE id = ?', (ticket_number, ticket_id))
            cursor.execute('UPDATE users SET total_tickets = total_tickets + 1 WHERE user_id = ?', (user_id,))
            return ticket_id
    
    def get_ticket(self, ticket_id: int) -> Dict:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT t.*, u.username, u.first_name, u.last_name, u.phone,
                   a.username as admin_username, a.first_name as admin_first_name
            FROM tickets t
            LEFT JOIN users u ON t.user_id = u.user_id
            LEFT JOIN admins a ON t.assigned_to = a.user_id
            WHERE t.id = ?
        ''', (ticket_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def get_ticket_by_number(self, ticket_number: str) -> Dict:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT t.*, u.username, u.first_name, u.last_name, u.phone
            FROM tickets t
            LEFT JOIN users u ON t.user_id = u.user_id
            WHERE t.ticket_number = ?
        ''', (ticket_number,))
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def update_ticket_status(self, ticket_id: int, status: str, admin_id: int = None):
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE tickets 
                SET status = ?, updated_at = CURRENT_TIMESTAMP,
                    closed_at = CASE WHEN ? IN ('COMPLETED', 'REJECTED') 
                                    THEN CURRENT_TIMESTAMP ELSE closed_at END
                WHERE id = ?
            ''', (status, status, ticket_id))
            
            if status in ['COMPLETED', 'REJECTED']:
                cursor.execute('''
                    UPDATE tickets 
                    SET resolution_time = ROUND((JULIANDAY(CURRENT_TIMESTAMP) - 
                                                JULIANDAY(created_at)) * 24 * 60)
                    WHERE id = ?
                ''', (ticket_id,))
    
    def assign_ticket(self, ticket_id: int, admin_id: int):
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE tickets SET assigned_to = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', 
                         (admin_id, ticket_id))
            cursor.execute('UPDATE admins SET total_assigned = total_assigned + 1 WHERE user_id = ?', (admin_id,))
    
    def add_response(self, ticket_id: int, response_text: str, admin_id: int = None, 
                    user_id: int = None, response_type: str = 'public') -> int:
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO responses (ticket_id, admin_id, user_id, response_text, response_type)
                VALUES (?, ?, ?, ?, ?)
            ''', (ticket_id, admin_id, user_id, response_text, response_type))
            response_id = cursor.lastrowid
            
            if admin_id and response_type == 'public':
                cursor.execute('''
                    UPDATE tickets 
                    SET response_time = ROUND((JULIANDAY(CURRENT_TIMESTAMP) - 
                                              JULIANDAY(created_at)) * 24 * 60)
                    WHERE id = ? AND response_time IS NULL
                ''', (ticket_id,))
                cursor.execute('UPDATE admins SET total_responses = total_responses + 1 WHERE user_id = ?', (admin_id,))
            
            return response_id
    
    def get_ticket_responses(self, ticket_id: int, include_internal: bool = False) -> List[Dict]:
        cursor = self.conn.cursor()
        query = '''
            SELECT r.*, a.username as admin_username, a.first_name as admin_name,
                   u.username as user_username, u.first_name as user_name
            FROM responses r
            LEFT JOIN admins a ON r.admin_id = a.user_id
            LEFT JOIN users u ON r.user_id = u.user_id
            WHERE r.ticket_id = ?
        '''
        if not include_internal:
            query += " AND r.response_type != 'internal'"
        query += " ORDER BY r.created_at"
        cursor.execute(query, (ticket_id,))
        return [dict(row) for row in cursor.fetchall()]
    
    def add_media(self, ticket_id: int, file_id: str, file_type: str, file_size: int = None, 
                 file_name: str = None, mime_type: str = None):
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO media (ticket_id, file_id, file_type, file_size, file_name, mime_type)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (ticket_id, file_id, file_type, file_size, file_name, mime_type))
    
    def get_ticket_media(self, ticket_id: int) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM media WHERE ticket_id = ? ORDER BY uploaded_at', (ticket_id,))
        return [dict(row) for row in cursor.fetchall()]
    
    def get_user_tickets(self, user_id: int, limit: int = 10, offset: int = 0) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM tickets 
            WHERE user_id = ? 
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        ''', (user_id, limit, offset))
        return [dict(row) for row in cursor.fetchall()]
    
    def get_all_tickets(self, status: str = None, limit: int = 50, offset: int = 0) -> List[Dict]:
        query = "SELECT * FROM tickets WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]
    
    def search_tickets(self, query_text: str, limit: int = 20) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT t.*, u.username, u.first_name
            FROM tickets t
            LEFT JOIN users u ON t.user_id = u.user_id
            WHERE t.description LIKE ? OR t.ticket_number LIKE ? OR u.username LIKE ?
            ORDER BY t.created_at DESC
            LIMIT ?
        ''', (f'%{query_text}%', f'%{query_text}%', f'%{query_text}%', limit))
        return [dict(row) for row in cursor.fetchall()]
    
    def add_admin(self, user_id: int, username: str, first_name: str, added_by: int, role: str = 'moderator') -> bool:
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT role, can_manage_admins FROM admins WHERE user_id = ?', (added_by,))
            adder = cursor.fetchone()
            if not adder or (adder[1] != 1 and adder[0] != AdminRole.SUPER_ADMIN.value):
                return False
            cursor.execute('''
                INSERT OR REPLACE INTO admins 
                (user_id, username, first_name, role, added_by, can_manage_admins, can_ban_users, can_export_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, username, first_name, role, added_by, 1 if role == AdminRole.ADMIN.value else 0,
                  1 if role in [AdminRole.SUPER_ADMIN.value, AdminRole.ADMIN.value] else 0,
                  1 if role == AdminRole.SUPER_ADMIN.value else 0))
            return True
    
    def remove_admin(self, user_id: int, removed_by: int) -> bool:
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT role FROM admins WHERE user_id = ?', (removed_by,))
            remover = cursor.fetchone()
            if not remover or remover[0] != AdminRole.SUPER_ADMIN.value:
                return False
            cursor.execute('SELECT role FROM admins WHERE user_id = ?', (user_id,))
            admin = cursor.fetchone()
            if admin and admin[0] == AdminRole.SUPER_ADMIN.value:
                return False
            cursor.execute('DELETE FROM admins WHERE user_id = ?', (user_id,))
            return cursor.rowcount > 0
    
    def get_all_admins(self) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM admins ORDER BY role, added_at')
        return [dict(row) for row in cursor.fetchall()]
    
    def is_admin(self, user_id: int) -> bool:
        cursor = self.conn.cursor()
        cursor.execute('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
        return cursor.fetchone() is not None
    
    def get_admin_role(self, user_id: int) -> Optional[str]:
        cursor = self.conn.cursor()
        cursor.execute('SELECT role FROM admins WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result[0] if result else None
    
    def update_admin_activity(self, admin_id: int):
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE admins SET last_active = CURRENT_TIMESTAMP WHERE user_id = ?', (admin_id,))
    
    def create_notification(self, user_id: int, ticket_id: int = None, notif_type: str = 'system', 
                          title: str = None, message: str = None):
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO notifications (user_id, ticket_id, type, title, message)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, ticket_id, notif_type, title, message))
    
    def get_user_notifications(self, user_id: int, unread_only: bool = True) -> List[Dict]:
        cursor = self.conn.cursor()
        query = 'SELECT * FROM notifications WHERE user_id = ?'
        params = [user_id]
        if unread_only:
            query += ' AND is_read = 0'
        query += ' ORDER BY created_at DESC'
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]
    
    def update_statistics(self):
        today = datetime.now().date()
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) as new_tickets,
                       SUM(CASE WHEN status IN ('COMPLETED', 'REJECTED') AND 
                                      DATE(closed_at) = DATE('now') THEN 1 ELSE 0 END) as closed_tickets,
                       AVG(response_time) as avg_response_time,
                       AVG(resolution_time) as avg_resolution_time,
                       AVG(rating) as satisfaction_rate
                FROM tickets
                WHERE DATE(created_at) = DATE('now')
            ''')
            stats = cursor.fetchone()
            cursor.execute('''
                INSERT OR REPLACE INTO statistics 
                (date, new_tickets, closed_tickets, avg_response_time, avg_resolution_time, satisfaction_rate)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (today, stats[0] or 0, stats[1] or 0, stats[2], stats[3], stats[4]))
    
    def get_statistics(self, period: str = 'week') -> Dict:
        cursor = self.conn.cursor()
        if period == 'day':
            date_filter = "DATE(date) = DATE('now')"
        elif period == 'week':
            date_filter = "date >= DATE('now', '-7 days')"
        elif period == 'month':
            date_filter = "date >= DATE('now', '-30 days')"
        else:
            date_filter = "1=1"
        
        cursor.execute(f'''
            SELECT SUM(new_tickets) as total_tickets, SUM(closed_tickets) as total_closed,
                   AVG(avg_response_time) as overall_avg_response,
                   AVG(avg_resolution_time) as overall_avg_resolution,
                   AVG(satisfaction_rate) as overall_satisfaction
            FROM statistics
            WHERE {date_filter}
        ''')
        summary = cursor.fetchone()
        
        cursor.execute(f'''
            SELECT date, new_tickets, closed_tickets, satisfaction_rate
            FROM statistics
            WHERE {date_filter}
            ORDER BY date
        ''')
        daily_stats = [dict(row) for row in cursor.fetchall()]
        
        return {
            'summary': {
                'total_tickets': summary[0] or 0,
                'total_closed': summary[1] or 0,
                'avg_response_time': round(summary[2] or 0, 2),
                'avg_resolution_time': round(summary[3] or 0, 2),
                'satisfaction_rate': round(summary[4] or 0, 2)
            },
            'daily': daily_stats
        }
    
    def log_action(self, user_id: int, action: str, entity_type: str = None, 
                  entity_id: int = None, details: Dict = None):
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO action_logs (user_id, admin_id, action, entity_type, entity_id, details)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                user_id if not self.is_admin(user_id) else None,
                user_id if self.is_admin(user_id) else None,
                action, entity_type, entity_id,
                json.dumps(details) if details else None
            ))
    
    def get_setting(self, key: str, default: Any = None) -> Any:
        cursor = self.conn.cursor()
        cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
        result = cursor.fetchone()
        return result[0] if result else default
    
    def backup_database(self) -> str:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_name = f"backup_{timestamp}.db"
        backup_path = os.path.join(self.backup_dir, backup_name)
        shutil.copy2(self.db_name, backup_path)
        logger.info(f"Backup created: {backup_path}")
        return backup_path
    
    def close(self):
        if self.conn:
            self.conn.close()

# ==================== ИНИЦИАЛИЗАЦИЯ БД ====================

db = Database()

# ==================== RATE LIMITER ====================

class RateLimiter:
    def __init__(self):
        self.requests = {}
    
    def is_allowed(self, user_id: int) -> bool:
        if not config.ENABLE_RATE_LIMIT:
            return True
        now = datetime.now()
        if user_id not in self.requests:
            self.requests[user_id] = []
        self.requests[user_id] = [t for t in self.requests[user_id] if (now - t).seconds < 60]
        if len(self.requests[user_id]) >= config.MAX_REQUESTS_PER_MINUTE:
            return False
        self.requests[user_id].append(now)
        return True

rate_limiter = RateLimiter()

def rate_limit(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not rate_limiter.is_allowed(user_id):
            await update.message.reply_text("⚠️ Слишком много запросов. Пожалуйста, подождите минуту.")
            return
        return await func(update, context)
    return wrapper

# ==================== КЛАВИАТУРЫ ====================

def get_main_keyboard():
    return ReplyKeyboardMarkup([
        [TicketType.PROBLEM.value, TicketType.COMPLAINT.value],
        [TicketType.QUESTION.value, TicketType.SUGGESTION.value],
        ['📋 Мои заявки', '❓ Помощь']
    ], resize_keyboard=True)

def get_admin_keyboard():
    return ReplyKeyboardMarkup([
        ['📋 Все заявки', '⏳ В рассмотрении'],
        ['✅ Принятые', '📤 Переданные'],
        ['❌ Отклоненные', '✅ Завершенные'],
        ['📊 Статистика', '👥 Управление админами'],
        ['🚫 Черный список', '🔍 Поиск'],
        ['⚙️ Настройки', '🔙 В меню']
    ], resize_keyboard=True)

def get_cancel_keyboard():
    return ReplyKeyboardMarkup([['❌ Отмена']], resize_keyboard=True)

def get_phone_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📱 Отправить номер телефона", request_contact=True)],
        ['❌ Отмена']
    ], resize_keyboard=True, one_time_keyboard=True)

def get_priority_keyboard():
    keyboard = [
        [InlineKeyboardButton("🟢 Низкий", callback_data='priority_LOW')],
        [InlineKeyboardButton("🟡 Средний", callback_data='priority_MEDIUM')],
        [InlineKeyboardButton("🟠 Высокий", callback_data='priority_HIGH')],
        [InlineKeyboardButton("🔴 Срочный", callback_data='priority_URGENT')],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_status_keyboard():
    keyboard = [
        [InlineKeyboardButton(TicketStatus.PENDING.value, callback_data='status_PENDING')],
        [InlineKeyboardButton(TicketStatus.ACCEPTED.value, callback_data='status_ACCEPTED')],
        [InlineKeyboardButton(TicketStatus.FORWARDED.value, callback_data='status_FORWARDED')],
        [InlineKeyboardButton(TicketStatus.REJECTED.value, callback_data='status_REJECTED')],
        [InlineKeyboardButton(TicketStatus.COMPLETED.value, callback_data='status_COMPLETED')],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_ticket_keyboard(ticket_id: int):
    keyboard = [
        [
            InlineKeyboardButton("💬 Ответить", callback_data=f'respond_{ticket_id}'),
            InlineKeyboardButton("📊 Статус", callback_data=f'status_{ticket_id}')
        ],
        [
            InlineKeyboardButton("⚡ Приоритет", callback_data=f'priority_{ticket_id}'),
            InlineKeyboardButton("👁 Просмотр", callback_data=f'view_{ticket_id}')
        ],
        [InlineKeyboardButton("📝 Назначить", callback_data=f'assign_{ticket_id}')]
    ]
    return InlineKeyboardMarkup(keyboard)

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

@rate_limit
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if db.is_user_blocked(user.id):
        await update.message.reply_text("❌ Вы были заблокированы в этом боте.")
        return ConversationHandler.END
    
    db.get_or_create_user(user.id, user.username, user.first_name, user.last_name)
    db.update_user_activity(user.id)
    
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "Я бот для отправки заявок. Выберите тип обращения:",
        reply_markup=get_main_keyboard()
    )
    return SELECTING_ACTION

@rate_limit
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📋 *Доступные команды:*

*Для пользователей:*
• Проблема - сообщить о проблеме
• Жалоба - оставить жалобу
• Вопрос - задать вопрос
• Предложение - внести предложение
• Мои заявки - просмотреть свои заявки

*Для админов:*
• /admin - панель администратора
• /stats - просмотр статистики
• /search [текст] - поиск заявок
• /backup - создать бэкап БД

*Требования:*
• Номер телефона обязателен для обратной связи
• Можно прикреплять фото и видео
• Статусы заявок отслеживаются в реальном времени
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

@rate_limit
async def select_ticket_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    
    if db.is_user_blocked(user.id):
        await update.message.reply_text("❌ Вы заблокированы в боте.")
        return SELECTING_ACTION
    
    if text == TicketType.PROBLEM.value:
        context.user_data['ticket_type'] = TicketType.PROBLEM.name
        await update.message.reply_text("Опишите проблему подробно:", reply_markup=get_cancel_keyboard())
        return TYPING_PROBLEM
    elif text == TicketType.COMPLAINT.value:
        context.user_data['ticket_type'] = TicketType.COMPLAINT.name
        await update.message.reply_text("Опишите вашу жалобу подробно:", reply_markup=get_cancel_keyboard())
        return TYPING_COMPLAINT
    elif text == TicketType.QUESTION.value:
        context.user_data['ticket_type'] = TicketType.QUESTION.name
        await update.message.reply_text("Задайте ваш вопрос:", reply_markup=get_cancel_keyboard())
        return TYPING_QUESTION
    elif text == TicketType.SUGGESTION.value:
        context.user_data['ticket_type'] = TicketType.SUGGESTION.name
        await update.message.reply_text("Опишите ваше предложение:", reply_markup=get_cancel_keyboard())
        return TYPING_SUGGESTION
    elif text == '📋 Мои заявки':
        return await show_user_tickets(update, context)
    elif text == '❓ Помощь':
        return await help_command(update, context)
    
    return SELECTING_ACTION

@rate_limit
async def receive_ticket_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['ticket_text'] = update.message.text
    await update.message.reply_text(
        "Пожалуйста, поделитесь вашим номером телефона для обратной связи:\n\n"
        "Нажмите кнопку ниже, чтобы отправить номер, привязанный к Telegram:",
        reply_markup=get_phone_keyboard()
    )
    return WAITING_PHONE

@rate_limit
async def receive_phone_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    
    if contact.user_id != update.effective_user.id:
        await update.message.reply_text(
            "Пожалуйста, поделитесь именно своим номером телефона.",
            reply_markup=get_phone_keyboard()
        )
        return WAITING_PHONE
    
    phone_number = contact.phone_number
    if not phone_number.startswith('+'):
        phone_number = f"+{phone_number}"
    
    context.user_data['phone'] = phone_number
    
    with db.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET phone = ? WHERE user_id = ?', (phone_number, update.effective_user.id))
    
    await update.message.reply_text(
        f"✅ Номер телефона получен: {phone_number}\n\n"
        "Теперь вы можете прикрепить фото или видео к заявке (или отправьте /skip чтобы пропустить):",
        reply_markup=get_cancel_keyboard()
    )
    return WAITING_MEDIA

@rate_limit
async def receive_phone_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text
    
    if phone == '❌ Отмена':
        return await cancel(update, context)
    
    cleaned_phone = ''.join(c for c in phone if c.isdigit() or c == '+')
    if not cleaned_phone.startswith('+'):
        cleaned_phone = f"+{cleaned_phone}"
    
    context.user_data['phone'] = cleaned_phone
    
    with db.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET phone = ? WHERE user_id = ?', (cleaned_phone, update.effective_user.id))
    
    await update.message.reply_text(
        f"✅ Номер телефона получен: {cleaned_phone}\n\n"
        "Теперь вы можете прикрепить фото или видео к заявке (или отправьте /skip чтобы пропустить):",
        reply_markup=get_cancel_keyboard()
    )
    return WAITING_MEDIA

@rate_limit
async def receive_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'media' not in context.user_data:
        context.user_data['media'] = []
    
    if len(context.user_data['media']) >= config.MAX_MEDIA_PER_TICKET:
        await update.message.reply_text(f"❌ Достигнут лимит медиафайлов ({config.MAX_MEDIA_PER_TICKET})")
        return WAITING_MEDIA
    
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_size = update.message.photo[-1].file_size
        context.user_data['media'].append({
            'file_id': file_id, 'file_type': 'photo', 'file_size': file_size
        })
        await update.message.reply_text("📸 Фото добавлено. Можете отправить еще медиа или /done чтобы завершить.")
    elif update.message.video:
        file_id = update.message.video.file_id
        file_size = update.message.video.file_size
        file_name = update.message.video.file_name
        mime_type = update.message.video.mime_type
        context.user_data['media'].append({
            'file_id': file_id, 'file_type': 'video', 'file_size': file_size,
            'file_name': file_name, 'mime_type': mime_type
        })
        await update.message.reply_text("🎥 Видео добавлено. Можете отправить еще медиа или /done чтобы завершить.")
    elif update.message.document:
        file_id = update.message.document.file_id
        file_size = update.message.document.file_size
        file_name = update.message.document.file_name
        mime_type = update.message.document.mime_type
        context.user_data['media'].append({
            'file_id': file_id, 'file_type': 'document', 'file_size': file_size,
            'file_name': file_name, 'mime_type': mime_type
        })
        await update.message.reply_text("📎 Документ добавлен. Можете отправить еще медиа или /done чтобы завершить.")
    
    return WAITING_MEDIA

@rate_limit
async def skip_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await submit_ticket(update, context)

@rate_limit
async def done_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await submit_ticket(update, context)

@rate_limit
async def submit_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    ticket_id = db.create_ticket(
        user_id=user.id,
        ticket_type=context.user_data['ticket_type'],
        description=context.user_data['ticket_text']
    )
    
    ticket = db.get_ticket(ticket_id)
    ticket_number = ticket['ticket_number']
    
    if 'media' in context.user_data:
        for media in context.user_data['media']:
            db.add_media(
                ticket_id=ticket_id,
                file_id=media['file_id'],
                file_type=media['file_type'],
                file_size=media.get('file_size'),
                file_name=media.get('file_name'),
                mime_type=media.get('mime_type')
            )
    
    db.log_action(user.id, 'create_ticket', 'ticket', ticket_id, {'type': context.user_data['ticket_type']})
    
    await send_ticket_to_admins(context, ticket_id, ticket_number)
    
    await update.message.reply_text(
        "✅ *Ваша заявка успешно отправлена!*\n\n"
        f"📋 *Номер заявки:* `{ticket_number}`\n"
        f"📊 *Статус:* {TicketStatus.PENDING.value}\n\n"
        "Следить за статусом можно в разделе 'Мои заявки'.",
        parse_mode='Markdown',
        reply_markup=get_main_keyboard()
    )
    
    context.user_data.clear()
    return SELECTING_ACTION

async def send_ticket_to_admins(context, ticket_id: int, ticket_number: str):
    ticket = db.get_ticket(ticket_id)
    user = db.get_or_create_user(ticket['user_id'])
    
    message_text = f"""
📨 *НОВАЯ ЗАЯВКА* #{ticket_number}

*Тип:* {TicketType[ticket['type']].value}
*Приоритет:* {TicketPriority[ticket['priority']].value}
*От:* {user['first_name']} {user['last_name'] or ''}
*Username:* @{user['username'] or 'нет'}
*ID:* {user['user_id']}
*Телефон:* {user['phone'] or 'не указан'}

*Описание:*
{ticket['description']}

*Дата:* {ticket['created_at']}
    """
    
    media_files = db.get_ticket_media(ticket_id)
    
    try:
        if media_files:
            first_media = media_files[0]
            if first_media['file_type'] == 'photo':
                await context.bot.send_photo(
                    chat_id=ADMIN_CHAT_ID, photo=first_media['file_id'],
                    caption=message_text, parse_mode='Markdown',
                    reply_markup=get_ticket_keyboard(ticket_id)
                )
            elif first_media['file_type'] == 'video':
                await context.bot.send_video(
                    chat_id=ADMIN_CHAT_ID, video=first_media['file_id'],
                    caption=message_text, parse_mode='Markdown',
                    reply_markup=get_ticket_keyboard(ticket_id)
                )
            else:
                await context.bot.send_document(
                    chat_id=ADMIN_CHAT_ID, document=first_media['file_id'],
                    caption=message_text, parse_mode='Markdown',
                    reply_markup=get_ticket_keyboard(ticket_id)
                )
            
            for media in media_files[1:]:
                if media['file_type'] == 'photo':
                    await context.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=media['file_id'])
                elif media['file_type'] == 'video':
                    await context.bot.send_video(chat_id=ADMIN_CHAT_ID, video=media['file_id'])
                else:
                    await context.bot.send_document(chat_id=ADMIN_CHAT_ID, document=media['file_id'])
        else:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID, text=message_text,
                parse_mode='Markdown', reply_markup=get_ticket_keyboard(ticket_id)
            )
    except Exception as e:
        logger.error(f"Error sending to admin chat: {e}")

@rate_limit
async def show_user_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    page = context.user_data.get('tickets_page', 0)
    limit = config.TICKETS_PER_PAGE
    
    tickets = db.get_user_tickets(user.id, limit=limit, offset=page * limit)
    
    if not tickets:
        await update.message.reply_text("📭 У вас пока нет заявок.")
        return SELECTING_ACTION
    
    response = f"📋 *Ваши заявки* (страница {page + 1}):\n\n"
    
    for ticket in tickets:
        status_emoji = {'PENDING': '⏳', 'ACCEPTED': '✅', 'FORWARDED': '📤', 
                       'REJECTED': '❌', 'COMPLETED': '✔️'}.get(ticket['status'], '📌')
        response += (
            f"{status_emoji} *{ticket['ticket_number']}*\n"
            f"📌 *Тип:* {TicketType[ticket['type']].value}\n"
            f"📊 *Статус:* {TicketStatus[ticket['status']].value}\n"
            f"📅 *Дата:* {ticket['created_at'][:16]}\n{'-'*30}\n"
        )
    
    keyboard = []
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data='tickets_prev'))
    if len(tickets) == limit:
        nav_buttons.append(InlineKeyboardButton("Вперед ▶️", callback_data='tickets_next'))
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data='back_to_menu')])
    
    await update.message.reply_text(response, parse_mode='Markdown', 
                                   reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
    return SELECTING_ACTION

@rate_limit
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if not db.is_admin(user.id):
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return SELECTING_ACTION
    
    db.update_admin_activity(user.id)
    role = db.get_admin_role(user.id)
    role_emoji = {'super_admin': '👑', 'admin': '⭐', 'moderator': '🛡️'}.get(role, '👤')
    
    await update.message.reply_text(
        f"{role_emoji} *Панель администратора*\n\n*Роль:* {role}\nВыберите действие:",
        reply_markup=get_admin_keyboard(), parse_mode='Markdown'
    )
    return ADMIN_SELECTING_TICKET

@rate_limit
async def show_all_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_admin(user.id):
        return ADMIN_SELECTING_TICKET
    
    page = context.user_data.get('admin_tickets_page', 0)
    limit = config.TICKETS_PER_PAGE
    tickets = db.get_all_tickets(limit=limit, offset=page * limit)
    
    if not tickets:
        await update.message.reply_text("📭 Заявок пока нет.")
        return ADMIN_SELECTING_TICKET
    
    response = f"📋 *Все заявки* (страница {page + 1}):\n\n"
    for ticket in tickets:
        priority_emoji = {'LOW': '🟢', 'MEDIUM': '🟡', 'HIGH': '🟠', 'URGENT': '🔴'}.get(ticket['priority'], '⚪')
        status_emoji = {'PENDING': '⏳', 'ACCEPTED': '✅', 'FORWARDED': '📤', 
                       'REJECTED': '❌', 'COMPLETED': '✔️'}.get(ticket['status'], '📌')
        response += (
            f"{priority_emoji} {status_emoji} *{ticket['ticket_number']}*\n"
            f"👤 *От:* {ticket['first_name']}\n"
            f"📌 *Тип:* {TicketType[ticket['type']].value}\n"
            f"📊 *Статус:* {TicketStatus[ticket['status']].value}\n"
            f"📅 *Дата:* {ticket['created_at'][:16]}\n{'-'*30}\n"
        )
    
    keyboard = []
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data='admin_tickets_prev'))
    if len(tickets) == limit:
        nav_buttons.append(InlineKeyboardButton("Вперед ▶️", callback_data='admin_tickets_next'))
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='admin_back')])
    
    await update.message.reply_text(response, parse_mode='Markdown', 
                                   reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_SELECTING_TICKET

@rate_limit
async def show_tickets_by_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_admin(user.id):
        return ADMIN_SELECTING_TICKET
    
    status_map = {
        '⏳ В рассмотрении': 'PENDING', '✅ Принятые': 'ACCEPTED',
        '📤 Переданные': 'FORWARDED', '❌ Отклоненные': 'REJECTED',
        '✅ Завершенные': 'COMPLETED'
    }
    status = status_map.get(update.message.text)
    if not status:
        return ADMIN_SELECTING_TICKET
    
    tickets = db.get_all_tickets(status=status, limit=20)
    
    if not tickets:
        await update.message.reply_text(f"📭 Заявок со статусом '{update.message.text}' нет.")
        return ADMIN_SELECTING_TICKET
    
    response = f"📋 *Заявки - {update.message.text}:*\n\n"
    for ticket in tickets:
        response += (
            f"📌 *{ticket['ticket_number']}*\n"
            f"👤 *От:* {ticket['first_name']}\n"
            f"📅 *Дата:* {ticket['created_at'][:16]}\n{'-'*30}\n"
        )
    
    await update.message.reply_text(response, parse_mode='Markdown')
    return ADMIN_SELECTING_TICKET

@rate_limit
async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_admin(user.id):
        return ADMIN_SELECTING_TICKET
    
    db.update_statistics()
    stats_day = db.get_statistics('day')
    stats_week = db.get_statistics('week')
    stats_month = db.get_statistics('month')
    
    response = f"""
📊 *СТАТИСТИКА РАБОТЫ БОТА*

*За сегодня:*
📨 Новых заявок: {stats_day['summary']['total_tickets']}
✅ Закрыто: {stats_day['summary']['total_closed']}
⏱ Среднее время ответа: {stats_day['summary']['avg_response_time']} мин
⭐ Удовлетворенность: {stats_day['summary']['satisfaction_rate']}%

*За неделю:*
📨 Новых заявок: {stats_week['summary']['total_tickets']}
✅ Закрыто: {stats_week['summary']['total_closed']}
⏱ Среднее время ответа: {stats_week['summary']['avg_response_time']} мин
⭐ Удовлетворенность: {stats_week['summary']['satisfaction_rate']}%

*За месяц:*
📨 Новых заявок: {stats_month['summary']['total_tickets']}
✅ Закрыто: {stats_month['summary']['total_closed']}
⏱ Среднее время ответа: {stats_month['summary']['avg_response_time']} мин
⭐ Удовлетворенность: {stats_month['summary']['satisfaction_rate']}%
    """
    
    await update.message.reply_text(response, parse_mode='Markdown')
    return ADMIN_SELECTING_TICKET

@rate_limit
async def show_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_admin(user.id):
        return ADMIN_SELECTING_TICKET
    
    cursor = db.conn.cursor()
    cursor.execute('''
        SELECT b.*, a.first_name as banned_by_name
        FROM blacklist b
        LEFT JOIN admins a ON b.banned_by = a.user_id
        ORDER BY b.banned_at DESC
        LIMIT 20
    ''')
    blacklisted = cursor.fetchall()
    
    if not blacklisted:
        await update.message.reply_text("✅ Черный список пуст.")
        return ADMIN_SELECTING_TICKET
    
    response = "🚫 *Черный список:*\n\n"
    for user_data in blacklisted:
        expires = "навсегда" if user_data[7] else f"до {user_data[6][:16]}"
        response += (
            f"👤 {user_data[2]} (@{user_data[1] or 'нет'})\n"
            f"📝 Причина: {user_data[4]}\n"
            f"⏱ Блокировка: {expires}\n"
            f"👮 Заблокировал: {user_data[9]}\n{'-'*30}\n"
        )
    
    await update.message.reply_text(response, parse_mode='Markdown')
    return ADMIN_SELECTING_TICKET

@rate_limit
async def search_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_admin(user.id):
        return ADMIN_SELECTING_TICKET
    
    await update.message.reply_text(
        "🔍 Введите текст для поиска (номер заявки, текст или имя пользователя):",
        reply_markup=get_cancel_keyboard()
    )
    return ADMIN_SEARCH

@rate_limit
async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text
    
    if query_text == '❌ Отмена':
        return await cancel(update, context)
    
    results = db.search_tickets(query_text)
    
    if not results:
        await update.message.reply_text("❌ Ничего не найдено.", reply_markup=get_admin_keyboard())
        return ADMIN_SELECTING_TICKET
    
    response = f"🔍 *Результаты поиска* (найдено: {len(results)}):\n\n"
    for ticket in results[:10]:
        response += (
            f"📌 *{ticket['ticket_number']}*\n"
            f"👤 *От:* {ticket['first_name']}\n"
            f"📊 *Статус:* {TicketStatus[ticket['status']].value}\n"
            f"📝 *Текст:* {ticket['description'][:100]}...\n{'-'*30}\n"
        )
    
    await update.message.reply_text(response, parse_mode='Markdown')
    return ADMIN_SELECTING_TICKET

# ==================== CALLBACK ОБРАБОТЧИКИ ====================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user = query.from_user
    
    if data == 'tickets_next':
        context.user_data['tickets_page'] = context.user_data.get('tickets_page', 0) + 1
        await show_user_tickets(update, context)
        return
    elif data == 'tickets_prev':
        context.user_data['tickets_page'] = max(0, context.user_data.get('tickets_page', 0) - 1)
        await show_user_tickets(update, context)
        return
    elif data == 'back_to_menu':
        await query.message.reply_text("Выберите действие:", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    if not db.is_admin(user.id):
        await query.message.reply_text("❌ У вас нет прав администратора.")
        return
    
    if data == 'admin_tickets_next':
        context.user_data['admin_tickets_page'] = context.user_data.get('admin_tickets_page', 0) + 1
        await show_all_tickets(update, context)
        return
    elif data == 'admin_tickets_prev':
        context.user_data['admin_tickets_page'] = max(0, context.user_data.get('admin_tickets_page', 0) - 1)
        await show_all_tickets(update, context)
        return
    elif data == 'admin_back':
        await admin_panel(update, context)
        return
    
    if data.startswith('view_'):
        ticket_id = int(data.split('_')[1])
        await view_ticket_details(query, ticket_id)
    elif data.startswith('respond_'):
        ticket_id = int(data.split('_')[1])
        context.user_data['current_ticket_id'] = ticket_id
        await query.message.reply_text(
            f"💬 Введите ответ на заявку #{ticket_id}:",
            reply_markup=get_cancel_keyboard()
        )
        return ADMIN_TYPING_RESPONSE
    elif data.startswith('priority_'):
        parts = data.split('_')
        if len(parts) == 3:
            ticket_id, priority = int(parts[1]), parts[2]
            await update_ticket_priority(query, ticket_id, priority)
        else:
            await show_priority_options(query, int(parts[1]))
    elif data.startswith('status_'):
        parts = data.split('_')
        if len(parts) == 3:
            ticket_id, status = int(parts[1]), parts[2]
            await update_ticket_status_callback(query, ticket_id, status)
        else:
            await show_status_options(query, int(parts[1]))
    elif data.startswith('assign_'):
        ticket_id = int(data.split('_')[1])
        await assign_ticket_to_self(query, ticket_id)

async def view_ticket_details(query, ticket_id: int):
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        await query.message.reply_text("❌ Заявка не найдена.")
        return
    
    media = db.get_ticket_media(ticket_id)
    responses = db.get_ticket_responses(ticket_id, include_internal=True)
    
    priority_emoji = {'LOW': '🟢', 'MEDIUM': '🟡', 'HIGH': '🟠', 'URGENT': '🔴'}.get(ticket['priority'], '⚪')
    status_emoji = {'PENDING': '⏳', 'ACCEPTED': '✅', 'FORWARDED': '📤', 
                   'REJECTED': '❌', 'COMPLETED': '✔️'}.get(ticket['status'], '📌')
    
    response = f"""
📄 *Детали заявки* {ticket['ticket_number']}

{priority_emoji} *Приоритет:* {TicketPriority[ticket['priority']].value}
{status_emoji} *Статус:* {TicketStatus[ticket['status']].value}
📌 *Тип:* {TicketType[ticket['type']].value}

👤 *Информация о пользователе:*
• Имя: {ticket['first_name']} {ticket['last_name'] or ''}
• Username: @{ticket['username'] or 'нет'}
• Телефон: {ticket['phone'] or 'не указан'}

📝 *Описание:*
{ticket['description']}

📅 *Создано:* {ticket['created_at']}
🔄 *Обновлено:* {ticket['updated_at']}
    """
    
    if ticket['assigned_to']:
        response += f"👨‍💼 *Назначено:* {ticket['admin_first_name']}\n"
    if ticket['response_time']:
        response += f"⏱ *Время первого ответа:* {ticket['response_time']} мин\n"
    if ticket['resolution_time']:
        response += f"✅ *Время решения:* {ticket['resolution_time']} мин\n"
    if media:
        response += f"\n📎 *Медиафайлы:* {len(media)} шт.\n"
    
    if responses:
        response += "\n💬 *История переписки:*\n"
        for resp in responses:
            author = resp['admin_name'] or resp['user_name'] or 'Система'
            if resp['response_type'] == 'internal':
                response += f"\n🔒 *{author}* (внутренне):\n{resp['response_text']}\n"
            else:
                response += f"\n💬 *{author}* ({resp['created_at'][:16]}):\n{resp['response_text']}\n"
    
    if len(response) > 4000:
        for part in [response[i:i+4000] for i in range(0, len(response), 4000)]:
            await query.message.reply_text(part, parse_mode='Markdown')
    else:
        await query.message.reply_text(response, parse_mode='Markdown')

async def show_priority_options(query, ticket_id: int):
    await query.message.reply_text(
        f"⚡ Выберите приоритет для заявки #{ticket_id}:",
        reply_markup=get_priority_keyboard()
    )

async def show_status_options(query, ticket_id: int):
    await query.message.reply_text(
        f"📊 Выберите новый статус для заявки #{ticket_id}:",
        reply_markup=get_status_keyboard()
    )

async def update_ticket_priority(query, ticket_id: int, priority: str):
    with db.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE tickets SET priority = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', 
                      (priority, ticket_id))
    
    db.log_action(query.from_user.id, 'change_priority', 'ticket', ticket_id, {'priority': priority})
    await query.message.reply_text(f"✅ Приоритет заявки #{ticket_id} изменен на '{TicketPriority[priority].value}'")

async def update_ticket_status_callback(query, ticket_id: int, status: str):
    db.update_ticket_status(ticket_id, status, query.from_user.id)
    ticket = db.get_ticket(ticket_id)
    
    if ticket and ticket['user_id'] and config.NOTIFY_STATUS_CHANGE:
        try:
            status_text = TicketStatus[status].value
            await query.bot.send_message(
                chat_id=ticket['user_id'],
                text=f"📢 *Обновление статуса заявки {ticket['ticket_number']}*\n\nНовый статус: *{status_text}*",
                parse_mode='Markdown'
            )
            db.create_notification(ticket['user_id'], ticket_id, 'status_change', 
                                  'Статус заявки изменен', f'Статус изменен на {status_text}')
        except Exception as e:
            logger.error(f"Could not notify user {ticket['user_id']}: {e}")
    
    await query.message.reply_text(f"✅ Статус заявки #{ticket_id} изменен на '{TicketStatus[status].value}'")

async def assign_ticket_to_self(query, ticket_id: int):
    admin_id = query.from_user.id
    db.assign_ticket(ticket_id, admin_id)
    db.log_action(admin_id, 'assign_ticket', 'ticket', ticket_id)
    await query.message.reply_text(f"✅ Заявка #{ticket_id} назначена вам.")

@rate_limit
async def admin_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    response_text = update.message.text
    ticket_id = context.user_data.get('current_ticket_id')
    user = update.effective_user
    
    if not ticket_id:
        await update.message.reply_text("❌ Ошибка: не найдена заявка для ответа.")
        return ADMIN_SELECTING_TICKET
    
    db.add_response(ticket_id, response_text, admin_id=user.id, response_type='public')
    ticket = db.get_ticket(ticket_id)
    
    if ticket and config.NOTIFY_RESPONSES:
        try:
            await context.bot.send_message(
                chat_id=ticket['user_id'],
                text=f"📨 *Ответ на вашу заявку {ticket['ticket_number']}*\n\n"
                     f"*Администратор:* {user.first_name}\n*Ответ:*\n{response_text}",
                parse_mode='Markdown'
            )
            db.create_notification(ticket['user_id'], ticket_id, 'response', 
                                  'Получен ответ на заявку', response_text[:100])
        except Exception as e:
            logger.error(f"Could not send response to user {ticket['user_id']}: {e}")
    
    await update.message.reply_text(f"✅ Ответ на заявку #{ticket_id} отправлен.", reply_markup=get_admin_keyboard())
    context.user_data.pop('current_ticket_id', None)
    return ADMIN_SELECTING_TICKET

@rate_limit
async def manage_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_admin(user.id):
        return ADMIN_SELECTING_TICKET
    
    role = db.get_admin_role(user.id)
    if role not in [AdminRole.SUPER_ADMIN.value, AdminRole.ADMIN.value]:
        await update.message.reply_text("❌ У вас нет прав для управления администраторами.")
        return ADMIN_SELECTING_TICKET
    
    admins = db.get_all_admins()
    response = "👥 *Список администраторов:*\n\n"
    keyboard = []
    role_emoji = {'super_admin': '👑', 'admin': '⭐', 'moderator': '🛡️'}
    
    for admin in admins:
        emoji = role_emoji.get(admin['role'], '👤')
        response += f"{emoji} *{admin['first_name']}* (@{admin['username'] or 'нет'})\n"
        response += f"   • Роль: {admin['role']}\n"
        response += f"   • Ответов: {admin['total_responses']}\n"
        response += f"   • Назначено: {admin['total_assigned']}\n\n"
        
        if role == AdminRole.SUPER_ADMIN.value and admin['role'] != 'super_admin':
            keyboard.append([InlineKeyboardButton(f"❌ Удалить {admin['first_name']}", 
                                                callback_data=f'remove_admin_{admin["user_id"]}')])
    
    response += "\n*Команды:*\n/add_admin [ID] [role] - добавить администратора"
    
    if keyboard:
        await update.message.reply_text(response, parse_mode='Markdown', 
                                       reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(response, parse_mode='Markdown')
    
    return ADMIN_MANAGING_USERS

@rate_limit
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    role = db.get_admin_role(user.id)
    
    if role != AdminRole.SUPER_ADMIN.value:
        await update.message.reply_text("❌ Только суперадмин может добавлять администраторов.")
        return ADMIN_MANAGING_USERS
    
    if not context.args:
        await update.message.reply_text("Использование: /add_admin [ID_пользователя] [role]")
        return ADMIN_MANAGING_USERS
    
    try:
        new_admin_id = int(context.args[0])
        new_role = context.args[1] if len(context.args) > 1 else 'moderator'
        if new_role not in [r.value for r in AdminRole]:
            await update.message.reply_text("❌ Неверная роль. Допустимые: super_admin, admin, moderator")
            return ADMIN_MANAGING_USERS
    except ValueError:
        await update.message.reply_text("❌ ID пользователя должен быть числом.")
        return ADMIN_MANAGING_USERS
    
    try:
        new_admin_user = await context.bot.get_chat(new_admin_id)
        success = db.add_admin(new_admin_id, new_admin_user.username, new_admin_user.first_name, user.id, new_role)
        
        if success:
            await update.message.reply_text(f"✅ Пользователь {new_admin_user.first_name} добавлен как {new_role}.")
            try:
                await context.bot.send_message(
                    chat_id=new_admin_id,
                    text=f"🎉 Вам предоставлены права администратора!\nРоль: {new_role}\nКоманда: /admin"
                )
            except Exception as e:
                logger.error(f"Could not notify new admin {new_admin_id}: {e}")
        else:
            await update.message.reply_text("❌ Не удалось добавить администратора.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    return ADMIN_MANAGING_USERS

async def remove_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.message.reply_text("❌ Только суперадмин может удалять администраторов.")
        return
    
    admin_id = int(query.data.split('_')[2])
    if admin_id == SUPER_ADMIN_ID:
        await query.message.reply_text("❌ Нельзя удалить суперадмина.")
        return
    
    success = db.remove_admin(admin_id, query.from_user.id)
    if success:
        await query.message.reply_text("✅ Администратор удален.")
        try:
            await context.bot.send_message(chat_id=admin_id, text="ℹ Ваши права администратора отозваны.")
        except Exception as e:
            logger.error(f"Could not notify removed admin {admin_id}: {e}")
    else:
        await query.message.reply_text("❌ Не удалось удалить администратора.")
    
    return ADMIN_MANAGING_USERS

@rate_limit
async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != SUPER_ADMIN_ID:
        await update.message.reply_text("❌ Только суперадмин может создавать бэкапы.")
        return
    
    await update.message.reply_text("🔄 Создание бэкапа базы данных...")
    try:
        backup_path = db.backup_database()
        await update.message.reply_document(
            document=open(backup_path, 'rb'),
            filename=os.path.basename(backup_path),
            caption="✅ Бэкап базы данных создан"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при создании бэкапа: {e}")

@rate_limit
async def show_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != SUPER_ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа")
        return
    
    config_info = f"""
📋 *Текущая конфигурация:*

*Бот:*
• Токен: {config.BOT_TOKEN[:10]}...{config.BOT_TOKEN[-5:]}
• Язык: {config.BOT_LANGUAGE}
• Таймзона: {config.BOT_TIMEZONE}

*База данных:*
• Путь: {config.DB_PATH}
• Бэкапы: {config.DB_BACKUP_DIR}
• Хранение бэкапов: {config.DB_BACKUP_DAYS} дней

*Администраторы:*
• Чат заявок: {config.ADMIN_CHAT_ID}
• Суперадмин: {config.SUPER_ADMIN_ID}

*Ограничения:*
• Медиа на заявку: {config.MAX_MEDIA_PER_TICKET}
• Длина текста: {config.MAX_TICKET_TEXT_LENGTH}
• Заявок на странице: {config.TICKETS_PER_PAGE}

*Уведомления:*
• Новые заявки: {config.NOTIFY_NEW_TICKETS}
• Смена статуса: {config.NOTIFY_STATUS_CHANGE}
• Ответы: {config.NOTIFY_RESPONSES}

*Безопасность:*
• Rate limit: {config.ENABLE_RATE_LIMIT}
• Запросов в минуту: {config.MAX_REQUESTS_PER_MINUTE}
• Блокировка: {config.BLOCK_DURATION_HOURS} часов
    """
    
    await update.message.reply_text(config_info, parse_mode='Markdown')
    return ADMIN_SELECTING_TICKET

@rate_limit
async def reload_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != SUPER_ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа")
        return
    
    # Перезагружаем переменные окружения
    from dotenv import load_dotenv
    load_dotenv(override=True)
    
    # Обновляем конфиг
    global config
    config = Config()
    
    await update.message.reply_text("✅ Конфигурация перезагружена")
    return ADMIN_SELECTING_TICKET

@rate_limit
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_admin(user.id):
        return ADMIN_SELECTING_TICKET
    
    settings_keyboard = [
        [InlineKeyboardButton("📝 Макс. медиа", callback_data='set_max_media')],
        [InlineKeyboardButton("📄 Заявок на странице", callback_data='set_per_page')],
        [InlineKeyboardButton("🔔 Уведомления", callback_data='set_notifications')],
        [InlineKeyboardButton("🔙 Назад", callback_data='admin_back')]
    ]
    
    await update.message.reply_text(
        "⚙️ *Настройки бота*\n\nВыберите параметр для изменения:",
        reply_markup=InlineKeyboardMarkup(settings_keyboard),
        parse_mode='Markdown'
    )
    return ADMIN_SELECTING_TICKET

# ==================== КОМАНДА ОТМЕНЫ ====================

@rate_limit
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data.clear()
    
    if db.is_admin(user.id):
        await update.message.reply_text("❌ Действие отменено.", reply_markup=get_admin_keyboard())
        return ADMIN_SELECTING_TICKET
    else:
        await update.message.reply_text("❌ Действие отменено.", reply_markup=get_main_keyboard())
        return SELECTING_ACTION

# ==================== ГЛАВНАЯ ФУНКЦИЯ ====================

def main():
    """Главная функция запуска бота"""
    
    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN не найден в .env файле!")
        print("❌ Ошибка: BOT_TOKEN не найден в .env файле!")
        print("Пожалуйста, создайте файл .env и добавьте BOT_TOKEN=ваш_токен")
        return
    
    # Создаем приложение
    application = Application.builder().token(config.BOT_TOKEN).build()
    
    # Обработчик диалога для пользователей
    user_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('help', help_command),
            MessageHandler(filters.TEXT & ~filters.COMMAND, select_ticket_type)
        ],
        states={
            SELECTING_ACTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, select_ticket_type),
                CallbackQueryHandler(button_callback)
            ],
            TYPING_PROBLEM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ticket_text)
            ],
            TYPING_COMPLAINT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ticket_text)
            ],
            TYPING_QUESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ticket_text)
            ],
            TYPING_SUGGESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ticket_text)
            ],
            WAITING_PHONE: [
                MessageHandler(filters.CONTACT, receive_phone_contact),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone_manual)
            ],
            WAITING_MEDIA: [
                MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL, receive_media),
                CommandHandler('skip', skip_media),
                CommandHandler('done', done_media)
            ],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            MessageHandler(filters.Regex('^❌ Отмена$'), cancel)
        ],
        map_to_parent={
            SELECTING_ACTION: SELECTING_ACTION,
        }
    )
    
    # Обработчик диалога для админов
    admin_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('admin', admin_panel),
            CommandHandler('stats', show_statistics),
            CommandHandler('backup', backup_command),
            CommandHandler('search', search_tickets),
            CommandHandler('config', show_config),
            CommandHandler('reload', reload_config)
        ],
        states={
            ADMIN_SELECTING_TICKET: [
                MessageHandler(filters.Regex('^📋 Все заявки$'), show_all_tickets),
                MessageHandler(filters.Regex('^(⏳ В рассмотрении|✅ Принятые|📤 Переданные|❌ Отклоненные|✅ Завершенные)$'), 
                             show_tickets_by_status),
                MessageHandler(filters.Regex('^📊 Статистика$'), show_statistics),
                MessageHandler(filters.Regex('^👥 Управление админами$'), manage_admins),
                MessageHandler(filters.Regex('^🚫 Черный список$'), show_blacklist),
                MessageHandler(filters.Regex('^🔍 Поиск$'), search_tickets),
                MessageHandler(filters.Regex('^⚙️ Настройки$'), settings_menu),
                MessageHandler(filters.Regex('^🔙 В меню$'), start),
                CallbackQueryHandler(button_callback)
            ],
            ADMIN_TYPING_RESPONSE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_response)
            ],
            ADMIN_MANAGING_USERS: [
                CommandHandler('add_admin', add_admin_command),
                CallbackQueryHandler(remove_admin_callback, pattern='^remove_admin_'),
                MessageHandler(filters.Regex('^🔙 В меню$'), start)
            ],
            ADMIN_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, perform_search)
            ],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            MessageHandler(filters.Regex('^❌ Отмена$'), cancel),
            CommandHandler('admin', admin_panel)
        ],
        map_to_parent={
            SELECTING_ACTION: SELECTING_ACTION,
        }
    )
    
    # Добавляем обработчики
    application.add_handler(user_conv_handler)
    application.add_handler(admin_conv_handler)
    
    # Добавляем обработчик для неизвестных команд
    async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "❌ Неизвестная команда. Используйте /help для списка доступных команд."
        )
    
    application.add_handler(MessageHandler(filters.COMMAND, unknown))
    
    # Запуск бота
    logger.info("🚀 Бот запущен...")
    logger.info(f"📊 База данных: {config.DB_PATH}")
    logger.info(f"👑 Суперадмин ID: {config.SUPER_ADMIN_ID}")
    logger.info(f"📨 Чат заявок: {config.ADMIN_CHAT_ID}")
    
    print(f"""
╔════════════════════════════════════╗
║     🚀 БОТ ПОДДЕРЖКИ ЗАПУЩЕН       ║
╠════════════════════════════════════╣
║ 📊 База данных: {config.DB_NAME}          ║
║ 👑 Суперадмин: {config.SUPER_ADMIN_ID}      ║
║ 📨 Чат заявок: {config.ADMIN_CHAT_ID}   ║
║ 🌐 Язык: {config.BOT_LANGUAGE}                      ║
║ 🔒 Rate limit: {config.ENABLE_RATE_LIMIT}                   ║
╚════════════════════════════════════╝
    """)
    
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        # Закрываем соединение с БД при остановке
        db.close()
        logger.info("👋 Бот остановлен")
        print("👋 Бот остановлен")

if __name__ == '__main__':
    main()