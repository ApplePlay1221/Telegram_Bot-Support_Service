import subprocess
import time
import psutil

# Конфигурация
BOT_SCRIPT = "main.py"  # Имя файла с ботом
CHECK_INTERVAL = 3600  # Проверка каждые 1 час
LOG_FILE = "bot_restarter.log"  # Файл для логов

def is_bot_running():

    for proc in psutil.process_iter(['cmdline']):
        if proc.info['cmdline'] and 'python' in proc.info['cmdline'][0] and BOT_SCRIPT in proc.info['cmdline'][1]:
            return True
    return False

def start_bot():

    subprocess.Popen(["python", BOT_SCRIPT])
    log_message(f"Бот перезапущен в {time.strftime('%Y-%m-%d %H:%M:%S')}")

def log_message(message):

    with open(LOG_FILE, "a") as f:
        f.write(f"{message}\n")
    print(message)

def main():
    log_message(f"🔄 Рестартер бота запущен. Проверка каждые {CHECK_INTERVAL} секунд.")
    
    while True:
        if not is_bot_running():
            log_message("⚠️ Бот не работает. Перезапуск...")
            start_bot()
        else:
            log_message("✅ Бот работает нормально.")
        
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()