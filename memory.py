"""记忆系统 v1：自动提取 + 时间线式存储 + 相关召回 + 话题弹药。

设计取舍（单用户场景，先跑通再优化）：
- 提取：每轮对话后后台线程调一次 LLM，从最近对话里抽"值得长期记住的事"。
- 存储：**追加式时间线，不去重不合并**。同一话题的新进展直接新记一条，
  靠 (category, theme) 分组，/memories 里同组按时间排列，能看到认知的演变。
  仅跳过与已有记忆原文完全相同的重复（防相邻轮次重复提取）。
- 时间标签绝对化：提取时注入当前日期，"今天下午"→"2026-10-06 下午"，
  召回时查询里的相对时间词也会换算成日期去匹配。
- 召回：字符 bigram 重叠打分 + 时间标签加成；命中后同组（category+theme
  完全一致）的更新进展一起带出，AI 看到的是最新状态而非过时记忆。
- 弹药：random 主动话题有概率接一条旧记忆（"你之前说的那个……后来咋样了"）。
- 事实便签（/note）仍是最高优先级的"用户亲口确认"层，与本系统并存。
"""
import json
import re
import threading
from datetime import datetime, timedelta

import config
import corpus
import db
import llm

EXTRACT_PROMPT = """你在回想并写下自己的记忆——从最近的对话里，记下对你（{WHO}）来说有趣、重要、和你有关系的事。

对话里"我"是{WHO}，跟你说话的"{USER}"只是你的一个朋友（不是需要特殊对待的对象）。
标注"（这条对方还没回应）"的"我"的消息，对方还没看到——不能记成对方已回应/已答应。

输出：一个 JSON 数组，不要输出任何其他文字。每项格式：
{
  "content": "一句话记忆（<50字，一条只说一件事）",
  "category": "画像 | 偏好 | 计划 | 事件 | 人际 | 情绪",
  "time": "绝对时间标签——格式 YYYY-MM-DD，可带时段（如 2026-10-06 下午）。"
          "参照【现在时间】把相对时间换算成绝对日期：'今天下午'→当天日期+下午，"
          "'明天早上'→次日日期+早上，'下周三'→对应日期。"
          "定位不到具体日期的（最近/以后/改天/有空）一律留空字符串",
  "place": "地点（没有就空字符串）",
  "person": "涉及的人名（你自己或别人，可多个，逗号分隔；没有就空字符串）",
  "subject": "视角——这条是关于谁的：我（我自己）/ 对方（正在聊的这个朋友）/ 某人（提到的第三方）/ 我们（我们之间的关系或共同经历）。四选一",
  "theme": "学业 | 技术 | 健康作息 | 饮食 | 娱乐 | 社交情感 | 出行 | 财务 | 其他"
}
如果这条信息"一句两句说不完"——比如对方给你完整讲了一段经历/故事、一次深入的讨论、
一个详细的技术方案、一起做过的某件事的完整过程——不要硬压成一句话。这时：
- "content" 仍写一句摘要（<50字，说清这是件什么事）
- 额外加 "long": true 和 "full": "完整内容"（把细节尽量原样保留，可写到几百字）
普通的一句话事实、偏好、计划、情绪不需要 long/full，只要 content 即可。
如果这条信息属于对方的"个人清单"字段（称呼/生日/城市/学校专业/年级/作息习惯/
兴趣爱好/在忙的事/雷区），额外加一项 "profile": "字段名：值"
（如 "profile": "作息习惯：夜猫子，凌晨才睡"）。只有对方亲口陈述的确定性事实才能进清单；
一次性的计划、情绪、事件不进清单。

【类别说明】这些类别可以描述任何一个人——你自己、对方、或提到的别人。
画像=某个人是谁（称呼/城市/身份）；偏好=喜欢/讨厌；计划=打算做的事；
事件=已经发生的事；人际=某个人与别人的关系；情绪=状态与感受。

【提议删除】如果发现某条已知记忆是错的——是"我"自己说的话、对方明确否定过、
或与对话中的新事实直接矛盾——输出一项：{"content": "旧记忆中的关键词", "delete": true}。
删除不会立即生效，会标记为待确认，由对方最终决定。不要因"信息过时"就提议删除：
过时的计划/状态是时间线的一部分，新进展新记一条即可。

【值得记的——别漏】
- 对你（{WHO}）来说有趣、重要、和你有关系的事：对方说的、你自己说的、以及你们之间的关系。
- **你自己的事也要记**：你的偏好、计划、想法、经历——不只有对方的事才值得记。
- **你们的互动里程碑**：深入讨论过的话题、你帮对方弄懂的东西、一起做过的事。
  例："我给{USER}详细讲过SVD图像压缩的原理"——共同经历用一句话记下，这是你们的关系。
- **同一话题的新进展、新细节、计划变更**：直接新记一条，不要和旧记忆合并，
  也不要改写旧记忆。系统会把同主题的记忆排成时间线，能看到事情的演变。
  只有和已知记忆内容几乎完全一样的，才不要重复提取。

【质量红线——违反就不要输出】
- 不记你对对方的客套、调侃、评价、起的昵称——那不是事实。
  反例："{USER}被我称为技术宅"——那是"我"自己说的俏皮话，一律不记。
  但你自己真实说过的偏好、计划、想法、经历要记（如"我其实喜欢熬夜看书"）。
- 不记闲聊客套、一次性话题
- **不记"失忆"类（关键）**：不记"我"不记得/想不起来某事、不记对方追问"记不记得"、
  发现我失忆、说我失忆这类对话过程，也不记"某位朋友失忆了"这种描述——这是对话
  元信息，会把"失忆"固化成人设，形成"越问越不记得"的死循环。事情本身才记：
  群里聊了 X → 记"群里聊了 X"，而不是"对方问我记不记得群里聊了 X"。
- 一条记忆只说一件事，别囫囵吞枣把多个事实塞进一句
- 兜底（程序还会再过滤一道，漏网=失败）：元信息、不确定推测、
  把对方还没回应的提议记成"{USER}答应…"

没有合格的新信息就输出：[]"""


