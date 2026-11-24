"""Command Palette — портативная всплывающая командная строка для Windows.

Все данные хранятся в подпапке data рядом с программой (BASE_DIR/data):
config.json, commands.json, state.json, app.log. Хоткей по умолчанию
Alt+Space. Один файл, без внешних ресурсов: иконки рисуются QPainter'ом,
стили — инлайн-строки.
"""

from __future__ import annotations

# ============================================================================
# СЕКЦИЯ 1. ИМПОРТЫ
# ============================================================================

import ast
import copy
import ctypes
import hashlib
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Callable

from PySide6.QtCore import (
    QAbstractListModel, QEvent, QModelIndex, QObject, QPointF, QRect,
    QRunnable, QSize, Qt, QThreadPool, QTimer, Signal, Slot,
)
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon, QPainter,
    QPalette, QPen, QPixmap, QScreen,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QLineEdit, QListView, QMenu, QMessageBox,
    QPushButton, QStyle, QStyledItemDelegate, QSystemTrayIcon, QVBoxLayout, QWidget,
)

try:
    import winreg
except ImportError:  # не-Windows: приложение всё равно не запустится
    winreg = None  # type: ignore[assignment]

# ============================================================================
# СЕКЦИЯ 2. КОНСТАНТЫ, BASE_DIR, WIN32-ПРОТОТИПЫ, ЗНАЧЕНИЯ ПО УМОЛЧАНИЮ
# ============================================================================

APP_NAME = "Command Palette"
APP_VERSION = "1.9.1"
IS_WIN = sys.platform == "win32"

# Портативный базовый путь: рядом с exe (frozen) или рядом со скриптом.
BASE_DIR = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent)
# Все изменяемые данные — в подпапке data рядом с программой.
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
COMMANDS_PATH = DATA_DIR / "commands.json"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "app.log"
DATA_FILES = ("config.json", "commands.json", "state.json")

MUTEX_NAME = "Local\\CommandPalette_v1_single_instance"
HOTKEY_ID = 0xB17B
CLIP_HOTKEY_ID = 0xB17C
CMD_HOTKEY_BASE = 0xB200
CMD_HOTKEY_MAX = 32
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
MOD_NOREPEAT = 0x4000
SW_SHOWNORMAL = 1
CREATE_NO_WINDOW = 0x08000000
MAX_RESULTS = 60
DANGEROUS_ACTIONS = {"shutdown", "restart"}  # empty_recycle — без подтверждения  # empty_recycle — без подтверждения

# Геометрия окна (логические пиксели, Qt масштабирует под DPI).
MARGIN = 10          # отступ layout внутри окна
INPUT_H = 48         # высота поля ввода
SPACING = 6          # зазор между полем и списком
LIST_PAD = 8         # вертикальные отступы viewport списка (4+4)
BG_INSET = 4         # отступ фонового прямоугольника (место под тень)
MARK_FOOTER_H = 26   # панель состояния отметок
PHRASES_H = 34       # панель быстрых фраз
RESIZE_GRIP = 10     # зона ресайза по краям окна

DEFAULT_CONFIG: dict[str, Any] = {
    "hotkey": "Alt+Space",
    "theme": "dark",
    "window_width": 640,
    "max_visible_rows": 9,
    "row_height": 48,
    "debounce_ms": 70,
    "history_limit": 50,
    "input_font_pt": 14,
    "title_font_pt": 11.5,
    "subtitle_font_pt": 9,
    "color_bg": "#1b1e26",
    "color_panel": "#232833",
    "color_text": "#e9edf5",
    "color_subtext": "#9aa5bb",
    "color_accent": "#5b8cff",
    "color_border": "#3a4256",
    "datetime_insert": True,
    "datetime_paste_delay_ms": 120,
    "clipboard_history": True,
    "clipboard_limit": 30,
    "clipboard_hotkey": "Ctrl+Alt+V",
    "window_under_cursor": False,
    "show_all_on_empty": True,
    "sort_mode": "score",
    "pin_window": False,
    "window_opacity": 100,
    "phrases_enabled": False,
    "phrases": ["g ", "> ", "#буфер", "= ", "https://", "*"],
}

THEMES: dict[str, dict[str, str]] = {
    "dark": {"color_bg": "#1b1e26", "color_panel": "#232833",
             "color_text": "#e9edf5", "color_subtext": "#9aa5bb",
             "color_accent": "#5b8cff", "color_border": "#3a4256"},
    "light": {"color_bg": "#f4f6fb", "color_panel": "#ffffff",
              "color_text": "#1c2030", "color_subtext": "#5c6577",
              "color_accent": "#2f6bff", "color_border": "#c9cfdd"},
    "midnight": {"color_bg": "#0d1117", "color_panel": "#161b22",
                 "color_text": "#e6edf3", "color_subtext": "#8b949e",
                 "color_accent": "#58a6ff", "color_border": "#30363d"},
    "forest": {"color_bg": "#10201a", "color_panel": "#182b23",
               "color_text": "#e8f5ee", "color_subtext": "#93b3a4",
               "color_accent": "#3ddc84", "color_border": "#2c4a3c"},
    "solarized": {"color_bg": "#002b36", "color_panel": "#073642",
                  "color_text": "#eee8d5", "color_subtext": "#93a1a1",
                  "color_accent": "#b58900", "color_border": "#586e75"},
    "dracula": {"color_bg": "#282a36", "color_panel": "#343746",
                "color_text": "#f8f8f2", "color_subtext": "#bd93f9",
                "color_accent": "#ff79c6", "color_border": "#6272a4"},
}
THEME_NAMES = {"dark": "Тёмная", "light": "Светлая",
               "midnight": "Полночь", "forest": "Лес",
               "solarized": "Solarized Dark", "dracula": "Dracula"}

DEFAULT_COMMANDS: dict[str, Any] = {"commands": [
    {"name": "Блокнот", "type": "path",
     "target": "%WINDIR%\\system32\\notepad.exe", "args": "",
     "synonyms": ["notepad", "текст", "редактор"],
     "description": "Открыть Блокнот", "icon": "app"},
    {"name": "Калькулятор Windows", "type": "path", "target": "calc.exe",
     "synonyms": ["calc", "калькулятор"],
     "description": "Стандартный калькулятор", "icon": "calc"},
    {"name": "Проводник", "type": "path", "target": "explorer.exe",
     "synonyms": ["explorer", "файлы"],
     "description": "Открыть Проводник", "icon": "folder"},
    {"name": "Командная строка", "type": "path", "target": "cmd.exe",
     "args": "/k", "synonyms": ["cmd", "консоль", "terminal"],
     "description": "cmd.exe /k", "icon": "terminal"},
    {"name": "Диспетчер задач", "type": "path", "target": "taskmgr.exe",
     "synonyms": ["taskmgr", "процессы", "task manager"],
     "description": "Диспетчер задач Windows", "icon": "settings"},
    {"name": "Paint", "type": "path", "target": "mspaint.exe",
     "synonyms": ["paint", "рисование"],
     "description": "Графический редактор Paint", "icon": "app"},
    {"name": "Папка данных", "type": "path", "target": "{data}",
     "synonyms": ["папка", "данные", "config", "настройки", "data"],
     "description": "Открыть папку data с настройками и историей",
     "icon": "folder"},
    {"name": "Панель управления", "type": "path", "target": "control.exe",
     "synonyms": ["control", "панель"],
     "description": "Панель управления Windows", "icon": "settings"},
    {"name": "GitHub", "type": "url", "target": "https://github.com",
     "synonyms": ["git", "гитхаб", "код"],
     "description": "Открыть github.com", "icon": "url"},
    {"name": "Википедия", "type": "url", "target": "https://ru.wikipedia.org",
     "synonyms": ["wiki", "вики", "энциклопедия"],
     "description": "Русская Википедия", "icon": "url"},
]}

DEFAULT_STATE: dict[str, Any] = {"version": 1, "history": [], "usage": {}, "clipboard": []}

SHELL_ERRORS = {
    2: "файл не найден", 3: "путь не найден", 5: "отказано в доступе",
    8: "недостаточно памяти", 26: "ошибка совместного доступа",
    27: "неполное имя файла", 28: "тайм-аут", 29: "ошибка DDE",
    30: "файл занят другим приложением", 31: "нет связанного приложения",
    32: "не найдена нужная DLL",
}

COMMON_TLDS = {
    "ru", "com", "net", "org", "io", "dev", "app", "gov", "edu", "info",
    "biz", "me", "co", "ai", "xyz", "online", "site", "tech", "su", "ua",
    "by", "kz", "de", "fr", "uk", "us", "рф", "pro", "top", "cloud",
}

NAMED_VK = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "insert": 0x2D,
    "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "printscreen": 0x2C, "pause": 0x13,
    "capslock": 0x14, "apps": 0x5D, "plus": 0xBB, "minus": 0xBD,
}
PUNCT_VK = {
    "`": 0xC0, "~": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "+": 0xBB,
}

if IS_WIN:
    USER32 = ctypes.WinDLL("user32", use_last_error=True)
    KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    SHELL32 = ctypes.WinDLL("shell32", use_last_error=True)
    POWRPROF = ctypes.WinDLL("powrprof", use_last_error=True)
    OLE32 = ctypes.WinDLL("ole32")

    class MSG(ctypes.Structure):
        """Win32 MSG для message-loop потока хоткея."""
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                    ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                    ("time", wintypes.DWORD), ("pt", wintypes.POINT)]

    class MONITORINFOEXW(ctypes.Structure):
        """Win32 MONITORINFOEXW для определения активного монитора."""
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    USER32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                      wintypes.UINT, wintypes.UINT]
    USER32.RegisterHotKey.restype = wintypes.BOOL
    USER32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    USER32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    USER32.GetMessageW.restype = ctypes.c_int
    USER32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
    USER32.GetForegroundWindow.restype = wintypes.HWND
    USER32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    USER32.MonitorFromWindow.restype = wintypes.HMONITOR
    USER32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR,
                                       ctypes.POINTER(MONITORINFOEXW)]
    USER32.SetForegroundWindow.argtypes = [wintypes.HWND]
    USER32.LockWorkStation.restype = wintypes.BOOL
    USER32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.UINT]
    KERNEL32.GetCurrentThreadId.restype = wintypes.DWORD
    KERNEL32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                      wintypes.LPCWSTR]
    KERNEL32.CreateMutexW.restype = wintypes.HANDLE
    KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    SHELL32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR, ctypes.c_int]
    SHELL32.ShellExecuteW.restype = wintypes.HINSTANCE
    SHELL32.SHEmptyRecycleBinW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                           wintypes.DWORD]
    SHELL32.SHEmptyRecycleBinW.restype = ctypes.c_long
    POWRPROF.SetSuspendState.argtypes = [wintypes.BOOLEAN, wintypes.BOOLEAN,
                                         wintypes.BOOLEAN]
    POWRPROF.SetSuspendState.restype = wintypes.BOOLEAN
    OLE32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    OLE32.CoInitializeEx.restype = ctypes.c_long

# ============================================================================
# СЕКЦИЯ 3. ЛОГИРОВАНИЕ (ротация ~1 МБ × 3, только в DATA_DIR)
# ============================================================================

log = logging.getLogger("palette")


