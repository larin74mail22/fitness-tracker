"""Значок FITNESS Tracker в области уведомлений Windows (ctypes, без сторонних пакетов).

Меню: открыть панель, состояние, журнал, папка данных, импортировать сейчас, проверить MCP, перезапуск, выход.
Цвет значка: зелёный — всё работает, жёлтый — запускается или есть предупреждение, красный — ошибка.
Скрытое окно верхнего уровня получает WM_QUERYENDSESSION/WM_ENDSESSION: при выходе из Windows компоненты
останавливаются по порядку (импорт дописывает текущий файл, затем туннель).
"""

import ctypes
import os
import threading
from ctypes import wintypes
from pathlib import Path

from . import supervisor as sv

ICON_DIR = Path(__file__).resolve().parent / "icons"

WM_DESTROY, WM_CLOSE, WM_QUERYENDSESSION, WM_ENDSESSION, WM_COMMAND = 0x0002, 0x0010, 0x0011, 0x0016, 0x0111
WM_NULL, WM_APP, WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0000, 0x8000, 0x0203, 0x0205, 0x007B
WM_TRAY, WM_REFRESH, WM_BALLOON = WM_APP + 1, WM_APP + 2, WM_APP + 3
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, NIIF_WARNING = 0x1, 0x2
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
MB_OK, MB_ICONINFORMATION, MB_ICONERROR, MB_SETFOREGROUND, MB_TOPMOST = 0x0, 0x40, 0x10, 0x10000, 0x40000

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uTimeout", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT), ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", wintypes.POINT),
                ("lPrivate", wintypes.DWORD)]


