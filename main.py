import curses
import curses.panel
import fcntl
import ipaddress
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from typing import TextIO

try:
    import pyte
except ImportError:  # pragma: no cover - optional dependency
    pyte = None

ARR_SIZE = 1024
DEBUG = True
LOG_FILE: TextIO | None = None
INPUT_POLL_INTERVAL_MS = 250
SYSTEM_BAR_GREEN_PAIR = 5
SYSTEM_BAR_YELLOW_PAIR = 6
SYSTEM_BAR_RED_PAIR = 7
LIGHT_THEME_INACTIVE_BORDER_PAIR = 8
LIGHT_THEME_ACTIVE_BORDER_PAIR = 9
SHIFT_ENTER_KEY = -2
COMMAND_S_KEY = -3
COMMAND_V_KEY = -4
COMMAND_C_KEY = -9
COMMAND_Z_KEY = -10
COMMAND_SHIFT_Z_KEY = -11
SHIFT_ARROW_UP = -5
SHIFT_ARROW_DOWN = -6
SHIFT_ARROW_LEFT = -7
SHIFT_ARROW_RIGHT = -8
ALT_TAB_KEY = -12
TOP_BAR_TEXT = "Files    Settings"

SHELL_COLOR_MAP = {
    "black": 0,
    "red": 1,
    "green": 2,
    "brown": 3,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
    "brightblack": 0,
    "brightred": 1,
    "brightgreen": 2,
    "brightbrown": 3,
    "brightyellow": 3,
    "brightblue": 4,
    "brightmagenta": 5,
    "brightcyan": 6,
    "brightwhite": 7,
}

SHELL_KEY_SEQUENCES = {
    curses.KEY_UP: b"\x1b[A",
    curses.KEY_DOWN: b"\x1b[B",
    curses.KEY_RIGHT: b"\x1b[C",
    curses.KEY_LEFT: b"\x1b[D",
    curses.KEY_HOME: b"\x1b[H",
    curses.KEY_END: b"\x1b[F",
    curses.KEY_PPAGE: b"\x1b[5~",
    curses.KEY_NPAGE: b"\x1b[6~",
    curses.KEY_IC: b"\x1b[2~",
    curses.KEY_DC: b"\x1b[3~",
    curses.KEY_BTAB: b"\x1b[Z",
    curses.KEY_F1: b"\x1bOP",
    curses.KEY_F2: b"\x1bOQ",
    curses.KEY_F3: b"\x1bOR",
    curses.KEY_F4: b"\x1bOS",
    curses.KEY_F5: b"\x1b[15~",
    curses.KEY_F6: b"\x1b[17~",
    curses.KEY_F7: b"\x1b[18~",
    curses.KEY_F8: b"\x1b[19~",
    curses.KEY_F9: b"\x1b[20~",
    curses.KEY_F10: b"\x1b[21~",
    curses.KEY_F11: b"\x1b[23~",
    curses.KEY_F12: b"\x1b[24~",
}


def log_info(value: object) -> None:
    if DEBUG and LOG_FILE is not None:
        LOG_FILE.write(f"LOG_INFO, {value}\n")


def color_num(fg: int, bg: int) -> int:
    return 0x80 | ((7 & bg) << 4) | (7 & fg)


def right_panel_geometry(columns: int, lines: int) -> list[tuple[int, int, int, int]]:
    x1 = int(columns * 0.75)
    y1 = max(7, int(lines * 0.125) - 1)
    panel_height = max(1, (lines - 3 - y1) // 3)
    # Bottom (Network info) panel gets one extra row to fit the traffic block
    return [
        (x1, y1 + panel_height * index, columns, y1 + panel_height * (index + 1) + (1 if index == 2 else 0))
        for index in range(3)
    ]


def top_tab_menu_at(x: int, y: int) -> int | None:
    if y != 0:
        return None

    tabs = ((1, "Files"), (2, "Settings"))
    starts = [1 + TOP_BAR_TEXT.index(label) for _, label in tabs]
    ends = starts[1:] + [1 + len(TOP_BAR_TEXT)]
    for (menu_number, _), start, end in zip(tabs, starts, ends):
        if start <= x < end:
            return menu_number
    return None


def bottom_bar_labels(width: int) -> list[tuple[int, str, str]]:
    labels = [
        (1, "F1", "new"),
        (10, "F2", "rename"),
        (23, "F3", "copy"),
        (34, "F4", "paste"),
        (46, "F5", "undo"),
        (58, "F6", "redo"),
        (70, "F7", "layout"),
        (84, "F8", "delete"),
        (96, "F10", "quit"),
    ]

    if width > 100:
        labels = [
            (1, "F1", "Создать"),
            (13, "F2", "Переим."),
            (27, "F3", "Копия"),
            (39, "F4", "Вставка"),
            (53, "F5", "Undo"),
            (65, "F6", "Redo"),
            (77, "F7", "Раскладка"),
            (93, "F8", "Удалить"),
            (106, "F10", "Выход"),
        ]

    return [label for label in labels if label[0] < width]


BOTTOM_BAR_KEY_CODES = {
    "F1": curses.KEY_F1,
    "F2": curses.KEY_F2,
    "F3": curses.KEY_F3,
    "F4": curses.KEY_F4,
    "F5": curses.KEY_F5,
    "F6": curses.KEY_F6,
    "F7": curses.KEY_F7,
    "F8": curses.KEY_F8,
    "F10": curses.KEY_F10,
}


def bottom_bar_at(x: int, y: int) -> int | None:
    if y != curses.LINES - 2:
        return None

    for column, key_label, description in bottom_bar_labels(curses.COLS):
        end = column + len(key_label) + 1 + len(f"-{description}")
        if column <= x < end:
            return BOTTOM_BAR_KEY_CODES.get(key_label)
    return None


def curses_color(fg: int) -> int:
    colors = (
        curses.COLOR_BLACK,
        curses.COLOR_BLUE,
        curses.COLOR_GREEN,
        curses.COLOR_CYAN,
        curses.COLOR_RED,
        curses.COLOR_MAGENTA,
        curses.COLOR_YELLOW,
        curses.COLOR_WHITE,
    )
    return colors[7 & fg]


def init_colorpairs() -> None:
    for bg in range(8):
        for fg in range(8):
            try:
                curses.init_pair(color_num(fg, bg), curses_color(fg), curses_color(bg))
            except curses.error:
                pass


def draw_window_border(window: curses.window, light_theme: bool = False) -> None:
    if not light_theme:
        window.box()
        return

    height, width = window.getmaxyx()
    if width < 2 or height < 2:
        return

    top = "┌" + "─" * (width - 2) + "┐"
    bottom = "└" + "─" * (width - 2) + "┘"
    try:
        window.addstr(0, 0, top)
    except curses.error:
        pass

    for row in range(1, height - 1):
        try:
            window.addstr(row, 0, "│")
        except curses.error:
            pass
        try:
            window.addstr(row, width - 1, "│")
        except curses.error:
            pass

    for column, character in enumerate(bottom):
        try:
            window.addstr(height - 1, column, character)
        except curses.error:
            try:
                window.insstr(height - 1, column, character)
            except curses.error:
                pass


def init_terminal() -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass

    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    curses.noecho()
    curses.cbreak()
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
    except curses.error:
        pass
    try:
        terminal_attributes = termios.tcgetattr(sys.stdin.fileno())
        terminal_attributes[0] &= ~termios.IXON
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, terminal_attributes)
    except (OSError, termios.error, ValueError):
        pass
    init_colorpairs()

    try:
        curses.init_color(curses.COLOR_BLACK, 140, 140, 140)
    except curses.error:
        pass

    for pair, foreground, background in (
        (1, curses.COLOR_RED, curses.COLOR_BLACK),
        (2, curses.COLOR_WHITE, curses.COLOR_RED),
        (3, curses.COLOR_BLACK, curses.COLOR_WHITE),
        (4, curses.COLOR_YELLOW, curses.COLOR_RED),
        (SYSTEM_BAR_GREEN_PAIR, curses.COLOR_GREEN, curses.COLOR_BLACK),
        (SYSTEM_BAR_YELLOW_PAIR, curses.COLOR_YELLOW, curses.COLOR_BLACK),
        (SYSTEM_BAR_RED_PAIR, curses.COLOR_RED, curses.COLOR_BLACK),
    ):
        try:
            curses.init_pair(pair, foreground, background)
        except curses.error:
            pass

    for pair, foreground in (
        (LIGHT_THEME_INACTIVE_BORDER_PAIR, curses.COLOR_WHITE),
        (LIGHT_THEME_ACTIVE_BORDER_PAIR, curses.COLOR_YELLOW),
    ):
        try:
            curses.init_pair(pair, foreground, -1)
        except curses.error:
            try:
                curses.init_pair(pair, foreground, curses.COLOR_BLACK)
            except curses.error:
                pass


class Window:
    def __init__(self, x1: int, y1: int, x2: int, y2: int, title: str) -> None:
        if x2 <= x1:
            raise ValueError(f"Invalid window width for {title}: x2 ({x2}) must be greater than x1 ({x1})")
        if y2 <= y1:
            raise ValueError(f"Invalid window height for {title}: y2 ({y2}) must be greater than y1 ({y1})")

        self.height = y2 - y1
        self.width = x2 - x1
        self.y1 = y1
        self.x1 = x1
        self.y2 = y2
        self.x2 = x2
        self.title = title
        self.light_theme = False

        self.window = curses.newwin(self.height, self.width, self.y1, self.x1)
        self.window.keypad(True)
        self.refresh_window()

    def draw_border(self, active: bool = False) -> None:
        if self.light_theme:
            border_pair = LIGHT_THEME_ACTIVE_BORDER_PAIR if active else LIGHT_THEME_INACTIVE_BORDER_PAIR
        else:
            border_pair = 4 if active else 3
        border_color = curses.color_pair(border_pair)
        self.window.attron(border_color)
        draw_window_border(self.window, self.light_theme)
        self.window.attroff(border_color)

    def refresh_window(self, active: bool = False) -> None:
        self.window.erase()
        self.draw_border(active)
        self.print_title(curses.color_pair(2 if active else 3))
        self.window.refresh()

    def resize(self, height: int, width: int) -> None:
        self.height = height
        self.width = width
        self.refresh_window()

    def set_geometry(self, x1: int, y1: int, x2: int, y2: int) -> None:
        if x2 <= x1 or y2 <= y1:
            return
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2
        self.height = y2 - y1
        self.width = x2 - x1
        self.window = curses.newwin(self.height, self.width, self.y1, self.x1)
        self.window.keypad(True)

    def print_title(self, color: int) -> None:
        title_size = (self.width - len(self.title)) // 2
        title_x = max(1, title_size)
        
        if self.width > 2:
            title_x = min(title_x, self.width - 2)
            title = self.title[: self.width - 2]

            try:
                self.window.attron(curses.A_BOLD)
                self.window.addstr(0, title_x, title, color)
                self.window.attroff(curses.A_BOLD)
            except curses.error:
                pass

    def get_window(self) -> curses.window:
        return self.window

    def get_x1(self) -> int:
        return self.x1

    def get_y1(self) -> int:
        return self.y1

    def get_x2(self) -> int:
        return self.x2

    def get_y2(self) -> int:
        return self.y2

    def get_height(self) -> int:
        return self.height

    def get_width(self) -> int:
        return self.width


