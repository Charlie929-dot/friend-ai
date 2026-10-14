"""生活引擎（v0.89）：TA 的今天——**唯一的钟、唯一的进度真相**。

取代 `work.py`（已退役）。旧架构的病根（详见 docs/方案-生活引擎重构-v0.89.md §0）：
TA 的「生活」不是一条时间线，而是四本账（`work_state` / `trueread_plan` /
`trueread_cursor` / `reading_progress`）+ 三套钟（SCHED 固定时钟 / 人格锚点 /
offline 起床推算），靠「同步 + 注入」拼成体感——同步就会失步，于是七种症状一起发作。

现在：

- **两本账，零同步**：`day_state`（今日生活，醒来排定、当日只读）+ `reading_state`
  （书级书签，只被章节结算推进，见 `trueread`）。进度 / 经验 / 精力 / phase 全是
  **派生投影，不落库**。
- **唯一的钟**：`life.today()` 一处推算起床 / 睡前 / 会话窗口。醒来、起晚、开工、
  读完一章、收工、读得晚——全部作为**事件**落意识流（再问「为什么 10 点才醒」，
  翻一行 wake 日志就有答案）。
- **事件是源，数字是视图**：数字（进度 / 经验 / 精力）给屏幕（HUD、`/work`），
  事件给舌头（意识流 + prompt **事实句**）。prompt 里不再念账本——
  「今天第 3/6 件、攒了 60 块」换成「我在读《X》的「Y」，今天圈了 4 章，读完 2 章」。
- **章节边界是节奏**：读到下一个章节边界算一段（短章贪心合并到 ≳40 分钟），
  读完一章歇一会；**唯一硬边界是睡前**，没有下班铃。

⚠️ 「经验」是纯叙事资源（= 当日读书进度百分比），**绝不绑真实付费/内购**。
"""
import json
import random
import threading
from datetime import datetime, timedelta

import config
import db
import stream

_DAY_KEY = "day_state"
_SHARE_KEY = "work_shareables"    # 素材池 key 沿用旧名（里面有攒下的素材，不换 key）
_SHARE_KEEP = 30

# 代码常量（不进 config：是推导口径的组成部分，不是可调旋钮）
NOMINAL_SESSION_MIN = 40   # 名义会话时长：贪心合并下限 + 精力消耗率的分母
NOMINAL_REST_MIN = 20      # 名义休息时长：精力恢复率的分母
DAY_BUDGET_MIN = 320       # 名义日阅读预算（速度 = 当日圈定字数 ÷ 这个值）
REST_RATIO = 0.3           # 休息 ≈ 刚读完那段的长度 × 系数
MAX_CATCHUP = 3            # 冷启动最多补结算几章（防 LLM 尖峰）

PHASE_LABELS = {
    "idle": "还没开工",
    "working": "在读书",
    "flow": "刚读进去",
    "break": "歇会儿",
    "off": "今天忙完了",
    "sleep": "睡着",
}

# 注入 prompt 的话（§5.1：**事实句**，不念数字账本）。占位符：{book}/{ch}/{total}/{done}/{cur}
_FACT_TEMPLATES = {
    "idle": "我今天还没开始读自己的书。",
    "working": "我在读《{book}》的「{ch}」——今天圈了 {total} 章，读完 {done} 章，"
               "正在读第 {cur} 章。手头有活，回复可以短一点、慢一点。",
    "flow": "我刚读进去，脑子全在《{book}》这章上，回复会短一些、慢一些。"
            "只说自己的状态，别提对方，别抱怨。",
    "break": "我刚读完一章，正歇会儿（今天圈了 {total} 章，读完 {done} 章）。"
             "这会儿松弛，可以多聊两句。",
    "off": "我今天的事都忙完了，现在是自己的时间。",
    "sleep": "",
}

_settling = False          # 章节结算串行闸门（同时只跑 1 个 LLM 调用）


