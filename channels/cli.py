"""CLI 通道：本地命令行调试，主动消息直接打印到终端。

每条消息带时间戳（年-月-日 时:分），离开一会儿回来也能看清消息是啥时候发的。
用户消息的时间戳打在发送后一行（输入行本身没法回溯加前缀）。
"""
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


class CliChannel(Channel):
    def send(self, text: str, sender: str = "") -> None:
        # 群聊时 sender 是发言人自己的名字；单聊退回当前朋友名
        print(f"\n[{_ts()} {sender or _friend_name()}] {text}\n> ", end="", flush=True)

    def typing(self, sender: str = "") -> None:
        print(f"\n[{_ts()} {sender or _friend_name()}] 正在输入…", end="", flush=True)

    def run(self, on_message) -> None:
        print("CLI 模式启动，输入消息回车发送，Ctrl+C 退出。")
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
