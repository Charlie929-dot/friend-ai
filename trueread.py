"""阅读真读（v0.89 瘦身版）：读什么、读到哪、读完留下了什么。

时间面（几点开始读、读多久、什么时候歇）**全归 `life.py`**——本模块不再有钟。
v0.89 之前这里还有「上课作息 8 半节」的排期，与 work 的工作单元是两套钟，
两套时间参数互不联动（实测症状 #7）；现在节奏 = 章节边界，钟只有 life 那一台。

三件事：
1. **圈章**：从书级书签起按整数章节圈定（宁少勿超，受当日字数上限约束）。
2. **书签**：`kv.reading_state = {book_id, ch_idx}`——**唯一的阅读进度真相**
   （取代旧 `trueread_cursor` + `reading_progress` 两本账），只被章节结算推进。
3. **结算**：思考 buffer + 章节 → 读书笔记 alpha/beta + 打动判断（打动才进素材池）。

依赖设计：顶层只 import config/db（与 stream 同款约定），library/llm 等函数内延迟导入，
避免与 life/core 成环。
"""
import json

import config
import db

_STATE_KEY = "reading_state"     # 书级书签（v0.89：唯一阅读进度真相）

# 结算产物解析用的行前缀（三行纯文本，比 JSON 稳——DeepSeek 结构化输出有 content:null 风险）
_P_ALPHA = "短概述："
_P_BETA = "完整转述："
_P_MOVED = "打动："


# ---------- 书签 ----------
def bookmark() -> dict:
    """当前书签 {book_id, ch_idx}（书级，跨天不重置）。"""
    raw = db.get_kv(_STATE_KEY)
    if not raw:
        return {"book_id": "", "ch_idx": 0}
    try:
        c = json.loads(raw)
    except ValueError:
        return {"book_id": "", "ch_idx": 0}
    if not isinstance(c, dict):
        return {"book_id": "", "ch_idx": 0}
    return {"book_id": str(c.get("book_id") or ""), "ch_idx": int(c.get("ch_idx") or 0)}


def set_bookmark(book_id: str, ch_idx: int) -> dict:
    """写书签。返回写后的书签。"""
    b = {"book_id": book_id, "ch_idx": max(0, int(ch_idx or 0))}
    db.set_kv(_STATE_KEY, json.dumps(b, ensure_ascii=False))
    return b


def align(book_id: str, ch_idx: int) -> dict:
    """把书签对齐到 (book_id, ch_idx)（与当前不一致时才写）。

    生活引擎（life）按当日计划推进结算，书签是唯一真相——两者失步时以 life 为准对齐
    （比如冷启动跳过了几章）。"""
    b = bookmark()
    if b["book_id"] != book_id or b["ch_idx"] != int(ch_idx or 0):
        return set_bookmark(book_id, ch_idx)
    return b


# ---------- 选书与圈章 ----------
def _book_order() -> list[dict]:
    """待读的书（按人格两级偏好排序）。"""
    try:
        import library
        import persona
        books_full = library._scan()
        params = persona.behavior_params()
        pref = params.get("reading_pref", "")
        pref_tags = params.get("reading_tags") or []
        return sorted(books_full,
                      key=lambda b: (library._pref_rank(b, pref, pref_tags), b["id"]))
    except Exception:
        return []


def pick_book() -> dict | None:
    """今天读哪本：书签指向的那本还在书架上就继续读它，否则按人格偏好取第一本。

    （v0.89：不再从「段落 idx 反推章节号」——书签就是章节号，书级、跨天不重置。）
    """
    books = _book_order()
    if not books:
        return None
    b = bookmark()
    if b["book_id"]:
        for x in books:
            if x["id"] == b["book_id"]:
                return x
    return books[0]


def chapters_of(book_id: str) -> list[dict]:
    """一本书的章节列表（[{title, text, char_count}]，正序）。"""
    try:
        import library
        return library.chapters(book_id)
    except Exception:
        return []