def _extract_prompt(group: bool = False) -> str:
    """提取器 system prompt：注入当前朋友的名字，模型才知道"我"是谁。

    不注名字时模型只能靠猜，会写出"小徐对小徐（AI）说…"这种四不像自称
    （2026-10-07 实测）；prompt 里全项目不出现"AI"身份——TA 就是抽到的角色本人。
    group=True：群聊提取，追加多对象规则（profile_person 注明是谁、视角判定）。
    """
    try:
        import persona_pool
        card = persona_pool.load()
        name = card["name"] if card else ""
    except Exception:
        name = ""
    import persona  # 延迟导入避免循环依赖
    prompt = (EXTRACT_PROMPT
              .replace("{WHO}", name or "这位朋友")
              .replace("{USER}", persona.user_name()))
    if group:
        prompt += _GROUP_EXTRACT_EXTRA.replace("{WHO}", name or "这位朋友")
    return prompt


_GROUP_EXTRACT_EXTRA = """\n\n【群聊场景】这段对话是群聊，除了你（{WHO}）还有好几个人，都是你的朋友、一视同仁。
- 记「profile」清单字段时，额外加一项 "profile_person"：这条字段是关于谁的（用那个人在对话里的名字；就是正在私聊的对方本人时可不写，默认记给 TA）。
- 视角 subject 里「对方」可以指群里任意一位朋友，「某人」指提到的、不在场的第三方。
- 多个朋友各有各的事，别把 A 的事记到 B 头上。"""


# ---------- 确定性后处理：能正则判断的红线不进 prompt（2026-10-07 用户拍板）----------
# 红线词表统一收在 corpus（语料，可扩充），这里 compile 成正则对象
_SPECULATION = re.compile(corpus.SPECULATION)                 # 推测词开头 → 丢弃
_META = re.compile(corpus.META)                               # 元信息 → 丢弃
_PROMISE = re.compile(corpus.PROMISE)                         # 承诺时序安全网
_SELF_AMNESIA = re.compile(corpus.SELF_AMNESIA)               # 失忆自证循环
_MEMORY_QUIZ = re.compile(corpus.MEMORY_QUIZ)                 # 记忆追问元信息
_MEMORY_REMINDER = re.compile(corpus.MEMORY_REMINDER)         # 叮嘱别忘
# 高置信自称改写：口误"AI"→"我"（先改写）。这是固定改写规则、不扩充，留本地。
# prompt 已无 AI 身份提示，这些改写只兜历史习惯/偶发口误；
# 合法话题（AI项目/朋友型AI）不受影响。
_SELF_REWRITE = (("（AI）", ""), ("(AI)", ""),
                 ("AI助手", "我"), ("告诉AI", "告诉我"), ("对AI", "对我"), ("跟AI", "跟我"),
                 ("和AI", "和我"), ("被AI", "被我"), ("向AI", "向我"),
                 ("AI问", "我问"), ("AI说", "我说"), ("AI叫", "我叫"), ("AI称", "我称"),
                 ("AI提醒", "我提醒"), ("AI邀请", "我邀请"), ("AI提议", "我提议"), ("AI推荐", "我推荐"),
                 ("AI给", "我给"), ("AI帮", "我帮"), ("AI陪", "我陪"), ("AI夸", "我夸"))


