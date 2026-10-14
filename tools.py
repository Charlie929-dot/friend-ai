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
import db
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
    cap: bool = False                        # 返回文本是否受 TOOL_RESULT_CHARS 截断
    #   —— 长文展开类工具（recall_detail）必须保持 False，否则砍掉"展开全文"本身


_REGISTRY: dict[str, Spec] = {}

# 未知工具名 → 机制层默认话术（这是机制固定文案、不会扩充，不属语料 → 就地维护）
_TOOL_UNKNOWN = "（不知道这是什么能力）"
# 触发截断时的补充说明（同上：机制固定文案，不属语料）
_TOOL_TRUNCATED = "…（后面还长，先看到这儿）"


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
# 外部查询信号（v0.94 批 4）：查证 / 时效 + 贴链接。
_LOOKUP_SIGNAL = re.compile(corpus.LOOKUP_SIGNALS)
_LOOKUP_FRESH = re.compile(corpus.LOOKUP_FRESH_SIGNALS)
_LOOKUP_QUESTION = re.compile(corpus.LOOKUP_QUESTIONS)
_LOOKUP_URL = re.compile(corpus.LOOKUP_URL_SIGNAL)


def _lookup_signal(text: str) -> bool:
    """外部查询信号（批 4）——完整目标形态（方案 §2.1）：

        (MEMORY_TOOLS_ENABLED 且 回忆信号)
        或 (LOOKUP_ENABLED 且 ((SEARCH_PROVIDER != "none" 且 (查证 或 时效)) 或 消息含 URL))

    拆开说：
    - 消息里贴了链接 → 永久可用——但**"永久"指不受 SEARCH_PROVIDER 门，仍受 LOOKUP_ENABLED 总门**；
    - 查证 / 时效词 → 只在配了搜索源时算（否则工具表里没有能干这事的工具，触发就白跑一轮）。
    """
    if not getattr(config, "LOOKUP_ENABLED", True):
        return False
    if _LOOKUP_URL.search(text):
        return True
    if (getattr(config, "SEARCH_PROVIDER", "none") or "none") != "none":
        return bool(_LOOKUP_SIGNAL.search(text)
                    or (_LOOKUP_FRESH.search(text) and _LOOKUP_QUESTION.search(text)))
    return False


def should_use(batch_text: str) -> bool:
    """这批消息里有没有"工具该上场"的信号——任一命中即启用工具循环。

    两个开关**各管各的信号**（别让 `MEMORY_TOOLS_ENABLED=off` 把外部查询一起掐死）。
    """
    if config.MEMORY_TOOLS_ENABLED and _RECALL_SIGNAL.search(batch_text):
        return True
    return _lookup_signal(batch_text)


def run(name: str, args: dict) -> str:
    """按名分发到对应 handler；未知工具名返回机制默认话术（替代旧 if 链）。

    `Spec.cap=True` 的工具，返回文本按 TOOL_RESULT_CHARS 截断（防烧 token）——
    长文展开类保持 False（否则砍掉"展开"本身）。"""
    spec = _REGISTRY.get(name)
    if spec is None:
        return _TOOL_UNKNOWN
    result = spec.handler(args)
    if spec.cap:
        result = _cap_result(result)
    return result


def _cap_result(text: str) -> str:
    """机制层截断（只对 `cap=True` 的工具生效）。"""
    limit = int(getattr(config, "TOOL_RESULT_CHARS", 1200) or 0)
    if limit and len(text) > limit:
        return text[:limit] + _TOOL_TRUNCATED
    return text


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
    """生成回复：有工具信号 → tool loop；否则普通快速路径（原 core._generate_reply_text）。

    开头先 `begin_reply()`：清空"本轮已用取数次数"（拍板 #13，core 一行不用改）。"""
    begin_reply()
    if should_use(" ".join(batch)):
        return _run_with_tools(messages)
    try:
        return llm.chat(messages)
    except llm.LLMUnavailable as e:
        return corpus.LLM_ERROR.format(e)


# --- 网络取数预算（方案 §3.3） ------------------------------------------------
# 延迟和 token 是真钱：每轮回复有上限、每天也有总额。**机制层**（不知道哪个工具在联网），
# 具体谁能上网由各自 handler 调 `allow_lookup()` 前先问一句。
# 每日计数走 `db.incr_counter`（按朋友库隔离、按天自动归零）；每轮计数是进程内的，
# `begin_reply()` 在每轮回复开始时清一次。
_LOOKUP_COUNTER_KEY = "lookup_online"
_reply_spent = 0


def begin_reply() -> None:
    """一轮回复开始：清空"本轮已用取数次数"（每日总额不受影响）。"""
    global _reply_spent
    _reply_spent = 0


def allow_lookup() -> bool:
    """还能不能再上一次网——每轮 + 每日双上限，任一超出就 False。"""
    if not getattr(config, "LOOKUP_ENABLED", True):
        return False
    if _reply_spent >= int(getattr(config, "LOOKUP_MAX_PER_REPLY", 1) or 0):
        return False
    if db.get_counter(_LOOKUP_COUNTER_KEY) >= int(getattr(config, "LOOKUP_DAILY_LIMIT", 20) or 0):
        return False
    return True


def note_lookup() -> None:
    """记一次网络取数（本轮 +1、今日 +1）。"""
    global _reply_spent
    _reply_spent += 1
    db.incr_counter(_LOOKUP_COUNTER_KEY)
