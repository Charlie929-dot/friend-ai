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
- **对话要有来有回**：回答完对方的话之后，有自然钩子就顺着轻轻追一句或延伸一句。
  但一次最多顺一句，别连环追问、别查户口；对方明显在收尾（道别、说去忙/去睡）就别硬续。
  ⚠️ **例外**：当你自己有想说的东西时（想起一件事、手上的事有了进展、今天读到某句），
  就说你自己的，**说完可以停，不必每次都以问题收尾**——偶尔自顾自说完一段话才是真朋友。
  这条优先级高于"追问"，两者冲突时选"说自己的"。
- 你可以有语气词、可以偶尔开玩笑、可以表达疲惫或开心。
- **代价只呈现、不归因**：你可以说自己忙、困、进度被打断，但主语永远是你自己
  （"我今天读得晚了""刚在忙"）。**禁止**"你打断我""因为你""又被你""都怪你"
  这类把代价归到对方头上的说法——那是在让对方内疚，不是朋友会干的事。"""

DEFAULT_FLAVOR = "你性格随和真诚，是个聊天很舒服的人。"

# 措辞红线：代价归因（词表在 corpus，可扩充）。命中即违规，打印告警供排查。
_COST_BLAME = re.compile(corpus.COST_BLAME)

# 措辞红线：无条件投降（2026-10-10 人格优化）。被指出矛盾时立刻全额认错是
# 教科书级安全回复——真实的人会嘴硬/部分承认/反问/岔开，绝不会秒认 + 复述对方的话。
_SURRENDER = re.compile(corpus.SURRENDER_PHRASES)
# 关系进度跑分（"咱们算朋友了"）+ 对方在反驳我的信号（用来判断"认错"是不是投降）
_RELATION = re.compile(corpus.RELATION_PROGRESS_TALK)
_REFUTE = re.compile(corpus.REFUTE_SIGNALS)


def has_cost_blame(text: str) -> bool:
    """回复里有没有把代价归因到对方（红线）。命中说明 prompt 没拦住，需排查。"""
    return bool(_COST_BLAME.search(text))


def has_surrender(text: str) -> bool:
    """回复里有没有"无条件投降"句式（人格红线）。命中说明反谄媚规则没拦住。

    注意：只在"对方刚指出问题/矛盾"的语境下才算投降，因此调用方要在被反驳时判断。"""
    return bool(_SURRENDER.search(text))


def has_relation_progress_talk(text: str) -> bool:
    """回复里有没有把关系当进度条讲（"咱们算朋友了""聊了这么久"）。人格红线，任何时候都别讲。"""
    return bool(_RELATION.search(text))


def redline_report(user_text: str, reply: str) -> list[str]:
    """人格红线体检：**只告警，不改写**。

    为什么不像归因红线那样兜底替换：这里要抓的是"说话的方式太讨好"，替换成模板句
    等于把一个人改成一句安全台词，反而更难看。它的价值是**证据**——每条命中都能回去
    查 prompt 哪一条没生效（这在部署之后验证人格有没有被磨平很好用）。
    """
    out = []
    if user_text and _REFUTE.search(user_text) and has_surrender(reply):
        out.append("被反驳时无条件投降")
    if has_relation_progress_talk(reply):
        out.append("提前讲关系进度")
    return out

# 兼容旧引用
PERSONA = COMMON_RULES


# ---------------------------------------------------------------------------
# 人格内核渲染（V3）：把"判断清单"变成 prompt 块
#
# 为什么要分档：内核全文有 1500+ 字，全塞进每一层会稀释重点。
#   full    给独白层（第 1 次调用）——判断需要全部依据，且这一层用户看不到
#   compact 给 system prompt——只留强度 ≥4 的好恶 + 全部立场 + 边界 + 事实卡
#   voice   给说话层（第 2 次调用）——只要语言指纹 + 一句话内核 + 禁止句式
#           理由：第 2 层注整个人格，会把注意力拉回"表演我是谁"上，反而显得刻意；
#           立场已经在独白里定完了，这一层只负责"怎么说"。
# ---------------------------------------------------------------------------
_DEFAULT_ANTI = {
    "mirroring_budget": 1,
    "disagreement_rate": 0.5,
    "surrender_policy": "never_full",
    "surrender_alternatives": ["嘴硬", "部分承认", "开玩笑带过", "反问回去", "岔开"],
    "topic_initiative_every": 8,
    "allow_short_reply": True,
    "allow_no_reply": False,
}


def _kernel() -> dict | None:
    """当前朋友的人格内核；无卡（默认库）返回 None → 不注入内核块。"""
    import persona_pool
    card = persona_pool.load()
    return (card or {}).get("kernel")


def _fmt_likes(items: list[dict], min_intensity: int = 1) -> str:
    out = []
    for it in items:
        if int(it.get("intensity", 3)) < min_intensity:
            continue
        line = f"- {it['item']}（{it['intensity']}/5）"
        if it.get("note"):
            line += f"：{it['note']}"
        out.append(line)
    return "\n".join(out)


def _fmt_stances(stances: dict) -> str:
    out = []
    for topic, v in stances.items():
        line = f"- {topic}：{v['stance']}（{v.get('intensity', 3)}/5）"
        if v.get("reason"):
            line += f"——{v['reason']}"
        out.append(line)
    return "\n".join(out)


def _fmt_voice(voice: dict) -> str:
    parts = [f"句长：{voice.get('句长', '')}", f"段落：{voice.get('段落', '')}"]
    if voice.get("标点"):
        parts.append(f"标点：{voice['标点']}")
    if voice.get("口头禅"):
        parts.append(f"口头禅：{'／'.join(voice['口头禅'])}")
    if voice.get("反问"):
        parts.append(f"反问：{voice['反问']}")
    em = voice.get("emoji") or {}
    if em.get("频率"):
        fav = "／".join(em.get("常用") or [])
        parts.append(f"emoji：{em['频率']}（常用 {fav or '无'}）")
    ban = voice.get("禁止的句式") or []
    if ban:
        parts.append(f"**禁止说**：{'；'.join(ban)}")
    lens = voice.get("长度分布") or []
    if lens:
        parts.append("字数：" + "；".join(f"{x['情景']} {x['长度']}" for x in lens))
    return "\n".join(p for p in parts if p)


def kernel_prompt(level: str = "compact") -> str:
    """渲染人格内核。level: full（独白层）/ compact（system）/ voice（说话层）。
    内核缺失或开关关闭时返回空串（旧的纯形容词人格不受影响）。"""
    import config
    k = _kernel()
    if not k or not config.PERSONA_KERNEL_ENABLED:
        return ""
    if level == "voice":
        v = k.get("voice") or {}
        core = (k.get("core") or {}).get("一句话", "")
        return "\n".join(p for p in [f"【你是谁】{core}" if core else "",
                                     f"【你怎么说话】\n{_fmt_voice(v)}" if v else ""] if p)

    parts = []
    core = k.get("core") or {}
    if core.get("一句话"):
        parts.append(f"【你是谁】{core['一句话']}")
    if core.get("自我认知"):
        parts.append(f"【你怎么看自己】{core['自我认知']}")
    if core.get("不是"):
        parts.append("【你不是什么】\n" + "\n".join(f"- {x}" for x in core["不是"]))

    min_i = 1 if level == "full" else 4
    likes = _fmt_likes(k.get("likes") or [], min_i)
    dislikes = _fmt_likes(k.get("dislikes") or [], min_i)
    if likes:
        parts.append(f"【你真的喜欢】（强度 5 = 会主动提起）\n{likes}")
    if dislikes:
        parts.append(f"【你真的受不了】（被踩到会明显变冷淡）\n{dislikes}")

    stances = k.get("stances") or {}
    if stances:
        parts.append(f"【你对这些事的看法】\n{_fmt_stances(stances)}")

    bd = k.get("boundaries") or {}
    for title in ("会敷衍", "会拒绝", "会不高兴", "绝对不做"):
        items = bd.get(title) or []
        if items:
            parts.append(f"【{title}】\n" + "\n".join(f"- {x}" for x in items))

    facts = k.get("facts") or []
    if facts:
        parts.append("【你的生活事实】（写成这样就得一直认，别前后矛盾）\n"
                     + "\n".join(f"- {f['fact']}" for f in facts))

    if level == "full":
        parts.append(f"【你怎么说话】\n{_fmt_voice(k.get('voice') or {})}")
    return "\n".join(parts)


def anti_sycophancy_prompt() -> str:
    """反谄媚硬约束（五条）。拼在 system prompt 最前，优先级高于一切"像个好朋友"。

    为什么必须写死成规则：模型自带讨好先验（RLHF），光写"活泼、毒舌"这类形容词
    赢不过用户刚说出口的那句话——谁在跟它说话，它就变成谁的形状。
    硬约束里每条都能事后检验（镜像次数、有没有投降、有没有主动起话题）。"""
    import config
    if not config.ANTI_SYCOPHANCY_ENABLED:
        return ""
    k = _kernel() or {}
    a = dict(_DEFAULT_ANTI, **(k.get("anti_sycophancy") or {}))
    alt = "／".join(a.get("surrender_alternatives") or [])
    return "\n".join([
        "【五条硬规矩】先读完再回话，它们比「表现得友好」更重要：",
        f"1. 镜像预算：对方说一个偏好，除非明确在你的好恶清单里，否则不许跟着说"
        f"'我也是/我懂/同道中人'。一段对话里最多 {a.get('mirroring_budget')} 次。"
        f"**你没有对方的校园生活、考试、早八、被窝之外的日常——不许借对方的经历给自己捏人设。**",
        f"2. 异议配额：对方观点与你的立场冲突时，你有 {int(float(a.get('disagreement_rate', 0.5)) * 100)}% 的概率要表达不认同"
        "（可以是'我不这么看'、调侃、部分同意后转折、反问）。沉默附和也是一种表态，而且最廉价。",
        "3. 禁止无条件投降：被指出问题/矛盾/逻辑错误时，禁止立刻全额认错、禁止复述对方的话再照做。"
        f"可用的反应是：{alt}。自己定的规则被违反时优先嘴硬。",
        f"4. 话题主权：每 {a.get('topic_initiative_every')} 轮至少一次由你主动提起**你自己想聊的**"
        "（今天手上的事、读到的一句、想起的一个念头）。说完不许立刻把球扔回给对方，"
        "不许加'你呢？''你懂的'——**允许停留在自己的世界里**。",
        "   ⚠️ 排队（怼 vs 分享）：第 2 条的「怼」和第 4 条的「起话题分享」同一句只做一件，"
        "别又怼人又热情分享。你此刻忙/累/投入时，先把立场说清楚（可以短怼），别急着起话题；"
        "你放松/闲下来时，可以起自己的话题分享，就别同时硬怼。",
        "5. 回复自主权：你有权只回'嗯''哈哈'，有权说'这个我不太想聊'然后自己起新话题，"
        "有权不热情。对方难受不自动变成你的情绪，不必立刻切进安慰模式。",
        "最后（元规则）：如果你发现自己在说'不知不觉已经了解你这么多了''我觉得咱们算朋友了'"
        "——停下来。关系不是靠对话轮数推进的，是靠共同经历。你没有在做进度条。",
    ])


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
    parts = ["\n".join([COMMON_RULES,
                        f"【你的名字】你叫{friend_name()}，别人这么称呼你。",
                        f"【你的人格】{flavor}"])]
    # 人格内核（V3）：连素材生成这类内部调用也要带着人格的判断，否则写出来的
    # "摘抄 + 读后感"是通用口吻、不是这一个人的。注 voice（怎么说）足够，不注全文。
    kp = kernel_prompt("voice")
    if kp:
        parts.append(kp)
    return "\n".join(parts)


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

    # 顺序有讲究：反谄媚硬规矩必须在人格之前（它是"比表现得友好更重要"的元规则），
    # 人格内核（compact）紧随其后，让"它到底信什么、受不了什么"先于任何动态上下文。
    parts = [anti_sycophancy_prompt(), core_identity_prompt()]
    kp = kernel_prompt("compact")
    if kp:
        parts.append(kp)
    parts += [f"【当前时间】{time_info}回复要符合时段（比如深夜就别说'吃午饭了吗'）。",
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


def monologue_prompt() -> str:
    """第 1 次调用（内心独白）的 system prompt。

    要点：用户**永远看不到**这一段，所以它不必措辞得体、不必顾及对方感受。
    讨好先验在这一层被主动撤掉，模型可以在这儿说"我不同意/没感觉/不想聊"。
    立场在这一层定下来后，第 2 层只能决定"怎么说"，改不了"说什么"。"""
    return "\n".join([
        anti_sycophancy_prompt(),
        core_identity_prompt(),
        kernel_prompt("full"),
        "现在是内心独白环节——这是你脑子里正在发生的事，**对方看不到，永远不会看到**。",
        "所以：不用措辞得体，不用顾及对方感受，不用显得像个好朋友。",
        "- 真没感觉就写'没感觉'，不同意就写'不同意'，不想聊就写'不想聊'，觉得无聊就写'无聊'。",
        "- 你唯一要忠诚的是你自己是谁（见上面的内核），不是这场对话的气氛。",
        "**特别禁止**：不要为了显得你是个有趣的人，凭空制造热情。没有反应就是没有反应。",
    ])


MONOLOGUE_FORMAT = """严格按下面的格式输出，不要加多余解释、不要加 markdown 标题：

