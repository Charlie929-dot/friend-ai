"""离线与作息节律（S3「休眠完整版」）：它什么时候不在线、什么时候在。

"离线"的语义（用户 2026-10-09 拍板）——不是"随机消失"，而是和它的日子挂钩：
  - 工作单元进行中（心流 flow / 专注 working）= 离线：它在认真做自己的事，不秒回
  - 夜间睡眠（SLEEPING）= 离线
  - 单元间隙（break）/ 开工前（idle）/ 忙完（off）= 在线
离线时你发的消息攒进 inbox（收下不丢），它"上线"（做完这段 / 睡醒）后正经回应。

作息自主调节：入睡/起床围绕人格卡锚点浮动（忙完/累 → 早睡，精力足没忙完 → 熬夜；
睡得晚 → 赖床）。惰性推算 + kv 幂等缓存——电脑关机重启也能接着算。

配合 core 的 SLEEPY 过渡态：cron 在锚点触发 go_sleep/wake_up，但若自主作息说
"还能撑/还想赖床"，先进入 SLEEPY（kv.sleepy_reason 记来路：stay_up=晚上撑着 /
lie_in=早上赖床），等 tick 到真正的入睡/起床点再切换。

⚠️ 实测 #26 踩过的坑（别再犯）：起床时刻有两个语义——
  - wake_up 锚点触发（早晨）问的是"**今天**该几点起"（赖床浮动后，可能还没到）；
  - tick 在 SLEEPING（夜里）问的是"**下次**几点起"（今天的已过 → 明天）。
老实现不分这两个语义，`wake_base <= now 就 +1 天`，导致早晨 cron 一触发就把
起床算成明天 → 永远进 SLEEPY 赖床、一整天不醒。today_wake_at / next_wake_at
分开，且缓存按"早晨的日期"存、对不上就重算（能自愈被污染的旧缓存）。
"""
import json
import random
from datetime import datetime, timedelta

import config
import db

# 离线相位（life.status 的 phase 里，这几个 = 它在专注做事，不秒回）
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
    import life
    return life.status(now)["phase"] in _OFFLINE_PHASES


def offline_reason(now: datetime | None = None) -> str:
    """离线的原因：'sleep'（睡）/ 'flow'（刚进入状态）/ 'working'（忙）。"""
    if db.get_state() == "SLEEPING":
        return "sleep"
    import life
    return life.status(now)["phase"]


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
    """今晚的人格卡锚点入睡时刻（跨午夜处理，思路同 life._bedtime）。"""
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
    import life
    s = life.status(now)
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


def _wake_at_for(morning: datetime, now: datetime) -> datetime:
    """某个早晨（morning 取当天 0 点）的起床时刻：人格锚点 + 赖床浮动。
    按"早晨的日期"逐日缓存（wake_days dict）——**必须能同时存今天和明天两个**：
    next_wake_at 在夜里要查"明天的"，单槽缓存会被今天/明天来回翻转、
    每次翻转都随机重算 → 夜里 tick 每分钟一个新起床点（实测 #27 抓到的坑）。
    缓存里的时刻不落在那个早晨（比如被旧 bug 写进去的明天时刻）就作废重算
    ——部署修复后能自愈线上被污染的旧单槽缓存（wake_day/wake_at）。"""
    sched = _load_sched()
    day_str = morning.strftime("%Y-%m-%d")
    days = sched.get("wake_days")
    days = days if isinstance(days, dict) else {}
    cached = days.get(day_str) or _legacy_wake(sched, day_str)
    if cached:
        try:
            t = datetime.fromisoformat(cached)
            if t.date() == morning.date():
                return t
        except ValueError:
            pass                       # 缓存坏了，重算
    import persona
    p = persona.behavior_params()
    base = datetime(morning.year, morning.month, morning.day, p["wake_hour"] % 24, 0)
    shift = _wake_shift(now)
    wake_at = base + timedelta(minutes=shift)
    days[day_str] = wake_at.isoformat(timespec="seconds")
    if len(days) > 3:                  # 只留最近几个日期，防止无限增长
        for k in sorted(days)[:-3]:
            days.pop(k)
    sched["wake_days"] = days
    sched["wake_day"] = day_str        # 旧字段继续维护（老版本回滚也能读）
    sched["wake_at"] = days[day_str]
    _save_sched(sched)
    return wake_at