def _validate(content: str, assistant_tail: bool) -> str | None:
    """确定性红线过滤。返回清洗后的 content；该丢弃返回 None。

    assistant_tail=True 表示本轮提取窗口里用户最后一条发言之后还有"我"的
    消息（用户没机会回应），此时"答应/承诺/保证"类记忆一律丢弃——
    正常情况结构化标注已防住，这是双保险。
    """
    if _SPECULATION.search(content):
        print(f"[memory] 过滤（推测）：{content[:30]}")
        return None
    if _META.search(content):
        print(f"[memory] 过滤（元信息）：{content[:30]}")
        return None
    if assistant_tail and _PROMISE.search(content):
        print(f"[memory] 过滤（承诺时序安全网）：{content[:30]}")
        return None
    for old, new in _SELF_REWRITE:
        content = content.replace(old, new)
    if _SELF_AMNESIA.search(content):
        print(f"[memory] 过滤（失忆自证循环）：{content[:30]}")
        return None
    if _MEMORY_QUIZ.search(content):
        print(f"[memory] 过滤（记忆追问元信息）：{content[:30]}")
        return None
    if _MEMORY_REMINDER.search(content):
        print(f"[memory] 过滤（叮嘱别忘元信息）：{content[:30]}")
        return None
    return content


def _format_convo(rows: list[dict]) -> tuple[str, bool]:
    """拼对话文本 + 结构化标注：用户最后一条发言之后的"我"的消息标记为未回应。

    返回 (convo, assistant_tail)。承诺类错位归因（反馈记录 #4）靠上下文
    结构从源头防——提取器直接"看见"哪些消息用户还没回应，不靠 prompt 规则。
    """
    last_user = max((i for i, r in enumerate(rows) if r["role"] == "user"), default=-1)
    convo = "\n".join(
        f"{'对方' if r['role'] == 'user' else ('我（这条对方还没回应）' if i > last_user else '我')}"
        f"：{r['content']}"
        for i, r in enumerate(rows))
    return convo, bool(rows) and rows[-1]["role"] != "user"


def extract_async() -> None:
    """每轮对话后后台提取，不阻塞回复。"""
    t = threading.Thread(target=_extract_safe, daemon=True)
    t.start()


def _extract_safe() -> None:
    try:
        with db.PATH_LOCK:   # 与用户切换朋友互斥：提取全程读写的是同一个库
            _extract()
    except Exception as e:
        print(f"[memory] 提取失败：{e}")


def _extract() -> None:
    context = db.recent_context(4)  # 最近 4 轮，够判断"新信息"
    if len(context) < 2:
        return
    known = [m["content"] for m in db.all_memories(limit=50)]
    facts = db.get_facts()

    convo, assistant_tail = _format_convo(context)
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    # 注入当前时间，提取器才能把"今天下午"换算成绝对日期标签
    user_msg = (f"【现在时间】{now:%Y-%m-%d} 周{week} {now:%H:%M}\n\n"
                f"【最近对话】\n{convo}")
    if known or facts:
        user_msg += "\n\n【已知记忆（内容几乎一样的不要重复提取）】\n" + "\n".join(
            f"- {x}" for x in (facts + known)[:60])

    raw = llm.chat([
        {"role": "system", "content": _extract_prompt()},
        {"role": "user", "content": user_msg},
    ], temperature=0.2)
    items = _parse_json_array(raw)
    for it in items:
        _apply(it, assistant_tail)


def _parse_json_array(raw: str) -> list[dict]:
    """容错解析：模型偶尔会给 JSON 包一层废话。"""
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return [x for x in data if isinstance(x, dict) and x.get("content")]
    except ValueError:
        return []


_CATEGORIES = ("画像", "偏好", "计划", "事件", "人际", "情绪")
_THEMES = ("学业", "技术", "健康作息", "饮食", "娱乐", "社交情感", "出行", "财务", "其他")
_SUBJECTS = ("我", "对方", "某人", "我们")  # 记忆视角（多对象一视同仁）
# 旧版英文类别 → 新类别（老朋友库里的历史记忆）
_LEGACY_CATEGORY = {"fact": "画像", "preference": "偏好", "plan": "计划",
                    "event": "事件", "person": "人际"}


