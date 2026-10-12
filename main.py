import curses
import ipaddress
import os
import re
import shutil
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

ARR_SIZE = 1024
DEBUG = True
LOG_FILE: TextIO | None = None
INPUT_POLL_INTERVAL_MS = 250
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


def log_info(value: object) -> None:
    if DEBUG and LOG_FILE is not None:
        LOG_FILE.write(f"LOG_INFO, {value}\n")


def color_num(fg: int, bg: int) -> int:
    return 0x80 | ((7 & bg) << 4) | (7 & fg)


def right_panel_geometry(columns: int, lines: int) -> list[tuple[int, int, int, int]]:
    x1 = int(columns * 0.75)
    y1 = max(7, int(lines * 0.125) - 1)
    panel_height = max(1, (lines - 3 - y1) // 3)
    return [
        (x1, y1 + panel_height * index, columns, y1 + panel_height * (index + 1))
        for index in range(3)
    ]


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


def init_terminal() -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass

    curses.start_color()
    curses.noecho()
    curses.cbreak()
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
    ):
        try:
            curses.init_pair(pair, foreground, background)
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

        self.window = curses.newwin(self.height, self.width, self.y1, self.x1)
        self.window.keypad(True)
        self.refresh_window()

    def refresh_window(self, active: bool = False) -> None:
        self.window.erase()
        border_color = curses.color_pair(4 if active else 3)
        self.window.attron(border_color)
        self.window.box()
        self.window.attroff(border_color)
        self.print_title(curses.color_pair(2 if active else 3))
        self.window.refresh()

    def resize(self, height: int, width: int) -> None:
        self.height = height
        self.width = width
        self.refresh_window()

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
    def __init__(self, x1: int, y1: int, x2: int, y2: int, title: str, _isDate: bool = False) -> None:
        super().__init__(x1, y1, x2, y2, title)
        self.isDate = _isDate
        self.render_time()

    def render_time(self, active: bool = False) -> None:
        if self.isDate:
            pattern = "%d.%m.%Y"
        else:
            pattern = "%H:%M:%S"

        current_time = datetime.now().strftime(pattern)
        self.window.erase()
        border_color = curses.color_pair(4 if active else 3)
        self.window.attron(border_color)
        self.window.box()
        self.window.attroff(border_color)
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
        self.metrics = ["CPU N/A/100%", "GPU N/A/100%", "RAM N/A", "SSD N/A", "SWAP N/A"]
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
                metrics = ["CPU N/A/100%", "GPU N/A/100%", "RAM N/A", "SSD N/A", "SWAP N/A"]
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

    def read_metrics(self) -> list[str]:
        cpu_output = self.run_command(["top", "-l", "1", "-n", "0"])
        cpu_match = re.search(r"CPU usage: .*?, .*?, ([\d.]+)% idle", cpu_output)
        cpu_usage = f"{100 - float(cpu_match.group(1)):.0f}%" if cpu_match else "N/A"

        gpu_output = self.run_command([
            "ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator", "-k", "PerformanceStatistics"
        ])
        gpu_match = re.search(r'"Device Utilization %"\s*=\s*([\d.]+)', gpu_output)
        gpu_usage = f"{float(gpu_match.group(1)):.0f}%" if gpu_match else "N/A"

        total_memory_output = self.run_command(["sysctl", "-n", "hw.memsize"])
        vm_output = self.run_command(["vm_stat"])
        memory_total_match = re.search(r"\d+", total_memory_output)
        page_size_match = re.search(r"page size of (\d+) bytes", vm_output)
        free_pages_match = re.search(r"Pages free:\s+(\d+)", vm_output)
        if memory_total_match and page_size_match and free_pages_match:
            memory_total = int(memory_total_match.group())
            memory_used = max(0, memory_total - int(page_size_match.group(1)) * int(free_pages_match.group(1)))
            memory = f"RAM {self.format_bytes(memory_used)}/{self.format_bytes(memory_total)}"
        else:
            memory = "RAM N/A"

        swap_output = self.run_command(["sysctl", "vm.swapusage"])
        swap_match = re.search(r"total = ([\d.]+)([KMGTP])\s+used = ([\d.]+)([KMGTP])", swap_output)
        if swap_match:
            unit_bytes = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}
            swap_total = int(float(swap_match.group(1)) * unit_bytes[swap_match.group(2)])
            swap_used = int(float(swap_match.group(3)) * unit_bytes[swap_match.group(4)])
            swap = f"SWAP {self.format_bytes(swap_used)}/{self.format_bytes(swap_total)}"
        else:
            swap = "SWAP N/A"

        try:
            disk = shutil.disk_usage("/")
            disk_used = disk.total - disk.free
            ssd = f"SSD {self.format_bytes(disk_used)}/{self.format_bytes(disk.total)}"
        except OSError:
            ssd = "SSD N/A"

        return [f"CPU {cpu_usage}/100%", f"GPU {gpu_usage}/100%", memory, ssd, swap]

    def refresh_window(self, active: bool = False) -> None:
        super().refresh_window(active)
        with self._metrics_lock:
            metrics = self.metrics.copy()
        for row, metric in enumerate(metrics, start=2):
            if row >= self.height - 1:
                break
            try:
                self.window.addstr(row, 3, metric[: max(0, self.width - 5)])
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
            (self.height - 4, "Traffic (recent)"),
            (self.height - 3, f"Down {self.format_rate(current_download)} {self.sparkline(download_values, graph_width)}"),
            (self.height - 2, f"Up   {self.format_rate(current_upload)} {self.sparkline(upload_values, graph_width)}"),
        )
        for row, line in traffic_rows:
            if row < 1:
                continue
            try:
                self.window.addstr(row, 3, line[: max(0, self.width - 4)])
            except curses.error:
                pass
        self.window.refresh()