# ---------- 钟：起床 / 睡前（全项目只此一处推算） ----------
def _anchor(now: datetime) -> tuple[datetime, int]:
    """今日生活日的起床锚点（人格作息）+ 睡觉时刻的小时数。"""
    import persona
    p = persona.behavior_params()
    day, _ = persona._life_day(now)
    return datetime(day.year, day.month, day.day, p["wake_hour"] % 24, 0), p["sleep_hour"]


def _bedtime(anchor: datetime, sleep_hour: int) -> datetime:
    """从起床时刻算睡觉时刻。睡点不晚于起点 = 跨到了第二天（夜猫子 10 点起、2 点睡）。"""
    bed = anchor.replace(hour=sleep_hour % 24, minute=0, second=0, microsecond=0)
    if bed <= anchor:
        bed += timedelta(days=1)
    return bed


def wake_point(now: datetime) -> datetime:
    """实际起床点 = 人格锚点 + 赖床浮动（作息模块不可用时退回锚点）。"""
    try:
        import offline
        return offline.today_wake_at(now)
    except Exception:
        return _anchor(now)[0]


def _life_day_str(now: datetime) -> str:
    import persona
    return persona._life_day(now)[0].strftime("%Y-%m-%d")


def speed() -> float:
    """阅读速度（字/分钟）= 当日圈定字数 ÷ 名义日预算。

    **常数，不从「剩余时间」反推**——起晚 → 时间窗变短 → 读得少或读得晚，
    零特判（这是「不追进度」的结构化落地）。
    """
    return max(1.0, config.READING_DAILY_CHARS / DAY_BUDGET_MIN)


def _jitter(minutes: float) -> int:
    """抖动：不整齐才像人（每次磨蹭/歇息时长都不一样）。"""
    return max(1, int(minutes * random.uniform(0.7, 1.4)))


# ---------- 账：day_state（两本账之一） ----------
def _load() -> dict:
    raw = db.get_kv(_DAY_KEY)
    if not raw:
        return {}
    try:
        s = json.loads(raw)
    except ValueError:
        return {}
    return s if isinstance(s, dict) else {}


def _save(state: dict):
    db.set_kv(_DAY_KEY, json.dumps(state, ensure_ascii=False))


def _blank(day: str) -> dict:
    return {"v": 2, "day": day, "planned": 0, "wake": "", "bedtime": "",
            "sessions": [], "settled_idx": 0, "plan": {}, "overtime": 0}


def today(now: datetime | None = None) -> dict:
    """今日生活（唯一的钟）：醒来那一刻排定、落 kv、当日只读。

    保留「醒来才排、同日幂等、惰性补账」的哲学——排班的起点是**实际起床点**
    （人格锚点 + 赖床浮动），人没醒就不排（实测 #26：人没醒、进度在涨）。
    读到 v1 旧账（units/money）一律忽略重排当日（自用阶段不迁移，代价 = 一天叙事）。
    """
    now = now or datetime.now()
    state = _load()
    day = _life_day_str(now)
    if state.get("v") != 2 or state.get("day") != day:
        state = _blank(day)
        _save(state)
    if not state.get("planned"):
        wake = wake_point(now)
        if now >= wake and db.get_state() != "SLEEPING":
            _plan_day(state, now, wake)
    return state


def _plan_day(state: dict, now: datetime, wake: datetime) -> None:
    """排今天：起床 → 磨蹭 → 一圈会话（章节边界），睡前是唯一硬边界。"""
    anchor, sleep_hour = _anchor(now)
    bed = _bedtime(wake if wake > anchor else anchor, sleep_hour)
    state["wake"] = wake.isoformat(timespec="seconds")
    state["bedtime"] = bed.isoformat(timespec="seconds")
    # 起晚 = 事件（不是旷课）：锚点后移 → 窗口压缩 → 自然少读或读得晚，无特判分支
    if wake - anchor > timedelta(minutes=5):
        try:
            if stream.once("late_wake", now):
                stream.append("late_wake", f"今天起晚了，{wake:%H:%M} 才醒", now)
        except Exception:
            pass
    plan = _reading_plan()
    state["plan"] = plan
    state["sessions"] = _make_sessions(wake, plan, bed) if not plan.get("done") else []
    state["settled_idx"] = 0
    state["overtime"] = 0
    state["planned"] = 1
    _save(state)
    if plan.get("done"):
        try:
            stream.append("work", "书架上没有想读的了", now)
        except Exception:
            pass