def _apply(item: dict, assistant_tail: bool = False, source: str = "private") -> bool:
    """落库一条提取结果。返回是否新记了一条。

    source: 'private'（1:1 提取）/ 'group'（群聊提取）——信息调度层据此
    在群聊场景下优先召回群聊来源的记忆。"""
    content = str(item["content"]).strip()

    # AI 提议删除：只标记待确认，不真删（用户 /delmem 确认、/keepmem 保留）
    if item.get("delete"):
        old = db.find_memory_by_keyword(content)
        if old and old.get("status", "ok") == "ok":
            db.set_memory_status(old["id"], "pending_delete")
            print(f"[memory] 提议删除（待确认）：{old['content'][:30]}")
        return False

    # A/B 两级库：带 long+full 的是"一句两句说不完"的长内容——摘要进 A 库
    # （memories，召回只看它），全文进 B 库（long_memories），A 库带 long_id 伪链接
    full = str(item.get("full", "")).strip()
    is_long = bool(item.get("long")) and len(full) >= 20

    # 长度约束：长文摘要放宽（正文在 B 库），普通记忆仍 4~200
    max_len = 100 if is_long else 200
    if len(content) < 4 or len(content) > max_len:  # 太短没信息量，太长是模型跑偏
        return False

    # 确定性红线（推测/元信息/自称/承诺时序安全网），能代码判断的不占 prompt
    cleaned = _validate(content, assistant_tail)
    if cleaned is None:
        return False
    content = cleaned

    # 个人清单：属于 profile 字段的确定性事实同步进 TA 的清单（/profile 可见）
    profile_line = str(item.get("profile", "")).strip()
    if profile_line:
        _apply_profile(profile_line,
                       person=_resolve_profile_person(item.get("profile_person")))

    category = str(item.get("category", "画像")).strip()
    category = _LEGACY_CATEGORY.get(category, category)
    if category not in _CATEGORIES:
        category = "画像"
    theme = str(item.get("theme", "")).strip()
    if theme not in _THEMES:
        theme = "其他" if theme else ""
    time_label = str(item.get("time", "")).strip()
    place = str(item.get("place", "")).strip()
    person = str(item.get("person", "")).strip()
    tags = str(item.get("tags", "")).strip()
    subject = str(item.get("subject", "")).strip()
    if subject not in _SUBJECTS:
        subject = ""  # 非法/缺失一律落空（历史遗留未知，不猜）

    # 长文全文也过确定性红线 + 截断防失控（B 库单条上限 2000 字）
    if is_long:
        full = full[:2000]
        cleaned_full = _validate(full, assistant_tail)
        if cleaned_full is None:   # 全文被污染 → 摘要一起丢弃
            return False
        full = cleaned_full

    # 追加式时间线：不去重不合并（同主题的新进展是历史的一部分），
    # 仅跳过原文完全相同的（防相邻轮次把同一段话重复提取）
    for m in db.all_memories(limit=200):
        if m["content"] == content:
            return False

    if is_long:
        long_id = db.add_long_memory(content, full, category, tags, time_label,
                                     place, person, theme, source, subject)
        db.add_memory(content, category, tags, time_label, place, person, theme,
                      source, subject, long_id=long_id)
        print(f"[memory] 记住长文：({category}|{theme or '—'}) {content[:30]}…"
              f"（全文 {len(full)} 字，B库#{long_id}）")
        return True

    db.add_memory(content, category, tags, time_label, place, person, theme,
                  source, subject)
    dims = "".join(f" @{x}" for x in (time_label, place, person) if x)
    print(f"[memory] 记住：({category}|{theme or '—'}) {content[:30]}{dims}")
    return True


def _resolve_profile_person(name: str | None) -> str:
    """把提取器给的 profile_person 归一成 profiles 键：真人→config.USER_NAME，其余原样。

    群聊里真人可能被写成 config.USER_NAME（本名）或 user_name()（该朋友的称呼），
    也可能沿旧习惯写"用户/对方"——统一归到真人键 config.USER_NAME。
    """
    name = (name or "").strip()
    if not name or name in ("用户", "对方"):
        return config.USER_NAME
    import persona
    if name in (config.USER_NAME, persona.user_name()):
        return config.USER_NAME
    return name