def plan_from(book_id: str, start_ch: int, max_chars: int) -> tuple[list[dict], int]:
    """从 start_ch 章起按整数章节累计，直到「再加一章就超」。

    返回 (圈定的章节 [{title, char_count}], 累计字数)。宁少勿超；首章就超上限时
    至少读首章（「有东西读」优先于上限保护）。
    """
    chs = chapters_of(book_id)
    if not chs:
        return [], 0
    start = max(0, min(int(start_ch or 0), len(chs) - 1))
    picked, acc = [], 0
    for ch in chs[start:]:
        n = int(ch.get("char_count") or 0)
        if picked and acc + n > max_chars:
            break
        picked.append({"title": ch.get("title", ""), "char_count": n})
        acc += n
        if acc >= max_chars:
            break
    if not picked:                      # 首章就超上限 → 至少读首章
        picked = [{"title": chs[start].get("title", ""),
                   "char_count": int(chs[start].get("char_count") or 0)}]
        acc = picked[0]["char_count"]
    return picked, acc


def chapter_at(book_id: str, idx: int) -> dict | None:
    """取第 idx 章（含正文），给思考/结算/注入用。越界返回 None。"""
    chs = chapters_of(book_id)
    i = int(idx or 0)
    if not (0 <= i < len(chs)):
        return None
    return {"book_id": book_id, "book_title": _book_title(book_id),
            "title": chs[i].get("title", ""), "text": chs[i].get("text", ""),
            "char_count": int(chs[i].get("char_count") or 0),
            "idx": i, "total": len(chs)}


def _book_title(book_id: str) -> str:
    try:
        import library
        for b in library.books():
            if b.get("id") == book_id:
                return b.get("title") or book_id
    except Exception:
        pass
    return book_id


def chapter_count(book_id: str) -> int:
    """这本书有几章。"""
    return len(chapters_of(book_id))


# ---------- 思考 buffer ----------
def buffer() -> list[str]:
    """当前章节攒下的思考片段（边读边想：reasoning_content 按窗口 append）。"""
    raw = db.get_kv(_THOUGHT_KEY)
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except ValueError:
        return []
    return [str(x) for x in v] if isinstance(v, list) else []


def append_thought(text: str) -> int:
    """把一段思考 append 进 buffer，返回 buffer 长度。空文本/真读关闭返回 0。"""
    if not config.TRUE_READ_ENABLED:
        return 0
    t = " ".join((text or "").split()).strip()
    if not t:
        return len(buffer())
    buf = buffer()
    buf.append(t)
    db.set_kv(_THOUGHT_KEY, json.dumps(buf[-20:], ensure_ascii=False))
    return len(buf)


def reset_buffer() -> None:
    """章节末结算后清空（下一章重新攒）。"""
    db.set_kv(_THOUGHT_KEY, "")


# ---------- 推进 ----------
def advance() -> dict:
    """读完一章：书签 +1。读到书末 → 翻下一本（意识流记一笔）。返回新书签。

    换书事件同时是 v0.88 R4「全书总结」的触发点（接口留在这）。
    """
    b = bookmark()
    chs = chapters_of(b["book_id"])
    nxt = b["ch_idx"] + 1
    if chs and nxt >= len(chs):
        books = _book_order()
        cur_i = next((i for i, x in enumerate(books) if x["id"] == b["book_id"]), -1)
        nxt_book = books[(cur_i + 1) % len(books)] if books and len(books) > 1 else None
        if nxt_book and nxt_book["id"] != b["book_id"]:
            try:
                import stream
                stream.append("reading",
                              f"《{_book_title(b['book_id'])}》读完了，翻开《{nxt_book['title']}》")
            except Exception:
                pass
            return set_bookmark(nxt_book["id"], 0)
    reset_buffer()
    return set_bookmark(b["book_id"], nxt)


def skip_to(book_id: str, ch_idx: int) -> dict:
    """跳到某章（冷启动：中间几章不补笔记，只把书签推过去）。"""
    reset_buffer()
    return set_bookmark(book_id, ch_idx)


# ---------- 章节末结算 ----------
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
    return (chapter or {}).get("text", "")[:max_chars]