def ensure_data_dir() -> bool:
    """Создаёт подпапку data рядом с программой (портативное хранилище)."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return DATA_DIR.is_dir()
    except OSError:
        return False


def migrate_legacy_data() -> list[str]:
    """Переносит данные старой версии из BASE_DIR в BASE_DIR/data."""
    moved: list[str] = []
    if not DATA_DIR.is_dir():
        return moved
    names = list(DATA_FILES) + ["app.log", "app.log.1", "app.log.2",
                                "app.log.3"]
    names += [n + ".bak" for n in DATA_FILES]
    for name in names:
        src, dst = BASE_DIR / name, DATA_DIR / name
        if not src.is_file() or dst.exists():
            continue
        try:
            shutil.move(str(src), str(dst))
            moved.append(name)
        except OSError:
            pass  # файл занят или нет прав — работаем с тем, что есть
    stale = BASE_DIR / ".portable_write_test"
    if stale.is_file():
        try:
            stale.unlink()
        except OSError:
            pass
    return moved


def probe_writable() -> bool:
    """Проверяет, доступна ли папка data для записи."""
    try:
        p = DATA_DIR / ".portable_write_test"
        with open(p, "w", encoding="utf-8") as f:
            f.write("ok")
        p.unlink()
        return True
    except OSError:
        return False


def setup_logging(writable: bool) -> None:
    """Файловый лог с ротацией; если запись невозможна — stderr (не падаем)."""
    log.setLevel(logging.INFO)
    logging.raiseExceptions = False
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    handler: logging.Handler | None = None
    if writable:
        try:
            handler = logging.handlers.RotatingFileHandler(
                LOG_PATH, maxBytes=1_000_000, backupCount=3,
                encoding="utf-8")
        except OSError:
            handler = None
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(fmt)
    log.addHandler(handler)


def _install_excepthooks() -> None:
    """Логирует необработанные исключения вместо тихого падения."""
    def hook(tp: type, val: BaseException, tb: Any) -> None:
        log.error("Необработанное исключение", exc_info=(tp, val, tb))

    def thread_hook(args: Any) -> None:
        log.error("Исключение в потоке %s", args.thread,
                  exc_info=(args.exc_type, args.exc_value,
                            args.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook


# ============================================================================
# СЕКЦИЯ 4. JSON-ХРАНИЛИЩЕ (config / commands / state): атомарно, *.bak
# ============================================================================

class JsonStore:
    """Загрузка/атомарное сохранение одного JSON-файла рядом с программой."""

    def __init__(self, path: Path, default: Any, writable: bool,
                 validate: Callable[[Any], bool]) -> None:
        self.path = path
        self.default = default
        self.writable = writable
        self.validate = validate
        self.data: Any = copy.deepcopy(default)
        self.problems: list[str] = []

    def load(self) -> None:
        """Читает файл; отсутствующий создаёт, битый переименовывает в .bak."""
        self.problems.clear()
        if not self.path.exists():
            self.data = copy.deepcopy(self.default)
            if self.writable:
                self.save()
                log.info("Создан файл по умолчанию: %s", self.path.name)
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not self.validate(raw):
                raise ValueError("неверная структура файла")
            self.data = raw
            return
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            msg = (f"{self.path.name} повреждён ({exc}); переименован в "
                   f"{self.path.name}.bak, загружены значения по умолчанию")
            log.warning(msg)
            self.problems.append(msg)
            try:
                os.replace(self.path, self.path.with_name(
                    self.path.name + ".bak"))
            except OSError as exc2:
                log.error("Не удалось создать .bak: %s", exc2)
        except OSError as exc:
            msg = f"{self.path.name} не читается ({exc}); значения по умолчанию"
            log.warning(msg)
            self.problems.append(msg)
        self.data = copy.deepcopy(self.default)
        self.save()

    def save(self) -> None:
        """Атомарная запись: временный файл в BASE_DIR + os.replace."""
        if not self.writable:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("Не удалось сохранить %s: %s", self.path.name, exc)


def _valid_commands(d: Any) -> bool:
    """Валидатор корня commands.json."""
    return isinstance(d, dict) and isinstance(d.get("commands"), list)


# ============================================================================
# СЕКЦИЯ 5. НЕЧЁТКИЙ ПОИСК (subsequence + substring, подсветка, веса полей)
# ============================================================================

_SEPARATORS = set(" \t_-./\\:()[]{}")


def _norm(s: str) -> str:
    """Нормализация без изменения длины (индексы подсветки сохраняются)."""
    return s.lower().replace("ё", "е")


FUZZY_SEPARATORS = set(" \t-./\\:()[]{}")


FUZZY_SEPARATORS = set(" \t-./\\:()[]{}")


def fuzzy_match(query: str, text: str) -> tuple[float, list[int]] | None:
    """Строгий матч токена: подстрока либо акроним по началам слов."""
    q, t = _norm(query), _norm(text)
    if not q or not t:
        return None
    idx = t.find(q)
    if idx >= 0:
        score = 100.0 + 10 * len(q) - 0.5 * idx
        if idx == 0 or t[idx - 1] in FUZZY_SEPARATORS:
            score += 15
        if t == q:
            score += 50
        return score, list(range(idx, idx + len(q)))
    if len(q) >= 2:
        starts = [i for i, c in enumerate(t)
                  if c.isalnum()
                  and (i == 0 or t[i - 1] in FUZZY_SEPARATORS)]
        positions: list[int] = []
        si = 0
        for ch in q:
            while si < len(starts) and t[starts[si]] != ch:
                si += 1
            if si >= len(starts):
                return None
            positions.append(starts[si])
            si += 1
        score = 40.0 + 6 * len(q) - 0.3 * positions[0]
        return score, positions
    return None


def fuzzy_tokens(query: str, text: str) -> tuple[float, list[int]] | None:
    """Все слова запроса должны найтись в одном поле; суммы очков."""
    tokens = _norm(query).split()
    if not tokens:
        return None
    total, positions = 0.0, []
    for tok in tokens:
        r = fuzzy_match(tok, text)
        if r is None:
            return None
        total += r[0]
        positions.extend(r[1])
    return total, sorted(set(positions))


def match_command(query: str, cmd: dict[str, Any]
                  ) -> tuple[float, list[int], str, str] | None:
    """Лучшее совпадение по полям команды: name > synonyms > desc > target."""
    fields: list[tuple[str, str, float]] = [("name", cmd["name"], 1.0)]
    fields += [("synonym", s, 0.9) for s in cmd["synonyms"]]
    fields += [("tag", s, 0.85) for s in cmd.get("tags", [])]
    fields.append(("description", cmd["description"], 0.65))
    fields.append(("target", cmd["target"], 0.55))
    best: tuple[float, list[int], str, str] | None = None
    for label, text, weight in fields:
        if not text:
            continue
        r = fuzzy_tokens(query, text)
        if r is None:
            continue
        score, pos = r[0] * weight, r[1]
        if best is None or score > best[0]:
            best = (score, pos if label == "name" else [], label, text)
    return best


# ============================================================================
# СЕКЦИЯ 6. ПРОВАЙДЕРЫ КОМАНД: калькулятор (AST), пути, URL, поиск, движок
# ============================================================================

class CalcError(ValueError):
    """Ошибка вычисления безопасного выражения."""


def _safe_pow(a: Any, b: Any) -> Any:
    """Степень с защитой от гигантских чисел и комплексных результатов."""
    if abs(b) > 512:
        raise CalcError("степень слишком велика (максимум 512)")
    r = a ** b
    if isinstance(r, complex):
        raise CalcError("комплексный результат не поддерживается")
    if isinstance(r, int) and r.bit_length() > 4096:
        raise CalcError("число слишком большое")
    return r


def _safe_factorial(x: Any) -> int:
    """Факториал с ограничением аргумента."""
    if not isinstance(x, int) or not 0 <= x <= 1000:
        raise CalcError("factorial: нужен целый аргумент 0..1000")
    return math.factorial(x)


_CALC_FUNCS: dict[str, Callable[..., Any]] = {
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "log": math.log, "log2": math.log2, "log10": math.log10,
    "exp": math.exp, "abs": abs, "round": round, "min": min, "max": max,
    "floor": math.floor, "ceil": math.ceil, "degrees": math.degrees,
    "radians": math.radians, "pow": _safe_pow, "factorial": _safe_factorial,
}
_CALC_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def calc_eval(expr: str) -> int | float:
    """Вычисляет арифметику через обход ast — без eval/exec."""
    src = expr.replace(",", ".").replace("^", "**").strip()
    if len(src) > 200:
        raise CalcError("слишком длинное выражение")
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise CalcError("синтаксическая ошибка") from exc
    if sum(1 for _ in ast.walk(tree)) > 120:
        raise CalcError("слишком сложное выражение")

    def ev(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            if isinstance(n.value, (int, float)) and not isinstance(
                    n.value, bool):
                return n.value
            raise CalcError("допустимы только числа")
        if isinstance(n, ast.Name):
            k = _norm(n.id)
            if k in _CALC_CONSTS:
                return _CALC_CONSTS[k]
            raise CalcError(f"неизвестное имя: {n.id}")
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            if isinstance(n.op, ast.USub):
                return -v
            if isinstance(n.op, ast.UAdd):
                return +v
            raise CalcError("недопустимая унарная операция")
        if isinstance(n, ast.BinOp):
            a, b, op = ev(n.left), ev(n.right), n.op
            if isinstance(op, ast.Add):
                return a + b
            if isinstance(op, ast.Sub):
                return a - b
            if isinstance(op, ast.Mult):
                return a * b
            if isinstance(op, ast.Div):
                if b == 0:
                    raise CalcError("деление на ноль")
                return a / b
            if isinstance(op, ast.FloorDiv):
                if b == 0:
                    raise CalcError("деление на ноль")
                return a // b
            if isinstance(op, ast.Mod):
                if b == 0:
                    raise CalcError("остаток от деления на ноль")
                return a % b
            if isinstance(op, ast.Pow):
                return _safe_pow(a, b)
            raise CalcError("недопустимый оператор")
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name) or n.keywords:
                raise CalcError("допустимы только простые вызовы функций")
            fname = _norm(n.func.id)
            if fname not in _CALC_FUNCS:
                raise CalcError(f"неизвестная функция: {fname}")
            args = [ev(a) for a in n.args]
            try:
                return _CALC_FUNCS[fname](*args)
            except CalcError:
                raise
            except (ValueError, OverflowError, TypeError) as exc:
                raise CalcError(f"{fname}: {exc}") from exc
        raise CalcError("недопустимая конструкция")

    try:
        result = ev(tree)
    except OverflowError as exc:
        raise CalcError("переполнение") from exc
    if isinstance(result, float) and not math.isfinite(result):
        raise CalcError("результат не определён")
    return result


def fmt_num(v: int | float) -> str:
    """Аккуратное строковое представление числа."""
    if isinstance(v, int):
        return str(v)
    if abs(v - round(v)) < 1e-10 and abs(v) < 1e15:
        return str(int(round(v)))
    return format(v, ".12g")


def calc_candidate(s: str) -> bool:
    """Похоже ли выражение на арифметику (для неявного режима)."""
    if not s or not re.fullmatch(r"[0-9a-zA-Z+\-*/%^()., _\t]+", s):
        return False
    if not re.search(r"[+\-*/%^()]", s):
        return False
    if re.search(r"\d", s):
        return True
    low = _norm(s)
    return any(re.search(r"\b" + n + r"\b", low)
               for n in list(_CALC_CONSTS) + list(_CALC_FUNCS))


def expand_path(s: str) -> str:
    """Подставляет {app}/{data}, переменные окружения и ~."""
    s = s.replace("{app}", str(BASE_DIR)).replace("{data}", str(DATA_DIR))
    return os.path.expandvars(os.path.expanduser(s))


def _windows_root() -> str:
    """Каталог Windows (без обращения к реестру)."""
    return os.environ.get("SystemRoot", r"C:\Windows")


def _system_dirs() -> list[str]:
    """Каталоги встроенных утилит Windows."""
    root = _windows_root()
    return [root, os.path.join(root, "System32"),
            os.path.join(root, "SysWOW64"), os.path.join(root, "Sysnative")]


def resolve_target(target: str) -> str | None:
    """Возвращает реальный путь цели либо None.

    Голые имена (calc.exe, cmd.exe, notepad) ищутся в PATH и каталогах
    Windows: раньше они проверялись относительно текущего каталога, из-за
    чего встроенные команды падали с «Путь не найден».
    """
    s = expand_path(target.strip().strip('"'))
    if not s:
        return None
    low = s.lower()
    if low.startswith(("shell:", "ms-settings:", "http://", "https://",
                       "www.")):
        return s
    if os.path.isabs(s) or "\\" in s or "/" in s:
        return s if os.path.exists(s) else None
    if os.path.isfile(s):
        return os.path.abspath(s)
    names = ([s] if Path(s).suffix else
             [s + e for e in ("", ".exe", ".com", ".bat", ".cmd", ".msc")])
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        for d in _system_dirs():
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


def split_path_args(s: str) -> tuple[str, str]:
    """Отделяет путь от аргументов: кавычки либо самый длинный существующий
    префикс до пробела."""
    s = s.strip()
    if s.startswith('"'):
        end = s.find('"', 1)
        if end > 0:
            return s[1:end], s[end + 1:].strip()
    if resolve_target(s):
        return s, ""
    spaces = [i for i, c in enumerate(s) if c == " "]
    for i in reversed(spaces):
        head, tail = s[:i], s[i + 1:].strip()
        if head and resolve_target(head):
            return head, tail
    return s, ""


def path_candidate(q: str) -> str | None:
    """Похоже ли на путь (для провайдера путей)."""
    s = q.strip()
    if s.lower().startswith(("http://", "https://", "www.", "g ")):
        return None
    if ("\\" in s or "/" in s or s.startswith(("~", ".", "%"))
            or re.match(r"^[A-Za-z]:", s) or "{app}" in s
            or "{data}" in s):
        return s
    if re.fullmatch(r"[\w.\-]+\.(exe|com|bat|cmd|msc)", s, re.I):
        return s
    return None


def url_candidate(q: str) -> str | None:
    """Похоже ли на URL; «голые» домены — только с известным TLD."""
    s = q.strip()
    if not s or " " in s:
        return None
    low = s.lower()
    if low.startswith(("http://", "https://")):
        return s
    if low.startswith("www."):
        return "https://" + s
    m = re.fullmatch(r"([\w-]+(?:\.[\w-]+)+)(:\d+)?(/\S*)?", s)
    if m and m[1].rsplit(".", 1)[-1].lower() in COMMON_TLDS:
        return "https://" + s
    return None


@dataclass
class ResultItem:
    """Одна строка списка результатов."""
    kind: str                 # path | url | system | search | calc | info | error
    title: str
    subtitle: str
    icon: str                 # ключ Icons.KIND или литеральный символ/эмодзи
    payload: dict[str, Any]   # данные для исполнения
    key: str                  # ключ статистики_usage
    score: float = 0.0
    matches: list[int] = field(default_factory=list)
    enabled: bool = True
    completion: str = ""      # текст для Tab


SYSTEM_ACTIONS = {"lock", "sleep", "shutdown", "restart",
                  "recycle", "empty_recycle", "restart_explorer"}


def _system_commands() -> list[dict[str, Any]]:
    """Встроенные системные действия (нормализованы как пользовательские)."""
    raw = [
        ("Блокировка экрана", "lock", "lock",
         ["lock", "блокировка", "заблокировать"], "Заблокировать сеанс"),
        ("Сон", "sleep", "sleep",
         ["sleep", "сон", "suspend", "ждущий"], "Спящий режим"),
        ("Выключение", "shutdown", "power",
         ["shutdown", "выключить", "poweroff"],
         "Завершить работу (с подтверждением)"),
        ("Перезагрузка", "restart", "restart",
         ["restart", "reboot", "перезагрузить"],
         "Перезагрузить (с подтверждением)"),
        ("Корзина", "recycle", "trash",
         ["recycle", "корзина", "trash"], "Открыть Корзину"),
        ("Очистить корзину", "empty_recycle", "trash",
         ["empty", "очистить корзину"],
         "Удалить содержимое корзины сразу, без подтверждения"),
        ("Перезапустить Проводник", "restart_explorer", "restart",
            ["restart explorer", "explorer restart", "перезапустить проводник",
            "перезапуск explorer", "рестарт проводника"],
            "Завершить explorer.exe и запустить заново"),
    ]
    return [{"name": n, "type": "system", "target": t, "args": "",
             "synonyms": s, "description": d, "icon": ic,
             "key": f"sys:{t}", "tags": ["система"]} for n, t, ic, s, d in raw]

# ---- дата/время: встроенные форматы и вычисление значения ----
RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря")
RU_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг",
               "пятница", "суббота", "воскресенье")
_DT_ALLOWED = re.compile(r"%#?[^aAbBcdHIjmMpSUwWxXyYzZ%]")

def datetime_value(spec: str, now: float | None = None) -> str:
    """Значение даты/времени: strftime-формат либо спец-имя.

    Спец-имена: unix, unix_ms, utc_iso, ru_date, ru_weekday, ru_datetime.
    Превью считается при поиске, реальное значение — при исполнении.
    """
    s = spec.strip()
    low = s.lower()
    ts = time.time() if now is None else now
    t = time.localtime(ts)
    if low == "unix":
        return str(int(ts))
    if low == "unix_ms":
        return str(int(ts * 1000))
    if low == "utc_iso":
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    if low == "ru_date":
        return f"{t.tm_mday} {RU_MONTHS[t.tm_mon - 1]} {t.tm_year}"
    if low == "ru_weekday":
        return (f"{RU_WEEKDAYS[t.tm_wday]}, {t.tm_mday} "
                f"{RU_MONTHS[t.tm_mon - 1]} {t.tm_year}")
    if low == "ru_datetime":
        return (f"{t.tm_mday} {RU_MONTHS[t.tm_mon - 1]} {t.tm_year}, "
                f"{t.tm_hour:02d}:{t.tm_min:02d}")
    if _DT_ALLOWED.search(s):
        raise ValueError(f"неизвестный спецификатор формата: {s}")
    return time.strftime(s, t)

def _datetime_commands() -> list[dict[str, Any]]:
    """Встроенные команды вставки даты/времени (target = формат)."""
    raw = [
        ("Дата: ДД.ММ.ГГГГ", "%d.%m.%Y",
         ["date", "дата", "сегодня", "today"], "Например: 22.11.2026"),
        ("Дата ISO: ГГГГ-ММ-ДД", "%Y-%m-%d",
         ["iso", "дата iso", "yyyy-mm-dd"], "Сортируемый формат ISO 8601"),
        ("Дата US: ММ/ДД/ГГГГ", "%m/%d/%Y",
         ["us date", "дата us", "mm/dd/yyyy"], "Американский формат"),
        ("Дата словами", "ru_date",
         ["дата словами", "date ru", "пропись"], "Например: 22 ноября 2026"),
        ("Дата с днём недели", "ru_weekday",
         ["weekday", "день недели", "дата полностью"],
         "Например: воскресенье, 22 ноября 2026"),
        ("Дата и время словами", "ru_datetime",
         ["datetime ru", "дата время словами"],
         "Например: 22 ноября 2026, 14:35"),
        ("Время: ЧЧ:ММ", "%H:%M",
         ["time", "время", "now", "сейчас"], "24-часовой формат"),
        ("Время с секундами", "%H:%M:%S",
         ["time seconds", "время с секундами", "hh:mm:ss"], "ЧЧ:ММ:СС"),
        ("Дата и время", "%d.%m.%Y %H:%M",
         ["datetime", "дата время", "дата и время"], "ДД.ММ.ГГГГ ЧЧ:ММ"),
        ("Дата и время ISO", "%Y-%m-%dT%H:%M:%S",
         ["iso datetime", "iso time", "timestamp iso"],
         "ISO 8601, локальное время"),
        ("Дата/время UTC", "utc_iso",
         ["utc", "gmt", "zulu", "время utc"], "ISO 8601 в UTC (Z)"),
        ("Метка для имени файла", "%Y-%m-%d_%H-%M-%S",
         ["filename", "имя файла", "штамп", "file stamp"],
         "Без двоеточий — для имён файлов"),
        ("Дата и время: ГГГГ-ММ-ДД ЧЧ:ММ:СС", "%Y-%m-%d %H:%M:%S",
            ["sql", "sql datetime", "дата время sql", "yyyy-mm-dd hh:mm:ss",
            "timestamp sql"],
            "Например: 2026-11-22 02:40:27"),
        ("Unix timestamp", "unix",
         ["unix", "unix time", "юникс", "timestamp", "epoch", "таймстамп"],
         "Секунды с 01.01.1970 UTC"),
        ("Unix timestamp (мс)", "unix_ms",
         ["unix ms", "timestamp ms", "миллисекунды"],
         "Миллисекунды с 01.01.1970 UTC"),
    ]
    return [{"name": n, "type": "datetime", "target": spec, "args": "",
             "synonyms": syn, "description": d, "icon": "clock",
             "key": f"dt:{spec}", "tags": ["дата", "время"]} for n, spec, syn, d in raw]


def _default_icon(ctype: str, target: str) -> str:
    """Иконка по типу/расширению — без обращения к диску."""
    if ctype == "url":
        return "url"
    if ctype == "datetime":
        return "clock"
    if ctype == "text":
        return TEXT_ICONS.get(target, "Aa")
    if ctype == "system":
        return {"lock": "lock", "sleep": "sleep", "shutdown": "power",
                "restart": "restart", "recycle": "trash",
                "empty_recycle": "trash",
                "restart_explorer": "restart"}.get(target, "settings")
    ext = Path(target).suffix.lower()
    if ext in (".exe", ".bat", ".cmd", ".com", ".msc"):
        return "app"
    if (not ext or target.endswith(("\\", "/"))
            or target in ("{app}", "{data}")):
        return "folder"
    return "file"


def _fmt_ago(ts: float) -> str:
    """Компактная давность запуска для подзаголовка недавних команд."""
    dt = max(0.0, time.time() - float(ts))
    if dt < 60:
        return "только что"
    if dt < 3600:
        return f"{int(dt // 60)} мин назад"
    if dt < 86400:
        return f"{int(dt // 3600)} ч назад"
    return f"{int(dt // 86400)} дн назад"


def _usage_bonus(usage: dict[str, Any], key: str, now: float) -> float:
    """Бонус за частоту и свежесть запусков."""
    rec = usage.get(key)
    if not isinstance(rec, dict):
        return 0.0
    bonus = min(40.0, 12.0 * math.log1p(int(rec.get("count", 0))))
    dt = now - float(rec.get("last", 0))
    if dt < 86400:
        bonus += 6
    elif dt < 604800:
        bonus += 3
    return bonus


def _settings_commands() -> list[dict[str, Any]]:
    """Встроенные команды управления настройками (type=settings)."""
    raw: list[tuple[str, str, str, list[str], str]] = []
    for key, name, syn, desc in (
            ("datetime_insert", "Настройка: дата/время — вставка в окно",
             ["дата вставка", "datetime insert"],
             "Вкл/выкл вставку Ctrl+V в активное окно"),
            ("clipboard_history", "Настройка: история буфера обмена",
             ["буфер история", "clipboard history"],
             "Вкл/выкл сбор истории буфера"),
            ("window_under_cursor", "Настройка: окно под курсором",
             ["под курсором", "under cursor"],
             "Показывать окно под курсором мыши"),
            ("show_all_on_empty", "Настройка: все команды при пустом вводе",
             ["пустой ввод", "show all"],
             "Список всех команд при пустой строке"),
            ("phrases_enabled", "Настройка: панель быстрых фраз",
             ["фразы", "phrases"], "Кнопки быстрых фраз под вводом"),
            ("pin_window", "Настройка: закрепить окно (поверх всех)",
             ["закрепить", "pin", "поверх всех"],
             "Не скрывать окно при потере фокуса")):
        raw.append((name, key, "toggle", syn, desc))
    for mode, label in (("score", "по релевантности"),
                        ("name", "по названию"),
                        ("last", "по последнему вызову"),
                        ("manual", "ручная")):
        raw.append((f"Сортировка: {label}", "sort_mode", mode,
                    ["сортировка", "sort", mode], "Быстро: Ctrl+1..4"))
    for tid, tname in THEME_NAMES.items():
        raw.append((f"Тема: {tname}", "theme", tid,
                    ["тема", "theme", tid], "Оформление окна"))
    for pct in (100, 90, 80, 70, 60, 50, 40, 30):
        raw.append((f"Прозрачность: {pct}%", "window_opacity", str(pct),
                    ["прозрачность", "opacity"], "Прозрачность окна"))
    raw += [
        ("Настройка: перезагрузить команды и конфиг", "reload", "",
         ["reload", "перезагрузить"], "Перечитать config.json/commands.json"),
        ("Настройка: автозапуск Windows", "autorun", "toggle",
         ["автозапуск", "autorun", "startup"], "Ключ HKCU Run"),
        ("Настройка: очистить историю ввода", "clear_history", "",
         ["очистить историю", "clear history"], "Список Ctrl+H"),
        ("Настройка: очистить буфер (кроме закреплённых)", "clear_clipboard",
         "", ["очистить буфер", "clear clipboard"],
         "Закреплённые записи (#!) сохраняются"),
    ]
    return [{"name": n, "type": "settings", "target": k, "args": v,
             "synonyms": s, "description": d, "icon": "settings",
             "key": f"set:{k}:{v}", "tags": ["настройки"], "hotkey": ""}
            for n, k, v, s, d in raw]

class CommandEngine:
    """Собирает результаты всех провайдеров; вызывается из рабочего потока."""

    def __init__(self) -> None:
        self._commands: list[dict[str, Any]] = []
        self._system = _system_commands()
        # v1.7: дата/время больше не встроена — только из commands.json.
        self._datetime: list[dict[str, Any]] = []
        self._text = _text_commands()
        self._clipboard_provider: Callable[[], list[Any]] = lambda: []

        self._settings = _settings_commands()
        self._settings_state: Callable[[], dict[str, Any]] = lambda: {}
        self._tag_overrides: dict[str, list[str]] = {}


    def set_clipboard_provider(self, fn: Callable[[], list[Any]]) -> None:
        """Источник истории буфера для тега #буфер / #clipboard."""
        self._clipboard_provider = fn


    def set_settings_provider(self, fn: Callable[[], dict[str, Any]]) -> None:
        """Источник текущих настроек для подзаголовков команд настроек."""
        self._settings_state = fn

    def set_tag_overrides(self, overrides: dict[str, Any]) -> None:
        """Пользовательские метки поверх встроенных (state.json)."""
        clean: dict[str, list[str]] = {}
        if isinstance(overrides, dict):
            for k, v in overrides.items():
                if isinstance(v, list):
                    clean[str(k)] = [str(t).strip().lstrip("#")
                                    for t in v if str(t).strip()]
        self._tag_overrides = clean
        for cmd in [*self._commands, *self._system, *self._datetime,
                    *self._text, *self._settings]:
            ov = clean.get(cmd["key"])
            if ov is not None:
                cmd["tags"] = list(ov)

    def tags_for(self, key: str) -> list[str]:
        """Текущие метки команды по ключу (для команд #+/#-)."""
        for c in [*self._commands, *self._system, *self._datetime,
                *self._text, *self._settings]:
            if c["key"] == key:
                return [str(t) for t in c.get("tags", [])]
        return []


    def _clip_item(self, e: dict[str, Any]) -> ResultItem:
        """ResultItem записи буфера (метки, признак постоянного хранения)."""
        text = str(e.get("text", ""))
        preview = " ".join(text.split())
        if len(preview) > 90:
            preview = preview[:90] + "…"
        ts = float(e.get("ts", 0) or 0)
        tags = [str(t) for t in e.get("tags", []) if str(t).strip()]
        sub = "📌 " if e.get("pinned") else ""
        sub += (_fmt_ago(ts) + "  •  " if ts else "") + "Enter — вставить"
        if tags:
            sub += "  •  " + " ".join("#" + t for t in tags)
        key = "clip:" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        return ResultItem("clipboard", preview, sub, "clipboard",
                        {"type": "clipboard", "text": text}, key,
                        completion=text)

    def clipboard_items(self, query: str = "") -> list[ResultItem]:
        """История буфера (#буфер): закреплённые сверху, поиск по тексту/меткам."""
        q = _norm(query.strip())
        entries = [e for e in self._clipboard_provider()
                if isinstance(e, dict)]
        entries.sort(key=lambda e: not bool(e.get("pinned")))
        items: list[ResultItem] = []
        for e in entries:
            text = str(e.get("text", ""))
            if not text:
                continue
            tags = [_norm(str(t)) for t in e.get("tags", [])]
            if q and q not in _norm(text) and not any(q in t for t in tags):
                continue
            items.append(self._clip_item(e))
        if not items:
            items = [ResultItem(
                "info",
                "Ничего не найдено в буфере" if q else "История буфера пуста",
                "Скопируйте текст — он появится здесь (#буфер); "
                "#! — постоянное хранение, #+метка — метки",
                "info", {}, "", enabled=False)]
        return items[:MAX_RESULTS]

    def _console_items(self, cmd: str) -> list[ResultItem]:
        """Провайдер консольных команд: ввод начинается с «>»."""
        if not cmd:
            return [ResultItem(
                "info", "Консольная команда",
                "После «>» введите команду, например: > ipconfig /all",
                "terminal", {}, "", enabled=False)]
        key = "console:" + cmd.lower()
        return [
            ResultItem(
                "console", f"Выполнить: {cmd}",
                "cmd.exe /c — скрыто, вывод попадёт в буфер обмена",
                "terminal",
                {"type": "console", "command": cmd, "mode": "c"},
                key, score=1000000, completion="> " + cmd),
            ResultItem(
                "console", f"В окне консоли: {cmd}",
                "cmd.exe /k — окно консоли останется открытым",
                "terminal",
                {"type": "console", "command": cmd, "mode": "k"},
                key + "|k", score=999999, completion="> " + cmd),
        ]

    def set_commands(self, raw: list[Any]) -> int:
        """Нормализует пользовательские команды; возвращает число валидных."""
        cmds: list[dict[str, Any]] = []
        for e in raw:
            if not isinstance(e, dict):
                log.warning("commands.json: пропущена запись не-объект")
                continue
            name = str(e.get("name", "")).strip()
            target = str(e.get("target", "")).strip()
            ctype = str(e.get("type", "path")).strip().lower()
            if not name or not target or ctype not in ("path", "url", "datetime",
                                                       "system", "text"):
                log.warning("commands.json: пропущена запись %r", name)
                continue
            if ctype == "system" and target not in SYSTEM_ACTIONS:
                log.warning("commands.json: неизвестное системное действие "
                            "%r", target)
                continue
            if ctype == "text":
                target = target.lower()
                if target not in TEXT_OPS:
                    log.warning("commands.json: неизвестная операция "
                                "над текстом %r", target)
                    continue
            syn = e.get("synonyms", [])
            syn = [str(s) for s in syn] if isinstance(syn, list) else []
            tags_raw = e.get("tags", [])
            if isinstance(tags_raw, str):
                tags_raw = re.split(r"[,;\s]+", tags_raw)
            if not isinstance(tags_raw, list):
                tags_raw = []
            tags = [str(t).strip().lstrip("#") for t in tags_raw]
            tags = [t for t in tags if t]
            hk_label = str(e.get("hotkey", "")).strip()
            cid = str(e.get("id", "")) or hashlib.sha1(
                f"{ctype}|{target}|{name}".encode("utf-8")).hexdigest()[:12]
            cmds.append({
                "name": name, "type": ctype, "target": target,
                "args": str(e.get("args", "")), "synonyms": syn,
                "description": str(e.get("description", "")),
                "icon": str(e.get("icon", "")), "key": cid,
                "tags": tags, "hotkey": hk_label,
            })
        self._commands = cmds
        return len(cmds)

    def user_commands(self) -> list[dict[str, Any]]:
        """Нормализованные пользовательские команды (метки и хоткеи)."""
        return list(self._commands)

    def item_for(self, cmd: dict[str, Any]) -> ResultItem:
        """ResultItem команды без поиска — для глобального хоткея."""
        return self._command_item(cmd, (50.0, [], "name", cmd["name"]),
                                  {}, time.time())

    def _command_item(self, cmd: dict[str, Any],
                      m: tuple[float, list[int], str, str],
                      usage: dict[str, Any], now: float) -> ResultItem:
        """Строит ResultItem из команды и результата fuzzy-матча."""
        score, pos, label, mtext = m
        desc, target = cmd["description"], cmd["target"]
        subtitle = desc or target
        if label == "synonym":
            subtitle = f"синоним: {mtext}" + (f" — {desc}" if desc else "")
        elif label == "tag":
            subtitle = f"метка: {mtext}" + (f" — {desc}" if desc else "")
        elif label == "target":
            subtitle = target + (f"  {cmd['args']}" if cmd["args"] else "")
        if cmd["type"] == "datetime":
            try:
                preview = datetime_value(target)
            except Exception:
                preview = target
            subtitle = preview + (f"  •  {desc}" if desc else "")

        if cmd["type"] == "settings":
            cur = self._settings_state().get(target)
            if target in ("sort_mode", "theme"):
                subtitle = f"Сейчас: {cur}  •  {desc}"
            elif cmd["args"] == "toggle":
                subtitle = ("Сейчас: вкл" if cur else "Сейчас: выкл") + f"  •  {desc}"
            elif target == "window_opacity":
                subtitle = f"Сейчас: {cur}%  •  {desc}"

        if cmd.get("tags"):
            subtitle += "  •  " + " ".join("#" + t for t in cmd["tags"])
        icon = cmd["icon"] or _default_icon(cmd["type"], target)
        confirm = (cmd["type"] == "system"
                   and target in DANGEROUS_ACTIONS)
        return ResultItem(
            kind=cmd["type"], title=cmd["name"], subtitle=subtitle,
            icon=icon,
            payload={"type": cmd["type"], "target": target,
                     "args": cmd["args"], "confirm": confirm},
            key=cmd["key"],
            score=score + _usage_bonus(usage, cmd["key"], now),
            matches=pos, completion=cmd["name"])

    def recent_commands(self, usage: dict[str, Any],
                        limit: int = MAX_RESULTS) -> list[ResultItem]:
        """Недавние команды из статистики запусков (для Ctrl+R)."""
        by_key = {c["key"]: c for c in [*self._commands, *self._system,
                                  *self._datetime, *self._text, *self._settings]}
        recs: list[tuple[float, int, str]] = []
        for key, rec in usage.items():
            if isinstance(rec, dict):
                recs.append((float(rec.get("last", 0)),
                             int(rec.get("count", 0)), str(key)))
            elif isinstance(rec, (int, float)):
                recs.append((0.0, int(rec), str(key)))
        recs.sort(reverse=True)
        items: list[ResultItem] = []
        for last, count, key in recs:
            if len(items) >= limit:
                break
            tail = f"запусков: {count}"
            if last:
                tail += f" • {_fmt_ago(last)}"
            cmd = by_key.get(key)
            if cmd:
                confirm = (cmd["type"] == "system"
                           and cmd["target"] in DANGEROUS_ACTIONS)
                items.append(ResultItem(
                    kind=cmd["type"], title=cmd["name"],
                    subtitle=(cmd["description"] or cmd["target"])
                             + "  •  " + tail,
                    icon=cmd["icon"] or _default_icon(cmd["type"],
                                                      cmd["target"]),
                    payload={"type": cmd["type"], "target": cmd["target"],
                             "args": cmd["args"], "confirm": confirm},
                    key=key, completion=cmd["name"]))
                continue
            if key.startswith("calc:"):
                expr = key[5:]
                try:
                    res = fmt_num(calc_eval(expr))
                except CalcError:
                    continue
                items.append(ResultItem(
                    "calc", f"= {res}", f"{expr}  •  {tail}", "calc",
                    {"type": "calc", "result": res}, key, completion=res))
            elif key.startswith("search:"):
                term = key[7:]
                url = ("https://www.google.com/search?q="
                       + urllib.parse.quote_plus(term))
                items.append(ResultItem(
                    "search", f"Поиск в Google: {term}", tail, "search",
                    {"type": "url", "target": url}, key,
                    completion="g " + term))
            elif key.startswith("url:"):
                url = key[4:]
                items.append(ResultItem(
                    "url", "Открыть URL", f"{url}  •  {tail}", "url",
                    {"type": "url", "target": url}, key, completion=url))
            elif key.startswith("dt:"):
                spec = key[3:]
                try:
                    preview = datetime_value(spec)
                except Exception:
                    continue
                items.append(ResultItem(
                    "datetime", f"Дата/время: {preview}",
                    f"{spec}  •  {tail}", "clock",
                    {"type": "datetime", "target": spec}, key,
                    completion=spec))
            elif key.startswith("console:"):
                cmd = key[8:]
                items.append(ResultItem(
                    "console", f"Выполнить: {cmd}", tail,
                    "terminal",
                    {"type": "console", "command": cmd,
                     "mode": "c"},
                    key, completion="> " + cmd))
            elif key.startswith("path:"):
                target, _, args = key[5:].rpartition("|")
                p = resolve_target(target)
                if not p:
                    continue
                pp = Path(p)
                icon = "folder" if pp.is_dir() else _default_icon("path", p)
                items.append(ResultItem(
                    "path", f"Открыть: {pp.name or p}",
                    p + (f"   {args}" if args else "") + "  •  " + tail,
                    icon, {"type": "path", "target": p, "args": args},
                    key, completion=p))
        return items

    def all_commands(self, usage: dict[str, Any], query: str = "",
                     limit: int = 500) -> list[ResultItem]:
        """Все команды: пользовательские + системные + дата/время + текст."""
        now = time.time()
        q = query.strip()
        pool = [*self._commands, *self._system, *self._datetime, *self._text, *self._settings]
        items: list[ResultItem] = []
        seen: set[str] = set()
        for cmd in pool:
            dk = f"{cmd['type']}|{cmd['target'].lower()}|{cmd.get('args', '')}"
            if dk in seen:
                continue
            if q:
                m = match_command(q, cmd)
                if m is None:
                    continue
            else:
                m = (50.0, [], "name", cmd["name"])
            seen.add(dk)
            items.append(self._command_item(cmd, m, usage, now))
        items.sort(key=lambda it: (-it.score, it.title.lower()))
        if not items:
            items.append(ResultItem(
                "info", "Команды не найдены",
                "Измените фильтр или добавьте записи в commands.json",
                "info", {}, "", enabled=False))
        return items[:limit]

    def _search_core(self, query: str, usage: dict[str, Any]) -> list[ResultItem]:
        """Полный поиск по всем провайдерам (в рабочем потоке)."""
        q = query.strip()
        if not q:
            return []
        items: list[ResultItem] = []
        now = time.time()
        if q.startswith(">"):
            return self._console_items(q[1:].strip())
        if q.startswith("#"):

            head = q[1:]
            if head[:1] in ("+", "-", "!") or head.lower() in ("pin", "unpin"):
                if head.startswith("+"):
                    title = "Метки: добавить " + (head[1:].strip() or "…")
                    sub = ("Enter — назначить метки отмеченным записям "
                        "(Пробел/Ctrl+Space) или последней текущей. "
                        "Пример: #+работа, личное")
                elif head.startswith("-"):
                    title = "Метки: снять " + (head[1:].strip() or "…")
                    sub = "Enter — убрать перечисленные метки у отмеченных записей"
                else:
                    title = "Буфер: постоянное хранение (📌)"
                    sub = ("Enter — закрепить/открепить отмеченные записи буфера; "
                        "закреплённые не вытесняются из истории (#pin / #unpin)")
                return [ResultItem("info", title, sub, "info", {}, "",
                                enabled=False)]

            now = time.time()
            parts = q[1:].split(None, 1)
            tag = _norm(parts[0]) if parts else ""
            rest = parts[1].strip() if len(parts) > 1 else ""
            if tag in ("буфер", "clipboard"):
                return self.clipboard_items(rest)
            pool = [*self._commands, *self._system, *self._datetime, *self._text, *self._settings]
            if not tag:
                all_tags = sorted({str(t) for c in pool
                                   for t in c.get("tags", [])})
                sub = (" ".join("#" + t for t in all_tags) if all_tags
                       else "Задайте tags в commands.json; #буфер — история; #+метка — назначить, #-метка — снять, #! — хранить запись буфера")
                return [ResultItem("info", "Метки команд", sub, "info",
                                   {}, "", enabled=False)]
            tag_items: list[ResultItem] = []
            for cmd in pool:
                tags = [_norm(str(t)) for t in cmd.get("tags", [])]
                if not any(t == tag or t.startswith(tag) for t in tags):
                    continue
                m = (match_command(rest, cmd) if rest
                     else (50.0, [], "name", cmd["name"]))
                if m is None:
                    continue
                tag_items.append(self._command_item(cmd, m, usage, now))
            tag_items.sort(key=lambda it: (-it.score, it.title.lower()))

            for e in self._clipboard_provider():
                if not isinstance(e, dict):
                    continue
                etags = [_norm(str(t)) for t in e.get("tags", [])]
                if not any(t == tag or t.startswith(tag) for t in etags):
                    continue
                text = str(e.get("text", ""))
                if rest and _norm(rest) not in _norm(text):
                    continue
                tag_items.append(self._clip_item(e))

            if not tag_items:
                tag_items.append(ResultItem(
                    "info", f"Нет команд с меткой «#{tag}»",
                    "Метки задаются полем tags; встроенные: #система, "
                    "#дата, #время, #текст, #регистр, #транслит",
                    "info", {}, "", enabled=False))
            return tag_items[:MAX_RESULTS]

        explicit_calc = q.startswith("=")
        expr = q[1:].strip() if explicit_calc else q
        calc_done = False
        if calc_candidate(expr):
            try:
                res = fmt_num(calc_eval(expr))
                items.append(ResultItem(
                    "calc", f"= {res}",
                    f"{expr}  —  Enter скопирует результат", "calc",
                    {"type": "calc", "result": res}, "calc:" + expr,
                    score=100000, completion=res))
                calc_done = True
            except CalcError as exc:
                if explicit_calc:
                    items.append(ResultItem(
                        "error", "Ошибка выражения", str(exc), "error",
                        {}, "", score=-1, enabled=False))
        if explicit_calc:
            return items or [self._nothing(q)]

        if q[:2].lower() == "g " and q[2:].strip():
            term = q[2:].strip()
            url = ("https://www.google.com/search?q="
                   + urllib.parse.quote_plus(term))
            items.append(ResultItem(
                "search", f"Поиск в Google: {term}",
                "Откроется браузер по умолчанию", "search",
                {"type": "url", "target": url}, "search:" + term.lower(),
                score=1000000, completion=q))

        seen: set[str] = set()
        for cmd in [*self._commands, *self._system, *self._datetime, *self._text, *self._settings]:
            dk = f"{cmd['type']}|{cmd['target'].lower()}|{cmd.get('args', '')}"
            if dk in seen:
                continue
            m = match_command(q, cmd)
            if m:
                seen.add(dk)
                items.append(self._command_item(cmd, m, usage, now))

        if not calc_done:
            cand = path_candidate(q)
            if cand:
                head, args = split_path_args(cand)
                p = resolve_target(head)
                if p:
                    pp = Path(p)
                    icon = ("folder" if pp.is_dir() else
                            _default_icon("path", p))
                    items.append(ResultItem(
                        "path", f"Открыть: {pp.name or p}",
                        p + (f"   {args}" if args else ""), icon,
                        {"type": "path", "target": p, "args": args},
                        f"path:{p.lower()}|{args}", score=90000,
                        completion=p))
                else:
                    items.append(ResultItem(
                        "error", "Путь не найден", expand_path(head),
                        "error", {}, "", score=-1, enabled=False))
            url = url_candidate(q)
            if url:
                items.append(ResultItem(
                    "url", "Открыть URL", url, "url",
                    {"type": "url", "target": url}, "url:" + url.lower(),
                    score=80000, completion=url))

        items.sort(key=lambda it: (-it.score, it.title.lower()))
        if not any(it.enabled for it in items):
            items.insert(0, ResultItem(
                "info", "Ничего не найдено",
                "Измените запрос или поищите в сети", "info", {}, "",
                score=-1, enabled=False))
            items.append(self._web_suggestion(q))
        return items[:MAX_RESULTS]

    def search(self, query: str, usage: dict[str, Any]) -> list[ResultItem]:
        """Пустой запрос или «*» — все команды; иначе обычный поиск."""
        q = query.strip()
        if not q:
            return self.all_commands(usage)
        if q.startswith("*"):
            return self.all_commands(usage, q[1:].strip())
        return self._search_core(query, usage)

    @staticmethod
    def _web_suggestion(q: str) -> ResultItem:
        """Строка «поискать в сети» при отсутствии совпадений."""
        url = ("https://www.google.com/search?q="
               + urllib.parse.quote_plus(q))
        return ResultItem(
            "search", f"Поиск в сети: «{q}»",
            "Откроется браузер по умолчанию", "search",
            {"type": "url", "target": url}, "search:" + q.lower(),
            score=-2, completion=f"g {q}")

    def _nothing(self, q: str) -> ResultItem:
        """Пустой результат для явного режима калькулятора."""
        return ResultItem("info", "Ничего не найдено", f"После «=» введите "
                          f"выражение (запрос: {q})", "info", {}, "",
                          enabled=False)