class DateTimeWindow(Window):
    def __init__(self, x1: int, y1: int, x2: int, y2: int, title: str, _isDate: bool = False, window_manager: "WindowManager | None" = None) -> None:
        super().__init__(x1, y1, x2, y2, title)
        self.isDate = _isDate
        self.window_manager = window_manager
        self.render_time()

    def render_time(self, active: bool = False) -> None:
        if self.isDate:
            pattern = "%d.%m.%Y"
        else:
            if self.window_manager is not None and self.window_manager.clock_seconds_enabled:
                pattern = "%H:%M:%S"
            else:
                pattern = "%H:%M"

        current_time = datetime.now().strftime(pattern)
        self.window.erase()
        self.draw_border(active)
        self.print_title(curses.color_pair(2 if active else 3))

        try:
            text_x = max(1, (self.width - len(current_time)) // 2)
            self.window.addstr(2, text_x, current_time, curses.A_BOLD)
        except curses.error:
            pass

        self.window.refresh()


class SystemInfoWindow(Window):
    REFRESH_INTERVAL = 1.0

    def __init__(self, x1: int, y1: int, x2: int, y2: int, title: str) -> None:
        self.metrics: list[tuple[str, str, float | None]] = [
            ("CPU", "N/A", None),
            ("GPU", "N/A", None),
            ("RAM", "N/A", None),
            ("SSD", "N/A", None),
            ("SWAP", "N/A", None),
        ]
        self._metrics_lock = threading.Lock()
        self._metrics_stop = threading.Event()
        super().__init__(x1, y1, x2, y2, title)
        self._metrics_thread = threading.Thread(target=self._refresh_metrics, daemon=True)
        self._metrics_thread.start()

    def _refresh_metrics(self) -> None:
        while not self._metrics_stop.is_set():
            try:
                metrics = self.read_metrics()
            except Exception:
                metrics = [(name, "N/A", None) for name in ("CPU", "GPU", "RAM", "SSD", "SWAP")]
            with self._metrics_lock:
                self.metrics = metrics
            if self._metrics_stop.wait(self.REFRESH_INTERVAL):
                break

    @staticmethod
    def run_command(arguments: list[str]) -> str:
        try:
            result = subprocess.run(
                arguments,
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout

    @staticmethod
    def format_bytes(value: int) -> str:
        gibibyte = 1024 ** 3
        if value >= 1024 ** 4:
            return f"{value / (1024 ** 4):.1f}T"
        return f"{value / gibibyte:.1f}G"

    @staticmethod
    def format_capacity(current: int, total: int) -> str:
        unit = 1024 ** 4 if total >= 1024 ** 4 else 1024 ** 3
        suffix = "T" if unit == 1024 ** 4 else "G"
        return f"{current / unit:.1f}/{total / unit:.1f}{suffix}"

    def read_metrics(self) -> list[tuple[str, str, float | None]]:
        cpu_output = self.run_command(["top", "-l", "1", "-n", "0"])
        cpu_match = re.search(r"CPU usage: .*?, .*?, ([\d.]+)% idle", cpu_output)
        cpu_ratio = max(0.0, min(1.0, (100 - float(cpu_match.group(1))) / 100)) if cpu_match else None
        cpu_usage = f"{cpu_ratio * 100:.0f}%/100%" if cpu_ratio is not None else "N/A"

        gpu_output = self.run_command([
            "ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator", "-k", "PerformanceStatistics"
        ])
        gpu_match = re.search(r'"Device Utilization %"\s*=\s*([\d.]+)', gpu_output)
        gpu_ratio = max(0.0, min(1.0, float(gpu_match.group(1)) / 100)) if gpu_match else None
        gpu_usage = f"{gpu_ratio * 100:.0f}%/100%" if gpu_ratio is not None else "N/A"

        total_memory_output = self.run_command(["sysctl", "-n", "hw.memsize"])
        vm_output = self.run_command(["vm_stat"])
        memory_total_match = re.search(r"\d+", total_memory_output)
        page_size_match = re.search(r"page size of (\d+) bytes", vm_output)
        free_pages_match = re.search(r"Pages free:\s+(\d+)", vm_output)
        if memory_total_match and page_size_match and free_pages_match:
            memory_total = int(memory_total_match.group())
            memory_used = max(0, memory_total - int(page_size_match.group(1)) * int(free_pages_match.group(1)))
            memory = self.format_capacity(memory_used, memory_total)
            memory_ratio = memory_used / memory_total if memory_total else None
        else:
            memory = "N/A"
            memory_ratio = None

        swap_output = self.run_command(["sysctl", "vm.swapusage"])
        swap_match = re.search(r"total = ([\d.]+)([KMGTP])\s+used = ([\d.]+)([KMGTP])", swap_output)
        if swap_match:
            unit_bytes = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}
            swap_total = int(float(swap_match.group(1)) * unit_bytes[swap_match.group(2)])
            swap_used = int(float(swap_match.group(3)) * unit_bytes[swap_match.group(4)])
            swap = self.format_capacity(swap_used, swap_total)
            swap_ratio = swap_used / swap_total if swap_total else None
        else:
            swap = "N/A"
            swap_ratio = None

        try:
            disk = shutil.disk_usage("/")
            disk_used = disk.total - disk.free
            ssd = self.format_capacity(disk_used, disk.total)
            ssd_ratio = disk_used / disk.total if disk.total else None
        except OSError:
            ssd = "N/A"
            ssd_ratio = None

        return [
            ("CPU", cpu_usage, cpu_ratio),
            ("GPU", gpu_usage, gpu_ratio),
            ("RAM", memory, memory_ratio),
            ("SSD", ssd, ssd_ratio),
            ("SWAP", swap, swap_ratio),
        ]

    def refresh_window(self, active: bool = False) -> None:
        super().refresh_window(active)
        with self._metrics_lock:
            metrics = self.metrics.copy()
        content_width = max(0, self.width - 5)
        for row, (name, value, ratio) in enumerate(metrics, start=2):
            if row >= self.height - 1:
                break
            prefix = f"{name} {value}"
            bar_inner_width = max(0, content_width - len(prefix) - 3)
            if ratio is None or bar_inner_width < 3:
                metric = prefix
                try:
                    self.window.addstr(row, 3, metric[:content_width])
                except curses.error:
                    pass
            else:
                utilization = max(0.0, min(1.0, ratio)) * 100
                if utilization > 85:
                    color_pair = SYSTEM_BAR_RED_PAIR
                elif utilization < 50:
                    color_pair = SYSTEM_BAR_GREEN_PAIR
                else:
                    color_pair = SYSTEM_BAR_YELLOW_PAIR
                filled = round(utilization / 100 * bar_inner_width)
                empty = bar_inner_width - filled
                bar_start = 3 + len(prefix)
                try:
                    self.window.addstr(row, 3, prefix)
                    self.window.addstr(row, bar_start, " [")
                    if filled:
                        self.window.addstr(row, bar_start + 2, "#" * filled, curses.color_pair(color_pair))
                    if empty:
                        self.window.addstr(row, bar_start + 2 + filled, "." * empty)
                    self.window.addstr(row, bar_start + 2 + bar_inner_width, "]")
                except curses.error:
                    pass
        self.window.refresh()


class HardwareInfoWindow(Window):
    REFRESH_INTERVAL = 30.0

    def __init__(self, x1: int, y1: int, x2: int, y2: int, title: str) -> None:
        self.components = ["CPU N/A", "GPU N/A", "RAM N/A", "SSD N/A", "SWAP Virtual (macOS)"]
        self._components_lock = threading.Lock()
        self._components_stop = threading.Event()
        super().__init__(x1, y1, x2, y2, title)
        self._components_thread = threading.Thread(target=self._refresh_components, daemon=True)
        self._components_thread.start()

    def _refresh_components(self) -> None:
        while not self._components_stop.is_set():
            try:
                components = self.read_components()
            except Exception:
                components = ["CPU N/A", "GPU N/A", "RAM N/A", "SSD N/A", "SWAP Virtual (macOS)"]
            with self._components_lock:
                self.components = components
            if self._components_stop.wait(self.REFRESH_INTERVAL):
                break

    @staticmethod
    def run_command(arguments: list[str]) -> str:
        try:
            result = subprocess.run(
                arguments,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout

    def read_components(self) -> list[str]:
        cpu = self.run_command(["sysctl", "-n", "machdep.cpu.brand_string"]).strip() or "N/A"

        display_data = self.run_command(["system_profiler", "SPDisplaysDataType", "-detailLevel", "mini"])
        gpu_match = re.search(r"Graphics/Displays:\s*\n\s*([^:\n]+):", display_data)
        gpu = gpu_match.group(1).strip() if gpu_match else "N/A"

        memory_data = self.run_command(["system_profiler", "SPMemoryDataType", "-detailLevel", "mini"])
        memory_type_match = re.search(r"^[ \t]*Type:[ \t]*(.+)$", memory_data, re.MULTILINE)
        memory_vendor_match = re.search(r"^[ \t]*Manufacturer:[ \t]*(.+)$", memory_data, re.MULTILINE)
        memory_size_match = re.search(r"^[ \t]*Memory:[ \t]*(.+)$", memory_data, re.MULTILINE)
        memory_parts = [match.group(1).strip() for match in (memory_vendor_match, memory_type_match) if match]
        if memory_size_match:
            memory_parts.append(memory_size_match.group(1).strip())
        memory = " ".join(memory_parts) or "N/A"

        storage_data = self.run_command(["system_profiler", "SPNVMeDataType", "-detailLevel", "mini"])
        storage_match = re.search(r"^\s*Model:\s*(.+)$", storage_data, re.MULTILINE)
        storage = storage_match.group(1).strip() if storage_match else "N/A"

        return [
            f"CPU {cpu}",
            f"GPU {gpu}",
            f"RAM {memory}",
            f"SSD {storage}",
            "SWAP Virtual (macOS)",
        ]

    def refresh_window(self, active: bool = False) -> None:
        super().refresh_window(active)
        with self._components_lock:
            components = self.components.copy()
        for row, component in enumerate(components, start=2):
            if row >= self.height - 1:
                break
            try:
                self.window.addstr(row, 3, component[: max(0, self.width - 5)])
            except curses.error:
                pass
        self.window.refresh()


class NetworkInfoWindow(Window):
    REFRESH_INTERVAL = 15.0
    TRAFFIC_SAMPLE_INTERVAL = 1.0
    TRAFFIC_HISTORY_SIZE = 24

    def __init__(self, x1: int, y1: int, x2: int, y2: int, title: str) -> None:
        self.network_lines = ["Interfaces: N/A", "Local IP: N/A", "Public IP: N/A", "Type: N/A"]
        self._network_lock = threading.Lock()
        self._network_stop = threading.Event()
        self.traffic_samples: deque[tuple[float, float]] = deque(maxlen=self.TRAFFIC_HISTORY_SIZE)
        self._traffic_lock = threading.Lock()
        self._traffic_stop = threading.Event()
        super().__init__(x1, y1, x2, y2, title)
        self._network_thread = threading.Thread(target=self._refresh_network, daemon=True)
        self._network_thread.start()
        self._traffic_thread = threading.Thread(target=self._sample_traffic, daemon=True)
        self._traffic_thread.start()

    def _refresh_network(self) -> None:
        while not self._network_stop.is_set():
            try:
                lines = self.read_network_info()
            except Exception:
                lines = ["Interfaces: N/A", "Local IP: N/A", "Public IP: N/A", "Type: N/A"]
            with self._network_lock:
                self.network_lines = lines
            if self._network_stop.wait(self.REFRESH_INTERVAL):
                break

    @staticmethod
    def run_command(arguments: list[str]) -> str:
        try:
            result = subprocess.run(
                arguments,
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout

    @staticmethod
    def read_public_ip() -> str:
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=4) as response:
                address = response.read(64).decode("ascii").strip()
            return str(ipaddress.ip_address(address))
        except (OSError, UnicodeDecodeError, ValueError, urllib.error.URLError):
            return "N/A"

    def read_network_info(self) -> list[str]:
        ports_output = self.run_command(["networksetup", "-listallhardwareports"])
        port_names = {
            device: port.strip()
            for port, device in re.findall(
                r"Hardware Port:\s*([^\n]+)\nDevice:\s*([^\n]+)",
                ports_output,
            )
        }

        route_output = self.run_command(["route", "-n", "get", "default"])
        default_match = re.search(r"^\s*interface:\s*(\S+)", route_output, re.MULTILINE)
        default_interface = default_match.group(1) if default_match else ""

        interface_output = self.run_command(["ifconfig", "-a"])
        active_interfaces: list[tuple[str, str, list[str]]] = []
        for block in re.split(r"(?m)(?=^[\w.-]+: flags=)", interface_output):
            header = re.match(r"([\w.-]+): flags=.*", block)
            if not header:
                continue
            interface = header.group(1)
            if interface == "lo0" or not re.search(r"\bstatus: active\b", block) and interface != default_interface:
                continue

            addresses = [
                address
                for address in re.findall(r"^\s+inet\s+(\d+(?:\.\d+){3})\b", block, re.MULTILINE)
                if not address.startswith(("127.", "169.254."))
            ]
            if not addresses:
                continue
            port_name = port_names.get(interface, "")
            if "wi-fi" in port_name.lower() or "airport" in port_name.lower():
                connection = "Wi-Fi"
            elif "ethernet" in port_name.lower():
                connection = "Ethernet"
            elif "thunderbolt" in port_name.lower():
                connection = "Thunderbolt"
            else:
                connection = port_name or "Network"
            active_interfaces.append((interface, connection, addresses))

        active_interfaces.sort(key=lambda item: item[0] != default_interface)
        if active_interfaces:
            interface_names = [interface for interface, _, _ in active_interfaces[:2]]
            if len(active_interfaces) > 2:
                interface_names.append(f"+{len(active_interfaces) - 2}")
            interface_lines = ["Interfaces: " + ", ".join(interface_names)]
            primary_interface, connection_type, primary_addresses = active_interfaces[0]
            local_ip = primary_addresses[0]
        else:
            interface_lines = ["Interfaces: none"]
            local_ip = "N/A"
            connection_type = "Disconnected"

        public_ip = self.read_public_ip()
        return interface_lines + [
            f"Local IP: {local_ip}",
            f"Public IP: {public_ip}",
            f"Type: {connection_type}",
        ]

    def read_traffic_counters(self) -> tuple[str, int, int] | None:
        route_output = self.run_command(["route", "-n", "get", "default"])
        route_match = re.search(r"^\s*interface:\s*(\S+)", route_output, re.MULTILINE)
        if not route_match:
            return None
        interface = route_match.group(1)

        netstat_output = self.run_command(["netstat", "-ibn"])
        for line in netstat_output.splitlines():
            fields = line.split()
            if len(fields) < 11 or fields[0] != interface or not fields[2].startswith("<Link#"):
                continue
            try:
                return interface, int(fields[6]), int(fields[9])
            except ValueError:
                return None
        return None

    def _sample_traffic(self) -> None:
        previous = self.read_traffic_counters()
        previous_time = time.monotonic()
        while not self._traffic_stop.wait(self.TRAFFIC_SAMPLE_INTERVAL):
            current = self.read_traffic_counters()
            current_time = time.monotonic()
            download_rate = upload_rate = 0.0
            if previous is not None and current is not None and previous[0] == current[0]:
                elapsed = max(0.001, current_time - previous_time)
                download_rate = max(0, current[1] - previous[1]) / elapsed
                upload_rate = max(0, current[2] - previous[2]) / elapsed
            with self._traffic_lock:
                self.traffic_samples.append((download_rate, upload_rate))
            previous = current
            previous_time = current_time

    @staticmethod
    def format_rate(bytes_per_second: float) -> str:
        if bytes_per_second < 1024:
            return f"{bytes_per_second:.0f}B/s"
        if bytes_per_second < 1024 ** 2:
            return f"{bytes_per_second / 1024:.1f}K/s"
        return f"{bytes_per_second / (1024 ** 2):.1f}M/s"

    @staticmethod
    def sparkline(values: list[float], width: int) -> str:
        if width <= 0:
            return ""
        values = values[-width:]
        values = [0.0] * (width - len(values)) + values
        maximum = max(values, default=0.0)
        levels = ".:-=+*#%@"
        if maximum <= 0:
            return levels[0] * width
        return "".join(levels[min(len(levels) - 1, int(value / maximum * (len(levels) - 1)))] for value in values)

    def refresh_window(self, active: bool = False) -> None:
        super().refresh_window(active)
        with self._network_lock:
            lines = self.network_lines.copy()
        with self._traffic_lock:
            samples = list(self.traffic_samples)
        for row, line in enumerate(lines, start=2):
            if row >= self.height - 4:
                break
            try:
                self.window.addstr(row, 3, line[: max(0, self.width - 4)])
            except curses.error:
                pass

        graph_width = max(0, self.width - 20)
        download_values = [sample[0] for sample in samples]
        upload_values = [sample[1] for sample in samples]
        current_download = download_values[-1] if download_values else 0.0
        current_upload = upload_values[-1] if upload_values else 0.0
        traffic_rows = (
            (self.height - 5, ""),
            (self.height - 4, "Traffic (recent)"),
            (self.height - 3, f"Down {self.format_rate(current_download)} {self.sparkline(download_values, graph_width)}"),
            (self.height - 2, f"Up   {self.format_rate(current_upload)} {self.sparkline(upload_values, graph_width)}"),
            (self.height - 1, ""),
        )
        for row, line in traffic_rows:
            if row < 1:
                continue
            try:
                self.window.addstr(row, 3, line[: max(0, self.width - 4)])
            except curses.error:
                pass
        self.window.refresh()


class ShellSession:
    """Interactive shell attached to a pseudo-terminal, rendered through pyte."""

    def __init__(self, rows: int, cols: int) -> None:
        self.rows = max(1, rows)
        self.cols = max(1, cols)
        self.lock = threading.Lock()
        self.alive = False
        self.master_fd = -1
        self.process: subprocess.Popen[bytes] | None = None
        self.screen = pyte.Screen(self.cols, self.rows) if pyte is not None else None
        self.stream = pyte.ByteStream(self.screen) if self.screen is not None else None
        self.error = "" if pyte is not None else "pyte is not installed: pip install pyte"

        if pyte is None:
            return

        shell = os.environ.get("SHELL") or "/bin/bash"
        environment = os.environ.copy()
        environment["TERM"] = "xterm-256color"
        environment.setdefault("COLORTERM", "truecolor")

        def child_setup() -> None:
            os.setsid()
            try:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        try:
            self.master_fd, slave_fd = pty.openpty()
            self._set_winsize(slave_fd, self.rows, self.cols)
            self.process = subprocess.Popen(
                [shell, "-i"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=environment,
                preexec_fn=child_setup,
                close_fds=True,
            )
            os.close(slave_fd)
        except OSError as error:
            self.error = f"Unable to start shell: {error}"
            if self.master_fd >= 0:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
                self.master_fd = -1
            return

        self.alive = True
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def _reader(self) -> None:
        while self.alive:
            try:
                readable, _, _ = select.select([self.master_fd], [], [], 0.1)
            except (OSError, ValueError):
                break
            if not readable:
                if self.process is not None and self.process.poll() is not None:
                    break
                continue
            try:
                data = os.read(self.master_fd, 65536)
            except OSError:
                break
            if not data:
                break
            if self.stream is not None:
                with self.lock:
                    try:
                        self.stream.feed(data)
                    except Exception:
                        pass
        self.alive = False

    def write(self, data: bytes) -> None:
        if self.master_fd < 0:
            return
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def resize(self, rows: int, cols: int) -> None:
        rows = max(1, rows)
        cols = max(1, cols)
        if rows == self.rows and cols == self.cols:
            return
        self.rows, self.cols = rows, cols
        if self.screen is not None:
            with self.lock:
                try:
                    self.screen.resize(rows, cols)
                except Exception:
                    pass
        if self.master_fd >= 0:
            self._set_winsize(self.master_fd, rows, cols)
            if self.process is not None and self.process.poll() is None:
                try:
                    os.kill(self.process.pid, signal.SIGWINCH)
                except OSError:
                    pass

    def close(self) -> None:
        self.alive = False
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGHUP)
            except OSError:
                pass
        if self.master_fd >= 0:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = -1


class TerminalWindow(Window):
    STATUS_BUTTONS: tuple[tuple[str, int], ...] = (
        ("F2 save", curses.KEY_F2),
        ("F3 paste", curses.KEY_F3),
        ("F4 copy", curses.KEY_F4),
        ("F5 undo", curses.KEY_F5),
        ("F6 redo", curses.KEY_F6),
    )

    @classmethod
    def status_bar_text(cls, dirty: bool) -> str:
        return "  ".join(label for label, _ in cls.STATUS_BUTTONS) + (" *" if dirty else "")

    def status_button_at(self, mouse_x: int, mouse_y: int) -> int | None:
        if mouse_y != self.y1 + self.height - 1:
            return None
        if self.file_path is None or self.save_error:
            return None

        column = self.x1 + 2
        for label, key_code in self.STATUS_BUTTONS:
            if column <= mouse_x < column + len(label):
                return key_code
            column += len(label) + 2
        return None

    def __init__(self, x1: int, y1: int, x2: int, y2: int, title: str) -> None:
        super().__init__(x1, y1, x2, y2, title)
        self.content_lines: list[str] = [""]
        self.scroll_offset = 0
        self.cursor_row = 0
        self.cursor_column = 0
        self.selection_anchor: tuple[int, int] | None = None
        self.horizontal_offset = 0
        self.file_path: str | None = None
        self.dirty = False
        self.save_error = ""
        self.saved_content_lines = self.content_lines.copy()
        self.undo_history: list[tuple[list[str], int, int, tuple[int, int] | None]] = []
        self.redo_history: list[tuple[list[str], int, int, tuple[int, int] | None]] = []
        self.file_operation_active = False
        self.file_operation_type: str = ""
        self.file_operation_source: str = ""
        self.file_operation_source_is_dir = False
        self.file_operation_dest_dir: str = ""
        self.file_operation_entries: list[tuple[str, str]] = []
        self.file_operation_selected_index = 0
        self.file_operation_input = ""
        self.file_operation_input_cursor = 0
        self.file_operation_error = ""
        self.file_operation_input_focused = False
        self.mode = "shell"
        self.previous_mode = "shell"
        self.preview_mode = False
        self.shell = ShellSession(max(1, self.height - 2), max(1, self.width - 2))
        self.render()

    def set_content(self, text: str, file_path: str | None = None, preview: bool = False) -> None:
        self.mode = "editor"
        self.preview_mode = preview
        self.content_lines = text.split("\n") or [""]
        self.scroll_offset = 0
        self.cursor_row = 0
        self.cursor_column = 0
        self.selection_anchor = None
        self.horizontal_offset = 0
        self.file_path = file_path
        self.dirty = False
        self.save_error = ""
        self.saved_content_lines = self.content_lines.copy()
        self.undo_history.clear()
        self.redo_history.clear()
        self.render()

    def close_preview(self) -> None:
        self.preview_mode = False
        if self.mode == "editor":
            self.mode = "shell"
            self.render()

    def save_content(self) -> None:
        if self.file_path is None:
            self.save_error = "No file open"
            return

        try:
            with open(self.file_path, "w", encoding="utf-8", newline="") as file:
                file.write("\n".join(self.content_lines))
        except OSError:
            self.save_error = "Save failed"
            return

        self.dirty = False
        self.save_error = ""
        self.saved_content_lines = self.content_lines.copy()

    def capture_editor_state(self) -> tuple[list[str], int, int, tuple[int, int] | None]:
        return self.content_lines.copy(), self.cursor_row, self.cursor_column, self.selection_anchor

    def remember_undo_state(self) -> None:
        self.undo_history.append(self.capture_editor_state())
        if len(self.undo_history) > 100:
            del self.undo_history[0]
        self.redo_history.clear()

    def start_file_operation(self, operation_type: str, source_path: str) -> None:
        self.previous_mode = self.mode
        self.mode = "file_operation"
        self.file_operation_active = True
        self.file_operation_type = operation_type
        self.file_operation_source = source_path
        self.file_operation_source_is_dir = os.path.isdir(source_path) and not os.path.islink(source_path)
        self.file_operation_dest_dir = os.path.dirname(source_path)
        self.file_operation_selected_index = 0
        self.file_operation_input = os.path.basename(source_path)
        self.file_operation_input_cursor = len(self.file_operation_input)
        self.file_operation_error = ""
        self.file_operation_input_focused = False
        self.refresh_file_operation_entries()

    def refresh_file_operation_entries(self) -> None:
        self.file_operation_entries = []
        parent = os.path.dirname(self.file_operation_dest_dir)
        if parent and self.file_operation_dest_dir != parent:
            self.file_operation_entries.append(("..", "dir"))
        try:
            items = sorted(
                os.scandir(self.file_operation_dest_dir),
                key=lambda entry: (not entry.is_dir(), entry.name.lower()),
            )
        except OSError:
            self.file_operation_dest_dir = os.getcwd()
            self.file_operation_entries = [("..", "dir")]
            items = sorted(
                os.scandir(self.file_operation_dest_dir),
                key=lambda entry: (not entry.is_dir(), entry.name.lower()),
            )
        for entry in items:
            kind = "dir" if entry.is_dir() else "file"
            self.file_operation_entries.append((entry.name, kind))
        if self.file_operation_selected_index >= len(self.file_operation_entries):
            self.file_operation_selected_index = max(0, len(self.file_operation_entries) - 1)

    def finish_file_operation(self) -> None:
        self.file_operation_active = False
        self.file_operation_type = ""
        self.file_operation_source = ""
        self.file_operation_source_is_dir = False
        self.file_operation_dest_dir = ""
        self.file_operation_entries = []
        self.file_operation_selected_index = 0
        self.file_operation_input = ""
        self.file_operation_input_cursor = 0
        self.file_operation_error = ""
        self.mode = self.previous_mode if self.previous_mode else "shell"
        self.previous_mode = self.mode

    def execute_file_operation(self, destination_path: str) -> None:
        source_path = os.path.abspath(self.file_operation_source)
        destination_path = os.path.abspath(destination_path)
        destination_name = os.path.basename(destination_path)
        if destination_name in {"", ".", ".."}:
            self.file_operation_error = "Invalid destination file name"
            return
        if not os.path.isdir(os.path.dirname(destination_path)):
            self.file_operation_error = "Destination folder does not exist"
            return
        if destination_path == source_path:
            if self.file_operation_type == "copy":
                self.file_operation_error = "Source and destination are identical"
                return
            self.finish_file_operation()
            return
        if self.file_operation_source_is_dir and not os.path.islink(source_path):
            if os.path.commonpath((source_path, destination_path)) == source_path:
                action_verb = "copy" if self.file_operation_type == "copy" else "move"
                self.file_operation_error = f"Cannot {action_verb} folder into itself"
                return
        if os.path.lexists(destination_path):
            self.file_operation_error = "Destination already exists"
            return
        try:
            if self.file_operation_type == "copy":
                if self.file_operation_source_is_dir:
                    if os.path.islink(source_path):
                        os.symlink(os.readlink(source_path), destination_path)
                    else:
                        shutil.copytree(source_path, destination_path)
                else:
                    shutil.copy2(source_path, destination_path)
            else:
                shutil.move(source_path, destination_path)
        except (OSError, shutil.Error):
            if self.file_operation_type == "copy":
                self.file_operation_error = "Failed to copy folder" if self.file_operation_source_is_dir else "Failed to copy file"
            else:
                self.file_operation_error = "Failed to move or rename folder" if self.file_operation_source_is_dir else "Failed to move or rename file"
            return
        self.finish_file_operation()

    def render_file_operation(self) -> None:
        if not self.file_operation_active:
            return
        self.window.erase()
        self.draw_border(True)
        title_text = "Copy file" if self.file_operation_type == "copy" else "Move / rename file"
        if self.file_operation_source_is_dir:
            title_text = "Copy folder" if self.file_operation_type == "copy" else "Move / rename folder"
        try:
            title_x = max(1, (self.width - len(title_text)) // 2)
            self.window.attron(curses.color_pair(2) | curses.A_BOLD)
            self.window.addstr(0, title_x, title_text[: max(0, self.width - 2)])
            self.window.attroff(curses.color_pair(2) | curses.A_BOLD)
        except curses.error:
            pass

        try:
            display_path = self.file_operation_dest_dir
            if len(display_path) > self.width - 4:
                display_path = "..." + display_path[-(self.width - 7):]
            self.window.addstr(1, 2, display_path[: self.width - 4], curses.A_BOLD)
        except curses.error:
            pass

        max_rows = max(1, self.height - 5)
        for offset in range(max_rows):
            row_idx = offset + 2
            entry_index = offset
            if entry_index >= len(self.file_operation_entries):
                break
            name, kind = self.file_operation_entries[entry_index]
            display_name = name
            if len(display_name) > self.width - 8:
                display_name = display_name[: self.width - 9] + "..."
            prefix = ">" if entry_index == self.file_operation_selected_index else " "
            label = f"{prefix} {display_name}"
            if kind == "dir":
                label += "/"
            style = curses.A_REVERSE if entry_index == self.file_operation_selected_index else curses.A_NORMAL
            try:
                self.window.addstr(row_idx, 2, label[: self.width - 4], style)
            except curses.error:
                pass

        input_row = self.height - 2
        try:
            if self.file_operation_input_focused:
                self.window.addstr(input_row, 2, "Path: ", curses.A_BOLD)
            else:
                self.window.addstr(input_row, 2, "Path: ", curses.color_pair(3))
            field_width = max(1, self.width - 10)
            field_start = max(0, self.file_operation_input_cursor - field_width + 1)
            visible_input = self.file_operation_input[field_start : field_start + field_width]
            if self.file_operation_input_focused:
                self.window.addstr(input_row, 8, visible_input, curses.A_BOLD)
            else:
                self.window.addstr(input_row, 8, visible_input)
            cursor_offset = min(self.file_operation_input_cursor - field_start, field_width - 1)
            cursor_character = visible_input[cursor_offset : cursor_offset + 1] or " "
            self.window.addstr(input_row, 8 + cursor_offset, cursor_character, curses.A_REVERSE)
        except curses.error:
            pass

        if self.file_operation_error:
            try:
                self.window.addstr(self.height - 1, 2, self.file_operation_error[: max(0, self.width - 4)], curses.A_BOLD)
            except curses.error:
                pass

        self.window.refresh()

    def handle_file_operation_key(self, key: int | str) -> None:
        if key == 27:
            self.finish_file_operation()
            return

        # Tab переключает фокус между навигацией и вводом
        if key == 9:
            self.file_operation_input_focused = not self.file_operation_input_focused
            return

        # Если фокус на вводе — обрабатываем ввод пути
        if self.file_operation_input_focused:
            if isinstance(key, str):
                if key.isprintable():
                    self.file_operation_input = (
                        self.file_operation_input[: self.file_operation_input_cursor]
                        + key
                        + self.file_operation_input[self.file_operation_input_cursor :]
                    )
                    self.file_operation_input_cursor += 1
                    self.file_operation_error = ""
                return

            if key in (10, 13, curses.KEY_ENTER):
                dest_input = os.path.expanduser(self.file_operation_input.strip())
                if not dest_input:
                    self.file_operation_error = "Destination path cannot be empty"
                    return
                if not os.path.isabs(dest_input):
                    dest_input = os.path.join(self.file_operation_dest_dir, dest_input)
                self.execute_file_operation(os.path.abspath(dest_input))
                return

            if key in (8, 127, curses.KEY_BACKSPACE):
                if self.file_operation_input_cursor > 0:
                    self.file_operation_input = (
                        self.file_operation_input[: self.file_operation_input_cursor - 1]
                        + self.file_operation_input[self.file_operation_input_cursor :]
                    )
                    self.file_operation_input_cursor -= 1
                self.file_operation_error = ""
                return

            if key == curses.KEY_DC:
                if self.file_operation_input_cursor < len(self.file_operation_input):
                    self.file_operation_input = (
                        self.file_operation_input[: self.file_operation_input_cursor]
                        + self.file_operation_input[self.file_operation_input_cursor + 1 :]
                    )
                self.file_operation_error = ""
                return

            if key in (curses.KEY_LEFT, 260):
                self.file_operation_input_cursor = max(0, self.file_operation_input_cursor - 1)
                return

            if key in (curses.KEY_RIGHT, 261):
                self.file_operation_input_cursor = min(len(self.file_operation_input), self.file_operation_input_cursor + 1)
                return

            if key in (curses.KEY_HOME,):
                self.file_operation_input_cursor = 0
                return

            if key in (curses.KEY_END,):
                self.file_operation_input_cursor = len(self.file_operation_input)
                return

            return

        # Если фокус на навигации — обрабатываем навигацию
        if isinstance(key, str):
            return

        if key in (curses.KEY_UP, 259):
            self.file_operation_selected_index = max(0, self.file_operation_selected_index - 1)
            return

        if key in (curses.KEY_DOWN, 258):
            self.file_operation_selected_index = min(len(self.file_operation_entries) - 1, self.file_operation_selected_index + 1)
            return

        if key == curses.KEY_PPAGE:
            self.file_operation_selected_index = 0
            return

        if key == curses.KEY_NPAGE:
            self.file_operation_selected_index = max(0, len(self.file_operation_entries) - 1)
            return

        if key in (10, 13, curses.KEY_ENTER):
            if not self.file_operation_entries:
                return
            name, kind = self.file_operation_entries[self.file_operation_selected_index]
            if kind == "dir":
                if name == "..":
                    parent = os.path.dirname(self.file_operation_dest_dir)
                    if parent and self.file_operation_dest_dir != parent:
                        self.file_operation_dest_dir = parent
                else:
                    self.file_operation_dest_dir = os.path.join(self.file_operation_dest_dir, name)
                self.file_operation_selected_index = 0
                self.refresh_file_operation_entries()
            return

        if key == curses.KEY_PPAGE:
            self.file_operation_selected_index = 0
            return

        if key == curses.KEY_NPAGE:
            self.file_operation_selected_index = max(0, len(self.file_operation_entries) - 1)
            return

        if key in (curses.KEY_UP, 259):
            self.file_operation_selected_index = max(0, self.file_operation_selected_index - 1)
            return

        if key in (curses.KEY_DOWN, 258):
            self.file_operation_selected_index = min(len(self.file_operation_entries) - 1, self.file_operation_selected_index + 1)
            return

    def restore_editor_state(self, state: tuple[list[str], int, int, tuple[int, int] | None]) -> None:
        lines, self.cursor_row, self.cursor_column, self.selection_anchor = state
        self.content_lines = lines.copy()
        self.dirty = self.content_lines != self.saved_content_lines
        self.save_error = ""
        self.render()

    def undo(self) -> None:
        if not self.undo_history:
            return
        self.redo_history.append(self.capture_editor_state())
        if len(self.redo_history) > 100:
            del self.redo_history[0]
        self.restore_editor_state(self.undo_history.pop())

    def redo(self) -> None:
        if not self.redo_history:
            return
        self.undo_history.append(self.capture_editor_state())
        if len(self.undo_history) > 100:
            del self.undo_history[0]
        self.restore_editor_state(self.redo_history.pop())

    def insert_text(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return

        line = self.content_lines[self.cursor_row]
        prefix = line[: self.cursor_column]
        suffix = line[self.cursor_column :]
        inserted_lines = text.split("\n")
        self.remember_undo_state()
        if len(inserted_lines) == 1:
            self.content_lines[self.cursor_row] = prefix + inserted_lines[0] + suffix
            self.cursor_column += len(inserted_lines[0])
        else:
            self.content_lines[self.cursor_row : self.cursor_row + 1] = (
                [prefix + inserted_lines[0]]
                + inserted_lines[1:-1]
                + [inserted_lines[-1] + suffix]
            )
            self.cursor_row += len(inserted_lines) - 1
            self.cursor_column = len(inserted_lines[-1])

        self.dirty = True
        self.save_error = ""
        self.selection_anchor = None
        self.render()

    def delete_selection(self) -> bool:
        if self.selection_anchor is None:
            return False

        selection_start = min(self.selection_anchor, (self.cursor_row, self.cursor_column))
        selection_end = max(self.selection_anchor, (self.cursor_row, self.cursor_column))
        self.selection_anchor = None
        if selection_start == selection_end:
            return False

        start_row, start_column = selection_start
        end_row, end_column = selection_end
        self.remember_undo_state()
        merged_line = self.content_lines[start_row][:start_column] + self.content_lines[end_row][end_column:]
        self.content_lines[start_row : end_row + 1] = [merged_line]
        self.cursor_row = start_row
        self.cursor_column = start_column
        self.dirty = True
        return True

    def copy_selection(self) -> None:
        if self.selection_anchor is None:
            return

        selection_start = min(self.selection_anchor, (self.cursor_row, self.cursor_column))
        selection_end = max(self.selection_anchor, (self.cursor_row, self.cursor_column))
        if selection_start == selection_end:
            return

        start_row, start_column = selection_start
        end_row, end_column = selection_end
        if start_row == end_row:
            selected_text = self.content_lines[start_row][start_column:end_column]
        else:
            selected_lines = [self.content_lines[start_row][start_column:]]
            selected_lines.extend(self.content_lines[start_row + 1 : end_row])
            selected_lines.append(self.content_lines[end_row][:end_column])
            selected_text = "\n".join(selected_lines)

        try:
            subprocess.run(
                ["pbcopy"],
                input=selected_text,
                check=True,
                text=True,
                encoding="utf-8",
            )
        except (OSError, subprocess.SubprocessError):
            self.save_error = "Clipboard copy failed"
            self.render()

    def paste_clipboard(self) -> None:
        if self.file_path is None:
            return
        try:
            result = subprocess.run(
                ["pbpaste"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except (OSError, subprocess.SubprocessError):
            self.save_error = "Clipboard paste failed"
            self.render()
            return
        self.insert_text(result.stdout)

    def keep_cursor_visible(self) -> None:
        visible_rows = max(1, self.height - 3)
        visible_columns = max(1, self.width - 4)
        if self.cursor_row < self.scroll_offset:
            self.scroll_offset = self.cursor_row
        elif self.cursor_row >= self.scroll_offset + visible_rows:
            self.scroll_offset = self.cursor_row - visible_rows + 1

        if self.cursor_column < self.horizontal_offset:
            self.horizontal_offset = self.cursor_column
        elif self.cursor_column >= self.horizontal_offset + visible_columns:
            self.horizontal_offset = self.cursor_column - visible_columns + 1

    def render(self, active: bool = False) -> None:
        if self.file_operation_active:
            self.render_file_operation()
            return
        if self.mode == "editor":
            self.render_editor(active)
            return
        self.render_shell(active)

    def render_shell(self, active: bool = False) -> None:
        self.window.erase()
        self.draw_border(active)
        self.print_title(curses.color_pair(2 if active else 3))

        screen = self.shell.screen if self.shell is not None else None
        if screen is None:
            message = self.shell.error if self.shell is not None else "Shell is not available"
            try:
                self.window.addstr(1, 2, message[: max(0, self.width - 4)])
            except curses.error:
                pass
            self.window.refresh()
            return

        rows = min(screen.lines, max(0, self.height - 2))
        columns = max(0, self.width - 2)
        default_char = pyte.screens.Char(" ", "default", "default")
        with self.shell.lock:
            for row in range(rows):
                line = screen.buffer[row]
                column = 0
                while column < columns:
                    cell = line.get(column) or default_char
                    run = cell.data
                    next_column = column + 1
                    while next_column < columns:
                        next_cell = line.get(next_column) or default_char
                        if (
                            next_cell.fg != cell.fg
                            or next_cell.bg != cell.bg
                            or next_cell.bold != cell.bold
                            or next_cell.underscore != cell.underscore
                            or next_cell.reverse != cell.reverse
                        ):
                            break
                        run += next_cell.data
                        next_column += 1
                    style = self.shell_cell_style(cell)
                    try:
                        self.window.addstr(1 + row, 1 + column, run[: columns - column], style)
                    except curses.error:
                        pass
                    column = next_column

            if active:
                cursor_y = screen.cursor.y
                cursor_x = screen.cursor.x
                if 0 <= cursor_y < rows and 0 <= cursor_x < columns:
                    cursor_cell = screen.buffer[cursor_y].get(cursor_x) or default_char
                    cursor_char = cursor_cell.data or " "
                    try:
                        self.window.addstr(1 + cursor_y, 1 + cursor_x, cursor_char, curses.A_REVERSE)
                    except curses.error:
                        pass

        self.window.refresh()

    @staticmethod
    def shell_cell_style(cell: object) -> int:
        foreground = getattr(cell, "fg", "default")
        background = getattr(cell, "bg", "default")
        reverse = getattr(cell, "reverse", False)
        if reverse:
            foreground, background = background, foreground
        style = curses.color_pair(color_num(SHELL_COLOR_MAP.get(foreground, 7), SHELL_COLOR_MAP.get(background, 0)))
        if getattr(cell, "bold", False):
            style |= curses.A_BOLD
        if getattr(cell, "underscore", False):
            style |= curses.A_UNDERLINE
        return style

    def render_editor(self, active: bool = False) -> None:
        self.keep_cursor_visible()
        self.window.erase()
        self.draw_border(active)
        self.print_title(curses.color_pair(2 if active else 3))

        visible_rows = max(1, self.height - 3)
        start_index = self.scroll_offset
        end_index = min(len(self.content_lines), start_index + visible_rows)

        for offset, line in enumerate(self.content_lines[start_index:end_index]):
            row = start_index + offset
            visible_line = line[self.horizontal_offset : self.horizontal_offset + max(0, self.width - 4)]
            selection_start = selection_end = None
            if self.selection_anchor is not None:
                selection_start = min(self.selection_anchor, (self.cursor_row, self.cursor_column))
                selection_end = max(self.selection_anchor, (self.cursor_row, self.cursor_column))

            for column_offset, character in enumerate(visible_line):
                column = self.horizontal_offset + column_offset
                selected = (
                    selection_start is not None
                    and selection_start <= (row, column) < selection_end
                )
                try:
                    self.window.addstr(1 + offset, 2 + column_offset, character, curses.A_REVERSE if selected else curses.A_NORMAL)
                except curses.error:
                    pass

        status_width = max(0, self.width - 4)
        location = ""
        status = ""
        if active and self.file_path is not None:
            status = self.status_bar_text(self.dirty)
            if self.save_error:
                status = self.save_error
            location = f"Ln {self.cursor_row + 1}, Col {self.cursor_column + 1}"
        location = location[:status_width]
        status_width = max(0, status_width - len(location) - bool(status))
        try:
            self.window.addstr(self.height - 1, 2, status[:status_width], curses.A_BOLD)
            location_x = self.width - 2 - len(location)
            self.window.addstr(self.height - 1, location_x, location, curses.A_BOLD)
        except curses.error:
            pass

        if active and self.content_lines:
            caret_y = self.cursor_row - self.scroll_offset + 1
            caret_x = self.cursor_column - self.horizontal_offset + 2
            line = self.content_lines[self.cursor_row]
            caret_char = line[self.cursor_column : self.cursor_column + 1] or " "
            try:
                self.window.addstr(caret_y, caret_x, caret_char, curses.A_REVERSE)
            except curses.error:
                pass

        self.window.refresh()

    def set_geometry(self, x1: int, y1: int, x2: int, y2: int) -> None:
        super().set_geometry(x1, y1, x2, y2)
        shell = getattr(self, "shell", None)
        if shell is not None:
            shell.resize(max(1, self.height - 2), max(1, self.width - 2))

    def handle_shell_key(self, key: int | str) -> None:
        if self.shell is None:
            return

        if isinstance(key, str):
            if key == "\x1b":
                self.shell.write(b"\x1b")
            elif key == "	":
                self.shell.write(b"	")
            elif key in ("\n", "\r"):
                self.shell.write(b"\r")
            elif key.isprintable():
                self.shell.write(key.encode("utf-8"))
            return

        if key == curses.ERR:
            return
        if key == 27:
            self.shell.write(b"\x1b")
            return
        if key == 9:
            self.shell.write(b"	")
            return
        if key in (10, 13, curses.KEY_ENTER):
            self.shell.write(b"\r")
            return
        if key in (8, 127, curses.KEY_BACKSPACE):
            self.shell.write(b"\x7f")
            return

        sequence = SHELL_KEY_SEQUENCES.get(key)
        if sequence is not None:
            self.shell.write(sequence)
            return

        if 1 <= key <= 26:
            self.shell.write(bytes([key]))

    def handle_key(self, key: int | str) -> None:
        if key == 27:
            self.close_preview()
            return
        if isinstance(key, str):
            if key in ("\b", "\x7f"):
                key = curses.KEY_BACKSPACE
            elif key == "\x13":
                key = COMMAND_S_KEY
            elif key in ("\n", "\r"):
                key = curses.KEY_ENTER
            elif key.isprintable():
                self.insert_text(key)
                return
            else:
                return

        shifted_arrow_keys = {
            SHIFT_ARROW_UP,
            SHIFT_ARROW_DOWN,
            SHIFT_ARROW_LEFT,
            SHIFT_ARROW_RIGHT,
            curses.KEY_SF,
            curses.KEY_SR,
            curses.KEY_SLEFT,
            curses.KEY_SRIGHT,
        }
        if key in shifted_arrow_keys:
            if self.selection_anchor is None:
                self.selection_anchor = (self.cursor_row, self.cursor_column)
        elif key in (
            curses.KEY_UP,
            curses.KEY_DOWN,
            curses.KEY_LEFT,
            curses.KEY_RIGHT,
            curses.KEY_PPAGE,
            curses.KEY_NPAGE,
        ):
            self.selection_anchor = None

        if key in (19, COMMAND_S_KEY, curses.KEY_F2):
            self.save_content()
        elif key in (COMMAND_Z_KEY, curses.KEY_F5):
            self.undo()
        elif key in (COMMAND_SHIFT_Z_KEY, curses.KEY_F6):
            self.redo()
        elif key in (COMMAND_C_KEY, curses.KEY_F4):
            self.copy_selection()
        elif key in (COMMAND_V_KEY, curses.KEY_F3):
            self.paste_clipboard()
        elif key == curses.KEY_PPAGE:
            self.cursor_row = 0
            self.cursor_column = 0
        elif key == curses.KEY_NPAGE:
            self.cursor_row = len(self.content_lines) - 1
            self.cursor_column = len(self.content_lines[self.cursor_row])
        elif key in (curses.KEY_UP, SHIFT_ARROW_UP, curses.KEY_SR):
            self.cursor_row = max(0, self.cursor_row - 1)
            self.cursor_column = min(self.cursor_column, len(self.content_lines[self.cursor_row]))
        elif key in (curses.KEY_DOWN, SHIFT_ARROW_DOWN, curses.KEY_SF):
            self.cursor_row = min(len(self.content_lines) - 1, self.cursor_row + 1)
            self.cursor_column = min(self.cursor_column, len(self.content_lines[self.cursor_row]))
        elif key in (curses.KEY_LEFT, SHIFT_ARROW_LEFT, curses.KEY_SLEFT):
            if self.cursor_column > 0:
                self.cursor_column -= 1
            elif self.cursor_row > 0:
                self.cursor_row -= 1
                self.cursor_column = len(self.content_lines[self.cursor_row])
        elif key in (curses.KEY_RIGHT, SHIFT_ARROW_RIGHT, curses.KEY_SRIGHT):
            if self.cursor_column < len(self.content_lines[self.cursor_row]):
                self.cursor_column += 1
            elif self.cursor_row < len(self.content_lines) - 1:
                self.cursor_row += 1
                self.cursor_column = 0
        elif key in (10, 13, curses.KEY_ENTER):
            line = self.content_lines[self.cursor_row]
            self.remember_undo_state()
            self.content_lines[self.cursor_row] = line[: self.cursor_column]
            self.content_lines.insert(self.cursor_row + 1, line[self.cursor_column :])
            self.cursor_row += 1
            self.cursor_column = 0
            self.dirty = True
        elif key in (8, 127, curses.KEY_BACKSPACE):
            if not self.delete_selection():
                if self.cursor_column > 0:
                    line = self.content_lines[self.cursor_row]
                    self.remember_undo_state()
                    self.content_lines[self.cursor_row] = line[: self.cursor_column - 1] + line[self.cursor_column :]
                    self.cursor_column -= 1
                    self.dirty = True
                elif self.cursor_row > 0:
                    previous_line = self.content_lines[self.cursor_row - 1]
                    self.remember_undo_state()
                    self.cursor_column = len(previous_line)
                    self.content_lines[self.cursor_row - 1] += self.content_lines.pop(self.cursor_row)
                    self.cursor_row -= 1
                    self.dirty = True
        elif key == curses.KEY_DC:
            line = self.content_lines[self.cursor_row]
            if self.cursor_column < len(line):
                self.remember_undo_state()
                self.content_lines[self.cursor_row] = line[: self.cursor_column] + line[self.cursor_column + 1 :]
                self.dirty = True
            elif self.cursor_row < len(self.content_lines) - 1:
                self.remember_undo_state()
                self.content_lines[self.cursor_row] += self.content_lines.pop(self.cursor_row + 1)
                self.dirty = True

        self.save_error = "" if self.dirty else self.save_error
        self.render()


class FileManagerWindow(Window):
    def __init__(self, x1: int, y1: int, x2: int, y2: int, title: str, start_path: str, terminal_window: TerminalWindow | None = None) -> None:
        super().__init__(x1, y1, x2, y2, title)
        self.current_path = start_path
        self.selected_index = 0
        self.entries: list[tuple[str, str]] = []
        self.terminal_window = terminal_window
        self.preview_enabled = False
        self.modal_active = False
        self.modal_input = ""
        self.modal_cursor = 0
        self.modal_error = ""
        self.modal_action = "create"
        self.modal_target_path: str | None = None
        self.modal_target_is_dir = False
        self.refresh_content()

    def open_create_file_modal(self) -> None:
        self.modal_active = True
        self.modal_action = "create"
        self.modal_target_path = None
        self.modal_cursor = 0
        self.modal_input = ""
        self.modal_error = ""

    def open_rename_file_modal(self) -> bool:
        if not self.entries or self.selected_index >= len(self.entries):
            return False

        name, kind = self.entries[self.selected_index]
        if kind not in {"file", "dir"} or name == "..":
            return False

        self.modal_active = True
        self.modal_action = "rename"
        self.modal_target_path = os.path.join(self.current_path, name)
        self.modal_target_is_dir = kind == "dir"
        self.modal_input = self.modal_target_path
        self.modal_cursor = len(self.modal_input)
        self.modal_error = ""
        return True

    def open_copy_file_modal(self) -> bool:
        if not self.entries or self.selected_index >= len(self.entries):
            return False

        name, kind = self.entries[self.selected_index]
        if kind not in {"file", "dir"} or name == "..":
            return False

        self.modal_active = True
        self.modal_action = "copy"
        self.modal_target_path = os.path.join(self.current_path, name)
        self.modal_target_is_dir = kind == "dir"
        self.modal_input = self.modal_target_path
        self.modal_cursor = len(self.modal_input)
        self.modal_error = ""
        return True

    def open_delete_file_modal(self) -> bool:
        if not self.entries or self.selected_index >= len(self.entries):
            return False

        name, kind = self.entries[self.selected_index]
        if kind not in {"file", "dir"} or name == "..":
            return False

        self.modal_active = True
        self.modal_action = "delete"
        self.modal_target_path = os.path.join(self.current_path, name)
        self.modal_target_is_dir = kind == "dir"
        self.modal_input = ""
        self.modal_error = ""
        return True

    def close_create_file_modal(self) -> None:
        self.modal_active = False
        self.modal_action = "create"
        self.modal_target_path = None
        self.modal_target_is_dir = False
        self.modal_cursor = 0
        self.modal_input = ""
        self.modal_error = ""

    def render_modal(self) -> None:
        if not self.modal_active:
            return

        terminal_height, terminal_width = curses.LINES, curses.COLS
        modal_width = min(terminal_width - 10, 60)
        modal_height = 5
        modal_x = max(1, (terminal_width - modal_width) // 2)
        modal_y = max(1, (terminal_height - modal_height) // 2)

        modal_window = curses.newwin(modal_height, modal_width, modal_y, modal_x)
        modal_window.keypad(True)
        modal_window.erase()

        modal_bg = curses.color_pair(3)
        modal_title = curses.color_pair(4)
        modal_text = curses.color_pair(0)
        if self.light_theme:
            modal_border = curses.color_pair(LIGHT_THEME_ACTIVE_BORDER_PAIR)
        else:
            modal_border = curses.color_pair(4)

        try:
            modal_window.attron(modal_border)
            draw_window_border(modal_window, self.light_theme)
            modal_window.attroff(modal_border)
            modal_window.attron(modal_bg)
            for y in range(1, modal_height - 1):
                modal_window.addstr(y, 1, " " * (modal_width - 2))
            modal_window.attroff(modal_bg)

            if self.modal_action == "delete":
                title = "Delete folder" if self.modal_target_is_dir else "Delete file"
            elif self.modal_action == "rename":
                title = "Move / rename folder" if self.modal_target_is_dir else "Move / rename file"
            elif self.modal_action == "copy":
                title = "Copy folder" if self.modal_target_is_dir else "Copy file"
            else:
                title = "Create file"
            modal_window.attron(modal_title | curses.A_BOLD)
            modal_window.addstr(0, (modal_width - len(title)) // 2, title)
            modal_window.attroff(modal_title | curses.A_BOLD)

            modal_window.attron(modal_text)
            if self.modal_action == "delete":
                prompt = "Вы уверены?"
                modal_window.addstr(2, max(1, (modal_width - len(prompt)) // 2), prompt)
            else:
                label = "Path: " if self.modal_action in {"rename", "copy"} else "Name: "
                modal_window.addstr(2, 3, label)
                field_width = max(1, modal_width - 12)
                field_start = max(0, self.modal_cursor - field_width + 1)
                visible_input = self.modal_input[field_start : field_start + field_width]
                modal_window.addstr(2, 9, visible_input)
                cursor_offset = min(self.modal_cursor - field_start, field_width - 1)
                cursor_character = visible_input[cursor_offset : cursor_offset + 1] or " "
                modal_window.addstr(2, 9 + cursor_offset, cursor_character, curses.A_REVERSE)
            if self.modal_error:
                modal_window.addstr(3, 3, self.modal_error[: max(0, modal_width - 8)], curses.A_BOLD)
            if self.modal_action == "delete":
                footer = "Enter = delete, Esc = cancel"
            elif self.modal_action == "rename":
                footer = "Enter = move / rename, Esc = cancel"
            elif self.modal_action == "copy":
                footer = "Enter = copy, Esc = cancel"
            else:
                footer = "Enter= file  Shift+Enter= folder  Esc= cancel"
            modal_window.addstr(4, 2, footer[: max(0, modal_width - 2)])
            modal_window.attroff(modal_text)
        except curses.error:
            pass

        modal_window.refresh()

    def refresh_content(self) -> None:
        self.entries = []
        parent = os.path.dirname(self.current_path)
        if parent and self.current_path != parent:
            self.entries.append(("..", "dir"))

        try:
            items = sorted(
                os.scandir(self.current_path),
                key=lambda entry: (not entry.is_dir(), entry.name.lower()),
            )
        except OSError:
            self.entries = [("..", "dir")]
            self.current_path = os.getcwd()
            items = sorted(
                os.scandir(self.current_path),
                key=lambda entry: (not entry.is_dir(), entry.name.lower()),
            )

        for entry in items:
            kind = "dir" if entry.is_dir() else "file"
            self.entries.append((entry.name, kind))

        if self.selected_index >= len(self.entries):
            self.selected_index = max(0, len(self.entries) - 1)

        self.render()

    def render(self, active: bool = False) -> None:
        self.window.erase()
        self.draw_border(active)
        self.print_title(curses.color_pair(2 if active else 3))

        try:
            display_path = self.current_path
            if len(display_path) > self.width - 4:
                display_path = "..." + display_path[-(self.width - 7):]
            self.window.addstr(1, 2, display_path[: self.width - 4], curses.A_BOLD)
        except curses.error:
            pass

        max_rows = max(1, self.height - 4)
        for offset in range(max_rows):
            row_idx = offset + 2
            entry_index = offset
            if entry_index >= len(self.entries):
                break

            name, kind = self.entries[entry_index]
            display_name = name
            if len(display_name) > self.width - 8:
                display_name = display_name[: self.width - 9] + "..."
            prefix = ">" if entry_index == self.selected_index else " "
            label = f"{prefix} {display_name}"
            if kind == "dir":
                label += "/"

            style = curses.A_REVERSE if entry_index == self.selected_index else curses.A_NORMAL
            try:
                self.window.addstr(row_idx, 2, label[: self.width - 4], style)
            except curses.error:
                pass

        self.window.refresh()

    def handle_modal_key(self, key: int) -> None:
        if key == 27:
            self.close_create_file_modal()
            return

        enter_keys = (10, 13, curses.KEY_ENTER)
        shifted_enter = key == SHIFT_ENTER_KEY or key == getattr(curses, "KEY_SENTER", None)
        if key in enter_keys or shifted_enter:
            if self.modal_action in {"rename", "copy"}:
                if self.modal_target_path is None:
                    self.close_create_file_modal()
                    return

                destination_input = os.path.expanduser(self.modal_input.strip())
                if not destination_input:
                    self.modal_error = "Destination path cannot be empty"
                    return

                if not os.path.isabs(destination_input):
                    destination_input = os.path.join(self.current_path, destination_input)
                destination_path = os.path.abspath(destination_input)
                source_path = os.path.abspath(self.modal_target_path)
                destination_name = os.path.basename(destination_path)
                if destination_name in {"", ".", ".."}:
                    self.modal_error = "Invalid destination file name"
                    return
                if not os.path.isdir(os.path.dirname(destination_path)):
                    self.modal_error = "Destination folder does not exist"
                    return
                if destination_path == source_path:
                    if self.modal_action == "copy":
                        self.modal_error = "Source and destination are identical"
                        return
                    self.close_create_file_modal()
                    return
                if self.modal_target_is_dir and not os.path.islink(source_path):
                    if os.path.commonpath((source_path, destination_path)) == source_path:
                        action_verb = "copy" if self.modal_action == "copy" else "move"
                        self.modal_error = f"Cannot {action_verb} folder into itself"
                        return
                if os.path.lexists(destination_path):
                    self.modal_error = "Destination already exists"
                    return

                try:
                    if self.modal_action == "copy":
                        if self.modal_target_is_dir:
                            if os.path.islink(source_path):
                                os.symlink(os.readlink(source_path), destination_path)
                            else:
                                shutil.copytree(source_path, destination_path)
                        else:
                            shutil.copy2(source_path, destination_path)
                    else:
                        shutil.move(source_path, destination_path)
                except (OSError, shutil.Error):
                    if self.modal_action == "copy":
                        self.modal_error = "Failed to copy folder" if self.modal_target_is_dir else "Failed to copy file"
                    else:
                        self.modal_error = "Failed to move or rename folder" if self.modal_target_is_dir else "Failed to move or rename file"
                    return

                destination_is_current = os.path.dirname(destination_path) == os.path.abspath(self.current_path)
                action = self.modal_action
                self.close_create_file_modal()
                self.refresh_content()
                if destination_is_current and action == "copy":
                    for index, (entry_name, _) in enumerate(self.entries):
                        if entry_name == destination_name:
                            self.selected_index = index
                            break
                    self.render()
                elif destination_is_current:
                    for index, (entry_name, _) in enumerate(self.entries):
                        if entry_name == destination_name:
                            self.selected_index = index
                            break
                    self.render()
                return

            if self.modal_action == "delete":
                if shifted_enter:
                    return
                if self.modal_target_path is None:
                    self.close_create_file_modal()
                    return
                try:
                    if self.modal_target_is_dir and not os.path.islink(self.modal_target_path):
                        shutil.rmtree(self.modal_target_path)
                    else:
                        os.remove(self.modal_target_path)
                except OSError:
                    self.modal_error = "Failed to delete folder" if self.modal_target_is_dir else "Failed to delete file"
                    return

                self.close_create_file_modal()
                self.refresh_content()
                return

            file_name = self.modal_input.strip()
            if not file_name or len(file_name) > 255:
                self.modal_error = "Name cannot be empty or exceed 255 symbols"
                return

            invalid_chars = set('<>:"/\\|?*')
            if any(ch in invalid_chars for ch in file_name) or file_name in {'.', '..'}:
                self.modal_error = "Invalid file name"
                return

            target_path = os.path.join(self.current_path, file_name)
            if os.path.exists(target_path):
                self.modal_error = "File already exists"
                return

            try:
                if shifted_enter:
                    os.mkdir(target_path)
                else:
                    with open(target_path, "x", encoding="utf-8") as new_file:
                        new_file.write("")
            except OSError:
                self.modal_error = "Failed to create folder" if shifted_enter else "Failed to create file"
                return

            self.close_create_file_modal()
            self.refresh_content()
            return

        if key in (curses.KEY_LEFT, 260):
            self.modal_cursor = max(0, self.modal_cursor - 1)
            return

        if key in (curses.KEY_RIGHT, 261):
            self.modal_cursor = min(len(self.modal_input), self.modal_cursor + 1)
            return

        if key in (curses.KEY_HOME,):
            self.modal_cursor = 0
            return

        if key in (curses.KEY_END,):
            self.modal_cursor = len(self.modal_input)
            return

        if key in (8, 127, curses.KEY_BACKSPACE):
            if self.modal_cursor > 0:
                self.modal_input = self.modal_input[: self.modal_cursor - 1] + self.modal_input[self.modal_cursor :]
                self.modal_cursor -= 1
            if self.modal_error:
                self.modal_error = ""
            return

        if key == curses.KEY_DC:
            if self.modal_cursor < len(self.modal_input):
                self.modal_input = self.modal_input[: self.modal_cursor] + self.modal_input[self.modal_cursor + 1 :]
            if self.modal_error:
                self.modal_error = ""
            return

        if key in range(32, 127):
            max_length = 4096 if self.modal_action == "rename" else 255
            if len(self.modal_input) >= max_length:
                self.modal_error = "Path is too long" if self.modal_action == "rename" else "Name cannot exceed 255 symbols"
                return
            self.modal_input = self.modal_input[: self.modal_cursor] + chr(key) + self.modal_input[self.modal_cursor :]
            self.modal_cursor += 1
            self.modal_error = ""

    TEXT_EXTENSIONS = frozenset({
        ".txt", ".md", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml",
        ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm", ".css", ".scss",
        ".sass", ".less", ".js", ".ts", ".jsx", ".tsx", ".py", ".pyw", ".rb", ".php",
        ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".cs", ".go",
        ".rs", ".swift", ".m", ".mm", ".sh", ".bash", ".zsh", ".fish", ".bat",
        ".cmd", ".ps1", ".sql", ".r", ".lua", ".pl", ".pm", ".tcl", ".vim", ".el",
        ".tex", ".srt", ".vtt", ".svg", ".properties", ".env", ".gitignore",
        ".dockerignore", ".editorconfig", ".eslintrc", ".prettierrc", ".lock",
    })

    @classmethod
    def is_text_file(cls, file_path: str) -> bool:
        _, ext = os.path.splitext(file_path)
        return ext.lower() in cls.TEXT_EXTENSIONS or ext == ""

    def preview_file(self) -> None:
        if not self.entries or self.selected_index >= len(self.entries):
            return
        name, kind = self.entries[self.selected_index]
        if kind == "dir" or self.terminal_window is None:
            return
        file_path = os.path.join(self.current_path, name)
        if not self.is_text_file(file_path):
            return
        loaded_file_path: str | None = None
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                text = file.read()
            loaded_file_path = file_path
        except UnicodeDecodeError:
            with open(file_path, "r", encoding="utf-8", errors="replace") as file:
                text = file.read()
            loaded_file_path = file_path
        except OSError:
            text = f"Unable to read file: {file_path}"
        self.terminal_window.set_content(text, loaded_file_path, preview=True)

    def handle_key(self, key: int | str) -> None:
        if isinstance(key, str):
            return
        if key == curses.KEY_F1:
            self.open_create_file_modal()
            self.render()
            return
        if key == curses.KEY_F8:
            self.open_delete_file_modal()
            self.render()
            return

        navigated = False
        if key == curses.KEY_PPAGE:
            self.selected_index = 0
            navigated = True
        elif key == curses.KEY_NPAGE:
            self.selected_index = max(0, len(self.entries) - 1)
            navigated = True
        elif key in (curses.KEY_UP, 259, 65):
            self.selected_index = max(0, self.selected_index - 1)
            navigated = True
        elif key in (curses.KEY_DOWN, 258, 66):
            self.selected_index = min(len(self.entries) - 1, self.selected_index + 1)
            navigated = True

        if navigated and self.preview_enabled:
            self.preview_file()
        elif key in (10, 13, curses.KEY_ENTER):
            if not self.entries:
                return
            name, kind = self.entries[self.selected_index]
            if kind == "dir":
                if name == "..":
                    self.current_path = os.path.dirname(self.current_path)
                else:
                    self.current_path = os.path.join(self.current_path, name)
                self.selected_index = 0
                self.refresh_content()
            elif kind == "file" and self.terminal_window is not None:
                file_path = os.path.join(self.current_path, name)
                if not self.is_text_file(file_path):
                    return
                loaded_file_path: str | None = None
                try:
                    with open(file_path, "r", encoding="utf-8") as file:
                        text = file.read()
                    loaded_file_path = file_path
                except UnicodeDecodeError:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as file:
                        text = file.read()
                    loaded_file_path = file_path
                except OSError:
                    text = f"Unable to read file: {file_path}"
                self.terminal_window.set_content(text, loaded_file_path)
        elif key in (8, 127, ord("b")):
            parent = os.path.dirname(self.current_path)
            if parent and self.current_path != parent:
                self.current_path = parent
                self.selected_index = 0
                self.refresh_content()

        self.render()


class WindowManager:
    NON_SWITCHABLE_WINDOWS = {"Clock", "Date", "System info", "Hardware info", "Network info"}
    INFO_WINDOWS = {"Clock", "Date", "System info", "Hardware info", "Network info"}

    COPY_MOVE_MODAL = "модал. окно"
    COPY_MOVE_TWO_WINDOWS = "два окна"
    LAYOUT_TERMINAL_CENTER = "терминал по центру"
    LAYOUT_FILES_CENTER = "файловый менеджер по центру"
    LAYOUT_COMBINED = "комбинированный"

    MENU_DEFINITIONS = {
        1: (
            "Files",
            [
                ("Превью при пролистывании", "preview_toggle"),
                ("Режим копир./перемещ.: два окна", "copy_move_mode_toggle"),
            ],
        ),
        2: (
            "Settings",
            [
                ("Режим отображения экранов: комбинированный", "layout_mode_toggle"),
                ("Отображать секунды у часов", "clock_seconds_toggle"),
                ("Скрыть инфо панели", "hide_info_panels_toggle"),
                ("Легкая тема", "light_theme_toggle"),
            ],
        ),
    }

    def __init__(self, stdscr: curses.window | None = None) -> None:
        self.windows: dict[str, Window] = {}
        self.active_window_name: str | None = None
        self.last_active_window_name: str | None = None
        self.modal_window_name: str | None = None
        self.stdscr = stdscr
        self.context_menu_number: int | None = None
        self.context_menu_items: list[tuple[str, str]] = []
        self.context_menu_selection = 0
        self.context_menu_window: curses.window | None = None
        self.context_menu_panel: curses.panel.panel | None = None
        self.copy_move_mode = self.COPY_MOVE_TWO_WINDOWS
        self.layout_mode = self.LAYOUT_COMBINED
        self.clock_seconds_enabled = False
        self.hide_info_panels = False
        self.light_theme_enabled = False

    def add_window(self, window: Window) -> None:
        if window.title in self.windows:
            raise ValueError(f"Window already exists: {window.title}")
        
        name = window.title if window.title == "" else window.title
        self.windows[name] = window
        
        if self.active_window_name is None:
            self.active_window_name = name

    def get_switchable_windows(self) -> list[str]:
        return [name for name in self.windows if name not in self.NON_SWITCHABLE_WINDOWS]

    def get_window(self, name: str) -> Window:
        return self.windows[name]

    def get_active_window(self) -> Window:
        if self.active_window_name is None:
            raise RuntimeError("No windows have been registered")
        
        return self.windows[self.active_window_name]

    def set_active_window(self, name: str | None) -> None:
        if name is None:
            self.active_window_name = None
            return
        if name not in self.windows:
            return
        self.active_window_name = name

    def switch_active_window(self, reverse: bool = False) -> None:
        names = self.get_switchable_windows()

        if not names:
            return

        if self.active_window_name not in names:
            self.active_window_name = names[0]
            self.render_windows()
            return

        active_index = names.index(self.active_window_name)
        step = -1 if reverse else 1
        self.active_window_name = names[(active_index + step) % len(names)]
        self.render_windows()

    def is_window_visible(self, name: str) -> bool:
        if self.hide_info_panels and name in self.INFO_WINDOWS:
            return False
        return True

    def apply_layout(self) -> None:
        columns, lines = curses.COLS, curses.LINES
        bottom = lines - 3
        files = self.windows.get("Files")
        terminal = self.windows.get("Terminal")

        if self.hide_info_panels:
            half = columns // 2
            if half < 1 or bottom < 3:
                return
            if files is not None:
                files.set_geometry(0, 2, half, bottom)
            if terminal is not None:
                terminal.set_geometry(half, 2, columns, bottom)
        else:
            quarter = int(columns * 0.25)
            three_quarters = int(columns * 0.75)
            if quarter < 1 or three_quarters <= quarter or bottom < 3:
                return
            if files is not None:
                files.set_geometry(0, 2, quarter, bottom)
            if terminal is not None:
                terminal.set_geometry(quarter, 2, three_quarters, bottom)

            clock = self.windows.get("Clock")
            if clock is not None:
                clock.set_geometry(three_quarters, 2, int(columns * 0.875), 7)
            date = self.windows.get("Date")
            if date is not None:
                date.set_geometry(int(columns * 0.875), 2, columns, 7)

            system_info_geometry, hardware_info_geometry, network_info_geometry = right_panel_geometry(columns, lines)
            for name, geometry in (
                ("System info", system_info_geometry),
                ("Hardware info", hardware_info_geometry),
                ("Network info", network_info_geometry),
            ):
                window = self.windows.get(name)
                if window is not None:
                    window.set_geometry(*geometry)

        if self.active_window_name is not None and not self.is_window_visible(self.active_window_name):
            self.active_window_name = "Files" if "Files" in self.windows else None

        self.redraw_screen()

    def render_windows(self) -> None:
        for name, window in self.windows.items():
            if not self.is_window_visible(name):
                continue
            window.light_theme = self.light_theme_enabled
            is_active = name == self.active_window_name
            if isinstance(window, DateTimeWindow):
                window.render_time(is_active)
            elif isinstance(window, FileManagerWindow):
                window.render(is_active)
            elif isinstance(window, TerminalWindow):
                window.render(is_active)
            else:
                window.refresh_window(is_active)

        for window in self.windows.values():
            if isinstance(window, FileManagerWindow) and window.modal_active:
                window.render_modal()

        if self.context_menu_panel is not None:
            self.render_context_menu()

    def close_active_preview(self) -> bool:
        file_manager = next(
            (window for window in self.windows.values() if isinstance(window, FileManagerWindow)),
            None,
        )
        if file_manager is None or file_manager.terminal_window is None:
            return False
        if not file_manager.terminal_window.preview_mode:
            return False
        file_manager.terminal_window.close_preview()
        return True

    def resolve_menu_label(self, label: str, action: str) -> str:
        if action == "copy_move_mode_toggle":
            return f"Режим копир./перемещ.: {self.copy_move_mode}"
        if action == "layout_mode_toggle":
            return f"Режим отображения экранов: {self.layout_mode}"
        if action == "clock_seconds_toggle":
            return f"* {label}" if self.clock_seconds_enabled else label
        if action == "hide_info_panels_toggle":
            return f"* {label}" if self.hide_info_panels else label
        if action == "light_theme_toggle":
            return f"* {label}" if self.light_theme_enabled else label
        return label

    def open_context_menu(self, menu_number: int) -> None:
        definition = self.MENU_DEFINITIONS.get(menu_number)
        if definition is None:
            return

        title, items = definition
        self.close_context_menu()
        self.context_menu_number = menu_number
        self.context_menu_items = items
        self.context_menu_selection = 0
        screen_height, screen_width = curses.LINES, curses.COLS
        resolved_labels = [self.resolve_menu_label(label, action) for label, action in items]
        menu_width = min(screen_width - 2, max(len(title), *(len(lbl) for lbl in resolved_labels)) + 4)
        item_line_counts = [self.count_wrap_lines(lbl, menu_width - 4) for lbl in resolved_labels]
        menu_height = min(screen_height - 2, sum(item_line_counts) + len(items) + 2)
        tab_labels = {1: "Files", 2: "Settings"}
        tab_x = 1 + TOP_BAR_TEXT.index(tab_labels[menu_number])
        menu_x = min(max(0, screen_width - menu_width), tab_x)
        menu_y = 1

        self.context_menu_window = curses.newwin(menu_height, menu_width, menu_y, menu_x)
        self.context_menu_window.keypad(True)
        self.context_menu_panel = curses.panel.new_panel(self.context_menu_window)
        self.context_menu_panel.top()
        self.render_context_menu()

    def close_context_menu(self) -> None:
        had_menu = self.context_menu_panel is not None
        if self.context_menu_panel is not None:
            self.context_menu_panel.hide()
        self.context_menu_panel = None
        self.context_menu_window = None
        self.context_menu_number = None
        self.context_menu_items = []
        self.context_menu_selection = 0
        if had_menu:
            self.redraw_screen()

    def redraw_screen(self) -> None:
        if self.stdscr is None:
            return

        self.stdscr.clearok(True)
        self.stdscr.erase()
        display_func(self.stdscr)
        self.render_windows()
        curses.panel.update_panels()
        curses.doupdate()

    @staticmethod
    def count_wrap_lines(text: str, max_width: int) -> int:
        if max_width <= 0 or not text:
            return 1
        words = text.split()
        lines = 0
        current_len = 0
        for word in words:
            if current_len == 0:
                if len(word) <= max_width:
                    current_len = len(word)
                else:
                    lines += 1
                    remaining = len(word)
                    lines += (remaining + max_width - 1) // max_width
                    current_len = 0
            elif current_len + 1 + len(word) <= max_width:
                current_len += 1 + len(word)
            else:
                lines += 1
                current_len = len(word)
        if current_len > 0:
            lines += 1
        return max(lines, 1)

    @staticmethod
    def wrap_text(text: str, max_width: int) -> list[str]:
        if max_width <= 0 or not text:
            return [text]
        words = text.split()
        lines: list[str] = []
        current_line = ""
        for word in words:
            if not current_line:
                if len(word) <= max_width:
                    current_line = word
                else:
                    while word:
                        lines.append(word[:max_width])
                        word = word[max_width:]
                    current_line = ""
            elif len(current_line) + 1 + len(word) <= max_width:
                current_line += " " + word
            else:
                lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)
        if not lines:
            lines = [""]
        return lines

    def render_context_menu(self) -> None:
        if self.context_menu_window is None or self.context_menu_panel is None:
            return

        menu_window = self.context_menu_window
        _, items = self.MENU_DEFINITIONS[self.context_menu_number]
        menu_window.erase()
        menu_window.attron(curses.color_pair(3))
        for row in range(1, menu_window.getmaxyx()[0] - 1):
            menu_window.addstr(row, 1, " " * (menu_window.getmaxyx()[1] - 2))
        menu_window.attroff(curses.color_pair(3))
        menu_border_pair = LIGHT_THEME_ACTIVE_BORDER_PAIR if self.light_theme_enabled else 4
        menu_window.attron(curses.color_pair(menu_border_pair))
        draw_window_border(menu_window, self.light_theme_enabled)
        menu_window.attroff(curses.color_pair(menu_border_pair))

        title = self.MENU_DEFINITIONS[self.context_menu_number][0]
        try:
            menu_window.addstr(0, 2, title[: max(0, menu_window.getmaxyx()[1] - 4)], curses.color_pair(2) | curses.A_BOLD)
        except curses.error:
            pass

        menu_content_width = max(0, menu_window.getmaxyx()[1] - 4)
        file_manager = next(
            (window for window in self.windows.values() if isinstance(window, FileManagerWindow)),
            None,
        )
        row = 1
        for index, (label, action) in enumerate(items):
            label = self.resolve_menu_label(label, action)
            if action == "preview_toggle" and file_manager is not None and file_manager.preview_enabled:
                label = "* " + label
            wrapped_lines = self.wrap_text(label, menu_content_width)
            for line_idx, wrapped_line in enumerate(wrapped_lines):
                if row >= menu_window.getmaxyx()[0] - 1:
                    break
                style = curses.A_REVERSE if index == self.context_menu_selection and line_idx == 0 else curses.A_NORMAL
                try:
                    menu_window.addstr(row, 1, wrapped_line[: max(0, menu_window.getmaxyx()[1] - 2)], curses.color_pair(3) | style)
                except curses.error:
                    pass
                row += 1
            if row >= menu_window.getmaxyx()[0] - 1:
                break

        self.context_menu_panel.top()
        curses.panel.update_panels()
        curses.doupdate()

    def activate_context_menu_item(self) -> None:
        if self.context_menu_number is None or not self.context_menu_items:
            return

        menu_number = self.context_menu_number
        _, action = self.context_menu_items[self.context_menu_selection]
        self.close_context_menu()

        if action.startswith("window:"):
            self.set_active_window(action.partition(":")[2])
            return

        if action == "copy_move_mode_toggle":
            if self.copy_move_mode == self.COPY_MOVE_MODAL:
                self.copy_move_mode = self.COPY_MOVE_TWO_WINDOWS
            else:
                self.copy_move_mode = self.COPY_MOVE_MODAL
            return

        if action == "layout_mode_toggle":
            if self.layout_mode == self.LAYOUT_TERMINAL_CENTER:
                self.layout_mode = self.LAYOUT_FILES_CENTER
            elif self.layout_mode == self.LAYOUT_FILES_CENTER:
                self.layout_mode = self.LAYOUT_COMBINED
            else:
                self.layout_mode = self.LAYOUT_TERMINAL_CENTER
            return

        if action == "clock_seconds_toggle":
            self.clock_seconds_enabled = not self.clock_seconds_enabled
            return

        if action == "hide_info_panels_toggle":
            self.hide_info_panels = not self.hide_info_panels
            self.apply_layout()
            return

        if action == "light_theme_toggle":
            self.light_theme_enabled = not self.light_theme_enabled
            for window in self.windows.values():
                window.light_theme = self.light_theme_enabled
            self.redraw_screen()
            return

        file_manager = next(
            (window for window in self.windows.values() if isinstance(window, FileManagerWindow)),
            None,
        )
        if file_manager is None:
            return

        if action == "create":
            file_manager.open_create_file_modal()
            opened = True
        elif action == "rename":
            opened = file_manager.open_rename_file_modal()
        elif action == "copy":
            opened = file_manager.open_copy_file_modal()
        elif action == "delete":
            opened = file_manager.open_delete_file_modal()
        elif action == "preview_toggle":
            file_manager.preview_enabled = not file_manager.preview_enabled
            self.active_window_name = file_manager.title
            opened = False
        else:
            opened = False

        if opened:
            self.last_active_window_name = self.active_window_name
            self.modal_window_name = file_manager.title
            self.active_window_name = None

    def handle_files_mouse_click(self, mouse_x: int, mouse_y: int, button_state: int) -> bool:
        click_mask = (
            getattr(curses, "BUTTON1_CLICKED", 0)
            | getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
        )
        if not click_mask or not button_state & click_mask:
            return False

        for window in self.windows.values():
            if not isinstance(window, FileManagerWindow):
                continue
            if not (window.x1 < mouse_x < window.x2 - 1):
                continue

            entry_index = mouse_y - window.y1 - 2
            if not 0 <= entry_index < min(len(window.entries), max(0, window.height - 4)):
                continue

            window.selected_index = entry_index
            self.active_window_name = window.title
            _, kind = window.entries[entry_index]
            window.handle_key(curses.KEY_ENTER)
            if kind == "file" and window.terminal_window is not None:
                terminal_window = window.terminal_window
                if self.windows.get(terminal_window.title) is terminal_window:
                    self.active_window_name = terminal_window.title
            return True

        return False

    def activate_window_at(self, mouse_x: int, mouse_y: int) -> bool:
        for name, window in self.windows.items():
            if not self.is_window_visible(name):
                continue
            if window.x1 <= mouse_x < window.x2 and window.y1 <= mouse_y < window.y2:
                self.active_window_name = name
                return True
        return False

    @staticmethod
    def read_modal_key(window: curses.window) -> int:
        try:
            key = window.getch()
        except curses.error:
            return curses.ERR
        if key != 27:
            return key

        sequence: list[int] = []
        window.timeout(50)
        try:
            while True:
                try:
                    next_key = window.getch()
                except curses.error:
                    break
                if next_key == curses.ERR:
                    break
                sequence.append(next_key)
                if len(sequence) > 1 and 0x40 <= next_key <= 0x7E:
                    break
        finally:
            window.timeout(INPUT_POLL_INTERVAL_MS)

        encoded = bytes(sequence) if all(0 <= value <= 0xFF for value in sequence) else b""
        if encoded in (b"[13;2u", b"[13;2~", b"[27;2;13~"):
            return SHIFT_ENTER_KEY
        return key

    @staticmethod
    def read_terminal_key(window: curses.window) -> int | str:
        try:
            key = window.get_wch()
        except curses.error:
            return curses.ERR
        if key != "\x1b":
            return key

        sequence: list[str] = []
        window.timeout(50)
        try:
            while True:
                try:
                    next_key = window.get_wch()
                except curses.error:
                    break
                if next_key == curses.ERR or not isinstance(next_key, str):
                    break
                sequence.append(next_key)
                if len(sequence) > 1 and 0x40 <= ord(next_key) <= 0x7E:
                    break
        finally:
            window.timeout(INPUT_POLL_INTERVAL_MS)

        encoded = "".join(sequence)
        if encoded in ("	", "[9;3u", "[27;3;9~"):
            return ALT_TAB_KEY
        if encoded in ("[115;9u", "[83;10u"):
            return COMMAND_S_KEY
        if encoded in ("[122;9u", "[90;9u"):
            return COMMAND_Z_KEY
        if encoded in ("[122;10u", "[90;10u"):
            return COMMAND_SHIFT_Z_KEY
        if encoded in ("[99;9u", "[67;10u"):
            return COMMAND_C_KEY
        if encoded in ("[118;9u", "[86;10u"):
            return COMMAND_V_KEY
        shifted_arrow_sequences = {
            "[1;2A": SHIFT_ARROW_UP,
            "[1;2B": SHIFT_ARROW_DOWN,
            "[1;2C": SHIFT_ARROW_RIGHT,
            "[1;2D": SHIFT_ARROW_LEFT,
        }
        if encoded in shifted_arrow_sequences:
            return shifted_arrow_sequences[encoded]
        return curses.ERR if sequence else key

    def handle_input(self) -> bool:
        if self.context_menu_window is not None:
            input_window = self.context_menu_window
            input_window.timeout(INPUT_POLL_INTERVAL_MS)
            try:
                key = input_window.getch()
            except curses.error:
                key = curses.ERR

            if key == 27:
                self.close_context_menu()
            elif key == curses.KEY_MOUSE:
                try:
                    _, mouse_x, mouse_y, _, button_state = curses.getmouse()
                except curses.error:
                    mouse_x = mouse_y = -1
                    button_state = 0
                left_click = getattr(curses, "BUTTON1_CLICKED", 0) | getattr(curses, "BUTTON1_PRESSED", 0)
                if button_state & left_click:
                    menu_y, menu_x = input_window.getbegyx()
                    menu_height, menu_width = input_window.getmaxyx()
                    item_index = mouse_y - menu_y - 1
                    clicked_item = (
                        menu_x < mouse_x < menu_x + menu_width - 1
                        and 0 <= item_index < len(self.context_menu_items)
                    )
                    clicked_tab = top_tab_menu_at(mouse_x, mouse_y)
                    if clicked_tab is not None:
                        if clicked_tab == self.context_menu_number:
                            self.close_context_menu()
                        else:
                            self.open_context_menu(clicked_tab)
                    elif clicked_item:
                        self.context_menu_selection = item_index
                        self.activate_context_menu_item()
                    else:
                        self.close_context_menu()
            elif key in (curses.KEY_UP, 259):
                self.context_menu_selection = (self.context_menu_selection - 1) % len(self.context_menu_items)
            elif key in (curses.KEY_DOWN, 258):
                self.context_menu_selection = (self.context_menu_selection + 1) % len(self.context_menu_items)
            elif key in (10, 13, curses.KEY_ENTER):
                self.activate_context_menu_item()
            self.render_windows()
            return True

        if self.modal_window_name is not None:
            modal_window = self.windows.get(self.modal_window_name)
            if modal_window is None or not isinstance(modal_window, FileManagerWindow):
                self.modal_window_name = None
                if self.last_active_window_name is not None:
                    self.set_active_window(self.last_active_window_name)
                    self.last_active_window_name = None
                self.render_windows()
                return True

            input_window = modal_window.get_window()
            input_window.timeout(INPUT_POLL_INTERVAL_MS)
            key = self.read_modal_key(input_window)
            if key == 27:
                modal_window.close_create_file_modal()
                self.modal_window_name = None
                if self.last_active_window_name is not None:
                    self.set_active_window(self.last_active_window_name)
                    self.last_active_window_name = None
                self.render_windows()
                return True

            modal_window.handle_modal_key(key)
            if not modal_window.modal_active:
                self.modal_window_name = None
                if self.last_active_window_name is not None:
                    self.set_active_window(self.last_active_window_name)
                    self.last_active_window_name = None
            self.render_windows()
            return True

        if self.active_window_name is None:
            return True

        active_window = self.get_active_window()
        input_window = active_window.get_window()
        shell_active = (
            isinstance(active_window, TerminalWindow)
            and active_window.mode == "shell"
            and not active_window.file_operation_active
        )
        input_window.timeout(50 if shell_active else INPUT_POLL_INTERVAL_MS)
        key = self.read_terminal_key(input_window)
        if key == "\x1b":
            key = 27
        elif key == "	":
            key = 9
        elif isinstance(key, str) and len(key) == 1 and not key.isprintable():
            key = ord(key)

        key_from_mouse = False
        if key == curses.KEY_MOUSE:
            try:
                _, mouse_x, mouse_y, _, button_state = curses.getmouse()
            except curses.error:
                button_state = 0
                mouse_x = mouse_y = -1
            left_click = getattr(curses, "BUTTON1_CLICKED", 0) | getattr(curses, "BUTTON1_PRESSED", 0)
            if button_state & left_click:
                bottom_key = bottom_bar_at(mouse_x, mouse_y)
                if bottom_key is not None:
                    key = bottom_key
                    key_from_mouse = True
                elif (
                    isinstance(active_window, TerminalWindow)
                    and (editor_key := active_window.status_button_at(mouse_x, mouse_y)) is not None
                ):
                    key = editor_key
                    key_from_mouse = True
                elif self.handle_files_mouse_click(mouse_x, mouse_y, button_state):
                    self.render_windows()
                    return True
                elif (context_menu_number := top_tab_menu_at(mouse_x, mouse_y)) is not None:
                    self.open_context_menu(context_menu_number)
                    self.render_windows()
                    return True
                elif self.activate_window_at(mouse_x, mouse_y):
                    self.render_windows()
                    return True

        if key == ALT_TAB_KEY:
            self.switch_active_window()
            return True

        if isinstance(active_window, TerminalWindow) and not active_window.file_operation_active:
            if key == curses.KEY_F10:
                return False
            if shell_active and not key_from_mouse:
                active_window.handle_shell_key(key)
                self.render_windows()
                return True
            if active_window.mode == "editor" and key == 27:
                active_window.handle_key(key)
                self.render_windows()
                return True

        if key == curses.KEY_F10:
            return False

        if key == 27:   # Escape key
            if isinstance(active_window, TerminalWindow) and active_window.file_operation_active:
                active_window.handle_file_operation_key(key)
                self.render_windows()
                return True
            if self.close_active_preview():
                self.render_windows()
                return True
            return True

        if key == 9:    # Tab key
            if isinstance(active_window, TerminalWindow) and active_window.file_operation_active:
                active_window.handle_file_operation_key(key)
                self.render_windows()
                return True
            return True

        if key == curses.KEY_F2 and isinstance(active_window, FileManagerWindow):
            if self.copy_move_mode == self.COPY_MOVE_TWO_WINDOWS:
                if active_window.entries and 0 <= active_window.selected_index < len(active_window.entries):
                    name, kind = active_window.entries[active_window.selected_index]
                    if kind in {"file", "dir"} and name != "..":
                        source_path = os.path.join(active_window.current_path, name)
                        terminal_window = active_window.terminal_window
                        if terminal_window is not None:
                            terminal_window.start_file_operation("rename", source_path)
                            self.active_window_name = terminal_window.title
                            self.render_windows()
                            return True
            elif active_window.open_rename_file_modal():
                self.last_active_window_name = self.active_window_name
                self.modal_window_name = active_window.title
                self.active_window_name = None
                self.render_windows()
                return True

        if key == curses.KEY_F3 and isinstance(active_window, FileManagerWindow):
            if self.copy_move_mode == self.COPY_MOVE_TWO_WINDOWS:
                if active_window.entries and 0 <= active_window.selected_index < len(active_window.entries):
                    name, kind = active_window.entries[active_window.selected_index]
                    if kind in {"file", "dir"} and name != "..":
                        source_path = os.path.join(active_window.current_path, name)
                        terminal_window = active_window.terminal_window
                        if terminal_window is not None:
                            terminal_window.start_file_operation("copy", source_path)
                            self.active_window_name = terminal_window.title
                            self.render_windows()
                            return True
            elif active_window.open_copy_file_modal():
                self.last_active_window_name = self.active_window_name
                self.modal_window_name = active_window.title
                self.active_window_name = None
                self.render_windows()
                return True

        if key == curses.KEY_F1:
            file_manager = next(
                (window for window in self.windows.values() if isinstance(window, FileManagerWindow)),
                None,
            )
            if file_manager is not None:
                self.last_active_window_name = self.active_window_name
                self.modal_window_name = file_manager.title
                file_manager.open_create_file_modal()
                self.active_window_name = None
                self.render_windows()
                return True

        if key == curses.KEY_F8:
            file_manager = next(
                (window for window in self.windows.values() if isinstance(window, FileManagerWindow)),
                None,
            )
            if file_manager is not None and file_manager.open_delete_file_modal():
                self.last_active_window_name = self.active_window_name
                self.modal_window_name = file_manager.title
                self.active_window_name = None
                self.render_windows()
                return True

        if isinstance(active_window, TerminalWindow):
            if active_window.file_operation_active:
                was_active = active_window.file_operation_active
                active_window.handle_file_operation_key(key)
                if was_active and not active_window.file_operation_active:
                    file_manager = next(
                        (window for window in self.windows.values() if isinstance(window, FileManagerWindow)),
                        None,
                    )
                    if file_manager is not None:
                        file_manager.refresh_content()
                    self.active_window_name = "Files"
                self.render_windows()
                return True
            if active_window.mode == "editor":
                active_window.handle_key(key)
            self.render_windows()
            return True

        if isinstance(active_window, FileManagerWindow):
            opening_file = (
                key in (10, 13, curses.KEY_ENTER)
                and active_window.terminal_window is not None
                and 0 <= active_window.selected_index < len(active_window.entries)
                and active_window.entries[active_window.selected_index][1] == "file"
            )
            active_window.handle_key(key)
            if opening_file:
                terminal_window = active_window.terminal_window
                if terminal_window is not None and self.windows.get(terminal_window.title) is terminal_window:
                    self.active_window_name = terminal_window.title

        self.render_windows()
        return True

def display_func(stdscr: curses.window) -> None:
    try:
        stdscr.addstr(0, 1, TOP_BAR_TEXT[: max(0, curses.COLS - 2)])

        row = curses.LINES - 2
        width = curses.COLS
        labels = bottom_bar_labels(width)

        for column, key, description in labels:
            if column >= width:
                continue
            try:
                stdscr.attron(curses.A_REVERSE)
                stdscr.addstr(row, column, key)
                stdscr.attroff(curses.A_REVERSE)
                if column + len(key) + 1 + len(description) < width:
                    stdscr.addstr(row, column + len(key) + 1, f"-{description}")
            except curses.error:
                pass
    except curses.error:
        pass
    stdscr.refresh()


def run(stdscr: curses.window) -> None:
    init_terminal()
    display_func(stdscr)

    path = os.getcwd()[: ARR_SIZE - 1]
    columns = curses.COLS
    lines = curses.LINES

    window_manager = WindowManager(stdscr)
    terminal_window = TerminalWindow(int(columns * 0.25), 2, int(columns * 0.75), lines - 3, "Terminal")
    window_manager.add_window(FileManagerWindow(0, 2, int(columns * 0.25), lines - 3, "Files", path, terminal_window))
    window_manager.add_window(terminal_window)
    window_manager.add_window(DateTimeWindow(int(columns * 0.75), 2, int(columns * 0.875), 7, "Clock", window_manager=window_manager))
    window_manager.add_window(DateTimeWindow(int(columns * 0.875), 2, columns, 7, "Date", True))
    system_info_geometry, hardware_info_geometry, network_info_geometry = right_panel_geometry(columns, lines)
    window_manager.add_window(SystemInfoWindow(*system_info_geometry, "System info"))
    window_manager.add_window(HardwareInfoWindow(*hardware_info_geometry, "Hardware info"))
    window_manager.add_window(NetworkInfoWindow(*network_info_geometry, "Network info"))

    window_manager.active_window_name = "Files"
    window_manager.render_windows()

    try:
        while window_manager.handle_input():
            window_manager.render_windows()
    finally:
        terminal_window.shell.close()


def main() -> None:
    global LOG_FILE

    terminal_type = os.environ.get("TERM", "")
    terminal_program = os.environ.get("TERM_PROGRAM", "")
    supports_modified_keys = terminal_type.startswith("xterm")
    supports_command_keys = terminal_program in {"vscode", "iTerm.app", "WezTerm", "kitty", "ghostty"} or terminal_type.startswith(("xterm-kitty", "xterm-ghostty"))
    if supports_modified_keys:
        sys.stdout.write("\x1b[>4;2m")
        sys.stdout.flush()
    if supports_command_keys:
        sys.stdout.write("\x1b[>1u")
        sys.stdout.flush()

    if DEBUG:
        try:
            with open("log.log", "w", encoding="utf-8") as log_file:
                LOG_FILE = log_file
                curses.wrapper(run)
        finally:
            if supports_command_keys:
                sys.stdout.write("\x1b[<u")
                sys.stdout.flush()
            if supports_modified_keys:
                sys.stdout.write("\x1b[>4;0m")
                sys.stdout.flush()
    else:
        try:
            curses.wrapper(run)
        finally:
            if supports_command_keys:
                sys.stdout.write("\x1b[<u")
                sys.stdout.flush()
            if supports_modified_keys:
                sys.stdout.write("\x1b[>4;0m")
                sys.stdout.flush()


if __name__ == "__main__":
    main()