def _reading_plan() -> dict:
    """今日读书任务：从书级书签起圈整数章节（宁少勿超）。"""
    import trueread
    book = trueread.pick_book()
    if not book:
        return {"done": True, "book_id": "", "book_title": "", "start_ch": 0,
                "chapters": [], "total_chars": 0}
    bm = trueread.bookmark()
    start_ch = bm["ch_idx"] if bm.get("book_id") == book["id"] else 0
    chapters, total = trueread.plan_from(book["id"], start_ch, config.READING_DAILY_CHARS)
    return {"done": not chapters, "book_id": book["id"], "book_title": book.get("title", ""),
            "start_ch": int(start_ch), "chapters": chapters, "total_chars": int(total)}


def _make_sessions(wake: datetime, plan: dict, bed: datetime) -> list[dict]:
    """排会话：读到下一个章节边界算一段（短章贪心合并到 ≳ 名义时长），
    读完一章歇一会，睡前是唯一硬边界。"""
    sp = speed()
    target = NOMINAL_SESSION_MIN * sp
    queue = list(plan.get("chapters") or [])
    t = wake + timedelta(minutes=_jitter(config.WORK_WARMUP_MIN))
    sessions: list[dict] = []
    idx = int(plan.get("start_ch") or 0)
    while queue and t < bed:
        picked, chars = [], 0
        while queue and (not picked or chars < target):
            c = queue.pop(0)
            picked.append({"idx": idx, "title": c.get("title", ""),
                           "char_count": int(c.get("char_count") or 0)})
            chars += int(c.get("char_count") or 0)
            idx += 1
        dur = chars / sp
        end = t + timedelta(minutes=dur)
        if end > bed:      # 读到睡前为止 → 明日按字数重圈（不追进度）
            sessions.append({"start": t.isoformat(timespec="seconds"),
                             "end": bed.isoformat(timespec="seconds"),
                             "chapters": picked, "partial": True})
            break
        sessions.append({"start": t.isoformat(timespec="seconds"),
                         "end": end.isoformat(timespec="seconds"),
                         "chapters": picked, "partial": False})
        t = end + timedelta(minutes=_rest_minutes(dur, end))
    return sessions


def _rest_minutes(session_min: float, after: datetime) -> int:
    """歇多久：刚读完那段的长度 × 系数 + 抖动；跨过中午的那次变长（午休**涌现**，不是排出来的）。"""
    rest = max(5, int(session_min * REST_RATIO * random.uniform(0.7, 1.4)))
    noon = after.replace(hour=config.NOON_HOUR % 24, minute=0, second=0, microsecond=0)
    if after < noon < after + timedelta(minutes=rest + 30):
        rest = max(rest, config.LUNCH_MIN)
    return rest


# ---------- 派生投影：进度 / 经验 / 精力 / phase ----------
def _session_windows(day: dict) -> list[tuple[datetime, datetime, dict]]:
    out = []
    for s in (day.get("sessions") or []):
        try:
            out.append((datetime.fromisoformat(s["start"]),
                        datetime.fromisoformat(s["end"]), s))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def read_chars(day: dict, now: datetime) -> float:
    """已读字数 = Σ（[排定起点, now] ∩ 各会话窗口）× 速度。纯派生。"""
    sp = speed()
    acc = 0.0
    for start, end, _ in _session_windows(day):
        if now <= start:
            break
        acc += min(now, end).timestamp() - start.timestamp()
    return acc / 60.0 * sp


