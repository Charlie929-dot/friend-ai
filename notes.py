"""读书笔记双库（§4 alpha/beta，v1.0 整体设计）：读书的短概述 / 完整转述。

⚠️ 与记忆 A/B 库**同名不同物**，必须区分：
- 记忆 A/B 库 = 记忆摘要 / 长文对话原文（memory.py / db.memories+long_memories）
- **读书笔记 alpha/beta = 读书的短概述 / 完整转述**（db.reading_notes，kind 字段区分）

| | alpha 库 | beta 库 |
| --- | --- | --- |
| 内容 | 短概述 | 完整转述 |
| 关联 | 挂 long_id → beta 库 | 存章节引用（不存原文） |

注入节奏（拍板）：
- 最近章节按**累计字数**窗口必带（不按章数——章节长度差异太大）；
- 每累计约 READING_SUMMARY_THRESHOLD 字触发一次阶段性大总结（阈值待实测）；
- 全书总结；读完 READING_CROSS_BOOK_COUNT 本跨书梳理（范围严格限当前这本）；
- alpha 库注入 = 最近必带 + 向量相关性补充。

与世界观咬合（§2）：阶段性大总结 / 全书 / 跨书梳理同时是「灵魂成长节点」的反思素材。
"""
import config
import db


def add_pair(book_id: str, book_title: str, chapter: str, alpha_summary: str,
             beta_content: str, refs: str = "", char_count: int = 0) -> tuple[int, int]:
    """章节末结算落一条读书笔记：alpha（短概述）+ beta（完整转述+章节引用）。
    返回 (alpha_id, beta_id)。原文不复制入库，只存章节引用（refs）。"""
    return db.add_reading_note_pair(book_id, book_title, chapter, alpha_summary,
                                    beta_content, refs, char_count)


def recent_alpha(limit: int = 20) -> list[dict]:
    """最近 N 条 alpha（短概述）笔记，新的在前。"""
    return db.reading_notes_by_book(kind="alpha", limit=limit)


def recent_alpha_by_chars(chars: int) -> list[dict]:
    """最近章节按累计字数窗口必带（不按章数）：从最新往回累计，直到超过 chars。
    返回这段窗口的 alpha 笔记（按时间正序，最旧的在前）。"""
    rows = db.reading_notes_by_book(kind="alpha", limit=500)
    if not rows:
        return []
    picked, acc = [], 0
    for r in rows:  # 新的在前 → 往回累计
        picked.append(r)
        acc += int(r.get("char_count") or 0)
        if acc >= chars:
            break
    return list(reversed(picked))  # 正序：最旧的在前


def book_ids_read() -> list[str]:
    """已读完（有笔记）的书 id 列表，按最近读的在前。跨书梳理/全书总结用。"""
    rows = db.reading_notes_by_book(kind="beta", limit=500)
    seen, out = [], []
    for r in rows:
        if r["book_id"] not in seen:
            seen.append(r["book_id"])
            out.append(r["book_id"])
    return out


def total_chars_read() -> int:
    """已读累计字数（beta 笔记 char_count 求和），阶段性大总结触发用。"""
    rows = db.reading_notes_by_book(kind="beta", limit=500)
    return sum(int(r.get("char_count") or 0) for r in rows)


def prompt_hint(chars: int | None = None) -> str:
    """注入【读书笔记】块：最近章节按累计字数窗口必带（alpha 短概述）。

    字符窗口默认取当日字数上限的 2 倍（最近读的比当日任务多一点，够回顾上下文）。
    没有笔记返回空串。"""
    window = chars or (config.READING_DAILY_CHARS * 2)
    notes = recent_alpha_by_chars(window)
    if not notes:
        return ""
    lines = ["【我最近读的书】下面是我最近读到的章节，脑子里留下的短概述"
             "（读得越近的越靠后）："]
    for n in notes:
        lines.append(f"- 《{n['book_title']}》{n['chapter']}：{n['summary']}")
    return "\n".join(lines)
