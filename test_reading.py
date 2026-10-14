"""书库与真读（书库/分类/epub/偏好两级/真读/笔记注入）（E4 拆分，v0.92）。

运行：`python run_tests.py reading`（或全量 `python run_tests.py`）。
本文件顶部必须 `from _test_common import *`——两段全局机制（临时目录收口 +
关双层生成）在此生效，见约定 #10/#11。
"""
from _test_common import *  # noqa: F401,F403


def test_today_notes_injection():
    """今天做过/读到的事：进 B 库 → 注入当天上下文 → 跨天自动不再注入（记忆保留）。"""
    import tempfile
    import datetime as _dt
    import db as _db
    import memory as _mem
    import context as _ctx
    import life as _life

    tmp = Path(tempfile.mkdtemp())
    today = _dt.date.today().isoformat()
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        # 1) 读到有触动的地方 → 进素材池，并顺手落 B 库（不额外调 LLM）
        #    v0.89：素材只来自章节结算的打动（泛化"手头的小事"已退役）
        _life._note_shareable_text(
            "读到《三体》的「黑暗森林」，有点被触动：不要回答", _dt.datetime.now())
        notes = _mem.today_notes()
        assert len(notes) == 1, f"今天的事应进 B 库：{notes}"
        assert "三体" in (notes[0].get("content") or ""), f"正文应带书名：{notes[0]}"

        # 2) 注入当天上下文
        msgs = _ctx.build_chat_messages("AWAKE", ["你今天干嘛了"])
        joined = "\n".join(m.get("content", "") for m in msgs)
        assert "我今天做过的事" in joined, "应注入今天的素材"
        assert "不要回答" in joined, "注入里应能看到读到的内容"

        # 3) 跨天不再注入，但 B 库记录保留（记忆还在）
        assert _mem.today_notes("2020-01-01") == [], "跨天不该再注入"
        assert _db.long_memory_count() == 1, "B 库记录应保留"
        print("ok 今日素材（B 库落库 + 当天注入 + 跨天不注入）")


