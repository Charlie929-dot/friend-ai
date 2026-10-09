"""工作系统（S3「有自己的生活」）：它自己的日子——工作循环、心流期、精力与产出。

它得有手头的活，"打断成本"才有意义——否则你永远不知道什么时候找它会打扰它。

两条设计原则：
1. **惰性推算 + 惰性结算**：不依赖常驻进程，状态由"现在几点"推出来。自用阶段服务端
   跑本机，电脑随时会休眠/关机，睡醒回来必须还能接着算（而不是计划凭空消失）。
2. **一视同仁**：它忙的是自己的日子，不是"为了陪你"。所有文案主语都是它自己，
   呈现状态（"今天第 3 件还没做完"）而不归因（红线见 corpus.COST_BLAME）。

一个工作单元 = WORK_UNIT_MINUTES 分钟；单元前 WORK_FLOW_RATIO 比例是**心流期**
（刚进入状态，打断代价最高）。每完成一个单元：消耗精力 E、产出叙事余额 + 1 条
可分享素材（供 C 模块主动分享当内容源）。做满配额或到睡觉时间 = 下班。

⚠️ "钱"是纯叙事资源，**绝不绑真实付费/内购**（需求文档已拍板）。
"""
import json
import random
import threading
from datetime import datetime, timedelta

import config
import db

# 阶段标签（HUD 与 prompt 共用）
PHASE_LABELS = {
    "idle": "还没开工",
    "working": "忙自己的事",
    "flow": "刚进入状态",
    "break": "歇会儿",
    "off": "今天忙完了",
    "sleep": "睡着",
}

# 注入 system prompt 的话：让它知道自己手头正在做什么（一视同仁 + 反愧疚：只说自己的状态）
PHASE_HINTS = {
    "idle": "你今天还没开始忙自己的事。",
    "working": "你正在忙自己的事（今天第 {done}/{quota} 件）。手头有活，回复可以短一点、慢一点。",
    "flow": "你刚忙到进入状态，这会儿脑子全在手头的事上，回复会短一些、慢一些。"
            "只说自己的状态，别提对方，别抱怨。",
    "break": "你刚忙完一件，正歇会儿（今天第 {done}/{quota} 件）。这会儿松弛，可以多聊两句。",
    "off": "你今天的事都忙完了，现在是自己的时间。",
    "sleep": "",
}

_STATE_KEY = "work_state"
_SHARE_KEY = "work_shareables"
_SHARE_KEEP = 30          # 素材池只留最近这些条（一天最多配额条，够用就好）


# --- 作息锚点：排班跟着人格走（夜猫子 10 点才起，不能按全局 8 点排） ---
def _anchor(now: datetime) -> tuple[datetime, int]:
    """今日生活日的起床时刻 + 睡觉时刻的小时数。"""
    import persona
    p = persona.behavior_params()
    day, _ = persona._life_day(now)
    return datetime(day.year, day.month, day.day, p["wake_hour"] % 24, 0), p["sleep_hour"]


def _bedtime(anchor: datetime, sleep_hour: int) -> datetime:
    """从起床时刻算睡觉时刻。睡点和起点相同或更早 = 跨到了第二天（夜猫子 10 点起、2 点睡）。"""
    bed = anchor.replace(hour=sleep_hour % 24, minute=0, second=0, microsecond=0)
    if bed <= anchor:
        bed += timedelta(days=1)
    return bed


def _jitter(minutes: int) -> int:
    """休息时长抖动：不整齐才像人（每次歇多久不一样）。"""
    return max(1, int(minutes * random.uniform(0.7, 1.4)))


def _life_day_str(now: datetime) -> str:
    import persona
    return persona._life_day(now)[0].strftime("%Y-%m-%d")


# --- 排班 ---
def _make_plan(now: datetime, quota: int) -> list[dict]:
    """排今天的工作单元：起床后磨蹭一会儿开工，做一件歇一会儿，排不下就到点收工。"""
    anchor, sleep_hour = _anchor(now)
    bed = _bedtime(anchor, sleep_hour)
    units = []
    t = anchor + timedelta(minutes=random.randint(15, 90))   # 起床后先磨蹭
    for _ in range(quota):
        end = t + timedelta(minutes=config.WORK_UNIT_MINUTES)
        if end > bed:
            break                                            # 今天排不下了，到点就睡
        units.append({"start": t.isoformat(timespec="seconds"),
                      "end": end.isoformat(timespec="seconds")})
        t = end + timedelta(minutes=_jitter(config.WORK_BREAK_MINUTES))
    return units