def progress(day: dict, now: datetime) -> float:
    """当日进度 0–1（已读字数 ÷ 当日圈定总字数）。"""
    total = int(day.get("plan", {}).get("total_chars") or 0)
    if total <= 0:
        return 0.0
    return min(1.0, read_chars(day, now) / total)


def _chapter_pos(day: dict, read: float) -> tuple[int, int, str]:
    """读到第几章（当日列表内的偏移 0 起）+ 总章数 + 章标题（按累计字数定位）。"""
    chs = day.get("plan", {}).get("chapters") or []
    acc = 0
    for i, c in enumerate(chs):
        acc += int(c.get("char_count") or 0)
        if read < acc:
            return i, len(chs), c.get("title", "")
    return max(0, len(chs) - 1), len(chs), (chs[-1].get("title", "") if chs else "")


def energy(day: dict, now: datetime) -> int:
    """精力（派生）：会话中按分钟耗、休息中按分钟回。

    语义换算为每分钟率（v0.89 §3.5）：消耗率 = WORK_ENERGY_COST ÷ 名义会话时长，
    恢复率 = WORK_ENERGY_RECOVER ÷ 名义休息时长。对外仍是整数，消费方零改动。
    """
    cap = float(config.WORK_ENERGY_MAX)
    cost_rate = config.WORK_ENERGY_COST / NOMINAL_SESSION_MIN
    rec_rate = config.WORK_ENERGY_RECOVER / NOMINAL_REST_MIN
    e = cap
    try:
        wake = datetime.fromisoformat(day["wake"])
    except (KeyError, ValueError, TypeError):
        return int(cap)
    prev = wake
    for start, end, _ in _session_windows(day):
        rest = max(0.0, (min(now, start) - prev).total_seconds() / 60)
        e = min(cap, e + rest * rec_rate)
        if now <= start:
            return int(round(e))
        work = max(0.0, (min(now, end) - start).total_seconds() / 60)
        e = max(0.0, e - work * cost_rate)
        prev = end
        if now < end:
            return int(round(e))
    rest = max(0.0, (now - prev).total_seconds() / 60)
    return int(round(min(cap, e + rest * rec_rate)))


def phase(day: dict, now: datetime) -> str:
    """此刻的相位。**phase 键集合不变**（idle/working/flow/break/off/sleep）——
    cost / mood / offline / tone 四个消费方靠这个契约零改动。"""
    if not day.get("planned") or db.get_state() == "SLEEPING":
        return "sleep"
    wins = _session_windows(day)
    if not wins or day.get("plan", {}).get("done"):
        return "off"
    for start, end, _ in wins:
        if now < start:
            return "idle" if start == wins[0][0] else "break"
        if now < end:
            total = (end - start).total_seconds() / 60
            into = (now - start).total_seconds() / 60
            return "flow" if total > 0 and into < total * config.WORK_FLOW_RATIO else "working"
    return "off"


# ---------- 对外：状态快照 ----------
def status(now: datetime | None = None) -> dict:
    """它此刻在忙什么。HUD / prompt / 打断成本 / 网页端都读这个。"""
    now = now or datetime.now()
    empty = {"phase": "off", "label": PHASE_LABELS["off"], "flow": False,
             "energy": config.WORK_ENERGY_MAX, "exp": 0, "progress": 0,
             "chapter": 0, "total_chapters": 0, "book_title": "", "chapter_title": "",
             "overtime": False}
    if not config.WORK_ENABLED:
        return empty
    day = today(now)
    ph = phase(day, now)
    if ph == "sleep":
        return dict(empty, phase="sleep", label=PHASE_LABELS["sleep"],
                    energy=config.WORK_ENERGY_MAX)
    read = read_chars(day, now)
    prog = progress(day, now)
    exp = int(round(prog * 100))
    cur, total, ctitle = _chapter_pos(day, read)
    overt = ph in ("working", "flow") and now.hour >= config.LATE_READ_HOUR
    _settle_due(day, now)
    _note_events(ph, day, now, exp, cur, total, ctitle)
    return {"phase": ph, "label": PHASE_LABELS[ph], "flow": ph == "flow",
            "energy": energy(day, now), "exp": exp, "progress": int(round(prog * 100)),
            "chapter": cur + 1, "total_chapters": total,
            "book_title": day.get("plan", {}).get("book_title", ""),
            "chapter_title": ctitle, "overtime": overt, "settled": int(day.get("settled_idx") or 0)}


