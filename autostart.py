r"""开机自启与常驻（G 模块）：让网页端在电脑重启后自己回来。

自用阶段服务端跑本机，电脑一重启就得手动 `python main.py web`——
那它就不是"一直在那儿的朋友"，是"你想起来才去开一下的程序"。

三件事：
1. **常驻** `daemon.py`：进程崩了自己重拉（隔 5 秒），看到 `web.stop` 才真停。
   计划任务只管"登录时拉一次"，管不了崩溃，所以得有这一层。
2. **开机自启**：优先注册计划任务（能查状态、能禁能删、不用管理员）；
   注册不了就兜底把启动器放进「启动」文件夹。
   ⚠️ 没用 `schtasks.exe`：某些终端里它连 `/query` 都返回
   "The system cannot find the path specified"，改走 PowerShell 的
   ScheduledTasks cmdlet（同一套东西，稳得多）。
3. **桌面快捷方式**：双击直接进聊天页，不用手敲 127.0.0.1:8787。

睡眠/休眠不用管：S0 现代待机下进程是挂起不是退出，醒来接着跑。

用法：
    python main.py autostart [on|off|status|start|stop]
网页里也能查：`/autostart`、`/autostart on|off`
（start/stop 只在终端做——在网页里把自己停掉就再也起不来了）。
"""
import os
import subprocess
import sys
from pathlib import Path

import config

TASK_NAME = "friend-ai-web"          # 计划任务名
LOG_FILE = config.BASE_DIR / "logs" / "web.log"
STOP_FLAG = config.BASE_DIR / "web.stop"
PID_FILE = config.BASE_DIR / "web.pid"        # 网页端进程（webui 自己写）
DAEMON_PID = config.BASE_DIR / "daemon.pid"   # 守护进程（daemon 自己写）

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


# --- 解释器：优先虚拟环境里的；守护用 pythonw（无窗口），服务用 python（要输出） ---
def _python() -> str:
    venv_py = config.BASE_DIR / ".venv" / "Scripts" / "python.exe"
    return str(venv_py if venv_py.exists() else Path(sys.executable))


def _pythonw() -> str:
    venv_pyw = config.BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    if venv_pyw.exists():
        return str(venv_pyw)
    return _python()      # 没有 pythonw 就退回 python（会多一个窗口，但不影响跑）


def _startup_dir() -> Path:
    """当前用户的「启动」文件夹（兜底方案用）。"""
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "Startup")


def _ps(script: str) -> subprocess.CompletedProcess:
    # 中文 Windows 的 PowerShell 输出是 GBK；errors=replace 防偶发乱码把调用搞崩
    ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                      "WindowsPowerShell", "v1.0", "powershell.exe")
    return subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, text=True, errors="replace")


# --- 计划任务（主路） ---
def _task_install():
    pyw = _pythonw()
    root = str(config.BASE_DIR)
    script = f"""
$ErrorActionPreference='Stop'
$a = New-ScheduledTaskAction -Execute '{pyw}' -Argument 'daemon.py' -WorkingDirectory '{root}'
$t = New-ScheduledTaskTrigger -AtLogOn
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
     -DontStopIfGoingOnBatteries -StartWhenAvailable `
     -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t `
     -Settings $s -Force | Out-Null
"""
    r = _ps(script)
    if r.returncode == 0:
        return True
    # 失败原因带回去，别让用户对着一句"失败"猜
    msg = (r.stderr or r.stdout or "").strip().replace("\n", " ")
    return f"{(msg[:200] or '未知错误')}（退出码 {r.returncode}）"


def _task_exists() -> bool:
    r = _ps(f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}' "
            f"-ErrorAction SilentlyContinue; if ($t) {{ 'YES' }} else {{ 'NO' }}")
    return "YES" in (r.stdout or "")


def _task_remove() -> bool:
    return _ps(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' "
               f"-Confirm:$false -ErrorAction SilentlyContinue").returncode == 0


# --- 启动文件夹（兜底）：wscript 跑一个一行启动器，无窗口 ---
def _startup_vbs() -> str:
    q = '"'      # vbs 里包路径要三个引号，直接写会撞上 Python 的三引号
    pyw = _pythonw()
    return ("' 朋友型 AI · 网页端开机启动器（由 autostart.py 生成，别手改）\n"
            'Set sh = CreateObject("WScript.Shell")\n'
            f'sh.CurrentDirectory = "{config.BASE_DIR}"\n'
            f'sh.Run {q}{q}{q}{pyw}{q}{q}{q} daemon.py", 0, False\n')


def _folder_install() -> bool:
    d = _startup_dir()
    if not d.exists():
        return False
    try:
        (d / f"{TASK_NAME}.vbs").write_text(_startup_vbs(), encoding="utf-16")
        return True
    except OSError:
        return False


def _folder_exists() -> bool:
    return (_startup_dir() / f"{TASK_NAME}.vbs").exists()


def _folder_remove() -> bool:
    try:
        p = _startup_dir() / f"{TASK_NAME}.vbs"
        if p.exists():
            p.unlink()
        return True
    except OSError:
        return False


