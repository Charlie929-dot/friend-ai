"""用户书库（S4）：TA 读你导入的真实电子书（txt / epub）。

**分类不靠建子目录**（用户 2026-10-11 拍板：建目录太麻烦）。
书一律平铺在 `books/` 根目录，分类由后端一张映射表管：`books/categories.json`

    {"version": 1, "books": {"三体.epub": {"category": "精品小说", "tags": ["科幻"],
                                           "by": "auto"}}}

分类取值优先级（谁说了算）：
    1. manual —— 网页端/命令手动选过（`by=manual`，永远不被自动重分类覆盖）
    2. dir    —— 子目录名（兼容旧摆法：books/科幻/三体.txt → 精品小说 + 标签 科幻）
    3. auto   —— 关键词自动分类（书名 ×10 / 作者 ×6 / 正文抽样每命中一词 ×1，
                 累计 ≥6 才认；词表在 `corpus.GENRES`，22 个一级分类）
    4. 未分类 —— 都没信号，落兜底类，随时可以手动归位

- 格式：txt（自动试 utf-8/gb18030）、epub（标准库 zipfile 解包 + 剥 HTML）。
- 进度：kv.library_progress，段级推进，一轮读完再从头。
- 解析结果按 (路径, mtime, 大小) 缓存——书架大了也不会每次全量解 epub。
- 只读自己的书，不联网、不传播，纯本地解析，合规。

`next_passage()` 返回与 `reading.next_article()` 同构的
{genre, category, tag, title, source, quote, text}——work 层优先读这里、没书才回退内置书摘。
选书顺序按人格两级偏好：一级分类命中 > 细标签命中 > 未分类 > 其他。
"""
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import config
import corpus
import db

_PROG_KEY = "library_progress"
_MAP_FILE = "categories.json"      # 分类映射表（放 books/ 根目录，跟着书走）
_MAP_VERSION = 1
_FORMATS = (".txt", ".epub")
_MAX_UPLOAD = 200 * 1024 * 1024    # 单本上传上限 200MB（epub 通常 1~20MB）
_SAMPLE_PASSAGES = 40              # 自动分类抽样前 N 段
_SAMPLE_CHARS = 6000               # 抽样字符上限

# 解析缓存：{str(path): (mtime, size, parsed)}——书架越大省得越明显
_CACHE: dict[str, tuple[float, int, dict]] = {}


# ---------- 分类映射表 ----------

def _map_path() -> Path:
    return config.BOOKS_DIR / _MAP_FILE


def _load_map() -> dict:
    """读分类映射表。文件不存在/损坏都返回空表（不停摆）。"""
    try:
        raw = _map_path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"version": _MAP_VERSION, "books": {}}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"version": _MAP_VERSION, "books": {}}
    if not isinstance(data, dict) or not isinstance(data.get("books"), dict):
        return {"version": _MAP_VERSION, "books": {}}
    return data


def _save_map(data: dict) -> None:
    data["version"] = _MAP_VERSION
    try:
        config.BOOKS_DIR.mkdir(parents=True, exist_ok=True)
        _map_path().write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except OSError:
        pass


def categories() -> list[dict]:
    """22 个一级分类 + 未分类，带各自藏书数（网页端下拉/分组用）。

    tags 是该类可用的细标签预设（corpus 的预设 ∪ 用户手打过的新标签）。
    """
    data = _load_map()
    counts: dict[str, int] = {}
    for entry in data.get("books", {}).values():
        if isinstance(entry, dict):
            c = entry.get("category") or corpus.UNCATEGORIZED
            counts[c] = counts.get(c, 0) + 1
    out = []
    for g in corpus.GENRES:
        tags = list(g["tags"])
        for entry in data.get("books", {}).values():
            if isinstance(entry, dict) and entry.get("category") == g["name"]:
                for t in entry.get("tags", []):
                    if t and t not in tags:
                        tags.append(t)
        out.append({"name": g["name"], "tags": tags, "count": counts.get(g["name"], 0)})
    out.append({"name": corpus.UNCATEGORIZED, "tags": [],
                "count": counts.get(corpus.UNCATEGORIZED, 0)})
    return out


def genre_names() -> list[str]:
    """一级分类名（含未分类兜底）。"""
    return [g["name"] for g in corpus.GENRES] + [corpus.UNCATEGORIZED]


# ---------- 自动分类 ----------

