"""入口：python main.py [cli|server|telegram]

载体路线（v0.30 定档）：**自用阶段 = 网页端 Web UI**（服务端常驻本机），
**推广阶段 = 鸿蒙 App**（服务端上云）。当前通道的用途随之调整：

  cli      命令行调试通道（默认；网页端上线后仍保留作调试用）
  server   企业微信通道（备用，不再是自用阶段主载体——见待确认 #1）
  telegram Telegram 通道（备用，无需域名/公网 IP 的兜底方案）
"""
import sys

import commands
import core
import db
from core import Engine


def make_channel(name: str):
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
    db.init()
    if mode == "cli":
        # 启动先选聊天对象（在引擎/调度器创建前，选完作息按选中朋友排）
        cmd = commands.pick_friend_on_startup()
        if cmd:
            print(commands.handle(cmd))
    channel = make_channel(mode)
    engine = Engine(channel)
    # 一进来就先把离开期间攒下的留言重放出来，不用等你说第一句话
    pending = core.pending_outbox_text()
    if pending:
        channel.send(pending)

    def on_message(text: str) -> None:
        text = text.strip()
        if text.startswith("/"):
            channel.send(commands.handle(text))
            cmd0 = text.split()[0].lower()
            if cmd0 in ("/new-friend", "/friend"):
                engine.on_persona_changed()  # 人格/朋友变了，重排作息与冒泡
            elif cmd0 == "/refresh":
                engine.on_config_reloaded()  # 热重载后定时任务按新配置重排
        else:
            engine.on_user_message(text)

    import scheduler
    sch = scheduler.start(engine)
    engine.sched = sch
    jobs = ", ".join(f"{j.id}→{j.next_run_time:%m-%d %H:%M}" for j in sch.get_jobs())
    print(f"[启动] 模式={mode} 状态={db.get_state()} 调度任务: {jobs}")

    channel.run(on_message)


if __name__ == "__main__":
    main()
