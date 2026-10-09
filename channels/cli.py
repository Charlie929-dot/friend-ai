"""CLI 通道：本地命令行调试。

输入框固定在屏幕底部：朋友 / 主动消息从上方滚动，不会把正在输入的框顶乱。
- 逐字符读取输入（Windows msvcrt / Unix termios），维护自己的输入缓冲。
- 消息到达时：清当前输入行 → 打印消息 → 重绘输入行（输入框始终在底部）。
- 非 TTY / 无 ANSI 环境（测试、重定向、老终端）自动降级为普通 input() 行为。
"""
import sys
import threading
from datetime import datetime

from channels.base import Channel


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _friend_name() -> str:
    """聊天界面显示朋友自己的名字：人格卡名 > 别名（初遇朋友这类老朋友的）> 老朋友。"""
    try:
        import persona_pool
        card = persona_pool.load()
        if card:
            return card["name"]
        import db
        alias = db.get_kv("alias")
        if alias:
            return alias
    except Exception:
        pass
    return "老朋友"


def _enable_vt() -> bool:
    """Windows 上启用 ANSI 转义（ENABLE_VIRTUAL_TERMINAL_PROCESSING）。"""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        k.SetConsoleMode(h, mode.value | 0x0004)
        return True
    except Exception:
        return False


def _is_tty() -> bool:
    return (getattr(sys.stdout, "isatty", lambda: False)()
            and getattr(sys.stdin, "isatty", lambda: False)())


def _getch() -> str:
    """读一个字符（不回显）。Windows msvcrt / Unix termios raw。"""
    if sys.platform == "win32":
        import msvcrt
        return msvcrt.getwch()
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


class CliChannel(Channel):
    def __init__(self):
        self._buf = ""                    # 当前正在输入的内容
        self._lock = threading.RLock()    # 保护 stdout 写入序列 + 输入缓冲
        self._ansi = _enable_vt()

    @property
    def _interactive(self) -> bool:
        """是否启用底部固定输入区（真实终端 + 支持 ANSI）。"""
        return _is_tty() and self._ansi

    # --- 输出：消息从上方滚动，输入框固定在底部 ---
    def _redraw(self) -> None:
        """重绘底部输入行："> " + 当前缓冲。"""
        with self._lock:
            sys.stdout.write("\r\x1b[2K> " + self._buf)
            sys.stdout.flush()

    def _emit_line(self, line: str) -> None:
        """打印一条消息行：清输入行 → 打印消息 → 重绘输入行。"""
        with self._lock:
            sys.stdout.write("\x1b[2K" + line + "\n")
            self._redraw()

    def send(self, text: str) -> None:
        name = _friend_name()
        if not self._interactive:
            print(f"\n[{_ts()} {name}] {text}")
            return
        self._emit_line(f"[{_ts()} {name}] {text}")

    def typing(self) -> None:
        name = _friend_name()
        if not self._interactive:
            print(f"\n[{_ts()} {name}] 正在输入…", end="", flush=True)
            return
        self._emit_line(f"[{_ts()} {name}] 正在输入…")

    # --- 输入循环 ---
    def run(self, on_message) -> None:
        print("CLI 模式启动，输入消息回车发送，Ctrl+C 退出。")
        if not self._interactive:
            self._run_fallback(on_message)
            return
        self._redraw()
        while True:
            ch = _getch()
            if ch in ("\r", "\n"):           # 回车：提交
                line = self._buf
                self._buf = ""
                self._emit_line(f"[{_ts()} 你] {line}")
                if line.strip():
                    on_message(line)
            elif ch in ("\x03", "\x04"):     # Ctrl+C / Ctrl+D：退出
                sys.stdout.write("\n退出。\n")
                break
            elif ch in ("\x08", "\x7f"):     # 退格
                if self._buf:
                    self._buf = self._buf[:-1]
                    self._redraw()
            elif ch in ("\xe0", "\x00"):     # Windows 功能键前缀（方向键等）
                _getch()                      # 吞后续键码
            elif ch == "\x1b":               # ESC / 转义序列
                self._swallow_escape()
            elif ch and (ch.isprintable() or ch == " "):
                self._buf += ch
                self._redraw()

    def _swallow_escape(self) -> None:
        """吞掉 ESC 开头的转义序列（方向键等），避免塞进输入缓冲。"""
        try:
            if sys.platform == "win32":
                import msvcrt
                if msvcrt.kbhit():
                    nxt = _getch()
                    if nxt == "[" and msvcrt.kbhit():
                        _getch()
                return
            nxt = _getch()
            if nxt == "[":
                _getch()
        except Exception:
            pass

    def _run_fallback(self, on_message) -> None:
        """非交互降级：普通 input()（测试 / 重定向 / 无 ANSI 时）。"""
        print("> ", end="", flush=True)
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                print("\n退出。")
                break
            print(f"[{_ts()} 你]", flush=True)
            on_message(line)
            print("> ", end="", flush=True)
