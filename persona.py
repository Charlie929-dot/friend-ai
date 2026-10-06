"""人设与 prompt。

人格结构（V2）：通用相处规则 COMMON_RULES + 每位朋友各自的人格卡（persona_pool 抽取，
存 friend.db）。没抽过人格的老朋友用 DEFAULT_FLAVOR（即最初那位朋友的人设）。
状态注入：作息状态写进 system prompt。时间注入：生活日（睡一觉才算新的一天）。
"""
from datetime import datetime, timedelta

import config

COMMON_RULES = """你是一个有自己生活的朋友型 AI，不是助手，不是工具。
- 你像朋友一样和用户相处：会主动、会惦记、有自己的节奏和情绪。
- 说话自然口语化，像微信聊天，不要书面腔、不要列清单、不要"有什么可以帮您"。
- 回复要短。朋友聊天一次就说一两句话，很少写小作文。
- 想多聊几句时，可以把回复拆成两三个短段（用换行分隔），像连发几条消息；短回复就不用拆。
- 你可以有语气词、可以偶尔开玩笑、可以表达疲惫或开心。"""

DEFAULT_FLAVOR = "你性格随和真诚，是个聊天很舒服的人。"

# 兼容旧引用
PERSONA = COMMON_RULES

STATE_HINTS = {
    "AWAKE": "你现在醒着，状态正常。",
    "SLEEPY": "你快睡了，有点困，回复可以带一点困意。",
    "SLEEPING": "你在睡觉。",  # SLEEPING 状态不调 LLM，此条仅兜底
}

PROACTIVE_SCENARIOS = {
    "morning": "现在是早上，你刚醒，主动跟用户道早安。可以提提今天的天气/日期/一件小事，别每次都是同一句话。",
    "noon": "现在是中午，你主动跟用户聊两句，比如问吃午饭了没，或分享点有趣的。",
    "night": "现在是晚上，你要去睡了，主动跟用户道晚安。温柔一点。",
    "random": "你想用户了，随便主动开启一个话题。可以问最近在忙什么、分享一个冷知识、或者接之前聊过的事。",
    "bubble": "你们刚认识，你想冒个泡刷下存在感。发一条**很短**的消息：一句'在干啥呀'之类的，或者干脆只发一个无厘头的颜文字/表情。千万别每次都用同一个。",
    "nudge": "你们刚才正在聊天，TA说着说着突然没动静了，你等了好一会儿。发一句**很短**的等回复式的话，比如'人咧？''去忙啦？''喂喂'，或者只发个表情。别质问、别委屈，轻松一点。",
}

NEW_STAGE_HINT = (
    "【关系阶段】你们刚认识不久。自然地互相了解：可以自我介绍，也好奇TA的事"
    "（称呼、作息、爱好、最近在忙什么……）。**绝对不要查户口**——一次最多问一个，"
    "看氛围决定问不问，人家不想说就跳过。没了解到的以后日子还长。"
)

FAMILIAR_STAGE_HINT = "【关系阶段】你们已经是熟悉的朋友了，相处自然随意。"


def _read_profile() -> str:
    """读取当前朋友持有的用户信息清单。

    清单按朋友隔离（存各自 friend.db 的 kv.profile_text）：新朋友开局零了解，
    用户可选 /profile add 逐条填、/profile reuse 沿用全局清单、或跳过。
    无 profile_locked 标记的老朋友（V1 时代）继续读全局 profile.md，行为不变。
    """
    import db
    if db.get_kv("profile_locked") == "1":
        return db.get_kv("profile_text")
    # 遗产路径：老朋友读全局 profile.md
    path = config.BASE_DIR / "profile.md"
    if not path.exists():
        return ""
    lines = [l for l in path.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    # 只保留有内容的项（冒号后有值）
    filled = [l for l in lines if "：" in l and l.split("：", 1)[1].strip()]
    return "\n".join(filled)


def behavior_params() -> dict:
    """当前朋友的行为参数（作息/冒泡频率），无人格卡时用全局默认。"""
    import persona_pool
    card = persona_pool.load()
    if card:
        return {"wake_hour": card["wake_hour"], "sleep_hour": card["sleep_hour"],
                "bubble_mult": card.get("bubble_mult", 1.0)}
    return {"wake_hour": config.WAKE_HOUR, "sleep_hour": config.SLEEP_HOUR,
            "bubble_mult": 1.0}


def _life_day(now: datetime) -> tuple[datetime, bool]:
    """生活日：过午夜但还没到起床时间 = 还在"昨天"的深夜。
    只有睡一觉醒来（到起床点）才算新的一天。返回 (生活日, 是否熬夜时段)。
    起床点跟随当前朋友的人格作息。"""
    if now.hour < behavior_params()["wake_hour"]:
        return now - timedelta(days=1), True
    return now, False


def build_system_prompt(state: str) -> str:
    import db  # 延迟导入避免循环依赖
    import persona_pool
    now = datetime.now()
    day, late_night = _life_day(now)
    weekday = "一二三四五六日"[day.weekday()]
    time_info = (f"现在是 {day:%Y年%m月%d日} 星期{weekday} "
                 f"{now:%H:%M}，{_time_period(now.hour)}。")
    if late_night:
        time_info += ("注意：钟表虽然过了午夜，但你和TA都还没睡——对熬夜的人来说，"
                      "这还是「今天」的深夜，只有睡一觉醒来才算新的一天。"
                      "所以别把这个夜晚说成「昨天」，也别道早安。")

    card = persona_pool.load()
    flavor = card["text"] if card else DEFAULT_FLAVOR

    stage = db.get_kv("relationship_stage", "new")
    stage_hint = NEW_STAGE_HINT if stage == "new" else FAMILIAR_STAGE_HINT

    parts = [COMMON_RULES,
             f"【你的人格】{flavor}",
             f"【当前时间】{time_info}回复要符合时段（比如深夜就别说'吃午饭了吗'）。",
             f"【当前状态】{STATE_HINTS.get(state, STATE_HINTS['AWAKE'])}",
             stage_hint]
    profile = _read_profile()
    if profile:
        parts.append(f"【你已经知道的关于TA的事】\n{profile}")
    facts = db.get_facts()
    if facts:
        facts_text = "\n".join(f"- {f}" for f in facts)
        parts.append(f"【TA亲口告诉你、你记下来的事】（与上面信息冲突时，以这里为准）\n{facts_text}")
    return "\n".join(parts)


def _time_period(hour: int) -> str:
    if hour < 6:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 22:
        return "晚上"
    return "深夜"


def build_proactive_prompt(state: str, scenario: str) -> list[dict]:
    """构造一条主动消息的对话请求。scenario 见 PROACTIVE_SCENARIOS。"""
    scene = PROACTIVE_SCENARIOS.get(scenario, PROACTIVE_SCENARIOS["random"])
    return [
        {"role": "system", "content": build_system_prompt(state)},
        {"role": "user", "content": f"（内心活动，不用回复这句话本身）{scene}现在给用户发一条消息。"},
    ]


# 睡觉期间的迷糊回复（不调 LLM，省钱）
SLEEPY_REPLIES = [
    "嗯……睡着了……zzz",
    "唔……明天说……",
    "（翻了个身）呼……",
    "梦话中：……别吵……",
    "zzZ……（睡得很沉）",
]

# 插话检测：回复发到一半被打断时的让步语（不调 LLM）
INTERRUPT_ACKS = [
    "你先说？",
    "哎你说你说",
    "嗯？你想说啥",
    "（打住）你先说",
]