def plan(now: datetime | None = None, quota: int | None = None) -> list[dict]:
    """取今日排班（同一天幂等：已排过就不重排，否则每次读到的计划都在变）。"""
    now = now or datetime.now()
    quota = quota if quota is not None else config.WORK_DAILY_QUOTA
    state = _load()
    if state and state.get("day") == _life_day_str(now) and state.get("units"):
        return state["units"]
    units = _make_plan(now, quota)
    _save({"day": _life_day_str(now), "quota": quota, "units": units,
           "settled": 0, "energy": config.WORK_ENERGY_MAX, "money": 0})
    return units


# --- 存取 ---
def _load() -> dict:
    raw = db.get_kv(_STATE_KEY)
    if not raw:
        return {}
    try:
        state = json.loads(raw)
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def _save(state: dict):
    db.set_kv(_STATE_KEY, json.dumps(state, ensure_ascii=False))


def _state(now: datetime) -> dict:
    """当前状态（跨天自动重排）。"""
    state = _load()
    if not state or state.get("day") != _life_day_str(now):
        units = _make_plan(now, config.WORK_DAILY_QUOTA)
        state = {"day": _life_day_str(now), "quota": config.WORK_DAILY_QUOTA,
                 "units": units, "settled": 0,
                 "energy": config.WORK_ENERGY_MAX, "money": 0}
        _save(state)
    return state


# --- 结算：把"已经结束但还没结算"的单元补上（电脑睡了一觉也能补回来） ---
def _settle(state: dict, now: datetime) -> bool:
    changed = False
    while state["settled"] < len(state["units"]):
        unit = state["units"][state["settled"]]
        if datetime.fromisoformat(unit["end"]) > now:
            break
        if state["settled"] > 0:      # 中间歇过了，回点血
            state["energy"] = min(config.WORK_ENERGY_MAX,
                                  state["energy"] + config.WORK_ENERGY_RECOVER)
        state["energy"] = max(0, state["energy"] - config.WORK_ENERGY_COST)
        state["money"] += config.WORK_PAY_PER_UNIT
        # 工作=读书：这个单元读一段（优先用户导入的真书，没书回退内置书摘），读完产出"摘抄+读后感"素材
        article = None
        if config.READING_ENABLED:
            import library
            article = library.next_passage()          # 用户书库优先
            if article is None:
                import reading
                article = reading.next_article()      # 没导入书才读内置书摘
        _produce_shareable(state["day"], state["settled"] + 1, article)
        state["settled"] += 1
        changed = True
    if changed:
        _save(state)
    return changed


_SHARE_PROMPT = (
    "（内心活动，不用回复这句话）你刚忙完今天第 {seq} 件自己的事。"
    "用一句话说一件你刚才手头遇到或想到的、值得一说的小事——"
    "可以是一个发现、一个小麻烦、一个念头、一件好笑的事。"
    "要求：第一人称、口语、不超过 30 字、不要提对方、"
    "不要写成'我想跟你说'、不要出现'工作'这个词。")

# 工作=读书：每读完一篇，产出"摘抄金句 + 人格化读后感"（quote 是预摘的真实金句，
# 读后感由 LLM 按它的人格生成——爱吐槽就吐槽、爱感慨就感慨）。
_READING_PROMPT = (
    "（内心活动，不用回复这句话）你刚读完{what}——{source}《{title}》，"
    "里面那句「{quote}」你很有感觉。\n"
    "补一句你的读后感或联想，带你的性格（爱吐槽就吐槽、爱感慨就感慨），"
    "可以自然地带出上面这句摘抄。\n"
    "要求：第一人称、口语、合起来一两句话、别写成'我想跟你分享'、"
    "别太书面、不要出现'工作'这个词。")

_generating = False   # 有素材正在生成（同一时刻只排一条，见 _produce_shareable）


def _gen_shareable(day: str, seq: int, article: dict | None = None) -> str:
    """真的去生成一条素材（要调 LLM，所以只能在后台线程里跑）。

    article 有值（工作=读书，刚读完的那篇）→ 生成"摘抄 + 读后感"；
    article 为 None（阅读关了/兜底）→ 退回泛化"手头遇到的小事"。"""
    global _generating
    import corpus
    import llm
    import persona
    user_content = _reading_content(article, seq)
    prompt = [
        {"role": "system", "content": persona.core_identity_prompt()},
        {"role": "user", "content": user_content},
    ]
    try:
        text = llm.chat(prompt, temperature=1.0).strip()
    except llm.LLMUnavailable:
        return ""
    finally:
        _generating = False
    if not text or text == corpus.LLM_STUB:
        return ""                      # 没配 key / 拿不到内容就不硬凑
    items = _shareables()
    items.append({"day": day, "seq": seq, "text": text, "used": 0})
    db.set_kv(_SHARE_KEY, json.dumps(items[-_SHARE_KEEP:], ensure_ascii=False))
    return text


