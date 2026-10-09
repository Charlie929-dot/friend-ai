"""防打扰：主动消息的刹车系统。"""
from datetime import datetime, timedelta

import config
import corpus
import db


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

    if not guard_enabled():
        return True, "ok"

    # 3. 忙碌静默期
    quiet_until = db.get_kv("quiet_until")
    if quiet_until and datetime.fromisoformat(quiet_until) > now:
        return False, f"静默期至 {quiet_until}"

    # 4. 每日上限（冒泡走独立额度）
    if scenario == "bubble":
        if db.get_counter("bubble_count") >= config.BUBBLE_DAILY_LIMIT:
            return False, "已达每日冒泡上限"
    elif db.get_counter("proactive_count") >= config.DAILY_PROACTIVE_LIMIT:
        return False, "已达每日主动消息上限"

    # 5. 连续未回复上限（所有类型共用：连发都不理就该停了）
    if db.get_counter("unanswered_proactive") >= config.MAX_UNANSWERED_PROACTIVE:
        return False, "连续主动消息未获回复，今日停止主动"

    return True, "ok"
