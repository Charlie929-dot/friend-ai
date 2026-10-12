"""统一向量索引（§5 拍板 #6②，第二批）：把「意识流条目 + beta 库转述 + 对话事件摘要」
统一进一个向量索引，近者赢。

与记忆召回（memory_vectors）的区别：记忆召回只索引 memories 表，这里是**统一事件源**
——意识流、读书笔记 beta、对话摘要都进一个池子。阅读时（视线窗口做 query）和对话时
（用户消息做 query）都触发——TA 不能读书时记性好、聊天时健忘。

定位（§5）：**联想增强而非关键记忆检索**。功能由常驻层兜底，漏召回只是少想起一件事，
优雅降级——向量模型不可用时返回空，不崩。
"""
import numpy as np

import config
import db

_MIN_SIM = config.EMBEDDING_MIN_SIMILARITY


def available() -> bool:
    import embeddings
    return config.EMBEDDING_ENABLED and embeddings.available()


def index_event(source: str, ref_id: int, text: str, ts: str) -> bool:
    """把一条事件写进统一向量索引。source: stream / note_beta / dialog。
    文本空 / 向量不可用 → 跳过（返回 False，优雅降级）。"""
    text = " ".join((text or "").split()).strip()
    if not text or not available():
        return False
    import embeddings
    vec = embeddings.embed([text])
    if vec is None:
        return False
    db.set_event_vector(source, ref_id, text, ts, vec[0].astype(np.float32).tobytes())
    return True


def recall(query: str, top_k: int = 5, exclude_source: str | None = None) -> list[dict]:
    """按 query 从统一向量索引召回 top-K 条（近者赢：相似度打平时时间戳新的优先）。

    返回 [{source, ref_id, text, ts, sim}]。向量不可用返回空列表。"""
    import embeddings
    if not query or not available():
        return []
    qv = embeddings.embed([query])
    if qv is None:
        return []
    qv = qv[0]
    rows = db.event_vectors_all()
    scored = []
    for r in rows:
        if exclude_source and r["source"] == exclude_source:
            continue
        try:
            v = np.frombuffer(r["vec"], dtype=np.float32)
        except (ValueError, TypeError):
            continue
        sim = float(np.dot(qv, v))
        if sim >= _MIN_SIM:
            scored.append((sim, r))
    # 相似度打平 → 近者赢（ts 新的排前）；再按相似度降序
    scored.sort(key=lambda x: (-x[0], x[1]["ts"]), reverse=False)
    # 上面 key 元组正序排不对，改两段：先按 ts 降序稳定排，再按 sim 降序（近者赢在 tie 时生效）
    scored.sort(key=lambda x: x[1]["ts"], reverse=True)   # 先近者
    scored.sort(key=lambda x: -x[0])                        # 再相似度（稳定排序保留 ts 序）
    out = []
    for sim, r in scored[:top_k]:
        out.append({"source": r["source"], "ref_id": r["ref_id"],
                    "text": r["text"], "ts": r["ts"], "sim": round(sim, 4)})
    return out


def render(events: list[dict]) -> str:
    """把召回结果渲染成给 TA 看的联想文本（第一人称体感，不出现检索/索引类词）。"""
    if not events:
        return ""
    lines = []
    for e in events:
        if e["source"] == "stream":
            lines.append(f"- （想起当时）{e['text']}")
        elif e["source"] == "note_beta":
            lines.append(f"- （读到时）{e['text']}")
        else:
            lines.append(f"- （那次聊到）{e['text']}")
    return "读到这里，你脑子里模模糊糊浮起这些：\n" + "\n".join(lines)