def _norm(s: str) -> str:
    """归一化：转小写、非字母数字/中文的字符统一成空格（连字符书名也能匹配上）。"""
    s = (s or "").lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", s)


def classify(title: str = "", author: str = "", sample: str = "",
             filename: str = "", hint: str = "") -> str:
    """关键词自动分类，累计 ≥6 才认定，否则落「未分类」——宁可让你手动点一下也别乱贴标签。

    打分（词表在 `corpus.GENRES`）：
    - 书名 / 文件名 / epub 的 dc:subject 命中一词 ×6，最多算 4 词（封顶 24）
    - 作者命中 ×6（名家姓名在词表里，认出作者就够定性）
    - 正文抽样每命中一词 ×1，最多算 6 词
    书名权重压过正文是刻意的：讲量子的科幻小说该是「精品小说」不是「科学技术」。
    同分按 GENRES 顺序取靠前的类（小说优先于科普）。
    """
    t = _norm(f"{title} {filename} {hint}")
    a = _norm(author)
    s = _norm(sample)[:_SAMPLE_CHARS]
    best, best_score = "", 0
    for g in corpus.GENRES:
        hit_t = hit_a = n_body = 0
        for kw in g["keywords"]:
            k = kw.lower()
            if k.isascii() and len(k) < 3:      # 太短的英文词（art/war）容易误伤
                continue
            if k in t:
                hit_t += 1
            if k in a:
                hit_a += 1
            if k in s:
                n_body += 1
        score = 6 * min(hit_t, 4) + (6 if hit_a else 0) + min(n_body, 6)
        if score > best_score:
            best, best_score = g["name"], score
    return best if best_score >= 6 else corpus.UNCATEGORIZED


def _dir_hint(rel: Path) -> tuple[str, list[str], str]:
    """旧摆法兼容：子目录名 → (分类, 标签, 来源)。不能识别就返回空分类。"""
    if len(rel.parts) < 2:
        return "", [], ""
    d = rel.parts[0]
    for g in corpus.GENRES:
        if g["name"] == d:
            return d, [], "dir"
        if d in g["tags"]:                      # 子目录名是细标签（如 books/科幻/）
            return g["name"], [d], "dir"
    if d in corpus.PREF_LEGACY_MAP:             # 旧偏好名（散文/热血…）
        cat = corpus.PREF_LEGACY_MAP[d]
        return cat, [d] if d in _tags_of(cat) else [], "dir"
    return "", [], ""


def _tags_of(category: str) -> list[str]:
    for g in corpus.GENRES:
        if g["name"] == category:
            return g["tags"]
    return []


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
        # ⚠️ 一律按"标签本地名"找元素，不要拼命名空间：
        # 掌上书苑(epubBuilder) 生成的一批书把 <package xmlns=...> 拼成了 mlns=...，
        # 整份 OPF 因此没有命名空间，按 {opf}item 找会一个都找不到 → 正文 0 段、
        # 整本书在书架里凭空消失（实测 v0.79：《再穷也要去旅行》《圣经》都中招）。
        def _find_all(local: str):
            return [el for el in root.iter() if _local(el.tag) == local]

        title_el = next(iter(_find_all("title")), None)
        creator_el = next(iter(_find_all("creator")), None)
        title = (title_el.text or "").strip() if title_el is not None else ""
        author = (creator_el.text or "").strip() if creator_el is not None else ""
        # dc:subject 是出版方自己标的题材（"科幻小说"/"Fiction"），比猜书名准得多
        # ——但国内盗版转换本里塞站名/URL/书名的极多，先洗一遍（v0.79）
        subjects = _clean_subjects([(el.text or "").strip()
                                    for el in _find_all("subject")], title)
        manifest = {it.get("id"): it.get("href")
                    for it in _find_all("item") if it.get("id")}
        spine = [it.get("idref") for it in _find_all("itemref") if it.get("idref")]
        # 封面（v0.78）：两种标准写法都认——① OPF <meta name="cover" content="封面itemId">
        # ② manifest 里 <item properties="cover-image">。找到就记下 zip 内路径，书架显示用。
        cover_href = ""
        meta_cover = ""
        for el in _find_all("meta"):
            if (el.get("name") or "").lower() == "cover":
                meta_cover = el.get("content") or ""
        if meta_cover and meta_cover in manifest:
            cover_href = manifest[meta_cover]
        if not cover_href:
            for it in _find_all("item"):
                if "cover-image" in (it.get("properties") or ""):
                    cover_href = it.get("href") or ""
                    break
        base = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""
        cover_path = ""
        if cover_href:
            cover_path = f"{base}/{cover_href}" if base and "/" not in cover_href \
                else cover_href
            if cover_path not in z.namelist():     # href 可能是相对 opf 的短路径
                alt = f"{base}/{cover_href}"
                cover_path = alt if alt in z.namelist() else ""
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
        return {"title": title or path.stem, "author": author, "passages": passages,
                "subject": " ".join(subjects), "cover": cover_path}
    except (ET.ParseError, KeyError, OSError):
        return None


