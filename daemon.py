"""网页端常驻守护：它得一直在那儿。

为什么单独一个进程：计划任务只管"登录时拉一次"，进程真崩了它不管。
这个守护盯着 `main.py web`，退了就等 5 秒再拉——除非看到 `web.stop`
标志（否则你永远关不掉它：kill 一次它起来一次）。

用 `pythonw.exe` 跑它（无控制台窗口），它再用 CREATE_NO_WINDOW 拉起
`main.py web`，所以整条链子上都不会有黑框闪来闪去。

    pythonw.exe daemon.py          # 一般不用手动跑，autostart start 会拉它
"""
import subprocess
import sys
import time
from pathlib import Path

import config

LOG_FILE = config.BASE_DIR / "logs" / "web.log"
STOP_FLAG = config.BASE_DIR / "web.stop"
DAEMON_PID = config.BASE_DIR / "daemon.pid"

CREATE_NO_WINDOW = 0x08000000       # 子进程不创建控制台窗口
RESTART_DELAY = 5                   # 秒：进程退了隔多久再拉


def _python() -> str:
    venv_py = config.BASE_DIR / ".venv" / "Scripts" / "python.exe"
    return str(venv_py if venv_py.exists() else Path(sys.executable))


def loop() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    DAEMON_PID.write_text(str(__import__("os").getpid()), encoding="utf-8")
    log = open(LOG_FILE, "a", encoding="utf-8", errors="replace")

    def _stamp(msg: str) -> None:
        log.write(f"[daemon {time.strftime('%m-%d %H:%M:%S')}] {msg}\n")
        log.flush()

    _stamp("守护启动")
    while True:
        if STOP_FLAG.exists():
            _stamp("看到 stop 标志，退出")
            break
        try:
            p = subprocess.Popen([_python(), "main.py", "web"],
                                 cwd=str(config.BASE_DIR),
                                 stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            _stamp(f"拉不起来：{e}")
            time.sleep(RESTART_DELAY)
            continue
        rc = p.wait()
        if STOP_FLAG.exists():
            _stamp("看到 stop 标志，退出")
            break
        _stamp(f"网页端退出（rc={rc}），{RESTART_DELAY} 秒后重新拉起")
        time.sleep(RESTART_DELAY)
    try:
        DAEMON_PID.unlink()
    except OSError:
        pass


if __name__ == "__main__":
    loop()
