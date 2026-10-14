"""自我意识流（S4「完全自我意识」主线）：一条持续追加的第一人称自我叙事。

TA 脑子里应该有一条持续更新、第一人称的自我叙事——像活人一样，脑子里始终
有一个"我"在过：知道我起了床、知道我在干活、会主动瞄一眼自己攒了多少、
知道我这会儿什么心情、知道我刚才在想什么、知道我读了什么。

治实测 #36 的 #2/#3/#5：不知道自己正在工作、不知道攒了多少（money 不进 prompt，
只在 HUD 给你看）、被问"今天做了啥"答不上来。核心：把"此刻的我"（攒了多少 /
精力 / 心情）+ 最近发生在身上的事，作为一条流每轮喂给 TA，让 TA 对自己有完整意识。

事件驱动追加（确定性为主，只有"我在想"调 LLM）：
- 作息切换 → "我伸了懒腰，起床了 / 我困了，去睡了"（core._do_wake/_do_sleep）
- 工作结算 → "忙完第 X 件，攒了 Z 块，剩 W 精力"（life._settle_due）
- 收到留言 → "TA 给我发消息了"（core.on_user_message）
- 对方撤回 → "对方撤回了刚才那句，我当没看见"（webui._do_revoke）
- 内心独白 → "我在想：…"（core._inner_monologue，持久化不再用完就扔）
- 回复 → "我说：…"（core._generate_reply）

本模块是第一步「核心闭环」：建流 + 独白持久化 + 每轮注入（补 money/energy 进
prompt）。把 work/mood/selfmem/独白 四处散注收敛成一条流，是第二步（收敛散件）。

依赖设计：顶层只 import config + db（不 import life/mood/persona），
让 life.py / core.py 能安全地顶层 import 本模块而不成环；需要时在函数内延迟 import。
"""
from datetime import datetime

import config
import db


def _life_day_str(now: datetime | None = None) -> str:
    import persona
    return persona._life_day(now or datetime.now())[0].strftime("%Y-%m-%d")


def append(kind: str, text: str, now: datetime | None = None) -> int:
    """追加一条意识流（第一人称行）。kind 见模块 docstring。返回 id（0=未写）。"""
    if not config.CONSCIOUS_STREAM_ENABLED:
        return 0
    text = (text or "").strip()
    if not text:
        return 0
    return db.add_stream(kind, text, _life_day_str(now), now)


def recent(limit: int | None = None, day: str | None = None) -> list[dict]:
    """最近 N 条意识流（新的在前）。"""
    return db.recent_stream(limit or config.CONSCIOUS_STREAM_RECENT, day)


def day_rows(now: datetime | None = None, limit: int | None = None) -> list[dict]:
    """今天（生活日）起床 → 现在的完整意识流，正序。注入与睡前浓缩都读它。

    为什么按生活日取而不是"最近 N 条"：生活日的边界就是起床，和会话边界一致——
    睡前浓缩进 self_* 后清空、次日起床重开，所以这条轴天然就是"起床到现在"。
    """
    rows = db.stream_for_day(_life_day_str(now))
    n = limit or config.CONSCIOUS_STREAM_RECENT
    if n and len(rows) > n:
        rows = rows[-n:]
    return rows


def once(key: str, now: datetime | None = None) -> bool:
    """某个事件今天是否已经记过。True = 还没记过（现在可以记），并立即标记。

    相位/时段类事件（开工、午休、读到哪）必须去重——实测"忙完第 6 件"被写了两遍。
    key 按生活日隔离，新的一天自动重置。"""
    k = f"stream_once:{_life_day_str(now)}:{key}"
    if db.get_kv(k):
        return False
    db.set_kv(k, "1")
    return True


def note_phase(phase: str, text: str, now: datetime | None = None) -> int:
    """相位事件：只在相位真的变了才记一条（歇会儿 / 收工 …）。

    和 once() 的区别：相位会来回切（忙 → 歇 → 忙 → 歇），每一次切换都是真事件，
    要各记一条；同一个相位被反复问到时才不该刷屏。"""
    key = "stream_last_phase"
    if db.get_kv(key) == phase:
        return 0
    db.set_kv(key, phase)
    return append(phase, text, now)