_JUNK_RE = [re.compile(p, re.I) for p in corpus.SUBJECT_JUNK]


def _local(tag: str) -> str:
    """XML 标签的本地名（去掉 {命名空间}）。OPF 有没有命名空间都得能解析。"""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _clean_subjects(raw: list[str], title: str = "") -> list[str]:
    """洗一遍 epub 自带的 dc:subject，只留真像题材的。

    脏值三类（实测这批书全都撞上）：源站域名（ibook.178.com）、URL、
    拿书名本身当题材（《活着》的 subject 就是"活着"）。留着它们会让分类
    凭空拿到 6 分，把书带偏——比如《时间简史》被 "lcsh: Novel" 带成小说。
    """
    norm_title = _norm(title)
    out = []
    for s in raw or []:
        s = (s or "").strip()
        if not s or len(s) > 40:
            continue
        if any(r.search(s) for r in _JUNK_RE):
            continue
        if norm_title and _norm(s) == norm_title:
            continue
        out.append(s)
    return out


def _parsed(path: Path) -> dict | None:
    """带缓存的解析（同一文件且 mtime/size 没变就直接复用上次结果）。"""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    hit = _CACHE.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    parsed = _parse_txt(path) if path.suffix.lower() == ".txt" else _parse_epub(path)
    if parsed and parsed.get("passages"):
        _CACHE[key] = (st.st_mtime, st.st_size, parsed)
    return parsed


# ---------- 扫描 ----------

def _entry_for(path: Path, rel: Path, data: dict, parsed: dict) -> dict:
    """给一本书定分类：manual > dir > auto > 未分类；没记过的自动分类后写回映射表。"""
    book_id = str(rel)
    entry = data["books"].get(book_id)
    category = tags = by = ""
    if isinstance(entry, dict) and entry.get("category"):
        category = entry["category"]
        tags = list(entry.get("tags") or [])
        by = entry.get("by") or "manual"
    else:
        d_cat, d_tags, d_by = _dir_hint(rel)
        if d_cat:
            category, tags, by = d_cat, d_tags, d_by
        else:
            sample = " ".join(parsed["passages"][:_SAMPLE_PASSAGES])
            category = classify(parsed["title"], parsed["author"], sample,
                                filename=path.stem, hint=parsed.get("subject", ""))
            tags, by = [], "auto"
        if category not in genre_names():
            category = corpus.UNCATEGORIZED
        data["books"][book_id] = {"category": category, "tags": tags, "by": by}
        _save_map(data)
    return {"category": category, "tags": tags, "by": by}