class SearchSignals(QObject):
    """Сигнал доставки результатов из рабочего потока в UI."""
    done = Signal(int, object)


class SearchTask(QRunnable):
    """Фоновая задача поиска или списка недавних (не блокирует UI)."""
    def __init__(self, gen: int, query: str, engine: CommandEngine,
                 usage: dict[str, Any], signals: SearchSignals,
                 mode: str = "search") -> None:
        super().__init__()
        self._gen, self._query = gen, query
        self._engine, self._usage, self._signals = engine, usage, signals
        self._mode = mode

    def run(self) -> None:
        try:
            if self._mode == "recent":
                items = self._engine.recent_commands(self._usage)
                if not items:
                    items = [ResultItem(
                        "info", "Недавних команд пока нет",
                        "Выполните команду — она появится здесь (Ctrl+R)",
                        "info", {}, "", enabled=False)]
            elif self._mode == "all":
                items = self._engine.all_commands(self._usage, self._query)
            else:
                items = self._engine.search(self._query, self._usage)
        except Exception:
            log.exception("Ошибка поиска")
            items = [ResultItem("error", "Ошибка поиска",
                                "Подробности в app.log", "error", {}, "",
                                enabled=False)]
        self._signals.done.emit(self._gen, items)


def shell_open(target: str, args: str | None = None,
               cwd: str | None = None) -> None:
    """ShellExecuteW: exe+аргументы, файлы, папки, URL — один API."""
    rc = SHELL32.ShellExecuteW(None, "open", target, args, cwd,
                               SW_SHOWNORMAL)
    code = int(rc or 0)
    if code <= 32:
        reason = SHELL_ERRORS.get(code, "ошибка ShellExecute")
        raise OSError(f"{reason} (код {code}): {target}")


def _fmt_last_error(api: str) -> str:
    """Текст последней ошибки Win32."""
    return f"{api}: {ctypes.FormatError(ctypes.get_last_error())}"


def run_util(name: str, args: str = "") -> None:
    """Запускает системную утилиту по полному пути, без окна консоли."""
    exe = resolve_target(name) or name
    line = f'"{exe}"' + (f" {args}" if args else "")
    try:
        subprocess.Popen(line, creationflags=CREATE_NO_WINDOW,
                         cwd=str(Path.home()))
    except OSError as exc:
        raise OSError(f"Не удалось запустить {name}: {exc}") from exc


class ExecSignals(QObject):
    """Сигналы завершения/ошибки запуска и вывода консольных команд."""
    finished = Signal(str)
    failed = Signal(str, str)
    consoleOutput = Signal(str, str)

class ExecTask(QRunnable):
    """Фоновое исполнение команды (ShellExecute может блокировать)."""

    def __init__(self, item: ResultItem, signals: ExecSignals) -> None:
        super().__init__()
        self._item, self._signals = item, signals

    def run(self) -> None:
        hr = OLE32.CoInitializeEx(None, 0x2)
        coinited = hr in (0, 1)
        try:
            p = self._item.payload
            t, target = p.get("type"), p.get("target", "")
            if t == "path":
                raw = expand_path(target.strip().strip('"'))
                tgt = resolve_target(target)
                if tgt is None:
                    if os.path.isabs(raw) or "\\" in raw or "/" in raw:
                        raise FileNotFoundError(f"Путь не найден: {raw}")
                    tgt = raw  # пусть ShellExecute поищет сам (App Paths)
                cwd = str(Path.home())
                if os.path.isabs(tgt) and os.path.isfile(tgt):
                    parent = str(Path(tgt).parent)
                    if not parent.lower().startswith(
                            _windows_root().lower()):
                        cwd = parent
                shell_open(tgt, p.get("args") or None, cwd)
            elif t == "url":
                shell_open(target)
            elif t == "system":
                self._system(target)
            elif t == "console":
                self._console(p)
            else:
                raise ValueError(f"неизвестный тип команды: {t}")
            self._signals.finished.emit(self._item.title)
        except Exception as exc:
            self._signals.failed.emit(self._item.title, str(exc))
        finally:
            if coinited:
                OLE32.CoUninitialize()

    def _console(self, p: dict[str, Any]) -> None:
        """Консольная команда: /c скрыто (вывод в буфер) либо /k в окне."""
        cmd = str(p.get("command", "")).strip()
        if not cmd:
            raise ValueError("пустая консольная команда")
        comspec = os.environ.get("ComSpec", r"C:\Windows\system32\cmd.exe")
        if str(p.get("mode", "c")) == "k":
            shell_open(comspec, f"/k {cmd}", str(Path.home()))
            return
        try:
            r = subprocess.run(
                f'"{comspec}" /c {cmd}',
                creationflags=CREATE_NO_WINDOW, cwd=str(Path.home()),
                capture_output=True, text=True, encoding="oem",
                errors="replace", timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise OSError("Команда не завершилась за 120 секунд") from exc
        out = (r.stdout or "")
        if r.stderr:
            out += ("\n" if out else "") + r.stderr
        out = out.strip()
        if r.returncode != 0:
            tail = out[-400:] if out else f"код возврата {r.returncode}"
            raise OSError(f"Команда завершилась с ошибкой:\n{tail}")
        self._signals.consoleOutput.emit(cmd, out)

    @staticmethod
    def _system(action: str) -> None:
        """Системные действия Win32 (с запасными вариантами запуска)."""
        if action == "lock":
            if not USER32.LockWorkStation():
                run_util("rundll32.exe", "user32.dll,LockWorkStation")
        elif action == "sleep":
            if not POWRPROF.SetSuspendState(False, True, False):
                raise OSError(
                    "Не удалось перевести компьютер в сон: "
                    + _fmt_last_error("SetSuspendState")
                    + ". При включённой гибернации Windows уходит в "
                      "гибернацию (отключается: powercfg /h off).")
        elif action in ("shutdown", "restart"):
            flag = "/s" if action == "shutdown" else "/r"
            run_util("shutdown.exe", f"{flag} /t 0")
        elif action == "recycle":
            try:
                shell_open("shell:RecycleBinFolder")
            except OSError:
                run_util("explorer.exe", "shell:RecycleBinFolder")
        elif action == "empty_recycle":
            hr = SHELL32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4)
            if hr not in (0, 1):
                code = hr & 0xFFFFFFFF
                if code == 0x8000FFFF:
                    return  # корзина уже пуста — не ошибка
                raise OSError("Не удалось очистить корзину "
                              f"(HRESULT {code:#010x})")
        elif action == "restart_explorer":
            exe = resolve_target("taskkill.exe") or "taskkill.exe"
            subprocess.run(f'"{exe}" /f /im explorer.exe',
                        creationflags=CREATE_NO_WINDOW, timeout=10,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            tl = resolve_target("tasklist.exe") or "tasklist.exe"
            deadline = time.time() + 3.0
            back = False
            while time.time() < deadline:
                time.sleep(0.4)
                out = subprocess.run(
                    f'"{tl}" /fi "imagename eq explorer.exe" /nh',
                    creationflags=CREATE_NO_WINDOW, timeout=10,
                    capture_output=True, text=True, errors="ignore").stdout.lower()
                if "explorer.exe" in out:
                    back = True
                    break
            if not back:
                run_util("explorer.exe")
        else:
            raise ValueError(f"неизвестное действие: {action}")


# ---- эмуляция Ctrl+V (SendInput) для вставки даты/времени ----
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL, VK_V = 0x11, 0x56
ULONG_PTR = ctypes.c_size_t

class KEYBDINPUT(ctypes.Structure):
    """Win32 KEYBDINPUT."""
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]