def _apply_profile(line: str, person: str | None = None) -> None:
    """把一条"字段：值"写进某对象的 profile（profiles 多对象表）。

    person 不给时 = 当前私聊对象（真人，config.USER_NAME 作键）。
    规则：
    - 必须有"字段：值"结构（否则只是普通记忆，不进清单）
    - 字段已存在时：AI 自己提取过的字段（source=extracted）才更新值；
      用户手填的（source=manual）不覆盖——以用户为准
    """
    m = re.match(r"^([^：:]{1,10})[：:]\s*(.+)$", line.strip())
    if not m:
        return
    key, value = m.group(1).strip(), m.group(2).strip()
    if not value or len(value) > 60:
        return
    person = person or config.USER_NAME
    existing = db.profile_field(person, key)
    if existing is not None and existing.get("source") == "manual":
        return  # 用户手填的字段：以用户为准，不覆盖
    db.set_profile_field(person, key, value, source="extracted")
    print(f"[memory] 清单更新（{person}）：{key}：{value}")


def backfill_history(batch: int = 16, progress=print) -> int:
    """历史回填：把旧聊天记录分批喂给提取器，将老朋友时代的信息补进记忆。
    断点续跑：kv.memfill_offset 记录已处理到的消息 id，中断后再跑接着来。
    关键：每批用该批消息的时间戳作为【现在时间】参照——10 月 5 号说的
    "今天下午"会锚定到 10-05，而不是回填当天。返回新记的记忆条数。"""
    offset = int(db.get_kv("memfill_offset", "0") or 0)
    known_facts = db.get_facts()
    added = 0
    while True:
        rows = db.messages_page(offset, batch)
        if not rows:
            break
        convo, assistant_tail = _format_convo(rows)
        # 用这批消息最后一条的时间作为"现在"，相对时间词才锚定到事发当天
        try:
            ref = datetime.fromisoformat(rows[-1]["ts"])
        except (ValueError, KeyError):
            ref = datetime.now()
        week = "一二三四五六日"[ref.weekday()]
        user_msg = (f"【现在时间】{ref:%Y-%m-%d} 周{week} {ref:%H:%M}\n\n"
                    f"【最近对话】\n{convo}")
        known = [m["content"] for m in db.all_memories(limit=50)]
        if known or known_facts:
            user_msg += "\n\n【已知记忆（内容几乎一样的不要重复提取）】\n" + "\n".join(
                f"- {x}" for x in (known_facts + known)[:60])
        raw = llm.chat([
            {"role": "system", "content": _extract_prompt()},
            {"role": "user", "content": user_msg},
        ], temperature=0.2)
        for it in _parse_json_array(raw):
            if _apply(it, assistant_tail):
                added += 1
        offset = rows[-1]["id"]
        db.set_kv("memfill_offset", str(offset))
        progress(f"[memfill] 已处理到第 {offset} 条消息，新记 {added} 条…")
    return added


_GROUP_PREFIX = re.compile(r"^【群聊·([^】]+)】\s*")


def backfill_group(batch: int = 12, progress=print) -> int:
    """补提取群聊记忆：把 messages 里 kind='group' 的记录重新喂给提取器，
    写 source='group' 的记忆。修复散群时提取失败（如旧库缺列）导致的遗漏。

    群聊记录 content 格式 = 「【群聊·发言人】内容」，发言人可能是多个朋友；
    提取时保留发言人的名字（真人=config.USER_NAME，其余朋友名），让提取器分清谁说了什么。
    「我」= 当前朋友（_extract_prompt 注入的人格名）。断点续跑：memfill_group_offset。"""
    offset = int(db.get_kv("memfill_group_offset", "0") or 0)
    added = 0
    while True:
        rows = db.group_messages_page(offset, batch)
        if not rows:
            break
        lines = []
        for r in rows:
            m = _GROUP_PREFIX.match(r["content"])
            if m:
                lines.append(f"{m.group(1)}：{r['content'][m.end():]}")
            else:
                lines.append(r["content"])
        convo = "\n".join(lines)
        # 用这批消息最后一条的时间作参照，相对时间词才锚定到事发当天
        try:
            ref = datetime.fromisoformat(rows[-1]["ts"])
        except (ValueError, KeyError):
            ref = datetime.now()
        week = "一二三四五六日"[ref.weekday()]
        user_msg = (f"【现在时间】{ref:%Y-%m-%d} 周{week} {ref:%H:%M}\n\n"
                    f"【群聊记录】\n{convo}")
        known = [m["content"] for m in db.all_memories(limit=50)
                 if m.get("status", "ok") == "ok"]
        if known:
            user_msg += ("\n\n【已知记忆（内容几乎一样的不要重复提取）】\n"
                         + "\n".join(f"- {x}" for x in known[:50]))
        raw = llm.chat([
            {"role": "system", "content": _extract_prompt(group=True)},
            {"role": "user", "content": user_msg},
        ], temperature=0.2)
        for it in _parse_json_array(raw):
            if _apply(it, False, source="group"):
                added += 1
        offset = rows[-1]["id"]
        db.set_kv("memfill_group_offset", str(offset))
        progress(f"[memfill-group] 已处理到第 {offset} 条，新记 {added} 条…")
    return added


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


