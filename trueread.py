"""阅读真读（§3，v1.0 整体设计的主要增量）：把"每单元读 N 段"升级为真读。

现状是"每单元读 N 段 → 产一条摘抄+读后感"（跳读），升级为**真读**：
- **上课作息**（默认 8:30 上班 / 12:00–14:00 午休 / 17:30 下班；上午 2 节 + 下午 2 节；
  每节 = 上半 40min + 节内休 15min + 下半 40min；节间休 20min）→ 共 8 半节 × 40min = 320min。
- **当日任务**：按整数章节圈定（不读半章），受当日字数上限约束，累计到"再加一章就超"就停；
  圈定的章节均分 8 个半节。
- **tps（吞吐速度）** = 当日总字数 ÷ 总上课时间——**推导值、不单独设**。
- **边读边做三件事**：视线窗口召回（向量）、思考即笔记（reasoning_content → 思考 buffer）、
  打动判断（不打动就不摘）。
- **章节末结算**：独立总结调用 = 思考 buffer + 章节内容 → 想法 + 总结 → 读书笔记 alpha/beta。

依赖设计：本模块顶层只 import config/db（与 stream 同款约定），library/llm 等函数内延迟导入，
避免与 work/core 成环。
"""
import json
from datetime import datetime, timedelta

import config
import db

_PLAN_KEY = "trueread_plan"      # 当日章节圈定计划
_CURSOR_KEY = "trueread_cursor"  # 阅读游标（读到哪章、哪半节、思考 buffer）


def _parse_hhmm(s: str) -> int:
    """'HH:MM' → 当日分钟数。"""
    try:
        h, m = (s or "").split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 0


