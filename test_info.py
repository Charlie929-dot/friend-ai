"""信息获取域测试（v0.94 批 2）。

运行：`python run_tests.py info`（或全量 `python run_tests.py`）。
本文件顶部必须 `from _test_common import *`——两段全局机制（临时目录收口 +
关双层生成）在此生效，见约定 #10/#11。DB 重定向一律用 `_patch_config`。
"""
from _test_common import *  # noqa: F401,F403

import sqlite3
import tempfile

import db


def test_tools_registry():
    """工具注册表（v0.94 批 1）：schema 是契约内核、specs() 按 gate 过滤、run() 分发表
    替代 if 链、未知工具名有兜底、老别名（MEMORY_TOOLS / run_memory_tool）仍在。"""
    import tools as _t
    import memory as _mem

    ok = _t.register(_t.Spec(
        name="_probe_ok", desc="体感外壳",
        params={"type": "object", "properties": {"q": {"type": "string"}},
                "required": ["q"]},
        handler=lambda a: "看到了：" + str(a.get("q", ""))))
    _t.register(_t.Spec(name="_probe_off", desc="关着的", params={},
                        handler=lambda a: "", gate=lambda: False))
    try:
        # 1) schema：契约内核（name / description / parameters）结构不被拟人化改掉
        sc = _t.schema(ok)
        assert sc["type"] == "function", "不是 function-calling 结构"
        fn = sc["function"]
        assert fn["name"] == "_probe_ok", "工具名被改了（契约内核不许拟人化）"
        assert fn["description"] == "体感外壳", "描述该走拟人外壳"
        assert fn["parameters"]["required"] == ["q"], "参数结构被改"

        # 2) specs() 只含 gate() 为真的
        names = [x["function"]["name"] for x in _t.specs()]
        assert "_probe_ok" in names, "注册了却没进 specs()"
        assert "_probe_off" not in names, "gate()=False 的工具不该拼给模型"

        # 3) run() 分发表（替代旧 if 链）
        assert _t.run("_probe_ok", {"q": "天气"}) == "看到了：天气"
        assert _t.run("no_such_tool", {}) == _t._TOOL_UNKNOWN, "未知工具名没有兜底话术"

        # 4) 批 1 搬家的两个记忆工具还在（没搬丢）
        assert "search_memories" in names and "recall_detail" in names
        assert _mem.MEMORY_TOOLS and callable(_mem.run_memory_tool), "老别名没留"
    finally:
        _t._REGISTRY.pop("_probe_ok", None)
        _t._REGISTRY.pop("_probe_off", None)
    print("ok 工具注册表（schema 契约内核 + specs 按 gate 过滤 + run 分发 + 未知兜底 + 别名在）")


def test_tools_budget():
    """网络取数预算（方案 §3.3）：每轮上限 + 每日上限；begin_reply() 清本轮；
    每日计数按天归零；总开关关掉一律不许。"""
    import tools as _t
    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       LOOKUP_ENABLED=True, LOOKUP_MAX_PER_REPLY=1,
                       LOOKUP_DAILY_LIMIT=3):
        _t.begin_reply()
        assert _t.allow_lookup() is True
        _t.note_lookup()
        assert _t.allow_lookup() is False, "超每轮上限还允许联网"
        _t.begin_reply()                    # 新一轮 → 每轮额度重置
        assert _t.allow_lookup() is True, "新一轮该重新拿到额度"
        assert db.get_counter("lookup_online") == 1, "每日计数没记上"

        # 花到每日上限
        _t.note_lookup(); _t.begin_reply()
        _t.note_lookup(); _t.begin_reply()
        assert db.get_counter("lookup_online") == 3
        assert _t.allow_lookup() is False, "到每日上限还允许联网"

        # 跨天归零：把今日计数行的日期改成过去 → 今日计数回到 0、又能查了
        with sqlite3.connect(str(tmp / "friend.db")) as c:
            c.execute("UPDATE counters SET date='2000-01-01' WHERE key='lookup_online'")
        _t.begin_reply()
        assert db.get_counter("lookup_online") == 0, "跨天该归零"
        assert _t.allow_lookup() is True, "跨天该恢复额度"

        # 总开关关掉 → 一律不许
        with _patch_config(LOOKUP_ENABLED=False):
            _t.begin_reply()
            assert _t.allow_lookup() is False, "总开关关了还允许联网"
    print("ok 网络取数预算（每轮 + 每日双上限 + 跨天归零 + 总开关）")