_STOP = set(corpus.STOP_CHARS)


def _keywords(s: str) -> set[str]:
    """有意义的单字（去虚词），补上 bigram 覆盖不到的弱相关。"""
    return {ch for ch in re.sub(r"\s", "", s) if ch not in _STOP and '一' <= ch <= '鿿'}


_REL_DAYS = corpus.REL_DAYS
_PERIODS = corpus.PERIODS


def _query_time_tokens(query: str) -> list[str]:
    """把 query 里的相对时间词换算成绝对日期/时段，用于匹配 time_label。
    "今天下午" → ["2026-10-06", "下午"]，与记忆里的绝对时间标签对齐。"""
    today = datetime.now().date()
    tokens = []
    for word, off in _REL_DAYS.items():
        if word in query:
            tokens.append((today + timedelta(days=off)).isoformat())
    tokens.extend(p for p in _PERIODS if p in query)
    return tokens


# 私聊场景下，享受"新鲜度加分"的最近群聊记忆条数（原来兜底注入也是 5 条）
_GROUP_RECENT_COUNT = 5

# 身份/关系询问的意图词：词表在 corpus（语料，可扩充）
_IDENTITY_QUERY = re.compile(corpus.IDENTITY_QUERY)


def _query_person_names(query: str) -> list[str]:
    """识别 query 里出现的人物名（朋友名 + 用户称呼 + 别名），用于 person 标签命中加权。"""
    import persona
    import persona_pool
    names = {config.USER_NAME, persona.user_name()}
    for p in persona_pool.POOL:
        names.update(p["names"])
    try:
        alias = db.get_kv("alias")
        if alias:
            names.add(alias)
    except Exception:
        pass
    return [n for n in names if n in query]


# 追问细节的意图词：词表在 corpus（语料，可扩充）
_EXPAND_QUERY = re.compile(corpus.EXPAND_QUERY)


def wants_expansion(query: str) -> bool:
    """判断 query 是否在追问细节——此时命中的长记忆应展开全文（B 库）。"""
    return bool(_EXPAND_QUERY.search(query))


# 回忆/追问信号：命中则启用 function calling 让 AI 主动查记忆（词表在 corpus）。
# 普通闲聊不含这些词，走快速路径（省 token、省延迟），只有"反复要求回忆"才主动检索。
_MEMORY_SEARCH_QUERY = re.compile(corpus.MEMORY_SEARCH_QUERY)


def wants_memory_search(text: str) -> bool:
    """判断用户消息是否在要求回忆/追问——此时启用 function calling 主动检索。"""
    return bool(_MEMORY_SEARCH_QUERY.search(text))


def _attach_long(mems_out: list[dict], expand_long: bool) -> list[dict]:
    """expand_long 时，把命中的长记忆全文从 B 库取出来附到 long_content。"""
    if not expand_long:
        return mems_out
    for m in mems_out:
        if m.get("long_id"):
            long = db.get_long_memory(m["long_id"])
            if long and long.get("status", "ok") == "ok":
                m["long_content"] = long["content"]
                db.touch_long_memory(m["long_id"])
    return mems_out


def expand_long_memory(mem: dict) -> str | None:
    """取一条 A 库记忆对应的 B 库全文；无长文（long_id=0）返回 None。"""
    long_id = mem.get("long_id")
    if not long_id:
        return None
    long = db.get_long_memory(long_id)
    if long and long.get("status", "ok") == "ok":
        db.touch_long_memory(long_id)
        return long["content"]
    return None


