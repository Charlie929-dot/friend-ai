"""兴趣修炼（S4 人格连续性延伸）：TA 的偏好从三源（读/聊/刷）动态养成。

内核（好恶/立场/雷区/语言指纹）钉死不动——那是「朋友是稳定的锚」（用户 2026-10-08）。
修炼只发生在「偏好/兴趣」这一层：TA 最近在读什么、聊什么、刷什么，就慢慢对这些
东西越来越有劲，累计到阈值转正成一条会主动提起的偏好。

纯规则、不调 LLM（用户 2026-10-11 拍板）：
- 阅读源（最强、确定性）：读完一篇 → 该 genre 直接 +1（life._settle_due 结算时埋点）。
- 聊天源：独白里「兴趣信号词 + 主题词」共现 → 该主题 +1（conservative 防误判）。
- 刷手机源（v0.97 批 3 落地）：**刷到的东西**用 `detect_topics` 纯主题词表匹配 + **最近交流过的主题**
  （近 `INTEREST_CONVO_DAYS` 个生活日双方消息），两条路命中即各 +1；累加在 `life` 的刷信息钩子里。
同一主题累计 ≥ 阈值（INTEREST_PROMOTE_THRESHOLD）→ 转正（promoted）。

存储：kv「interests」，按朋友隔离（各自 friend.db）。渲染进内核 prompt 的
「最近喜欢上的」块，也作为主动消息 random 场景的话题弹药。
"""
import json
import random
from datetime import date

import config
import corpus
import db

_KEY = "interests"


def _load() -> dict:
    raw = db.get_kv(_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    db.set_kv(_KEY, json.dumps(data, ensure_ascii=False))


def record(topic: str) -> bool:
    """给某个兴趣主题 +1；达到阈值自动转正。返回是否**刚**转正（供日志）。

    空主题/开关关闭直接跳过。主题是调用方给的（阅读源传 genre，聊天源传
    detect 命中的词表 key），这里不二次校验。
    """
    if not config.INTEREST_PROMOTE_ENABLED:
        return False
    topic = (topic or "").strip()
    if not topic:
        return False
    data = _load()
    entry = data.get(topic)
    if not isinstance(entry, dict):
        entry = {"count": 0, "promoted": False}
    entry["count"] = int(entry.get("count", 0)) + 1
    just_promoted = False
    if not entry.get("promoted") and entry["count"] >= config.INTEREST_PROMOTE_THRESHOLD:
        entry["promoted"] = True
        just_promoted = True
    entry.setdefault("first_seen", date.today().isoformat())
    data[topic] = entry
    _save(data)
    return just_promoted


def detect(text: str) -> list[str]:
    """从一段话里检测命中的兴趣主题（信号词 + 主题词共现，保守防误判）。

    只识别、不写库。返回命中的主题列表（去重、保序）。
    """
    if not config.INTEREST_PROMOTE_ENABLED:
        return []
    text = text or ""
    if not text:
        return []
    if not any(sig in text for sig in corpus.INTEREST_SIGNALS):
        return []
    hits = []
    for topic, kws in corpus.INTEREST_TOPICS.items():
        if any(kw in text for kw in kws):
            hits.append(topic)
    return hits


def detect_and_record(text: str) -> list[str]:
    """检测并记录：命中主题逐个 +1。返回**刚转正**的主题列表（供日志）。"""
    promoted_now = []
    for topic in detect(text):
        if record(topic):
            promoted_now.append(topic)
    return promoted_now


def detect_topics(text: str) -> list[str]:
    """从一段文本里按 `INTEREST_TOPICS` 主题词表匹配命中的主题（**不要信号词门槛**）。

    刷源专用（v0.97 批 3）：新闻/条目里不会写"我感兴趣"这种话，所以不能沿用
    `detect()` 的「信号词 + 主题词共现」保守门槛——这里纯主题词表匹配。
    只识别、不写库。返回命中的主题列表（去重、保序）。
    """
    if not config.INTEREST_PROMOTE_ENABLED:
        return []
    text = text or ""
    if not text:
        return []
    hits = []
    for topic, kws in corpus.INTEREST_TOPICS.items():
        if any(kw in text for kw in kws):
            hits.append(topic)
    return hits


def recent_convo_topics(now=None) -> set[str]:
    """最近 `INTEREST_CONVO_DAYS` 个生活日双方消息里出现过的兴趣主题集合。

    刷源兴趣判据的第二条路（v0.97 批 3 §1）：「不只是它本身的主题词，还和我相关
    （我做的事、问的问题、表达过的疑惑、探讨过的主题）」。

    ⚠️ 逐日回溯**必须走 `persona._life_day`**：睡一觉才算新的一天，夜里 1 点时
    `今天 - N` 这种自然日减法会把窗口取错。每天取数用 `db.today_messages(day)`
    （双方全部消息，含 user 与 assistant）。
    """
    days = int(getattr(config, "INTEREST_CONVO_DAYS", 3) or 0)
    if days <= 0 or not config.INTEREST_PROMOTE_ENABLED:
        return set()
    import persona
    from datetime import datetime, timedelta
    now = now or datetime.now()
    base, _ = persona._life_day(now)
    topics: set[str] = set()
    for i in range(days):
        day = (base - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            msgs = db.today_messages(day)
        except Exception:                   # noqa: BLE001 —— 取不到就当这天没交流
            continue
        for m in msgs:
            topics.update(detect_topics(m.get("content", "")))
    return topics


def record_topics(topics) -> list[str]:
    """给一批主题各 +1；**同一批内同主题只算一次**（防一轮 3 条同主题冲过阈值）。

    返回**刚转正**的主题列表（供日志）。刷源的累加调用方（`life` 钩子）用它。
    """
    promoted_now = []
    seen: set[str] = set()
    for t in topics:
        t = (t or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        if record(t):
            promoted_now.append(t)
    return promoted_now


def promoted() -> list[dict]:
    """已转正的兴趣偏好（按计数降序）。"""
    data = _load()
    items = [{"topic": t, "count": int(e.get("count", 0))}
             for t, e in data.items()
             if isinstance(e, dict) and e.get("promoted")]
    items.sort(key=lambda x: -x["count"])
    return items


def random_promoted() -> str:
    """随机取一个已转正的兴趣主题（供主动消息当话题弹药）。无则返回空串。"""
    items = promoted()
    if not items:
        return ""
    return random.choice(items)["topic"]


def prompt_hint() -> str:
    """渲染「最近喜欢上的」块，供内核 prompt 注入（compact/full 层）。

    没有转正的偏好返回空串。写这句约束是为了守住阶段 A 的红线：这些是
    「兴趣」，不是「正在做的事」——别把它们编成"我去了现场/我动手做了"。
    """
    if not config.INTEREST_PROMOTE_ENABLED:
        return ""
    items = promoted()
    if not items:
        return ""
    topics = "、".join(it["topic"] for it in items)
    return (f"【我最近喜欢上的】{topics}。这些是从我最近读的、聊的、刷到的东西里"
            f"慢慢长出来的兴趣，是我自己的一部分。可以自然地提起、多聊两句；"
            f"但别把它们编成'你正在做的事'（你没有身体，别假装去现场/动手做了）。")