def _today(now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def half_periods(now: datetime | None = None) -> list[dict]:
    """8 个半节的起止时间（上课作息）。返回 [{start, end, label}]，正序。

    排法：上午 SCHED_MORNING_PERIODS 节、下午 SCHED_AFTERNOON_PERIODS 节；
    每节 = 上半 40 + 节内休 15 + 下半 40；节间休 20；午休夹在上午/下午之间。
    """
    now = now or datetime.now()
    day = _today(now)
    work = _parse_hhmm(config.SCHED_WORK_START)
    lunch_s = _parse_hhmm(config.SCHED_LUNCH_START)
    lunch_e = _parse_hhmm(config.SCHED_LUNCH_END)
    half = config.SCHED_HALF_PERIOD_MIN
    intra = config.SCHED_INTRA_BREAK_MIN
    inter = config.SCHED_INTER_BREAK_MIN
    slots: list[dict] = []

    def _add_periods(start_min: int, n_periods: int, tag: str):
        nonlocal slots
        t = start_min
        for p in range(n_periods):
            # 上半
            slots.append({"start": t, "end": t + half, "label": f"{tag}第{p + 1}节上"})
            t += half + intra
            # 下半
            slots.append({"start": t, "end": t + half, "label": f"{tag}第{p + 1}节下"})
            t += half
            if p < n_periods - 1:
                t += inter

    _add_periods(work, config.SCHED_MORNING_PERIODS, "上午")
    # 上午末到午休开始之间留空（自然过渡），下午从午休结束起
    _add_periods(lunch_e, config.SCHED_AFTERNOON_PERIODS, "下午")
    return [{"start": day + timedelta(minutes=s["start"]),
             "end": day + timedelta(minutes=s["end"]), "label": s["label"]}
            for s in slots]


def total_teaching_min() -> int:
    """总上课时间（8 半节 × 半节时长，休息不算）。"""
    n = (config.SCHED_MORNING_PERIODS + config.SCHED_AFTERNOON_PERIODS) * 2
    return n * config.SCHED_HALF_PERIOD_MIN


# ---------- 当日任务：按整数章节圈定 ----------

def _book_order() -> list[dict]:
    """待读的书（按人格两级偏好排序，与 work._settle 的选书一致）。"""
    try:
        import library
        import persona
        books_full = library._scan()
        params = persona.behavior_params()
        pref = params.get("reading_pref", "")
        pref_tags = params.get("reading_tags") or []
        ordered = sorted(books_full,
                         key=lambda b: (library._pref_rank(b, pref, pref_tags), b["id"]))
        return ordered
    except Exception:
        return []


def _next_chapters_from(book: dict, start_ch: int, max_chars: int) -> tuple[list[dict], int]:
    """从某本书的第 start_ch 章起，按整数章节累计，直到"再加一章就超"。
    返回 (圈定的章节列表, 累计字数)。"""
    try:
        import library
        chs = library.chapters(book["id"])
    except Exception:
        chs = []
    picked, acc = [], 0
    for ch in chs[start_ch:]:
        n = int(ch.get("char_count") or 0)
        if picked and acc + n > max_chars:
            break                    # 再加一章就超 → 停（宁少勿超）
        picked.append(ch)
        acc += n
        if acc >= max_chars:
            break
    return picked, acc


def plan_day(now: datetime | None = None, force: bool = False) -> dict:
    """圈定当日阅读任务（幂等，缓存到 kv.trueread_plan）。

    返回 {day, book_id, book_title, chapters: [{title, char_count}], total_chars,
          per_half_chars, tps, done}。书架空返回 done=True（无书可读）。
    """
    now = now or datetime.now()
    day = _today(now).strftime("%Y-%m-%d")
    if not force:
        raw = db.get_kv(_PLAN_KEY)
        if raw:
            try:
                plan = json.loads(raw)
                if plan.get("day") == day:
                    return plan
            except ValueError:
                pass

    plan = {"day": day, "book_id": "", "book_title": "", "chapters": [],
            "total_chars": 0, "per_half_chars": 0, "tps": 0, "done": False}
    books = _book_order()
    if not books:
        plan["done"] = True
        _save_plan(plan)
        return plan

    max_chars = config.READING_DAILY_CHARS
    # 从当前进度（library.progress）接着读：book_id + 段落 idx → 大致章节号
    start_ch = 0
    book = books[0]
    try:
        import library
        prog = library.progress()
        if prog and prog.get("book_id"):
            for b in books:
                if b["id"] == prog["book_id"]:
                    book = b
                    break
            # 段落 idx → 章节号（按累计段落数估，落到哪章从哪章起读）
            chs = library.chapters(book["id"])
            idx = int(prog.get("idx", 0) or 0)
            acc_p = 0
            for i, ch in enumerate(chs):
                acc_p += len(library._split_passages(ch.get("text", "")))
                if acc_p > idx:
                    start_ch = i
                    break
    except Exception:
        pass

    chapters, total = _next_chapters_from(book, start_ch, max_chars)
    # 兜底：一本书首章就超过上限，至少读首章（宁少勿超的上限保护让位给"有东西读"）
    if not chapters:
        try:
            import library
            chs = library.chapters(book["id"])
            if chs:
                chapters = [chs[start_ch] if start_ch < len(chs) else chs[0]]
                total = int(chapters[0].get("char_count") or 0)
        except Exception:
            pass

    plan["book_id"] = book["id"]
    plan["book_title"] = book["title"]
    plan["chapters"] = [{"title": c["title"], "char_count": int(c.get("char_count") or 0)}
                        for c in chapters]
    plan["total_chars"] = total
    teach = total_teaching_min()
    plan["per_half_chars"] = max(1, total // max(1, (config.SCHED_MORNING_PERIODS
                                                    + config.SCHED_AFTERNOON_PERIODS) * 2))
    plan["tps"] = round(total / teach, 2) if teach else 0
    plan["done"] = not chapters
    _save_plan(plan)
    return plan


def _save_plan(plan: dict) -> None:
    db.set_kv(_PLAN_KEY, json.dumps(plan, ensure_ascii=False))


def tps(now: datetime | None = None) -> float:
    """当前 tps（推导值 = 当日字数 ÷ 总上课时间）。"""
    return float(plan_day(now).get("tps") or 0)


# ---------- 阅读游标 + 思考 buffer ----------

def _load_cursor() -> dict:
    raw = db.get_kv(_CURSOR_KEY)
    if not raw:
        return {}
    try:
        c = json.loads(raw)
    except ValueError:
        return {}
    return c if isinstance(c, dict) else {}