def recall(query: str, top_k: int = 5, group_first: bool = False,
           expand_long: bool = False, source: str | None = None) -> list[dict]:
    """按与 query 的相关度召回 top-K 记忆。
    记忆总量还小（≤10）时直接全量返回——早期每条都珍贵。
    打分：bigram/关键词重叠 + 时间标签加成（query 的相对时间词换算成日期匹配）。
    命中后同组（category+theme 完全一致）的更新进展一起带出——
    AI 看到的是时间线的最新状态，不是过时记忆。
    group_first=True（群聊场景）：群聊来源的记忆（source='group'）一律加分优先——
    同处一个群里的往事，比私聊里的事更贴合当下群聊语境。
    group_first=False（私聊场景）：最近 _GROUP_RECENT_COUNT 条群聊记忆加分
    （温和于群聊场景）——私聊也能自然想起群里最近聊过的事、不会"群聊失忆"；
    更早的群聊记忆靠关键词命中召回。"""
    mems = [m for m in db.all_memories(limit=200)
            if m.get("status", "ok") == "ok"]  # 待确认删除的不参与召回
    # 来源过滤：AI 主动检索时按用户语境指定（"群聊"→只在群聊记忆里搜）。
    # 一旦指定 group，群聊记忆全量参与，不再受私聊场景"最近 5 条"的新鲜度限制。
    if source in ("group", "private"):
        mems = [m for m in mems if m.get("source", "private") == source]
    if not mems:
        return []
    if len(mems) <= 10:
        if group_first:
            # 记忆少时也把群聊来源的排前面
            mems.sort(key=lambda m: (0 if m.get("source", "private") == "group" else 1,
                                     -m["id"]))
        for m in mems:
            db.touch_memory(m["id"])
        return _attach_long(mems[:top_k * 2], expand_long)
    q_bi = _bigrams(query)
    q_kw = _keywords(query)
    time_tokens = _query_time_tokens(query)
    # 意图感知：问"是谁/认识/记得"→ 人际记忆优先；query 点到人名 → person 标签命中优先
    identity_q = bool(_IDENTITY_QUERY.search(query))
    q_names = _query_person_names(query)
    # 私聊场景：最近几条群聊记忆给温和加分（新鲜度兜底，防"群聊失忆"）
    recent_group_ids: set[int] = set()
    if not group_first:
        gids = sorted((m["id"] for m in mems
                       if m.get("source", "private") == "group"), reverse=True)
        recent_group_ids = set(gids[:_GROUP_RECENT_COUNT])
    scored = []
    for m in mems:
        # 五维标签全部参与匹配（人物/地点/主题往往是检索入口）
        hay = (m["content"] + m["tags"] + m.get("time_label", "")
               + m.get("place", "") + m.get("person", "") + m.get("theme", ""))
        score = len(q_bi & _bigrams(hay)) * 2 + len(q_kw & _keywords(hay)) * 0.5
        # 时间标签衔接："今天下午"换算成日期后命中记忆的绝对时间标签
        score += sum(3.0 for t in time_tokens if t in m.get("time_label", ""))
        # 人名标签命中：query 里点到的名字，person 标签精确命中是强信号（标签 > 字面）
        if q_names and any(n in m.get("person", "") for n in q_names):
            score += 2.0
        # 身份/关系询问：人际类记忆（TA 与谁的关系）优先于琐事——
        # 修"记得小葵吗"只召回"小葵拿快递"、召不回"介绍认识"的排序缺陷
        if identity_q and m["category"] == "人际":
            score += 3.0
        # 群聊来源记忆加分：群聊场景全量优先 / 私聊场景最近几条温和优先
        if m.get("source", "private") == "group":
            if group_first:
                score += config.GROUP_MEMORY_BONUS
            elif m["id"] in recent_group_ids:
                score += config.GROUP_RECENT_BONUS
        if score >= 1.0:
            scored.append((score, m))
    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    hits = [m for _, m in scored[:top_k]]
    # 同组更新历史带出：命中条的同组（category+theme）里比它新的也附上
    hit_ids = {m["id"] for m in hits}
    extras = []
    for h in hits:
        for m in mems:
            if (len(extras) >= 3):
                break
            if (m["id"] not in hit_ids and m["id"] > h["id"]
                    and m["category"] == h["category"]
                    and m.get("theme", "") == h.get("theme", "")):
                extras.append(m)
                hit_ids.add(m["id"])
    out = hits + extras
    for m in out:
        db.touch_memory(m["id"])
    return _attach_long(out, expand_long)


# ---------- 记忆检索工具（function calling / MCP 阶段） ----------
# 让 AI 在对话里自己决定何时查记忆、查什么，而不是程序预先塞给它（程序自动召回
# 仍保留做第一层兜底）。两个工具对应 A/B 两级库：
#   search_memories → 查 A 库摘要（短内容直接给，长内容给摘要 + 长文标记）
#   recall_detail   → 深挖细节，展开 B 库全文

