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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import commands
import config
import auth      # 必须放在 config 之后：config 导入时才会把 .env 读进环境变量
import db
import guard
import persona
import life
from channels.base import Channel

WEB_DIR = config.BASE_DIR / "web"


class WebChannel(Channel):
    """网页通道：消息入库由 core 负责，这里只把"正在输入"状态暴露给前端。"""

    def __init__(self):
        self.generating = False

    def send(self, text: str) -> None:
        pass  # core 已写 db；前端靠 /api/messages 增量拉取，无需再推

    def set_generating(self, active: bool) -> None:
        # 生成中 = 从进入回复流程到全部段落发完（v0.75 起由 core 开关）。
        # 这就是"正在输入"的准确窗口，不再像旧实现那样叠加一个 +8 秒的假窗口
        # （回复已经到了气泡还挂着）。typing() 不覆写：WebChannel 不需要它。
        self.generating = active

    def typing_now(self) -> bool:
        return self.generating

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
    """人物标签：人格 label + 阅读偏好（分类 · 细标签）。旧卡没 reading_pref 就只给 label。"""
    if not card:
        return []
    tags = [card["label"]]
    pref = card.get("reading_pref")
    if pref:
        sub = card.get("reading_tags") or []
        tags.append(f"爱读{pref}" + (f"·{'/'.join(sub[:2])}" if sub else ""))
    return tags