class MOUSEINPUT(ctypes.Structure):
    """Win32 MOUSEINPUT — нужен для корректного размера union INPUT."""
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

class HARDWAREINPUT(ctypes.Structure):
    """Win32 HARDWAREINPUT — нужен для корректного размера union INPUT."""
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT),
                ("hi", HARDWAREINPUT)]

class INPUT(ctypes.Structure):
    """Win32 INPUT для SendInput."""
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

if IS_WIN:
    USER32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT),
                                 ctypes.c_int]
    USER32.SendInput.restype = wintypes.UINT
    USER32.IsWindow.argtypes = [wintypes.HWND]
    USER32.IsWindow.restype = wintypes.BOOL

def send_ctrl_v() -> bool:
    """Шлёт Ctrl+V в активное окно; False, если SendInput не сработал."""
    # v1.9.1: отпускаем зажатые Alt/Ctrl/Shift/Win — иначе при хоткее
    # Ctrl+Alt+D целевое окно получит Ctrl+Alt+V и вставки не будет
    release_held_modifiers()
    seq = ((VK_CONTROL, 0), (VK_V, 0),
           (VK_V, KEYEVENTF_KEYUP), (VK_CONTROL, KEYEVENTF_KEYUP))
    arr = (INPUT * len(seq))()
    for i, (vk, flags) in enumerate(seq):
        arr[i].type = INPUT_KEYBOARD
        arr[i].ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags,
                               time=0, dwExtraInfo=0)
    sent = USER32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))
    return int(sent) == len(seq)

# ============================================================================
# СЕКЦИЯ 6b. ОПЕРАЦИИ НАД ВЫДЕЛЕННЫМ ТЕКСТОМ (регистр, транслитерация)
# ============================================================================
TEXT_OPS = ("upper", "lower", "title", "capitalize", "invert",
            "translit", "untranslit")
TEXT_ICONS = {"upper": "AA", "lower": "aa", "title": "Aa",
              "capitalize": "A.", "invert": "aA",
              "translit": "Яa", "untranslit": "aЯ"}
CP_TEXT_MARK = "\u200bCommandPalette:text-op\u200b"
TEXT_MAX_LEN = 200_000
VK_C = 0x43
VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x12, 0x5B, 0x5C
KEYEVENTF_EXTENDEDKEY = 0x0001

# Простая схема без диакритики (близка к BGN/PCGN): пригодна для имён,
# ников и имён файлов; ъ/ь исчезают, щ -> shch, х -> kh, ц -> ts.
_TRANSLIT_RU = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g",
}
# Обратный транслит приближённый (исходные ъ/ь/й/ы не восстановимы).
_TRANSLIT_BACK = tuple(sorted((
    ("shch", "щ"), ("sch", "щ"), ("yo", "ё"), ("zh", "ж"), ("kh", "х"),
    ("ts", "ц"), ("ch", "ч"), ("sh", "ш"), ("yu", "ю"), ("ya", "я"),
    ("ju", "ю"), ("ja", "я"), ("iy", "ий"), ("yy", "ый"),
    ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"),
    ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"), ("j", "й"),
    ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"),
    ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"),
    ("u", "у"), ("v", "в"), ("w", "в"), ("x", "кс"), ("y", "й"),
    ("z", "з"), ("'", "ь"),
), key=lambda p: -len(p[0])))

_WORD_RE = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)

if IS_WIN:
    USER32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    USER32.GetAsyncKeyState.restype = wintypes.SHORT


def _case_like(first: str, out: str, prev: str, nxt: str) -> str:
    """Переносит регистр исходного символа на многобуквенную замену."""
    if not out or not first.isupper():
        return out
    if prev.isupper() or nxt.isupper():
        return out.upper()
    return out[:1].upper() + out[1:]


def translit_ru_to_lat(s: str) -> str:
    """Кириллица -> латиница с сохранением регистра (Щукин -> Shchukin)."""
    out: list[str] = []
    n = len(s)
    for i, ch in enumerate(s):
        mapped = _TRANSLIT_RU.get(ch.lower())
        if mapped is None:
            out.append(ch)
            continue
        prev = s[i - 1] if i else ""
        nxt = s[i + 1] if i + 1 < n else ""
        out.append(_case_like(ch, mapped, prev, nxt))
    return "".join(out)


def translit_lat_to_ru(s: str) -> str:
    """Латиница -> кириллица (приблизительно): Privet -> Привет."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        chunk = s[i:i + 4].lower()
        hit = next(((lat, rus) for lat, rus in _TRANSLIT_BACK
                    if chunk.startswith(lat)), None)
        if hit is None:
            out.append(s[i])
            i += 1
            continue
        lat, rus = hit
        prev = s[i - 1] if i else ""
        j = i + len(lat)
        nxt = s[j] if j < n else ""
        out.append(_case_like(s[i], rus, prev, nxt))
        i = j
    return "".join(out)


def text_transform(op: str, s: str) -> str:
    """Применяет операцию регистра/транслитерации к строке."""
    op = op.strip().lower()
    if op == "upper":
        return s.upper()
    if op == "lower":
        return s.lower()
    if op == "title":
        return _WORD_RE.sub(lambda m: m.group(0).capitalize(), s)
    if op == "capitalize":
        return s.capitalize()
    if op == "invert":
        return s.swapcase()
    if op == "translit":
        return translit_ru_to_lat(s)
    if op == "untranslit":
        return translit_lat_to_ru(s)
    raise ValueError(f"неизвестная операция над текстом: {op}")


def _text_commands() -> list[dict[str, Any]]:
    """Встроенные команды над выделенным текстом (target = операция)."""
    raw = [
        ("Текст: ВЕРХНИЙ РЕГИСТР", "upper",
         ["upper", "uppercase", "верхний регистр", "заглавные",
          "прописные", "caps"],
         "Выделенный текст -> ВСЕ ЗАГЛАВНЫЕ", ["текст", "регистр"]),
        ("Текст: нижний регистр", "lower",
         ["lower", "lowercase", "нижний регистр", "строчные",
          "маленькие буквы"],
         "Выделенный текст -> все строчные", ["текст", "регистр"]),
        ("Текст: Каждое Слово С Заглавной", "title",
         ["title", "title case", "каждое слово", "заглавные слова"],
         "Выделенный текст -> Каждое Слово С Заглавной",
         ["текст", "регистр"]),
        ("Текст: Как в предложении", "capitalize",
         ["capitalize", "sentence", "первая буква", "как в предложении"],
         "Первая буква заглавная, остальные строчные",
         ["текст", "регистр"]),
        ("Текст: иНВЕРТИРОВАТЬ рЕГИСТР", "invert",
         ["invert", "swapcase", "swap case", "инвертировать",
          "обратный регистр", "сменить регистр"],
         "Выделенный текст -> обмен регистра каждой буквы",
         ["текст", "регистр"]),
        ("Транслит: кириллица -> латиница", "translit",
         ["translit", "transliterate", "транслит", "транслитерация",
          "латиница", "romanize", "slug"],
         "Щукин -> Shchukin (выделенный текст)", ["текст", "транслит"]),
        ("Транслит: латиница -> кириллица", "untranslit",
         ["untranslit", "детранслит", "кириллица", "обратный транслит",
          "cyrillic"],
         "Privet -> Привет (приблизительно)", ["текст", "транслит"]),
    ]
    return [{"name": n, "type": "text", "target": op, "args": "",
             "synonyms": syn, "description": d,
             "icon": TEXT_ICONS.get(op, "Aa"), "key": f"txt:{op}",
             "tags": tags, "hotkey": ""} for n, op, syn, d, tags in raw]


def _send_inputs(seq: tuple[tuple[int, int], ...]) -> bool:
    """Отправляет последовательность (vk, flags) через SendInput."""
    if not seq:
        return True
    arr = (INPUT * len(seq))()
    for i, (vk, flags) in enumerate(seq):
        arr[i].type = INPUT_KEYBOARD
        arr[i].ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags,
                               time=0, dwExtraInfo=0)
    return int(USER32.SendInput(len(seq), arr,
                                ctypes.sizeof(INPUT))) == len(seq)


def release_held_modifiers() -> None:
    """Отпускает физически зажатые Alt/Ctrl/Shift/Win.

    Иначе после хоткея Alt+Space синтетический Ctrl+C превращается в
    Ctrl+Alt+C и целевое приложение ничего не копирует.
    """
    seq: list[tuple[int, int]] = []
    for vk, ext in ((VK_MENU, 0), (VK_CONTROL, 0), (VK_SHIFT, 0),
                    (VK_LWIN, KEYEVENTF_EXTENDEDKEY),
                    (VK_RWIN, KEYEVENTF_EXTENDEDKEY)):
        if USER32.GetAsyncKeyState(vk) & 0x8000:
            seq.append((vk, KEYEVENTF_KEYUP | ext))
    if seq:
        _send_inputs(tuple(seq))
        time.sleep(0.02)


def send_ctrl_c() -> bool:
    """Шлёт Ctrl+C в активное окно (копирование выделения)."""
    release_held_modifiers()
    return _send_inputs(((VK_CONTROL, 0), (VK_C, 0),
                         (VK_C, KEYEVENTF_KEYUP),
                         (VK_CONTROL, KEYEVENTF_KEYUP)))


def run_async(fn: Callable[[], None]) -> None:
    """Мелкие фоновые действия с логированием ошибок."""
    def wrapper() -> None:
        try:
            fn()
        except Exception:
            log.exception("Ошибка фоновой задачи")
    QThreadPool.globalInstance().start(QRunnable.create(wrapper))


# ============================================================================
# СЕКЦИЯ 7. ВИДЖЕТ ОКНА И СПИСКА ПОДСКАЗОК (иконки, модель, делегат, окно)
# ============================================================================

class Icons:
    """Рисованные иконки: цветная плашка + символ/эмодзи, кэш pixmap'ов."""

    KIND = {
        "app": ("▶", "#5b8cff"), "folder": ("📁", "#e8b34b"),
        "file": ("📄", "#9aa5bb"), "url": ("🌐", "#4fc3f7"),
        "search": ("🔎", "#7ee08a"), "calc": ("🧮", "#ffb74d"),
        "lock": ("🔒", "#e57373"), "sleep": ("🌙", "#b39ddb"),
        "power": ("⏻", "#ef5350"), "restart": ("🔄", "#4db6ac"),
        "trash": ("🗑", "#a1887f"), "terminal": ("❯", "#81c784"),
        "settings": ("⚙", "#90a4ae"), "info": ("ℹ", "#64b5f6"),
        "error": ("⚠", "#ff8a65"),
        "clock": ("🕒", "#4dd0e1"),
        "history": ("🕘", "#80cbc4"),
        "clipboard": ("📋", "#4db6ac"),
        "console": ("❯", "#81c784"),
        "history": ("🕘", "#80cbc4"),
    }
    FALLBACK = "●"
    _pm_cache: dict[tuple[str, str, int], QPixmap] = {}
    _glyph_cache: dict[str, tuple[str, str | None, str]] = {}

    @classmethod
    def _resolve(cls, spec: str) -> tuple[str, str | None, str]:
        """Подбирает глиф и шрифт, который его точно содержит."""
        if spec in cls._glyph_cache:
            return cls._glyph_cache[spec]
        color: str | None
        if spec in cls.KIND:
            glyph, color = cls.KIND[spec]
        elif spec and len(spec) <= 2:
            glyph, color = spec, None
        else:
            glyph, color = cls.FALLBACK, None
        family = None
        for fam in ("Segoe UI Emoji", "Segoe UI Symbol", "Segoe UI"):
            if QFontMetrics(QFont(fam)).inFontUcs4(ord(glyph[0])):
                family = fam
                break
        if family is None:
            glyph, family = cls.FALLBACK, "Segoe UI"
        res = (glyph, color, family)
        cls._glyph_cache[spec] = res
        return res

    @classmethod
    def pixmap(cls, spec: str, accent: str, size: int = 32) -> QPixmap:
        """QPixmap иконки заданного размера (кэшируется)."""
        key = (spec, accent, size)
        if key in cls._pm_cache:
            return cls._pm_cache[key]
        glyph, color, family = cls._resolve(spec)
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 20))
        p.drawRoundedRect(0, 0, size, size, size * 0.26, size * 0.26)
        f = QFont(family)
        f.setPixelSize(int(size * 0.58))
        p.setFont(f)
        p.setPen(QPen(QColor(color or accent)))
        p.drawText(pm.rect(), Qt.AlignCenter, glyph)
        p.end()
        cls._pm_cache[key] = pm
        return pm


def app_icon(accent: str) -> QIcon:
    """Иконка трея/приложения: плашка акцентного цвета с символом «❯»."""
    glyph = "❯" if QFontMetrics(QFont("Segoe UI Symbol")).inFontUcs4(0x276F) \
        else ">"
    ic = QIcon()
    for s in (16, 20, 24, 32, 48, 64):
        pm = QPixmap(s, s)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(accent))
        p.drawRoundedRect(0, 0, s, s, s * 0.22, s * 0.22)
        p.setFont(QFont("Segoe UI", max(8, int(s * 0.5)), QFont.Bold))
        p.setPen(QPen(QColor("#ffffff")))
        p.drawText(pm.rect(), Qt.AlignCenter, glyph)
        p.end()
        ic.addPixmap(pm)
    return ic


def active_screen() -> QScreen:
    """Монитор активного (foreground) окна; запасные — курсор/primary."""
    scr: QScreen | None = None
    if IS_WIN:
        try:
            hwnd = USER32.GetForegroundWindow()
            if hwnd:
                hmon = USER32.MonitorFromWindow(hwnd, 2)
                mi = MONITORINFOEXW()
                mi.cbSize = ctypes.sizeof(mi)
                if USER32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    for s in QGuiApplication.screens():
                        if s.name() == mi.szDevice:
                            scr = s
                            break
        except Exception:
            log.exception("Не удалось определить активный монитор")
    return (scr or QGuiApplication.screenAt(QCursor.pos())
            or QGuiApplication.primaryScreen())