def _iter_files():
    """遍历 books/ 下所有可解析的书（README 说明和映射表本身不算书）。

    扫描和重分类都走这里——两处各写一份过滤条件，迟早会漏掉一边（实测 #31：
    reindex 少过滤 README，把说明文件也当书建了条目）。
    """
    if not config.BOOKS_DIR.exists():
        return
    for path in sorted(config.BOOKS_DIR.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _FORMATS:
            continue
        if path.name.lower().startswith("readme") or path.name == _MAP_FILE:
            continue
        yield path


def _scan() -> list[dict]:
    """扫描 books/ 目录，解析所有 txt/epub，并按映射表补齐分类。"""
    data = _load_map()
    books = []
    for path in _iter_files():
        rel = path.relative_to(config.BOOKS_DIR)
        parsed = _parsed(path)
        if not parsed or not parsed.get("passages"):
            continue
        got = _entry_for(path, rel, data, parsed)
        books.append({"id": str(rel), "title": parsed["title"], "author": parsed["author"],
                      "category": got["category"], "tags": got["tags"], "by": got["by"],
                      "genre": got["category"],          # 兼容旧字段（work/interests 用）
                      "cover": parsed.get("cover", ""),
                      "passages": parsed["passages"]})
    return books


def books() -> list[dict]:
    """书架摘要（id/title/author/category/tags/by/count/has_cover），供 /books 与网页端展示。"""
    return [{"id": b["id"], "title": b["title"], "author": b["author"],
             "category": b["category"], "tags": b["tags"], "by": b["by"],
             "has_cover": bool(b.get("cover")),
             "count": len(b["passages"])} for b in _scan()]


def get(book_id: str) -> dict | None:
    """按 id（相对 books/ 的路径）取一本书的摘要。"""
    for b in books():
        if b["id"] == book_id:
            return b
    return None


def set_category(book_id: str, category: str, tags: list[str] | None = None) -> dict | None:
    """手动给一本书定分类（by=manual，之后自动重分类不会覆盖它）。

    tags 为 None 表示保留原标签；传 [] 表示清空。返回更新后的书摘要。
    """
    if category not in genre_names():
        return None
    data = _load_map()
    entry = data["books"].get(book_id)
    if not isinstance(entry, dict):
        entry = {}
    entry["category"] = category
    if tags is not None:
        entry["tags"] = [t for t in tags if t]
    entry["by"] = "manual"
    data["books"][book_id] = entry
    _save_map(data)
    return get(book_id)


def reindex() -> int:
    """重跑自动分类：只动 by=auto / by=dir 的书，手动指定过的一律不动。

    返回被改动分类的书数量（词表补词后跑一下，新词就生效了）。
    """
    data = _load_map()
    changed = 0
    for path in _iter_files():
        rel = path.relative_to(config.BOOKS_DIR)
        book_id = str(rel)
        entry = data["books"].get(book_id)
        if isinstance(entry, dict) and entry.get("by") == "manual":
            continue
        parsed = _parsed(path)
        if not parsed or not parsed.get("passages"):
            continue
        sample = " ".join(parsed["passages"][:_SAMPLE_PASSAGES])
        category = classify(parsed["title"], parsed["author"], sample,
                            filename=path.stem, hint=parsed.get("subject", ""))
        if category not in genre_names():
            category = corpus.UNCATEGORIZED
        old = entry.get("category") if isinstance(entry, dict) else None
        if old != category:
            changed += 1
        data["books"][book_id] = {"category": category, "tags": [], "by": "auto"}
    _save_map(data)
    return changed


def delete_book(book_id: str) -> bool:
    """从书架删掉一本书（文件 + 映射表条目 + 阅读进度）。"""
    path = config.BOOKS_DIR / book_id
    try:
        if not path.is_file() or config.BOOKS_DIR not in path.parents:
            return False
        path.unlink()
    except OSError:
        return False
    data = _load_map()
    data["books"].pop(book_id, None)
    _save_map(data)
    prog = _load_progress()
    if prog.get("book_id") == book_id:
        db.set_kv(_PROG_KEY, json.dumps({}, ensure_ascii=False))
    return True


# ---------- 上传 ----------

def _safe_filename(name: str) -> str:
    """上传文件名消毒：去掉路径穿越与非法字符，只放行 txt/epub。"""
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "", name).strip(" .")
    if os.path.splitext(name)[1].lower() not in _FORMATS:
        return ""
    return name[:120]


def save_upload(filename: str, payload: bytes) -> dict | None:
    """把上传的书存进 books/ 根目录（**不建子目录**），解析并自动分类。

    同名自动加 (2)(3) 后缀，不覆盖已有文件。返回该书摘要；格式不支持返回 None。
    """
    safe = _safe_filename(filename)
    if not safe or not payload or len(payload) > _MAX_UPLOAD:
        return None
    config.BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.BOOKS_DIR / safe
    stem, ext = os.path.splitext(safe)
    n = 1
    while dest.exists():
        n += 1
        dest = config.BOOKS_DIR / f"{stem}({n}){ext}"
    try:
        dest.write_bytes(payload)
    except OSError:
        return None
    parsed = _parsed(dest)
    if not parsed or not parsed.get("passages"):
        return None
    rel = dest.relative_to(config.BOOKS_DIR)
    data = _load_map()
    sample = " ".join(parsed["passages"][:_SAMPLE_PASSAGES])
    category = classify(parsed["title"], parsed["author"], sample,
                        filename=dest.stem, hint=parsed.get("subject", ""))
    if category not in genre_names():
        category = corpus.UNCATEGORIZED
    data["books"][str(rel)] = {"category": category, "tags": [], "by": "auto"}
    _save_map(data)
    return {"id": str(rel), "title": parsed["title"], "author": parsed["author"],
            "category": category, "tags": [], "by": "auto",
            "count": len(parsed["passages"])}


