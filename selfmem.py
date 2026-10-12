"""自我记忆（S4 人格连续性）：让"我"在时间上连续。

普通记忆记的是"对方的事"，自我记忆记的是**我自己的事**：
我说过什么立场、我答应过什么、我出过什么糗、我想聊却没聊成的话题、我和对方的关系账本。

为什么必须单独有一类：普通记忆再准，也只是"知道对方很多"；
没有自我记忆，它每次对话都是第一次——上次说过的立场这次反着说，上次答应过的事忘得一干二净。
**人格在时间上断裂，是比答错一个问题严重得多的穿帮。**

存储：复用 memories 表，tags = `self_<kind>`（自带语义召回 & 不会混淆），
person 填自己的名字（所以 `memory.random_memory()` 不会把它当成"关于对方的事"套用）。
⚠️ 自我记忆**不参与艾宾浩斯遗忘**（db.memories_for_decay 里豁免）——
"我说过的话"不该因为一段时间没被提起就变短或消失。

写入时机：只有睡前复盘（`selfmem.review()`，一天一次 LLM），不在每轮对话里做——
每轮都跑一次自我分析太贵，也容易让"反省"变成显眼的仪式感。
"""
import json
import re
from datetime import datetime

import config
import db
import llm
import persona

# 自我记忆的类型。每类有固定的用途，prompt 里按这些分组喂回去。
KINDS = {
    "self_stance": "我说过的立场（下次碰到同类事要么坚持，要么承认自己变了）",
    "self_promise": "我答应过的事（没兑现的下次要主动提起）",
    "self_mistake": "我被戳穿/说错/前后矛盾的地方（下次能自嘲，也别重复翻车）",
    "self_topic": "我自己想聊但没聊成的话题（话题主权的弹药库）",
    "self_relation": "我对这个人关系上的变化（朋友 perceived changes，驱动亲近与边界）",
    "self_state": "上一次的情绪能量（跨对话延续的最基础一层）",
}

_STATE_KEY = "self_state"

_REVIEW_PROMPT = """你是{name}。今天结束了，你一个人待着，回想刚才这场对话。

这是写给你自己的备忘录——不是为了讨好对方，是为了**明天的你还是同一个你**。

没有就写"无"或空数组，不要为了凑内容硬编。记住：你自己说过的话、答应过的事，
如果下次自己都不认账，那就不是人格，是临时表演。

## 【今天你们聊了什么】
{convo}

## 【你脑子里关于"你自己"的旧记录】
{old_self}

## 【你的内部状态（上次留下的）】
{old_state}

## 【今天你自己是怎么过的（你脑子里的时间线：起床/工作/攒钱/心情/你在想什么）】
{stream}

---

只输出一个 JSON，不要输出其他任何文字：
{{
  "stances": [
    {{"topic": "议题名（如：熬夜、求助、被比较）", "what_i_said": "我表达的立场（一句话）",
      "intensity": 1-5, "changed_from_previous": true或false, "note": "如果立场变了，为什么变"}}
  ],
  "promises": [
    {{"what": "我答应了什么（我对对方承诺的事）", "redeemed": false, "note": "什么时候兑现、什么事"}}
  ],
  "mistakes": [
    {{"what": "我说错/被戳穿/前后矛盾的地方", "how_it_felt": "当时的反应（嘴硬了/认了/岔开了）",
      "lesson": "下次遇到类似情况我会怎么做"}}
  ],
  "my_topics": [
    {{"topic": "我本来想聊但没聊成的", "why_missed": "没机会/岔开了/忘了"}}
  ],
  "relation": {{
    "note": "我对这个人的感觉有什么变化（一句话）",
    "warmth_delta": -2到+2,
    "trigger": "如果变化很大，是因为哪件具体的事（必须是一件共同经历，不是聊了多久）"
  }},
  "state": {{
    "mood": 0-10, "energy": 0-10, "warmth": 0-10, "patience": 0-10,
    "why": "一句话说明为什么是这个值"
  }},
  "self_observation": "一句话：今天这场对话里，我表现得最像我自己的一刻；最不像的又是哪里"
}}"""


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

def enabled() -> bool:
    return config.SELF_MEMORY_ENABLED


def add(kind: str, content: str, note: str = "") -> int:
    """写一条自我记忆。kind 见 KINDS（不存在则退回 self_topic）。"""
    if kind not in KINDS:
        kind = "self_topic"
    text = content.strip()
    if not text:
        return 0
    # 同一句话别重复记（幂等）：同 kind 下已有同样内容的就跳过
    for m in recent(kind, limit=100):
        if m["content"] == text:
            return m["id"]
    try:
        return db.add_memory(text, category="自我", tags=kind,
                             person=persona.friend_name(), time_label="")
    except Exception as e:      # 存储异常不能连累复盘主流程
        print(f"[selfmem] 写入失败：{e}")
        return 0