class TerminalWindow(Window):
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
        self.render()

    def set_content(self, text: str, file_path: str | None = None) -> None:
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
        self.keep_cursor_visible()
        self.window.erase()
        border_color = curses.color_pair(4 if active else 3)
        self.window.attron(border_color)
        self.window.box()
        self.window.attroff(border_color)
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
            status = "F2 save  F3 paste  F4 copy  F5 undo  F6 redo" + (" *" if self.dirty else "")
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

    def handle_key(self, key: int | str) -> None:
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
        modal_border = curses.color_pair(4)

        try:
            modal_window.attron(modal_border)
            modal_window.box()
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
        border_color = curses.color_pair(4 if active else 3)
        self.window.attron(border_color)
        self.window.box()
        self.window.attroff(border_color)
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

    def handle_key(self, key: int) -> None:
        if key == curses.KEY_F1:
            self.open_create_file_modal()
            self.render()
            return
        if key == curses.KEY_F8:
            self.open_delete_file_modal()
            self.render()
            return

        if key == curses.KEY_PPAGE:
            self.selected_index = 0
        elif key == curses.KEY_NPAGE:
            self.selected_index = max(0, len(self.entries) - 1)
        elif key in (curses.KEY_UP, 259, 65):
            self.selected_index = max(0, self.selected_index - 1)
        elif key in (curses.KEY_DOWN, 258, 66):
            self.selected_index = min(len(self.entries) - 1, self.selected_index + 1)
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

    def __init__(self) -> None:
        self.windows: dict[str, Window] = {}
        self.active_window_name: str | None = None
        self.last_active_window_name: str | None = None
        self.modal_window_name: str | None = None

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

    def render_windows(self) -> None:
        for name, window in self.windows.items():
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
        return key

    def handle_input(self) -> bool:
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
        if isinstance(active_window, TerminalWindow):
            input_window = active_window.get_window()
            input_window.timeout(INPUT_POLL_INTERVAL_MS)
            key = self.read_terminal_key(input_window)
            if key == "\x1b":
                key = 27
            elif key == "\t":
                key = 9
        else:
            input_window = active_window.get_window()
            input_window.timeout(INPUT_POLL_INTERVAL_MS)
            try:
                key = input_window.getch()
            except curses.error:
                key = curses.ERR

        if key == 27:   # Escape key
            return False

        if key == 9:    # Tab key
            self.switch_active_window()
            return True

        if key == curses.KEY_BTAB:
            self.switch_active_window(reverse=True)
            return True

        if key == curses.KEY_F2 and isinstance(active_window, FileManagerWindow):
            if active_window.open_rename_file_modal():
                self.last_active_window_name = self.active_window_name
                self.modal_window_name = active_window.title
                self.active_window_name = None
                self.render_windows()
                return True

        if key == curses.KEY_F3 and isinstance(active_window, FileManagerWindow):
            if active_window.open_copy_file_modal():
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
        stdscr.addstr(0, 1, "Left panel    Settings    Right panel")

        row = curses.LINES - 2
        width = curses.COLS
        labels = [
            (1, "Tab", "next"),
            (11, "Shift+Tab", "prev"),
            (26, "Esc", "quit"),
        ]

        if width > 100:
            labels = [
                (1, "Tab", "след. окно"),
                (17, "Shift+Tab", "пред. окно"),
                (39, "Esc", "Выход"),
                (50, "F1", "Создать"),
                (62, "F2", "Переим."),
                (74, "F3", "Копия"),
                (84, "F8", "Удалить"),
                (96, "F5", "Undo"),
                (105, "F6", "Redo"),
            ]

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

    window_manager = WindowManager()
    terminal_window = TerminalWindow(int(columns * 0.25), 2, int(columns * 0.75), lines - 3, "Terminal")
    window_manager.add_window(FileManagerWindow(0, 2, int(columns * 0.25), lines - 3, "Files", path, terminal_window))
    window_manager.add_window(terminal_window)
    window_manager.add_window(DateTimeWindow(int(columns * 0.75), 2, int(columns * 0.875), 7, "Clock"))
    window_manager.add_window(DateTimeWindow(int(columns * 0.875), 2, columns, 7, "Date", True))
    system_info_geometry, hardware_info_geometry, network_info_geometry = right_panel_geometry(columns, lines)
    window_manager.add_window(SystemInfoWindow(*system_info_geometry, "System info"))
    window_manager.add_window(HardwareInfoWindow(*hardware_info_geometry, "Hardware info"))
    window_manager.add_window(NetworkInfoWindow(*network_info_geometry, "Network info"))

    window_manager.active_window_name = "Files"
    window_manager.render_windows()

    while window_manager.handle_input():
        window_manager.render_windows()


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