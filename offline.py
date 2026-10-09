"""离线与作息节律（S3「休眠完整版」）：它什么时候不在线、什么时候在。

"离线"的语义（用户 2026-10-09 拍板）——不是"随机消失"，而是和它的日子挂钩：
  - 工作单元进行中（心流 flow / 专注 working）= 离线：它在认真做自己的事，不秒回
  - 夜间睡眠（SLEEPING）= 离线
  - 单元间隙（break）/ 开工前（idle）/ 忙完（off）= 在线
离线时你发的消息攒进 inbox（收下不丢），它"上线"（做完这段 / 睡醒）后正经回应。

作息自主调节：入睡/起床围绕人格卡锚点浮动（忙完/累 → 早睡，精力足没忙完 → 熬夜；
睡得晚 → 赖床）。惰性推算 + kv 幂等缓存——电脑关机重启也能接着算。

配合 core 的 SLEEPY 过渡态：cron 在锚点触发 go_sleep/wake_up，但若自主作息说
"还能撑/还想赖床"，先进入 SLEEPY，等 tick 到真正的入睡/起床点再切换。
"""
import json
import random
from datetime import datetime, timedelta

import config
import db

# 离线相位（work.status 的 phase 里，这几个 = 它在专注做事，不秒回）
_OFFLINE_PHASES = ("flow", "working")

_INBOX_KEY = "inbox_queue"
_SCHED_KEY = "rest_schedule"


# ---------- 离线判定 ----------

def is_offline(now: datetime | None = None) -> bool:
    """现在它是否离线（专注工作 或 睡觉）。"""
    state = db.get_state()
    if state == "SLEEPING":
        return True
    if state != "AWAKE":
        return False          # SLEEPY 过渡态算在线（还能回话，带困意）
    if not config.WORK_ENABLED:
        return False
    import work
    return work.status(now)["phase"] in _OFFLINE_PHASES


def offline_reason(now: datetime | None = None) -> str:
    """离线的原因：'sleep'（睡）/ 'flow'（刚进入状态）/ 'working'（忙）。"""
    if db.get_state() == "SLEEPING":
        return "sleep"
    import work
    return work.status(now)["phase"]


# ---------- 离线留言（你 → 它） ----------

def _inbox() -> list[dict]:
    raw = db.get_kv(_INBOX_KEY)
    if not raw:
        return []
    try:
        q = json.loads(raw)
    except ValueError:
        return []
    return [m for m in q if isinstance(m, dict) and m.get("text")]


def push_inbox(text: str) -> int:
    """攒一条离线留言，返回当前留言数。"""
    q = _inbox()
    q.append({"ts": datetime.now().isoformat(timespec="seconds"), "text": text})
    db.set_kv(_INBOX_KEY, json.dumps(q, ensure_ascii=False))
    return len(q)


def take_inbox() -> list[dict]:
    """取出并清空离线留言（上线时调用）。"""
    q = _inbox()
    db.set_kv(_INBOX_KEY, "")
    return q


def pending_inbox_count() -> int:
    return len(_inbox())


# ---------- 作息自主调节 ----------

def _load_sched() -> dict:
    raw = db.get_kv(_SCHED_KEY)
    if not raw:
        return {}
    try:
        s = json.loads(raw)
    except ValueError:
        return {}
    return s if isinstance(s, dict) else {}


def _save_sched(s: dict) -> None:
    db.set_kv(_SCHED_KEY, json.dumps(s, ensure_ascii=False))


def _life_day_str(now: datetime) -> str:
    import persona
    return persona._life_day(now)[0].strftime("%Y-%m-%d")


def _anchor_bedtime(now: datetime) -> datetime:
    """今晚的人格卡锚点入睡时刻（跨午夜处理，思路同 work._bedtime）。"""
    import persona
    p = persona.behavior_params()
    day, _ = persona._life_day(now)
    anchor = datetime(day.year, day.month, day.day, p["wake_hour"] % 24, 0)
    bed = anchor.replace(hour=p["sleep_hour"] % 24, minute=0, second=0, microsecond=0)
    if bed <= anchor:
        bed += timedelta(days=1)
    return bed