def sessions(now: datetime | None = None) -> list[dict]:
    """今日排定的会话窗口（原 work.plan()，测试锚点用）。"""
    return today(now).get("sessions") or []


def hud(now: datetime | None = None) -> str:
    """一行状态条（§5.2：数字常驻可见——看得见才会自发尊重它的边界）。"""
    if not config.WORK_ENABLED:
        return ""
    s = status(now)
    if s["phase"] == "sleep":
        return ""
    tag = "读得晚" if s["overtime"] else s["label"]
    book = f"《{s['book_title']}》" if s["book_title"] else ""
    return f"[{tag} {book} 进度 {s['progress']}% · 精力 {s['energy']} · 经验 {s['exp']}]"


def prompt_hint(now: datetime | None = None) -> str:
    """注入 system prompt 的【手头的事】：**事实句，不念数字**（§5.1）。

    保留自然量词（读完 2 章 / 读到第 3 章），被问「读多少了」答得上来；
    精确数字走 `/work` 与 HUD。反愧疚红线不变：只说自己的状态。"""
    if not config.WORK_ENABLED:
        return ""
    s = status(now)
    tpl = _FACT_TEMPLATES.get(s["phase"], "")
    if not tpl:
        return ""
    return tpl.format(book=s["book_title"], ch=s["chapter_title"],
                      total=s["total_chapters"], done=s["settled"], cur=s["chapter"])


def tick() -> dict:
    """调度器每分钟调一次：把该结算的章节结算掉（惰性补账，电脑睡一觉也能补上）。"""
    return status()


def reset():
    """清掉今日生活状态（换朋友 / 调试用）。"""
    db.set_kv(_DAY_KEY, "")
    db.set_kv(_SHARE_KEY, "")


# ---------- 事件：发生的事才是本体（§5.3） ----------
def _note_events(ph: str, day: dict, now: datetime, exp: int,
                 cur: int, total: int, ctitle: str) -> None:
    """把「身上发生的事」往意识流轴上追加一笔。

    一次性事件走 `stream.once`（今天只记一次），来回切的相位走 `stream.note_phase`
    （相位真变了才记）。"""
    if not config.CONSCIOUS_STREAM_ENABLED or ph == "sleep":
        return
    book = day.get("plan", {}).get("book_title", "")
    if ph in ("working", "flow"):
        if stream.once("work_start", now):
            stream.append("work", "开始读今天圈的书了", now)
        s_idx = _current_session_idx(day, now)
        if stream.once(f"session{s_idx}", now):
            line = f"在读《{book}》" if book else "在读书"
            if ctitle:
                line += f"的「{ctitle}」（今天第 {cur + 1}/{total} 章）"
            stream.append("reading", line, now)
        if now.hour >= config.LATE_READ_HOUR and stream.once("overtime", now):
            stream.append("overtime", "今天读得有点晚了", now)
    elif ph == "break":
        stream.note_phase("break", "读完一章，歇会儿", now)
    elif ph == "off":
        tail = f"，攒了 {exp} 点经验" if exp else ""
        stream.note_phase("off", f"今天的事忙完了{tail}", now)


def _current_session_idx(day: dict, now: datetime) -> int:
    for i, (start, end, _) in enumerate(_session_windows(day)):
        if start <= now < end:
            return i
    return -1


