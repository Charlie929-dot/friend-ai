"""关怀事件（S4「事件记忆与关怀触发」）：TA 记住对朋友的承诺/约定，到点提醒。

核心场景：朋友说"12点提醒我睡觉"→ TA 答应 → 记成一条关怀事件（带精确时间）。
  - 到点且醒着 → 主动提醒（履行承诺）
  - 到点但睡着了 → 不强行唤醒（作息优先，更真实），醒来补一句
    "昨晚说好提醒你，结果我睡着了"
  - 过期太久（>24h）→ 静默错过，不再翻旧账

数据：kv.care_events（JSON 数组），按朋友隔离（存在各自 friend.db）。
惰性 + 幂等：同 what+when 只记一条；关机重启后 kv 还在，接着算。

v0.103 批 3 起另立**日历**（kv.calendar_events）：承接「TA 自己的任务 / 用户说的计划 /
节日 / 生日」这类**确定时间**的事（与 care 的「明确时刻承诺」区分）——临期多触点
注入念头、聊过取消、带准备状态、年重复。见下方「日历」段。
"""
import json
import random
import re
from datetime import datetime, timedelta

import config
import db

_KEY = "care_events"


def _load() -> list[dict]:
    raw = db.get_kv(_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [x for x in data if isinstance(x, dict)]


def _save(items: list[dict]) -> None:
    db.set_kv(_KEY, json.dumps(items, ensure_ascii=False))


def _parse_when(when_str: str) -> datetime | None:
    """解析模型给的绝对时间（"YYYY-MM-DD HH:MM" 等几种格式）。"""
    when_str = (when_str or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(when_str, fmt)
        except ValueError:
            continue
    return None


def add_care(what: str, when_str: str, kind: str = "提醒") -> str | None:
    """记一条关怀事件（TA 对朋友的承诺）。未来才存、同 what+when 幂等。

    返回规范化的 when（"YYYY-MM-DD HH:MM"）；已存在 / 过期 / 解析失败返回 None。
    """
    what = (what or "").strip()
    when = _parse_when(when_str)
    if not what or when is None or when <= datetime.now():
        return None
    when_norm = when.strftime("%Y-%m-%d %H:%M")
    for it in _load():
        if it.get("what") == what and it.get("when") == when_norm:
            return None          # 已记过，幂等
    items = _load()
    items.append({
        "what": what,
        "when": when_norm,
        "kind": kind or "提醒",
        "status": "pending",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    _save(items)
    return when_norm


def cares() -> list[dict]:
    """所有关怀事件：pending 在前，其余按时间。"""
    items = _load()
    items.sort(key=lambda x: (x.get("status") != "pending", x.get("when", "")))
    return items


def pending_cares() -> list[dict]:
    return [x for x in _load() if x.get("status") == "pending"]


def _due(now: datetime) -> list[dict]:
    out = []
    for it in _load():
        if it.get("status") != "pending":
            continue
        w = _parse_when(it.get("when", ""))
        if w and w <= now:
            out.append(it)
    return out


def _set_status(ev: dict, status: str) -> None:
    items = _load()
    for it in items:
        if it.get("what") == ev.get("what") and it.get("when") == ev.get("when"):
            it["status"] = status
    _save(items)


def _fire_due(engine, missed: bool) -> None:
    """把到点的关怀事件交给 engine 履行（提醒 / 醒来补）。"""
    now = datetime.now()
    for ev in _due(now):
        w = _parse_when(ev.get("when", ""))
        if w and now - w > timedelta(hours=24):
            _set_status(ev, "missed")       # 过期太久，提醒已无意义，静默错过
            continue
        if engine.care_remind(ev, missed=missed):
            _set_status(ev, "done")
            # 统一事件源（§5 拍板 #6④）：承诺到点 = 一件"发生在我身上的事"，
            # 往意识流注入条目，与待办/用户消息同一机制、无需单独逻辑。
            try:
                import stream
                stream.append("care", f"到点了，我答应过要提醒TA：{ev.get('what', '')}")
            except Exception:
                pass
        # 生成失败（LLM 不可用 / 熔断）保持 pending，下次再试


def tick(engine) -> None:
    """调度器每分钟调：到点且醒着 → 履行承诺；睡着跳过（醒来再补）。"""
    if not config.CARE_ENABLED:
        return
    if db.get_state() == "SLEEPING":
        return
    _fire_due(engine, missed=False)


def flush_missed(engine) -> None:
    """醒来时调：把昨晚/之前到点没发的关怀补上（带"结果我睡着了"语境）。"""
    if not config.CARE_ENABLED:
        return
    _fire_due(engine, missed=True)


# ============================================================================
# 日历（v0.103 批 3「念头驱动」）：确定时间的事 = 日历/日程，不是「提醒便签」
# ----------------------------------------------------------------------------
# 与 care 的区别：care = TA 亲口答应的「明确时刻承诺」（到点提醒，闹钟式但口语化）；
# 日历 = TA 自己的任务 / 用户说的计划 / 节日 / 生日（临期多触点、聊过取消、
# 带准备状态、年重复）。开口形态是**念头**：到点注入意识流 → 用自己的话聊近况
# （了解近况 + 提醒，不是闹钟式推送）。
# ============================================================================

_CAL_KEY = "calendar_events"

# 公历内置节日（月-日 → 名称）。农历不做（中秋这类手工建）。
BUILTIN_HOLIDAYS = [
    ("01-01", "元旦"),
    ("02-14", "情人节"),
    ("05-01", "劳动节"),
    ("06-01", "儿童节"),
    ("10-01", "国庆节"),
    ("12-25", "圣诞节"),
]

# 临期触点的提前量（分钟）：前一周 / 前三天 / 前一天 / 几小时，从大到小依次取
# 第一个落在未来的（多触点：一轮触完推进到更近的一轮）。
_CAL_LEADS_MINUTES = (7 * 24 * 60, 3 * 24 * 60, 24 * 60, 3 * 60)


def _load_cal() -> list[dict]:
    raw = db.get_kv(_CAL_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [x for x in data if isinstance(x, dict) and x.get("what")]


def _save_cal(items: list[dict]) -> None:
    db.set_kv(_CAL_KEY, json.dumps(items, ensure_ascii=False))


def _random_time(t: datetime) -> datetime:
    """提醒日当天随机时间触发（了解近况的体感，不是闹钟定点）。"""
    return t.replace(hour=random.randint(9, 21), minute=random.randint(0, 59),
                     second=0, microsecond=0)


def _event_datetime(ev: dict, now: datetime) -> datetime | None:
    """事件的下一个发生时刻：非年重复解析 when；年重复取今年（或明年）的月-日 09:00。"""
    if ev.get("annual"):
        m, d = int(ev.get("month") or 0), int(ev.get("day") or 0)
        if not (1 <= m <= 12 and 1 <= d <= 31):
            return None
        for year in (now.year, now.year + 1):
            try:
                dt = datetime(year, m, d, 9, 0)
            except ValueError:
                continue
            if dt > now:
                return dt
        return None
    return _parse_when(ev.get("when", ""))


def _next_touch_for(ev: dict, now: datetime) -> str:
    """算下一个触点（ISO 串）：提前量里第一个落未来的，都没有就落在事件当天。
    事件已过（非年重复）返回空串——由调用方标记 done。"""
    d = _event_datetime(ev, now)
    if not d or d <= now:
        return ""
    for lead in _CAL_LEADS_MINUTES:
        t = d - timedelta(minutes=lead)
        if t > now:
            return _random_time(t).isoformat(timespec="seconds")
    return _random_time(d).isoformat(timespec="seconds")


def add_calendar(what: str, when_str: str = "", kind: str = "task",
                 prepare: str = "", annual: bool = False,
                 now: datetime | None = None) -> str | None:
    """记一条日历项（TA 的任务 / 用户说的计划 / 节日 / 生日）。

    - 非年重复：when_str = "YYYY-MM-DD HH:MM"，未来才存，同 what 幂等。
    - 年重复：when_str = "MM-DD"，生成今年（或明年）的月-日事件。
    返回规范化的 when；不合法 / 已存在 / 已过期返回 None。
    """
    what = (what or "").strip()
    if not what:
        return None
    now = now or datetime.now()
    ev = {"what": what, "kind": kind or "task", "prepare": (prepare or "").strip(),
          "status": "pending", "annual": bool(annual), "talked": 0,
          "created_at": now.isoformat(timespec="seconds")}
    if annual:
        m = re.match(r"^(\d{1,2})-(\d{1,2})$", (when_str or "").strip())
        if not m:
            return None
        ev["month"], ev["day"] = int(m.group(1)), int(m.group(2))
        d = _event_datetime(ev, now)
        if not d:
            return None
        ev["when"] = d.strftime("%Y-%m-%d %H:%M")
    else:
        d = _parse_when(when_str)
        if d is None or d <= now:
            return None
        ev["when"] = d.strftime("%Y-%m-%d %H:%M")
    ev["next_touch"] = _next_touch_for(ev, now)
    items = _load_cal()
    for it in items:
        if it.get("what") == what and bool(it.get("annual")) == bool(annual):
            return None          # 同 what 幂等
    items.append(ev)
    _save_cal(items)
    return ev["when"]


def set_prepare(what: str, prepare: str) -> bool:
    """准备状态随对话更新（如"ppt 汇报——还没开始 / 在做了"）。返回是否命中。"""
    what = (what or "").strip()
    if not what:
        return False
    items = _load_cal()
    hit = False
    for it in items:
        if it.get("what") == what:
            it["prepare"] = (prepare or "").strip()
            hit = True
    if hit:
        _save_cal(items)
    return hit


def mark_talked(what: str) -> bool:
    """聊过取消：对话里已经聊到这件事 → 取消近期未触的提醒（清 next_touch）。"""
    what = (what or "").strip()
    if not what:
        return False
    items = _load_cal()
    hit = False
    for it in items:
        if it.get("what") == what:
            it["talked"] = 1
            it["next_touch"] = ""
            hit = True
    if hit:
        _save_cal(items)
    return hit


def calendar_events() -> list[dict]:
    """所有日历项：pending 在前，其余按时间。"""
    items = _load_cal()
    items.sort(key=lambda x: (x.get("status") != "pending", x.get("when", "")))
    return items


def pending_calendar() -> list[dict]:
    return [x for x in _load_cal() if x.get("status") == "pending"]


def ensure_birthday(now: datetime | None = None) -> str | None:
    """生日年重复：从 profile 的「生日」字段自动生成年事件（幂等）。

    profile 是「字段：值」文本，找含「生日」的行并解析 "MM-DD"。找不到返回 None。
    """
    now = now or datetime.now()
    try:
        import persona
        lines = (persona._read_profile() or "").splitlines()
    except Exception:
        return None
    for line in lines:
        if "生日" not in line:
            continue
        m = re.search(r"(\d{1,2})[-/.月](\d{1,2})", line)
        if not m:
            continue
        return add_calendar("TA的生日", f"{m.group(1)}-{m.group(2)}",
                            kind="birthday", annual=True, now=now)
    return None


def _parse_touch(ev: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(ev.get("next_touch", ""))
    except (ValueError, TypeError):
        return None


def _advance_touch(ev: dict, now: datetime) -> None:
    """一轮触点触完 → 推进到下一个更近的触点；事件已过则 done（年重复推进到下一年）。"""
    items = _load_cal()
    ev["next_touch"] = _next_touch_for(ev, now + timedelta(minutes=1))
    if not ev["next_touch"] and not ev.get("annual"):
        ev["status"] = "done"
    for it in items:
        if it.get("what") == ev.get("what") and bool(it.get("annual")) == bool(ev.get("annual")):
            it.update(ev)
    _save_cal(items)


def calendar_tick(engine) -> None:
    """调度器每分钟调：临期触点到点 → 注入念头 + 主动开口（了解近况 + 提醒，非闹钟）。

    聊过取消（talked）的事件不再触；睡着跳过（念头会顺延，因为 next_touch 没被推进）。
    """
    if not config.CALENDAR_ENABLED:
        return
    if db.get_state() == "SLEEPING":
        return
    now = datetime.now()
    for ev in pending_calendar():
        if ev.get("talked"):
            continue
        nt = _parse_touch(ev)
        if not nt or nt > now:
            continue
        try:
            import stream
            stream.append("calendar", f"惦记着TA的一件事：{ev.get('what', '')}")
        except Exception:
            pass
        db.set_kv("calendar_pending", json.dumps(ev, ensure_ascii=False))
        engine.proactive("calendar")
        db.set_kv("calendar_pending", "")
        _advance_touch(ev, now)

