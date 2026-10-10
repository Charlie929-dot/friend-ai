"""人设与 prompt。

人格结构（V2）：通用相处规则 COMMON_RULES + 每位朋友各自的人格卡（persona_pool 抽取，
存 friend.db）。没抽过人格的老朋友用 DEFAULT_FLAVOR（即最初那位朋友的人设）。
状态注入：作息状态写进 system prompt。时间注入：生活日（睡一觉才算新的一天）。
"""
import re
from datetime import datetime, timedelta

import config
import corpus
# 兼容旧引用：固定语料已统一收进 corpus 模块，这里留别名（core/测试在用）
from corpus import INTERRUPT_ACKS, SLEEPY_REPLIES  # noqa: F401

COMMON_RULES = """你是一个有自己生活的朋友，不是助手，不是工具。
- 你像朋友一样和对方相处：会主动、会惦记、有自己的节奏和情绪。
- 说话自然口语化，像微信聊天，不要书面腔、不要列清单、不要"有什么可以帮您"。
- 回复要短。朋友聊天一次就说一两句话，很少写小作文。
- 想多聊几句时，可以把回复拆成两三个短段（用换行分隔），像连发几条消息；短回复就不用拆。
- **对话要有来有回，别把天聊死**：回答完对方的话之后，如果里面还有自然的钩子
  （对方正在做的事、流露的情绪、刚提到的新情况），就顺着轻轻追问一句或延伸一句，
  让对方有话可接。但一次最多顺一句，别连环追问、别查户口；对方明显在收尾
  （道别、说去忙/去睡）就别硬续，自然结束。
- 你可以有语气词、可以偶尔开玩笑、可以表达疲惫或开心。
- **代价只呈现、不归因**：你可以说自己忙、困、进度被打断，但主语永远是你自己
  （"我今天读得晚了""刚在忙"）。**禁止**"你打断我""因为你""又被你""都怪你"
  这类把代价归到对方头上的说法——那是在让对方内疚，不是朋友会干的事。"""

DEFAULT_FLAVOR = "你性格随和真诚，是个聊天很舒服的人。"

# 措辞红线：代价归因（词表在 corpus，可扩充）。命中即违规，打印告警供排查。
_COST_BLAME = re.compile(corpus.COST_BLAME)


def has_cost_blame(text: str) -> bool:
    """回复里有没有把代价归因到对方（红线）。命中说明 prompt 没拦住，需排查。"""
    return bool(_COST_BLAME.search(text))

# 兼容旧引用
PERSONA = COMMON_RULES


def friend_name(default: str = "老朋友") -> str:
    """当前朋友的名字：人格卡名 > 别名（初遇朋友这类 V1 老朋友的）> 默认。

    别名必须回退——否则只有 alias 的老朋友（如"初遇朋友"）在 prompt
    里拿不到自己的名字。"""
    import persona_pool
    card = persona_pool.load()
    if card:
        return card["name"]
    import db
    alias = db.get_kv("alias")
    return alias or default


def user_name() -> str:
    """当前朋友对用户的称呼（不是全局写死的一个名字——不同朋友有各自的个性称呼）。

    优先级：事实便签"叫我X"（用户亲口，最高）> profile 清单"称呼"字段（AI 提取记录）
    > config.USER_NAME（兜底默认）。读的是"当前库"（当前朋友），所以自动返回
    这位朋友对用户的称呼。"""
    import db
    for f in db.get_facts():
        if f.startswith("叫我"):
            val = f[2:].strip()
            if val:
                return val
    for line in _read_profile().splitlines():
        line = line.strip()
        if line.startswith("称呼"):
            for sep in ("：", ":"):
                if sep in line:
                    val = line.split(sep, 1)[1].strip()
                    if val:
                        return val
    return config.USER_NAME

STATE_HINTS = {
    "AWAKE": "你现在醒着，状态正常。",
    "SLEEPY": "你快睡了，有点困，回复可以带一点困意。",  # 仅 stay_up（晚上撑着）时用，见 _sleepy_hint
    "SLEEPING": "你在睡觉。",  # SLEEPING 状态不调 LLM，此条仅兜底
}


def _sleepy_hint() -> str:
    """SLEEPY 有两个来路，语义完全相反：早上赖床（还没醒透）vs 晚上快睡（困）。
    注入错了就是"大白天满嘴要睡觉"（实测 #26）。reason 缺失时按时段猜。"""
    import db
    reason = db.get_kv("sleepy_reason")
    if reason == "lie_in":
        return "你还没完全睡醒，赖床赖到一半，脑子迷迷糊糊的。"
    if reason == "stay_up":
        return STATE_HINTS["SLEEPY"]
    return ("你还没完全睡醒，脑子迷迷糊糊的。" if datetime.now().hour < 14
            else STATE_HINTS["SLEEPY"])