def _reading_content(article: dict | None, seq: int) -> str:
    """按"读没读到素材"选 prompt：读到 → 摘抄+读后感；没读到 → 泛化小事。"""
    if article and config.READING_ENABLED:
        genre = article.get("genre")
        what = f"一篇{genre}类的文字" if genre else "一本书里的一段"
        return _READING_PROMPT.format(
            what=what, source=article["source"], title=article["title"],
            quote=article["quote"])
    return _SHARE_PROMPT.format(seq=seq)


def _produce_shareable(day: str, seq: int, article: dict | None = None) -> None:
    """每单元产出 1 条可分享素材：它忙完一件事，脑子里留下的那点东西。

    走**后台线程 + 同时只排一条**：结算是同步的（还要喂 HUD），而素材要调 LLM。
    冷启动补账时会一次结算好几个单元（电脑睡了一觉），串行生成就是连续几次
    网络请求——网页端拉 /api/state 会被卡住好几秒。所以这里只管"排队一条"，
    生成完自然由下一个单元接上（单元间隔几十分钟，不会积压）。
    """
    global _generating
    if not config.WORK_SHAREABLE_ENABLED or _generating:
        return
    _generating = True
    threading.Thread(target=_gen_shareable, args=(day, seq, article), daemon=True).start()


def _shareables() -> list[dict]:
    raw = db.get_kv(_SHARE_KEY)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    return [x for x in items if isinstance(x, dict) and x.get("text")]


def take_shareable() -> str:
    """取一条还没用过的素材（最早的优先），取到就标记用过。供 C 模块主动分享当内容源。"""
    items = _shareables()
    for x in items:
        if not x.get("used"):
            x["used"] = 1
            db.set_kv(_SHARE_KEY, json.dumps(items, ensure_ascii=False))
            return x["text"]
    return ""


# --- 当前状态 ---
def status(now: datetime | None = None) -> dict:
    """它此刻在忙什么。返回字典，HUD / prompt / 打断成本都读这个。"""
    now = now or datetime.now()
    if not config.WORK_ENABLED:
        return {"phase": "off", "label": PHASE_LABELS["off"], "flow": False,
                "energy": config.WORK_ENERGY_MAX, "money": 0,
                "done": 0, "quota": 0, "into": 0}
    state = _state(now)
    _settle(state, now)

    if db.get_state() == "SLEEPING":
        phase = "sleep"
    elif not state["units"] or state["settled"] >= len(state["units"]):
        phase = "off"
    else:
        unit = state["units"][state["settled"]]
        start = datetime.fromisoformat(unit["start"])
        end = datetime.fromisoformat(unit["end"])
        if now < start:
            phase = "idle" if state["settled"] == 0 else "break"
        elif now < end:
            into = (now - start).total_seconds() / 60
            phase = "flow" if into < config.WORK_UNIT_MINUTES * config.WORK_FLOW_RATIO else "working"
        else:
            phase = "break"

    done = state["settled"]
    return {
        "phase": phase,
        "label": PHASE_LABELS[phase],
        "flow": phase == "flow",
        "energy": state["energy"],
        "money": state["money"],
        "done": done,
        "quota": len(state["units"]),
        "into": int((now - datetime.fromisoformat(
            state["units"][done]["start"])).total_seconds() // 60) if phase in (
            "working", "flow") else 0,
    }


def hud(now: datetime | None = None) -> str:
    """一行状态条：状态 · 进度 · 精力 · 余额。常驻可见才会自发尊重它的边界。"""
    s = status(now)
    if s["quota"] == 0:
        return ""
    return f"[{s['label']} {s['done']}/{s['quota']} · 精力 {s['energy']} · 余额 {s['money']}]"


def prompt_hint(now: datetime | None = None) -> str:
    """注入 system prompt：让它知道自己手头正在做什么（空串 = 不注入）。"""
    if not config.WORK_ENABLED:
        return ""
    s = status(now)
    hint = PHASE_HINTS.get(s["phase"], "")
    return hint.format(done=s["done"], quota=s["quota"]) if hint else ""


def tick() -> dict:
    """调度器每分钟调一次：把该结算的单元结算掉（惰性补账，错过了也能补上）。"""
    return status()


def reset():
    """清掉工作状态（换朋友 / 调试用）。"""
    db.set_kv(_STATE_KEY, "")
    db.set_kv(_SHARE_KEY, "")
