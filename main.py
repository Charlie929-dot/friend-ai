"""入口：python main.py [web|cli|server|telegram]

载体路线（v0.30 定档）：**自用阶段 = 网页端 Web UI**（服务端常驻本机），
**推广阶段 = 鸿蒙 App**（服务端上云）。当前通道的用途随之调整：

  web      网页端（自用阶段主载体）：浏览器打开 http://127.0.0.1:8787 就能聊
  cli      命令行调试通道；与网页端同一份 db，改配置/查记忆/跑回填用它顺手
  server   企业微信通道（备用，不再是自用阶段主载体——见待确认 #1）
  telegram Telegram 通道（备用，无需域名/公网 IP 的兜底方案）

  autostart [on|off|status|start|stop]   网页端的开机自启与常驻（见 autostart.py）
"""
import sys

import commands
import config
import core
import db
import persona
from core import Engine


def make_channel(name: str):
    if name == "web":
        from webui import WebChannel
        return WebChannel()
    if name == "server":
        from channels.wecom import WecomChannel
        return WecomChannel()
    if name == "telegram":
        from channels.telegram import TelegramChannel
        return TelegramChannel()
    from channels.cli import CliChannel
    return CliChannel()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "cli"
    if mode == "autostart":
        # 开机自启不碰 db，先于 init 处理（装/卸/查都不需要起引擎）
        import autostart
        print(autostart.cli(sys.argv[2] if len(sys.argv) > 2 else "status"))
        return
    db.init()
    if mode == "cli":
        # 启动先选聊天对象（在引擎/调度器创建前，选完作息按选中朋友排）
        cmd = commands.pick_friend_on_startup()
        if cmd:
            print(commands.handle(cmd))
    elif mode == "web":
        # 网页端没法在终端里选人：用当前默认库，要换朋友在页面里发 /friend <编号>
        print(f"[网页端] 当前朋友：{persona.friend_name()}（{config.DB_PATH.name}）")
    channel = make_channel(mode)
    engine = Engine(channel)
    # 一进来就先把离开期间攒下的留言重放出来，不用等你说第一句话
    pending = core.pending_outbox_text()
    if pending:
        channel.send(pending)

    def handle_text(text: str, quote: dict | None = None):
        """处理一条输入：斜杠命令返回输出文本（str）；普通消息返回它落库的 id（int）
        （普通消息的回复由引擎后台线程生成，不在这里等）。"""
        text = text.strip()
        if text.startswith("/"):
            out = commands.handle(text)
            cmd0 = text.split()[0].lower()
            if cmd0 in ("/new-friend", "/friend"):
                engine.on_persona_changed()  # 人格/朋友变了，重排作息与冒泡
            elif cmd0 == "/refresh":
                engine.on_config_reloaded()  # 热重载后定时任务按新配置重排
            return out
        return engine.on_user_message(text, quote=quote)

    def on_message(text: str, quote: dict | None = None):
        out = handle_text(text, quote)
        if isinstance(out, str) and out:
            channel.send(out)   # 网页端 send 是空操作，输出走 /api/send 的返回值
        return out              # 网页端要把命令输出回给浏览器

    import scheduler
    sch = scheduler.start(engine)
    engine.sched = sch
    jobs = ", ".join(f"{j.id}→{j.next_run_time:%m-%d %H:%M}" for j in sch.get_jobs())
    print(f"[启动] 模式={mode} 状态={db.get_state()} 调度任务: {jobs}")

    channel.run(on_message)


if __name__ == "__main__":
    main()