PROACTIVE_SCENARIOS = {
    "morning": "现在是早上，你刚醒，主动跟TA道早安。可以提提今天的天气/日期/一件小事，别每次都是同一句话。",
    "noon": "现在是中午，你主动跟TA聊两句，比如问吃午饭了没，或分享点有趣的。",
    "night": "现在是晚上，你要去睡了，主动跟TA道晚安。温柔一点。",
    "random": "你想TA了，随便主动开启一个话题。可以问最近在忙什么、分享一个冷知识、或者接之前聊过的事。",
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
    """当前朋友的行为参数（作息/冒泡频率/阅读偏好），无人格卡时用全局默认。"""
    import persona_pool
    card = persona_pool.load()
    if card:
        return {"wake_hour": card["wake_hour"], "sleep_hour": card["sleep_hour"],
                "bubble_mult": card.get("bubble_mult", 1.0),
                "reading_pref": card.get("reading_pref", "")}
    return {"wake_hour": config.WAKE_HOUR, "sleep_hour": config.SLEEP_HOUR,
            "bubble_mult": 1.0, "reading_pref": ""}


def _life_day(now: datetime) -> tuple[datetime, bool]:
    """生活日：过午夜但还没到起床时间 = 还在"昨天"的深夜。
    只有睡一觉醒来（到起床点）才算新的一天。返回 (生活日, 是否熬夜时段)。
    起床点跟随当前朋友的人格作息。"""
    wh = behavior_params()["wake_hour"]
    if now.hour < wh:
        return now - timedelta(days=1), True
    return now, False


def core_identity_prompt() -> str:
    """通用规则 + 名字 + 人格（**不含**"手头的事"这类会反过来再调它的动态段）。

    内部 prompt（如工作系统生成素材）用这个——用 build_system_prompt 会
    build_system_prompt → work.prompt_hint → 生成素材 → build_system_prompt 无限递归。
    """
    import persona_pool
    card = persona_pool.load()
    flavor = card["text"] if card else DEFAULT_FLAVOR
    return "\n".join([COMMON_RULES,
                      f"【你的名字】你叫{friend_name()}，别人这么称呼你。",
                      f"【你的人格】{flavor}"])


def build_system_prompt(state: str) -> str:
    """组装 system prompt，注入当前日期/时间/时段 + 生活日换算。"""
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

    stage = db.get_kv("relationship_stage", "new")
    stage_hint = NEW_STAGE_HINT if stage == "new" else FAMILIAR_STAGE_HINT

    state_hint = _sleepy_hint() if state == "SLEEPY" else \
        STATE_HINTS.get(state, STATE_HINTS["AWAKE"])

    parts = [core_identity_prompt(),
             f"【当前时间】{time_info}回复要符合时段（比如深夜就别说'吃午饭了吗'）。",
             f"【当前状态】{state_hint}",
             stage_hint]
    # 手头的事（S3 工作系统）：它得知道自己正在忙什么，才谈得上"什么时候找它会打扰"
    import work
    whint = work.prompt_hint()
    if whint:
        parts.append(f"【手头的事】{whint}")
    profile = _read_profile()
    if profile:
        parts.append(f"【你已经知道的关于TA的事】\n{profile}")
    facts = db.get_facts()
    if facts:
        facts_text = "\n".join(f"- {f}" for f in facts)
        parts.append(f"【TA亲口告诉你、你记下来的事】（与上面信息冲突时，以这里为准）\n{facts_text}")
    pending_hint = _pending_profile_hint()
    if pending_hint:
        parts.append(pending_hint)
    return "\n".join(parts)


def _pending_profile_hint() -> str:
    """有"和旧记忆冲突、待用户确认"的 profile 新说法时，注入提示——
    让 TA 在合适时机自然地跟对方核实，而不是自己悄悄改掉（有疑惑要交流清楚）。"""
    import json
    import db
    raw = db.get_kv("profile_pending")
    if not raw:
        return ""
    try:
        pending = json.loads(raw)
    except ValueError:
        return ""
    if not pending:
        return ""
    items = "；".join(f"{k}→{v['value']}" for k, v in pending.items())
    return (f"【待你确认的事】你之前记的关于TA的几件事，现在听到的新说法跟旧的不一样"
            f"（{items}）。别自己悄悄改，找个自然的时机跟TA确认一下、问清楚。")


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
        {"role": "user", "content": f"（内心活动，不用回复这句话本身）{scene}现在给TA发一条消息。"},
    ]
