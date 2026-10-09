"""关怀事件（S4「事件记忆与关怀触发」）：TA 记住对朋友的承诺/约定，到点提醒。

核心场景：朋友说"12点提醒我睡觉"→ TA 答应 → 记成一条关怀事件（带精确时间）。
  - 到点且醒着 → 主动提醒（履行承诺）
  - 到点但睡着了 → 不强行唤醒（作息优先，更真实），醒来补一句
    "昨晚说好提醒你，结果我睡着了"
  - 过期太久（>24h）→ 静默错过，不再翻旧账

数据：kv.care_events（JSON 数组），按朋友隔离（存在各自 friend.db）。
惰性 + 幂等：同 what+when 只记一条；关机重启后 kv 还在，接着算。
"""
import json
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
