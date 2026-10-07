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
import db
import llm

EXTRACT_PROMPT = """你在整理{WHO}和小徐的聊天记录，从最近的对话片段里提取值得长期记住的事。
对话里"我"是{WHO}，"用户"是小徐。标注"（这条用户还没回应）"的
"我"的消息用户还没看到——不能记成用户已回应/已答应。

输出：一个 JSON 数组，不要输出任何其他文字。每项格式：
{
  "content": "一句话记忆（<50字，一条只说一件事）",
  "category": "画像 | 偏好 | 计划 | 事件 | 人际 | 情绪",
  "time": "绝对时间标签——格式 YYYY-MM-DD，可带时段（如 2026-10-06 下午）。"
          "参照【现在时间】把相对时间换算成绝对日期：'今天下午'→当天日期+下午，"
          "'明天早上'→次日日期+早上，'下周三'→对应日期。"
          "定位不到具体日期的（最近/以后/改天/有空）一律留空字符串",
  "place": "地点（没有就空字符串）",
  "person": "涉及的人物（没有就空字符串）",
  "theme": "学业 | 技术 | 健康作息 | 饮食 | 娱乐 | 社交情感 | 出行 | 财务 | 其他"
}
如果这条信息属于用户的"个人清单"字段（称呼/生日/城市/学校专业/年级/作息习惯/
兴趣爱好/在忙的事/雷区），额外加一项 "profile": "字段名：值"
（如 "profile": "作息习惯：夜猫子，凌晨才睡"）。只有用户亲口陈述的确定性事实才能进清单；
一次性的计划、情绪、事件不进清单。

【类别说明】画像=TA是谁（称呼/城市/身份）；偏好=喜欢/讨厌；计划=打算做的事；
事件=已经发生的事；人际=TA与别人的关系；情绪=状态与感受。

【提议删除】如果发现某条已知记忆是错的——是"我"自己说的话、用户明确否定过、
或与对话中的新事实直接矛盾——输出一项：{"content": "旧记忆中的关键词", "delete": true}。
删除不会立即生效，会标记为待确认，由用户最终决定。不要因"信息过时"就提议删除：
过时的计划/状态是时间线的一部分，新进展新记一条即可。

【值得记的——别漏】
- 用户亲口说的事实、计划、偏好
- **你们的互动里程碑**：深入讨论过的话题、你帮TA弄懂的东西、一起做过的事。
  例："你给小徐详细讲过SVD图像压缩的原理"——共同经历用一句话记下，这是你们的关系。
- **同一话题的新进展、新细节、计划变更**：直接新记一条，不要和旧记忆合并，
  也不要改写旧记忆。系统会把同主题的记忆排成时间线，能看到事情的演变。
  只有和已知记忆内容几乎完全一样的，才不要重复提取。

【质量红线——违反就不要输出】
- 不记"我"自己说的话："我"对用户的评价、起的昵称、调侃、客套都不是用户的事实。
  反例："小徐被我称为技术宅"——那是"我"自己说的俏皮话，一律不记。
  只有用户亲口陈述或明确确认过的事才能记。
- 不记闲聊客套、一次性话题、"我"自己的事
- 一条记忆只说一件事，别囫囵吞枣把多个事实塞进一句
- 兜底（程序还会再过滤一道，漏网=失败）：元信息、不确定推测、
  把用户还没回应的提议记成"小徐答应…"

没有合格的新信息就输出：[]"""


def _extract_prompt() -> str:
    """提取器 system prompt：注入当前朋友的名字，模型才知道"我"是谁。

    不注名字时模型只能靠猜，会写出"小徐对小徐（AI）说…"这种四不像自称
    （2026-10-07 实测）；prompt 里全项目不出现"AI"身份——TA 就是抽到的角色本人。
    """
    try:
        import persona_pool
        card = persona_pool.load()
        name = card["name"] if card else ""
    except Exception:
        name = ""
    return EXTRACT_PROMPT.replace("{WHO}", name or "这位朋友")


