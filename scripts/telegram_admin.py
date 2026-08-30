"""
Telegram Full Remote Control Bot (Remote Terminal & IDE).
Gives full control over PC/Laptop from your Smartphone:
- Execute ONLY whitelisted PowerShell/terminal commands (/cmd <command>)
- Read files ONLY inside project, excluding sensitive paths (/read <path>)
- File System Explorer (/ls)
- Run Backtests, Retrain Models, Fetch History directly
- Interactive Buttons & Live MT5 Balance
- View logs (/logs)
- View safe config (/config)
- View open positions (/positions)
"""

import logging
import os
import subprocess
import sys
import time
from datetime import datetime

import MetaTrader5 as mt5
import telegram

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config.loader import get_env, load_config
from execution.mt5_trader import positions_get_by_magic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("telegram_remote")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)

BOT_TOKEN = get_env("TELEGRAM_ADMIN_BOT_TOKEN", required=False) or get_env("TELEGRAM_BOT_TOKEN", required=True)
MY_CHAT_ID = int(get_env("TELEGRAM_CHAT_ID", required=True))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.path.join(BASE_DIR, "logs", "trader.pid")
TRADER_PROCESS = None

# --- Безопасность ---
# HIGH 23: the /cmd handler must NEVER invoke a shell. Commands are allowed only if
# they parse to a restricted argv: either a fixed builtin (handled in Python) or
# `python -m <module> [args...]` where <module> is in the allowlist below. Any shell
# metacharacter (; | & > < ` $ ( ) * ? [ ] quotes, newline) is rejected outright.
_SHELL_METACHARS = set(";&|<>`$(){}*?[]'\"\n\r")

# Modules runnable via `/cmd python -m <module> ...`
ALLOWED_CMD_MODULES = {
    "execution.mt5_trader",
    "scripts.summary_report",
    "scripts.run_backtest",
    "scripts.train_all_assets",
    "scripts.retrain_models",
    "scripts.backfill",
}

# Builtin commands implemented directly in Python (no shell involved).
ALLOWED_BUILTIN_CMDS = {"dir", "ls", "pwd", "echo"}

FORBIDDEN_SUBSTRINGS = [
    ".env",
    "backup",
    ".sqlite",
    ".joblib",
    ".log",
    ".pid",
    "output",
    "logs",
    "models",
    "__pycache__",
    ".git",
]

ALLOWED_READ_EXTENSIONS = {".py", ".yaml", ".yml", ".json", ".md", ".txt", ".cfg", ".ini"}
MAX_OUTPUT_LENGTH = 3500
MAX_LOG_LINES = 50