def _save_cursor(c: dict) -> None:
    db.set_kv(_CURSOR_KEY, json.dumps(c, ensure_ascii=False))


def current_chapter(now: datetime | None = None) -> dict | None:
    """当前正在读的章节（含正文 text，给思考/结算/视线窗口用）。跨天自动重定位。"""
    plan = plan_day(now)
    if plan.get("done") or not plan.get("book_id"):
        return None
    c = _load_cursor()
    if c.get("day") != plan["day"]:
        c = {"day": plan["day"], "ch_idx": 0, "buffer": []}
        _save_cursor(c)
    try:
        import library
        chs = library.chapters(plan["book_id"])
        idx = int(c.get("ch_idx") or 0)
        if 0 <= idx < len(chs):
            return {"book_id": plan["book_id"], "book_title": plan["book_title"],
                    "title": chs[idx]["title"], "text": chs[idx]["text"],
                    "char_count": int(chs[idx].get("char_count") or 0),
                    "idx": idx, "total": len(chs)}
    except Exception:
        pass
    return None


def buffer(now: datetime | None = None) -> list[str]:
    """当前章节的思考 buffer（reasoning_content 按窗口 append 进来的思考片段）。"""
    plan = plan_day(now)
    c = _load_cursor()
    if c.get("day") != plan["day"]:
        return []
    return list(c.get("buffer") or [])


def append_thought(text: str, now: datetime | None = None) -> int:
    """边读边想：把一段思考（reasoning_content）append 进当前章节的思考 buffer。
    返回 buffer 当前长度。空文本/真读关闭返回 0。"""
    if not config.TRUE_READ_ENABLED:
        return 0
    text = " ".join((text or "").split()).strip()
    if not text:
        return len(buffer(now))
    plan = plan_day(now)
    c = _load_cursor()
    if c.get("day") != plan["day"]:
        c = {"day": plan["day"], "ch_idx": 0, "buffer": []}
    buf = list(c.get("buffer") or [])
    buf.append(text)
    c["buffer"] = buf
    _save_cursor(c)
    return len(buf)


def reset_buffer(now: datetime | None = None) -> None:
    """章节末结算前清空思考 buffer（下一章重新攒）。"""
    plan = plan_day(now)
    c = _load_cursor()
    if c.get("day") != plan["day"]:
        return
    c["buffer"] = []
    _save_cursor(c)


def advance_chapter(now: datetime | None = None) -> int:
    """读完一章：游标推进到下一章、清空 buffer。返回新的章节号（0 起）。"""
    plan = plan_day(now)
    c = _load_cursor()
    if c.get("day") != plan["day"]:
        c = {"day": plan["day"], "ch_idx": 0, "buffer": []}
    c["ch_idx"] = int(c.get("ch_idx") or 0) + 1
    c["buffer"] = []
    _save_cursor(c)
    return int(c["ch_idx"])


def prompt_hint(now: datetime | None = None) -> str:
    """注入【正在读】块（真读）：当前章节标题 + 读到哪 + 当日计划概览。"""
    if not config.TRUE_READ_ENABLED:
        return ""
    plan = plan_day(now)
    if plan.get("done"):
        return ""
    ch = current_chapter(now)
    if not ch:
        return ""
    lines = [
        f"我正在读《{ch['book_title']}》的「{ch['title']}」（今天是第 {ch['idx'] + 1}/{ch['total']} 章）。",
        f"今天圈定 {len(plan['chapters'])} 章、共约 {plan['total_chars']} 字，"
        f"我读得不快，一点点地读（约 {plan['tps']} 字/分）。",
    ]
    return "\n".join(lines)


# ---------- 章节末结算：思考 buffer + 章节 → 想法 + 总结 → 读书笔记 alpha/beta ----------

