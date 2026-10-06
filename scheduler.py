"""调度器：定时问候 + 随机话题 + 作息切换。抖动（jitter）让生活感更真实。"""
import random

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config


def start(engine) -> BackgroundScheduler:
    sch = BackgroundScheduler()

    # 作息时刻跟随当前朋友的人格卡（无人格卡用全局默认）
    import persona
    p = persona.behavior_params()
    wake_hour, sleep_hour = p["wake_hour"], p["sleep_hour"]

    # 早安（±30 分钟抖动）：醒来并道早安
    sch.add_job(engine.wake_up, CronTrigger(
        hour=wake_hour, minute=0, jitter=1800), id="wake")

    # 午间话题（±40 分钟抖动）
    sch.add_job(lambda: engine.proactive("noon"), CronTrigger(
        hour=config.NOON_HOUR, minute=30, jitter=2400), id="noon")

    # 晚安（±30 分钟抖动）：道晚安并入睡
    sch.add_job(engine.go_sleep, CronTrigger(
        hour=sleep_hour, minute=0, jitter=1800), id="sleep")

    # 随机闲聊：白天每小时掷一次骰子，20% 概率发起（沿用全局时段，避免跨午夜边界）
    def maybe_random_chat():
        if random.random() < 0.2:
            engine.proactive("random")

    sch.add_job(maybe_random_chat, CronTrigger(
        hour=f"{config.WAKE_HOUR}-{config.SLEEP_HOUR - 1}", minute=random.randint(0, 59)),
        id="random_chat")

    # 初见阶段冒泡：每分钟检查一次是否到了冒泡时间
    sch.add_job(engine.check_bubble, CronTrigger(minute="*"), id="bubble")

    # 会话级等待：每分钟检查是否该轻催一下
    sch.add_job(engine.check_nudge, CronTrigger(minute="*"), id="nudge")

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
    sch.reschedule_job("noon", trigger=CronTrigger(
        hour=config.NOON_HOUR, minute=30, jitter=2400))
    sch.reschedule_job("random_chat", trigger=CronTrigger(
        hour=f"{config.WAKE_HOUR}-{config.SLEEP_HOUR - 1}", minute=random.randint(0, 59)))