# ---------- 确定性后处理：能正则判断的红线不进 prompt（2026-10-07 用户拍板）----------
# 推测词开头 → 丢弃
_SPECULATION = re.compile(r"^(可能|应该|也许|大概|估计|疑似)")
# 元信息（对对话过程的描述而非事实）→ 丢弃
_META = re.compile(r"用户(没有|未)(主动)?(告知|提及|说)|用户不知道|AI没看到|没有看到相关|没有主动告知")
# 高置信自称改写：口误"AI"→"我"（先改写）。prompt 已无 AI 身份提示，
# 这些改写只兜历史习惯/偶发口误；合法话题（AI项目/朋友型AI）不受影响。
_SELF_REWRITE = (("（AI）", ""), ("(AI)", ""),
                 ("AI助手", "我"), ("告诉AI", "告诉我"), ("对AI", "对我"), ("跟AI", "跟我"),
                 ("和AI", "和我"), ("被AI", "被我"), ("向AI", "向我"),
                 ("AI问", "我问"), ("AI说", "我说"), ("AI叫", "我叫"), ("AI称", "我称"),
                 ("AI提醒", "我提醒"), ("AI邀请", "我邀请"), ("AI提议", "我提议"), ("AI推荐", "我推荐"),
                 ("AI给", "我给"), ("AI帮", "我帮"), ("AI陪", "我陪"), ("AI夸", "我夸"))
# 承诺类安全网：窗口尾部是"我"的发言（用户未回应）时，"答应/承诺"必是错位归因 → 丢弃
_PROMISE = re.compile(r"(答应|承诺|保证)")


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
    return content