def _finished_chapters(day: dict, now: datetime) -> int:
    """已结束的会话累计包含几章（结算的触发边界 = 章末即会话末）。"""
    n = 0
    for _, end, s in _session_windows(day):
        if end <= now:
            n += len(s.get("chapters") or [])
    return n


def _settle_due(day: dict, now: datetime) -> None:
    """会话结束 = 章末 → 触发章节结算（后台串行，深 ≤ MAX_CATCHUP）。

    冷启动（电脑睡了一觉跨过好几章）：更早的章只推 settled_idx + 意识流记一笔，
    **不补笔记**（防 LLM 尖峰）。
    """
    global _settling
    if not config.TRUE_READ_ENABLED or not config.WORK_SHAREABLE_ENABLED:
        return
    if _settling:
        return
    done = _finished_chapters(day, now)
    settled = int(day.get("settled_idx") or 0)
    if settled >= done:
        return
    if done - settled > MAX_CATCHUP:
        day["settled_idx"] = done - MAX_CATCHUP
        _save(day)
        try:
            stream.append("reading", "中间有几章翻过去了，没来得及细想", now)
        except Exception:
            pass
    _settling = True
    threading.Thread(target=_settle_one, args=(now,), daemon=True).start()


def _settle_one(now: datetime) -> None:
    """后台线程：结算一章（要调 LLM，不阻塞 HUD）。"""
    global _settling
    try:
        import trueread
        day = _load()
        plan = day.get("plan") or {}
        if not plan.get("book_id"):
            return
        abs_idx = int(plan.get("start_ch") or 0) + int(day.get("settled_idx") or 0)
        trueread.align(plan["book_id"], abs_idx)
        result = trueread.settle_chapter(now)
        if not result:
            return
        day["settled_idx"] = int(day.get("settled_idx") or 0) + 1
        _save(day)
        try:
            stream.append("chapter",
                          f"读完《{result.get('book_title', '')}》的「{result.get('chapter', '')}」",
                          now)
        except Exception:
            pass
        if result.get("moved"):
            _note_shareable_text(result.get("shareable_text", ""), now)
    except Exception as e:
        print(f"[生活] 章节结算失败：{e}")
    finally:
        _settling = False


# ---------- 素材池（v0.89：只来自章节结算的打动，泛化素材已退役） ----------
def _shareables() -> list[dict]:
    raw = db.get_kv(_SHARE_KEY)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    return [x for x in items if isinstance(x, dict) and x.get("text")]


def _note_shareable_text(text: str, now: datetime | None = None) -> None:
    """读到有触动的地方 → 进素材池 + 今日素材 + 待办（不额外调 LLM）。"""
    if not text:
        return
    now = now or datetime.now()
    todo_id = 0
    if config.TODOS_ENABLED:
        import todos
        todo_id = todos.add(text, source="reading")
    items = _shareables()
    items.append({"day": now.strftime("%Y-%m-%d"), "seq": 0,
                  "text": text, "used": 0, "todo_id": todo_id})
    db.set_kv(_SHARE_KEY, json.dumps(items[-_SHARE_KEEP:], ensure_ascii=False))
    if config.TODAY_NOTES_ENABLED:
        db.add_today_note("今天读到一句有触动的话", text)


def take_shareable() -> str:
    """取一条还没用过的素材（最早的优先）。"""
    text, _ = take_shareable_item()
    return text


def take_shareable_item() -> tuple[str, int]:
    """取一条还没用过的素材，返回 (text, todo_id)。取到就标记用过。

    §5 拍板 #6③：主动消息生成时携带素材 id，消息发出后按 id 打勾（不再字面指纹）。
    """
    items = _shareables()
    for x in items:
        if not x.get("used"):
            x["used"] = 1
            db.set_kv(_SHARE_KEY, json.dumps(items, ensure_ascii=False))
            return x["text"], int(x.get("todo_id") or 0)
    return "", 0
