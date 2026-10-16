"""防打扰：主动消息的刹车系统。"""
import re
from datetime import datetime, timedelta

import config
import corpus
import db

# 紧急信号（词表在 corpus，会扩充）
_URGENT = re.compile(corpus.URGENT_QUERY)


def is_urgent(text: str) -> bool:
    """对方是不是有急事？紧急通道的判定入口（打断成本的安全阀）。

    判定必须保守——日常夸张说法（"我急死了""着急"）不算，否则安全阀天天被误触，
    等于没有。词表在 corpus.URGENT_QUERY，可扩充。
    """
    return bool(_URGENT.search(text or ""))


def in_urgent_window(now: datetime | None = None) -> bool:
    """现在是否处于紧急窗口：这段时间内它随叫随到（绕开静默与打断阻尼）。"""
    now = now or datetime.now()
    until = db.get_kv("urgent_until")
    if not until:
        return False
    try:
        return datetime.fromisoformat(until) > now
    except ValueError:
        return False


def open_urgent_window(now: datetime | None = None) -> None:
    """识别到急事 → 开一扇窗（URGENT_WINDOW_MINUTES 分钟内随叫随到）。"""
    now = now or datetime.now()
    db.set_kv("urgent_until", (now + timedelta(
        minutes=config.URGENT_WINDOW_MINUTES)).isoformat(timespec="seconds"))
    db.set_kv("quiet_until", "")      # 急事当前：静默期一并解除


def check_busy_signal(text: str) -> bool:
    """用户回复里出现忙碌信号（词表在 corpus）→ 进入静默期。"""
    if any(s in text for s in corpus.BUSY_SIGNALS):
        db.set_kv("quiet_until", (datetime.now() + timedelta(
            hours=config.QUIET_AFTER_BUSY_HOURS)).isoformat(timespec="seconds"))
        return True
    return False


def guard_enabled() -> bool:
    """防打扰总开关：kv 运行时覆盖优先，默认看 config（默认关）。"""
    v = db.get_kv("guard_enabled")
    if v:
        return v == "on"
    return config.GUARD_ENABLED


def can_send_proactive(now: datetime | None = None, scenario: str = "") -> tuple[bool, str]:
    """现在能不能发主动消息？返回 (可否, 原因)。
    防打扰开关关闭时只保留「睡觉中」拦截（那是作息，不是打扰）。
    scenario='bubble' 时使用冒泡独立额度（初见期高频冒泡不占普通主动上限）。"""
    now = now or datetime.now()

    # 1. 睡觉状态永远不发（作息系统，不属于防打扰）
    if db.get_state() == "SLEEPING":
        return False, "睡觉中"

    # 2. 娱乐模式：游戏进行中暂停一切主动消息（冒泡/问候/轻催），结束自动恢复
    import game
    if game.current():
        return False, "游戏中"

    # 3. 离线专注（flow/working）：TA 在忙自己的事，不主动闲聊（v0.99 修：
    #    random_chat 在它读书时连发 4 条）。必须在 guard_enabled 短路**之前**——
    #    这是作息语义不是打扰偏好，guard 关着也得拦。早安/晚安是作息锚点事件不拦
    #    （早睡浮动可能撞上最后一段会话还在跑，拦了晚安会无声消失）。
    import offline
    if scenario not in ("morning", "night") and offline.is_offline(now):
        return False, f"离线专注中（{offline.offline_reason(now)}）"

    if not guard_enabled():
        return True, "ok"

    # 4. 忙碌静默期
    quiet_until = db.get_kv("quiet_until")
    if quiet_until and datetime.fromisoformat(quiet_until) > now:
        return False, f"静默期至 {quiet_until}"

    # 5. 每日上限（冒泡走独立额度）
    if scenario == "bubble":
        if db.get_counter("bubble_count") >= config.BUBBLE_DAILY_LIMIT:
            return False, "已达每日冒泡上限"
    elif db.get_counter("proactive_count") >= config.DAILY_PROACTIVE_LIMIT:
        return False, "已达每日主动消息上限"

    # 6. 连续未回复上限（所有类型共用：连发都不理就该停了）
    if db.get_counter("unanswered_proactive") >= config.MAX_UNANSWERED_PROACTIVE:
        return False, "连续主动消息未获回复，今日停止主动"

    return True, "ok"