class ResultsModel(QAbstractListModel):
    """Список ResultItem для QListView."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.items: list[ResultItem] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        if role == Qt.UserRole:
            return self.items[index.row()]
        if role == Qt.DisplayRole:
            return self.items[index.row()].title
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        it = self.item(index)
        if it and it.enabled:
            return Qt.ItemIsEnabled | Qt.ItemIsSelectable
        return Qt.NoItemFlags

    def item(self, index: QModelIndex) -> ResultItem | None:
        if index.isValid() and 0 <= index.row() < len(self.items):
            return self.items[index.row()]
        return None

    def set_items(self, items: list[ResultItem]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def first_enabled(self) -> int:
        for i, it in enumerate(self.items):
            if it.enabled:
                return i
        return -1

    def enabled_rows(self) -> list[int]:
        return [i for i, it in enumerate(self.items) if it.enabled]


KIND_TAGS = {"path": "путь", "url": "URL", "system": "система",
             "search": "поиск", "calc": "калькулятор",
             "datetime": "дата/время", "console": "консоль",
             "info": "", "error": "", "history": "история",
             "clipboard": "буфер", "text": "текст", "settings": "настройка"}

class ResultDelegate(QStyledItemDelegate):
    """Отрисовка строки: иконка, заголовок с подсветкой, подзаголовок, тег."""

    def __init__(self, view: QListView, palette_win: "PaletteWindow") -> None:
        super().__init__(view)
        self._view = view
        self._pal = palette_win
        self._title_font = QFont("Segoe UI")
        self._sub_font = QFont("Segoe UI")
        self._title_fm = QFontMetrics(self._title_font)
        self._sub_fm = QFontMetrics(self._sub_font)

    def refresh_fonts(self) -> None:
        """Пересоздаёт шрифты из текущей темы (при смене конфига)."""
        th = self._pal.theme
        self._title_font = QFont("Segoe UI")
        self._title_font.setPointSizeF(float(th["title_pt"]))
        self._title_font.setWeight(QFont.DemiBold)
        self._sub_font = QFont("Segoe UI")
        self._sub_font.setPointSizeF(float(th["sub_pt"]))
        self._title_fm = QFontMetrics(self._title_font)
        self._sub_fm = QFontMetrics(self._sub_font)

    def sizeHint(self, option: Any, index: QModelIndex) -> QSize:
        return QSize(10, int(self._pal.row_height))

    def paint(self, painter: QPainter, option: Any,
              index: QModelIndex) -> None:
        item: ResultItem | None = index.data(Qt.UserRole)
        if item is None:
            return
        th = self._pal.theme
        marked = (self._pal.is_marked(item)
                  if hasattr(self._pal, "is_marked") else False)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        r = option.rect.adjusted(6, 1, -6, -1)
        selected = bool(option.state & QStyle.State_Selected) and item.enabled
        hover = (self._view.property("hover_row") == index.row()
                 and item.enabled)
        painter.setPen(Qt.NoPen)
        if selected:
            c = QColor(th["accent"])
            c.setAlpha(95 if marked else 70)
            painter.setBrush(c)
            painter.drawRoundedRect(r, 8, 8)
        elif marked:
            c = QColor(th["accent"])
            c.setAlpha(38)
            painter.setBrush(c)
            painter.drawRoundedRect(r, 8, 8)
        elif hover:
            painter.setBrush(QColor(255, 255, 255, 14))
            painter.drawRoundedRect(r, 8, 8)
        if marked:
            painter.setBrush(QColor(th["accent"]))
            painter.drawRoundedRect(
                QRect(r.left() + 2, r.top() + 7, 3, max(6, r.height() - 14)),
                1.5, 1.5)
        pm = Icons.pixmap(item.icon, th["accent"], 32)
        ir = QRect(r.left() + 10, r.top() + (r.height() - 26) // 2, 26, 26)
        painter.drawPixmap(ir, pm)
        x0, reserve = r.left() + 48, 86
        tw = max(40, r.width() - 48 - reserve)
        title_color = QColor(th["text"] if item.enabled else th["subtext"])
        painter.setFont(self._title_font)
        title = self._title_fm.elidedText(item.title, Qt.ElideRight, tw)
        matches = set(item.matches)
        x, i, n = float(x0), 0, len(title)
        base_y = r.top() + 20
        while i < n:
            j = i
            hi = i in matches
            while j < n and (j in matches) == hi:
                j += 1
            seg = title[i:j]
            painter.setPen(QPen(QColor(th["accent"] if hi else title_color)))
            painter.drawText(QPointF(x, base_y), seg)
            x += self._title_fm.horizontalAdvance(seg)
            i = j
        painter.setFont(self._sub_font)
        sub = self._sub_fm.elidedText(item.subtitle, Qt.ElideRight, tw + 40)
        painter.setPen(QPen(QColor(th["subtext"])))
        painter.drawText(QPointF(x0, r.top() + 38), sub)
        tag = "Enter ↵" if selected else KIND_TAGS.get(item.kind, "")
        if marked:
            tag = ("✓ " + tag).strip()
        if tag:
            painter.setPen(QPen(QColor(th["accent"] if selected or marked
                                       else th["subtext"])))
            painter.drawText(QRect(r.right() - reserve, r.top(),
                                   reserve - 10, r.height()),
                             Qt.AlignVCenter | Qt.AlignRight, tag)
        painter.restore()


INPUT_QSS = Template(
    "QLineEdit { background: $panel; color: $text;"
    " border: 1px solid $border; border-radius: 10px; padding: 0 14px;"
    " font-size: ${font_pt}pt; selection-background-color: $accent;"
    " selection-color: #ffffff; }"
    "QLineEdit:focus { border: 1px solid $accent; }")

LIST_QSS = Template(
    "QListView { background: transparent; border: none; outline: none; }"
    "QScrollBar:vertical { background: transparent; width: 8px;"
    " margin: 2px; }"
    "QScrollBar::handle:vertical { background: $border;"
    " border-radius: 4px; min-height: 24px; }"
    "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
    " { height: 0; }"
    "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
    " { background: transparent; }")

MENU_QSS = Template(
    "QMenu { background: $panel; color: $text; border: 1px solid $border;"
    " padding: 6px; }"
    "QMenu::item { padding: 6px 24px; border-radius: 6px; }"
    "QMenu::item:selected { background: $accent; color: #ffffff; }"
    "QMenu::separator { height: 1px; background: $border;"
    " margin: 6px 8px; }")


class PaletteWindow(QWidget):
    """Безрамочное окно палитры: поле ввода + список подсказок."""

    executeRequested = Signal(object, str, bool)
    executeBatchRequested = Signal(object, str, bool)
    userResized = Signal(int, int)
    sortOrderChanged = Signal(object)
    sortModeChanged = Signal(str)
    pinChanged = Signal(bool)

    tagCommandRequested = Signal(str, object)


    def __init__(self, engine: CommandEngine,
                 usage_provider: Callable[[], dict[str, Any]],
                 history_provider: Callable[[], list[str]],
                 clipboard_provider: Callable[[], list[Any]] | None = None,
                 sort_order_provider: Callable[[], list[str]] | None = None
                 ) -> None:
        super().__init__()
        self._engine = engine
        self._usage_provider = usage_provider
        self._history_provider = history_provider
        self._clipboard_provider = clipboard_provider or (lambda: [])
        self._sort_order_provider = sort_order_provider or (lambda: [])
        self._gen = 0
        self._marked: set[str] = set()

        self._marked_items: dict[str, ResultItem] = {}
        self._last_current: ResultItem | None = None

        self._hist_idx = -1
        self.prev_hwnd = 0
        self._last_query = ""
        self._last_items = []
        self._last_row = -1
        self._last_scroll = 0
        self._fg_seen = False
        self._sort_mode = "score"
        self._under_cursor = False
        self._show_all_on_empty = True
        self._phrases_enabled = False
        self._pinned = False
        self._phrases: list[str] = []
        self._dragging = False
        self._drag_off = None
        self._resize_edge = ""
        self._resize_geo = None
        self._outside_timer = QTimer(self)
        self._outside_timer.setInterval(200)
        self._outside_timer.timeout.connect(self._check_outside_click)
        self.theme: dict[str, Any] = {}
        self.row_height = 48
        self._max_rows = 9
        self._width = 640
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self._input = QLineEdit(self)
        self._input.setPlaceholderText(
            "Команда, путь, URL, «> консоль», «g запрос», #метка, #буфер…  "
            "Пробел — отметить  •  #+метка / #-метка / #! — метки и хранение  "
            "•  Ctrl+1..4 — сортировка  •  Ctrl+R — недавние  •  Ctrl+H — история")
        self._input.setFixedHeight(INPUT_H)
        self._input.setClearButtonEnabled(True)
        self._input.installEventFilter(self)
        self._model = ResultsModel(self)
        self._list = QListView(self)
        self._list.setModel(self._model)
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._list.setUniformItemSizes(True)
        self._list.setSpacing(0)
        self._list.setViewportMargins(0, LIST_PAD // 2, 0, LIST_PAD // 2)
        self._list.setMouseTracking(True)
        self._list.viewport().setMouseTracking(True)
        self._list.viewport().installEventFilter(self)
        self._list.setProperty("hover_row", -1)
        self._delegate = ResultDelegate(self._list, self)
        self._list.setItemDelegate(self._delegate)
        self._list.clicked.connect(self._on_clicked)
        self._list.entered.connect(self._on_entered)
        self._list.hide()
        self._phrases_bar = QWidget(self)
        self._phrases_bar.setFixedHeight(PHRASES_H)
        self._phrases_lay = QHBoxLayout(self._phrases_bar)
        self._phrases_lay.setContentsMargins(2, 0, 2, 0)
        self._phrases_lay.setSpacing(6)
        self._phrases_bar.hide()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        lay.setSpacing(SPACING)
        lay.addWidget(self._input)
        lay.addWidget(self._phrases_bar)
        lay.addWidget(self._list)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(70)
        self._debounce.timeout.connect(self._start_search)
        self._search_signals = SearchSignals()
        self._search_signals.done.connect(self._on_results)
        self._input.textChanged.connect(self._on_text_changed)
        self._input.textEdited.connect(self._on_text_edited)

    # ---- конфигурация/тема ----

    def apply_config(self, cfg: dict[str, Any]) -> None:
        """Применяет тему, размеры, дебаунс и опции v1.7 из config.json."""
        self._gen += 1
        self.theme = {
            "bg": cfg["color_bg"], "panel": cfg["color_panel"],
            "text": cfg["color_text"], "subtext": cfg["color_subtext"],
            "accent": cfg["color_accent"], "border": cfg["color_border"],
            "title_pt": cfg["title_font_pt"],
            "sub_pt": cfg["subtitle_font_pt"],
        }
        self.row_height = int(cfg["row_height"])
        self._max_rows = int(cfg["max_visible_rows"])
        self._width = int(cfg["window_width"])
        self._debounce.setInterval(int(cfg["debounce_ms"]))
        self._under_cursor = bool(cfg.get("window_under_cursor", False))
        self._show_all_on_empty = bool(cfg.get("show_all_on_empty", True))
        self._sort_mode = str(cfg.get("sort_mode", "score"))
        self._phrases_enabled = bool(cfg.get("phrases_enabled", False))
        self._pinned = bool(cfg.get("pin_window", False))  # v1.8
        try:
            _op = int(cfg.get("window_opacity", 100))
        except (TypeError, ValueError):
            _op = 100
        self.setWindowOpacity(max(30, min(100, _op)) / 100.0)
        self.setWindowTitle(APP_NAME + (" — закреплено" if self._pinned else ""))
        ph = cfg.get("phrases", [])
        self._phrases = ([str(s) for s in ph][:12]
                         if isinstance(ph, list) else [])
        th = self.theme
        self._input.setStyleSheet(INPUT_QSS.substitute(
            panel=th["panel"], text=th["text"], border=th["border"],
            accent=th["accent"], font_pt=cfg["input_font_pt"]))
        pal = self._input.palette()
        pal.setColor(QPalette.PlaceholderText, QColor(th["subtext"]))
        self._input.setPalette(pal)
        self._list.setStyleSheet(LIST_QSS.substitute(border=th["border"]))
        self._delegate.refresh_fonts()
        self._rebuild_phrases()
        self._update_height()
        self.update()

    # ---- показ/скрытие ----

    def is_pinned(self) -> bool:
        """v1.8: окно закреплено — не скрывается при потере фокуса."""
        return bool(getattr(self, "_pinned", False))

    def set_pinned(self, on: bool) -> None:
        self._pinned = bool(on)
        self.setWindowTitle(APP_NAME + (" — закреплено" if self._pinned else ""))
        self.update()
        self.pinChanged.emit(self._pinned)

    def toggle_pinned(self) -> None:
        self.set_pinned(not self.is_pinned())

    def toggle(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.open()

    def open(self) -> None:
        """Показ: активный монитор или под курсором; ввод восстанавливается."""
        self.prev_hwnd = (int(USER32.GetForegroundWindow() or 0)
                          if IS_WIN else 0)
        self._gen += 1
        self._hist_idx = -1
        self._input.blockSignals(True)
        self._input.setText(self._last_query)
        self._input.blockSignals(False)
        self._input.selectAll()
        self._debounce.stop()
        self._model.set_items(list(self._last_items))
        self._update_height()
        row = self._last_row
        if not (0 <= row < len(self._model.items)
                and self._model.items[row].enabled):
            row = self._model.first_enabled()
        if row >= 0:
            idx = self._model.index(row, 0)
            self._list.setCurrentIndex(idx)
            self._list.scrollTo(idx)
        self._list.verticalScrollBar().setValue(self._last_scroll)
        self._list.setProperty("hover_row", -1)
        if self._last_query.strip():
            if not self._last_items:
                self._debounce.start()
        elif self._show_all_on_empty:
            self.show_all()
        else:
            self._gen += 1
            self._model.set_items([])
            self._update_height()
        if self._under_cursor:
            pos = QCursor.pos()
            scr = QGuiApplication.screenAt(pos) or active_screen()
            geo = scr.availableGeometry()
            x = min(max(pos.x() - self.width() // 2, geo.left()),
                    max(geo.left(), geo.right() - self.width() + 1))
            y = min(max(pos.y() + 18, geo.top()),
                    max(geo.top(), geo.bottom() - self.height() + 1))
        else:
            scr = active_screen()
            geo = scr.availableGeometry()
            x = geo.center().x() - self.width() // 2
            y = geo.top() + max(12, geo.height() // 6 - self.height() // 2)
        self.winId()  # реализует нативное окно до setScreen
        wh = self.windowHandle()
        if wh:
            wh.setScreen(scr)
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        if IS_WIN:
            USER32.SetForegroundWindow(wintypes.HWND(int(self.winId())))
        self._input.setFocus()
        QTimer.singleShot(40, self._refocus)
        self._fg_seen = False
        self._outside_timer.start()

    def _refocus(self) -> None:
        """Повторная попытка фокуса (особенности Windows foreground)."""
        if self.isVisible():
            self.raise_()
            self.activateWindow()
            self._input.setFocus()

    def hideEvent(self, event: QEvent) -> None:
        """Запоминает последний ввод; останавливает слежение за фокусом."""
        if getattr(self, "_input", None) is not None:
            self._last_query = self._input.text()
        if getattr(self, "_model", None) is not None:
            self._last_items = list(self._model.items)
            self._last_row = self._list.currentIndex().row()
            self._last_scroll = self._list.verticalScrollBar().value()
        if getattr(self, "_outside_timer", None) is not None:
            self._outside_timer.stop()
        super().hideEvent(event)

    def _check_outside_click(self) -> None:
        """Клик мимо окна: активным стало чужое окно — скрываемся."""
        if self._pinned:  # v1.8: закреплённое окно не скрываем
            return
        if not self.isVisible():
            self._outside_timer.stop()
            return
        if (QApplication.activePopupWidget() is not None
                or QApplication.activeModalWidget() is not None):
            return
        if not IS_WIN:
            return
        fg = int(USER32.GetForegroundWindow() or 0)
        if fg == int(self.winId()):
            self._fg_seen = True
        elif self._fg_seen and fg:
            self.hide()

    def _mouse_over_list(self) -> bool:
        """Курсор физически находится над списком результатов."""
        if not self._list.isVisible():
            return False
        vp = self._list.viewport()
        return vp.rect().contains(vp.mapFromGlobal(QCursor.pos()))

    def changeEvent(self, event: QEvent) -> None:
        """Скрытие при потере фокуса (кроме своих popup/menu)."""
        if (event.type() == QEvent.WindowDeactivate and self.isVisible() and not self._pinned
                and QApplication.activePopupWidget() is None
                and QApplication.activeModalWidget() is None):
            self.hide()
        super().changeEvent(event)

    def paintEvent(self, event: Any) -> None:
        """Фон, разделитель, уголок ресайза и строка состояния отметок."""
        th = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(BG_INSET, BG_INSET, -BG_INSET,
                                 -BG_INSET - 1)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawRoundedRect(r.adjusted(0, 2, 0, 1), 14, 14)
        p.setBrush(QColor(th["bg"]))
        p.drawRoundedRect(r, 14, 14)
        p.setPen(QPen(QColor(th["border"]), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 14, 14)
        if self._list.isVisible():
            y = MARGIN + INPUT_H + SPACING // 2
            if not self._phrases_bar.isHidden():  # v1.8: панель учитывается и при скрытом окне
                y += PHRASES_H + SPACING
            c = QColor(th["border"])
            c.setAlpha(140)
            p.setPen(QPen(c, 1))
            p.drawLine(MARGIN + 8, y, self.width() - MARGIN - 8, y)
        # Уголок изменения размера (нижний правый).
        p.setPen(QPen(QColor(th["border"]), 1))
        gx = self.width() - BG_INSET - 5
        gy = self.height() - BG_INSET - 6
        for k in range(3):
            p.drawLine(gx - 3 - 4 * k, gy, gx, gy - 3 - 4 * k)
        marked = getattr(self, "_marked", None)
        if marked:
            p.setFont(QFont("Segoe UI", 9))
            p.setPen(QPen(QColor(th["accent"])))
            p.drawText(
                QRect(MARGIN + 10, self.height() - MARGIN - MARK_FOOTER_H,
                      self.width() - 2 * MARGIN - 20, MARK_FOOTER_H - 4),
                Qt.AlignVCenter | Qt.AlignLeft,
                f"Отмечено: {len(marked)}  •  Enter — выполнить все  "
                f"•  Ctrl+U — снять отметки")
        p.end()

    # ---- поиск с дебаунсом ----

    def _on_text_changed(self, _text: str) -> None:
        """Пустой ввод: все команды (если опция включена) либо ничего."""
        self._current_item()  # кэш для команд #+/#-/#
        self._debounce.stop()
        if self._input.text().strip():
            self._debounce.start()
        elif self._show_all_on_empty:
            self.show_all()
        else:
            self._gen += 1
            self._model.set_items([])
            self._update_height()

    def _on_text_edited(self, _text: str) -> None:
        self._hist_idx = -1

    def _start_search(self) -> None:
        query = self._input.text()
        if not query.strip():
            return
        self._gen += 1
        self._keep_sel_key = None  # новый запрос — выделение на первый пункт
        usage = dict(self._usage_provider())
        QThreadPool.globalInstance().start(SearchTask(
            self._gen, query, self._engine, usage, self._search_signals))
    @Slot(int, object)
    def _on_results(self, gen: int, items: list[ResultItem]) -> None:
        if gen != self._gen or not self.isVisible():
            return
        items = self._sort_items(items)
        self._model.set_items(items)
        self._update_height()
        self._list.setProperty("hover_row", -1)
        self._list.verticalScrollBar().setValue(0)
        # v1.9.1: восстановление выделения после refresh_current()
        keep = getattr(self, "_keep_sel_key", None)
        self._keep_sel_key = None
        row = -1
        if keep:
            for k, it in enumerate(self._model.items):
                if it.enabled and self._item_id(it) == keep:
                    row = k
                    break
        if row < 0:
            row = self._model.first_enabled()
        if row >= 0:
            idx = self._model.index(row, 0)
            self._list.setCurrentIndex(idx)
            self._list.scrollTo(idx)
            self._last_current = self._model.item(idx)

    def _update_height(self) -> None:
        """Высота под контент + панель фраз/отметок; окно можно ресайзить."""
        marked = getattr(self, "_marked", None)
        if marked and not self._is_tag_command():
            ids = {self._item_id(it) for it in self._model.items if it.enabled}
            marked.intersection_update(ids)
            self._marked_items = {k: v for k, v in self._marked_items.items()
                                  if k in marked}
        vis = min(len(self._model.items), self._max_rows)
        if vis == 0:
            self._list.hide()
            self._list.setFixedHeight(0)
            h = MARGIN * 2 + INPUT_H
        else:
            self._list.show()
            lh = vis * self.row_height + LIST_PAD
            self._list.setFixedHeight(lh)
            h = MARGIN * 2 + INPUT_H + SPACING + lh
        if not self._phrases_bar.isHidden():  # v1.8: панель учитывается и при скрытом окне
            h += PHRASES_H + SPACING
        if marked:
            h += MARK_FOOTER_H
        self.setMinimumSize(360, MARGIN * 2 + INPUT_H)
        self.setMaximumSize(16777215, 16777215)
        self.resize(self._width, h)
        self.update()

    # ---- клавиатура и мышь ----

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._input and event.type() == QEvent.KeyPress:
            key, mods = event.key(), event.modifiers()
            plain = mods == Qt.NoModifier
            ctrl = mods == Qt.ControlModifier
            if key == Qt.Key_R and ctrl:
                self.show_recent()
                return True
            if key == Qt.Key_H and ctrl:
                self.show_history()
                return True
            if key == Qt.Key_L and ctrl:
                self.show_all()
                return True
            if key in (Qt.Key_Space, Qt.Key_M) and ctrl:
                self._toggle_mark()
                return True
            if key == Qt.Key_Space and mods == Qt.ShiftModifier:
                self._toggle_mark()
                return True
            if key == Qt.Key_Space and plain and not self._input.text():
                # Пробел при пустой строке — отметка пункта, не ввод пробела.
                self._toggle_mark()
                return True
            if key in (Qt.Key_Up, Qt.Key_Down) and ctrl:
                self._manual_move(-1 if key == Qt.Key_Up else 1)
                return True
            if key == Qt.Key_A and mods == (Qt.ControlModifier
                                            | Qt.ShiftModifier):
                self._mark_all()
                return True
            if key == Qt.Key_U and ctrl:
                self._clear_marks()
                return True
            if key == Qt.Key_P and ctrl:  # v1.8: закрепить/открепить окно
                self.toggle_pinned()
                return True

            if ctrl and key in (Qt.Key_1, Qt.Key_2, Qt.Key_3, Qt.Key_4):
                self._set_sort_mode_local(
                    {Qt.Key_1: "score", Qt.Key_2: "name",
                    Qt.Key_3: "last", Qt.Key_4: "manual"}[key])
                return True

            if key == Qt.Key_Escape:
                self.hide()
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                # Ctrl+Enter / Shift+Enter — выполнить, не закрывая окно.
                keep = bool(mods & (Qt.ControlModifier | Qt.ShiftModifier))
                self._accept(keep)
                return True
            if key in (Qt.Key_Tab, Qt.Key_Backtab):
                self._complete()
                return True
            if key == Qt.Key_Up and plain:
                if (self._mouse_over_list()
                        or not self._history_up()):
                    self._move(-1)
                return True
            if key == Qt.Key_Down and plain:
                if (self._mouse_over_list()
                        or not self._history_down()):
                    self._move(1)
                return True
            if key == Qt.Key_PageUp and plain:
                self._move(-self._max_rows)
                return True
            if key == Qt.Key_PageDown and plain:
                self._move(self._max_rows)
                return True
            if (key == Qt.Key_Backspace and ctrl
                    and not self._input.hasSelectedText()):
                self._input.cursorWordBackward(True)
                self._input.del_()
                return True
        if obj is self._list.viewport():
            if event.type() == QEvent.Leave:
                self._list.setProperty("hover_row", -1)
                self._list.viewport().update()
            elif (event.type() == QEvent.MouseButtonPress
                  and event.button() == Qt.LeftButton
                  and not self._list.indexAt(
                      event.position().toPoint()).isValid()):
                # Перетаскивание окна за пустую область списка.
                self._begin_move(event.globalPosition().toPoint())
                return True
        return super().eventFilter(obj, event)

    def _move(self, delta: int) -> None:
        """Циклический переход по доступным (enabled) строкам."""
        rows = self._model.enabled_rows()
        if not rows:
            return
        cur = self._list.currentIndex().row()
        pos = rows.index(cur) if cur in rows else (0 if delta > 0 else -1)
        nxt = rows[(pos + delta) % len(rows)]
        idx = self._model.index(nxt, 0)
        self._list.setCurrentIndex(idx)
        self._list.scrollTo(idx)
        self._list.setProperty("hover_row", -1)
        self._list.viewport().update()


    def _current_item(self) -> ResultItem | None:
        it = self._model.item(self._list.currentIndex())
        if it and it.enabled:
            self._last_current = it
            return it
        row = self._model.first_enabled()
        if row >= 0:
            it = self._model.item(self._model.index(row, 0))
            self._last_current = it
            return it
        return None

    def _accept(self, keep_open: bool = False) -> None:
        """Enter: отмеченные команды выполняются пакетно, иначе — текущая."""

        text = self._input.text().strip()
        if (text.startswith(("#+", "#-", "#!"))
                or text.lower() in ("#pin", "#unpin")):
            self.tagCommandRequested.emit(text, self.tag_targets())
            return

        marked = self.marked_items()
        if len(marked) > 1:
            self.executeBatchRequested.emit(marked, self._input.text(), keep_open)
            self._clear_marks()
            return
        it = marked[0] if marked else self._current_item()
        if it:
            self.executeRequested.emit(it, self._input.text(), keep_open)
            self._clear_marks()

    def _complete(self) -> None:
        it = self._current_item()
        if it:
            self._input.setText(it.completion or it.title)
            self._input.setCursorPosition(len(self._input.text()))

    def show_recent(self) -> None:
        """Ctrl+R: недавние команды (список строится в фоне)."""
        self._gen += 1
        self._debounce.stop()
        self._list.setProperty("hover_row", -1)
        QThreadPool.globalInstance().start(SearchTask(
            self._gen, "", self._engine, dict(self._usage_provider()),
            self._search_signals, mode="recent"))

    def show_all(self, query: str = "") -> None:
        """Список всех команд (пустой ввод, «*» или Ctrl+L)."""
        self._gen += 1
        self._debounce.stop()
        self._list.setProperty("hover_row", -1)
        QThreadPool.globalInstance().start(SearchTask(
            self._gen, query, self._engine, dict(self._usage_provider()),
            self._search_signals, mode="all"))

    # ---- отметки для пакетного выполнения ----
    @staticmethod
    def _item_id(it: ResultItem) -> str:
        return it.key or f"{it.kind}|{it.title}"

    def is_marked(self, it: ResultItem) -> bool:
        return self._item_id(it) in getattr(self, "_marked", set())

    def marked_items(self) -> list[ResultItem]:
        return [it for it in self._model.items
                if it.enabled and self.is_marked(it)]

    def _toggle_mark(self, advance: bool = False) -> None:
        """Пробел / Ctrl+Space / Ctrl+M / Shift+клик — отметить строку.
        v1.7: выделение по умолчанию НЕ перемещается на следующий пункт."""
        it = self._model.item(self._list.currentIndex())
        if it is None or not it.enabled:
            return
        iid = self._item_id(it)
        if iid in self._marked:
            self._marked.discard(iid)
            self._marked_items.pop(iid, None)
        else:
            self._marked.add(iid)
            self._marked_items[iid] = it
        if advance:
            self._move(1)
        self._update_height()
        self._list.viewport().update()


    def _mark_all(self) -> None:
        """Ctrl+Shift+A — отметить все доступные строки / снять все."""
        rows = [it for it in self._model.items if it.enabled]
        if not rows:
            return
        if all(self.is_marked(it) for it in rows):
            self._marked.clear()
            self._marked_items.clear()
        else:
            for it in rows:
                iid = self._item_id(it)
                self._marked.add(iid)
                self._marked_items[iid] = it
        self._update_height()
        self._list.viewport().update()


    def _clear_marks(self) -> None:
        """Ctrl+U — снять все отметки."""
        if not self._marked and not self._marked_items:
            return
        self._marked.clear()
        self._marked_items.clear()
        self._update_height()
        self._list.viewport().update()

    def _is_tag_command(self) -> bool:
        """Строка ввода — команда меток/хранения (#+, #-, #!, #pin/#unpin)."""
        t = self._input.text().strip()
        return (t.startswith(("#+", "#-", "#!"))
                or t.lower() in ("#pin", "#unpin"))

    def tag_targets(self) -> list[ResultItem]:
        """Цели команды меток: отмеченные, иначе последняя текущая запись."""
        items = self.marked_items()
        if items:
            return items
        remembered = [it for k, it in self._marked_items.items()
                    if k in self._marked]
        if remembered:
            return remembered
        cur = self._current_item() or self._last_current
        return [cur] if cur is not None else []

    def after_tag_command(self) -> None:
        """После команды меток: сброс отметок и обновление списка."""
        self._clear_marks()
        self._last_current = None
        self._input.clear()
        self.show_all()

    def refresh_current(self) -> None:
        """Перестроить текущий список (после смены настроек/меток)."""
        # v1.9.1: сохраняем текущий пункт, чтобы выделение не уезжало
        cur = self._current_item()
        self._keep_sel_key = self._item_id(cur) if cur is not None else None
        q = self._input.text()
        self._gen += 1
        self._debounce.stop()
        if q.strip():
            QThreadPool.globalInstance().start(SearchTask(
                self._gen, q, self._engine, dict(self._usage_provider()),
                self._search_signals))
        else:
            self.show_all()

    def _set_sort_mode_local(self, mode: str) -> None:
        """Ctrl+1..4: смена сортировки списка с сохранением в config."""
        if mode == self._sort_mode:
            return
        self._sort_mode = mode
        self.sortModeChanged.emit(mode)
        self.resort_current()

    def show_clipboard(self) -> None:
        """Список истории буфера обмена (хоткей или тег #буфер)."""
        if not self.isVisible():
            self.open()
        self._debounce.stop()
        self._input.clear()
        self._gen += 1
        items = self._engine.clipboard_items()
        self._model.set_items(items)
        self._update_height()
        self._list.setProperty("hover_row", -1)
        row = self._model.first_enabled()
        if row >= 0:
            self._list.setCurrentIndex(self._model.index(row, 0))
            self._last_current = self._model.item(self._model.index(row, 0))

    def show_history(self) -> None:
        """Ctrl+H: полный список истории ввода."""
        self._gen += 1
        self._debounce.stop()
        hist = [str(h) for h in self._history_provider() if str(h).strip()]
        items = [ResultItem(
            "history", q, "Enter — подставить и найти", "history",
            {"type": "history", "query": q}, "hist:" + q, completion=q)
            for q in hist[:MAX_RESULTS]]
        if not items:
            items = [ResultItem("info", "История пуста",
                                "Выполненные запросы появятся здесь",
                                "info", {}, "", enabled=False)]
        self._model.set_items(items)
        self._update_height()
        self._list.setProperty("hover_row", -1)
        row = self._model.first_enabled()
        if row >= 0:
            self._list.setCurrentIndex(self._model.index(row, 0))
            self._last_current = self._model.item(self._model.index(row, 0))

    def input_selection(self) -> str:
        """Выделенный текст в поле ввода (только когда окно видно)."""
        if not self.isVisible() or not self._input.hasSelectedText():
            return ""
        return self._input.selectedText()

    def replace_input_selection(self, text: str) -> None:
        """Заменяет выделение в поле ввода результатом преобразования."""
        self._input.insert(text)
        self._input.setFocus()

    def set_query(self, text: str) -> None:
        """Подставляет текст в поле и запускает поиск."""
        self._input.setText(text)
        self._input.setCursorPosition(len(text))
        self._input.setFocus()
        self._debounce.start()

    def _history_up(self) -> bool:
        """↑ при пустом поле (или в режиме истории) листает историю."""
        hist = self._history_provider()
        if not hist:
            return False
        if self._hist_idx < 0:
            if self._input.text():
                return False
            self._hist_idx = 0
        elif self._hist_idx + 1 < len(hist):
            self._hist_idx += 1
        else:
            return True
        self._input.setText(hist[self._hist_idx])
        self._input.setCursorPosition(len(self._input.text()))
        return True

    def _history_down(self) -> bool:
        """↓ в режиме истории — к более новым, затем к пустой строке."""
        if self._hist_idx < 0:
            return False
        hist = self._history_provider()
        if self._hist_idx > 0:
            self._hist_idx -= 1
            self._input.setText(hist[self._hist_idx])
            self._input.setCursorPosition(len(self._input.text()))
        else:
            self._hist_idx = -1
            self._input.clear()
        return True

    def _on_clicked(self, index: QModelIndex) -> None:
        it = self._model.item(index)
        if not (it and it.enabled):
            return
        mods = QApplication.keyboardModifiers()
        if mods & Qt.ShiftModifier:
            # Shift+клик — отметить строку для пакетного выполнения.
            self._list.setCurrentIndex(index)
            self._toggle_mark(advance=False)
            return
        # Ctrl+клик — выполнить, не закрывая окно.
        keep = bool(mods & Qt.ControlModifier)
        self.executeRequested.emit(it, self._input.text(), keep)

    def _on_entered(self, index: QModelIndex) -> None:
        self._list.setProperty("hover_row", index.row())
        self._list.viewport().update()


    # ---- v1.7: панель быстрых фраз ----
    def _rebuild_phrases(self) -> None:
        """Пересобирает кнопки быстрых фраз из config.json."""
        while self._phrases_lay.count():
            it = self._phrases_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        th = self.theme or {"panel": "#232833", "subtext": "#9aa5bb",
                            "text": "#e9edf5", "accent": "#5b8cff",
                            "border": "#3a4256"}
        qss = ("QPushButton { background: %s; color: %s;"
               " border: 1px solid %s; border-radius: 9px;"
               " padding: 2px 10px; font-size: 9pt; }"
               "QPushButton:hover { color: %s; border-color: %s; }"
               % (th["panel"], th["subtext"], th["border"],
                  th["text"], th["accent"]))
        for phr in self._phrases:
            b = QPushButton(phr.strip() or "␣", self._phrases_bar)
            b.setToolTip("Вставить в строку ввода: " + repr(phr))
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet(qss)
            b.clicked.connect(lambda _=False, s=phr: self._insert_phrase(s))
            self._phrases_lay.addWidget(b)
        self._phrases_lay.addStretch(1)
        self._phrases_bar.setVisible(
            bool(self._phrases_enabled and self._phrases))

    def _insert_phrase(self, s: str) -> None:
        self._input.insert(s)
        self._input.setFocus()

    # ---- v1.7: сортировка списка ----
    def _sort_items(self, items: list[ResultItem]) -> list[ResultItem]:
        mode = self._sort_mode
        if mode == "name":
            return sorted(items, key=lambda it: (not it.enabled,
                                                 it.title.lower()))
        if mode == "last":
            usage = self._usage_provider()

            def last(it: ResultItem) -> float:
                rec = usage.get(it.key)
                if isinstance(rec, dict):
                    return float(rec.get("last", 0) or 0)
                return 0.0

            return sorted(items, key=lambda it: (not it.enabled, -last(it),
                                                 it.title.lower()))
        if mode == "manual":
            order = {str(k): i for i, k
                     in enumerate(self._sort_order_provider())}
            big = len(order) + 1
            return sorted(items, key=lambda it: (
                not it.enabled, order.get(self._item_id(it), big),
                -it.score, it.title.lower()))
        return items

    def resort_current(self) -> None:
        """Пересортировать текущий список после смены режима."""
        cur = self._current_item()
        items = self._sort_items(list(self._model.items))
        self._model.set_items(items)
        row = -1
        if cur is not None:
            for i, it in enumerate(items):
                if self._item_id(it) == self._item_id(cur):
                    row = i
                    break
        if row < 0:
            row = self._model.first_enabled()
        if row >= 0:
            idx = self._model.index(row, 0)
            self._list.setCurrentIndex(idx)
            self._list.scrollTo(idx)
        self._update_height()

    def _manual_move(self, delta: int) -> None:
        """Ctrl+↑/↓: ручное перемещение пункта; включает режим «manual»."""
        rows = self._model.enabled_rows()
        cur = self._list.currentIndex().row()
        if cur not in rows or len(rows) < 2:
            return
        pos = rows.index(cur)
        np = pos + delta
        if not 0 <= np < len(rows):
            return
        other = rows[np]
        items = list(self._model.items)
        a, b = items[cur], items[other]
        items[cur], items[other] = items[other], items[cur]
        order = [str(k) for k in self._sort_order_provider()]
        seen = set(order)
        for it in items:
            k = self._item_id(it)
            if it.enabled and k not in seen:
                order.append(k)
                seen.add(k)
        ka, kb = self._item_id(a), self._item_id(b)
        if ka in seen and kb in seen:
            ia, ib = order.index(ka), order.index(kb)
            order[ia], order[ib] = order[ib], order[ia]
        self.sortOrderChanged.emit(order)
        if self._sort_mode != "manual":
            self._sort_mode = "manual"
            self.sortModeChanged.emit("manual")
        self._model.set_items(items)
        idx = self._model.index(other, 0)
        self._list.setCurrentIndex(idx)
        self._list.scrollTo(idx)
        self._update_height()

    # ---- v1.7: перемещение и изменение размера окна мышью ----
    def _edge_at(self, pos: Any) -> str:
        g = RESIZE_GRIP
        r = self.rect()
        right = pos.x() >= r.right() - g
        bottom = pos.y() >= r.bottom() - g
        if right and bottom:
            return "br"
        if right:
            return "r"
        if bottom:
            return "b"
        return ""

    def _begin_move(self, global_pos: Any) -> None:
        self._drag_off = global_pos - self.frameGeometry().topLeft()
        self._dragging = True

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.LeftButton:
            edge = self._edge_at(event.position().toPoint())
            if edge:
                self._resize_edge = edge
                self._resize_geo = (event.globalPosition().toPoint(),
                                    self._width, self._max_rows)
            else:
                self._begin_move(event.globalPosition().toPoint())
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        gp = event.globalPosition().toPoint()
        if self._dragging:
            self.move(gp - self._drag_off)
            return
        if self._resize_edge:
            g0, w0, r0 = self._resize_geo
            dx, dy = gp.x() - g0.x(), gp.y() - g0.y()
            if self._resize_edge in ("r", "br"):
                self._width = max(360, min(2000, w0 + dx))
            if self._resize_edge in ("b", "br"):
                rh = max(1, self.row_height)
                self._max_rows = max(1, min(40, (r0 * rh + dy) // rh))
            self._update_height()
            return
        edge = self._edge_at(event.position().toPoint())
        if edge == "br":
            self.setCursor(Qt.SizeFDiagCursor)
        elif edge == "r":
            self.setCursor(Qt.SizeHorCursor)
        elif edge == "b":
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if self._resize_edge:
            self._resize_edge = ""
            self.userResized.emit(int(self._width), int(self._max_rows))
        self._dragging = False
        super().mouseReleaseEvent(event)

# ============================================================================
# СЕКЦИЯ 8. ГЛОБАЛЬНЫЙ ХОТКЕЙ (RegisterHotKey в отдельном потоке, ctypes)
# ============================================================================

def vk_for(key: str) -> int:
    """Имя клавиши → виртуальный код Win32."""
    k = key.lower()
    if len(k) == 1:
        if "a" <= k <= "z":
            return ord(k.upper())
        if "0" <= k <= "9":
            return ord(k)
        if k in PUNCT_VK:
            return PUNCT_VK[k]
    if k in NAMED_VK:
        return NAMED_VK[k]
    m = re.fullmatch(r"f(\d{1,2})", k)
    if m and 1 <= int(m[1]) <= 24:
        return 0x6F + int(m[1])
    raise ValueError(f"неизвестная клавиша: {key}")


def parse_hotkey(s: str) -> tuple[int, int]:
    """«Ctrl+Alt+K» → (модификаторы, VK). Требует модификатор (кроме F13+)."""
    parts = [p.strip() for p in re.split(r"\+(?!$)", s.strip()) if p.strip()]
    if not parts:
        raise ValueError("пустой хоткей")
    mods, key = 0, None
    for p in parts:
        t = p.lower()
        if t in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif t == "alt":
            mods |= MOD_ALT
        elif t == "shift":
            mods |= MOD_SHIFT
        elif t in ("win", "meta", "super"):
            mods |= MOD_WIN
        else:
            if key is not None:
                raise ValueError(f"лишняя клавиша в хоткее: {p}")
            key = p
    if key is None:
        raise ValueError("не указана основная клавиша")
    vk = vk_for(key)
    if mods == 0 and not 0x7C <= vk <= 0x87:
        raise ValueError("нужен модификатор Ctrl/Alt/Shift/Win "
                         "(или клавиша F13–F24)")
    return mods, vk


class HotkeyManager(QObject):
    """Регистрирует хоткей в выделенном потоке с GetMessage-loop."""

    triggered = Signal()
    failed = Signal(str)

    def __init__(self, hotkey_id: int = HOTKEY_ID) -> None:
        super().__init__()
        self._id = hotkey_id
        self._thread: threading.Thread | None = None
        self._tid: int | None = None

    def start(self, hotkey: str) -> None:
        """(Пере)регистрирует хоткей; пустая строка — хоткей отключён."""
        self.stop()
        label = hotkey.strip()
        if not label:
            log.info("Глобальный хоткей отключён (пустая строка в config)")
            return
        try:
            mods, vk = parse_hotkey(label)
        except ValueError as exc:
            msg = f"Хоткей «{label}» неверен: {exc}"
            log.error(msg)
            self.failed.emit(msg)
            return
        self._tid = None
        self._thread = threading.Thread(
            target=self._run, args=(mods, vk, label),
            name="Hotkey", daemon=True)
        self._thread.start()

    def _run(self, mods: int, vk: int, label: str) -> None:
        self._tid = int(KERNEL32.GetCurrentThreadId())
        if not USER32.RegisterHotKey(None, self._id,
                                     mods | MOD_NOREPEAT, vk):
            err = ctypes.get_last_error()
            msg = (f"Не удалось зарегистрировать {label} (код {err}). "
                   f"Комбинация занята — измените hotkey в config.json.")
            log.error(msg)
            self.failed.emit(msg)
            return
        log.info("Хоткей зарегистрирован: %s", label)
        try:
            msg = MSG()
            while True:
                ret = USER32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):
                    break
                if msg.message == WM_HOTKEY and msg.wParam == self._id:
                    self.triggered.emit()
        finally:
            USER32.UnregisterHotKey(None, self._id)
            log.info("Хоткей снят: %s", label)

    def stop(self) -> None:
        """Останавливает поток через WM_QUIT и ждёт завершения."""
        t = self._thread
        if t and t.is_alive():
            for _ in range(50):
                if self._tid:
                    break
                time.sleep(0.01)
            if self._tid:
                for _ in range(20):
                    if USER32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0):
                        break
                    time.sleep(0.01)
            t.join(timeout=2.0)
        self._thread = None


# ============================================================================
# СЕКЦИЯ 9. ТРЕЙ, АВТОЗАПУСК (единственный ключ реестра Run) И КОНТРОЛЛЕР
# ============================================================================

class Autorun:
    """HKCU\\...\\Run — только автозапуск, данные в реестре не хранятся."""

    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    VALUE = "CommandPalette"

    @staticmethod
    def command() -> str:
        """Корректная абсолютная команда запуска (exe или pythonw+скрипт)."""
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
        py = Path(sys.executable)
        pyw = py.with_name("pythonw.exe")
        interp = pyw if pyw.exists() else py
        return f'"{interp}" "{Path(__file__).resolve()}"'

    @classmethod
    def is_enabled(cls) -> bool:
        if winreg is None:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.RUN_KEY) as k:
                val, _ = winreg.QueryValueEx(k, cls.VALUE)
                return val == cls.command()
        except OSError:
            return False

    @classmethod
    def set(cls, enabled: bool) -> None:
        if winreg is None:
            raise OSError("winreg недоступен")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                if enabled:
                    winreg.SetValueEx(k, cls.VALUE, 0, winreg.REG_SZ,
                                      cls.command())
                else:
                    try:
                        winreg.DeleteValue(k, cls.VALUE)
                    except FileNotFoundError:
                        pass
        except OSError as exc:
            raise OSError(f"Ошибка реестра: {exc}") from exc