def _sleep_shift(now: datetime) -> int:
    """入睡浮动（分钟，正=熬夜晚睡，负=早睡），由当天状态决定。范围 [-早睡上限, +熬夜上限]。"""
    import work
    s = work.status(now)
    if s["energy"] <= config.REST_TIRED_ENERGY:    # 很累 → 早睡
        return -random.randint(10, config.REST_SLEEP_EARLY_MAX)
    if s["phase"] == "off":                        # 忙完了 → 想早点睡
        return -random.randint(0, config.REST_SLEEP_EARLY_MAX)
    if s["energy"] >= config.REST_ENERGETIC_ENERGY:  # 精力足还没忙完 → 熬夜
        return random.randint(0, config.REST_SLEEP_LATE_MAX)
    return random.randint(-15, 15)                 # 正常：轻微浮动


def _wake_shift(now: datetime) -> int:
    """起床浮动（分钟，正=赖床晚起，负=早醒），由昨晚睡得早晚决定。"""
    shift = random.randint(0, config.REST_WAKE_LIE_IN_MAX)
    late = _load_sched().get("sleep_shift", 0)
    if late >= 60:                                 # 昨晚熬得晚 → 更赖床
        shift += random.randint(0, config.REST_WAKE_LIE_IN_MAX)
    elif late <= -30:                              # 睡得早 → 可能早醒
        shift = random.randint(-config.REST_WAKE_EARLY_MAX, config.REST_WAKE_LIE_IN_MAX)
    return shift


def tonight_sleep_at(now: datetime) -> datetime:
    """今晚实际入睡时刻（锚点 + 自主浮动，同一生活日幂等）。"""
    sched = _load_sched()
    day = _life_day_str(now)
    if sched.get("day") == day and sched.get("sleep_at"):
        return datetime.fromisoformat(sched["sleep_at"])
    shift = _sleep_shift(now)
    sleep_at = _anchor_bedtime(now) + timedelta(minutes=shift)
    sched.update({"day": day, "sleep_at": sleep_at.isoformat(timespec="seconds"),
                  "sleep_shift": shift})
    _save_sched(sched)
    return sleep_at


def next_wake_at(now: datetime) -> datetime:
    """下次起床时刻（锚点 + 自主浮动，同一生活日幂等）。"""
    sched = _load_sched()
    import persona
    day, _ = persona._life_day(now)
    day_str = day.strftime("%Y-%m-%d")
    if sched.get("wake_day") == day_str and sched.get("wake_at"):
        return datetime.fromisoformat(sched["wake_at"])
    p = persona.behavior_params()
    wh = p["wake_hour"] % 24
    wake_base = datetime(day.year, day.month, day.day, wh, 0)
    if wake_base <= now:
        wake_base += timedelta(days=1)
    shift = _wake_shift(now)
    wake_at = wake_base + timedelta(minutes=shift)
    sched.update({"wake_day": day_str, "wake_at": wake_at.isoformat(timespec="seconds"),
                  "wake_shift": shift})
    _save_sched(sched)
    return wake_at


# ---------- 节律 tick（调度器每分钟调） ----------

def tick(engine) -> None:
    """每分钟：作息过渡（SLEEPY 到点入睡 / 赖床到点起床）+ 上线 flush 离线留言。"""
    now = datetime.now()
    state = db.get_state()
    if state == "SLEEPY":
        if now >= tonight_sleep_at(now):
            engine._do_sleep()
    elif state == "SLEEPING":
        if now >= next_wake_at(now):
            engine._do_wake()
    elif state == "AWAKE" and not is_offline(now) and pending_inbox_count():
        engine.flush_inbox()