def _parse_settle(raw: str) -> tuple[str, str, bool]:
    """解析三行结算文本（短概述 / 完整转述 / 打动）。"""
    alpha = beta = ""
    moved = False
    for line in (raw or "").splitlines():
        line = line.strip()
        if line.startswith(_P_ALPHA):
            alpha = line[len(_P_ALPHA):].strip()
        elif line.startswith(_P_BETA):
            beta = line[len(_P_BETA):].strip()
        elif line.startswith(_P_MOVED):
            moved = line[len(_P_MOVED):].strip().startswith("是")
    return alpha, beta, moved


def _refs(chapter: dict) -> str:
    """章节引用（不存原文）：书名 / 章节名。追问时按需取原文。"""
    return f"{chapter.get('book_title', '')} / {chapter.get('title', '')}"


def settle_chapter(now=None) -> dict | None:
    """结算书签当前指向的那一章（独立 LLM 调用）。

    思考 buffer + 章节 → 短概述 / 完整转述 / 打动判断 → 读书笔记 alpha/beta
    （+ 统一向量索引）。打动 = 是 → 额外带出 `shareable_text`（由 life 落素材池）。
    成功则书签推进一章。LLM 不可用 / 无章可读返回 None。
    """
    if not config.TRUE_READ_ENABLED:
        return None
    b = bookmark()
    ch = chapter_at(b["book_id"], b["ch_idx"]) if b["book_id"] else None
    if not ch:
        return None
    import llm
    import persona
    thoughts = "\n".join(f"- {t}" for t in buffer()) or "（读得比较平静，没冒出特别的想法）"
    prompt = _SETTLE_PROMPT.format(title=ch["book_title"], chapter=ch["title"],
                                   thoughts=thoughts, excerpt=_excerpt(ch))
    try:
        # 章节总结是"成品"不是"思考的压缩"；结构化 JSON 有 content:null 风险 →
        # 用三行纯文本格式（关思考更稳、省 token）。
        raw = llm.chat([
            {"role": "system", "content": persona.core_identity_prompt()},
            {"role": "user", "content": prompt},
        ], temperature=0.7, thinking=False)
    except llm.LLMUnavailable as e:
        print(f"[真读] 章节结算跳过（LLM 不可用）：{e}")
        return None
    if not raw:
        return None
    alpha, beta, moved = _parse_settle(raw)
    if not alpha or not beta:
        return None
    import notes
    alpha_id, beta_id = notes.add_pair(
        ch["book_id"], ch["book_title"], ch["title"],
        alpha_summary=alpha, beta_content=beta,
        refs=_refs(ch), char_count=int(ch.get("char_count") or 0))
    # 统一向量索引（§5）：beta 转述进索引，阅读/对话时都能联想召回
    try:
        import vecindex
        from datetime import datetime as _dt
        vecindex.index_event("note_beta", beta_id, beta, _dt.now().isoformat(timespec="seconds"))
    except Exception:
        pass
    # 兴趣修炼：书的分类就是一次兴趣信号（确定性最强，不经过词表匹配）
    try:
        import interests
        import library
        for bk in library.books():
            if bk.get("id") == ch["book_id"] and bk.get("category"):
                if interests.record(bk["category"]):
                    print(f"[兴趣] 转正：{bk['category']}")
                break
    except Exception:
        pass
    result = {"alpha_id": alpha_id, "beta_id": beta_id, "moved": moved,
              "chapter": ch["title"], "book_title": ch["book_title"]}
    if moved:
        result["shareable_text"] = (
            f"读到《{ch['book_title']}》的「{ch['title']}」，有点被触动：{alpha}")
    advance()
    return result


def prompt_hint(chars_hint: str = "") -> str:
    """【正在读】块：当前章节标题 + 读到哪（给 system prompt 注入用）。"""
    if not config.TRUE_READ_ENABLED:
        return ""
    b = bookmark()
    ch = chapter_at(b["book_id"], b["ch_idx"]) if b["book_id"] else None
    if not ch:
        return ""
    return (f"我正在读《{ch['book_title']}》的「{ch['title']}」"
            f"（第 {ch['idx'] + 1}/{ch['total']} 章）。")


_THOUGHT_KEY = "trueread_thoughts"
