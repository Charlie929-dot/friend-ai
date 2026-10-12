"""待办事项队列（v0.80，D 模块「自我意识流」配套）：脑子里想说、还没说的事。

需求来源（实测 #42 #1）：TA 的素材颠来倒去就一句、而且说得突兀——因为"想聊的
东西"从来没有被记下来，聊过没聊过全看这一轮模型抽什么风。用户原设计里就有
**待办事项**，做完当然要打勾标注。

两个来源进队列：
1. 读书读到有感触的句子（`work._gen_shareable` 产出素材时自动入队）
2. TA 自己冒出来的念头（独白【我自己的话题】栏，非空即入队）

每轮把"还没说的"打印进上下文，说过了就**打勾标注做完**——做完的不再当话题，
这直接把"颠来倒去就一句"治掉。

存储：`kv.todos`（按朋友隔离，和记忆/意识流一样一人一份）。
"""
import json
import re
from datetime import datetime

import config
import db

_KEY = "todos"
_MAX_KEEP = 40          # 只留最近这些条（够一天用，防无限增长）
_NOTHING = {"没有", "无", "暂无", "没有。", "无。", "没什么", "没"}

# 【我自己的话题】栏：独白格式里"此刻你脑子里有没有想聊的东西"，没有就写"没有"
_TOPIC_RE = re.compile(r"【我自己的话题】(.+?)(?=\n【|\Z)", re.S)


def _load() -> list[dict]:
    raw = db.get_kv(_KEY)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    return [x for x in items if isinstance(x, dict) and x.get("text")]


def _save(items: list[dict]):
    db.set_kv(_KEY, json.dumps(items[-_MAX_KEEP:], ensure_ascii=False))


def _norm(text: str) -> str:
    """指纹化：去掉空白和标点，只留字（中文 \w 是字，标点算 \W）。"""
    return re.sub(r"[\s\W_]+", "", text or "")


def add(text: str, source: str = "", now: datetime | None = None) -> int:
    """往队列里加一条"想说还没说的事"。相同内容不重复入队。返回条目 id（0=没加）。"""
    text = " ".join((text or "").split()).strip()
    if not text:
        return 0
    items = _load()
    fp = _norm(text)
    for x in items:
        if _norm(x["text"]) == fp and not x.get("done"):
            return 0          # 已经有同一条且还没说，别重复排队
    now = now or datetime.now()
    next_id = max((int(x.get("id") or 0) for x in items), default=0) + 1
    items.append({"id": next_id, "text": text, "source": source,
                  "ts": now.isoformat(timespec="seconds"), "done": 0, "done_ts": ""})
    _save(items)
    return next_id


def pending() -> list[dict]:
    """还没跟 TA 说的（按入队先后）。"""
    return [x for x in _load() if not x.get("done")]


def done_items() -> list[dict]:
    """已经说过的（打勾的）。"""
    return [x for x in _load() if x.get("done")]


def from_monologue(raw: str) -> str | None:
    """从内心独白里解析【我自己的话题】栏。非空（也不是"没有"）就返回这条话题。"""
    if not raw:
        return None
    m = _TOPIC_RE.search(raw)
    if not m:
        return None
    text = " ".join(m.group(1).split()).strip()
    if not text or text in _NOTHING:
        return None
    # 栏里偶尔夹着"没有，就是…"这种，取掉开头的"没有"
    if text.startswith("没有") and len(text) > 4:
        text = text[2:].lstrip("，,、：: ")
    return text or None


def _hit(text: str, said: str) -> bool:
    """这条待办是不是在 TA 刚说的话里被说到了（打勾判定）。

    不做语义，只做字面指纹：取条目开头 6 个字（去标点），在回复里能找到就算说过；
    开头没命中再试中间一段（TA 可能从半句开始引）。
    """
    t, s = _norm(text), _norm(said)
    if not t or not s:
        return False
    n = 6
    if len(t) <= n:
        return t in s
    return t[:n] in s or t[len(t) // 2:len(t) // 2 + n] in s


def mark_done(said: str, now: datetime | None = None) -> list[str]:
    """TA 刚说完一段话：把说到的待办打勾。返回被打勾的条目文本列表。

    打勾 = 往意识流追加一条「我把那件事跟TA说了」——那是它今天真的做了的事，
    也是"做完就不许再当新话题"的依据。
    """
    said = said or ""
    if not said:
        return []
    items = _load()
    now = now or datetime.now()
    hit = []
    for x in items:
        if x.get("done"):
            continue
        if _hit(x["text"], said):
            x["done"] = 1
            x["done_ts"] = now.isoformat(timespec="seconds")
            hit.append(x["text"])
    if hit:
        _save(items)
        import stream
        for t in hit:
            stream.append("todo", f"我把那件事跟{persona_name()}说了：{t}", now)
    return hit


def persona_name() -> str:
    """意识流里的称呼（延迟导入，避免与 persona 成环）。"""
    import persona
    return persona.user_name()


def prompt_hint() -> str:
    """每轮注入的【想说还没说的】块：没说的列出来当话题，说过的标 ✅ 提醒别重复。"""
    if not config.TODOS_ENABLED:
        return ""
    items = _load()
    pend = [x for x in items if not x.get("done")]
    if not pend:
        return ""
    lines = ["【想说还没说的】下面是你脑子里攒着、还没跟TA聊过的事。"
             "想聊就从这里挑一件自然地聊，别硬塞，也别一次全倒出来："]
    for x in pend:
        lines.append(f"- {x['text']}")
    done = [x["text"] for x in items if x.get("done")][-3:]
    if done:
        lines.append("已经跟TA聊过的（打勾了，**别再原样说第二遍**）："
                     + "；".join(f"✅ {t}" for t in done))
    return "\n".join(lines)


def reset():
    db.set_kv(_KEY, "")
