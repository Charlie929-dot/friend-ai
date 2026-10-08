"""日记系统 + 每日回顾（P2）。

"今日对话回顾线"：当日对话全量记录（messages 表，role+content+ts），
区别于正常提取的长期记忆。每天睡前跑一次每日回顾：

1. 读当日完整对话（回顾线）
2. LLM 复盘：今天答得怎么样、有没有误解小徐的意思、聊天里的感受
3. 写日记（第一人称，有生活感）
4. 补记遗漏的新事实 / 提议删除记错的旧记忆（复用记忆系统的待确认删除机制）

写完日记后，回顾线（当日原始对话）可按开关 PURGE_REVIEWED_MESSAGES 彻底删除——
长期只保留提取的记忆 + 日记。默认不删（安全），想删时开开关。
"""
import json
import re
from datetime import datetime, timedelta

import config
import db
import llm
import persona

REVIEW_PROMPT = """你是{name}。夜深了，你在写今天的日记，顺便复盘自己今天过得怎么样。

会给你两样东西：
1. 【今日对话】——今天你和别人聊过的内容（可能只和一个人，也可能和一群人）
2. 【你脑子里已有的记忆】——供参考，别硬套

请你以{name}的第一人称完成，只输出一个 JSON（别输出其他任何文字）：
{{
  "reflection": "复盘：今天你自己过得怎么样？聊得开不开心、有没有误解别人的意思、你心里什么感受？（一两句内心话，第一人称）",
  "diary": "日记正文：以{name}的口吻写你自己今天的一天。要有生活感、有情感，别写成流水账总结。引用今天真实发生的事（和谁聊了什么、你的心情），可以结合你记得的旧事。150字以内。",
  "mood": "今天的心情词（两个字以内，如：开心/平静/有点累；没有就空字符串）",
  "memory_add": ["今天值得记住、但你之前可能漏记的新事实，一句话一条，第一人称视角（如『{user_name}说他下周要考试』）。没有就 []"],
  "memory_delete": ["你发现之前记错了、要作废的旧记忆关键词（原文里的关键片段）。没有就 []"]
}}

规则：
- 日记和复盘里不要出现"AI""助手""系统"这类字眼，你就是{name}本人。
- 只写今天真实发生的，别编造没发生的事。
- 没话可写也不要硬凑，reflection 可以很简单。"""


def _life_day_str(now: datetime | None = None) -> str:
    """当前生活日（睡一觉醒来才算新的一天）的日期字符串，日记按它归日。"""
    now = now or datetime.now()
    day, _ = persona._life_day(now)
    return day.strftime("%Y-%m-%d")


def today_transcript(day: str | None = None) -> str:
    """把当日完整对话渲染成文本（回顾线内容）。"""
    rows = db.today_messages(day)
    name = persona.friend_name()
    lines = []
    for r in rows:
        who = persona.user_name() if r["role"] == "user" else name
        lines.append(f"{who}：{r['content']}")
    return "\n".join(lines)


def _parse_json_object(raw: str) -> dict:
    """容错解析：模型偶尔给 JSON 包一层废话。"""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def daily_review(force: bool = False) -> str:
    """每日回顾 + 写日记。返回给人看的一句话结果。"""
    if not config.DIARY_ENABLED:
        return "日记功能未开启（DIARY_ENABLED=off）。"
    day = _life_day_str()
    if not force and db.get_diary(day):
        return f"今天（{day}）的日记已经写过啦，用 /diary 查看。"
    convo = today_transcript(day)
    if not convo.strip():
        return "今天还没怎么聊，没什么可写的。"
    name = persona.friend_name()
    known = [m["content"] for m in db.all_memories(limit=50)
             if m.get("status", "ok") == "ok"]
    user_msg = f"【今日完整对话】\n{convo}"
    if known:
        user_msg += "\n\n【你脑子里已有的记忆】\n" + "\n".join(f"- {x}" for x in known[:60])

    raw = llm.chat([
        {"role": "system", "content": REVIEW_PROMPT.format(name=name,
                                                           user_name=persona.user_name())},
        {"role": "user", "content": user_msg},
    ], temperature=0.7)
    data = _parse_json_object(raw)
    reflection = str(data.get("reflection", "")).strip()
    diary = str(data.get("diary", "")).strip()
    mood = str(data.get("mood", "")).strip()
    if not diary:  # 兜底：模型没按格式输出时，把原文当日记至少留个记录
        diary = raw.strip() or "（今天没写出日记）"

    db.add_diary(day, diary, reflection, mood)

    # 补记遗漏的新记忆（走 _apply 的确定性红线 + 去重）
    import memory
    added = 0
    for s in data.get("memory_add") or []:
        if isinstance(s, str) and memory._apply({"content": s, "category": "事件"}, False):
            added += 1
    # 提议删除记错的旧记忆：只标记待确认，用户 /delmem 拍板
    deleted = 0
    for kw in data.get("memory_delete") or []:
        if not isinstance(kw, str):
            continue
        old = db.find_memory_by_keyword(kw)
        if old and old.get("status", "ok") == "ok":
            db.set_memory_status(old["id"], "pending_delete")
            deleted += 1

    # 回顾线彻底删除（默认关）：写完日记后清掉当日原始对话
    purged = 0
    if config.PURGE_REVIEWED_MESSAGES:
        # 加 1 秒容差，确保"刚刚"写入的消息（同秒精度）也被清掉
        cutoff = (datetime.now() + timedelta(seconds=1)).isoformat(timespec="seconds")
        purged = db.purge_messages_before(cutoff)

    parts = [f"日记写好啦（{day}）"]
    if mood:
        parts.append(f"心情：{mood}")
    if added or deleted:
        parts.append(f"补记 {added} 条 / 提议删 {deleted} 条")
    if purged:
        parts.append(f"回顾线已清（{purged} 条）")
    return "，".join(parts)


def read_diary(day: str | None = None) -> str:
    """查看某天的日记（含复盘）。默认今天。"""
    day = day or _life_day_str()
    entry = db.get_diary(day)
    if not entry:
        return f"没有 {day} 的日记。"
    head = entry["date"] + (f"（{entry['mood']}）" if entry.get("mood") else "")
    lines = [head, entry["content"]]
    if entry.get("reflection"):
        lines.append(f"\n——复盘——\n{entry['reflection']}")
    return "\n".join(lines)


def list_diaries() -> list[str]:
    """所有写过日记的日期，新→旧。"""
    return db.list_diary_dates()
