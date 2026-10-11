"""通道抽象：业务逻辑（Engine）只依赖这个接口，IM 平台可插拔。"""
from abc import ABC, abstractmethod


class Channel(ABC):
    @abstractmethod
    def send(self, text: str) -> None:
        """向对方发送一条消息（1:1 私聊，没有发言人概念）。"""

    def typing(self) -> None:
        """开始"正在输入"提示（可选能力，默认无操作）。
        Engine 在回复正文发出前调用；CLI 显示提示，Telegram 发原生 typing 状态。"""

    def set_generating(self, active: bool) -> None:
        """回复生成中（LLM 思考阶段）状态（可选能力，默认无操作）。
        Engine 在进入/退出回复流程时调用，网页端据此显示"正在回复"提示。"""

    @abstractmethod
    def run(self, on_message) -> None:
        """启动接收循环，收到用户消息时回调 on_message(text)。阻塞运行。"""