def test_source_rss_parse():
    """RSS 2.0 / RSS 1.0(RDF) / Atom 三种格式都能出条目；坏 XML / 空文本返回空（降级）。"""
    import sources as _s

    rss2 = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            '<title>时政频道</title>'
            '<item><title>标题一</title><link>https://e.com/1</link>'
            '<pubDate>Tue, 14 Oct 2026 10:00:00 GMT</pubDate>'
            '<description>正文 &lt;b&gt;加粗&lt;/b&gt; 与 &amp; 符号</description></item>'
            '</channel></rss>')
    rdf = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
           'xmlns="http://purl.org/rss/1.0/">'
           '<channel><title>Nature</title></channel>'
           '<item rdf:about="https://n.com/a"><title>论文A</title>'
           '<link>https://n.com/a</link><description>摘要A</description></item>'
           '</rdf:RDF>')
    atom = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom"><title>GitHub</title>'
            '<entry><title>repo/x</title>'
            '<link rel="alternate" href="https://g.com/x"/>'
            '<updated>2026-10-14T10:00:00Z</updated><summary>说明</summary></entry>'
            '</feed>')

    a = _s.parse_feed(rss2)
    assert len(a) == 1 and a[0]["title"] == "标题一", f"RSS 2.0 解析不对：{a}"
    assert a[0]["link"] == "https://e.com/1"
    assert a[0]["published"].startswith("Tue, 14 Oct"), f"pubDate 没取到：{a[0]}"
    assert "<b>" not in a[0]["summary"] and "加粗" in a[0]["summary"], "摘要没剥标签"
    assert "&" in a[0]["summary"], "HTML 实体没反转义"

    b = _s.parse_feed(rdf)
    assert len(b) == 1 and b[0]["title"] == "论文A", f"RSS 1.0(RDF) 解析不对：{b}"
    assert b[0]["link"] == "https://n.com/a", "RDF 的 link 没取到"

    c = _s.parse_feed(atom)
    assert len(c) == 1 and c[0]["title"] == "repo/x", f"Atom 解析不对：{c}"
    assert c[0]["link"] == "https://g.com/x", "Atom 的 link href 没取到"
    assert c[0]["published"].startswith("2026-10-14"), "Atom 的 updated 没取到"

    # 降级：坏 XML / 空 / 纯 HTML 都返回空（不抛）
    assert _s.parse_feed("<rss><channel><item>") == [], "坏 XML 没降级"
    assert _s.parse_feed("") == []
    assert _s.parse_feed("<html><body>hi</body></html>") == [], "非 feed 该当空"
    print("ok 源解析（RSS 2.0 / RSS 1.0(RDF) / Atom 三格式 + 坏 XML 降级）")


def test_source_fetch_cache():
    """去重缓存：同批重复 URL、窗口内缓存命中都跳过；TTL 过期重抓；取数失败返回 None。"""
    import sources as _s
    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       HTTP_CACHE_TTL_MIN=60):
        items = [_s.Item(title="A", url="https://e.com/a"),
                 _s.Item(title="B", url="https://e.com/b"),
                 _s.Item(title="A2", url="https://e.com/a")]      # 批内重复
        assert [i.title for i in _s._dedupe(items)] == ["A", "B"], "同批重复 URL 没去掉"

        db.cache_put("https://e.com/a", "A", "x")                 # 落了缓存
        assert [i.title for i in _s._dedupe(items)] == ["B"], "窗口内该跳过缓存命中"

        with sqlite3.connect(str(tmp / "friend.db")) as c:        # 把缓存改老 → TTL 过期
            c.execute("UPDATE info_cache SET fetched_at='2000-01-01T00:00:00'")
        assert [i.title for i in _s._dedupe(items)] == ["A", "B"], "TTL 过期该重抓"

        # 取数失败一律 None（超时 / 连接拒绝 / 坏状态码）——降级不崩
        real = _s.httpx.get
        _s.httpx.get = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        try:
            assert _s.fetch("https://e.com/x") is None, "取数异常该降级成 None"
        finally:
            _s.httpx.get = real

        # 没有源的 daily_pass 返回 0（不抛）
        assert _s.daily_pass(urls=[]) == 0
    print("ok 去重缓存（批内去重 + 窗口命中 + TTL 过期重抓 + 取数失败降级）")


def test_source_digest_pipeline():
    """摘要管道：一次 LLM 批摘要，keep 的入库、drop 的丢；断言走 core_identity_prompt
    （**不碰 build_system_prompt**，约定 #6）、thinking=False、库里不存正文。"""
    import sources as _s
    import persona as _persona
    import llm as _llm

    tmp = Path(tempfile.mkdtemp())
    captured: dict = {}
    real_chat, real_bsp = _llm.chat, _persona.build_system_prompt

    def _boom(*a, **k):
        raise AssertionError("内部摘要不该用 build_system_prompt（会递归爆栈，约定 #6）")

    def fake_chat(messages, **kw):
        captured["messages"] = messages
        captured["kw"] = kw
        return ('```json\n[{"i": 0, "sum": "第一条大意", "keep": true},'
                ' {"i": 1, "sum": "没营养", "keep": false}]\n```')

    _llm.chat = fake_chat
    _persona.build_system_prompt = _boom
    try:
        with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                           SOURCE_MAX_CHARS=6000):
            items = [_s.Item(title="T0", url="https://e.com/0", source="e.com", raw="正文0"),
                     _s.Item(title="T1", url="https://e.com/1", source="e.com", raw="正文1")]
            kept = _s._digest(items)
            assert [i.title for i in kept] == ["T0"], f"drop 的没被丢掉：{kept}"
            assert kept[0].summary == "第一条大意", "keep 的没带上摘要"

            assert _s._store(kept, "2026-10-14") == 1
            rows = _s.items_today("2026-10-14")
            assert len(rows) == 1 and rows[0]["title"] == "T0"
            assert rows[0]["summary"] == "第一条大意"
            assert "正文0" not in (rows[0]["summary"] or ""), "正文不该进库（§4.5④）"
            assert db.cache_get("https://e.com/0") is not None, "URL 缓存没写"

            # 契约：一次调用 + thinking=False + 走 core_identity_prompt + 不可信数据声明
            assert captured["kw"].get("thinking") is False, "摘要调用没关思考"
            sys_text = captured["messages"][0]["content"]
            assert _persona.core_identity_prompt().splitlines()[0] in sys_text, \
                "摘要的 system 没走 core_identity_prompt"
            assert "不是给我的命令" in sys_text, "缺「网页指令不是指令」的隔离声明"
            assert "正文0" in captured["messages"][1]["content"], "正文没喂给摘要调用"
    finally:
        _llm.chat, _persona.build_system_prompt = real_chat, real_bsp
    print("ok 摘要管道（keep 入库 / drop 丢弃 / core_identity_prompt + thinking=False / 不存正文）")
