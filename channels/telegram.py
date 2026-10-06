"""Telegram 通道（备案等待期兜底）：长轮询，无需域名/公网 IP。"""
import time

import httpx

import config
from channels.base import Channel

_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"


class TelegramChannel(Channel):
    def send(self, text: str, sender: str = "") -> None:
        if sender:  # 群聊模式：带上发言人名字
            text = f"{sender}：{text}"
        try:
            httpx.post(f"{_API}/sendMessage", json={
                "chat_id": config.TELEGRAM_CHAT_ID, "text": text,
            }, timeout=15)
        except httpx.HTTPError as e:
            print(f"[telegram] 发送失败: {e}")

    def run(self, on_message) -> None:
        print("Telegram 通道启动（长轮询）。")
        offset = 0
        while True:
            try:
                resp = httpx.get(f"{_API}/getUpdates", params={
                    "offset": offset, "timeout": 30,
                }, timeout=40).json()
                for upd in resp.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message", {})
                    text = msg.get("text", "")
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    # 只响应指定用户，顺便首次运行时帮用户发现 chat_id
                    if chat_id and chat_id != config.TELEGRAM_CHAT_ID:
                        print(f"[telegram] 收到来自 chat_id={chat_id} 的消息：{text}"
                              f"（如是你本人，请填入 .env 的 TELEGRAM_CHAT_ID）")
                        continue
                    if text:
                        on_message(text)
            except httpx.HTTPError as e:
                print(f"[telegram] 轮询异常: {e}")
                time.sleep(5)
