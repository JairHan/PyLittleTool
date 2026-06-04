from __future__ import annotations

import ctypes
import os
import queue
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Dict, Optional, Tuple

IS_WINDOWS = os.name == "nt"
APP_TITLE = "Fast File Manager"
MAX_WORKERS = max(4, min(16, (os.cpu_count() or 4) * 2))
CACHE_PATH = Path.home() / ".fast_macos_file_manager_cache.sqlite3"
DU_PATH = None if IS_WINDOWS else shutil.which("du")
DU_BATCH_SIZE = 96
SIZE_JOB_BATCH = max(8, min(64, MAX_WORKERS * 2))
CACHE_READ_BATCH = 450
CACHE_WRITE_BATCH = 128
CACHE_FLUSH_INTERVAL = 1.5
RESULT_DRAIN_LIMIT = 600
RESULT_DRAIN_SECONDS = 0.035
UI_REFRESH_INTERVAL_MS = 120
BUSY_REFRESH_INTERVAL_MS = 20
FAST_ROW_UPDATE_LIMIT = 250
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_WIN_API_LOCK = threading.Lock()
_WIN_FIND_FIRST = None
_WIN_FIND_NEXT = None
_WIN_FIND_CLOSE = None


class WIN32_FIND_DATAW(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("dwReserved0", wintypes.DWORD),
        ("dwReserved1", wintypes.DWORD),
        ("cFileName", wintypes.WCHAR * 260),
        ("cAlternateFileName", wintypes.WCHAR * 14),
        ("dwFileType", wintypes.DWORD),
        ("dwCreatorType", wintypes.DWORD),
        ("wFinderFlags", wintypes.WORD),
    ]


@dataclass
class FileEntry:
    path: Path
    name: str
    is_dir: bool
    size: Optional[int]
    modified: float
    kind: str
    error: Optional[str] = None


class SizeCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: Dict[str, Tuple[int, float]] = {}
        self._pending_rows: list[Tuple[str, float, int]] = []
        self._last_flush = time.monotonic()
        self._db = sqlite3.connect(CACHE_PATH, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA temp_store=MEMORY")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS sizes (path TEXT PRIMARY KEY, mtime REAL NOT NULL, size INTEGER NOT NULL)"
        )
        self._db.commit()

    def get(self, path: Path, mtime: float) -> Optional[int]:
        key = str(path)
        with self._lock:
            cached = self._items.get(key)
            if cached is None:
                row = self._db.execute(
                    "SELECT size, mtime FROM sizes WHERE path = ?", (key,)).fetchone()
                if row is not None:
                    cached = (int(row[0]), float(row[1]))
                    self._items[key] = cached
        if cached is None:
            return None
        size, cached_mtime = cached
        if cached_mtime == mtime:
            return size
        return None

    def get_many(self, items: list[Tuple[Path, float]]) -> Dict[str, int]:
        result: Dict[str, int] = {}
        missing: list[Tuple[str, float]] = []

        with self._lock:
            for path, mtime in items:
                key = str(path)
                cached = self._items.get(key)
                if cached is not None:
                    size, cached_mtime = cached
                    if cached_mtime == mtime:
                        result[key] = size
                    continue
                missing.append((key, mtime))

            for start in range(0, len(missing), CACHE_READ_BATCH):
                batch = missing[start: start + CACHE_READ_BATCH]
                keys = [key for key, _mtime in batch]
                if not keys:
                    continue
                placeholders = ",".join("?" for _ in keys)
                rows = self._db.execute(
                    f"SELECT path, size, mtime FROM sizes WHERE path IN ({placeholders})",
                    keys,
                ).fetchall()
                found = {str(row[0]): (int(row[1]), float(row[2]))
                         for row in rows}
                for key, mtime in batch:
                    cached = found.get(key)
                    if cached is None:
                        continue
                    self._items[key] = cached
                    size, cached_mtime = cached
                    if cached_mtime == mtime:
                        result[key] = size

        return result

    def _flush_locked(self) -> None:
        if not self._pending_rows:
            return
        rows = self._pending_rows
        self._pending_rows = []
        self._db.executemany(
            "INSERT OR REPLACE INTO sizes(path, mtime, size) VALUES (?, ?, ?)",
            rows,
        )
        self._db.commit()
        self._last_flush = time.monotonic()

    def _maybe_flush_locked(self) -> None:
        if len(self._pending_rows) >= CACHE_WRITE_BATCH or time.monotonic() - self._last_flush >= CACHE_FLUSH_INTERVAL:
            self._flush_locked()

    def set(self, path: Path, mtime: float, size: int) -> None:
        key = str(path)
        with self._lock:
            self._items[key] = (size, mtime)
            self._pending_rows.append((key, mtime, size))
            self._maybe_flush_locked()

    def set_many(self, items: list[Tuple[Path, float, int]]) -> None:
        if not items:
            return
        rows = [(str(path), mtime, size) for path, mtime, size in items]
        with self._lock:
            for path, mtime, size in items:
                self._items[str(path)] = (size, mtime)
            self._flush_locked()
            self._db.executemany(
                "INSERT OR REPLACE INTO sizes(path, mtime, size) VALUES (?, ?, ?)",
                rows,
            )
            self._db.commit()

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._pending_rows.clear()
            self._db.execute("DELETE FROM sizes")
            self._db.commit()
            self._db.execute("VACUUM")
            self._db.commit()
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.commit()
            self._last_flush = time.monotonic()

    def close(self) -> None:
        with self._lock:
            try:
                self._flush_locked()
            finally:
                self._db.close()


