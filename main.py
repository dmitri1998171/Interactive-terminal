import curses
import os
from datetime import datetime
from typing import TextIO

ARR_SIZE = 1024
DEBUG = True
LOG_FILE: TextIO | None = None


def log_info(value: object) -> None:
    if DEBUG and LOG_FILE is not None:
        LOG_FILE.write(f"LOG_INFO, {value}\n")


def color_num(fg: int, bg: int) -> int:
    return 0x80 | ((7 & bg) << 4) | (7 & fg)


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
            self.window.addstr(1, text_x, current_time, curses.A_BOLD)
        except curses.error:
            pass

        self.window.refresh()


class TerminalWindow(Window):
    def __init__(self, x1: int, y1: int, x2: int, y2: int, title: str) -> None:
        super().__init__(x1, y1, x2, y2, title)
        self.content_lines: list[str] = []
        self.scroll_offset = 0
        self.render()

    def set_content(self, text: str) -> None:
        self.content_lines = text.splitlines() or [""]
        self.scroll_offset = 0
        self.render()

    def scroll_up(self, lines: int = 3) -> None:
        self.scroll_offset = max(0, self.scroll_offset - lines)
        self.render()

    def scroll_down(self, lines: int = 3) -> None:
        max_offset = max(0, len(self.content_lines) - max(1, self.height - 3))
        self.scroll_offset = min(max_offset, self.scroll_offset + lines)
        self.render()

    def render(self, active: bool = False) -> None:
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
            if offset >= visible_rows:
                break
            text = line[: max(0, self.width - 4)]
            try:
                self.window.addstr(1 + offset, 2, text)
            except curses.error:
                pass

        self.window.refresh()

    def handle_key(self, key: int) -> None:
        if key in (curses.KEY_UP, 259):
            self.scroll_up(3)
        elif key in (curses.KEY_DOWN, 258):
            self.scroll_down(3)


class FileManagerWindow(Window):
    def __init__(self, x1: int, y1: int, x2: int, y2: int, title: str, start_path: str, terminal_window: TerminalWindow | None = None) -> None:
        super().__init__(x1, y1, x2, y2, title)
        self.current_path = start_path
        self.selected_index = 0
        self.entries: list[tuple[str, str]] = []
        self.terminal_window = terminal_window
        self.refresh_content()

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

    def handle_key(self, key: int) -> None:
        if key in (curses.KEY_UP, 259, 65):
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
                try:
                    with open(file_path, "r", encoding="utf-8") as file:
                        text = file.read()
                except UnicodeDecodeError:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as file:
                        text = file.read()
                except OSError:
                    text = f"Unable to read file: {file_path}"
                self.terminal_window.set_content(text)
        elif key in (8, 127, ord("b")):
            parent = os.path.dirname(self.current_path)
            if parent and self.current_path != parent:
                self.current_path = parent
                self.selected_index = 0
                self.refresh_content()

        self.render()


class WindowManager:
    NON_SWITCHABLE_WINDOWS = {"Clock", "Date", "System info", "Network info"}

    def __init__(self) -> None:
        self.windows: dict[str, Window] = {}
        self.active_window_name: str | None = None

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

    def handle_input(self) -> bool:
        active_window = self.get_active_window()
        key = active_window.get_window().getch()

        if key == 27:   # Escape key
            return False

        if key == 9:    # Tab key
            self.switch_active_window()
            return True

        if key == curses.KEY_BTAB:
            self.switch_active_window(reverse=True)
            return True

        if isinstance(active_window, TerminalWindow):
            active_window.handle_key(key)
            self.render_windows()
            return True

        if isinstance(active_window, FileManagerWindow):
            active_window.handle_key(key)

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
                (50, "F1", "Копировать"),
                (65, "F5", "Удалить"),
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

    terminal_window = TerminalWindow(int(columns * 0.25), 2, int(columns * 0.75), lines - 3, "Terminal")
    window_manager = WindowManager()
    window_manager.add_window(DateTimeWindow(0, 2, int(columns * 0.125), 5, "Clock"))
    window_manager.add_window(DateTimeWindow(int(columns * 0.125), 2, int(columns * 0.25), 5, "Date", True))
    window_manager.add_window(FileManagerWindow(0, int(lines * 0.125), int(columns * 0.25), lines - 3, "Files", path, terminal_window))
    window_manager.add_window(terminal_window)
    window_manager.add_window(Window(int(columns * 0.75), 2, columns, lines // 2, "System info"))
    window_manager.add_window(Window(int(columns * 0.75), lines // 2, columns, lines - 3, "Network info"))

    window_manager.active_window_name = "Files"
    window_manager.render_windows()

    while window_manager.handle_input():
        window_manager.render_windows()


def main() -> None:
    global LOG_FILE

    if DEBUG:
        with open("log.log", "w", encoding="utf-8") as log_file:
            LOG_FILE = log_file
            curses.wrapper(run)
    else:
        curses.wrapper(run)


if __name__ == "__main__":
    main()