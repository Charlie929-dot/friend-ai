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
import emoji_pool
import llm
import memory
import persona
import selfmem
import stream
import todos

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
    """私聊回复的完整 messages：静态 system → **意识流** → 动态 system → 其余注入 →
    整段会话上下文（user）。inbox=True 时带"刚上线看到留言"语境。
    quote 非空时额外注入一条【引用回复】说明对方在回哪句。

    ⚠️ 顺序是钱（v0.80）：静态 system + 意识流历史构成**缓存稳定前缀**，会变的
    （当前时间/状态/手头的事）一律排在意识流之后。否则全量读的 token 每轮都按
    未命中计价，一天就不是一两块而是十几块。"""
    # 1) 静态人设（每轮不变）
    messages = [{"role": "system", "content": persona.static_system_prompt()}]
    # 2) 自我意识流：紧跟静态区，在所有动态注入之前（缓存稳定前缀的尾巴）
    shint = stream.prompt_hint()
    if shint:
        messages.append({"role": "system", "content": shint})
    # 3) 动态：当前时间 / 状态 / 手头的事
    messages.append({"role": "system", "content": persona.dynamic_system_prompt(state)})
    # 4) 想说还没说的事（待办队列）：治"颠来倒去就一句"，说过的标 ✅ 提醒别重复
    thint = todos.prompt_hint()
    if thint:
        messages.append({"role": "system", "content": thint})
    if inbox:
        messages.append({"role": "system", "content": (
            "【你刚上线】我刚才在忙自己的事（或睡着了），没看手机。"
            "现在看到 TA 在我不在的时候发来的话，自然地回应一下——"
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
    # 表情包池（E 模块）：让 TA 聊天前"翻看"表情库存、可以只甩一个表情当懒人回应、
    # 别重复最近用过的（治「表情单一 😂 反复用」，实测 #36）。
    ehint = emoji_pool.prompt_hint()
    if ehint:
        messages.append({"role": "system", "content": ehint})
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
                         "content": f"【我今天做过的事】（{memory._TODAY_NOTE_HINT}，"
                                    f"细节过了今天就淡了）\n" + "\n".join(lines)})
    # 今天刷到的（v0.97 批 3）：TA 自己的视角——「我今天刷到过这些」，**不是"帮你查的"**
    # （一视同仁红线 §12）。空表则整块不出现；块内自带负约束守住"别把不知道的说成刚刷到"。
    # 位置在【我今天做过的事】之后、仍在动态区，不破坏 context.py 缓存稳定前缀。
    if config.BROWSE_ENABLED:
        import sources as _sources
        browsed = _sources.items_today(limit=config.BROWSE_CONTEXT_MAX)
        if browsed:
            blines = []
            for it in browsed:
                body = (it.get("summary") or it.get("title") or "").strip().replace("\n", " ")
                if len(body) > config.BROWSE_ITEM_CHARS:
                    body = body[:config.BROWSE_ITEM_CHARS] + "…"
                src = (it.get("source") or "").strip()
                blines.append("- " + body + (f"（{src}）" if src else ""))
            messages.append({"role": "system", "content": (
                "【我今天刷到的】这一块里有什么，我今天就真的只刷到了这些；"
                "没有这一块 = 今天没刷到什么，别把不知道的东西说成「我刚刷到」。\n"
                + "\n".join(blines))})
    # 读书笔记（§4 alpha 库）：最近章节按累计字数窗口必带（读得近的靠后）
    if config.TRUE_READ_ENABLED:
        import notes as _notes
        nhint = _notes.prompt_hint()
        if nhint:
            messages.append({"role": "system", "content": nhint})
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
    # 整段会话全量（v0.80）：今天起床 → 现在，不再按"轮"截断（实测 #42）
    messages += db.session_context(config.CONTEXT_MAX_MESSAGES)
    return messages


# ---------- 主动消息 ----------

# 主动消息必须知道"你们最近聊了什么"：刚聊过 2.5 小时又说"好久没听你唠了"
# 就是瞎编（实测 #26）。注入最近聊天 + 这条规则。
_PROACTIVE_CONTEXT_RULE = (
    "发消息前先看一眼你们最近的聊天：如果不久前刚聊过，就顺着最近的话茬自然接下去，"
    "千万别用「好久没聊」「最近在忙啥呀」这种跟事实对不上的开场。")


def build_proactive_messages(state: str, scenario: str) -> list[dict] | None:
    """主动消息的 messages：最近聊天上下文 + 场景指令。

    random 场景拿不到真实弹药时返回 **None**（v0.89 §6）——调用方（core）据此
    **不调 LLM、不发**，而不是让模型自由发挥编一个"我刚刷手机看到…"。
    """
    prompt = persona.build_proactive_prompt(state, scenario)
    prompt[-1]["content"] += f"\n{_PROACTIVE_CONTEXT_RULE}"
    if scenario == "random" and not _inject_random_topic(prompt):
        return None
    recent = db.session_context(config.PROACTIVE_CONTEXT_MAX_MESSAGES)
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


def _inject_random_topic(prompt: list[dict]) -> bool:
    """random 场景的话题弹药：**从真实发生过的事里取一件**。返回有没有取到。

    v0.89 §6（治「车怠速」连发四条）：**六源皆空** → 返回 False →
    `build_proactive_messages` 返回 None → core **不调 LLM、不发**。
    这是**删除一条路径**（旧实现无弹药时让 LLM 自由发挥编环境），不是加闸门。

    **六源全序**（v0.97 批 3 补全序）：素材池（读到有触动的话，50% 概率）→ 刷到的
    （今天刷到还没说出去的）→ 待办（想说还没说的事）→ 承诺（我答应过还没兑现的）→
    兴趣（最近喜欢上的）→ 旧记忆。
    """
    import db as _db

    def _carry(todo_id: int) -> None:
        """§5 拍板 #6③：携带素材 id，消息发出后按 id 打勾（不再用字面指纹猜）。"""
        _db.set_kv("proactive_todo_id", str(todo_id or 0))

    if random.random() < 0.5:
        import life
        mat, todo_id = life.take_shareable_item()
        if mat:
            _carry(todo_id)
            prompt[-1]["content"] += (
                f"\n（你刚忙完手头的事，脑子里还留着这么一句："
                f"「{mat}」。可以自然跟TA聊起它——比如提一句你最近在读什么书、"
                f"读到哪句挺有感觉。想聊别的也行。）"
                f"\n（分享质量：聊这个要落在具体内容上，别泛泛、别硬扯生活。）")
            return True
    # 刷到的（v0.97 批 3）：今天刷到、还没说出去的一条——它自己今天看到的东西。
    # 排在「素材」之后、「待办」之前（六源全序：素材 → 刷到的 → 待办 → 承诺 → 兴趣 → 旧记忆）。
    if config.BROWSE_ENABLED:
        import sources as _src
        pick = next((r for r in _src.items_today(limit=20)
                     if not r.get("shared")), None)
        if pick:
            _db.set_kv("proactive_info_id", str(int(pick.get("id") or 0)))
            body = (pick.get("summary") or pick.get("title") or "").strip()
            prompt[-1]["content"] += (
                f"\n（你今天刷手机看到一条：{body}。"
                f"可以自然跟TA聊起它，聊一两句；想聊别的也行。）"
                f"\n（分享质量：聊这个要落在具体内容上，别泛泛、别硬扯生活。）")
            return True
    # 待办：脑子里想说、还没说的事（读过/想过攒下来的）
    if config.TODOS_ENABLED:
        import todos
        pend = todos.pending()
        if pend:
            item = pend[0]
            _carry(int(item.get("id") or 0))
            prompt[-1]["content"] += (
                f"\n（你心里一直搁着这么一件事，还没跟TA说过：「{item.get('text', '')}」。"
                f"现在提起它正合适，别生硬地带出。）")
            return True
    # 自我记忆：我答应过却还没兑现的事
    if config.SELF_MEMORY_ENABLED:
        import selfmem
        prom = [m for m in selfmem.recent("self_promise", limit=5)
                if (m.get("content") or "").strip()]
        if prom:
            prompt[-1]["content"] += (
                f"\n（你答应过TA一件事还没兑现：「{prom[0]['content']}」。"
                f"如果现在正合适，可以自然提起/补上；不合适就别说。）")
            return True
    import interests
    topic = interests.random_promoted()
    if topic:
        prompt[-1]["content"] += (
            f"\n（你最近对「{topic}」挺来劲——这是你从读/聊/刷里慢慢喜欢上的。"
            f"可以自然跟TA提起它，聊一两句；想聊别的也行。）")
        return True
    mem = memory.random_memory()
    if mem:
        anchor = mem.get("time_label") or ""
        if not anchor and mem.get("created_at"):
            anchor = f"{mem['created_at'][5:10]}记的"
        when = f"（{anchor}）" if anchor else ""
        prompt[-1]["content"] += (
            f"\n（你忽然想起关于TA的一件事{when}：「{mem['content']}」。"
            f"可以就着它聊，比如问问后续；想聊别的也行。）")
        return True
    return False

