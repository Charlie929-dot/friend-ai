"""信息调度层：统一给 LLM 组装上下文，按优先级注入（P2）。

把原先散落在 core / groupchat 里的 prompt 拼装收拢到这里，统一三件事：

1. **统一注入优先级**（从高到低）：
   事实便签（/note，用户亲口确认）> profile（信息清单）> 记忆（自动提取）> 聊天上下文。
   - 便签 > profile：由 persona.build_system_prompt 里的"以这里为准"保证；
   - 记忆 > 上下文：记忆作为 system 注入（指令权重更高），聊天上下文作为 user 消息；
   - 记忆渲染统一带时间锚点（事发时间 / 存储时间），core 与 groupchat 不再各写一遍。

2. **群聊场景记忆优先**：群聊来源的记忆（source='group'）召回加分，
   优先于 1:1 记忆（source='private'）——同处一个群里的往事更贴合群聊语境。

3. **长会话防遗忘**：群聊早前滚出窗口的内容压成摘要兜底，不只靠最近 N 条。
"""
import random

import config
import db
import llm
import memory
import persona

# 记忆注入的统一文案（私聊 / 群聊共用）
_MEMORY_HINT = "自然使用，别生硬复述；同主题多条按时间先后排列，靠后的是最新进展"

# 长会话摘要的 prompt（把滚出窗口的早前群聊内容压成简短摘要）
_SUMMARY_PROMPT = """把下面这段群聊记录压缩成一段简短的摘要（100字以内），只保留对后续聊天有用的信息：
- 谁是谁（新成员介绍、身份、彼此的关系）
- 重要的事件、决定、计划、承诺
- 还没聊完、可能要继续的话题

用第三人称客观陈述，别加戏，别复述寒暄客套。"""


# ---------- 记忆渲染（统一带时间锚点） ----------

def _memory_line(m: dict) -> str:
    """一条记忆 → 一行注入文本。事发时间（time_label）与存储时间（created_at）
    是两个维度：AI 既能说出"你今天下午不是说……"，也知道这条是啥时候记下的。
    群聊来源的记忆（source='group'）标注「群聊」，AI 才知道这是群里发生的、
    不是私聊里 TA 单独听说的。"""
    tag = "（群聊）" if m.get("source") == "group" else ""
    anchor = (f"（{m['time_label']}）" if m.get("time_label")
              else (f"（{m['created_at'][5:10]} 记）" if m.get("created_at") else ""))
    return "- " + tag + anchor + m["content"]


def render_memories(mems: list[dict]) -> str:
    return "\n".join(_memory_line(m) for m in mems)


def _memory_message(mems: list[dict]) -> dict | None:
    """把召回的记忆渲染成一条 system 注入消息；没有命中返回 None。"""
    if not mems:
        return None
    return {"role": "system",
            "content": f"【你想起来的旧记忆】（{_MEMORY_HINT}）\n{render_memories(mems)}"}


# ---------- 私聊回复 ----------

def build_chat_messages(state: str, batch: list[str], game_rules: str = "") -> list[dict]:
    """私聊回复的完整 messages：system（人设+便签+profile）→ 游戏规则 →
    相关记忆（system）→ 最近的群聊记忆（system，兜底防"群聊失忆"）→
    最近聊天上下文（user）。"""
    messages = [{"role": "system", "content": persona.build_system_prompt(state)}]
    if game_rules:
        messages.append({"role": "system", "content": game_rules})
    combined = " ".join(batch)
    if combined:
        mem_msg = _memory_message(memory.recall(combined))
        if mem_msg:
            messages.append(mem_msg)
    # 最近的群聊记忆：关键词召回靠命中，问得含糊就召不回——这里兜底注入，
    # 让 TA 私聊里也记得"我们最近在群里聊过这些"，能接上、不会失忆
    group_mems = memory.recent_group_memories(limit=5)
    if group_mems:
        messages.append({"role": "system",
                         "content": "【你们最近在群里聊过的事】（这些是在群聊里发生的，"
                                    "不是私聊单独说的，别搞混）\n" + render_memories(group_mems)})
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


# ---------- 群聊发言 ----------

def build_group_messages(system_prompt: str, transcript: list, idle: bool = False,
                         summary: str = "") -> list[dict]:
    """群聊发言的完整 messages。

    system_prompt：groupchat 拼好的（人设 + 群聊规则 + 闲聊提示）。
    transcript：list[(speaker, text)]，按时间顺序。
    summary：滚出窗口的更早内容摘要（长会话防遗忘兜底），没有传空串。
    """
    messages = [{"role": "system", "content": system_prompt}]
    # 群聊来源记忆优先召回（同处一个群的往事优先于私聊记忆）
    recent = " ".join(t for _, t in transcript[-15:])
    recalled = memory.recall(recent, top_k=3, group_first=True) if recent else []
    mem_msg = _memory_message(recalled)
    if mem_msg:
        messages.append(mem_msg)
    # 长会话防遗忘：更早内容用摘要兜底，让 AI 记得"这是新朋友阿燃"这类早前介绍
    if summary:
        messages.append({"role": "system",
                         "content": f"【更早的群聊摘要】（早前滚出窗口的内容，帮你别忘）\n{summary}"})
    # 上下文窗口：回应流程多带点（记住早前介绍），自主闲聊带短一点省 token
    window = (transcript[-config.GROUP_IDLE_CONTEXT_MESSAGES:] if idle
              else transcript[-config.GROUP_CONTEXT_MESSAGES:])
    log = "\n".join(f"{sp}：{tx}" for sp, tx in window)
    messages.append({"role": "user",
                     "content": f"【群聊记录】\n{log}\n\n（轮到你了，直接说内容）"})
    return messages


def summarize_transcript(head: list) -> str:
    """把滚出窗口的早前群聊内容压成一段摘要。失败返回空串（不阻塞发言）。"""
    if not head:
        return ""
    log = "\n".join(f"{sp}：{tx}" for sp, tx in head)
    try:
        return llm.chat([
            {"role": "system", "content": _SUMMARY_PROMPT},
            {"role": "user", "content": log},
        ], temperature=0.3).strip()
    except llm.LLMUnavailable:
        return ""
