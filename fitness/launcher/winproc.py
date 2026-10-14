"""Windows-специфика запуска процессов: без окон консоли, в Job Object (дочерние процессы не переживают launcher),
поиск уже запущенных процессов, переменные окружения пользователя из реестра. Только стандартная библиотека."""

import csv
import ctypes
import io
import os
import subprocess

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def hidden_popen_kwargs():
    """Аргументы Popen, чтобы дочерний процесс не открывал окно консоли."""
    if not IS_WINDOWS:
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": info}


def user_environment_value(name):
    """Значение переменной окружения пользователя из реестра (HKCU\Environment) или None.

    Нужна, когда launcher запущен из процесса, который появился раньше, чем переменную задали (её нет в os.environ).
    Значение не записывается ни в журналы, ни в файлы — только передаётся дочернему процессу.
    """
    if not IS_WINDOWS:
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    if kind == winreg.REG_EXPAND_SZ:
        value = os.path.expandvars(value)
    return value if isinstance(value, str) and value else None


def find_processes(image_name):
    """PID процессов с таким именем exe (без учёта регистра). Пустой список, если узнать не удалось."""
    if not IS_WINDOWS:
        return []
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=15, **hidden_popen_kwargs()).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    text = out.decode("utf-8", "replace") if b"\x00" not in out else out.decode("utf-16", "replace")
    pids = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[0].lower() == image_name.lower() and row[1].isdigit():
            pids.append(int(row[1]))
    return pids


def pid_alive(pid):
    if not pid:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        return code.value == 259                                   # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


class Job:
    """Job Object с KILL_ON_JOB_CLOSE: если launcher аварийно завершится, Windows закроет и его дочерние процессы,
    поэтому после повторного запуска не останется «осиротевшего» второго туннеля."""

    def __init__(self):
        self.handle = None
        if not IS_WINDOWS:
            return
        try:
            self._create()
        except OSError:
            self.handle = None

    def _create(self):
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = EXTENDED()
        info.Basic.LimitFlags = 0x2000                              # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(ctypes.get_last_error())
        self._kernel32 = kernel32
        self.handle = handle

    def assign(self, popen):
        """Добавить процесс в Job. False, если не получилось (процесс всё равно работает)."""
        if not self.handle:
            return False
        return bool(self._kernel32.AssignProcessToJobObject(self.handle, ctypes.c_void_p(int(popen._handle))))

    def close(self):
        """Закрыть Job: Windows завершает все оставшиеся в нём процессы."""
        if self.handle:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None