def test_user_library():
    """S4 用户书库：本地 txt（含 gbk）/ epub 解析、偏好选书、段级进度、结构与内置同构。"""
    import json
    import tempfile
    import zipfile

    import config as _cfg
    import db as _db
    import library as _lib

    def _make_epub(path, title, author, chapters):
        container = ('<?xml version="1.0" encoding="UTF-8"?>'
                     '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                     '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                     'media-type="application/oebps-package+xml"/></rootfiles></container>')
        manifest = "".join(f'<item id="c{i}" href="chapter{i}.xhtml" '
                           f'media-type="application/xhtml+xml"/>' for i in range(len(chapters)))
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
        opf = (f'<?xml version="1.0" encoding="UTF-8"?>'
               f'<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
               f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
               f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator></metadata>'
               f'<manifest>{manifest}</manifest><spine>{spine}</spine></package>')
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", container)
            z.writestr("OEBPS/content.opf", opf)
            for i, ch in enumerate(chapters):
                z.writestr(f"OEBPS/chapter{i}.xhtml",
                           f"<html><body><p>{ch}</p></body></html>")

    tmp = Path(tempfile.mkdtemp())
    # BOOKS_DIR 先指向临时书架；body 中途会再改（测空书架），退出统一还原
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       BOOKS_DIR=tmp / "books"):
        books_dir = tmp / "books"
        (books_dir / "科幻").mkdir(parents=True)
        (books_dir / "散文").mkdir(parents=True)
        # txt（gbk 编码，模拟中文书常见编码）
        (books_dir / "科幻" / "三体.txt").write_bytes(
            "给时光以生命，给岁月以文明。\n\n给岁月以文明，而不是给文明以岁月。\n\n"
            "弱小和无知不是生存的障碍，傲慢才是。".encode("gbk"))
        # epub（带书名/作者元数据）
        _make_epub(books_dir / "散文" / "背影.epub", "背影", "朱自清",
                   ["我与父亲不相见已二年余了。", "我最不能忘记的是他的背影。"])
        (books_dir / "README.txt").write_text("说明文件，不该被当成书", encoding="utf-8")
        config.BOOKS_DIR = books_dir

        # 夜猫子人格：爱读科幻
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "q",
            "text": "夜猫子", "wake_hour": 10, "sleep_hour": 2, "bubble_mult": 1.2,
            "reading_pref": "科幻", "drawn_at": "2026-10-10T00:00:00"}, ensure_ascii=False))

        items = _lib.books()
        assert len(items) == 2, f"应解析出 2 本书：{items}"
        assert {b["title"] for b in items} == {"三体", "背影"}, items
        # 旧摆法兼容：子目录名是细标签 → 归到它所属的一级分类
        got = {b["title"]: (b["category"], b["tags"], b["by"]) for b in items}
        assert got["三体"] == ("精品小说", ["科幻"], "dir"), got
        assert got["背影"] == ("文学", ["散文"], "dir"), got

        # 章节解析（v0.89：跳读退役，真读是唯一路径）——两本书都能切出章节
        import trueread as _tr
        for b in items:
            chs = _lib.chapters(b["id"])
            assert chs and all(c.get("char_count", 0) > 0 for c in chs), \
                f"{b['title']} 没切出章节：{chs}"

        # 偏好选书：人格两级偏好（精品小说 + [科幻]）命中三体
        picked = _tr.pick_book()
        assert picked and picked["title"] == "三体", f"偏好选书没命中三体：{picked}"

        # 圈章：从书签起按整数章节累计（宁少勿超）
        chs, acc = _tr.plan_from(picked["id"], 0, 10 ** 9)
        assert chs and acc > 0, "没圈出章节"
        one, acc1 = _tr.plan_from(picked["id"], 0, 1)   # 上限极小 → 宁少勿超，至少读首章
        assert len(one) == 1 and acc1 == one[0]["char_count"], (one, acc1)

        # 书级书签是唯一阅读进度真相：跨天不重置，推进只发生在章节结算
        _tr.set_bookmark(picked["id"], 2)
        assert _tr.bookmark() == {"book_id": picked["id"], "ch_idx": 2}, _tr.bookmark()
        assert _tr.pick_book()["id"] == picked["id"], "书签指向的书该继续读它（不换本）"
        assert _tr.chapter_at(picked["id"], 0), "按书签取得到章节正文"

        # 书架空 → 今日无任务（真读是唯一路径，不再回退内置书摘）
        config.BOOKS_DIR = tmp / "empty"
        assert _tr.pick_book() is None, "空书架应返回 None"

        print("ok 用户书库（txt/gbk + epub 解析 + 偏好选书 + 章节切分 + 书级书签）")


