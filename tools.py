"""工具层：注册 / 分发 / 预算的纯机制（v0.94 §3）。

设计要点：
- 本模块**不知道任何具体工具**——具体能力由别处向它 `register()` 一个 Spec。
  这也是打破循环依赖的关键：`tools` 永不 import `sources`（详见方案 §1.3①），
  外部源模块 `sources.py` 自己向这里注册。
- **拟人化只管外壳**：`desc`（给模型看的描述，体感口吻）与工具返回文案可以拟人；
  契约内核——工具名（英文、无歧义）、参数 JSON Schema、返回结构、标记 token——
  一律保留清晰，别为拟人改掉（项目硬性约定 #2）。
- 加新工具 = 在对应模块 `register(Spec(...))` 一处，不再改 `tools`/`core`。

现有能力：`memory.py` 注册的 `search_memories` / `recall_detail`。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

import config
import corpus
import llm


@dataclass
class Spec:
    """一个工具的全部定义。"""

    name: str                                # 契约内核：给模型看的英文名，不拟人化
    desc: str                                # 拟人外壳：给模型看的描述，体感口吻
    params: dict                             # JSON Schema（显式写全 type/properties/required）
    handler: Callable[[dict], str]           # 收到 args → 返回给模型读的纯文本
    cost: int = 1                            # 预算权重（模块内静态表，不进 config）
    gate: Callable[[], bool] = lambda: True  # 开关（读 config）


_REGISTRY: dict[str, Spec] = {}

# 未知工具名 → 机制层默认话术（这是机制固定文案、不会扩充，不属语料 → 就地维护）
_TOOL_UNKNOWN = "（不知道这是什么能力）"


def register(spec: Spec) -> Spec:
    """注册一个工具并返回它（同名以最后一次为准）。"""
    _REGISTRY[spec.name] = spec
    return spec


def schema(spec: Spec) -> dict:
    """把一个 Spec 转成 OpenAI function-calling 的 schema。"""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.desc,
            "parameters": spec.params,
        },
    }


def specs() -> list[dict]:
    """拼给 llm.chat_with_tools 的工具列表（只含 gate() 为真的）。"""
    return [schema(s) for s in _REGISTRY.values() if s.gate()]


# 回忆/追问信号（原 memory.wants_memory_search 的信号，v0.94 批 1 原样搬来）：
# 命中则启用工具让 AI 主动查记忆；普通闲聊不含这些词，走快速路径（省 token、省延迟）。
# 词表唯一真相在 corpus（语料，会扩充）；这里只做机制级编译。
_RECALL_SIGNAL = re.compile(corpus.MEMORY_SEARCH_QUERY)


def should_use(batch_text: str) -> bool:
    """这批消息里有没有"工具该上场"的信号——任一命中即启用工具循环。

    v0.94 批 1：只有回忆类信号（行为与搬家前完全一致）。
    v0.94 批 4 会再加一类"外部查询"信号（corpus.LOOKUP_SIGNALS，受 LOOKUP_ENABLED 管）。
    """
    if not config.MEMORY_TOOLS_ENABLED:
        return False
    return bool(_RECALL_SIGNAL.search(batch_text))


def run(name: str, args: dict) -> str:
    """按名分发到对应 handler；未知工具名返回机制默认话术（替代旧 if 链）。"""
    spec = _REGISTRY.get(name)
    if spec is None:
        return _TOOL_UNKNOWN
    return spec.handler(args)


def _run_with_tools(messages: list[dict]) -> str:
    """带工具的回复生成（原 core.Engine._chat_with_memory_tools，逻辑等价搬来）。

    程序自动召回仍是第一层兜底（messages 里已注入相关记忆），工具调用是"主动深挖"——
    AI 自己决定查什么。最多 config.MEMORY_TOOL_MAX_ROUNDS 轮，防无限循环；
    最终没生成文本时用无工具调用兜底。"""
    final_text = ""
    for _ in range(config.MEMORY_TOOL_MAX_ROUNDS):
        try:
            msg = llm.chat_with_tools(messages, specs())
        except llm.LLMUnavailable as e:
            return corpus.LLM_ERROR.format(e)
        tool_calls = msg.get("tool_calls") or []
        final_text = msg.get("content") or ""
        if not tool_calls:
            return final_text  # 不调工具：直接用这轮的文本
        # 回填 assistant 的工具调用消息。⚠️ 多轮 tools 必须连 reasoning_content 一起
        # 回传（DeepSeek V4 Flash：否则工具调用质量下降、部分端点 400）。
        assistant_msg = {
            "role": "assistant",
            "content": final_text,
            "tool_calls": tool_calls,
        }
        if msg.get("reasoning_content"):
            assistant_msg["reasoning_content"] = msg["reasoning_content"]
        messages.append(assistant_msg)
        # 执行每个工具，结果作为 tool 消息回填给下一轮
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            result = run(name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": result,
            })
    # 超过轮数上限仍无文本：无工具兜底拿一个回复
    if not final_text.strip():
        try:
            return llm.chat(messages)
        except llm.LLMUnavailable as e:
            return corpus.LLM_ERROR.format(e)
    return final_text


def run_reply(messages: list[dict], batch: list[str]) -> str:
    """生成回复：有工具信号 → tool loop；否则普通快速路径（原 core._generate_reply_text）。"""
    if should_use(" ".join(batch)):
        return _run_with_tools(messages)
    try:
        return llm.chat(messages)
    except llm.LLMUnavailable as e:
        return corpus.LLM_ERROR.format(e)
