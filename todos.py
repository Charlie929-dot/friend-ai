"""待办事项队列（v0.80，D 模块「自我意识流」配套）：脑子里想说、还没说的事。

需求来源（实测 #42 #1）：TA 的素材颠来倒去就一句、而且说得突兀——因为"想聊的
东西"从来没有被记下来，聊过没聊过全看这一轮模型抽什么风。用户原设计里就有
**待办事项**，做完当然要打勾标注。

两个来源进队列：
1. 读书读到有感触的句子（`life._note_shareable_text` 产出素材时自动入队）
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
# v0.112：脑子里的念头只在当天有效。过了 24 小时还拎出来说，就是"早上那阵光"
# 从 10-16 一直硬塞到 10-21 的根因——旧念头不会自己过期。
PENDING_TTL_HOURS = 24
DONE_TTL_HOURS = 48
HINT_MAX = 3            # 每轮最多注入几条（以前把 58 条全倒进 prompt，模型只能反复挑同一条）

# 【我自己的话题】栏：独白格式里"此刻你脑子里有没有想聊的东西"，没有就写"没有"
_TOPIC_RE = re.compile(r"【我自己的话题】(.+?)(?=\n【|\Z)", re.S)
# 【要不要主动起话题】：独白自己已经判过"否"的，不许再入库（以前这个字段根本没被读）
_INITIATIVE_RE = re.compile(r"【要不要主动起话题】\s*(是|否|yes|no)", re.I)
# 独白里的"状态/情绪/不想聊"碎片不是可分享的话题——它们被当话题注入后，
# 就成了对话里那句跟上下文无关的"早上那阵光……"。
_META_RE = re.compile(
    r"(不想现在|不想聊|不想说|没别的|脑子里|脑中|书也合上|合上书|这局收|"
    r"今晚就这一件事|这会儿|懒得|困|算了|随便|没什么|不用|不接这个茬)")


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


def _age_hours(ts: str, now: datetime) -> float:
    try:
        t = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (now - t).total_seconds() / 3600.0)


def _fresh(x: dict, now: datetime) -> bool:
    ttl = DONE_TTL_HOURS if x.get("done") else PENDING_TTL_HOURS
    return _age_hours(x.get("ts"), now) <= ttl


def _prune(items: list[dict], now: datetime) -> list[dict]:
    """丢掉过期条目：没说的过一天就作废，说过的留两天供提示/排查。"""
    return [x for x in items if _fresh(x, now)]


def _similar(a: str, b: str) -> bool:
    """近似重复判定（v0.112）："早上那阵光今天终于照进来了"和"早上光沿着书脊那道缝…"
    是同一件事的变体，字面不同但说的是一个话头——旧逻辑只认完全相同，于是排了 6 条。"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b or (len(a) >= 6 and len(b) >= 6 and (a in b or b in a)):
        return True
    # 前缀相同只对短念头成立；长文本（读书素材）开头都是同一个模板，
    # 不能因为"读到《同一本书》的…"就把不同章节的素材判成重复。
    if 8 <= max(len(a), len(b)) <= 30 and a[:8] == b[:8]:
        return True
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    if ga and gb:
        return len(ga & gb) / len(ga | gb) >= 0.6
    return False


def add(text: str, source: str = "", now: datetime | None = None) -> int:
    """往队列里加一条"想说还没说的事"。相同/近似内容不重复入队。返回条目 id（0=没加）。"""
    text = " ".join((text or "").split()).strip()
    if not text:
        return 0
    now = now or datetime.now()
    items = _prune(_load(), now)
    for x in items:
        if not x.get("done") and _similar(x["text"], text):
            return 0          # 已经有类似的一条且还没说，别重复排队
    next_id = max((int(x.get("id") or 0) for x in items), default=0) + 1
    items.append({"id": next_id, "text": text, "source": source,
                  "ts": now.isoformat(timespec="seconds"), "done": 0, "done_ts": ""})
    _save(items)
    return next_id


def pending() -> list[dict]:
    """还没跟 TA 说的（按入队先后，只留当天有效的）。"""
    now = datetime.now()
    return [x for x in _load() if not x.get("done") and _fresh(x, now)]


def done_items() -> list[dict]:
    """已经说过的（打勾的）。"""
    now = datetime.now()
    return [x for x in _load() if x.get("done") and _fresh(x, now)]


def from_monologue(raw: str) -> str | None:
    """从内心独白里解析【我自己的话题】栏。

    v0.112：这个字段原来只被读出来、入库时连独白自己的判断都不看——
    独白写"【要不要主动起话题】否"照样入队，下一轮又当"想说还没说的"注入，
    于是回完正事后面硬插一句跟上下文无关的"早上那阵光…"。现在：
    自己判"否"的不入库；情绪/状态碎片（"书也合上了""不想现在开"）也不入库。
    """
    if not raw:
        return None
    m = _TOPIC_RE.search(raw)
    if not m:
        return None
    init = _INITIATIVE_RE.search(raw)
    if init and init.group(1).lower() in ("否", "no"):
        return None
    text = " ".join(m.group(1).split()).strip()
    if not text or text in _NOTHING:
        return None
    # 栏里偶尔夹着"没有，就是…"/"有，但…"这种，取掉开头的应答词
    for head in ("没有", "有"):
        if text.startswith(head) and len(text) > len(head):
            rest = text[len(head):]
            if rest[:1] in "，,。.、：: ":
                text = rest.lstrip("，,。.、：: ")
                break
    if len(_norm(text)) < 8:        # "书也合上了。"这类短碎片不是话题
        return None
    if _META_RE.search(text):       # 情绪/状态/不想聊，不是可分享的内容
        return None
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


def mark_done_by_id(todo_id: int, now: datetime | None = None) -> str | None:
    """按素材 id 打勾（§5 拍板 #6③）：主动消息生成时携带素材 id，发出后按 id 打勾，
    不做文本指纹。返回被打勾条目的文本；没找到 / 已打勾返回 None。"""
    todo_id = int(todo_id or 0)
    if not todo_id:
        return None
    items = _load()
    for x in items:
        if int(x.get("id") or 0) == todo_id and not x.get("done"):
            x["done"] = 1
            x["done_ts"] = (now or datetime.now()).isoformat(timespec="seconds")
            _save(items)
            import stream
            stream.append("todo", f"我把那件事跟{persona_name()}说了：{x['text']}", now)
            return x["text"]
    return None


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
    now = datetime.now()
    pend = [x for x in items if not x.get("done") and _fresh(x, now)]
    if not pend:
        return ""
    # v0.112：只给最近 3 条（旧版 40 条全倒进 prompt，模型每轮都在同一条上打转）。
    # 并且明确"接不上就不提"——待办是可选弹药，不是每轮必须说完的任务。
    lines = ["【想说还没说的】下面是我脑子里攒着、还没跟TA聊过的事（只列最近的几条）。"
             "先看它跟TA刚说的事有没有关系：接得上就自然聊一句；接不上就别提，"
             "这次只回TA。别硬塞，也别一次全倒出来；提的时候给出具体的东西"
             "（哪一句/哪个细节），别只给一句评价："]
    for x in pend[-HINT_MAX:]:
        lines.append(f"- {x['text']}")
    done = [x["text"] for x in items if x.get("done")][-3:]
    if done:
        lines.append("已经跟TA聊过的（打勾了，**别再原样说第二遍**）："
                     + "；".join(f"✅ {t}" for t in done))
    return "\n".join(lines)


def reset():
    db.set_kv(_KEY, "")