def test_library_categories():
    """书库分类（2026-10-11）：平铺不建子目录 + 映射表分类 + 手动优先 + 上传/重分类/删除。"""
    import tempfile

    import config as _cfg
    import corpus as _corpus
    import db as _db
    import library as _lib

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       BOOKS_DIR=tmp / "books"):
        books = config.BOOKS_DIR
        books.mkdir(parents=True)
        # 全部平铺在根目录：靠书名/作者/正文自动分类，一个子目录都不建
        (books / "三体.txt").write_text("给时光以生命，给岁月以文明。\n\n" + "弱小和无知不是生存的障碍。\n\n" * 20,
                                        encoding="utf-8")
        (books / "活着.txt").write_text("人是为了活着本身而活着，而不是为了活着之外的任何事物。\n\n" * 20,
                                        encoding="utf-8")
        (books / "孙子兵法.txt").write_text("兵者，诡道也。故能而示之不能，用而示之不用。\n\n" * 20,
                                            encoding="utf-8")
        (books / "随便起的名字.txt").write_text("这是一段完全看不出门类的正文内容。\n\n" * 20,
                                                encoding="utf-8")
        (books / "README.txt").write_text("说明文件，不该被当成书", encoding="utf-8")

        got = {b["id"]: b["category"] for b in _lib.books()}
        assert "README.txt" not in got, f"README 不该当书：{got}"
        assert got["三体.txt"] == "精品小说", got
        assert got["活着.txt"] == "文学", got
        assert got["孙子兵法.txt"] == "军事", got
        assert got["随便起的名字.txt"] == _corpus.UNCATEGORIZED, got

        # 分类清单：22 类 + 未分类兜底，计数对得上
        cats = {c["name"]: c["count"] for c in _lib.categories()}
        assert len(cats) == 23, f"应是 22 类 + 未分类：{len(cats)}"
        assert cats["精品小说"] == 1 and cats[_corpus.UNCATEGORIZED] == 1, cats

        # 手动指定 → by=manual，reindex 重跑自动分类也动不了它
        _lib.set_category("三体.txt", "科学技术", ["科普"])
        assert _lib.get("三体.txt")["by"] == "manual", _lib.get("三体.txt")
        _lib.reindex()
        assert _lib.get("三体.txt")["category"] == "科学技术", "手动选过的不能被自动重分类覆盖"

        # 上传：平铺存根目录（不建子目录）+ 自动分类；不支持的格式直接拒
        up = _lib.save_upload("人类简史.txt", ("认知革命让智人走上食物链顶端。\n\n" * 20).encode("utf-8"))
        assert up and up["category"] == "历史", up
        assert (books / "人类简史.txt").is_file(), "上传的书必须平铺在根目录"
        assert _lib.save_upload("坏东西.pdf", b"junk") is None, "只收 txt / epub"
        dup = _lib.save_upload("人类简史.txt", ("同名再来一本，正文也要够长才作数。\n\n" * 5).encode("utf-8"))
        assert dup and dup["id"] == "人类简史(2).txt", f"同名要另存，不能覆盖：{dup}"

        # 删书：文件 + 映射表条目一起没
        assert _lib.delete_book("人类简史(2).txt")
        assert not (books / "人类简史(2).txt").exists()
        assert _lib.get("人类简史(2).txt") is None

        print("ok 书库分类（平铺 + 映射表 + 手动优先 + 上传/重分类/删书）")


def test_epub_cover():
    """epub 封面解析（v0.78）：两种标准写法都认（meta name=cover / properties=cover-image）。"""
    import tempfile
    import zipfile

    import config as _cfg
    import db as _db
    import library as _lib

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       BOOKS_DIR=tmp / "books"):
        books = config.BOOKS_DIR
        books.mkdir(parents=True)
        jpg = b"\xff\xd8\xff\xe0" + b"fakecover" * 8      # 假图（只验抽取，不解码）

        def _epub(path, cover_meta: bool):
            container = ('<?xml version="1.0"?><container version="1.0" '
                         'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                         '<rootfile full-path="OEBPS/content.opf" '
                         'media-type="application/oebps-package+xml"/></rootfiles></container>')
            cover_item = ('<item id="cover-img" href="images/cover.jpg" media-type="image/jpeg"'
                          + (' properties="cover-image"/>' if not cover_meta else '/>'))
            meta = ('<meta name="cover" content="cover-img"/>' if cover_meta else "")
            opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:title>封面测试书</dc:title><dc:creator>某人</dc:creator>'
                   + meta + '</metadata>'
                   '<manifest><item id="c0" href="chapter0.xhtml" '
                   'media-type="application/xhtml+xml"/>' + cover_item + '</manifest>'
                   '<spine><itemref idref="c0"/></spine></package>')
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("mimetype", "application/epub+zip")
                z.writestr("META-INF/container.xml", container)
                z.writestr("OEBPS/content.opf", opf)
                z.writestr("OEBPS/chapter0.xhtml",
                           "<html><body><p>" + "这是一段足够长的正文内容。" * 5 + "</p></body></html>")
                z.writestr("OEBPS/images/cover.jpg", jpg)

        _epub(books / "甲书.epub", cover_meta=True)       # 写法① meta name=cover
        _epub(books / "乙书.epub", cover_meta=False)      # 写法② properties=cover-image
        (books / "丙书.txt").write_text("没有封面的 txt 书。\n\n" * 20, encoding="utf-8")

        got = {b["id"]: b["has_cover"] for b in _lib.books()}
        assert got["甲书.epub"] and got["乙书.epub"], f"两种封面的写法都要认出来：{got}"
        assert not got["丙书.txt"], "txt 不该有封面"

        data, ctype = _lib.cover_bytes("甲书.epub")
        assert ctype == "image/jpeg" and data == jpg, "封面应原样抽出、媒体类型按扩展名判"
        assert _lib.cover_bytes("丙书.txt") is None, "txt 没有封面"
        assert _lib.cover_bytes("不存在.epub") is None, "不存在的书不该抛异常"

        print("ok epub 封面（meta/properties 两种写法 + 无封面兜底 + 防越界）")