def _api():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sig = {
        (user32, "DefWindowProcW"): ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], LRESULT),
        (user32, "RegisterClassExW"): ([ctypes.POINTER(WNDCLASSEXW)], wintypes.ATOM),
        (user32, "CreateWindowExW"): ([wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID], wintypes.HWND),
        (user32, "DestroyWindow"): ([wintypes.HWND], wintypes.BOOL),
        (user32, "PostMessageW"): ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        (user32, "GetMessageW"): ([ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
        (user32, "TranslateMessage"): ([ctypes.POINTER(MSG)], wintypes.BOOL),
        (user32, "DispatchMessageW"): ([ctypes.POINTER(MSG)], LRESULT),
        (user32, "PostQuitMessage"): ([ctypes.c_int], None),
        (user32, "CreatePopupMenu"): ([], wintypes.HMENU),
        (user32, "AppendMenuW"): ([wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR], wintypes.BOOL),
        (user32, "DestroyMenu"): ([wintypes.HMENU], wintypes.BOOL),
        (user32, "TrackPopupMenu"): ([wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wintypes.HWND, wintypes.LPVOID], wintypes.BOOL),
        (user32, "GetCursorPos"): ([ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
        (user32, "SetForegroundWindow"): ([wintypes.HWND], wintypes.BOOL),
        (user32, "LoadImageW"): ([wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  wintypes.UINT], wintypes.HANDLE),
        (user32, "LoadIconW"): ([wintypes.HINSTANCE, wintypes.LPVOID], wintypes.HICON),
        (user32, "RegisterWindowMessageW"): ([wintypes.LPCWSTR], wintypes.UINT),
        (user32, "MessageBoxW"): ([wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT], ctypes.c_int),
        (shell32, "Shell_NotifyIconW"): ([wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)], wintypes.BOOL),
        (kernel32, "GetModuleHandleW"): ([wintypes.LPCWSTR], wintypes.HMODULE),
    }
    for (dll, name), (argtypes, restype) in sig.items():
        func = getattr(dll, name)
        func.argtypes, func.restype = argtypes, restype
    return user32, shell32, kernel32


def message_box(text, title="FITNESS Tracker", error=False):
    """Окно с сообщением (в отдельном потоке не блокирует меню)."""
    user32 = _api()[0]
    user32.MessageBoxW(None, text, title,
                       MB_OK | MB_SETFOREGROUND | MB_TOPMOST | (MB_ICONERROR if error else MB_ICONINFORMATION))


MENU = [
    (1, "Открыть панель"), (2, "Состояние…"), (3, "Открыть журнал"), (4, "Открыть папку данных"), None,
    (5, "Импортировать сейчас"), (6, "Проверить MCP"), (7, "Перезапустить туннель"), (8, "Перезапустить всё"), None,
    (9, "Выход"),
]


class TrayApp:
    def __init__(self, supervisor):
        self.sup = supervisor
        self.user32, self.shell32, self.kernel32 = _api()
        self.hwnd = None
        self._icons = {}
        self._balloons = []
        self._balloon_lock = threading.Lock()
        self._wndproc = WNDPROC(self._proc)          # ссылка хранится, иначе сборщик мусора удалит обратный вызов
        self._taskbar_created = self.user32.RegisterWindowMessageW("TaskbarCreated")
        self._ended = False

    # значок
    def _icon(self, kind):
        if kind not in self._icons:
            path = ICON_DIR / f"tracker_{kind}.ico"
            handle = None
            if path.is_file():
                handle = self.user32.LoadImageW(None, str(path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            self._icons[kind] = handle or self.user32.LoadIconW(None, ctypes.c_void_p(32512))
        return self._icons[kind]

    def _data(self, flags):
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = flags
        return data

    def _tooltip(self):
        tunnel, importer = self.sup.tunnel.status(), self.sup.importer.status()
        check = self.sup.mcp_check
        mcp = "MCP: проверка…" if not check else ("MCP: отвечает" if check.get("ok") else "MCP: ОШИБКА")
        text = f"FITNESS Tracker\nТуннель: {tunnel['state_text']}\nИмпорт: {importer['state_text']}\n{mcp}"
        return text[:127]

    def _update_icon(self, action=NIM_MODIFY):
        data = self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self._icon(self.sup.overall())
        data.szTip = self._tooltip()
        if not self.shell32.Shell_NotifyIconW(action, ctypes.byref(data)) and action == NIM_ADD:
            self.sup.log.warning("Значок в трее не добавлен (Проводник ещё не запущен?); добавлю при его запуске")

    def refresh(self):
        """Можно вызывать из любого потока."""
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)

    def balloon(self, title, text):
        """Всплывающее уведомление; можно вызывать из любого потока."""
        with self._balloon_lock:
            self._balloons.append((title, text))
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, WM_BALLOON, 0, 0)

    def _show_balloons(self):
        with self._balloon_lock:
            pending, self._balloons = self._balloons, []
        for title, text in pending[-3:]:
            data = self._data(NIF_INFO)
            data.szInfoTitle = title[:63]
            data.szInfo = text[:255]
            data.dwInfoFlags = NIIF_WARNING if "ошибк" in (title + text).lower() else NIIF_INFO
            self.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    # меню и действия
    def _menu(self):
        menu = self.user32.CreatePopupMenu()
        for item in MENU:
            if item is None:
                self.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            else:
                self.user32.AppendMenuW(menu, MF_STRING, item[0], item[1])
        point = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        self.user32.SetForegroundWindow(self.hwnd)
        command = self.user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD, point.x, point.y,
                                             0, self.hwnd, None)
        self.user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        self.user32.DestroyMenu(menu)
        if command:
            self._run(command)

    def _background(self, func, *args):
        threading.Thread(target=self._safe, args=(func, *args), daemon=True).start()

    def _safe(self, func, *args):
        try:
            func(*args)
        except Exception as exc:                          # noqa: BLE001
            self.sup.log.exception("Ошибка действия меню")
            message_box(f"Ошибка: {exc}", error=True)

    def _show_status(self):
        message_box("\n".join(sv.describe_status(self.sup.status())), "FITNESS Tracker — состояние")

    def _open_dashboard(self):
        if not self.sup.dashboard.open():
            message_box(f"Панель не запустилась: {self.sup.dashboard.message}", error=True)

    def _check_mcp(self):
        result = self.sup.check_mcp()
        if result.get("ok"):
            message_box(f"server.py отвечает: версия {result['server_version']}, инструментов {result['tools']}, "
                        f"база {'в порядке' if result['database_ok'] else 'недоступна: ' + result.get('warning', '')}.",
                        "FITNESS Tracker — проверка MCP")
        else:
            message_box(f"server.py не отвечает: {result.get('error')}", "FITNESS Tracker — проверка MCP", error=True)

    def _run(self, command):
        paths = self.sup.paths
        actions = {
            1: lambda: self._background(self._open_dashboard),
            2: lambda: self._background(self._show_status),
            3: lambda: self._background(os.startfile, str(paths["tracker_log"]) if paths["tracker_log"].exists()
                                        else str(paths["logs"])),
            4: lambda: self._background(os.startfile, str(paths["data"])),
            5: lambda: self.sup.importer.run_now(),
            6: lambda: self._background(self._check_mcp),
            7: lambda: self._background(self.sup.restart, "tunnel"),
            8: lambda: self._background(self.sup.restart, "all"),
            9: lambda: self._background(self._exit),
        }
        actions.get(command, lambda: None)()

    def _exit(self):
        self.sup.stop("выход из меню")
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    # окно
    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                if lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._menu()
                elif lparam == WM_LBUTTONDBLCLK:
                    self._background(self._open_dashboard)
                return 0
            if msg == WM_REFRESH:
                self._update_icon()
                return 0
            if msg == WM_BALLOON:
                self._show_balloons()
                return 0
            if msg == self._taskbar_created and msg:
                self._update_icon(NIM_ADD)               # Проводник перезапустился — значок нужно добавить снова
                return 0
            if msg == WM_QUERYENDSESSION:
                return 1
            if msg == WM_ENDSESSION:
                if wparam:
                    self._ended = True
                    self.sup.stop("завершение работы Windows", fast=True)
                return 0
            if msg == WM_CLOSE:
                self.user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data(0)))
                self.user32.PostQuitMessage(0)
                return 0
        except Exception:                                 # noqa: BLE001 — исключение в WndProc уронило бы процесс
            self.sup.log.exception("Ошибка обработки сообщения окна")
            return 0
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        """Цикл сообщений. Возвращается после «Выход», команды --stop или завершения работы Windows."""
        hinst = self.kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = hinst
        cls.lpszClassName = "FitnessTrackerTray"
        self.user32.RegisterClassExW(ctypes.byref(cls))
        # Окно верхнего уровня (не message-only): только такие получают WM_QUERYENDSESSION. Оно не показывается.
        self.hwnd = self.user32.CreateWindowExW(0, cls.lpszClassName, "FITNESS Tracker", 0, 0, 0, 0, 0,
                                                None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._update_icon(NIM_ADD)
        self.sup.log.info("Значок в трее создан")
        self.sup.on_change = self.refresh
        self.sup.notify = self.balloon

        def close_when_stopped():
            self.sup.wait()
            self.sup._stopped.wait()
            if self.hwnd and not self._ended:
                self.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

        threading.Thread(target=close_when_stopped, name="tray-close", daemon=True).start()
        msg = MSG()
        while self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))
        self.hwnd = None
