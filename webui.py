"""网页端 Web UI（G 模块 S3 核心）：自用阶段的主载体——浏览器打开就能聊。

设计要点：
- **与 CLI 同一份数据**：服务端常驻本机，读写同一个 `friend-<id>.db`，记忆/日记/
  工作系统/调度器全部照常跑。网页只是另一块屏幕，不是另一个 AI。
- **消息靠增量轮询**：`core` 把消息写进 db（含它主动发的），前端每 900ms 拉一次
  `after=lastId`。这样"它自己发起的问候/分享"也能自然出现在对话流里。
- **HUD 常驻顶部**（参考原型的布局：状态 + 时钟 / 精力·进度双条 / 余额·配额·素材）：
  看得见它的日子，才知道什么时候找它会打扰它。
- 服务端只依赖标准库，不引第三方 Web 框架（自用阶段少一个依赖少一件事）。
"""
import json
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import commands
import config
import db
import guard
import persona
import work
from channels.base import Channel

WEB_DIR = config.BASE_DIR / "web"


class WebChannel(Channel):
    """网页通道：消息入库由 core 负责，这里只把"正在输入"状态暴露给前端。"""

    def __init__(self):
        self.typing_until = 0.0

    def send(self, text: str) -> None:
        pass  # core 已写 db；前端靠 /api/messages 增量拉取，无需再推

    def typing(self) -> None:
        # core 紧接着自己 sleep 打字时长，这里给个略长的窗口，避免一闪而过
        self.typing_until = time.time() + 8

    def typing_now(self) -> bool:
        return time.time() < self.typing_until

    def run(self, on_message) -> None:
        """启动 HTTP 服务并阻塞（调度器已在 run 之前启动）。

        on_message 的返回值是斜杠命令的输出（普通消息返回 None）。"""
        try:
            server = ThreadingHTTPServer((config.WEB_HOST, config.WEB_PORT),
                                         _make_handler(self, on_message))
        except OSError as e:
            print(f"[网页端] 起不来：{config.WEB_HOST}:{config.WEB_PORT} —— {e}\n"
                  f"          多半是已经有一个在跑了（python main.py autostart status）")
            return
        _write_pid()      # 让 autostart 能查到/停掉这个进程
        print(f"[网页端] http://{config.WEB_HOST}:{config.WEB_PORT}  "
              f"（Ctrl+C 退出；CLI 调试用 python main.py cli）")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[网页端] 已停止")
        finally:
            _clear_pid()
            server.server_close()


# --- PID 文件：给 autostart 用（查进程 / 停服务） ---
def _write_pid() -> None:
    try:
        import autostart
        autostart.PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except (ImportError, OSError):
        pass


def _clear_pid() -> None:
    try:
        import autostart
        if autostart.PID_FILE.exists():
            autostart.PID_FILE.unlink()
    except (ImportError, OSError):
        pass


# --- 状态快照：HUD + 朋友档案 ---
def _state_payload(channel: WebChannel) -> dict:
    s = work.status()
    import cost
    c = cost.state()
    import mood
    m = mood.mood()
    import persona_pool
    card = persona_pool.load()
    p = persona.behavior_params()
    fresh = len([x for x in work._shareables() if not x.get("used")])
    return {
        "name": persona.friend_name(),
        "clock": datetime.now().strftime("%H:%M"),
        "state": db.get_state(),
        "urgent": guard.in_urgent_window(),
        "guard": guard.guard_enabled(),
        "typing": channel.typing_now(),
        "work": {k: s[k] for k in ("phase", "energy", "money", "done", "quota")},
        "cost": {"level": c["level"],
                 "badge": cost.LEVEL_BADGE.get(c["level"], ""),
                 "index": c["cost_index"]},
        "mood": {"label": m["label"], "tone": m["tone"]},
        "materials": fresh,
        "schedule": f"{p['wake_hour']}:00 起 · {p['sleep_hour']}:00 睡",
        "stage": "初见" if db.get_kv("relationship_stage", "new") == "new" else "熟悉",
        "persona": {"desc": card["text"] if card else persona.DEFAULT_FLAVOR,
                    "tags": ([card["label"]] if card else [])},
    }


def _make_handler(channel: WebChannel, on_message):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # 轮询很频繁，别刷屏

        def _send_body(self, body: bytes, ctype: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200):
            self._send_body(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                            "application/json; charset=utf-8", code)

        # --- 页面 ---
        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                try:
                    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
                except OSError as e:
                    self._json({"error": f"读不到 web/index.html：{e}"}, 500)
                    return
                self._send_body(html.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/state":
                self._json(_state_payload(channel))
            elif u.path == "/api/friends":
                # 网页端不做"输 /friend 3"这种事：列表点一下就换人。
                # 编号必须跟 /friend <编号> 一致（唯一真相是 commands.list_friends）
                import commands as _cmd
                self._json({"current": persona.friend_name(),
                            "friends": _cmd.list_friends()})
            elif u.path == "/api/messages":
                try:
                    after = int(parse_qs(u.query).get("after", ["0"])[0])
                except ValueError:
                    after = 0
                rows = db.messages_page(after, 200)
                self._json({"messages": rows,
                            "last_id": rows[-1]["id"] if rows else after})
            else:
                self._json({"error": "not found"}, 404)

        # --- 发消息 / 执行命令 ---
        def do_POST(self):
            if urlparse(self.path).path != "/api/send":
                self._json({"error": "not found"}, 404)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                self._json({"error": "请求不是合法 JSON"}, 400)
                return
            text = (data.get("text") or "").strip()
            if not text:
                self._json({"error": "消息是空的"}, 400)
                return
            # 手动安全阀：勾选"紧急"直接开窗口（也可以直接说"有急事"，它自己认）
            if data.get("urgent"):
                guard.open_urgent_window()
            # 重启服务：改了后端代码后点一下，守护会自动拉起新进程，不用手动去终端
            if text == "/restart":
                self._restart()
                return
            with db.PATH_LOCK:      # 与切换朋友/回复流程互斥，别写错库
                out = on_message(text)
            self._json({
                "echo": out is None,   # 命令不回显用户输入，只把输出显示出来
                "reply": out or "",
                "ts": datetime.now().isoformat(timespec="seconds"),
            })

        def _restart(self):
            import autostart
            if not autostart._daemon_pid():
                self._json({"echo": False, "reply":
                            "没有守护在跑，重启就再也起不来了。请先在终端 "
                            "python main.py autostart start。", "ts": ""})
                return
            # 先回浏览器，稍等再退出——守护看到进程退出、5 秒后重拉新代码
            self._json({"echo": False, "reply": "正在重启，几秒后回来…", "ts": ""})
            t = threading.Timer(0.4, lambda: os._exit(0))
            t.daemon = True
            t.start()

    return Handler