def test_library_epub_metadata():
    """epub 元数据的两个坑（v0.79 实测）：①OPF 命名空间拼错 ②dc:subject 是脏值。

    ① 掌上书苑(epubBuilder) 一批书把 <package xmlns=...> 写成 mlns=...，整份 OPF
       没有命名空间 → manifest/spine 全读不出来 → 正文 0 段 → 书在书架里凭空消失。
    ② 盗版转换本爱往 dc:subject 里塞源站名（ibook.178.com）/URL/书名本身，
       不洗掉会给分类凭空加 6 分，把书带偏（《时间简史》被 "lcsh: Novel" 带成小说）。
    """
    import tempfile
    import zipfile

    import config as _cfg
    import db as _db
    import library as _lib

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       BOOKS_DIR=tmp / "books"):
        books = config.BOOKS_DIR
        books.mkdir(parents=True)
        container = ('<?xml version="1.0"?><container version="1.0" '
                     'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                     '<rootfile full-path="OPS/fb.opf" '
                     'media-type="application/oebps-package+xml"/></rootfiles></container>')

        def _epub(path, pkg_attr: str, subject: str):
            opf = ('<?xml version="1.0"?><package version="2.0" ' + pkg_attr + '>'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:title>时间简史</dc:title><dc:creator>霍金</dc:creator>'
                   '<dc:subject>' + subject + '</dc:subject></metadata>'
                   '<manifest><item id="chapter1" href="chapter1.html" '
                   'media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="chapter1"/></spine></package>')
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("mimetype", "application/epub+zip")
                z.writestr("META-INF/container.xml", container)
                z.writestr("OPS/fb.opf", opf)
                z.writestr("OPS/chapter1.html",
                           "<html><body><p>" + "宇宙的起源与黑洞的蒸发。" * 30 + "</p></body></html>")

        # ① 命名空间拼错成 mlns（= 整份 OPF 无命名空间）
        _epub(books / "拼错命名空间.epub", 'mlns="http://www.idpf.org/2007/opf"', "科普")
        broke = _lib._parsed(books / "拼错命名空间.epub")
        assert broke and broke["passages"], "OPF 没命名空间也得解析出正文（否则书会消失）"
        assert broke["title"] == "时间简史", f"书名也该读到：{broke}"
        ids = {b["id"] for b in _lib.books()}
        assert "拼错命名空间.epub" in ids, "正文为 0 的书会被 _scan 丢掉——这就是那个 bug"

        # ② 脏 subject：站名 / URL / 国会图书馆前缀 / 跟书名同值
        assert _lib._clean_subjects(["ibook.178.com"], "某书") == [], "源站名要洗掉"
        assert _lib._clean_subjects(["http://x.com/a"], "某书") == [], "URL 要洗掉"
        assert _lib._clean_subjects(["lcsh: Novel"], "某书") == [], "lcsh 前缀是噪声"
        assert _lib._clean_subjects(["活着"], "活着") == [], "拿书名当题材等于没标"
        assert _lib._clean_subjects(["科幻小说", "ibook.178.com"], "某书") == ["科幻小说"], \
            "脏的丢、好的留"

        # 干净 subject 仍然生效（同一本书，脏值 → 靠书名/正文认成科学技术）
        _epub(books / "脏标签.epub", 'xmlns="http://www.idpf.org/2007/opf"', "ibook.178.com")
        assert not (_lib._parsed(books / "脏标签.epub") or {}).get("subject"), "脏 subject 不该留下"

        print("ok epub 元数据（OPF 命名空间容错 + dc:subject 洗脏值）")