def cover_bytes(book_id: str) -> tuple[bytes, str] | None:
    """取一本书的封面图（原图字节 + 媒体类型）。没封面返回 None。

    只在 epub 里找（txt 没有图）；从 zip 直接抽原图不转码——书架缩略图交给浏览器缩。
    路径从 ID 重新解一次（防客户端传任意 zip 内路径进来）。
    """
    path = config.BOOKS_DIR / book_id
    if path.suffix.lower() != ".epub" or not path.is_file():
        return None
    try:
        parsed = _parsed(path)
        if not parsed or not parsed.get("cover"):
            return None
        with zipfile.ZipFile(path) as z:
            data = z.read(parsed["cover"])
    except (zipfile.BadZipFile, KeyError, OSError):
        return None
    ext = parsed["cover"].rsplit(".", 1)[-1].lower() if "." in parsed["cover"] else "jpeg"
    ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
             "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml"}.get(
                 ext, "image/jpeg")
    return data, ctype


# ---------- 阅读进度 ----------

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
    """从段落里摘一句当"金句"：中英文句末标点都算，取第一个 8~40 字的完整句。

    英文句末标点（字母后的 .!? 后跟空白）也切句，保留原文标点；小数点 "3.14"
    不会误切（前面是数字不是字母）。
    """
    sentences = re.split(r"(?<=[。！？!?…])|(?<=[A-Za-z][.!?])\s+", passage)
    for s in sentences:
        s = s.strip()
        if 8 <= len(s) <= 40:
            return s
    return passage[:40].rstrip()


def _pref_rank(book: dict, pref: str, tags: list[str]) -> int:
    """这本书在 TA 口味里的排位：0 一级分类命中 → 1 细标签命中 → 2 未分类 → 3 其他。"""
    if pref and book.get("category") == pref:
        return 0
    if tags and set(book.get("tags") or []) & set(tags):
        return 1
    if book.get("category") == corpus.UNCATEGORIZED or not book.get("category"):
        return 2
    return 3


def next_passage(n: int = 1) -> dict | None:
    """取 TA 下一段（或下 n 段）要读的：按人格两级偏好选书、段级顺序推进、一轮读完再从头。

    一个工作单元读 n 段（n 可配，如 50）：text = 这 n 段合并的内容（截断），
    quote = 从中摘的金句，进度推进 n 段。返回 {genre, category, tag, title, source,
    quote, text}（与 reading.next_article 同构）；书架为空返回 None（work 层回退内置书摘）。
    """
    books_full = _scan()
    if not books_full:
        return None
    n = max(1, n)
    import persona
    params = persona.behavior_params()
    pref = params.get("reading_pref", "")
    pref_tags = params.get("reading_tags") or []
    # 偏好分类 → 偏好细标签命中 → 未分类 → 其他；同一档内按文件名序（稳定）
    ordered = sorted(books_full,
                     key=lambda b: (_pref_rank(b, pref, pref_tags), b["id"]))
    prog = _load_progress()
    book_id = prog.get("book_id")
    idx = prog.get("idx", 0)
    cur = next((i for i, b in enumerate(ordered) if b["id"] == book_id), 0)

    chunks = []
    title = author = category = tag = ""
    for i in range(n):
        # 当前书读到末尾（或空书）就换下一本
        for _ in range(len(ordered)):
            book = ordered[cur]
            if idx < len(book["passages"]):
                break
            idx = 0
            cur = (cur + 1) % len(ordered)
        passages = ordered[cur]["passages"]
        if not passages:
            break
        chunks.append(passages[idx])
        if i == 0:
            title = ordered[cur]["title"]
            author = ordered[cur]["author"]
            category = ordered[cur]["category"]
            tag = (ordered[cur]["tags"] or [""])[0]
        idx += 1
    if not chunks:
        return None
    _save_progress(ordered[cur]["id"], idx)

    text = " ".join(chunks)[:800]
    return {"genre": "" if category == corpus.UNCATEGORIZED else category,
            "category": category, "tag": tag, "title": title,
            "source": author or "佚名",
            "quote": _pick_quote(text), "text": text}