def format_size(size: Optional[int]) -> str:
    if size is None:
        return "计算中..."
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def format_time(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def size_engine_name() -> str:
    if IS_WINDOWS:
        return "Win32 API"
    if DU_PATH:
        return Path(DU_PATH).name
    return "Python"


def directory_size(path: Path, should_cancel: Callable[[], bool]) -> int:
    if IS_WINDOWS:
        return windows_directory_size(path, should_cancel)

    total = 0
    stack = [path]
    while stack:
        if should_cancel():
            raise RuntimeError("cancelled")
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for item in iterator:
                    if should_cancel():
                        raise RuntimeError("cancelled")
                    try:
                        if item.is_dir(follow_symlinks=False):
                            stack.append(Path(item.path))
                        else:
                            total += item.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def windows_directory_size(path: Path, should_cancel: Callable[[], bool]) -> int:
    find_first, find_next, find_close = windows_find_api()

    total = 0
    stack = [str(path)]
    while stack:
        if should_cancel():
            raise RuntimeError("cancelled")
        current = stack.pop()
        data = WIN32_FIND_DATAW()
        handle = find_first(os.path.join(current, "*"), ctypes.byref(data))
        if handle == INVALID_HANDLE_VALUE:
            continue
        try:
            while True:
                if should_cancel():
                    raise RuntimeError("cancelled")
                name = data.cFileName
                if name not in (".", ".."):
                    attrs = data.dwFileAttributes
                    child = os.path.join(current, name)
                    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                        pass
                    elif attrs & FILE_ATTRIBUTE_DIRECTORY:
                        stack.append(child)
                    else:
                        total += (int(data.nFileSizeHigh) << 32) + \
                            int(data.nFileSizeLow)
                if not find_next(handle, ctypes.byref(data)):
                    break
        finally:
            find_close(handle)
    return total


def windows_find_api():
    global _WIN_FIND_FIRST, _WIN_FIND_NEXT, _WIN_FIND_CLOSE
    if _WIN_FIND_FIRST is not None:
        return _WIN_FIND_FIRST, _WIN_FIND_NEXT, _WIN_FIND_CLOSE

    with _WIN_API_LOCK:
        if _WIN_FIND_FIRST is None:
            kernel32 = ctypes.windll.kernel32
            find_first = kernel32.FindFirstFileW
            find_first.argtypes = [wintypes.LPCWSTR,
                                   ctypes.POINTER(WIN32_FIND_DATAW)]
            find_first.restype = wintypes.HANDLE
            find_next = kernel32.FindNextFileW
            find_next.argtypes = [wintypes.HANDLE,
                                  ctypes.POINTER(WIN32_FIND_DATAW)]
            find_next.restype = wintypes.BOOL
            find_close = kernel32.FindClose
            find_close.argtypes = [wintypes.HANDLE]
            find_close.restype = wintypes.BOOL
            _WIN_FIND_FIRST = find_first
            _WIN_FIND_NEXT = find_next
            _WIN_FIND_CLOSE = find_close

    return _WIN_FIND_FIRST, _WIN_FIND_NEXT, _WIN_FIND_CLOSE


def du_directory_sizes(paths: list[Path], should_cancel: Callable[[], bool]) -> Dict[str, int]:
    if not paths:
        return {}
    if not DU_PATH:
        return {str(path): directory_size(path, should_cancel) for path in paths}

    command = [DU_PATH, "-sk"] + [str(path) for path in paths]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    while process.poll() is None:
        if should_cancel():
            process.terminate()
            try:
                process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                process.kill()
            raise RuntimeError("cancelled")
        time.sleep(0.03)

    stdout, stderr = process.communicate()
    sizes: Dict[str, int] = {}
    for line in stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            size_kb = int(parts[0])
        except ValueError:
            continue
        sizes[parts[1]] = size_kb * 1024

    missing = [path for path in paths if str(path) not in sizes]
    for path in missing:
        if should_cancel():
            raise RuntimeError("cancelled")
        sizes[str(path)] = directory_size(path, should_cancel)

    if process.returncode not in (0, None) and not sizes:
        raise OSError(stderr.strip() or f"{shlex.join(command)} failed")
    return sizes


def move_to_trash(path: Path) -> None:
    if IS_WINDOWS:
        move_to_windows_recycle_bin(path)
        return

    subprocess.run(
        [
            "osascript",
            "-e",
            f'tell application "Finder" to delete POSIX file "{escape_applescript(str(path))}"',
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def move_to_windows_recycle_bin(path: Path) -> None:
    shell32 = ctypes.windll.shell32
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3
    operation.pFrom = str(path) + "\0\0"
    operation.fFlags = 0x0040 | 0x0010 | 0x0004
    result = shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError(f"Windows 回收站操作失败，错误码：{result}")


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.USHORT),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def windows_drive_label(root: str) -> str:
    if not IS_WINDOWS:
        return ""

    volume_name = ctypes.create_unicode_buffer(261)
    file_system_name = ctypes.create_unicode_buffer(261)
    serial_number = wintypes.DWORD()
    max_component_length = wintypes.DWORD()
    file_system_flags = wintypes.DWORD()

    try:
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            root,
            volume_name,
            len(volume_name),
            ctypes.byref(serial_number),
            ctypes.byref(max_component_length),
            ctypes.byref(file_system_flags),
            file_system_name,
            len(file_system_name),
        )
    except OSError:
        return ""

    return volume_name.value.strip() if ok else ""


def format_drive_item(root: str) -> str:
    label = windows_drive_label(root)
    return f"{root} {label}" if label else root


def drive_root_from_item(value: str) -> str:
    value = value.strip()
    if len(value) >= 3 and value[1:3] == ":\\":
        return value[:3]
    return value


def windows_drives() -> list[str]:
    if not IS_WINDOWS:
        return []

    drives: list[str] = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for index, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        if bitmask & (1 << index):
            root = f"{letter}:\\"
            drives.append(format_drive_item(root))
    return drives


class FileManagerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1120x720")
        self.minsize(860, 520)

        self.cache = SizeCache()
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self.result_queue: queue.Queue = queue.Queue()
        self.entries: Dict[str, FileEntry] = {}
        self.current_path = Path.home()
        self.sort_column = "name"
        self.sort_desc = False
        self.scan_generation = 0

        self.path_var = tk.StringVar(value=str(self.current_path))
        self.drive_var = tk.StringVar(value="")
        self.drive_combo: Optional[ttk.Combobox] = None
        self.status_var = tk.StringVar(value="")
        self.search_var = tk.StringVar(value="")
        self.show_hidden_var = tk.BooleanVar(value=False)
        self.context_target: Optional[str] = None

        self._build_ui()
        self._bind_events()
        self.open_directory(self.current_path)
        self.after(80, self._drain_results)

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=(10, 8))
        toolbar.pack(fill=tk.X)

        ttk.Button(toolbar, text="上一级",
                   command=self.go_parent).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="刷新", command=self.refresh).pack(
            side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="清理计算缓存", command=self.clear_cache).pack(
            side=tk.LEFT, padx=(6, 0))
        file_browser_name = "资源管理器" if IS_WINDOWS else "Finder"
        ttk.Button(toolbar, text=file_browser_name, command=self.open_in_finder).pack(
            side=tk.LEFT, padx=(6, 10))

        if IS_WINDOWS:
            ttk.Label(toolbar, text="盘符").pack(side=tk.LEFT, padx=(0, 4))
            self.drive_combo = ttk.Combobox(
                toolbar,
                textvariable=self.drive_var,
                values=windows_drives(),
                width=18,
                state="readonly",
            )
            self.drive_combo.pack(side=tk.LEFT, padx=(0, 10))

        path_entry = ttk.Entry(toolbar, textvariable=self.path_var)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(toolbar, text="打开", command=self.open_from_entry).pack(
            side=tk.LEFT, padx=(6, 0))

        search = ttk.Entry(toolbar, textvariable=self.search_var, width=24)
        search.pack(side=tk.LEFT, padx=(12, 0))
        ttk.Checkbutton(
            toolbar,
            text="显示隐藏文件",
            variable=self.show_hidden_var,
            command=self.refresh,
        ).pack(side=tk.LEFT, padx=(10, 0))

        list_frame = ttk.Frame(self, padding=(10, 0, 10, 0))
        list_frame.pack(fill=tk.BOTH, expand=True)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        columns = ("name", "kind", "size", "modified", "path")
        self.tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("name", text="名称",
                          command=lambda: self.set_sort("name"))
        self.tree.heading("kind", text="类型",
                          command=lambda: self.set_sort("kind"))
        self.tree.heading("size", text="大小",
                          command=lambda: self.set_sort("size"))
        self.tree.heading("modified", text="修改时间",
                          command=lambda: self.set_sort("modified"))
        self.tree.heading("path", text="路径",
                          command=lambda: self.set_sort("path"))

        self.tree.column("name", width=300, anchor=tk.W)
        self.tree.column("kind", width=90, anchor=tk.W)
        self.tree.column("size", width=120, anchor=tk.E)
        self.tree.column("modified", width=170, anchor=tk.W)
        self.tree.column("path", width=420, anchor=tk.W)

        y_scroll = ttk.Scrollbar(
            list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        x_scroll = ttk.Scrollbar(
            list_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set,
                            xscrollcommand=x_scroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")

        status = ttk.Label(self, textvariable=self.status_var,
                           anchor=tk.W, padding=(10, 4))
        status.pack(fill=tk.X, side=tk.BOTTOM)

        self.context_menu = tk.Menu(self, tearoff=False)
        self.context_menu.add_command(
            label=f"在{file_browser_name}中打开", command=self.open_context_in_finder)
        self.context_menu.add_command(
            label="复制路径", command=self.copy_context_path)
        self.context_menu.add_separator()
        trash_label = "移到回收站" if IS_WINDOWS else "移到废纸篓"
        self.context_menu.add_command(
            label=trash_label, command=self.trash_context_path)

    def _bind_events(self) -> None:
        self.tree.bind("<Double-1>", self.open_selected)
        self.tree.bind("<Return>", self.open_selected)
        self.tree.bind("<Button-2>", self.show_context_menu)
        self.tree.bind("<Button-3>", self.show_context_menu)
        self.tree.bind("<Control-Button-1>", self.show_context_menu)
        if self.drive_combo is not None:
            self.drive_combo.bind("<<ComboboxSelected>>",
                                  self.open_selected_drive)
        self.search_var.trace_add("write", lambda *_: self.render_entries())

    def open_selected_drive(self, _event: tk.Event | None = None) -> None:
        drive = drive_root_from_item(self.drive_var.get())
        if drive:
            self.open_directory(Path(drive))

    def open_from_entry(self) -> None:
        self.open_directory(Path(self.path_var.get()).expanduser())

    def refresh(self) -> None:
        self.open_directory(self.current_path, force=True)

    def clear_cache(self) -> None:
        confirmed = messagebox.askyesno(
            "清理缓存",
            "确定要清理文件夹大小缓存吗？\n\n这不会删除任何真实文件，只会删除已保存的大小计算结果。",
            parent=self,
        )
        if not confirmed:
            return

        try:
            self.cache.clear()
        except sqlite3.Error as exc:
            messagebox.showerror("清理失败", str(exc), parent=self)
            return

        for entry in self.entries.values():
            if entry.is_dir:
                entry.size = None
                entry.error = None
        self.render_entries(preserve_selection=True)
        self.status_var.set("缓存已清理；点击刷新可重新计算当前目录大小")

    def go_parent(self) -> None:
        self.open_directory(self.current_path.parent)

    def open_selected(self, _event: tk.Event | None = None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        entry = self.entries.get(selected[0])
        if entry is None:
            return
        if entry.is_dir:
            self.open_directory(entry.path)
        else:
            self.reveal_path(entry.path)

    def open_in_finder(self) -> None:
        self.reveal_path(self.current_path)

    def open_context_in_finder(self) -> None:
        entry = self._context_entry()
        if entry is None:
            return
        self.reveal_path(entry.path)

    def copy_context_path(self) -> None:
        entry = self._context_entry()
        if entry is None:
            return
        self.clipboard_clear()
        self.clipboard_append(str(entry.path))
        self.status_var.set(f"已复制路径：{entry.path}")

    def trash_context_path(self) -> None:
        entry = self._context_entry()
        if entry is None:
            return
        trash_name = "回收站" if IS_WINDOWS else "废纸篓"
        confirmed = messagebox.askyesno(
            f"移到{trash_name}",
            f"确定要将“{entry.name}”移到{trash_name}吗？",
            parent=self,
        )
        if not confirmed:
            return

        try:
            move_to_trash(entry.path)
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() or str(exc)
            messagebox.showerror("删除失败", detail, parent=self)
            return
        except OSError as exc:
            messagebox.showerror("删除失败", str(exc), parent=self)
            return

        self.entries.pop(str(entry.path), None)
        if self.tree.exists(str(entry.path)):
            self.tree.delete(str(entry.path))
        self.status_var.set(f"已移到{trash_name}：{entry.path}")

    def show_context_menu(self, event: tk.Event) -> None:
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        self.context_target = row_id
        self.tree.selection_set(row_id)
        self.tree.focus(row_id)
        self.context_menu.tk_popup(event.x_root, event.y_root)

    def _context_entry(self) -> Optional[FileEntry]:
        if self.context_target and self.context_target in self.entries:
            return self.entries[self.context_target]
        selected = self.tree.selection()
        if selected:
            return self.entries.get(selected[0])
        return None

    def reveal_path(self, path: Path) -> None:
        try:
            if IS_WINDOWS:
                if path.is_dir():
                    subprocess.run(["explorer", str(path)], check=False)
                else:
                    subprocess.run(
                        ["explorer", "/select,", str(path)], check=False)
            elif path.is_dir():
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["open", "-R", str(path)], check=False)
        except OSError as exc:
            file_browser_name = "资源管理器" if IS_WINDOWS else "Finder"
            messagebox.showerror(f"无法打开{file_browser_name}", str(exc))

    def open_directory(self, path: Path, force: bool = False) -> None:
        resolved = path.expanduser()
        if not resolved.exists() or not resolved.is_dir():
            messagebox.showerror("无法打开目录", f"{resolved} 不是有效文件夹")
            self.path_var.set(str(self.current_path))
            return

        self.scan_generation += 1
        generation = self.scan_generation
        self.current_path = resolved
        self.path_var.set(str(resolved))
        self.update_drive_selection(resolved)
        self.entries.clear()
        self.tree.delete(*self.tree.get_children())
        self.status_var.set("正在读取目录...")

        try:
            with os.scandir(resolved) as iterator:
                raw_items = [item for item in iterator if self.show_hidden_var.get(
                ) or not item.name.startswith(".")]
        except OSError as exc:
            messagebox.showerror("读取失败", str(exc))
            return

        scanned: list[Tuple[object, Path, bool, os.stat_result]] = []
        cache_candidates: list[Tuple[Path, float]] = []

        for item in raw_items:
            try:
                is_dir = item.is_dir(follow_symlinks=False)
                stat = item.stat(follow_symlinks=False)
            except OSError:
                continue

            child_path = Path(item.path)
            scanned.append((item, child_path, is_dir, stat))
            if is_dir and not force:
                cache_candidates.append((child_path, stat.st_mtime))

        cached_sizes = {} if force else self.cache.get_many(cache_candidates)

        for item, child_path, is_dir, stat in scanned:
            cached_size = cached_sizes.get(str(child_path)) if is_dir else None
            size = cached_size if is_dir else stat.st_size
            kind = "文件夹" if is_dir else child_path.suffix.lower().lstrip(".") or "文件"
            entry = FileEntry(
                path=child_path,
                name=item.name,
                is_dir=is_dir,
                size=size,
                modified=stat.st_mtime,
                kind=kind,
            )
            self.entries[str(child_path)] = entry

        self.render_entries()
        pending = [entry for entry in self.entries.values(
        ) if entry.is_dir and entry.size is None]
        engine = size_engine_name()
        self.status_var.set(
            f"{len(self.entries)} 项，{len(pending)} 个文件夹正在用 {engine} 高速计算大小")

        if pending and not DU_PATH:
            pending_items = [(entry.path, entry.modified) for entry in pending]
            for start in range(0, len(pending_items), SIZE_JOB_BATCH):
                batch = pending_items[start: start + SIZE_JOB_BATCH]
                self.executor.submit(
                    self._scan_size_python_batch_job, generation, batch)
        elif pending:
            self.executor.submit(self._scan_size_batch_job, generation, [
                                 (entry.path, entry.modified) for entry in pending])

    def update_drive_selection(self, path: Path) -> None:
        if not IS_WINDOWS or self.drive_combo is None:
            return

        drives = windows_drives()
        self.drive_combo.configure(values=drives)
        drive = path.drive
        if drive:
            current_drive = f"{drive}\\"
            for item in drives:
                if drive_root_from_item(item).casefold() == current_drive.casefold():
                    self.drive_var.set(item)
                    break

    def _scan_size_job(self, generation: int, path: Path, mtime: float) -> None:
        def should_cancel() -> bool:
            return generation != self.scan_generation

        try:
            size = directory_size(path, should_cancel)
            self.cache.set(path, mtime, size)
            self.result_queue.put((generation, path, size))
        except RuntimeError:
            return
        except Exception as exc:
            self.result_queue.put((generation, path, str(exc)))

    def _scan_size_python_batch_job(self, generation: int, items: list[Tuple[Path, float]]) -> None:
        def should_cancel() -> bool:
            return generation != self.scan_generation

        cache_items: list[Tuple[Path, float, int]] = []
        for path, mtime in items:
            if should_cancel():
                return
            try:
                size = directory_size(path, should_cancel)
                cache_items.append((path, mtime, size))
                self.result_queue.put((generation, path, size))
            except RuntimeError:
                return
            except Exception as exc:
                self.result_queue.put((generation, path, str(exc)))

        if cache_items and not should_cancel():
            self.cache.set_many(cache_items)

    def _scan_size_batch_job(self, generation: int, items: list[Tuple[Path, float]]) -> None:
        def should_cancel() -> bool:
            return generation != self.scan_generation

        for start in range(0, len(items), DU_BATCH_SIZE):
            if should_cancel():
                return
            batch = items[start: start + DU_BATCH_SIZE]
            paths = [path for path, _mtime in batch]
            mtimes = {str(path): mtime for path, mtime in batch}
            try:
                sizes = du_directory_sizes(paths, should_cancel)
                cache_items = [(Path(path), mtimes[path], size)
                               for path, size in sizes.items() if path in mtimes]
                self.cache.set_many(cache_items)
                for path, size in sizes.items():
                    self.result_queue.put((generation, Path(path), size))
            except RuntimeError:
                return
            except Exception as exc:
                for path, _mtime in batch:
                    self.result_queue.put((generation, path, str(exc)))

    def _drain_results(self) -> None:
        changed = False
        changed_paths: list[str] = []
        processed = 0
        started = time.perf_counter()
        while True:
            if processed >= RESULT_DRAIN_LIMIT or time.perf_counter() - started >= RESULT_DRAIN_SECONDS:
                break
            try:
                generation, path, result = self.result_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if generation != self.scan_generation:
                continue
            entry = self.entries.get(str(path))
            if entry is None:
                continue
            if isinstance(result, int):
                entry.size = result
                entry.error = None
            else:
                entry.error = result
                entry.size = 0
            changed = True
            changed_paths.append(str(path))

        if changed:
            if self.sort_column == "size" or len(changed_paths) > FAST_ROW_UPDATE_LIMIT:
                self.render_entries(preserve_selection=True)
            else:
                for path in changed_paths:
                    entry = self.entries.get(path)
                    if entry is not None:
                        self.update_tree_row(entry)
        pending = sum(1 for entry in self.entries.values()
                      if entry.is_dir and entry.size is None)
        self.status_var.set(f"{len(self.entries)} 项，{pending} 个文件夹仍在计算")
        delay = BUSY_REFRESH_INTERVAL_MS if not self.result_queue.empty(
        ) else UI_REFRESH_INTERVAL_MS
        self.after(delay, self._drain_results)

    def update_tree_row(self, entry: FileEntry) -> None:
        row_id = str(entry.path)
        if not self.tree.exists(row_id):
            return
        size_text = "无权限" if entry.error else format_size(entry.size)
        self.tree.item(
            row_id,
            values=(entry.name, entry.kind, size_text,
                    format_time(entry.modified), str(entry.path)),
        )

    def render_entries(self, preserve_selection: bool = False) -> None:
        selected_path = self.tree.selection(
        )[0] if preserve_selection and self.tree.selection() else None
        query = self.search_var.get().casefold().strip()
        visible = [entry for entry in self.entries.values(
        ) if not query or query in entry.name.casefold()]

        visible.sort(key=self._sort_value, reverse=self.sort_desc)
        self.tree.delete(*self.tree.get_children())
        for entry in visible:
            size_text = "无权限" if entry.error else format_size(entry.size)
            self.tree.insert(
                "",
                tk.END,
                iid=str(entry.path),
                values=(entry.name, entry.kind, size_text,
                        format_time(entry.modified), str(entry.path)),
            )
        if selected_path and self.tree.exists(selected_path):
            self.tree.selection_set(selected_path)

    def _sort_value(self, entry: FileEntry) -> tuple:
        if self.sort_column == "size":
            size = -1 if entry.size is None else entry.size
            return size, entry.name.casefold()
        if self.sort_column == "modified":
            return entry.modified, entry.name.casefold()
        if self.sort_column == "kind":
            return entry.kind, entry.name.casefold()
        if self.sort_column == "path":
            return (str(entry.path).casefold(),)
        return (entry.name.casefold(),)

    def set_sort(self, column: str) -> None:
        if self.sort_column == column:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_column = column
            self.sort_desc = column in {"size", "modified"}
        self.render_entries(preserve_selection=True)

    def destroy(self) -> None:
        self.scan_generation += 1
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.cache.close()
        super().destroy()


if __name__ == "__main__":
    app = FileManagerApp()
    app.mainloop()