class App(QObject):
    """Контроллер: хранилища, движок, окно, хоткей, трей, статистика."""

    def __init__(self, qapp: QApplication, writable: bool) -> None:
        super().__init__()
        self._qapp = qapp
        self._writable = writable
        self.config_store = JsonStore(CONFIG_PATH, DEFAULT_CONFIG, writable,
                                      lambda d: isinstance(d, dict))
        self.commands_store = JsonStore(COMMANDS_PATH, DEFAULT_COMMANDS,
                                        writable, _valid_commands)
        self.state_store = JsonStore(STATE_PATH, DEFAULT_STATE, writable,
                                     lambda d: isinstance(d, dict))
        for store in (self.config_store, self.commands_store,
                      self.state_store):
            store.load()
        self._clamp_config()
        self.state_store.data.setdefault("history", [])
        self.state_store.data.setdefault("usage", {})
        self.state_store.data.setdefault("sort_order", [])
        self.state_store.data.setdefault("tag_overrides", {})
        self._migrate_datetime_commands()
        self.engine = CommandEngine()
        self.engine.set_clipboard_provider(
            lambda: self.state_store.data.get("clipboard", []))
        n = self.engine.set_commands(
            self.commands_store.data.get("commands", []))
        log.info("Загружено пользовательских команд: %d", n)

        self.engine.set_settings_provider(lambda: self.config_store.data)
        self.engine.set_tag_overrides(
            self.state_store.data.get("tag_overrides", {}))

        self.palette = PaletteWindow(
            self.engine,
            usage_provider=lambda: self.state_store.data["usage"],
            history_provider=lambda: self.state_store.data["history"],
            clipboard_provider=lambda: self.state_store.data.get(
                "clipboard", []),
            sort_order_provider=lambda: self.state_store.data.get(
                "sort_order", []))
        self.palette.apply_config(self.config_store.data)
        self.palette.executeRequested.connect(self._on_execute)
        self.palette.executeBatchRequested.connect(self._on_execute_batch)
        self.palette.userResized.connect(self._on_user_resized)
        self.palette.sortOrderChanged.connect(self._on_sort_order)
        self.palette.sortModeChanged.connect(self._on_sort_mode)
        self.palette.pinChanged.connect(self._on_pin_changed)
        self.palette.tagCommandRequested.connect(self._on_tag_command)
        self._batch_queue: list[ResultItem] = []
        self._exec_signals = ExecSignals()
        self._exec_signals.finished.connect(self._on_exec_finished)
        self._exec_signals.failed.connect(self._on_exec_failed)
        self._exec_signals.consoleOutput.connect(self._on_console_output)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(2000)
        self._save_timer.timeout.connect(self.flush_state)
        self._build_tray()
        self.hotkey = HotkeyManager()
        self.hotkey.triggered.connect(self.toggle)
        self.hotkey.failed.connect(
            lambda m: self.notify(m, QSystemTrayIcon.Warning, 6000))
        self.hotkey.start(self.config_store.data["hotkey"])
        self.clip_hotkey = HotkeyManager(CLIP_HOTKEY_ID)
        self.clip_hotkey.triggered.connect(self.show_clipboard)
        self.clip_hotkey.failed.connect(
            lambda m: self.notify(m, QSystemTrayIcon.Warning, 6000))
        self.clip_hotkey.start(
            self.config_store.data.get("clipboard_hotkey", ""))
        self._init_clipboard()
        self._cmd_hotkeys = []
        self._setup_command_hotkeys()
        self._startup_warnings()

    # ---- конфигурация ----

    def _clamp_config(self) -> None:
        """Сливает с дефолтами и ограничивает диапазоны значений."""
        d = dict(DEFAULT_CONFIG)
        d.update({k: v for k, v in self.config_store.data.items()
                  if k in DEFAULT_CONFIG})
        limits = {"window_width": (360, 2000), "max_visible_rows": (1, 40),
                  "row_height": (36, 72), "debounce_ms": (30, 300),
                  "history_limit": (5, 500),
                  "datetime_paste_delay_ms": (40, 600),
                  "clipboard_limit": (5, 200), "window_opacity": (30, 100)}
        for k, (lo, hi) in limits.items():
            try:
                d[k] = max(lo, min(hi, int(d[k])))
            except (TypeError, ValueError):
                d[k] = DEFAULT_CONFIG[k]
        flimits = {"input_font_pt": (8.0, 24.0),
                   "title_font_pt": (8.0, 20.0),
                   "subtitle_font_pt": (6.0, 14.0)}
        for k, (lo, hi) in flimits.items():
            try:
                d[k] = max(lo, min(hi, float(d[k])))
            except (TypeError, ValueError):
                d[k] = DEFAULT_CONFIG[k]

        if "theme" in self.config_store.data:
            theme = str(d.get("theme", "")).strip().lower()
        else:
            theme = "custom"
            for name, cols in THEMES.items():
                if all(str(d.get(k, "")).lower() == v.lower()
                    for k, v in cols.items()):
                    theme = name
                    break
        if theme not in THEMES:
            theme = "custom"
        d["theme"] = theme
        if theme in THEMES:
            d.update(THEMES[theme])

        for k in DEFAULT_CONFIG:
            if k.startswith("color_"):
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(d[k])):
                    d[k] = DEFAULT_CONFIG[k]
        d["hotkey"] = str(d["hotkey"]).strip()
        for k in ("datetime_insert", "clipboard_history",
                  "window_under_cursor", "show_all_on_empty",
                  "phrases_enabled", "pin_window"):
            d[k] = (str(d.get(k, DEFAULT_CONFIG[k])).strip().lower()
                    not in ("false", "0", "no", "off"))
        d["clipboard_hotkey"] = str(d.get("clipboard_hotkey", "")).strip()
        d["sort_mode"] = str(d.get("sort_mode", "score")).strip().lower()
        if d["sort_mode"] not in ("score", "name", "last", "manual"):
            d["sort_mode"] = "score"
        ph = d.get("phrases", DEFAULT_CONFIG["phrases"])
        d["phrases"] = ([str(s)[:60] for s in ph][:12]
                        if isinstance(ph, list)
                        else list(DEFAULT_CONFIG["phrases"]))
        self.config_store.data = d

    # ---- трей ----

    def _build_tray(self) -> None:
        cfg = self.config_store.data
        self.tray = QSystemTrayIcon(app_icon(cfg["color_accent"]), self)
        self._menu = QMenu()
        self._menu.setStyleSheet(MENU_QSS.substitute(
            panel=cfg["color_panel"], text=cfg["color_text"],
            border=cfg["color_border"], accent=cfg["color_accent"]))
        hk = cfg["hotkey"] or "хоткей отключён"
        self._act_show = self._menu.addAction(f"Показать ({hk})")
        self._act_show.triggered.connect(self.toggle)
        act = self._menu.addAction("Все команды (Ctrl+L)")
        act.triggered.connect(self.show_all_commands)
        self._menu.addSeparator()
        act = self._menu.addAction("Перезагрузить команды")
        act.triggered.connect(self.reload)
        act = self._menu.addAction("Открыть папку программы")
        act.triggered.connect(
            lambda: run_async(lambda: shell_open(str(BASE_DIR))))
        act = self._menu.addAction("Открыть папку данных (data)")
        act.triggered.connect(
            lambda: run_async(lambda: shell_open(str(DATA_DIR))))
        self._act_autorun = self._menu.addAction("Автозапуск")
        self._act_autorun.setCheckable(True)
        self._act_autorun.triggered.connect(self._toggle_autorun)
        self._act_dt = self._menu.addAction("Дата/время: вставлять в окно")
        self._act_dt.setCheckable(True)
        self._act_dt.triggered.connect(self._toggle_dt_insert)
        self._act_cursor = self._menu.addAction("Окно под курсором мыши")
        self._act_cursor.setCheckable(True)
        self._act_cursor.triggered.connect(self._toggle_under_cursor)
        self._act_empty = self._menu.addAction("Все команды при пустом вводе")
        self._act_empty.setCheckable(True)
        self._act_empty.triggered.connect(self._toggle_show_all_empty)
        self._act_phrases = self._menu.addAction("Панель быстрых фраз")
        self._act_phrases.setCheckable(True)
        self._act_phrases.triggered.connect(self._toggle_phrases)
        self._act_pin = self._menu.addAction("Закрепить окно (Ctrl+P)")
        self._act_pin.setCheckable(True)
        self._act_pin.setToolTip("Не скрывать при потере фокуса; поверх всех окон")
        self._act_pin.triggered.connect(self._toggle_pin)
        self._opacity_menu = self._menu.addMenu("Прозрачность окна")
        self._opacity_menu.setStyleSheet(self._menu.styleSheet())
        self._opacity_group = QActionGroup(self)
        self._opacity_group.setExclusive(True)
        self._opacity_acts: dict[int, Any] = {}
        for _pct in (100, 90, 80, 70, 60, 50, 40, 30):
            _a = self._opacity_menu.addAction(f"{_pct}%")
            _a.setCheckable(True)
            self._opacity_group.addAction(_a)
            _a.triggered.connect(lambda _=False, p=_pct: self._set_opacity(p))
            self._opacity_acts[_pct] = _a

        self._theme_menu = self._menu.addMenu("Тема оформления")
        self._theme_menu.setStyleSheet(self._menu.styleSheet())
        self._theme_group = QActionGroup(self)
        self._theme_group.setExclusive(True)
        self._theme_acts: dict[str, Any] = {}
        for _tid, _tname in THEME_NAMES.items():
            _a = self._theme_menu.addAction(_tname)
            _a.setCheckable(True)
            self._theme_group.addAction(_a)
            _a.triggered.connect(lambda _=False, t=_tid: self._set_theme(t))
            self._theme_acts[_tid] = _a

        self._sort_menu = self._menu.addMenu("Сортировка списка")
        self._sort_menu.setStyleSheet(self._menu.styleSheet())
        self._sort_group = QActionGroup(self)
        self._sort_group.setExclusive(True)
        self._sort_acts: dict[str, Any] = {}
        for mode, label in (("score", "По релевантности (Ctrl+1)"),
                            ("name", "По названию (Ctrl+2)"),
                            ("last", "По времени последнего вызова (Ctrl+3)"),
                            ("manual", "Ручная (Ctrl+4, Ctrl+↑/↓)")):
            a = self._sort_menu.addAction(label)
            a.setCheckable(True)
            self._sort_group.addAction(a)
            a.triggered.connect(
                lambda _=False, m=mode: self._set_sort_mode(m))
            self._sort_acts[mode] = a
        act = self._menu.addAction("История буфера обмена")
        act.triggered.connect(self.show_clipboard)
        self._menu.addSeparator()
        act = self._menu.addAction("Выход")
        act.triggered.connect(self._qapp.quit)
        self._menu.aboutToShow.connect(self._menu_about)
        self.tray.setContextMenu(self._menu)
        self.tray.setToolTip(f"{APP_NAME} — {hk}")
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _menu_about(self) -> None:
        cfg = self.config_store.data
        self._act_autorun.setChecked(Autorun.is_enabled())
        self._act_dt.setChecked(bool(cfg.get("datetime_insert", True)))
        self._act_cursor.setChecked(
            bool(cfg.get("window_under_cursor", False)))
        self._act_empty.setChecked(bool(cfg.get("show_all_on_empty", True)))
        self._act_phrases.setChecked(bool(cfg.get("phrases_enabled", False)))
        self._act_pin.setChecked(bool(cfg.get("pin_window", False)))
        _pct = int(cfg.get("window_opacity", 100))
        if _pct in self._opacity_acts:
            self._opacity_acts[_pct].setChecked(True)

        _tid = str(cfg.get("theme", "dark"))
        if _tid in getattr(self, "_theme_acts", {}):
            self._theme_acts[_tid].setChecked(True)

        mode = str(cfg.get("sort_mode", "score"))
        if mode in self._sort_acts:
            self._sort_acts[mode].setChecked(True)
        hk = cfg["hotkey"] or "хоткей отключён"
        self._act_show.setText(f"Показать ({hk})")

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason
                        ) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self.toggle()

    def _toggle_autorun(self, checked: bool) -> None:
        try:
            Autorun.set(checked)
            self.notify("Автозапуск " + ("включён" if checked else
                                         "выключен"))
            log.info("Автозапуск %s", "включён" if checked else "выключен")
        except OSError as exc:
            log.error("Автозапуск: %s", exc)
            QMessageBox.critical(None, APP_NAME, str(exc))

    def _toggle_dt_insert(self, checked: bool) -> None:
        """Переключает режим: вставка в окно или только буфер обмена."""
        self.config_store.data["datetime_insert"] = bool(checked)
        self.config_store.save()
        self.notify("Дата/время: " + ("вставка в активное окно"
                                      if checked else "только буфер обмена"))

    def notify(self, text: str,
               icon: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.Information,
               msecs: int = 3000) -> None:
        """Уведомления отключены: только запись в app.log, без всплывающих."""
        if icon == QSystemTrayIcon.Critical:
            log.error("%s", text)
        elif icon == QSystemTrayIcon.Warning:
            log.warning("%s", text)
        else:
            log.info("%s", text)

    # ---- показ и исполнение ----

    def toggle(self) -> None:
        if QApplication.activeModalWidget() is not None:
            return
        self.palette.toggle()

    def _on_execute(self, item: ResultItem, query: str,
                    keep_open: bool = False) -> None:
        if item.payload.get("type") == "history":
            self.palette.set_query(str(item.payload.get("query", "")))
            return
        if item.payload.get("type") == "history":
            self.palette.set_query(str(item.payload.get("query", "")))
            return
        if item.payload.get("type") == "text":
            self._record(query, item.key)
            self._run_text_op(str(item.payload.get("target", "")),
                              item.title)
            return
        self._record(query, item.key)
        p = item.payload

        if p.get("type") == "settings":
            self._apply_setting(str(p.get("target", "")),
                                str(p.get("args", "")))
            return

        # Ctrl+Enter / Shift+Enter / Ctrl+клик — не закрывать палитру.
        # Вставка даты/времени и буфера требует фокуса внешнего окна:
        # для этих типов палитра скрывается в любом случае.
        needs_focus = (
            p.get("type") == "clipboard"
            or (p.get("type") == "datetime"
                and self.config_store.data.get("datetime_insert", True)))
        if not self.palette.is_pinned() and (needs_focus or not keep_open):
            self.palette.hide()
        t = p.get("type")
        log.info("Запуск: %s (%s)", item.title, t)
        if t == "calc":
            QGuiApplication.clipboard().setText(p["result"])
            self.notify(f"Скопировано: {p['result']}")
            return
        if t == "datetime":
            try:
                text = datetime_value(str(p.get("target", "")))
            except Exception as exc:
                log.error("Ошибка формата даты/времени: %s", exc)
                self.notify(f"Ошибка формата: {exc}",
                            QSystemTrayIcon.Critical, 5000)
                return
            self._insert_datetime(text)
            return
        if t == "clipboard":
            self._insert_clipboard(str(p.get("text", "")))
            return
        if p.get("confirm"):
            btn = QMessageBox.question(
                None, APP_NAME, f"Выполнить «{item.title}»?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if btn != QMessageBox.Yes:
                return
        if t == "system" and p.get("target") in ("shutdown", "restart"):
            self.flush_state()
        QThreadPool.globalInstance().start(
            ExecTask(item, self._exec_signals))

    def show_all_commands(self) -> None:
        """Пункт трея: открыть палитру со списком всех команд."""
        if QApplication.activeModalWidget() is not None:
            return
        if not self.palette.isVisible():
            self.palette.open()
        self.palette.set_query("")
        self.palette.show_all()

    def _on_execute_batch(self, items: object, query: str,
                          keep_open: bool = False) -> None:
        """Пакетное выполнение отмеченных команд (последовательно)."""
        seq = [it for it in list(items)
               if isinstance(it, ResultItem) and it.enabled
               and it.payload.get("type") != "history"]
        if not seq:
            return
        if len(seq) == 1:
            self._on_execute(seq[0], query, keep_open)
            return
        dangerous = [it.title for it in seq if it.payload.get("confirm")]
        if dangerous:
            btn = QMessageBox.question(
                None, APP_NAME,
                f"Выполнить отмеченные команды ({len(seq)})?\n"
                "Среди них опасные:\n  • " + "\n  • ".join(dangerous),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if btn != QMessageBox.Yes:
                return
        prepared: list[ResultItem] = []
        for it in seq:
            cp = copy.copy(it)
            cp.payload = dict(it.payload)
            cp.payload["confirm"] = False  # подтверждение получено один раз
            prepared.append(cp)
        # Выключение/перезагрузка — всегда в конце очереди.
        prepared.sort(key=lambda it: 1 if (
            it.payload.get("type") == "system"
            and it.payload.get("target") in ("shutdown", "restart")) else 0)
        self._record(query, "")
        needs_focus = any(
            it.payload.get("type") in ("clipboard", "text")
            or (it.payload.get("type") == "datetime"
                and self.config_store.data.get("datetime_insert", True))
            for it in prepared)
        if not self.palette.is_pinned() and (needs_focus or not keep_open):
            self.palette.hide()
        self._batch_queue = prepared
        log.info("Пакетный запуск: %d команд", len(prepared))
        self._run_batch_step()

    def _run_batch_step(self) -> None:
        """Один шаг очереди пакетного запуска с паузой между командами."""
        queue = getattr(self, "_batch_queue", None) or []
        if not queue:
            self._batch_queue = []
            return
        it = queue.pop(0)
        t = str(it.payload.get("type", ""))
        try:
            self._on_execute(it, "", True)
        except Exception:
            log.exception("Ошибка пакетного запуска: %s", it.title)
        if t == "system" and it.payload.get("target") in ("shutdown", "restart"):
            self._batch_queue = []
            return
        if not queue:
            self._batch_queue = []
            return
        delay = 900 if t in ("text", "clipboard", "datetime") else 180
        QTimer.singleShot(delay, self._run_batch_step)

    # ---- операции над выделенным текстом: регистр и транслитерация ----
    def _text_delay(self) -> int:
        """Пауза между шагами (общая настройка с вставкой даты/времени)."""
        return int(self.config_store.data.get("datetime_paste_delay_ms", 120))

    def _run_text_op(self, op: str, title: str) -> None:
        """Преобразует выделение во внешнем окне; строка ввода не меняется."""
        op = str(op).strip().lower()
        if op not in TEXT_OPS:
            self.notify(f"Неизвестная операция над текстом: {op}",
                        QSystemTrayIcon.Warning, 6000)
            return
        # Патч: строка ввода не преобразуется — операция
        # применяется только к выделению во внешнем окне.
        if getattr(self, "_text_ctx", None):
            self.notify("Предыдущая операция с текстом ещё выполняется",
                        QSystemTrayIcon.Warning, 3000)
            return
        hwnd = int(getattr(self.palette, "prev_hwnd", 0) or 0)
        if not self.palette.is_pinned():
            self.palette.hide()
        if not (IS_WIN and hwnd and USER32.IsWindow(wintypes.HWND(hwnd))
                and hwnd != int(self.palette.internalWinId())):
            self.notify("Выделите текст в нужном окне и повторите команду",
                        QSystemTrayIcon.Warning, 6000)
            return
        self._text_ctx = {"op": op, "title": title, "hwnd": hwnd,
                          "old": QGuiApplication.clipboard().text()}
        self._clip_mute = True
        QGuiApplication.clipboard().setText(CP_TEXT_MARK)
        USER32.SetForegroundWindow(wintypes.HWND(hwnd))
        QTimer.singleShot(max(60, self._text_delay()), self._text_copy_step)

    def _text_copy_step(self) -> None:
        """Шаг 1: копируем выделение целевого окна (Ctrl+C)."""
        ctx = getattr(self, "_text_ctx", None)
        if not ctx:
            return
        if int(USER32.GetForegroundWindow() or 0) != int(ctx["hwnd"]):
            self._text_fail("Окно, где был выделен текст, недоступно")
            return
        if not send_ctrl_c():
            self._text_fail("Не удалось отправить Ctrl+C (SendInput)")
            return
        QTimer.singleShot(max(120, self._text_delay()), self._text_paste_step)

    def _text_paste_step(self) -> None:
        """Шаг 2: преобразуем полученный текст и кладём его в буфер."""
        ctx = getattr(self, "_text_ctx", None)
        if not ctx:
            return
        text = QGuiApplication.clipboard().text()
        if not text.strip() or text == CP_TEXT_MARK:
            self._text_fail("Выделенный текст не получен — выделите текст "
                            "и повторите (в терминалах копирование может "
                            "быть на Ctrl+Shift+C)")
            return
        if len(text) > TEXT_MAX_LEN:
            self._text_fail("Выделение слишком длинное "
                            f"(лимит {TEXT_MAX_LEN} символов)")
            return
        try:
            new = text_transform(str(ctx["op"]), text)
        except Exception as exc:
            log.exception("Ошибка преобразования текста")
            self._text_fail(str(exc))
            return
        self._clip_last = new
        QGuiApplication.clipboard().setText(new)
        self._clip_mute = False
        if len(new) <= 10000:
            self._clip_push(new)
        hwnd = int(ctx["hwnd"])
        if int(USER32.GetForegroundWindow() or 0) != hwnd:
            self._text_ctx = None
            self.notify("Скопировано (окно для вставки недоступно)")
            return
        QTimer.singleShot(60, lambda: self._text_send_paste(
            hwnd, new, str(ctx["title"])))

    def _text_send_paste(self, hwnd: int, text: str, title: str) -> None:
        """Шаг 3: вставляем результат обратно (Ctrl+V)."""
        self._text_ctx = None
        if int(USER32.GetForegroundWindow() or 0) != hwnd:
            self.notify("Готово — результат в буфере обмена")
            return
        release_held_modifiers()
        if send_ctrl_v():
            log.info("Текст преобразован: %s (%d симв.)", title, len(text))
        else:
            self.notify("Готово, но вставка недоступна — текст в буфере",
                        QSystemTrayIcon.Warning, 5000)

    def _text_fail(self, message: str) -> None:
        """Откат: возвращаем прежний буфер и сообщаем о неудаче."""
        ctx = getattr(self, "_text_ctx", None) or {}
        self._text_ctx = None
        old = str(ctx.get("old", ""))
        if QGuiApplication.clipboard().text() == CP_TEXT_MARK:
            self._clip_last = old
            QGuiApplication.clipboard().setText(old)
        self._clip_mute = False
        log.warning("Операция над текстом: %s", message)
        self.notify(message, QSystemTrayIcon.Warning, 6000)

    def _insert_datetime(self, text: str) -> None:
        """Копирует значение в буфер; при включённом режиме вставляет
        Ctrl+V в окно, которое было активным до открытия палитры."""
        QGuiApplication.clipboard().setText(text)
        hwnd = int(getattr(self.palette, "prev_hwnd", 0) or 0)
        insert = bool(self.config_store.data.get("datetime_insert", True))
        if not (IS_WIN and insert and hwnd
                and USER32.IsWindow(wintypes.HWND(hwnd))
                and hwnd != int(self.palette.internalWinId())):
            self.notify(f"Скопировано: {text}")
            return
        USER32.SetForegroundWindow(wintypes.HWND(hwnd))
        delay = int(self.config_store.data.get("datetime_paste_delay_ms",
                                               120))

        def paste() -> None:
            if int(USER32.GetForegroundWindow() or 0) != hwnd:
                self.notify(f"Скопировано: {text}")
                return
            if send_ctrl_v():
                self.notify(f"Вставлено: {text}")
            else:
                self.notify(f"Скопировано (вставка недоступна): {text}",
                            QSystemTrayIcon.Warning, 5000)

        QTimer.singleShot(delay, paste)

    @Slot(str)
    def _on_exec_finished(self, title: str) -> None:
        log.info("Выполнено: %s", title)

    @Slot(str, str)
    def _on_exec_failed(self, title: str, message: str) -> None:
        log.error("Ошибка запуска «%s»: %s", title, message)
        self.notify(f"Ошибка: {message}", QSystemTrayIcon.Critical, 5000)
        QMessageBox.critical(None, APP_NAME,
                             f"Не удалось выполнить «{title}»:\n{message}")

    # ---- история и статистика (отложенная атомарная запись) ----

    def _record(self, query: str, key: str) -> None:
        st = self.state_store.data
        if not isinstance(st, dict):
            st = self.state_store.data = copy.deepcopy(DEFAULT_STATE)
        usage = st.get("usage")
        if not isinstance(usage, dict):
            usage = st["usage"] = {}
        prev = usage.get(key)
        if isinstance(prev, dict):
            count = int(prev.get("count", 0))
        elif isinstance(prev, (int, float)):
            count = int(prev)
        else:
            count = 0
        if key:
            usage[key] = {"count": count + 1, "last": time.time()}
        q = query.strip()
        if q:
            hist = st.get("history")
            if not isinstance(hist, list):
                hist = st["history"] = []
            if q in hist:
                hist.remove(q)
            hist.insert(0, q)
            del hist[int(self.config_store.data["history_limit"]):]
        self._save_timer.start()

    def flush_state(self) -> None:
        """Сброс state.json на диск (по таймеру, перед выходом/выключением)."""
        self._save_timer.stop()
        usage = self.state_store.data.get("usage", {})
        if isinstance(usage, dict) and len(usage) > 500:
            def rank(kv: tuple) -> tuple:
                v = kv[1]
                if isinstance(v, dict):
                    return (float(v.get("count", 0)),
                            float(v.get("last", 0)))
                return (float(v) if isinstance(v, (int, float)) else 0.0,
                        0.0)
            top = sorted(usage.items(), key=rank, reverse=True)[:500]
            self.state_store.data["usage"] = dict(top)
        self.state_store.save()

    # ---- перезагрузка и завершение ----

    # ---- буфер обмена: история и вставка ----
    def _init_clipboard(self) -> None:
        st = self.state_store.data
        if not isinstance(st.get("clipboard"), list):
            st["clipboard"] = []
        self._clip_last = ""
        self._clip_mute = False
        self._text_ctx: dict[str, Any] | None = None
        QGuiApplication.clipboard().dataChanged.connect(
            self._on_clipboard_changed)
        self._on_clipboard_changed()  # захватить текущее содержимое

    def _on_clipboard_changed(self) -> None:
        if getattr(self, "_clip_mute", False):
            return
        if not self.config_store.data.get("clipboard_history", True):
            return
        text = QGuiApplication.clipboard().text()
        if not text or not text.strip() or len(text) > 10000:
            return
        if text == self._clip_last or text == CP_TEXT_MARK:
            return
        self._clip_last = text
        self._clip_push(text)


    def _clip_push(self, text: str) -> None:
        """Добавляет текст в историю буфера; закреплённые (#!) не вытесняются."""
        if not self.config_store.data.get("clipboard_history", True):
            return
        st = self.state_store.data
        clip = st.get("clipboard")
        if not isinstance(clip, list):
            clip = st["clipboard"] = []
        old = next((e for e in clip
                    if isinstance(e, dict) and e.get("text") == text), None)
        entry: dict[str, Any] = {"text": text, "ts": time.time()}
        if isinstance(old, dict):
            if old.get("pinned"):
                entry["pinned"] = True
            if old.get("tags"):
                entry["tags"] = list(old["tags"])
        clip[:] = [e for e in clip
                if not (isinstance(e, dict) and e.get("text") == text)]
        clip.insert(0, entry)
        limit = int(self.config_store.data.get("clipboard_limit", 30))
        pinned = [e for e in clip if isinstance(e, dict) and e.get("pinned")]
        regular = [e for e in clip
                if not (isinstance(e, dict) and e.get("pinned"))]
        clip[:] = pinned + regular[:limit]
        self._save_timer.start()

    def show_clipboard(self) -> None:
        if QApplication.activeModalWidget() is not None:
            return
        self.palette.show_clipboard()

    def _insert_clipboard(self, text: str) -> None:
        """Копирует выбранный текст и вставляет Ctrl+V в исходное окно."""
        self._clip_last = text
        QGuiApplication.clipboard().setText(text)
        self._clip_push(text)  # использованное — наверх списка
        hwnd = int(getattr(self.palette, "prev_hwnd", 0) or 0)
        if not (IS_WIN and hwnd
                and USER32.IsWindow(wintypes.HWND(hwnd))
                and hwnd != int(self.palette.internalWinId())):
            self.notify("Скопировано в буфер обмена")
            return
        USER32.SetForegroundWindow(wintypes.HWND(hwnd))
        delay = int(self.config_store.data.get("datetime_paste_delay_ms", 120))

        def paste() -> None:
            if int(USER32.GetForegroundWindow() or 0) != hwnd:
                self.notify("Скопировано (окно для вставки недоступно)")
                return
            if send_ctrl_v():
                log.info("Вставлено из буфера обмена")
            else:
                self.notify("Скопировано (вставка недоступна)",
                            QSystemTrayIcon.Warning, 5000)

        QTimer.singleShot(delay, paste)

    def _setup_command_hotkeys(self) -> None:
        """Глобальные хоткеи команд: поле hotkey в commands.json."""
        for hm in self._cmd_hotkeys:
            hm.stop()
        self._cmd_hotkeys = []
        used = set()
        main = set()
        for label in (self.config_store.data.get("hotkey", ""),
                      self.config_store.data.get("clipboard_hotkey", "")):
            try:
                if str(label).strip():
                    main.add(parse_hotkey(str(label)))
            except ValueError:
                pass
        n = 0
        for cmd in self.engine.user_commands():
            label = str(cmd.get("hotkey", "")).strip()
            if not label:
                continue
            name = cmd.get("name", "?")
            if n >= CMD_HOTKEY_MAX:
                log.warning("Хоткеев команд больше %d — остальные пропущены",
                            CMD_HOTKEY_MAX)
                break
            try:
                combo = parse_hotkey(label)
            except ValueError as exc:
                msg = f"Хоткей «{label}» команды «{name}» неверен: {exc}"
                log.error(msg)
                self.notify(msg, QSystemTrayIcon.Warning, 6000)
                continue
            if combo in main or combo in used:
                msg = (f"Хоткей «{label}» команды «{name}» уже занят — "
                       "пропущен")
                log.warning(msg)
                self.notify(msg, QSystemTrayIcon.Warning, 6000)
                continue
            used.add(combo)
            item = self.engine.item_for(cmd)
            hm = HotkeyManager(CMD_HOTKEY_BASE + n)
            hm.triggered.connect(
                lambda it=item: self._run_command_hotkey(it))
            hm.failed.connect(
                lambda m: self.notify(m, QSystemTrayIcon.Warning, 6000))
            hm.start(label)
            self._cmd_hotkeys.append(hm)
            n += 1
        if n:
            log.info("Хоткеев команд зарегистрировано: %d", n)


    def _run_command_hotkey(self, item: ResultItem) -> None:
        """v1.9: мгновенное исполнение по хоткею — палитра не показывается."""
        if QApplication.activeModalWidget() is not None:
            return
        was_visible = self.palette.isVisible()
        if not was_visible and IS_WIN:
            self.palette.prev_hwnd = int(USER32.GetForegroundWindow() or 0)
        try:
            self._on_execute(item, "", keep_open=True)
        finally:
            if not was_visible and self.palette.isVisible():
                self.palette.hide()

    @Slot(str, object)
    def _on_tag_command(self, text: str, items: object) -> None:
        """Команды ввода: #+метка, #-метка, #! / #pin / #unpin."""
        seq = [it for it in list(items) if isinstance(it, ResultItem)]
        if not seq:
            self.notify("Нет записей: отметьте строки (Пробел/Ctrl+Space) "
                        "или выделите одну", QSystemTrayIcon.Warning, 5000)
            return
        body = text.strip()
        low = body.lower()
        if body.startswith("#!") or low in ("#pin", "#unpin"):
            if not any(it.payload.get("type") == "clipboard" for it in seq):
                self.notify("#! — только для записей буфера обмена (#буфер)",
                            QSystemTrayIcon.Warning, 5000)
                return
            mode = ("unpin" if low == "#unpin"
                    else "pin" if low == "#pin" else "toggle")
            n = self._clip_pin_targets(seq, mode)
            self.notify(f"Постоянное хранение буфера: изменено записей — {n}")
            self.palette.after_tag_command()
            return
        add = body.startswith("#+")
        tags = [t.strip().lstrip("#") for t in re.split(r"[,;\s]+", body[2:])
                if t.strip()]
        tags = [t for t in tags if t]
        if not tags:
            self.notify("Формат: #+метка [, метка2] или #-метка",
                        QSystemTrayIcon.Warning, 5000)
            return
        overrides = self.state_store.data.setdefault("tag_overrides", {})
        n_cmd = n_clip = 0
        for it in seq:
            if it.payload.get("type") == "clipboard":
                if self._clip_tag_entry(str(it.payload.get("text", "")),
                                        tags, add):
                    n_clip += 1
                continue
            key = it.key
            if not key:
                continue
            cur = list(overrides.get(key) or self.engine.tags_for(key))
            if add:
                for t in tags:
                    if t not in cur:
                        cur.append(t)
            else:
                cur = [t for t in cur if t not in tags]
            overrides[key] = cur
            n_cmd += 1
        if n_cmd:
            self.engine.set_tag_overrides(overrides)
        self.state_store.save()
        verb = "Добавлены" if add else "Сняты"
        self.notify(f"{verb} метки: " + ", ".join("#" + t for t in tags)
                    + f" (команд: {n_cmd}, буфер: {n_clip})")
        self.palette.after_tag_command()

    def _clip_tag_entry(self, text: str, tags: list[str], add: bool) -> bool:
        """Метки записи буфера обмена (хранятся в state.json)."""
        clip = self.state_store.data.get("clipboard", [])
        if not isinstance(clip, list):
            return False
        for e in clip:
            if isinstance(e, dict) and e.get("text") == text:
                cur = [str(t) for t in e.get("tags", []) if str(t).strip()]
                if add:
                    for t in tags:
                        if t not in cur:
                            cur.append(t)
                else:
                    cur = [t for t in cur if t not in tags]
                e["tags"] = cur
                return True
        return False

    def _clip_pin_targets(self, seq: list[ResultItem], mode: str) -> int:
        """#! / #pin / #unpin — постоянное хранение записей буфера."""
        clip = self.state_store.data.get("clipboard", [])
        if not isinstance(clip, list):
            return 0
        n = 0
        for it in seq:
            if it.payload.get("type") != "clipboard":
                continue
            text = str(it.payload.get("text", ""))
            for e in clip:
                if isinstance(e, dict) and e.get("text") == text:
                    new = (not bool(e.get("pinned")) if mode == "toggle"
                        else mode == "pin")
                    if bool(e.get("pinned")) != new:
                        e["pinned"] = new
                        n += 1
                    break
        pinned = [e for e in clip if isinstance(e, dict) and e.get("pinned")]
        rest = [e for e in clip
                if not (isinstance(e, dict) and e.get("pinned"))]
        clip[:] = pinned + rest
        self.state_store.save()
        return n

    def _apply_setting(self, key: str, value: str) -> None:
        """Команды управления настройками (type=settings) из палитры."""
        cfg = self.config_store.data
        key, value = key.strip(), str(value).strip()
        if key == "reload":
            self.reload()
            self.palette.refresh_current()
            return
        if key == "autorun":
            try:
                on = not Autorun.is_enabled()
                Autorun.set(on)
                self.notify("Автозапуск " + ("включён" if on else "выключен"))
            except OSError as exc:
                self.notify(f"Автозапуск: {exc}",
                            QSystemTrayIcon.Critical, 5000)
            return
        if key == "clear_history":
            self.state_store.data["history"] = []
            self.state_store.save()
            self.notify("История ввода очищена")
            return
        if key == "clear_clipboard":
            clip = self.state_store.data.get("clipboard", [])
            self.state_store.data["clipboard"] = [
                e for e in clip if isinstance(e, dict) and e.get("pinned")]
            self._clip_last = ""
            self.state_store.save()
            self.notify("История буфера очищена (закреплённые сохранены)")
            self.palette.refresh_current()
            return
        if key == "theme":
            self._set_theme(value)
            self.palette.refresh_current()
            return
        if key == "sort_mode":
            if value in ("score", "name", "last", "manual"):
                self._set_sort_mode(value)
                self.palette.refresh_current()
            return
        if key == "window_opacity":
            try:
                self._set_opacity(int(value))
                self.palette.refresh_current()
            except ValueError:
                pass
            return
        if key == "pin_window" and value == "toggle":
            self.palette.toggle_pinned()
            self.palette.refresh_current()
            return
        if value == "toggle" and key in (
                "datetime_insert", "clipboard_history",
                "window_under_cursor", "show_all_on_empty",
                "phrases_enabled"):
            new = not bool(cfg.get(key, False))
            cfg[key] = new
            self.config_store.save()
            self.palette.apply_config(cfg)
            self.notify(f"{key}: " + ("вкл" if new else "выкл"))
            self.palette.refresh_current()
            return
        self.notify(f"Неизвестная настройка: {key}",
                    QSystemTrayIcon.Warning, 5000)

    def _set_theme(self, tid: str) -> None:
        """Смена темы оформления (меню трея или команда «Тема: …»)."""
        tid = str(tid).strip().lower()
        if tid not in THEMES:
            self.notify(f"Неизвестная тема: {tid}",
                        QSystemTrayIcon.Warning, 5000)
            return
        cfg = self.config_store.data
        cfg["theme"] = tid
        cfg.update(THEMES[tid])
        self.config_store.save()
        self.palette.apply_config(cfg)
        self.tray.setIcon(app_icon(cfg["color_accent"]))
        qss = MENU_QSS.substitute(
            panel=cfg["color_panel"], text=cfg["color_text"],
            border=cfg["color_border"], accent=cfg["color_accent"])
        self._menu.setStyleSheet(qss)
        for m in (getattr(self, "_opacity_menu", None),
                getattr(self, "_sort_menu", None),
                getattr(self, "_theme_menu", None)):
            if m is not None:
                m.setStyleSheet(qss)
        if tid in getattr(self, "_theme_acts", {}):
            self._theme_acts[tid].setChecked(True)
        self.notify("Тема оформления: " + THEME_NAMES.get(tid, tid))

    def reload(self) -> None:
        """Перечитывает config/commands, применяет тему и хоткей."""
        self.config_store.load()
        self.commands_store.load()
        self._clamp_config()
        n = self.engine.set_commands(
            self.commands_store.data.get("commands", []))
        self.engine.set_tag_overrides(
            self.state_store.data.get("tag_overrides", {}))
        cfg = self.config_store.data
        self.palette.apply_config(cfg)
        self.tray.setIcon(app_icon(cfg["color_accent"]))
        self.tray.setToolTip(f"{APP_NAME} — {cfg['hotkey'] or 'отключён'}")
        self.hotkey.start(cfg["hotkey"])
        self.clip_hotkey.start(cfg.get("clipboard_hotkey", ""))
        self._setup_command_hotkeys()
        problems = self.config_store.problems + self.commands_store.problems
        for pr in problems:
            self.notify(pr, QSystemTrayIcon.Warning, 6000)
        self.notify(f"Команды перезагружены: {n} пользовательских + "
                    f"{len(SYSTEM_ACTIONS)} системных")
        log.info("Перезагрузка: %d команд, хоткей %s", n, cfg["hotkey"])

    def _startup_warnings(self) -> None:
        problems = (self.config_store.problems
                    + self.commands_store.problems
                    + self.state_store.problems)
        if not self._writable:
            msg = ("Папка данных недоступна для записи:\n"
                   f"{DATA_DIR}\n\nНастройки, команды и история не "
                   "сохраняются, программа работает на текущих/дефолтных "
                   "значениях. Переместите папку в место с правом записи "
                   "(не Program Files).")
            log.warning(msg.replace("\n", " "))

            def show_ro() -> None:
                self.notify("Папка недоступна для записи — работа без "
                            "сохранения", QSystemTrayIcon.Warning, 8000)
                QMessageBox.warning(None, APP_NAME, msg)
            QTimer.singleShot(600, show_ro)
        for pr in problems:
            QTimer.singleShot(
                900, lambda p=pr: self.notify(p, QSystemTrayIcon.Warning,
                                              7000))

    def shutdown(self) -> None:
        """Корректное завершение: сброс состояния, снятие хоткея."""
        self.flush_state()
        self.hotkey.stop()
        self.clip_hotkey.stop()
        for hm in self._cmd_hotkeys:
            hm.stop()
        self.palette.hide()
        self.tray.hide()
        QThreadPool.globalInstance().waitForDone(2000)
        log.info("Выход")


    # ---- v1.7: миграция дата/времени в commands.json ----
    def _migrate_datetime_commands(self) -> None:
        """Один раз переносит стандартные команды даты/времени
        в commands.json; дальше пользователь управляет ими сам."""
        st = self.state_store.data
        if st.get("dt_commands_migrated"):
            return
        cmds = self.commands_store.data.get("commands")
        if not isinstance(cmds, list):
            cmds = self.commands_store.data["commands"] = []
        has_dt = any(isinstance(c, dict)
                     and str(c.get("type", "")).strip().lower() == "datetime"
                     for c in cmds)
        added = 0
        if not has_dt:
            for c in _datetime_commands():
                cmds.append({
                    "name": c["name"], "type": "datetime",
                    "target": c["target"], "args": "",
                    "synonyms": list(c["synonyms"]),
                    "description": c["description"], "icon": c["icon"],
                    "id": c["key"], "tags": list(c.get("tags", [])),
                })
                added += 1
            self.commands_store.save()
        st["dt_commands_migrated"] = True
        self.state_store.save()
        if added:
            log.info("Дата/время: %d команд добавлено в commands.json", added)

    # ---- v1.7: переключатели опций из трея ----
    def _toggle_under_cursor(self, checked: bool) -> None:
        self.config_store.data["window_under_cursor"] = bool(checked)
        self.config_store.save()
        self.palette.apply_config(self.config_store.data)
        self.notify("Окно под курсором: " + ("вкл" if checked else "выкл"))

    def _toggle_show_all_empty(self, checked: bool) -> None:
        self.config_store.data["show_all_on_empty"] = bool(checked)
        self.config_store.save()
        self.palette.apply_config(self.config_store.data)
        self.notify("Все команды при пустом вводе: "
                    + ("вкл" if checked else "выкл"))

    def _toggle_phrases(self, checked: bool) -> None:
        self.config_store.data["phrases_enabled"] = bool(checked)
        self.config_store.save()
        self.palette.apply_config(self.config_store.data)
        self.notify("Панель быстрых фраз: " + ("вкл" if checked else "выкл"))

    def _toggle_pin(self, checked: bool) -> None:
        """v1.8: закрепление окна из трея (сохранение через pinChanged)."""
        self.palette.set_pinned(bool(checked))

    @Slot(bool)
    def _on_pin_changed(self, on: bool) -> None:
        self.config_store.data["pin_window"] = bool(on)
        self.config_store.save()
        if hasattr(self, "_act_pin"):
            self._act_pin.setChecked(bool(on))
        self.notify("Окно закреплено: не скрывается при потере фокуса"
                    if on else "Окно откреплено")

    def _set_opacity(self, pct: int) -> None:
        """v1.8: прозрачность окна, 30-100%."""
        pct = max(30, min(100, int(pct)))
        self.config_store.data["window_opacity"] = pct
        self.config_store.save()
        self.palette.apply_config(self.config_store.data)
        self.notify(f"Прозрачность окна: {pct}%")


    def _set_sort_mode(self, mode: str) -> None:
        self.config_store.data["sort_mode"] = str(mode)
        self.config_store.save()
        self.palette.apply_config(self.config_store.data)
        self.palette.resort_current()
        self.notify("Сортировка списка: " + str(mode))

    @Slot(str)
    def _on_sort_mode(self, mode: str) -> None:
        self.config_store.data["sort_mode"] = str(mode)
        self.config_store.save()
        if mode in getattr(self, "_sort_acts", {}):
            self._sort_acts[mode].setChecked(True)

    @Slot(object)
    def _on_sort_order(self, order: object) -> None:
        self.state_store.data["sort_order"] = [str(k) for k in list(order)]
        self._save_timer.start()

    @Slot(int, int)
    def _on_user_resized(self, w: int, rows: int) -> None:
        self.config_store.data["window_width"] = int(w)
        self.config_store.data["max_visible_rows"] = int(rows)
        self.config_store.save()

    @Slot(str, str)
    def _on_console_output(self, cmd: str, out: str) -> None:
        log.info("Консоль: %s — вывод %d симв.", cmd, len(out))
        if out.strip():
            QGuiApplication.clipboard().setText(out)
            self.notify("Вывод консольной команды скопирован в буфер обмена")
        else:
            self.notify("Консольная команда выполнена (без вывода)")

# ============================================================================
# СЕКЦИЯ 10. main() / SINGLE INSTANCE (именованный мьютекс)
# ============================================================================

def main() -> int:
    """Точка входа: мьютекс, QApplication, контроллер, цикл событий."""
    if not IS_WIN:
        print("Приложение поддерживает только Windows 10/11.")
        return 1
    mutex = KERNEL32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        USER32.MessageBoxW(None, "Приложение уже запущено (иконка в трее).",
                           APP_NAME, 0x40)
        return 0
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qapp = QApplication(sys.argv)
    qapp.setApplicationName(APP_NAME)
    qapp.setApplicationVersion(APP_VERSION)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setStyle("Fusion")
    qapp.setFont(QFont("Segoe UI", 10))
    dir_ok = ensure_data_dir()
    moved = migrate_legacy_data()
    writable = dir_ok and probe_writable()
    setup_logging(writable)
    _install_excepthooks()
    log.info("%s %s | frozen=%s | BASE_DIR=%s | DATA_DIR=%s | writable=%s "
             "| python=%s", APP_NAME, APP_VERSION,
             getattr(sys, "frozen", False), BASE_DIR, DATA_DIR, writable,
             sys.version.split()[0])
    if moved:
        log.info("Данные перенесены в data: %s", ", ".join(moved))
    if not dir_ok:
        log.warning("Не удалось создать папку данных: %s", DATA_DIR)
    qapp.setWindowIcon(app_icon(DEFAULT_CONFIG["color_accent"]))
    app = App(qapp, writable)
    qapp.aboutToQuit.connect(app.shutdown)
    rc = qapp.exec()
    KERNEL32.CloseHandle(mutex)
    return rc


if __name__ == "__main__":
    sys.exit(main())
# palette-patch v1.2 (recent-by-ctrl+r, hints-on-demand)

# palette-patch v1.3 (datetime-insert)
# palette-patch v1.4 (quiet-tray, restore-menu, tags, command-hotkeys)
# palette-patch v1.5 (text-case, translit, tag-search-fix)

# palette-patch v1.6 (all-commands, multi-exec, silent)

# palette-patch v1.7 (clipboard-tag, mouse-move/resize, dt-to-json, console, cursor-window, empty-option, space-mark, sorting, phrases)
# palette-patch v1.8 (pin-window, window-opacity, instant-hotkey-exec, phrases-overlap-fix)
# palette-patch v1.9 (no-accent-frame, pinned-exec-no-hide, tag-commands, clip-pin-tags, settings-commands, sort-hotkeys, themes, instant-hotkey-fix)
