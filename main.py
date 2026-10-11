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
        self.height = y2 - y1
        self.width = x2 - x1
        self.y1 = y1
        self.x1 = x1
        self.y2 = y2
        self.x2 = x2
        self.title = title

        self.window = curses.newwin(self.height, self.width, self.y1, self.x1)
        self.refresh_window()

    def refresh_window(self) -> None:
        self.window.erase()
        self.window.box()
        self.print_title(curses.color_pair(2))
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

    def render_time(self) -> None:
        if self.isDate:
            pattern = "%d.%m.%Y"
        else:
            pattern = "%H:%M:%S"

        current_time = datetime.now().strftime(pattern)
        self.window.erase()
        self.window.box()
        self.print_title(curses.color_pair(2))

        try:
            text_x = max(1, (self.width - len(current_time)) // 2)
            self.window.addstr(1, text_x, current_time, curses.A_BOLD)
        except curses.error:
            pass

        self.window.refresh()


class WindowManager:
    def __init__(self) -> None:
        self.windows: dict[str, Window] = {}
        self.active_window_name: str | None = None

    def add_window(self, name: str, window: Window) -> None:
        if name in self.windows:
            raise ValueError(f"Window already exists: {name}")
        
        self.windows[name] = window
        
        if self.active_window_name is None:
            self.active_window_name = name

    def get_window(self, name: str) -> Window:
        return self.windows[name]

    def get_active_window(self) -> Window:
        if self.active_window_name is None:
            raise RuntimeError("No windows have been registered")
        
        return self.windows[self.active_window_name]

    def switch_active_window(self) -> None:
        names = list(self.windows)
        
        if names:
            active_index = names.index(self.active_window_name)
            self.active_window_name = names[(active_index + 1) % len(names)]

    def handle_input(self) -> bool:
        key = self.get_active_window().get_window().getch()
        
        if key == 27:
            return False
        
        if key == 9:
            self.switch_active_window()
        
        return True

def display_func(stdscr: curses.window) -> None:
    try:
        stdscr.addstr(0, 1, "Left panel    Settings    Right panel")

        row = curses.LINES - 2
        stdscr.attron(curses.A_REVERSE)
        for column, label in ((1, "Tab"), (20, "F1"), (30, "F5"), (40, "F8")):
            stdscr.addstr(row, column, label)
        stdscr.attroff(curses.A_REVERSE)

        for column, label in (
            (4, " - Switch panel"),
            (22, " - Quit"),
            (32, " - Copy"),
            (42, " - Delete"),
        ):
            stdscr.addstr(row, column, label)
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
    window_manager.add_window(
        "Clock",
        DateTimeWindow(0, 2, int(columns * 0.125), 5, "Clock"),
    )
    window_manager.add_window(
        "Date",
        DateTimeWindow(int(columns * 0.125), 2, int(columns * 0.25), 5, "Date", True),
    )
    window_manager.add_window(
        "Files", 
        Window(0, int(lines * 0.125), int(columns * 0.25), lines - 3, path)
        )
    window_manager.add_window(
        "Terminal",
        Window(int(columns * 0.25), 2, int(columns * 0.75), lines - 3, "Terminal"),
    )
    window_manager.add_window(
        "System Info",
        Window(int(columns * 0.75), 2, columns, lines // 2, "System info"),
    )
    window_manager.add_window(
        "Network Info",
        Window(int(columns * 0.75), lines // 2, columns, lines - 3, "Network info"),
    )

    while window_manager.handle_input():
        for window in window_manager.windows.values():
            if isinstance(window, DateTimeWindow):
                window.render_time()
            elif isinstance(window, DateWindow):
                window.render_date()


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