def test_reading_pref_two_level():
    """阅读偏好两级（一级分类 + 细标签）：旧标签自动迁移，内置书摘翻译到同一套分类。"""
    import json
    import tempfile

    import config as _cfg
    import corpus as _corpus
    import db as _db

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        import persona as _persona
        import persona_pool as _pool

        # 旧卡（reading_pref 是老标签）→ 一级分类 + 细标签，口味没丢
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "q",
            "text": "夜猫子", "wake_hour": 10, "sleep_hour": 2, "bubble_mult": 1.2,
            "reading_pref": "科幻", "drawn_at": "2026-10-10T00:00:00"}, ensure_ascii=False))
        p = _persona.behavior_params()
        assert p["reading_pref"] == "精品小说", p
        assert p["reading_tags"] == ["科幻"], p

        # 新卡：POOL 里直接就是两级，且一级分类必须是 22 类之一
        names = [g["name"] for g in _corpus.GENRES]
        for _ in range(12):
            card = _pool.draw()
            assert card["reading_pref"] in names, card
            assert isinstance(card.get("reading_tags"), list), card
            for t in card["reading_tags"]:
                assert t in _corpus.PREF_LEGACY_MAP or any(
                    t in g["tags"] for g in _corpus.GENRES), (card, t)

        # 旧细标签 → 一级分类，必须落在 22 类之内（和用户书库同一套分类）
        for g, cat in _corpus.PREF_LEGACY_MAP.items():
            assert cat in names, f"旧标签 {g} 的归类 {cat} 不在 22 类里"

        print("ok 阅读偏好两级（分类 + 细标签）+ 旧标签迁移进同一套分类")