# --- 状态快照：HUD + 朋友档案 ---
def _state_payload(channel: WebChannel) -> dict:
    s = life.status()
    import cost
    c = cost.state()
    import mood
    m = mood.mood()
    import persona_pool
    card = persona_pool.load()
    p = persona.behavior_params()
    fresh = len([x for x in life._shareables() if not x.get("used")])
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
        # v0.89 §5.2：HUD 契约换成「在读哪本 / 进度% / 章节 / 经验」（余额与"第几件"退役）
        "work": {k: s[k] for k in ("phase", "energy", "exp", "progress",
                                   "chapter", "total_chapters", "book_title", "overtime")},
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
                qs = parse_qs(u.query)
                # 进页面定位到最后：latest=1 返回最近一批（正序）+ 总数；
                # 往上翻历史：before=<id> 返回更早一批（正序）；增量轮询走 after。
                if qs.get("latest", ["0"])[0] in ("1", "true"):
                    try:
                        limit = int(qs.get("limit", ["500"])[0])
                    except ValueError:
                        limit = 500
                    rows = db.messages_latest(limit)
                    self._json({"messages": rows, "total": db.messages_count(),
                                "has_more": db.messages_count() > len(rows),
                                "last_id": rows[-1]["id"] if rows else 0})
                elif "before" in qs:
                    try:
                        before = int(qs["before"][0])
                    except ValueError:
                        before = 0
                    rows = db.messages_before(before, 200)
                    self._json({"messages": rows,
                                "has_more": bool(rows) and rows[0]["id"] > 1})
                else:
                    try:
                        after = int(qs.get("after", ["0"])[0])
                    except ValueError:
                        after = 0
                    rows = db.messages_page(after, 200)
                    self._json({"messages": rows,
                                "last_id": rows[-1]["id"] if rows else after})
            elif u.path == "/api/memories":
                # 「最近记住的事」卡片（右侧洞察栏）：TA 记住的事，最近几条 + 总数。
                # 排除自我记忆（self_*，那是"我说过的话"，归意识流面板）和待删除项。
                rows = [m for m in db.all_memories(limit=200)
                        if m.get("status", "ok") == "ok"
                        and not str(m.get("tags", "")).startswith("self_")]
                recent = [{"content": m["content"],
                           "category": m.get("category", ""),
                           "theme": m.get("theme", ""),
                           "created_at": m.get("created_at", "")}
                          for m in rows[:4]]
                self._json({"total": len(rows), "recent": recent})
            elif u.path == "/api/memories/all":
                # 记忆管理（v0.105）：A 库自动记忆 + B 库长文 + 事实便签，全量带 id，
                # 给调试面板「管理记忆」做删除 / 确认 AI 提议删除 / 保留用。
                # 排除自我记忆（self_*，那是"我说过的话"，归意识流面板）。
                mems = [dict(m) for m in db.all_memories(limit=200)
                        if not str(m.get("tags", "")).startswith("self_")]
                longs = [dict(m) for m in db.all_long_memories(limit=200)]
                facts = [{"id": i + 1, "content": f}
                         for i, f in enumerate(db.get_facts())]
                self._json({"mems": mems, "longs": longs, "facts": facts})
            elif u.path == "/api/books":
                # 书架（S4 用户书库）：分类清单 + 全部书 + 阅读进度 + TA 当前口味。
                # 分类在后端映射表里（books/categories.json），不靠建子目录。
                import library as _lib
                p = persona.behavior_params()
                try:
                    # 「在读哪本」由 life 管（v0.91：library.progress 已随跳读退役）
                    self._json({"categories": _lib.categories(), "books": _lib.books(),
                                "progress": {"book_id": life.reading_book()},
                                "pref": {"category": p.get("reading_pref", ""),
                                         "tags": p.get("reading_tags") or []}})
                except Exception as e:
                    self._json({"error": f"书架读不出来：{e}"}, 500)
            elif u.path == "/api/books/cover":
                # 书封（v0.78）：epub 里解析出的封面图。只认书架里的书 id，防穿越。
                import library as _lib
                bid = unquote(parse_qs(u.query).get("id", [""])[0])
                got = _lib.cover_bytes(bid)
                if not got:
                    self._json({"error": "没有封面"}, 404)
                    return
                self._send_body(got[0], got[1])
            elif u.path == "/api/emoji":
                # 表情包池（E 模块）：分桶表情库存，前端「表情」面板渲染用。
                import emoji_pool
                self._json({"buckets": emoji_pool.payload()})
            elif u.path == "/api/emoji_img":
                # 表情图（v0.70）：按文件名吐 emoji_images/ 里的图，防穿越只认纯文件名。
                import emoji_pool
                name = unquote(parse_qs(u.query).get("name", [""])[0])
                p = emoji_pool.img_file(name)
                if not p:
                    self._json({"error": "没有这张表情图"}, 404)
                    return
                self._send_body(p.read_bytes(), emoji_pool.img_type(p.name))
            elif u.path == "/api/stream":
                # 意识流（S4「完全自我意识」）：TA 脑子里那条第一人称自我叙事。
                # 网页端原本看不到，这里吐出来给「查看意识流」面板用。
                qs = parse_qs(u.query)
                day = qs.get("day", [None])[0]
                if not day:
                    day = persona._life_day(datetime.now())[0].strftime("%Y-%m-%d")
                try:
                    limit = int(qs.get("limit", ["0"])[0])
                except ValueError:
                    limit = 0
                rows = db.stream_for_day(day)
                if limit:
                    rows = rows[-limit:]
                self._json({"day": day, "count": len(rows),
                            "stream": [{"ts": r["ts"], "hhmm": r["ts"][11:16],
                                        "kind": r["kind"], "text": r["text"]}
                                       for r in rows]})
            elif u.path == "/api/llm/config":
                # 模型设置（v0.80 / v0.102）：当前全局配置 + 官方可用模型列表（开面板时
                # 拉一次，上新模型自动进下拉）+ 供应商声明（思考强度档位按供应商动态渲染）。
                import llm as _llm
                self._json({"config": _llm.current_config(),
                            "models": _llm.list_models(),
                            "providers": _llm.providers()})
            elif u.path == "/api/tuning/config":
                # 调优参数面板（v0.86）：全项目实测优化参数的清单 + 当前值，前端渲染
                # L1 首屏 / L2 高级折叠。读用，改走 POST。
                import tuning as _tuning
                self._json({"groups": _tuning.catalog()})
            elif u.path == "/api/sources":
                # 信息获取（v0.94 批 2）：订阅源列表 + 今天刷到的。改走 POST。
                import sources as _src
                self._json({"enabled": bool(config.BROWSE_ENABLED),
                            "sources": _src.load_sources(),
                            "items": _src.items_today(limit=20)})
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
            if u.path == "/api/revoke":
                self._do_revoke()
                return
            if u.path == "/api/memories/delete":
                self._do_memory_delete()
                return
            if u.path == "/api/memories/long/delete":
                self._do_long_memory_delete()
                return
            if u.path == "/api/memories/fact/delete":
                self._do_fact_delete()
                return
            if u.path == "/api/memories/keep":
                self._do_memory_keep()
                return
            if u.path == "/api/llm/config":
                self._do_llm_config()
                return
            if u.path == "/api/llm/test":
                self._do_llm_test()
                return
            if u.path == "/api/tuning/config":
                self._do_tuning_config()
                return
            if u.path == "/api/sources":
                self._do_sources()
                return
            if u.path == "/api/emoji":
                self._do_emoji()
                return
            if u.path.startswith("/api/books/"):
                self._do_books(u.path)
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
            # 对话引用：前端把被引消息 {id, role, content} 传上来。只认 dict，
            # 结构不对就丢弃（不阻断发送）；"让 TA 知道在引用哪句"待后端对齐。
            quote = data.get("quote")
            if not isinstance(quote, dict):
                quote = None
            # 手动安全阀：勾选"紧急"直接开窗口（也可以直接说"有急事"，它自己认）
            if data.get("urgent"):
                guard.open_urgent_window()
            # 重启服务：改了后端代码后点一下，守护会自动拉起新进程，不用手动去终端
            if text == "/restart":
                self._restart()
                return
            with db.SEND_LOCK:      # 只与切库互斥；不等回复生成（否则消息全卡到 TA 回完，实测 #30）
                out = on_message(text, quote)
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

        def _do_sources(self):
            """信息获取（v0.94 批 2）：订阅源增删 + 手动刷一次。

            只读写 sources.txt（一行一条 URL，`#` 注释），不改业务逻辑；
            「刷一次」走 sources.daily_pass()（同一套管道，网页端只是另一个触发口）。
            """
            import sources as _src
            data = self._read_json()
            action = (data.get("action") or "").strip()
            if action == "add":
                url = (data.get("url") or "").strip()
                if not url:
                    self._json({"ok": False, "error": "地址是空的"}, 400)
                    return
                if not _src.add_source(url):
                    self._json({"ok": False, "error": "这条已经有了"}, 400)
                    return
                msg = f"加好了：{url}"
            elif action == "del":
                url = (data.get("url") or "").strip()
                if not _src.remove_source(url):
                    self._json({"ok": False, "error": "订阅表里没有这条"}, 400)
                    return
                msg = f"删掉了：{url}"
            elif action == "refresh":
                if not config.BROWSE_ENABLED:
                    self._json({"ok": False,
                                "error": "刷信息关着呢（BROWSE_ENABLED=off）"}, 400)
                    return
                n = _src.daily_pass()
                msg = f"刷到 {n} 条新的" if n else "这一轮没刷到新的（源不通 / 都是旧的）"
            else:
                self._json({"ok": False, "error": "未知操作"}, 400)
                return
            self._json({"ok": True, "msg": msg,
                        "sources": _src.load_sources(),
                        "items": _src.items_today(limit=20)})

        def _do_emoji(self):
            """表情包池维护（E 模块）：用户新增桶 / 新增表情 / 删自定义表情/桶。
            表情/桶是全局共享的（换朋友不换表情库），内置的不可删。"""
            import emoji_pool
            data = self._read_json()
            action = (data.get("action") or "").strip()
            if action == "add_bucket":
                ok, err = emoji_pool.add_bucket(data.get("name") or "")
            elif action == "add_emoji":
                img = ""
                img_data = data.get("img_data") or ""
                if img_data:
                    img, err = emoji_pool.save_img(img_data)
                    if not img:
                        self._json({"ok": False, "error": err})
                        return
                ok, err = emoji_pool.add_emoji(
                    data.get("bucket") or "", data.get("text") or "",
                    data.get("emotion") or "", img)
            elif action == "remove_emoji":
                ok, err = emoji_pool.remove_emoji(
                    data.get("bucket") or "", data.get("text") or "")
            elif action == "remove_bucket":
                ok, err = emoji_pool.remove_bucket(data.get("name") or "")
            else:
                self._json({"ok": False, "error": "未知操作"}, 400)
                return
            self._json({"ok": ok, "error": "" if ok else err})

        def _read_multipart(self) -> list[tuple[str, bytes]]:
            """自写 multipart/form-data 解析（Python 3.14 已移除 cgi，只能手撸）。

            只取带 filename 的段（上传的书），返回 [(原文件名, 字节内容)]。
            文件名支持 RFC 5987 的 `filename*=UTF-8''%E4%B8%89%E4%BD%93.epub` 写法。
            """
            ctype = self.headers.get("Content-Type", "")
            m = re.search(r'boundary=(?:"([^"]+)"|([^;\r\n]+))', ctype)
            if not m:
                return []
            boundary = (m.group(1) or m.group(2) or "").strip()
            if not boundary:
                return []
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return []
            raw = self.rfile.read(n) if n > 0 else b""
            out = []
            for part in raw.split(b"--" + boundary.encode("utf-8", "ignore")):
                if b"\r\n\r\n" not in part:
                    continue
                head, body = part.split(b"\r\n\r\n", 1)
                if body.endswith(b"\r\n"):        # 每段尾部是 CRLF + 下一个 boundary
                    body = body[:-2]
                name = ""
                for line in head.split(b"\r\n"):
                    if not line.lower().startswith(b"content-disposition:"):
                        continue
                    fm = re.search(rb"filename\*\s*=\s*([^;\r\n]+)|filename\s*=\s*"
                                   rb'(?:"([^"]*)"|([^;\r\n]+))', line, re.I)
                    if not fm:
                        continue
                    if fm.group(1):               # filename*=UTF-8''xxx
                        raw_name = fm.group(1).decode("utf-8", "ignore").strip()
                        name = unquote(raw_name.split("''", 1)[1]) if "''" in raw_name \
                            else unquote(raw_name)
                    else:
                        name = (fm.group(2) or fm.group(3) or b"").decode(
                            "utf-8", "ignore").strip()
                if name:
                    out.append((name, body))
            return out

        def _do_books(self, path: str):
            """书架维护（S4）：批量上传 / 改分类 / 重新分类 / 删书。

            上传的书一律平铺进 books/ 根目录（不建子目录），分类由后端映射表管，
            上传完前端把这一批列出来让用户逐本确认标签——分错了点一下就改过来。
            """
            import library as _lib
            if path == "/api/books/upload":
                files = self._read_multipart()
                if not files:
                    self._json({"error": "没收到文件（只支持 .txt / .epub）"}, 400)
                    return
                added, failed = [], []
                for name, payload in files:
                    got = _lib.save_upload(name, payload)
                    if got:
                        added.append(got)
                    else:
                        failed.append(name)
                self._json({"added": added, "failed": failed})
                return
            data = self._read_json()
            if path == "/api/books/set":
                book_id = (data.get("id") or "").strip()
                got = _lib.set_category(book_id, (data.get("category") or "").strip(),
                                        data.get("tags"))
                if got is None:
                    self._json({"ok": False, "error": "书不存在或分类名不认识"}, 400)
                    return
                self._json({"ok": True, "book": got})
            elif path == "/api/books/reindex":
                self._json({"ok": True, "changed": _lib.reindex()})
            elif path == "/api/books/delete":
                ok = _lib.delete_book((data.get("id") or "").strip())
                self._json({"ok": ok, "error": "" if ok else "删不掉（书不在书架里？）"})
            else:
                self._json({"error": "not found"}, 404)

        def _do_llm_config(self):
            """保存模型设置：写回 .env（全局）+ 热生效，不重启。"""
            import llm as _llm
            try:
                data = self._read_json()
            except Exception:
                self._json({"error": "读不到请求体"}, 400)
                return
            model = str(data.get("model") or "").strip()
            effort = str(data.get("effort") or "").strip().lower()
            base_url = str(data.get("base_url") or "").strip()
            api_key = str(data.get("api_key") or "").strip()
            raw_temp = data.get("temperature", None)
            temp = None
            if raw_temp not in (None, ""):
                try:
                    temp = float(raw_temp)
                except (TypeError, ValueError):
                    self._json({"error": "温度得是数字"}, 400)
                    return
                if not 0.0 <= temp <= 2.0:
                    self._json({"error": "温度要在 0–2 之间"}, 400)
                    return
            if base_url and not base_url.startswith(("http://", "https://")):
                self._json({"error": "接口地址得是 http(s):// 开头"}, 400)
                return
            # 思考强度档位按供应商声明校验（v0.102，不再写死 DeepSeek 的 low/high/max）
            opts = _llm.reasoning_options(base_url or config.DEEPSEEK_BASE_URL)
            if effort and opts and effort not in opts:
                self._json({"error": f"思考强度只能是 {' / '.join(opts)}"}, 400)
                return
            if effort and opts is None:
                effort = ""   # 该供应商无思考强度参数，不落盘
            try:
                cfg = _llm.save_config(model=model, effort=effort, temperature=temp,
                                       base_url=base_url, api_key=api_key)
            except Exception as e:
                self._json({"error": f"保存失败：{e}"}, 500)
                return
            self._json({"ok": True, "config": cfg})

        def _do_llm_test(self):
            """测试连接：用面板里填的地址/密钥/模型（留空用当前配置）实际连一次。
            只验证，不落盘——保存仍走 /api/llm/config。"""
            import llm as _llm
            try:
                data = self._read_json()
            except Exception:
                data = {}
            self._json(_llm.test_connection(
                base_url=str(data.get("base_url") or ""),
                api_key=str(data.get("api_key") or ""),
                model=str(data.get("model") or "")))

        def _do_tuning_config(self):
            """保存调优参数：校验 + 写回 .env（全局）+ 热生效，不重启。"""
            import tuning as _tuning
            data = self._read_json()
            updates = data.get("updates")
            if not isinstance(updates, dict) or not updates:
                self._json({"error": "没有要保存的参数"}, 400)
                return
            try:
                values = _tuning.save_values(updates)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            except Exception as e:
                self._json({"error": f"保存失败：{e}"}, 500)
                return
            self._json({"ok": True, "values": values})

        def _do_revoke(self):
            """撤回一条消息：删库 + 意识流注入一条同步响应（TA 下一轮知道"你撤回了"）。
            只允许撤回自己（role=user）发的；撤回的是"外部记录"，意识流是 TA 的
            "内部感知"——只删库不改感知，TA 会对着空气回那句已经没了的消息。"""
            data = self._read_json()
            try:
                mid = int(data.get("id") or 0)
            except (ValueError, TypeError):
                self._json({"ok": False, "error": "消息 id 不对"})
                return
            removed = db.delete_message(mid)
            if not removed:
                self._json({"ok": False, "error": "这条消息不能撤回（可能不是你的，或已撤回）"})
                return
            import stream
            stream.append("revoke", f"对方撤回了刚才那句「{removed['content']}」，我当没看见")
            self._json({"ok": True, "id": mid})

        def _do_memory_delete(self):
            """删除一条自动记忆（v0.105）：网页端「管理记忆」的删除按钮，
            等价于 CLI 的 /delmem <编号>；AI 提议删除的待确认项点「确认」也走这里。"""
            data = self._read_json()
            try:
                mid = int(data.get("id") or 0)
            except (ValueError, TypeError):
                self._json({"ok": False, "error": "记忆 id 不对"})
                return
            removed = db.delete_memory(mid)
            if not removed:
                self._json({"ok": False, "error": "这条记忆不存在（可能已经被删了）"})
                return
            self._json({"ok": True, "id": mid})

        def _do_long_memory_delete(self):
            """删除一条 B 库长文（v0.105）：网页端「管理记忆 · 长记忆」删除按钮。
            会顺手断开 A 库记忆对它的引用（delete_long_memory 内部已处理）。"""
            data = self._read_json()
            try:
                lid = int(data.get("id") or 0)
            except (ValueError, TypeError):
                self._json({"ok": False, "error": "长记忆 id 不对"})
                return
            removed = db.delete_long_memory(lid)
            if not removed:
                self._json({"ok": False, "error": "这条长记忆不存在（可能已经被删了）"})
                return
            self._json({"ok": True, "id": lid})

        def _do_fact_delete(self):
            """删除一条事实便签（v0.105）：网页端「管理记忆 · 便签」删除按钮，
            等价于 CLI 的 /forget <编号>。"""
            data = self._read_json()
            try:
                fid = int(data.get("id") or 0)
            except (ValueError, TypeError):
                self._json({"ok": False, "error": "便签编号不对"})
                return
            removed = db.delete_fact(fid)
            if not removed:
                self._json({"ok": False, "error": "没有这个编号的便签"})
                return
            self._json({"ok": True, "id": fid})

        def _do_memory_keep(self):
            """保留一条 AI 提议删除的记忆（v0.105）：取消 pending_delete 标记，
            等价于 CLI 的 /keepmem <编号>。"""
            data = self._read_json()
            try:
                mid = int(data.get("id") or 0)
            except (ValueError, TypeError):
                self._json({"ok": False, "error": "记忆 id 不对"})
                return
            db.set_memory_status(mid, "ok")
            self._json({"ok": True, "id": mid})

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
