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
import selfmem
import stream

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

def _quote_hint(quote: dict | None) -> dict | None:
    """引用回复：让 TA 知道对方这句话在接着谁说的哪句往下聊，避免答非所问。

    quote = {"id","role","content"}，role 决定被引句是谁说的：assistant=TA 自己
    之前说的、user=对方自己之前说的。content 存的是原文快照（发送时前端带上），
    所以哪怕被引的那句早于 CONTEXT_TURNS 也能拿到。称呼走动态口（不写死名字）。"""
    if not quote or not (quote.get("content") or "").strip():
        return None
    speaker = (persona.friend_name() if quote.get("role") == "assistant"
               else persona.user_name())
    return {"role": "system", "content": (
        f"【引用回复】对方这句话是在接着 {speaker} 之前说的这句往下聊："
        f"「{quote['content'].strip()}」。回应时结合这句被引的话来理解对方在接什么。")}


def build_chat_messages(state: str, batch: list[str], game_rules: str = "",
                        inbox: bool = False, quote: dict | None = None) -> list[dict]:
    """私聊回复的完整 messages：system（人设+便签+profile）→ 游戏规则 →
    相关记忆（system）→ 最近聊天上下文（user）。inbox=True 时带"刚上线看到留言"语境。
    quote 非空时额外注入一条【引用回复】说明对方在回哪句。"""
    messages = [{"role": "system", "content": persona.build_system_prompt(state)}]
    if inbox:
        messages.append({"role": "system", "content": (
            "【你刚上线】你刚才在忙自己的事（或睡着了），没看手机。"
            "现在看到 TA 在你不在的时候发来的话，自然地回应一下——"
            "可以顺口带一句刚在忙/刚醒，然后正经回 TA 说的内容。")})
    # 紧急通道（打断成本的安全阀）：识别到急事 → 手头的活先放下，简短回应、先听TA说
    import guard
    if guard.in_urgent_window():
        messages.append({"role": "system", "content": (
            "【急事】TA 刚才的话听着有急事。把手头的事先放下，立刻回应、"
            "简短、先听TA说，别扯别的、别卖关子。")})
    else:
        # 语气主轨（A 模块，10-10）：每轮定一个主导情绪状态（mood 主轨），打断成本
        # （cost 相处姿势）从属于它，收成一条【此刻的我】块——治语言割裂（实测 #36）。
        import tone
        thint = tone.voice_hint()
        if thint:
            messages.append({"role": "system", "content": thint})
    # 自我意识流（S4）：此刻的我（攒了多少/精力/心情）+ 最近发生的事——补 money/energy
    # 进 prompt（原来钱只在 HUD 给你看、TA 自己不知道，实测 #36）。紧急窗口也注入。
    shint = stream.prompt_hint()
    if shint:
        messages.append({"role": "system", "content": shint})
    if config.SELF_MEMORY_ENABLED:
        self_hint = selfmem.prompt_hint(" ".join(batch))
        if self_hint:
            messages.append({"role": "system", "content": self_hint})
    if game_rules:
        messages.append({"role": "system", "content": game_rules})
    # 今天做过/读到的事（B 库，跨天自动不再注入）：TA 才知道自己这一天是怎么过的
    notes = memory.today_notes() if config.TODAY_NOTES_ENABLED else []
    if notes:
        lines = []
        for n in notes:
            body = (n.get("content") or "").strip().replace("\n", " ")
            if len(body) > config.TODAY_NOTES_CHARS:
                body = body[:config.TODAY_NOTES_CHARS] + "…"
            lines.append(f"- {n['summary']}：{body}")
        messages.append({"role": "system",
                         "content": f"【你今天做过的事】（{memory._TODAY_NOTE_HINT}，"
                                    f"细节过了今天就淡了）\n" + "\n".join(lines)})
    combined = " ".join(batch)
    if combined:
        # 追问细节（"详细讲讲/完整说说"）时展开命中的长记忆全文
        mem_msg = _memory_message(
            memory.recall(combined, expand_long=memory.wants_expansion(combined)))
        if mem_msg:
            messages.append(mem_msg)
    # 引用回复：放在最近对话之前，让 TA 先知道"对方在回哪句"再读原文
    qhint = _quote_hint(quote)
    if qhint:
        messages.append(qhint)
    messages += db.recent_context(config.CONTEXT_TURNS)
    return messages


# ---------- 主动消息 ----------

# 主动消息必须知道"你们最近聊了什么"：刚聊过 2.5 小时又说"好久没听你唠了"
# 就是瞎编（实测 #26）。注入最近聊天 + 这条规则。
_PROACTIVE_CONTEXT_RULE = (
    "发消息前先看一眼你们最近的聊天：如果不久前刚聊过，就顺着最近的话茬自然接下去，"
    "千万别用「好久没聊」「最近在忙啥呀」这种跟事实对不上的开场。")


def build_proactive_messages(state: str, scenario: str) -> list[dict]:
    """主动消息的 messages：最近聊天上下文 + 场景指令。
    random 场景的话题弹药二选一：攒下的素材（读后感想）或关于TA的旧记忆。"""
    prompt = persona.build_proactive_prompt(state, scenario)
    prompt[-1]["content"] += f"\n{_PROACTIVE_CONTEXT_RULE}"
    if scenario == "random":
        _inject_random_topic(prompt)
    recent = db.recent_context(config.PROACTIVE_CONTEXT_TURNS)
    # 自我记忆：主动开口最该兑现的是"我答应过的事"——那是它自己的承诺，比寒暄有分量
    extra = []
    if config.SELF_MEMORY_ENABLED:
        hint = selfmem.prompt_hint()
        if hint:
            extra.append({"role": "system", "content": hint})
            prompt[-1]["content"] += (
                "\n（上面有你答应过却还没兑现的事的话，现在提它正合适。）")
    if recent or extra:
        # 顺序：system（人设）→ 关于我自己的记忆 → 最近聊天（事实）→ 指令（内心活动）最后说
        return [prompt[0]] + extra + recent + [prompt[-1]]
    return prompt


def _inject_random_topic(prompt: list[dict]) -> None:
    """random 场景的话题弹药：一半概率翻一条攒下的素材（工作=读书的读后感），
    没有素材（或没抽中）才退回翻一条关于TA的旧记忆。素材攒了就要用得起来，
    否则 TA 闷头读书、攒一堆感想却从来不跟人聊（实测 #28）。"""
    if random.random() < 0.5:
        import work
        mat = work.take_shareable()
        if mat:
            prompt[-1]["content"] += (
                f"\n（你刚忙完手头的事，脑子里还留着这么一句："
                f"「{mat}」。可以自然跟TA聊起它——比如提一句你最近在读什么书、"
                f"读到哪句挺有感觉。想聊别的也行。）")
            return
    mem = memory.random_memory()
    if mem:
        anchor = mem.get("time_label") or ""
        if not anchor and mem.get("created_at"):
            anchor = f"{mem['created_at'][5:10]}记的"
        when = f"（{anchor}）" if anchor else ""
        prompt[-1]["content"] += (
            f"\n（你忽然想起关于TA的一件事{when}：「{mem['content']}」。"
            f"可以就着它聊，比如问问后续；想聊别的也行。）")