def test_trueread():
    """阅读真读（v0.89 瘦身版，时间面已归 life）：书级书签跨天不重置 + 整数章节圈定
    + 笔记判重 + 思考 buffer + 章节末结算落 alpha/beta + 打动进素材池。"""
    import json
    import tempfile
    from datetime import datetime as _dt

    import config as _cfg
    import db as _db
    import library as _lib
    import notes as _notes
    import trueread as _tr

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       BOOKS_DIR=tmp / "books", TRUE_READ_ENABLED=True):
        config.BOOKS_DIR.mkdir(parents=True, exist_ok=True)
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "reading_pref": "科幻",
            "drawn_at": "2026-10-09T00:00:00"}))

        # 1) txt 章节解析：第X章 正则切章（时间面不在这里，这里只管"读到哪"）
        txt = ("第一章 起\n" + "正文甲" * 2000 + "\n"
               "第二章 承\n" + "正文乙" * 2000 + "\n"
               "第三章 转\n" + "正文丙" * 2000)
        (_cfg.BOOKS_DIR / "测试书.txt").write_text(txt, encoding="utf-8")
        chs = _lib.chapters("测试书.txt")
        assert len(chs) >= 3, f"第X章 正则该切出 ≥3 章：{len(chs)}"
        assert chs[0]["title"].startswith("第一章"), chs[0]["title"]

        # 2) 选书 + 圈章：从书签起按整数章节累计（宁少勿超）
        book = _tr.pick_book()
        assert book and book["id"] == "测试书.txt", f"没选中测试书：{book}"
        picked, acc = _tr.plan_from(book["id"], 0, _cfg.READING_DAILY_CHARS)
        assert picked and acc <= _cfg.READING_DAILY_CHARS, f"圈定字数超上限：{acc}"

        # 3) 书级书签 = 唯一阅读进度真相：**跨天不重置**（治"每天重读第一章"）
        _tr.set_bookmark("测试书.txt", 1)
        assert _tr.bookmark() == {"book_id": "测试书.txt", "ch_idx": 1}, _tr.bookmark()
        rest, _ = _tr.plan_from("测试书.txt", 1, 10 ** 9)
        assert len(rest) == 2, f"从第 2 章起该只剩 2 章：{rest}"
        # align：与 life 的当日计划失步时以 life 为准对齐（冷启动跳章）
        assert _tr.align("测试书.txt", 0)["ch_idx"] == 0, _tr.bookmark()

        # 4) 思考 buffer：append 边读边想（思考即笔记，章节末一起结算）
        assert _tr.append_thought("这段有点意思") == 1
        assert _tr.append_thought("又想到一个") == 2
        assert len(_tr.buffer()) == 2

        # 5) 读书笔记双库：alpha（短概述）挂 long_id → beta（完整转述）
        a, b = _notes.add_pair("测试书.txt", "测试书", "第一章",
                               "第一章讲了起", "这章说的是起，然后……", "测试书 / 第一章", 1200)
        assert a > 0 and b > 0
        row = _db.get_reading_note(a)
        assert row["kind"] == "alpha" and row["long_id"] == b, f"alpha 应挂 long_id→beta：{row}"
        beta = _db.get_reading_note(b)
        assert beta["kind"] == "beta" and "这章说的是起" in beta["content"]

        # 5b) 判重（批0-②）：同一 (书, 章, kind) 再结算 = 覆盖，不堆第二条笔记
        a2, b2 = _notes.add_pair("测试书.txt", "测试书", "第一章",
                                 "换个说法的概述", "换个说法的转述", "测试书 / 第一章", 1200)
        assert (a2, b2) == (a, b), f"同章重复结算该覆盖：{(a2, b2)} vs {(a, b)}"
        assert _db.reading_note_count() == 2, f"不该有重复笔记：{_db.reading_note_count()}"

        # 6) alpha 注入：最近必带（按累计字数窗口）
        hint = _notes.prompt_hint()
        assert "我最近读的书" in hint and "第一章" in hint, hint

        # 7) 章节末结算（mock LLM 返回三行格式）：落笔记 + 打动 → 带出可分享的一句
        import llm as _llm
        real_chat = _llm.chat
        _llm.chat = lambda messages, temperature=0.7, thinking=False, **k: (
            "短概述：第一章开了个头\n完整转述：这章讲的是起，然后承\n打动：是")
        try:
            _tr.append_thought("这段值得记")
            result = _tr.settle_chapter()
            assert result and result["moved"] is True, f"结算应产出笔记且打动：{result}"
            assert result["alpha_id"] > 0 and result["beta_id"] > 0
            assert result.get("shareable_text"), "打动了要带出可分享的一句"
            # 结算完书签推进一章（下一章从头攒思考）
            assert _tr.bookmark()["ch_idx"] == 1, _tr.bookmark()
            assert _tr.buffer() == [], "章节末结算后思考 buffer 该清空"
        finally:
            _llm.chat = real_chat

        # 8) 真读关闭 → 不注入、不追加思考
        config.TRUE_READ_ENABLED = False
        assert _tr.prompt_hint() == ""
        n_before = len(_tr.buffer())
        assert _tr.append_thought("不该记") == n_before, "真读关了不该再攒思考"
        config.TRUE_READ_ENABLED = True

        print("ok 阅读真读（书级书签跨天 + 整数章节圈定 + 笔记判重 + 思考 buffer + 章节结算）")
