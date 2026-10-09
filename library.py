"""用户书库（S4）：TA 读你导入的真实电子书（txt / epub）。

把书丢进 `books/` 目录（可按下图分类放子目录），TA 就会按自己的人格阅读偏好
选书、一段一段真读，读过的内容产出"摘抄 + 读后感"素材（复用 work._gen_shareable）。

- 分类（genre）：来自书的**第一层子目录名**（`books/科幻/三体.txt` → 科幻）；
  直接放根目录的书 genre 为空（不匹配任何偏好，偏好书读完后再读）。
- 格式：txt（自动试 utf-8/gb18030 编码）、epub（标准库 zipfile 解包 + 剥 HTML）。
- 进度：kv.library_progress，段级推进，一轮读完再从头；只读自己的书，不联网、
  不传播，纯本地解析，合规。

`next_passage()` 返回与 `reading.next_article()` 同构的
{genre, title, source, quote, text}——work 层优先读这里、没书才回退内置书摘。
"""
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import config
import db

_PROG_KEY = "library_progress"
_FORMATS = (".txt", ".epub")


# ---------- 文本解析 ----------

def _split_passages(text: str) -> list[str]:
    """按空行/换行切成段落，过滤过短的。"""
    lines = [l.strip() for l in text.split("\n")]
    out, buf = [], []
    for l in lines:
        if l:
            buf.append(l)
        elif buf:
            out.append("".join(buf))
            buf = []
    if buf:
        out.append("".join(buf))
    return [p for p in out if len(p) >= 10]


def _strip_html(html: str) -> str:
    """剥 HTML 标签取正文（<p>/<br> 换行、去 script/style、解常见实体）。"""
    html = re.sub(r"<br\s*/?>|</p>|</div>|</h\d>", "\n", html, flags=re.I)
    html = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "", html)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(a, b)
    return text


def _parse_txt(path: Path) -> dict | None:
    """读 txt，自动试常见编码。"""
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            text = path.read_text(encoding=enc)
            break
        except (UnicodeDecodeError, OSError):
            continue
    else:
        return None
    passages = _split_passages(text)
    return {"title": path.stem, "author": "", "passages": passages}


def _parse_epub(path: Path) -> dict | None:
    """解 epub：container.xml → OPF（书名/作者/阅读顺序）→ 逐章剥标签取正文。"""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return None
    try:
        container = z.read("META-INF/container.xml").decode("utf-8", "ignore")
        m = re.search(r'full-path="([^"]+)"', container)
        if not m:
            return None
        opf_path = m.group(1)
        opf = z.read(opf_path).decode("utf-8", "ignore")
        root = ET.fromstring(opf)
        OPF = "{http://www.idpf.org/2007/opf}"
        DC = "{http://purl.org/dc/elements/1.1/}"
        title_el = root.find(f".//{DC}title")
        creator_el = root.find(f".//{DC}creator")
        title = (title_el.text or "").strip() if title_el is not None else ""
        author = (creator_el.text or "").strip() if creator_el is not None else ""
        manifest = {it.get("id"): it.get("href")
                    for it in root.iter(f"{OPF}item") if it.get("id")}
        spine = [it.get("idref") for it in root.iter(f"{OPF}itemref") if it.get("idref")]
        base = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""
        passages = []
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            full = f"{base}/{href}" if base else href
            try:
                html = z.read(full).decode("utf-8", "ignore")
            except KeyError:
                continue
            passages.extend(_split_passages(_strip_html(html)))
        return {"title": title or path.stem, "author": author, "passages": passages}
    except (ET.ParseError, KeyError, OSError):
        return None


# ---------- 扫描 ----------

def _scan() -> list[dict]:
    """扫描 books/ 目录，解析所有 txt/epub（第一层子目录名 = genre）。"""
    if not config.BOOKS_DIR.exists():
        return []
    books = []
    for path in sorted(config.BOOKS_DIR.rglob("*")):
        if not path.is_file():
            continue
        if path.name.lower().startswith("readme"):   # 跳过说明文件
            continue
        ext = path.suffix.lower()
        if ext not in _FORMATS:
            continue
        rel = path.relative_to(config.BOOKS_DIR)
        genre = rel.parts[0] if len(rel.parts) > 1 else ""
        parsed = _parse_txt(path) if ext == ".txt" else _parse_epub(path)
        if not parsed or not parsed.get("passages"):
            continue
        books.append({"id": str(rel), "title": parsed["title"], "author": parsed["author"],
                      "genre": genre, "passages": parsed["passages"]})
    return books


def books() -> list[dict]:
    """书架摘要（id/title/author/genre/count），供 /books 展示。"""
    return [{"id": b["id"], "title": b["title"], "author": b["author"],
             "genre": b["genre"], "count": len(b["passages"])} for b in _scan()]


def _load_progress() -> dict:
    raw = db.get_kv(_PROG_KEY)
    try:
        p = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return p if isinstance(p, dict) else {}


def _save_progress(book_id: str, idx: int) -> None:
    db.set_kv(_PROG_KEY, json.dumps({"book_id": book_id, "idx": idx}, ensure_ascii=False))


def progress() -> dict:
    """当前阅读进度 {book_id, idx}。"""
    return _load_progress()


def _pick_quote(passage: str) -> str:
    """从段落里摘一句当"金句"：找第一个 8~40 字的完整句；摘不出就取前 40 字。"""
    for m in re.finditer(r"[^。！？!?…]+[。！？!?…]", passage):
        s = m.group(0).strip()
        if 8 <= len(s) <= 40:
            return s
    return passage[:40]


def next_passage() -> dict | None:
    """取 TA 下一段要读的：按人格阅读偏好选书、段级顺序推进、一轮读完再从头。

    返回 {genre, title, source, quote, text}（与 reading.next_article 同构）；
    书架为空返回 None（work 层回退内置书摘）。
    """
    books_full = _scan()
    if not books_full:
        return None
    import persona
    pref = persona.behavior_params().get("reading_pref", "")
    # 偏好书 → 无分类书 → 其他偏好书；同类内按文件名序（稳定）
    ordered = sorted(
        books_full,
        key=lambda b: (0 if b["genre"] == pref else (1 if not b["genre"] else 2), b["id"]))
    prog = _load_progress()
    book_id = prog.get("book_id")
    idx = prog.get("idx", 0)
    cur = next((i for i, b in enumerate(ordered) if b["id"] == book_id), 0)
    # 当前书读到末尾（或空书）就换下一本
    for _ in range(len(ordered)):
        book = ordered[cur]
        if idx < len(book["passages"]):
            break
        idx = 0
        cur = (cur + 1) % len(ordered)
    book = ordered[cur]
    passages = book["passages"]
    if not passages:
        return None
    passage = passages[idx]
    _save_progress(book["id"], idx + 1)
    return {"genre": book["genre"], "title": book["title"],
            "source": book["author"] or "佚名",
            "quote": _pick_quote(passage), "text": passage}
