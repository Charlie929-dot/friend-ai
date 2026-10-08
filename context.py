"""信息调度层：统一给 LLM 组装上下文，按优先级注入（P2）。

把原先散落在 core 里的 prompt 拼装收拢到这里，统一注入优先级（从高到低）：
   事实便签（/note，用户亲口确认）> profile（信息清单）> 记忆（自动提取）> 聊天上下文。
   - 便签 > profile：由 persona.build_system_prompt 里的"以这里为准"保证；
   - 记忆 > 上下文：记忆作为 system 注入（指令权重更高），聊天上下文作为 user 消息；
   - 记忆渲染统一带时间锚点（事发时间 / 存储时间）。
"""
import random

import config
import db
import llm
import memory
import persona

# 记忆注入的统一文案
_MEMORY_HINT = "自然使用，别生硬复述；同主题多条按时间先后排列，靠后的是最新进展"


# ---------- 记忆渲染（统一带时间锚点） ----------

def _memory_line(m: dict) -> str:
    """一条记忆 → 一行注入文本。事发时间（time_label）与存储时间（created_at）
    是两个维度：AI 既能说出"你今天下午不是说……"，也知道这条是啥时候记下的。"""
    anchor = (f"（{m['time_label']}）" if m.get("time_label")
              else (f"（{m['created_at'][5:10]} 记）" if m.get("created_at") else ""))
    line = "- " + anchor + m["content"]
    # A/B 库：带 long_id 的记忆是一条"伪链接"，只给摘要 + 提示，需要时才展开全文
    if m.get("long_id"):
        line += "【有长文，可说「详细讲讲」】"
    return line


def render_memories(mems: list[dict]) -> str:
    return "\n".join(_memory_line(m) for m in mems)


def _memory_message(mems: list[dict]) -> dict | None:
    """把召回的记忆渲染成一条 system 注入消息；没有命中返回 None。"""
    if not mems:
        return None
    # A/B 库：召回里已展开的长记忆（long_content）把全文一并渲染进去
    lines = []
    for m in mems:
        lines.append(_memory_line(m))
        if m.get("long_content"):
            lines.append("  （详情全文）" + m["long_content"])
    return {"role": "system",
            "content": f"【你想起来的旧记忆】（{_MEMORY_HINT}）\n" + "\n".join(lines)}


# ---------- 私聊回复 ----------

def build_chat_messages(state: str, batch: list[str], game_rules: str = "") -> list[dict]:
    """私聊回复的完整 messages：system（人设+便签+profile）→ 游戏规则 →
    相关记忆（system）→ 最近聊天上下文（user）。"""
    messages = [{"role": "system", "content": persona.build_system_prompt(state)}]
    if game_rules:
        messages.append({"role": "system", "content": game_rules})
    combined = " ".join(batch)
    if combined:
        # 追问细节（"详细讲讲/完整说说"）时展开命中的长记忆全文
        mem_msg = _memory_message(
            memory.recall(combined, expand_long=memory.wants_expansion(combined)))
        if mem_msg:
            messages.append(mem_msg)
    messages += db.recent_context(config.CONTEXT_TURNS)
    return messages


# ---------- 主动消息 ----------

def build_proactive_messages(state: str, scenario: str) -> list[dict]:
    """主动消息的 messages；random 场景有一半概率翻一条旧记忆接着聊（话题弹药）。"""
    prompt = persona.build_proactive_prompt(state, scenario)
    if scenario == "random" and random.random() < 0.5:
        mem = memory.random_memory()
        if mem:
            prompt[-1]["content"] += (
                f"\n（你忽然想起关于TA的一件事：「{mem['content']}」。"
                f"可以就着它聊，比如问问后续；想聊别的也行。）")
    return prompt