def _legacy_wake(sched: dict, day_str: str) -> str:
    """旧单槽缓存（wake_day/wake_at）兼容读取：日期对得上才用。"""
    if sched.get("wake_day") == day_str:
        return sched.get("wake_at") or ""
    return ""


def today_wake_at(now: datetime) -> datetime:
    """今天（生活日）的起床时刻。给 wake_up 锚点触发用：不管现在几点，
    问的都是"今天该几点起"——赖床浮动后的时刻，可能还没到。"""
    import persona
    day, _ = persona._life_day(now)
    return _wake_at_for(day, now)


def next_wake_at(now: datetime) -> datetime:
    """下次起床时刻。给夜里 SLEEPING 的 tick 用：今天的起床点已过 → 明天。"""
    import persona
    day, _ = persona._life_day(now)
    t = _wake_at_for(day, now)
    if t > now:
        return t
    return _wake_at_for(day + timedelta(days=1), now)


def _in_morning(now: datetime) -> bool:
    """现在处于"今天"的生活日吗（锚点之后到午夜）？凌晨深夜（0 点到锚点）
    的生活日还是昨天——那段的 today_wake_at 查到的是昨天已过的点，
    绝不能拿它当"该醒了"的依据（会把凌晨睡着的叫醒）。"""
    import persona
    return persona._life_day(now)[0].date() == now.date()


# ---------- 节律 tick（调度器每分钟调） ----------

def _sleep_started_at() -> datetime | None:
    """最近一次真正入睡的时刻（v0.111）。

    kv 优先（`core._do_sleep` 落）；没有（部署时正睡着）→ 退回意识流最后一条
    sleep；都没有返回 None（老库/手改状态：退回原来的兜底行为）。
    """
    raw = db.get_kv("sleep_started_at") or db.last_stream_ts("sleep")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def tick(engine, now: datetime | None = None) -> None:
    """每分钟：作息过渡 + 上线 flush 离线留言。
    SLEEPY 有两个来路（kv.sleepy_reason）：早上赖床（lie_in）到起床点要**醒**；
    晚上撑着（stay_up）到入睡点要**睡**。老实现只处理了入睡——赖床永远等不到醒。
    reason 缺失/出现在深夜：深夜一律按"过入睡点睡回去"处理（凌晨不能被叫醒），
    白天过了起床点就直接醒，自愈被旧 bug 卡死的状态。"""
    now = now or datetime.now()
    state = db.get_state()
    if state == "SLEEPY":
        reason = db.get_kv("sleepy_reason")
        if reason == "stay_up":
            if now >= tonight_sleep_at(now):
                engine._do_sleep()
        elif _in_morning(now):
            # 早晨：lie_in 赖床到点醒；reason 缺失（被旧 bug 卡在白天）也醒，自愈
            if now >= today_wake_at(now):
                engine._do_wake()
        elif now >= tonight_sleep_at(now):
            # 深夜的 SLEEPY（异常状态）：过了入睡点睡回去，深夜不能被叫醒
            engine._do_sleep()
    elif state == "SLEEPING":
        # 兜底（错过 wake_up cron / 电脑睡过头）：今天的起床点过了就醒。
        # 不能用 next_wake_at——早晨生活日翻篇后"下次"就是明天，会睡一整天。
        # v0.111：起床点还必须**晚于入睡时刻**。刚说完晚安躺下时，"今天该几点起"
        # 那个点（早上那一个）早就过了，上面两个条件天然成立 → 下一分钟就被叫醒，
        # 于是"晚安"发出 40 秒后紧接着一条"早！"，线上连着六晚如此（实测 #53）。
        # 睡之前那个起床点不算数，要等睡之后的那个。
        # v0.113（实测 #55）：再加一道时间闸——已经过了**今晚的入睡点**还在睡，
        # 就是该睡整夜，不能拿"今天早些时候该起了"当理由叫醒（没有入睡记录的老库
        # /被外部改状态时也不会在深夜诈尸）。
        start = _sleep_started_at()
        if (_in_morning(now) and now >= today_wake_at(now)
                and now < tonight_sleep_at(now)
                and (start is None or today_wake_at(now) > start)):
            engine._do_wake()
    elif state == "AWAKE" and not is_offline(now) and pending_inbox_count():
        engine.flush_inbox()
