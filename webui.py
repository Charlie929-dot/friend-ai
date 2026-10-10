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
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import commands
import config
import auth      # 必须放在 config 之后：config 导入时才会把 .env 读进环境变量
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


def _persona_tags(card: dict | None) -> list[str]:
    """人物标签：人格 label + 阅读偏好（爱读X）。旧卡没 reading_pref 就只给 label。"""
    if not card:
        return []
    tags = [card["label"]]
    pref = card.get("reading_pref")
    if pref:
        tags.append(f"爱读{pref}")
    return tags


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
    import offline as _off
    state = db.get_state()
    off = _off.is_offline()
    if state == "SLEEPING":
        presence = "睡着"
    elif state == "SLEEPY":
        # SLEEPY 两个来路：早上赖床 / 晚上快睡（实测 #26 之前不分，白天显示"快睡了"）
        presence = "赖床中" if db.get_kv("sleepy_reason") == "lie_in" else "快睡了"
    elif off:
        presence = "专注中"
    else:
        presence = "在线"
    sleep_at = _off.tonight_sleep_at(datetime.now())
    wake_at = _off.next_wake_at(datetime.now())
    return {
        "name": persona.friend_name(),
        "clock": datetime.now().strftime("%H:%M"),
        "state": state,
        "presence": presence,
        "offline": off,
        "offline_reason": _off.offline_reason() if off else "",
        "urgent": guard.in_urgent_window(),
        "guard": guard.guard_enabled(),
        "typing": channel.typing_now(),
        "work": {k: s[k] for k in ("phase", "energy", "money", "done", "quota")},
        "cost": {"level": c["level"],
                 "badge": cost.LEVEL_BADGE.get(c["level"], ""),
                 "index": c["cost_index"]},
        "mood": {"label": m["label"], "tone": m["tone"]},
        "materials": fresh,
        "schedule": f"今晚 {sleep_at:%H:%M} 睡 · 下次 {wake_at:%H:%M} 起",
        "stage": "初见" if db.get_kv("relationship_stage", "new") == "new" else "熟悉",
        "persona": {"desc": card["text"] if card else persona.DEFAULT_FLAVOR,
                    "tags": _persona_tags(card)},
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

        def _redirect(self, to: str):
            self.send_response(302)
            self.send_header("Location", to)
            self.send_header("Content-Length", "0")
            self.end_headers()

        # --- 登录（公网部署时才启用；本机 WEB_AUTH_ENABLED 默认 off，不受影响）---
        def _do_login(self):
            ip = self.client_address[0]
            ctype = self.headers.get("Content-Type") or ""
            # 登录页是纯 HTML 表单；前端 fetch 才是 JSON。两种返回形式不同：
            # 表单 -> 成功 303 跳转 / 失败直接重渲染登录页；JSON -> 保持 JSON 语义。
            wants_json = ("json" in ctype) or (
                "json" in (self.headers.get("Accept") or ""))
            if auth.locked(ip):
                msg = "试错太多次，10 分钟后再来"
                if wants_json:
                    self._json({"error": msg}, 429)
                else:
                    self._send_body(auth.login_page(msg).encode("utf-8"),
                                    "text/html; charset=utf-8")
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            raw = self.rfile.read(n) if n > 0 else b""
            data = {}
            if "json" in ctype:
                try:
                    data = json.loads(raw or b"{}")
                except ValueError:
                    data = {}
            else:   # 登录页用的是普通表单
                data = {k: v[0] for k, v in
                        parse_qs(raw.decode("utf-8", "replace")).items()}
            # 容错：复制粘贴常带首尾空格/换行；验证器 App 有时显示成 "123 456"
            pw = (data.get("password") or "").strip()
            code = re.sub(r"\D", "", data.get("totp") or "")
            if not auth.verify_password(pw):
                auth.note_failure(ip)
                msg = "密码不对（注意大小写，别带空格）"
            elif auth.totp_enabled() and not auth.verify_totp(code):
                auth.note_failure(ip)
                msg = "动态口令不对（检查手机时间是否自动同步）"
            else:
                msg = ""
            if msg:
                if wants_json:
                    self._json({"error": msg}, 401)
                else:
                    self._send_body(auth.login_page(msg).encode("utf-8"),
                                    "text/html; charset=utf-8")
                return

            auth.note_success(ip)
            remember = str(data.get("remember", "")).lower() in ("1", "on", "true")
            days = 30 if remember else 1
            if wants_json:
                body = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
            else:
                # 303：让浏览器把 POST 转成 GET，否则表单提交后会停在空白/JSON 页
                body = b""
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
            self.send_header("Set-Cookie", auth.cookie_header(days))
            self.end_headers()
            if body:
                self.wfile.write(body)

        # --- 页面 ---
        def do_GET(self):
            u = urlparse(self.path)
            if auth.enabled():
                ok = auth.check_cookie(self.headers.get("Cookie", ""))
                if u.path == "/login":
                    if ok:
                        self._redirect("/")
                    else:
                        self._send_body(auth.login_page().encode("utf-8"),
                                        "text/html; charset=utf-8")
                    return
                if not ok:
                    # 接口返回 401（前端会跳登录页），页面直接 302 过去
                    if u.path.startswith("/api/"):
                        self._json({"error": "未登录"}, 401)
                    else:
                        self._redirect("/login")
                    return
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
            elif u.path == "/account":
                # 账号管理页（改密码 / 重绑 TOTP）
                self._serve_web_file("account.html", "text/html; charset=utf-8")
            elif u.path == "/qr.js":
                # 二维码生成库（内嵌的开源 qrcode-generator，MIT；TOTP 绑定扫码用）
                self._serve_web_file("qr.js", "application/javascript; charset=utf-8")
            elif u.path == "/api/account/status":
                self._json({"totp_enabled": auth.totp_enabled()})
            else:
                self._json({"error": "not found"}, 404)

        def _serve_web_file(self, name: str, ctype: str):
            try:
                body = (WEB_DIR / name).read_text(encoding="utf-8")
            except OSError as e:
                self._json({"error": f"读不到 web/{name}：{e}"}, 500)
                return
            self._send_body(body.encode("utf-8"), ctype)

        # --- 发消息 / 执行命令 / 账号管理 ---
        def do_POST(self):
            u = urlparse(self.path)
            if auth.enabled() and u.path == "/api/login":
                self._do_login()
                return
            if auth.enabled() and not auth.check_cookie(self.headers.get("Cookie", "")):
                self._json({"error": "未登录"}, 401)
                return
            if u.path.startswith("/api/account/"):
                self._do_account(u.path)
                return
            if u.path != "/api/send":
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
            with db.SEND_LOCK:      # 只与切库互斥；不等回复生成（否则消息全卡到 TA 回完，实测 #30）
                out = on_message(text)
            is_cmd = isinstance(out, str)   # 命令：输出走返回值；普通消息：out 是消息 id
            self._json({
                "echo": not is_cmd,   # 普通消息回显用户输入；命令只把输出显示出来
                "reply": out if is_cmd else "",
                # 回显这条消息的 id：前端把轮询游标 lastId 推上去，这条就不会被
                # 轮询再渲染一遍（"发送出现两次"的老 bug，实测 #25）
                "id": out if isinstance(out, int) else 0,
                "ts": datetime.now().isoformat(timespec="seconds"),
            })

        # --- 账号管理（改密码 / 重绑 TOTP）：改完写回 .env 并热生效，不用重启 ---
        def _read_json(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def _verify_credentials(self, data: dict) -> str:
            """敏感操作前的身份复核：密码必验；开了 TOTP 还要当前动态码。
            复用登录的限流（同一 IP 试错 5 次锁 10 分钟）。返回错误信息，空串=通过。"""
            ip = self.client_address[0]
            if auth.locked(ip):
                return "试错太多次，10 分钟后再来"
            pw = (data.get("password") or "").strip()
            if not auth.verify_password(pw):
                auth.note_failure(ip)
                return "密码不对"
            if auth.totp_enabled():
                code = re.sub(r"\D", "", data.get("totp") or "")
                if not auth.verify_totp(code):
                    auth.note_failure(ip)
                    return "动态口令不对（检查手机时间是否自动同步）"
            auth.note_success(ip)
            return ""

        def _do_account(self, path: str):
            data = self._read_json()
            if path == "/api/account/password":
                err = self._verify_credentials(data)
                if err:
                    self._json({"error": err}, 401)
                    return
                new = (data.get("new") or "").strip()
                if len(new) < 8:
                    self._json({"error": "新密码至少 8 位"})
                    return
                if new == (data.get("password") or "").strip():
                    self._json({"error": "新密码不能和旧的一样"})
                    return
                auth.change_password(new)
                auth.rotate_session_secret()   # 旧会话全部失效，需要重新登录
                self._json({"ok": True, "relogin": True})
            elif path == "/api/account/totp/setup":
                # 生成新密钥（还没生效）：前端展示二维码 + 密钥，用户扫码后带码 confirm
                err = self._verify_credentials(data)
                if err:
                    self._json({"error": err}, 401)
                    return
                secret = auth.new_totp_secret()
                self._json({"ok": True, "secret": secret,
                            "uri": auth.otpauth_uri(secret)})
            elif path == "/api/account/totp/confirm":
                # 用户已扫码、输对了新密钥的动态码 → 正式启用
                err = self._verify_credentials(data)
                if err:
                    self._json({"error": err}, 401)
                    return
                secret = (data.get("secret") or "").strip().replace(" ", "").upper()
                code = re.sub(r"\D", "", data.get("code") or "")
                if not secret or not code:
                    self._json({"error": "参数不全"})
                    return
                if not auth.verify_totp(code, secret):
                    self._json({"error": "动态口令不对，确认手机时间自动同步后重试"})
                    return
                auth.activate_totp(secret)
                self._json({"ok": True})
            elif path == "/api/account/totp/disable":
                err = self._verify_credentials(data)
                if err:
                    self._json({"error": err}, 401)
                    return
                auth.disable_totp()
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)

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