def log_action(user_id: int, action: str, details: str = ""):
    log_line = f"{datetime.now().isoformat()} | user={user_id} | action={action} | {details}"
    logger.info(log_line)
    log_path = os.path.join(BASE_DIR, "logs", "telegram_actions.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception:
        pass


def is_safe_path(path: str) -> bool:
    full_path = os.path.abspath(path)
    if not full_path.startswith(BASE_DIR):
        return False
    normalized = full_path.replace("\\", "/").lower()
    for forbidden in FORBIDDEN_SUBSTRINGS:
        if forbidden.lower() in normalized:
            return False
    return True


def parse_command(command: str) -> list[str] | None:
    """Tokenize a /cmd command into an argv list.

    Returns None if the command contains shell metacharacters or cannot be parsed,
    otherwise the whitespace-split argv (already validated to be metachar-free).
    """
    if any(ch in command for ch in _SHELL_METACHARS):
        return None
    parts = command.strip().split()
    if not parts:
        return None
    return parts


def is_allowed_command(command: str) -> bool:
    argv = parse_command(command)
    if argv is None:
        return False
    head = argv[0].lower()
    if head in ALLOWED_BUILTIN_CMDS:
        return True
    if head == "python" and len(argv) >= 3 and argv[1] == "-m":
        return argv[2] in ALLOWED_CMD_MODULES
    return False


def save_pid(pid: int):
    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w") as f:
            f.write(str(pid))
    except Exception as e:
        logger.error(f"Failed to save PID: {e}")


def load_pid() -> int | None:
    try:
        with open(PID_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def is_process_running(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def get_robot_status() -> tuple:
    global TRADER_PROCESS
    if TRADER_PROCESS and TRADER_PROCESS.poll() is None:
        return "🟢 РАБОТАЕТ", TRADER_PROCESS.pid
    pid = load_pid()
    if pid and is_process_running(pid):
        TRADER_PROCESS = None
        return f"🟢 РАБОТАЕТ (PID: {pid}, но управление недоступно после перезапуска бота)", pid
    return "🔴 ОСТАНОВЛЕН", None


def get_main_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("▶️ Запустить", callback_data="start_trader"),
            InlineKeyboardButton("⏹ Остановить", callback_data="stop_trader"),
            InlineKeyboardButton("🔄 Перезапустить", callback_data="restart_trader"),
        ],
        [
            InlineKeyboardButton("📊 Баланс MT5", callback_data="status_mt5"),
            InlineKeyboardButton("🏆 Отчет PnL", callback_data="report_pnl"),
        ],
        [
            InlineKeyboardButton("📊 Позиции", callback_data="view_positions"),
            InlineKeyboardButton("🔄 Обновить", callback_data="refresh_positions"),
        ],
        [
            InlineKeyboardButton("📄 Логи", callback_data="view_logs"),
            InlineKeyboardButton("⚙️ Конфиг", callback_data="view_config"),
        ],
        [
            InlineKeyboardButton("🚀 Обучить Модели", callback_data="cmd_train_all"),
            InlineKeyboardButton("🔥 Переобучить Модели", callback_data="cmd_retrain"),
        ],
        [
            InlineKeyboardButton("📈 Бэктест", callback_data="cmd_backtest_all"),
            InlineKeyboardButton("📂 Файлы (/ls)", callback_data="cmd_ls"),
        ],
        [
            InlineKeyboardButton("🛡 Безопасность", callback_data="cmd_help"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_back_keyboard():
    keyboard = get_main_keyboard()
    rows = [list(row) for row in keyboard.inline_keyboard]
    rows.append(
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="back"),
            InlineKeyboardButton("🏠 Главное меню", callback_data="home"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def safe_edit_message_text(query, text: str, parse_mode=None, reply_markup=None):
    """Безопасное редактирование сообщения с обработкой 'Message is not modified'."""
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e):
            await query.answer("⚠️ Контент уже такой.", show_alert=False)
        else:
            raise


async def is_authorized(update: Update) -> bool:
    return update.effective_user.id == MY_CHAT_ID


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return

    status_str, pid = get_robot_status()
    msg = (
        f"💻 **УПРАВЛЕНИЕ ПК ЧЕРЕЗ TELEGRAM**\n\n"
        f"• Статус робота: **{status_str}**\n"
        f"• Безопасный режим активирован!\n\n"
        f"**Доступные команды:**\n"
        f"• `/cmd <команда>` — только разрешённые команды\n"
        f"• `/read <путь>` — чтение файлов проекта (без секретов)\n"
        f"• `/logs` — последние {MAX_LOG_LINES} строк лога\n"
        f"• `/config` — конфигурация активов (без секретов)\n"
        f"• `/positions` — открытые позиции\n"
        f"• `/ls` — список файлов\n"
        f"• *Загрузка файла* — только `.py`/`.yaml` в безопасные папки"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_main_keyboard())


def _run_safe_command(argv: list[str]) -> str:
    """HIGH 23: exec without a shell. Builtins handled in-process."""
    head = argv[0].lower()
    if head in ALLOWED_BUILTIN_CMDS:
        if head == "pwd":
            out = os.getcwd()
        elif head in ("dir", "ls"):
            target = argv[1] if len(argv) > 1 else "."
            if os.path.isdir(target):
                entries = sorted(os.listdir(target))
                out = "\n".join(entries) if entries else "(empty)"
            else:
                out = f"Not a directory: {target}"
        elif head == "echo":
            out = " ".join(argv[1:])
        else:  # pragma: no cover - defensive
            out = ""
        return out
    # All other allowed commands are `python -m <module>` (validated earlier).
    result = subprocess.run(
        [sys.executable, "-m", *argv[2:]],
        cwd=os.getcwd(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    return (
        result.stdout or result.stderr or "✅ Команда выполнена без вывода."
    ).strip() or "✅ Команда выполнена без вывода."


async def cmd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return

    command = " ".join(context.args)
    if not command:
        await update.message.reply_text(
            "⚠️ Укажите команду. Пример: `/cmd python -m scripts.summary_report`", parse_mode="Markdown"
        )
        return

    if not is_allowed_command(command):
        log_action(update.effective_user.id, "cmd_denied", command)
        await update.message.reply_text(
            "⛔ Команда не входит в список разрешённых.\nДоступны: summary_report, run_backtest, train_all_assets, retrain_models, execution.mt5_trader, базовые команды (dir, echo)."
        )
        return

    await update.message.reply_text(f"⏳ Выполняю: `{command}`...", parse_mode="Markdown")
    log_action(update.effective_user.id, "cmd", command)

    argv = parse_command(command)
    if argv is None:
        await update.message.reply_text("⛔ Команда отклонена: содержит недопустимые символы.")
        return

    try:
        output = _run_safe_command(argv)
        if len(output) > MAX_OUTPUT_LENGTH:
            output = output[-MAX_OUTPUT_LENGTH:]
        await update.message.reply_text(f"```\n{output}\n```", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def read_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return

    filepath = " ".join(context.args)
    if not filepath:
        await update.message.reply_text("⚠️ Укажите путь. Пример: `/read config/config.yaml`", parse_mode="Markdown")
        return

    if not is_safe_path(filepath):
        log_action(update.effective_user.id, "read_denied", filepath)
        await update.message.reply_text("⛔ Доступ к этому файлу запрещён по соображениям безопасности.")
        return

    if not os.path.exists(filepath):
        await update.message.reply_text(f"❌ Файл `{filepath}` не найден.", parse_mode="Markdown")
        return

    ext = os.path.splitext(filepath)[1].lower()
    if ext not in ALLOWED_READ_EXTENSIONS:
        await update.message.reply_text(f"⛔ Расширение `{ext}` не поддерживается для чтения.")
        return

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        if len(content) > MAX_OUTPUT_LENGTH:
            await update.message.reply_document(document=open(filepath, "rb"), caption=f"📄 Файл `{filepath}`")
        else:
            await update.message.reply_text(f"📄 **Файл `{filepath}`:**\n```\n{content}\n```", parse_mode="Markdown")
        log_action(update.effective_user.id, "read", filepath)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка чтения: {e}")


async def write_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⛔ Команда `/write` отключена для безопасности.")


async def ls_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return

    path = " ".join(context.args) or "."
    if not is_safe_path(path):
        await update.message.reply_text("⛔ Доступ к этой папке запрещён.")
        return

    try:
        files = os.listdir(path)
        items = []
        for f in files:
            full_p = os.path.join(path, f)
            icon = "📁" if os.path.isdir(full_p) else "📄"
            items.append(f"{icon} {f}")
        msg = f"📂 **Содержимое `{path}`:**\n\n" + "\n".join(items[:40])
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return

    doc = update.message.document
    file_name = doc.file_name or ""
    ext = os.path.splitext(file_name)[1].lower()
    if ext not in {".py", ".yaml", ".yml"}:
        await update.message.reply_text("⛔ Можно загружать только `.py`, `.yaml`, `.yml` файлы.")
        return

    target_path = None
    for root, dirs, files in os.walk(BASE_DIR):
        if root.startswith(os.path.join(BASE_DIR, "venv")):
            continue
        if file_name in files:
            target_path = os.path.join(root, file_name)
            break
    if target_path is None:
        target_path = (
            os.path.join(BASE_DIR, "scripts", file_name)
            if file_name.endswith(".py")
            else os.path.join(BASE_DIR, "config", file_name)
        )
        os.makedirs(os.path.dirname(target_path), exist_ok=True)

    if not is_safe_path(target_path):
        await update.message.reply_text("⛔ Целевой путь запрещён.")
        return

    await update.message.reply_text(f"📥 Сохраняю файл `{file_name}`...", parse_mode="Markdown")

    try:
        file = await context.bot.get_file(doc.file_id)
        await file.download_to_drive(target_path)
        await update.message.reply_text(f"✅ Файл сохранён в `{target_path}`", parse_mode="Markdown")
        log_action(update.effective_user.id, "upload", target_path)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка сохранения: {e}")


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE, use_query: bool = False):
    if not await is_authorized(update):
        return

    if not mt5.initialize():
        text = "❌ MT5 не подключён."
        if use_query and update.callback_query:
            await safe_edit_message_text(update.callback_query, text, reply_markup=get_back_keyboard())
        else:
            await update.message.reply_text(text)
        return

    positions = positions_get_by_magic(magic=777111)
    if not positions:
        text = "📭 Открытых позиций нет."
    else:
        lines = ["📊 **ОТКРЫТЫЕ ПОЗИЦИИ:**\n"]
        for p in positions:
            p_type = "BUY 🟢" if p.type == 0 else "SELL 🔴"
            tick = mt5.symbol_info_tick(p.symbol)
            price = tick.bid if p.type == 0 else tick.ask
            pnl = p.profit + p.swap
            lines.append(
                f"• **{p.symbol}** {p_type}\n"
                f"  Вход: `{p.price_open}` | Текущая: `{price}`\n"
                f"  SL: `{p.sl}` | TP: `{p.tp}`\n"
                f"  PnL: `${pnl:.2f}`"
            )
        text = "\n".join(lines)

    keyboard = get_back_keyboard()
    rows = [list(row) for row in keyboard.inline_keyboard]
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="refresh_positions")])
    keyboard = InlineKeyboardMarkup(rows)

    if use_query and update.callback_query:
        await safe_edit_message_text(update.callback_query, text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE, use_query: bool = False):
    if not await is_authorized(update):
        return

    log_path = os.path.join(BASE_DIR, "logs", "trader.log")
    if not os.path.exists(log_path):
        text = "❌ Лог-файл `logs/trader.log` не найден."
        if use_query and update.callback_query:
            await safe_edit_message_text(
                update.callback_query, text, parse_mode="Markdown", reply_markup=get_back_keyboard()
            )
        else:
            await update.message.reply_text(text, parse_mode="Markdown")
        return

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-MAX_LOG_LINES:]
        content = "".join(lines)
        if len(content) > MAX_OUTPUT_LENGTH:
            content = content[-MAX_OUTPUT_LENGTH:]
        text = f"```\n{content}\n```"
        if use_query and update.callback_query:
            await safe_edit_message_text(
                update.callback_query, text, parse_mode="Markdown", reply_markup=get_back_keyboard()
            )
        else:
            await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        error_text = f"❌ Ошибка чтения лога: {e}"
        if use_query and update.callback_query:
            await safe_edit_message_text(
                update.callback_query, error_text, parse_mode="Markdown", reply_markup=get_back_keyboard()
            )
        else:
            await update.message.reply_text(error_text, parse_mode="Markdown")


async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE, use_query: bool = False):
    if not await is_authorized(update):
        return

    try:
        cfg = load_config()
        assets = cfg.get("assets", {})
        msg = "⚙️ **Конфигурация активов:**\n\n"
        for key, a in assets.items():
            if a.get("enabled"):
                msg += f"• `{key}` → {a.get('mt5_symbol', '?')} | {a.get('display_name', '?')}\n"
        if use_query and update.callback_query:
            await safe_edit_message_text(
                update.callback_query, msg, parse_mode="Markdown", reply_markup=get_back_keyboard()
            )
        else:
            await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        error_text = f"❌ Ошибка загрузки конфига: {e}"
        if use_query and update.callback_query:
            await safe_edit_message_text(
                update.callback_query, error_text, parse_mode="Markdown", reply_markup=get_back_keyboard()
            )
        else:
            await update.message.reply_text(error_text, parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TRADER_PROCESS
    query = update.callback_query
    try:
        await query.answer()
    except telegram.error.BadRequest as e:
        logger.warning(f"Callback query answer failed (likely too old): {e}")
    except Exception as e:
        logger.warning(f"Unexpected callback query answer error: {e}")

    if query.from_user.id != MY_CHAT_ID:
        await query.edit_message_text("⛔ Доступ запрещён.")
        return

    action = query.data

    if action in ("back", "home"):
        status_str, pid = get_robot_status()
        msg = (
            f"💻 **УПРАВЛЕНИЕ ПК ЧЕРЕЗ TELEGRAM**\n\n"
            f"• Статус робота: **{status_str}**\n"
            f"• Безопасный режим активирован!\n\n"
            f"**Доступные команды:**\n"
            f"• `/cmd <команда>` — только разрешённые команды\n"
            f"• `/read <путь>` — чтение файлов проекта (без секретов)\n"
            f"• `/logs` — последние {MAX_LOG_LINES} строк лога\n"
            f"• `/config` — конфигурация активов (без секретов)\n"
            f"• `/positions` — открытые позиции\n"
            f"• `/ls` — список файлов\n"
            f"• *Загрузка файла* — только `.py`/`.yaml` в безопасные папки"
        )
        await safe_edit_message_text(query, msg, parse_mode="Markdown", reply_markup=get_main_keyboard())

    elif action == "start_trader":
        status, pid = get_robot_status()
        if pid and is_process_running(pid):
            await query.edit_message_text("⚠️ Робот уже работает.", reply_markup=get_main_keyboard())
            return
        TRADER_PROCESS = subprocess.Popen([sys.executable, "-m", "execution.mt5_trader"])
        save_pid(TRADER_PROCESS.pid)
        await safe_edit_message_text(
            query, f"🚀 Робот запущен (PID: {TRADER_PROCESS.pid})", reply_markup=get_main_keyboard()
        )

    elif action == "stop_trader":
        status, pid = get_robot_status()
        if TRADER_PROCESS and TRADER_PROCESS.poll() is None:
            TRADER_PROCESS.terminate()
            TRADER_PROCESS = None
            try:
                os.remove(PID_FILE)
            except Exception:
                pass
            await safe_edit_message_text(query, "⏹ Робот остановлен.", reply_markup=get_main_keyboard())
        elif pid and is_process_running(pid):
            try:
                os.kill(pid, 9)
                await safe_edit_message_text(query, "⏹ Робот остановлен (по PID).", reply_markup=get_main_keyboard())
            except Exception as e:
                await query.edit_message_text(f"❌ Не удалось остановить по PID: {e}", reply_markup=get_main_keyboard())
        else:
            await query.edit_message_text("ℹ️ Робот и так остановлен.", reply_markup=get_main_keyboard())

    elif action == "restart_trader":
        status, pid = get_robot_status()
        if TRADER_PROCESS and TRADER_PROCESS.poll() is None:
            TRADER_PROCESS.terminate()
            TRADER_PROCESS = None
            time.sleep(1)
        elif pid and is_process_running(pid):
            try:
                os.kill(pid, 9)
            except Exception:
                pass
        TRADER_PROCESS = subprocess.Popen([sys.executable, "-m", "execution.mt5_trader"])
        save_pid(TRADER_PROCESS.pid)
        await safe_edit_message_text(
            query, f"🔄 Робот перезапущен (PID: {TRADER_PROCESS.pid})", reply_markup=get_main_keyboard()
        )

    elif action == "status_mt5":
        if not mt5.initialize():
            await query.edit_message_text("❌ Ошибка подключения к MT5.", reply_markup=get_back_keyboard())
            return
        acc = mt5.account_info()
        positions = positions_get_by_magic(magic=777111)
        status_text = (
            f"📊 **СОСТОЯНИЕ СЧЕТА MT5**\n\n"
            f"💰 Баланс: `${acc.balance:.2f}` | Средства: `${acc.equity:.2f}`\n"
            f"🔹 Открытых сделок: **{len(positions) if positions else 0}**\n"
        )
        if positions:
            status_text += "\n**Позиции:**\n"
            for p in positions:
                p_type = "BUY 🟢" if p.type == 0 else "SELL 🔴"
                status_text += f"• {p.symbol} {p_type} | PnL: `${p.profit:+.2f}`\n"
        await safe_edit_message_text(query, status_text, parse_mode="Markdown", reply_markup=get_back_keyboard())

    elif action == "report_pnl":
        try:
            cmd_res = subprocess.run(
                [sys.executable, "-m", "scripts.summary_report"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                timeout=120,
            )
            output = cmd_res.stdout or cmd_res.stderr or "✅ Отчёт сформирован."
            if len(output) > MAX_OUTPUT_LENGTH:
                output = output[-MAX_OUTPUT_LENGTH:]
            await safe_edit_message_text(
                query, f"<pre>{output}</pre>", parse_mode="HTML", reply_markup=get_back_keyboard()
            )
        except Exception as e:
            await safe_edit_message_text(query, f"❌ Ошибка формирования отчёта: {e}", reply_markup=get_back_keyboard())

    elif action == "view_positions":
        await positions_command(update, context, use_query=True)

    elif action == "refresh_positions":
        await positions_command(update, context, use_query=True)

    elif action == "cmd_train_all":
        await query.edit_message_text("⏳ Запускаю обучение моделей...", reply_markup=get_main_keyboard())
        subprocess.Popen([sys.executable, "-m", "scripts.train_all_assets"])

    elif action == "cmd_retrain":
        await query.edit_message_text(
            "⏳ Запускаю переобучение моделей (с обновлением данных)...", reply_markup=get_main_keyboard()
        )
        subprocess.Popen([sys.executable, "-m", "scripts.retrain_models"])

    elif action == "cmd_backtest_all":
        await query.edit_message_text("⏳ Запускаю бэктесты...", reply_markup=get_main_keyboard())
        subprocess.Popen(
            [
                "powershell",
                "-Command",
                "foreach ($a in @('XAUUSD','XAGUSD','BTCUSD','EURUSD','GBPUSD')) { python -m scripts.run_backtest --asset $a --timeframe M5 }",
            ]
        )

    elif action == "cmd_ls":
        files = os.listdir(".")
        msg = "📂 **Файлы в корне проекта:**\n" + "\n".join([f"• `{f}`" for f in files[:30]])
        await safe_edit_message_text(query, msg, parse_mode="Markdown", reply_markup=get_back_keyboard())

    elif action == "view_logs":
        await logs_command(update, context, use_query=True)

    elif action == "view_config":
        await config_command(update, context, use_query=True)

    elif action == "cmd_help":
        help_txt = (
            "🛡 **БЕЗОПАСНОСТЬ И КОМАНДЫ:**\n\n"
            "✅ Разрешённые `/cmd`: <code>summary_report</code>, <code>run_backtest</code>, <code>train_all_assets</code>, <code>retrain_models</code>, <code>execution.mt5_trader</code>, <code>backfill</code>\n"
            "✅ `/read`: только файлы внутри проекта, кроме <code>.env</code>, <code>backups</code>, <code>sqlite</code>, <code>models</code>, <code>logs</code>\n"
            "✅ `/write` отключена\n"
            "✅ Загрузка файлов: только <code>.py</code>, <code>.yaml</code>, <code>.yml</code>\n\n"
            "❌ Запрещены: любые команды вне whitelist, чтение секретов."
        )
        await safe_edit_message_text(query, help_txt, parse_mode="HTML", reply_markup=get_back_keyboard())


def main():
    logger.info("Starting Telegram Full Remote Terminal Bot (safe mode)...")
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", start_command))
    app.add_handler(CommandHandler("cmd", cmd_command))
    app.add_handler(CommandHandler("read", read_command))
    app.add_handler(CommandHandler("write", write_command))
    app.add_handler(CommandHandler("ls", ls_command))
    app.add_handler(CommandHandler("logs", logs_command))
    app.add_handler(CommandHandler("config", config_command))
    app.add_handler(CommandHandler("positions", positions_command))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(CallbackQueryHandler(button_handler))

    async def error_handler(update, context):
        logger.error(f"Update {update} caused error {context.error}")

    app.add_error_handler(error_handler)

    logger.info("✅ FULL REMOTE TERMINAL & IDE IS ONLINE (safe mode)!")
    app.run_polling(
        drop_pending_updates=True,
        bootstrap_retries=5,
    )


if __name__ == "__main__":
    main()
