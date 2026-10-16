"""调度器：定时问候 + 作息切换 + 各类后台 tick。抖动（jitter）让生活感更真实。

v0.103（对话延续批 1）：删掉三个「无理由定时通道」——random_chat（20%/小时骰子）、
nudge（轻催）、noon（午间话题）。「定时器 + 骰子」的根因公式废除，主动开口改由
**念头驱动**（临期日历项 + 分享冲动注入意识流），见 `docs/方案-对话延续-v0.100.md`。
剩下来的定时任务是作息锚点（早安/晚安）、初见冒泡、以及工作/离线/关怀/冲动的每分钟 tick。
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


def start(engine) -> BackgroundScheduler:
    sch = BackgroundScheduler()

    # 作息时刻跟随当前朋友的人格卡（无人格卡用全局默认）
    import persona
    p = persona.behavior_params()
    wake_hour, sleep_hour = p["wake_hour"], p["sleep_hour"]

    # 早安（±30 分钟抖动）：醒来并道早安
    sch.add_job(engine.wake_up, CronTrigger(
        hour=wake_hour, minute=0, jitter=1800), id="wake")

    # 晚安（±30 分钟抖动）：道晚安并入睡
    sch.add_job(engine.go_sleep, CronTrigger(
        hour=sleep_hour, minute=0, jitter=1800), id="sleep")

    # 初见阶段冒泡：每分钟检查一次是否到了冒泡时间
    sch.add_job(engine.check_bubble, CronTrigger(minute="*"), id="bubble")

    # 工作系统：每分钟结算一次该结算的单元（惰性补账——电脑睡一觉醒来也能补上）
    import life
    sch.add_job(life.tick, CronTrigger(minute="*"), id="work")

    # 离线与作息：每分钟检查 SLEEPY 过渡到点 + 上线 flush 离线留言
    import offline
    sch.add_job(offline.tick, CronTrigger(minute="*"), args=[engine], id="offline")

    # 关怀事件（S4）：每分钟检查到点的承诺/约定（提醒），醒着就发、睡着顺延
    import events
    sch.add_job(events.tick, CronTrigger(minute="*"), args=[engine], id="care")

    # 日历（v0.103 批 3）：每分钟检查临期触点到点 → 注入念头 + 主动开口
    sch.add_job(events.calendar_tick, CronTrigger(minute="*"), args=[engine], id="calendar")

    # 分享冲动（v0.103 批 2）：每分钟检查待开口的分享念头 → 预算内主动开口
    sch.add_job(engine.check_share_impulse, CronTrigger(minute="*"), id="share_impulse")

    sch.start()
    return sch


def reschedule_routine(sch: BackgroundScheduler, engine, wake_hour: int, sleep_hour: int) -> None:
    """人格变更后重排作息任务（夜猫子人格 2 点才睡，老干部 6 点就起）。"""
    sch.reschedule_job("wake", trigger=CronTrigger(hour=wake_hour, minute=0, jitter=1800))
    sch.reschedule_job("sleep", trigger=CronTrigger(hour=sleep_hour, minute=0, jitter=1800))


def reschedule_all(sch: BackgroundScheduler, engine) -> None:
    """/refresh 热重载后：按最新 config 参数和人格卡重排所有定时任务。"""
    import persona
    p = persona.behavior_params()
    reschedule_routine(sch, engine, p["wake_hour"], p["sleep_hour"])