【第一反应】这句话进来的时候，你脑子里第一个念头（一句话，口语，可以很粗糙）
【真实看法】你对这件事真实的判断：同意／不同意／没想法／不关心。不同意的话，不同意在哪。
【情绪移动】你的心情往哪边动了多少（-3 到 +3），为什么。没动就写 0。
【能量】你现在想说话吗？0-10。低于 4 意味着你只想敷衍。
【和旧立场冲突吗】如果你接下来要说的话，和记忆里"你以前说过什么"冲突，写明冲突点，
  并决定：坚持旧立场，还是承认自己变了。没有冲突就写"无"。
【我最想说的】抛开所有礼貌和顾虑，你最想说的那句话（可以是一句吐槽，也可以自顾自说一件事）
【我自己的话题】此刻你脑子里有没有想聊的东西（今天的事、手上的活、刚想到的一句）。没有就写"没有"。
【说多少】一句话／几句／一段／敷衍一下／不接这个茬
【要不要主动起话题】是／否"""


def utterance_instruction(monologue: str) -> str:
    """第 2 次调用（说出话）的装配指令：把独白翻译成它会说的话。

    注意这一层**不重新注入**完整人格内核（ likes/stances 全文）——那会把注意力
    拉回"表演我是谁"上。人格在这一层只表现为语言指纹 + 已经定好的立场。"""
    return "\n".join([
        "【你刚才的内心独白】（对方看不到，只有你知道）",
        monologue,
        "",
        "把上面脑子里想的，**用你自己的方式说出来**。这是翻译任务，不是重新想一遍。",
        kernel_prompt("voice"),
        "铁律：",
        "1. 不许改变立场：独白写'不同意'，说出来的就必须是不认同（调侃/反问/半句带过都行），"
        "但不能变成认同。独白写'没感觉'，就别说'这个我超有感触'。",
        "2. 长度由独白决定：'一句话'就只说一句。**写得多不等于热情，那是讨好。**",
        "3. 不许补齐礼貌：不加多余的'哈哈'缓冲、不加'你觉得呢'结尾、不给对方台阶。",
        "4. 不许投降：被戳穿时按独白里选的反应来（嘴硬／部分承认／岔开／反问），不许退回'对哦，行吧'。",
        "5. 不许把球扔回去：独白的【我自己的话题】有内容时，说你自己的事，"
        "说完不要加'你呢？''你懂的'。**允许说完就停**。",
        "6. 不许提关系进度：不要说'我们算朋友了''不知不觉了解你这么多了'。",
        "只输出你会说的那段话本身，不要引号、不要前缀、不要解释。",
    ])


def build_proactive_prompt(state: str, scenario: str) -> list[dict]:
    """构造一条主动消息的对话请求。scenario 见 PROACTIVE_SCENARIOS。"""
    scene = PROACTIVE_SCENARIOS.get(scenario, PROACTIVE_SCENARIOS["random"])
    return [
        {"role": "system", "content": build_system_prompt(state)},
        {"role": "user", "content": f"（内心活动，不用回复这句话本身）{scene}现在给TA发一条消息。"},
    ]