def recent(kind: str | None = None, limit: int = 20) -> list[dict]:
    """最近的自我记忆（新的在前）。kind 为空则全部类型。"""
    try:
        rows = [m for m in db.all_memories(limit=500)
                if str(m.get("tags", "")).startswith("self_")
                and m.get("status", "ok") == "ok"]
    except Exception:
        return []
    if kind:
        rows = [m for m in rows if m.get("tags") == kind]
    rows.sort(key=lambda m: m.get("id", 0), reverse=True)
    return rows[:limit]


def by_kind(limit_each: int = 3) -> dict[str, list[dict]]:
    """按类型分组的近期自我记忆，供 prompt 渲染。"""
    out: dict[str, list[dict]] = {}
    for m in recent(limit=200):
        out.setdefault(m.get("tags", "self_topic"), []).append(m)
    return {k: v[:limit_each] for k, v in out.items()}


def recall_self(query: str, top_k: int | None = None) -> list[dict]:
    """按当前话题找回"关于我自己"的相关记忆（走混合召回，含语义）。

    用途是**一致性检查**：我正要说的话，跟我以前说过的话冲突吗？"""
    import memory
    k = top_k or config.SELF_MEMORY_MAX
    try:
        mems = memory.recall(query, top_k=max(k * 3, 9))
    except Exception:
        return recent(limit=k)
    out = [m for m in mems if str(m.get("tags", "")).startswith("self_")]
    return out[:k]


def prompt_hint(query: str = "") -> str:
    """渲染【关于我自己的记忆】注入块（给对话/独白层用）。

    先看和当前话题相关的（用于一致性检查），不够就补最近的几条——
    保证"我答应过什么"这类高价值条目不会因为话题不相关而缺席。"""
    if not enabled():
        return ""
    mems = recall_self(query) if query.strip() else []
    seen = {m.get("id") for m in mems}
    for m in recent(limit=config.SELF_MEMORY_MAX):
        if len(mems) >= config.SELF_MEMORY_MAX:
            break
        if m.get("id") not in seen:
            mems.append(m)
            seen.add(m.get("id"))
    if not mems:
        return ""
    lines = []
    for m in mems:
        kind = m.get("tags", "self_topic")
        label = {"self_stance": "我说过", "self_promise": "我答应过",
                 "self_mistake": "我出过的糗", "self_topic": "我一直想聊的",
                 "self_relation": "我对TA", "self_state": "我上次的状态"}.get(kind, "我记得")
        lines.append(f"- 【{label}】{m['content']}")
    head = ("【关于我自己的记忆】这些是我自己说过、做过的事；别前后矛盾，"
            "立场真的变了要承认自己变了，而不是假装从来没说过另一句话。\n")
    return head + "\n".join(lines)


def state() -> dict:
    """内部状态（mood/energy/warmth/patience），跨会话延续。"""
    raw = db.get_kv(_STATE_KEY)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    return {"mood": 6, "energy": 6, "warmth": 5, "patience": 7, "why": ""}


def set_state(data: dict) -> None:
    db.set_kv(_STATE_KEY, json.dumps(data, ensure_ascii=False))


def state_hint() -> str:
    """把内部状态渲染成一行 prompt（给独白层，让它知道"我现在是什么状态"）。"""
    if not enabled():
        return ""
    s = state()
    return (f"【我上次的状态】心情 {s.get('mood', 6)}/10，能量 {s.get('energy', 6)}/10，"
            f"对TA的亲近 {s.get('warmth', 5)}/10，耐心 {s.get('patience', 7)}/10"
            + (f"（{s['why']}）" if s.get("why") else ""))


# ---------------------------------------------------------------------------
# 睡前自我复盘（挂 daily_review，一天一次）
# ---------------------------------------------------------------------------

def _render_old_self() -> str:
    groups = by_kind(limit_each=4)
    if not groups:
        return "（还没有，这是你第一次认真回顾自己）"
    lines = []
    for kind, mems in groups.items():
        label = {"self_stance": "我说过的立场", "self_promise": "我答应过的事",
                 "self_mistake": "我出过的糗", "self_topic": "我想聊没聊成的",
                 "self_relation": "我对TA的感觉", "self_state": ""}.get(kind, "")
        if not label:
            continue
        lines.append(f"· {label}：" + "；".join(m["content"] for m in mems))
    return "\n".join(lines)