_SETTLE_PROMPT = (
    "（内心活动，不用回复这句话）你刚读完《{title}》的「{chapter}」。\n"
    "下面是你在读的过程里脑子里冒出来的片段想法（思考过程本身），"
    "以及这章的内容（节选）。\n\n"
    "【读时冒出的想法】\n{thoughts}\n\n"
    "【这章内容节选】\n{excerpt}\n\n"
    "把这些整理成三条：\n"
    "1. 短概述：一两句话，说这章讲了什么（60字以内）。\n"
    "2. 完整转述：用你自己的话把这章复述下来（可以到几百字，别抄原文，"
    "是「读完留下的印象」）。\n"
    "3. 打动：是/否。真的被触动了才写是，没感觉就写否。\n"
    "输出严格三行，每行以「短概述：」「完整转述：」「打动：」开头，不要别的文字。"
)


def _excerpt(chapter: dict, max_chars: int = 2000) -> str:
    text = (chapter or {}).get("text", "")
    return text[:max_chars]


def _parse_settle(raw: str) -> tuple[str, str, bool]:
    """解析章节结算的三行文本（短概述 / 完整转述 / 打动）。"""
    alpha = beta = ""
    moved = False
    for line in (raw or "").splitlines():
        line = line.strip()
        if line.startswith("短概述："):
            alpha = line[len("短概述："):].strip()
        elif line.startswith("完整转述："):
            beta = line[len("完整转述："):].strip()
        elif line.startswith("打动："):
            moved = line[len("打动："):].strip().startswith("是")
    return alpha, beta, moved


def _refs(chapter: dict) -> str:
    """章节引用（不存原文）：书名 + 章节名。追问时按需取原文。"""
    return f"{chapter.get('book_title', '')} / {chapter.get('title', '')}"


def settle_chapter(now: datetime | None = None) -> dict | None:
    """章节末结算：独立 LLM 调用（思考 buffer + 章节内容 → 短概述 + 完整转述 + 打动判断）。

    打动 = 是 → 产出读书笔记 alpha/beta + 一条可分享素材；不打动 → 笔记照写（章节总结
    照写），但**不进素材池 / 待办 / 今日素材**（§3.2 打动判断）。

    返回结算结果 dict（含 note alpha_id / beta_id / moved / shareable_text），
    失败（LLM 不可用 / 无章节）返回 None。真读关闭返回 None。
    """
    if not config.TRUE_READ_ENABLED:
        return None
    ch = current_chapter(now)
    if not ch:
        return None
    import llm
    import persona
    thoughts = "\n".join(f"- {t}" for t in buffer(now)) or "（读得比较平静，没冒出特别的想法）"
    prompt = _SETTLE_PROMPT.format(title=ch["book_title"], chapter=ch["title"],
                                   thoughts=thoughts, excerpt=_excerpt(ch))
    try:
        # 章节总结是"成品"不是"思考的压缩"，允许再想一步 → 开思考；结构化输出有 content:null
        # 风险，所以不强制 JSON，用三行开头的纯文本格式解析（关思考更稳、省 token）。
        raw = llm.chat([
            {"role": "system", "content": persona.core_identity_prompt()},
            {"role": "user", "content": prompt},
        ], temperature=0.7, thinking=False)
    except llm.LLMUnavailable as e:
        print(f"[真读] 章节结算跳过（LLM 不可用）：{e}")
        return None
    if not raw or raw == "":
        return None
    alpha, beta, moved = _parse_settle(raw)
    if not alpha or not beta:
        return None
    import notes
    alpha_id, beta_id = notes.add_pair(
        ch["book_id"], ch["book_title"], ch["title"],
        alpha_summary=alpha, beta_content=beta,
        refs=_refs(ch), char_count=int(ch.get("char_count") or 0))
    # 统一向量索引（§5 第二批）：beta 转述进向量索引，阅读/对话时都能联想召回
    try:
        import vecindex
        from datetime import datetime as _dt
        vecindex.index_event("note_beta", beta_id, beta, _dt.now().isoformat(timespec="seconds"))
    except Exception:
        pass
    result = {"alpha_id": alpha_id, "beta_id": beta_id, "moved": moved}
    if moved:
        # 打动 → 进素材池（C 模块主动分享当内容源），不额外调 LLM（转述已经是素材）
        text = f"读到《{ch['book_title']}》的「{ch['title']}」，有点被触动：{alpha}"
        result["shareable_text"] = text
    advance_chapter(now)
    reset_buffer(now)
    return result