def status_line(now: datetime | None = None) -> dict:
    """当前状态行（此刻的我）：phase/money/energy/mood。
    关闭工作系统或睡着时返回空 dict（没有"自己的日子"就无从谈意识）。"""
    if not config.CONSCIOUS_STREAM_ENABLED or not config.WORK_ENABLED:
        return {}
    import mood
    import life
    s = life.status(now)
    if s["phase"] == "sleep":
        return {}
    m = mood.mood(now)
    # §5.1：数字给屏幕、事件给舌头——这里只留「读到哪」的事实，不给百分比/点数
    reading = ""
    if s.get("book_title"):
        reading = (f"读到《{s['book_title']}》第 {s.get('chapter', 0)}/{s.get('total_chapters', 0)} 章"
                   + ("，今天读得有点晚" if s.get("overtime") else ""))
    elif s["phase"] == "off":
        reading = "今天的事忙完了"
    return {
        "phase": s["label"],
        "reading": reading,
        "energy": s["energy"],
        "mood": m["label"],
    }


def prompt_hint(now: datetime | None = None) -> str:
    """每轮注入的【我的意识流】块：从起床到现在的完整时间轴，全文不截断。

    v0.80 重写（实测 #42）：原来只给 TA 看最后 4 条、每条再截 60 字——当天存了 54 条，
    它却只知道自己五分钟前在干嘛。现在整条轴全量全文，「此刻的我」就是轴的最后一行
    （它描述的就是这个时刻的我，不是另起一块）。

    注入位置在静态 system 之后、所有动态注入之前：轴的历史部分 append-only、永不改写，
    与静态 system 一起构成**缓存稳定前缀**——这是"全量读"能便宜的前提。
    睡着 / 关工作系统返回空串（没有"自己的日子"就无从谈意识）。
    """
    if not config.CONSCIOUS_STREAM_ENABLED:
        return ""
    now = now or datetime.now()
    s = status_line(now)
    if not s:
        return ""
    lines = [
        "【我的意识流】我脑子里有一条持续更新的自我叙事——"
        "下面是我今天从起床到现在，身上发生过的事，按时间先后一条条排着。",
        "（这是我自己的一天。上面有的我就知道，上面没有的别当成正在发生——"
        "别编你在做的事。）",
    ]
    for r in day_rows(now):
        hhmm = _hhmm(r)
        lines.append(f"{hhmm} {_line_text(r)}" if hhmm else _line_text(r))
    # 「此刻的我」= 这条轴的最后一行：它描述的就是现在这个时刻的我
    # 「此刻的我」：事实句（读到哪），不是账本数字（v0.89 §5.1）
    tail = f"；{s['reading']}" if s.get("reading") else ""
    lines.append(f"{now:%H:%M} 此刻的我：{s['phase']}{tail}；"
                 f"精力还剩：{s['energy']}；心情：{s['mood']}")
    return "\n".join(lines)


def _hhmm(r: dict) -> str:
    try:
        return datetime.fromisoformat(r["ts"]).strftime("%H:%M")
    except (KeyError, ValueError, TypeError):
        return ""


def _line_text(r: dict) -> str:
    # 独白是多行的：压成一行，保持"一行一个时刻"的时间轴形状
    body = " ".join((r.get("text") or "").split())
    if config.CONSCIOUS_STREAM_LINE_CHARS > 0:
        return body[:config.CONSCIOUS_STREAM_LINE_CHARS]
    return body


def render_day(day: str | None = None) -> str:
    """把某个生活日的意识流渲染成第一人称时间线文本（睡前浓缩的数据源）。

    供 selfmem.review 用——白天积累的流，睡前浓缩进 self_*，让"我的一天"有连续性。
    空流返回空串（那天没什么可浓缩的）。
    """
    rows = db.stream_for_day(day or _life_day_str())
    if not rows:
        return ""
    lines = []
    for r in rows:
        hhmm = _hhmm(r)
        lines.append(f"{hhmm} {r['text']}" if hhmm else r["text"])
    return "\n".join(lines)