def _format_convo(rows: list[dict]) -> tuple[str, bool]:
    """拼对话文本 + 结构化标注：用户最后一条发言之后的"我"的消息标记为未回应。

    返回 (convo, assistant_tail)。承诺类错位归因（反馈记录 #4）靠上下文
    结构从源头防——提取器直接"看见"哪些消息用户还没回应，不靠 prompt 规则。
    """
    last_user = max((i for i, r in enumerate(rows) if r["role"] == "user"), default=-1)
    convo = "\n".join(
        f"{'用户' if r['role'] == 'user' else ('我（这条用户还没回应）' if i > last_user else '我')}"
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

    if len(content) < 4 or len(content) > 200:  # 太短没信息量，太长是模型跑偏
        return False

    # 确定性红线（推测/元信息/自称/承诺时序安全网），能代码判断的不占 prompt
    cleaned = _validate(content, assistant_tail)
    if cleaned is None:
        return False
    content = cleaned

    # 个人清单：属于 profile 字段的确定性事实同步进 TA 的清单（/profile 可见）
    profile_line = str(item.get("profile", "")).strip()
    if profile_line:
        _apply_profile(profile_line)

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

    # 追加式时间线：不去重不合并（同主题的新进展是历史的一部分），
    # 仅跳过原文完全相同的（防相邻轮次把同一段话重复提取）
    for m in db.all_memories(limit=200):
        if m["content"] == content:
            return False
    db.add_memory(content, category, tags, time_label, place, person, theme, source)
    dims = "".join(f" @{x}" for x in (time_label, place, person) if x)
    print(f"[memory] 记住：({category}|{theme or '—'}) {content[:30]}{dims}")
    return True


def _apply_profile(line: str) -> None:
    """把一条"字段：值"写进当前朋友的 profile 清单（kv.profile_text）。

    规则：
    - 必须有"字段：值"结构（否则只是普通记忆，不进清单）
    - 字段已存在时：AI 自己写过的字段才更新值；用户手动加的（不在
      profile_auto_keys 里）不覆盖——以用户为准
    - 自动填写会置 profile_locked=1，让新朋友沿用这份清单而非遗产 profile.md
    """
    m = re.match(r"^([^：:]{1,10})[：:]\s*(.+)$", line.strip())
    if not m:
        return
    key, value = m.group(1).strip(), m.group(2).strip()
    if not value or len(value) > 60:
        return
    auto_keys = set(filter(None, db.get_kv("profile_auto_keys").split(",")))
    lines = [l for l in db.get_kv("profile_text").splitlines() if l.strip()]
    new_line = f"{key}：{value}"
    for i, l in enumerate(lines):
        if re.match(rf"^{re.escape(key)}\s*[：:]", l.strip()):
            if key in auto_keys:        # AI 自己写过的字段：更新值
                lines[i] = new_line
            else:                       # 用户手动加的字段：以用户为准，不覆盖
                return
            break
    else:
        lines.append(new_line)          # 新字段：追加
    db.set_kv("profile_text", "\n".join(lines))
    db.set_kv("profile_locked", "1")
    auto_keys.add(key)
    db.set_kv("profile_auto_keys", ",".join(sorted(auto_keys)))
    print(f"[memory] 清单更新：{new_line}")


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
    提取时把「小徐」映射成「用户」、其余保留朋友名，让提取器分清谁说了什么。
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
                who = "用户" if m.group(1) == "小徐" else m.group(1)
                lines.append(f"{who}：{r['content'][m.end():]}")
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
            {"role": "system", "content": _extract_prompt()},
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


_STOP = set("的了是在我你他她这那有没吗呢吧啊呀哦嘛哈么什怎都很也还就去要来上个下和跟与及或对把被让向从但而因所如果真")


def _keywords(s: str) -> set[str]:
    """有意义的单字（去虚词），补上 bigram 覆盖不到的弱相关。"""
    return {ch for ch in re.sub(r"\s", "", s) if ch not in _STOP and '一' <= ch <= '鿿'}


_REL_DAYS = {"今天": 0, "今晚": 0, "明天": 1, "昨天": -1, "前天": -2, "后天": 2}
_PERIODS = ("凌晨", "早上", "上午", "中午", "下午", "晚上", "深夜")


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


def recall(query: str, top_k: int = 5, group_first: bool = False) -> list[dict]:
    """按与 query 的相关度召回 top-K 记忆。
    记忆总量还小（≤10）时直接全量返回——早期每条都珍贵。
    打分：bigram/关键词重叠 + 时间标签加成（query 的相对时间词换算成日期匹配）。
    命中后同组（category+theme 完全一致）的更新进展一起带出——
    AI 看到的是时间线的最新状态，不是过时记忆。
    group_first=True（群聊场景）：群聊来源的记忆（source='group'）加分优先——
    同处一个群里的往事，比私聊里的事更贴合当下群聊语境。"""
    mems = [m for m in db.all_memories(limit=200)
            if m.get("status", "ok") == "ok"]  # 待确认删除的不参与召回
    if not mems:
        return []
    if len(mems) <= 10:
        if group_first:
            # 记忆少时也把群聊来源的排前面
            mems.sort(key=lambda m: (0 if m.get("source", "private") == "group" else 1,
                                     -m["id"]))
        for m in mems:
            db.touch_memory(m["id"])
        return mems[:top_k * 2]
    q_bi = _bigrams(query)
    q_kw = _keywords(query)
    time_tokens = _query_time_tokens(query)
    scored = []
    for m in mems:
        # 五维标签全部参与匹配（人物/地点/主题往往是检索入口）
        hay = (m["content"] + m["tags"] + m.get("time_label", "")
               + m.get("place", "") + m.get("person", "") + m.get("theme", ""))
        score = len(q_bi & _bigrams(hay)) * 2 + len(q_kw & _keywords(hay)) * 0.5
        # 时间标签衔接："今天下午"换算成日期后命中记忆的绝对时间标签
        score += sum(3.0 for t in time_tokens if t in m.get("time_label", ""))
        # 群聊场景：群聊来源的记忆优先（信息调度层）
        if group_first and m.get("source", "private") == "group":
            score += config.GROUP_MEMORY_BONUS
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
    return out


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


def recent_group_memories(limit: int = 5) -> list[dict]:
    """最近的群聊记忆（source='group'），按存储时间倒序。

    私聊回复时注入：让 TA 记得"我们最近在群里聊过这些"，能接上群聊里的事、
    不会"群聊失忆"（召回靠关键词命中，问得含糊就召不回——这里直接兜底注入）。"""
    mems = [m for m in db.all_memories(limit=200)
            if m.get("status", "ok") == "ok" and m.get("source") == "group"]
    mems.sort(key=lambda m: -m["id"])
    return mems[:limit]