# 工具文案：与工具 schema 强耦合的固定描述/返回句/渲染标记。
# 这些"未来不会扩充"，按约定不归语料，就近维护在本模块（不塞 corpus）。
# 拟人化约定（2026-10-08 用户定）：工具不是外部 API，是 TA 自己的"身体组分"——
# 记忆检索 = TA 的大脑在回忆，所以描述用"我可以回想"的能力口吻、返回用
# "想起来了/回忆不起来了"的体感口吻，不出现"搜索/检索/数据库/工具"这类词。
_TOOL_SEARCH_DESC = (
    "这是你的记忆，你可以随时回想。想不起某件事、需要回忆过去聊过的内容时，"
    "就回想一下。别每句话都回想，真的需要才回想。"
    "如果对方说的是群里/群聊里发生的事，用 source=group 专门回想群里的记忆。")
_TOOL_DETAIL_DESC = (
    "把模糊的印象想成完整的细节。当上面的回想结果里某条标了【还有长文】，"
    "且你想把整件事想起来时，用它展开。"
    "同样：对方说的是群里的事就传 source=group。")
_TOOL_NO_QUERY = "（一下子不知道该从哪想起）"
_TOOL_NO_HIT = "（回忆不起来了）"
_TOOL_HIT_PREFIX = "想起来了："
_TOOL_DETAIL_NO_HIT = "（细节回忆不起来了）"
_TOOL_DETAIL_PREFIX = "想起来的具体内容："
_TOOL_UNKNOWN = "（不知道这是什么能力）"
_TOOL_GROUP_TAG = "（群聊）"
_TOOL_LONG_MARK = "【还有长文，可以再回想一下】"
_TOOL_FULL_PREFIX = "（想起来的完整内容）"

MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_memories",
            "description": _TOOL_SEARCH_DESC,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "你想回想的事，用你自己的话说出来。可以多想几个相关的词一起回想：比如想'几点睡'，就一起想'睡觉时间 作息 熬夜 凌晨'。",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["private", "group", "all"],
                        "description": "这些事发生在哪：group=在群里发生的，private=单独聊的，all=都回想一下。对方提到'群里/群聊/那天在群里'就用 group。",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_detail",
            "description": _TOOL_DETAIL_DESC,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "想展开细节的那件事（用你自己的话说，可多想几个相关词）。",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["private", "group", "all"],
                        "description": "这些事发生在哪：group=在群里发生的，private=单独聊的，all=都回想一下。",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


def _render_tool_memories(mems: list[dict]) -> str:
    """把召回结果渲染成给 AI 看的文本（工具返回内容）。与 context._memory_line
    语义一致，但这里自包含，避免 memory↔context 循环依赖。"""
    lines = []
    for m in mems:
        tag = _TOOL_GROUP_TAG if m.get("source") == "group" else ""
        anchor = f"（{m['time_label']}）" if m.get("time_label") else ""
        line = f"- {tag}{anchor}{m['content']}"
        if m.get("long_id"):
            line += _TOOL_LONG_MARK
        lines.append(line)
        if m.get("long_content"):
            lines.append(f"  {_TOOL_FULL_PREFIX}{m['long_content']}")
    return "\n".join(lines)


def run_memory_tool(name: str, args: dict) -> str:
    """执行记忆检索工具，返回渲染好的文本结果（供 AI 阅读后继续回复）。"""
    query = str(args.get("query", "")).strip()
    source = str(args.get("source", "")).strip() or None
    if source not in ("group", "private"):
        source = None  # 只认 group/private，其余（含 all/空/乱填）一律不过滤
    if not query:
        return _TOOL_NO_QUERY
    if name == "search_memories":
        hits = recall(query, top_k=5, group_first=False, source=source)
        if not hits:
            return _TOOL_NO_HIT
        return _TOOL_HIT_PREFIX + "\n" + _render_tool_memories(hits)
    if name == "recall_detail":
        hits = recall(query, top_k=3, group_first=False, expand_long=True, source=source)
        if not hits:
            return _TOOL_DETAIL_NO_HIT
        return _TOOL_DETAIL_PREFIX + "\n" + _render_tool_memories(hits)
    return _TOOL_UNKNOWN


def random_memory(category: str | None = None) -> dict | None:
    """给主动话题供弹药：随机抽一条（优先最近没被召回过的）。"""
    mems = [m for m in db.all_memories(limit=200)
            if m.get("status", "ok") == "ok"]
    if category:
        mems = [m for m in mems if m["category"] == category]
    if not mems:
        return None
    mems.sort(key=lambda m: (m["recall_count"], m["recalled_at"]))
    import random as _r
    pick = _r.choice(mems[:max(3, len(mems) // 3)])
    db.touch_memory(pick["id"])
    return pick
