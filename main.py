"""入口：python main.py [cli|server|telegram]

  cli      本地命令行调试（默认，备案等待期用这个开发）
  server   企业微信通道（FastAPI 回调，部署到云服务器后用）
  telegram Telegram 通道（手机上提前体验的兜底方案）
"""
import sys

import commands
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
            import groupchat
            if groupchat.is_active():
                groupchat.on_user_text(text, channel)  # 群聊模式：大家轮流说
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