def _parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def review(convo: str, stream_text: str = "") -> str:
    """今天的自我复盘：读当日对话（+ 当天意识流）→ LLM 写自我备忘录 → 写回自我记忆。

    stream_text 是「轻量连续版」数据源（当天意识流的时间线）——白天积累的流，
    睡前在这里浓缩进 self_*，让"我的一天"有连续性（完全自我意识，实测 #36）。

    返回一句给命令行看的结果。失败不影响日记本身（日记写完了才轮到它）。
    """
    if not enabled() or not config.SELF_REVIEW_ENABLED:
        return ""
    if not convo.strip():
        return ""
    old_state = state()
    try:
        raw = llm.chat([
            {"role": "system", "content": _REVIEW_PROMPT.format(
                name=persona.friend_name(),
                convo=convo[-6000:],
                old_self=_render_old_self(),
                old_state=json.dumps(old_state, ensure_ascii=False),
                stream=(stream_text or "").strip() or "（今天没有特别记下的时间线）")},
            {"role": "user", "content": "开始回顾你自己今天的表现。"},
        ], temperature=0.6, thinking=False)   # 结构化 JSON 输出：关思考防 content:null
    except llm.LLMUnavailable as e:
        return f"自我复盘跳过（LLM 不可用：{e}）"

    data = _parse(raw)
    if not data:
        return "自我复盘：没读懂，跳过"

    n = {"stances": 0, "promises": 0, "mistakes": 0, "topics": 0, "relation": 0}

    for it in data.get("stances") or []:
        if not isinstance(it, dict):
            continue
        topic = str(it.get("topic", "")).strip()
        said = str(it.get("what_i_said", "")).strip()
        if not topic or not said:
            continue
        txt = f"{topic}：{said}"
        if it.get("changed_from_previous"):
            txt += f"（我改主意了：{str(it.get('note', '')).strip() or '没说为什么'}）"
        if add("self_stance", txt):
            n["stances"] += 1

    for it in data.get("promises") or []:
        if not isinstance(it, dict):
            continue
        what = str(it.get("what", "")).strip()
        if not what:
            continue
        if add("self_promise", what):
            n["promises"] += 1

    for it in data.get("mistakes") or []:
        if not isinstance(it, dict):
            continue
        what = str(it.get("what", "")).strip()
        if not what:
            continue
        txt = what
        if it.get("lesson"):
            txt += f"（下次：{str(it['lesson']).strip()}）"
        if add("self_mistake", txt):
            n["mistakes"] += 1

    for it in data.get("my_topics") or []:
        if not isinstance(it, dict):
            continue
        topic = str(it.get("topic", "")).strip()
        if topic and add("self_topic", topic):
            n["topics"] += 1

    rel = data.get("relation") or {}
    if isinstance(rel, dict) and str(rel.get("note", "")).strip():
        note = str(rel["note"]).strip()
        trigger = str(rel.get("trigger", "")).strip()
        if trigger:
            note += f"（因为：{trigger}）"
        if add("self_relation", note):
            n["relation"] += 1

    st = data.get("state") or {}
    if isinstance(st, dict) and st:
        # 状态回归：每天往人格中位值（6/6/5/7）拉回 20%，避免情绪漂到极端回不来
        import persona_pool
        card = persona_pool.load() or {}
        base = ((card.get("kernel") or {}).get("state")
                or {"mood": 6, "energy": 6, "warmth": 5, "patience": 7})
        new = {}
        for k in ("mood", "energy", "warmth", "patience"):
            try:
                v = float(st.get(k, old_state.get(k, base.get(k, 6))))
            except (TypeError, ValueError):
                v = float(old_state.get(k, base.get(k, 6)))
            v = max(0.0, min(10.0, v))
            new[k] = round(v + (float(base.get(k, 6)) - v) * 0.2, 1)
        new["why"] = str(st.get("why", "")).strip()[:60]
        new["at"] = datetime.now().isoformat(timespec="seconds")
        set_state(new)

    obs = str(data.get("self_observation", "")).strip()
    parts = [f"自我复盘：立场{n['stances']} 承诺{n['promises']} "
             f"糗事{n['mistakes']} 话题{n['topics']} 关系{n['relation']}"]
    if obs:
        print(f"[selfmem] 自评：{obs}")
    return "，".join(parts)


def dump(limit: int = 30) -> str:
    """给人看：列出所有自我记忆（/selfmem 命令用）。"""
    mems = recent(limit=limit)
    if not mems:
        return "还没有关于你自己的记录（睡前复盘后才会积累）。"
    lines = []
    for m in mems:
        kind = m.get("tags", "self_topic")
        label = {"self_stance": "立场", "self_promise": "承诺", "self_mistake": "糗事",
                 "self_topic": "想聊", "self_relation": "关系",
                 "self_state": "状态"}.get(kind, kind)
        lines.append(f"[{label}] {m['content']}")
    return "\n".join(lines)