# --- 对外动作 ---
def install() -> str:
    """注册开机自启（登录时拉起网页端）。"""
    how = None
    why = ""
    r = _task_install()
    if r is True:
        how = "计划任务（登录时触发）"
    else:
        why = r                       # 失败原因（PowerShell 的报错）
        if _folder_install():
            how = "启动文件夹（计划任务这条路在本机不通，已兜底）"
    if not how:
        return (f"❌ 两条路都没走通。\n"
                f"   计划任务：{why}\n"
                f"   启动文件夹：{_startup_dir()}\n"
                f"   兜底：手动把 {TASK_NAME}.vbs 放进「启动」文件夹也能自启\n"
                f"   现在只想先跑起来：python main.py autostart start")
    _shortcut()      # 顺手在桌面放个打开聊天页的快捷方式
    return (f"✅ 已开机自启：{how}\n"
            f"   日志：{LOG_FILE}\n"
            f"   现在就启动：python main.py autostart start\n"
            f"   关掉：python main.py autostart stop（放 stop 标志，重启才回来）")


def uninstall() -> str:
    """取消开机自启（不杀当前进程）。"""
    a = _task_remove()
    b = _folder_remove()
    if not (a or b):
        return "⚠ 没删掉东西（可能本来就没装）"
    return "✅ 已取消开机自启（当前进程还在跑，要停就 autostart stop）"


def is_installed() -> bool:
    return _task_exists() or _folder_exists()


def status() -> str:
    on = is_installed()
    running = _running_pid()
    how = ("计划任务" if _task_exists()
           else "启动文件夹" if _folder_exists() else "—")
    url = f"http://{config.WEB_HOST}:{config.WEB_PORT}"
    lines = [f"开机自启：{'✅ 已开启' if on else '❌ 未开启'}（方式：{how}）",
             f"网页端进程：{'✅ 在跑（PID ' + str(running) + f'）  →  {url}' if running else '❌ 没在跑'}",
             f"守护进程：{'✅ 在跑' if _daemon_pid() else '❌ 没在跑'}"]
    if STOP_FLAG.exists():
        lines.append("⏸ 有 stop 标志：守护不会重拉（autostart start 会清掉它）")
    if LOG_FILE.exists():
        lines.append(f"日志：{LOG_FILE}（{LOG_FILE.stat().st_size} 字节）")
    if not on:
        lines.append("开启：python main.py autostart on")
    return "\n".join(lines)


def _pid_alive(pid: int) -> bool:
    """进程还活着吗。

    ⚠️ 别用 os.kill(pid, 0) 探活——Windows 上它不等于 POSIX 的信号 0，
    CPython 会直接 TerminateProcess，等于把人杀了。这里用 OpenProcess 查。
    """
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                           False, pid)
    if not h:
        return False                      # 打不开 = 进程没了
    try:
        code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _pid_from(path: Path):
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid if _pid_alive(pid) else None


def _running_pid():
    return _pid_from(PID_FILE)


def _daemon_pid():
    return _pid_from(DAEMON_PID)


def _kill(pid) -> None:
    subprocess.run(["taskkill", "/pid", str(pid), "/t", "/f"],
                   capture_output=True, text=True, errors="replace")


def start() -> str:
    """立刻在后台拉起（不等开机）。"""
    if STOP_FLAG.exists():
        STOP_FLAG.unlink()          # 清掉停止标志，否则守护不会再拉
    if _daemon_pid():
        return f"守护已经在跑了（PID {_daemon_pid()}）；网页端：{_running_pid() or '还没起来'}"
    if not is_installed():
        install()
    # 守护用 pythonw 跑：没有控制台窗口；DETACHED 让它活得比当前终端久
    subprocess.Popen([_pythonw(), "daemon.py"], cwd=str(config.BASE_DIR),
                     creationflags=(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                                    | CREATE_NO_WINDOW),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    return f"🚀 守护已拉起，几秒后打开 http://{config.WEB_HOST}:{config.WEB_PORT}"


def stop() -> str:
    """停掉网页端，并让守护别再拉起来。"""
    STOP_FLAG.write_text("stopped\n", encoding="utf-8")
    parts = []
    for p in (_running_pid(), _daemon_pid()):
        if p:
            _kill(p)
            parts.append(str(p))
    for f in (PID_FILE, DAEMON_PID):
        try:
            if f.exists():
                f.unlink()
        except OSError:
            pass
    killed = f"（已结束 PID {'、'.join(parts)}）" if parts else ""
    return f"⏹ 已停止{killed}\n守护也不会重拉了。要恢复：python main.py autostart start"


# --- 桌面快捷方式：双击直接进聊天页 ---
def _shortcut() -> bool:
    try:
        desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
        if not desktop.exists():      # 有的机器桌面挂在 OneDrive 下
            alt = Path(os.environ.get("USERPROFILE", "")) / "OneDrive" / "Desktop"
            desktop = alt if alt.exists() else desktop
        desktop.mkdir(parents=True, exist_ok=True)
        (desktop / "朋友.url").write_text(
            "[InternetShortcut]\n"
            f"URL=http://{config.WEB_HOST}:{config.WEB_PORT}\n"
            "IconIndex=0\n", encoding="utf-8")
        return True
    except OSError:
        return False


def cli(arg: str = "status") -> str:
    arg = (arg or "status").lower()
    if arg in ("on", "install", "enable"):
        return install()
    if arg in ("off", "uninstall", "disable"):
        return uninstall()
    if arg == "start":
        return start()
    if arg == "stop":
        return stop()
    return status()
