"""通道抽象：业务逻辑（Engine）只依赖这个接口，IM 平台可插拔。"""
from abc import ABC, abstractmethod


class Channel(ABC):
    @abstractmethod
    def send(self, text: str, sender: str = "") -> None:
        """向用户发送一条消息。sender 非空时表示发言人名字（群聊模式）。"""

    def typing(self, sender: str = "") -> None:
        """开始"正在输入"提示（可选能力，默认无操作）。
        Engine 在回复正文发出前调用；CLI 显示提示，Telegram 发原生 typing 状态。"""

    @abstractmethod
    def run(self, on_message) -> None:
        """启动接收循环，收到用户消息时回调 on_message(text)。阻塞运行。"""
