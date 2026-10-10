"""自我意识流（S4「完全自我意识」主线）：一条持续追加的第一人称自我叙事。

TA 脑子里应该有一条持续更新、第一人称的自我叙事——像活人一样，脑子里始终
有一个"我"在过：知道我起了床、知道我在干活、会主动瞄一眼自己攒了多少、
知道我这会儿什么心情、知道我刚才在想什么、知道我读了什么。

治实测 #36 的 #2/#3/#5：不知道自己正在工作、不知道攒了多少（money 不进 prompt，
只在 HUD 给你看）、被问"今天做了啥"答不上来。核心：把"此刻的我"（攒了多少 /
精力 / 心情）+ 最近发生在身上的事，作为一条流每轮喂给 TA，让 TA 对自己有完整意识。

事件驱动追加（确定性为主，只有"我在想"调 LLM）：
- 作息切换 → "我伸了懒腰，起床了 / 我困了，去睡了"（core._do_wake/_do_sleep）
- 工作结算 → "忙完第 X 件，攒了 Z 块，剩 W 精力"（work._settle）
- 收到留言 → "TA 给我发消息了"（core.on_user_message）
- 内心独白 → "我在想：…"（core._inner_monologue，持久化不再用完就扔）
- 回复 → "我说：…"（core._generate_reply）

本模块是第一步「核心闭环」：建流 + 独白持久化 + 每轮注入（补 money/energy 进
prompt）。把 work/mood/selfmem/独白 四处散注收敛成一条流，是第二步（收敛散件）。

依赖设计：顶层只 import config + db（不 import work/mood/persona），
让 work.py / core.py 能安全地顶层 import 本模块而不成环；需要时在函数内延迟 import。
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
    return db.add_stream(kind, text, _life_day_str(now))


def recent(limit: int | None = None, day: str | None = None) -> list[dict]:
    """最近 N 条意识流（新的在前）。"""
    return db.recent_stream(limit or config.CONSCIOUS_STREAM_RECENT, day)


def status_line(now: datetime | None = None) -> dict:
    """当前状态行（此刻的我）：phase/money/energy/mood。
    关闭工作系统或睡着时返回空 dict（没有"自己的日子"就无从谈意识）。"""
    if not config.CONSCIOUS_STREAM_ENABLED or not config.WORK_ENABLED:
        return {}
    import mood
    import work
    s = work.status(now)
    if s["phase"] == "sleep":
        return {}
    m = mood.mood(now)
    return {
        "phase": s["label"],
        "money": s["money"],
        "energy": s["energy"],
        "mood": m["label"],
    }


def prompt_hint(now: datetime | None = None) -> str:
    """每轮注入的【我的意识流】块：此刻的我（含 money/energy/mood）+ 最近几条流。

    只补 work.prompt_hint 缺的部分（攒了多少/精力/心情）——phase 和"正在读什么"
    仍由 work.prompt_hint 注入（收敛是第二步的事），这里不重复。睡着/关闭返回空串。
    """
    if not config.CONSCIOUS_STREAM_ENABLED:
        return ""
    s = status_line(now)
    if not s:
        return ""
    lines = [
        "【我的意识流】你脑子里有一条持续更新的自我叙事——此刻的你，心里有数：",
        f"- 攒了多少：{s['money']} 块（这是你自己挣的，你清楚自己有多少）",
        f"- 精力还剩：{s['energy']}",
        f"- 心情：{s['mood']}",
    ]
    rows = recent()
    if rows:
        lines.append("")
        lines.append("最近发生在你身上的事（你自己的时间线，从早到晚）：")
        for r in reversed(rows):  # 正序展示，像一条在流动的时间线
            lines.append(f"- {_hhmm(r)} {_line_text(r)}")
    return "\n".join(lines)


def _hhmm(r: dict) -> str:
    try:
        return datetime.fromisoformat(r["ts"]).strftime("%H:%M")
    except (KeyError, ValueError, TypeError):
        return ""


def _line_text(r: dict) -> str:
    body = (r.get("text") or "").strip()
    return body[:config.CONSCIOUS_STREAM_LINE_CHARS]
