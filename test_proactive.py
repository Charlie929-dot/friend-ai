"""主动与上下文（主动消息/引用/兴趣/意识流/表情包/lore）（E4 拆分，v0.92）。

运行：`python run_tests.py proactive`（或全量 `python run_tests.py`）。
本文件顶部必须 `from _test_common import *`——两段全局机制（临时目录收口 +
关双层生成）在此生效，见约定 #10/#11。
"""
from _test_common import *  # noqa: F401,F403


def test_proactive_context_injection():
    """主动消息带最近聊天 + 「别说好久没聊」规则（实测 #26：瞎编开场）。"""
    import json
    import tempfile

    import context as _ctx
    import db as _db

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        # 两小时前刚聊过的话题
        _db.add_message("user", "我在赶论文，头都大了")
        _db.add_message("assistant", "那你先忙，写完这段再找我唠")
        # v0.103 批 2：impulse 场景要有话题弹药才生成（无弹药返回 None，不空转发）
        _db.set_kv("work_shareables", json.dumps(
            [{"day": "2026-10-10", "seq": 1, "text": "读到一句挺有意思的话", "used": 0}]))

        # 素材池是 50% 概率翻（其余走待办/承诺/兴趣/旧记忆），循环到命中为止
        msgs = None
        for _ in range(40):
            msgs = _ctx.build_proactive_messages("AWAKE", "impulse")
            if msgs:
                break
        assert msgs, "有弹药却没生成主动消息"
        contents = [m["content"] for m in msgs]
        assert any("论文" in c for c in contents), \
            "最近聊天没进主动消息上下文（还会瞎编开场）"
        assert any("好久没聊" in c for c in contents), "缺「别说好久没聊」的规则注入"
        # 顺序：system（人设）在最前、指令（内心活动）在最后说
        assert msgs[0]["role"] == "system", f"第一条应是 system：{msgs[0]['role']}"
        assert "好久没聊" in msgs[-1]["content"], "规则应贴在最后的指令里"
        print("ok 主动消息上下文（带最近聊天 + 别瞎编开场规则）")


def test_quote_context_injection():
    """引用回复后端对齐：带 quote 时注入被引句，让 TA 知道这句话在回哪句。"""
    import tempfile

    import context as _ctx
    import db as _db
    import persona as _persona

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):

        def _quote_msg(messages):
            return next((m for m in messages if m["role"] == "system"
                         and "引用回复" in (m.get("content") or "")), None)

        # 1) 不带 quote：不该注入
        assert _quote_msg(_ctx.build_chat_messages("AWAKE", ["在吗"])) is None, \
            "没引用不该出现【引用回复】注入"

        # 2) 引用 TA 的话（role=assistant）：注入被引原文 + TA 自己的名字
        q = {"id": 7, "role": "assistant", "content": "我最近在读《雾都孤儿》"}
        hint = _quote_msg(_ctx.build_chat_messages("AWAKE", ["继续讲讲"], quote=q))
        assert hint and "我最近在读《雾都孤儿》" in hint["content"], \
            f"被引原文没注入：{hint}"
        assert _persona.friend_name() in hint["content"], \
            f"引用 TA 的话该用 TA 的名字：{hint}"

        # 3) 引用对方自己的话（role=user）：用 TA 对对方的称呼
        q2 = {"id": 3, "role": "user", "content": "我明天考试"}
        hint2 = _quote_msg(_ctx.build_chat_messages("AWAKE", ["对对"], quote=q2))
        assert hint2 and "我明天考试" in hint2["content"], f"被引原文没注入：{hint2}"
        assert _persona.user_name() in hint2["content"], \
            f"引用对方的话该用 TA 对对方的称呼：{hint2}"

        # 4) content 为空：视为无引用，不注入
        assert _quote_msg(_ctx.build_chat_messages(
            "AWAKE", ["在吗"], quote={"id": 1, "role": "user", "content": "  "})) is None, \
            "空引用不该注入"
        print("ok 引用回复上下文（带引用注入被引句 / 无引用不注入 / 说话人名字正确）")


def test_proactive_segment():
    """主动消息分段：LLM 用换行把两段话并成一条时，拆成两条连发（修 bug：两段话没分开）。"""
    import json
    import tempfile
    import db as _db
    import llm as _llm
    from core import Engine

    class DummyChannel:
        def __init__(self):
            self.sent = []
        def send(self, text):
            self.sent.append(text)

    tmp = Path(tempfile.mkdtemp())
    real_chat = _llm.chat
    # WORK_ENABLED=False：隔离，否则 build_system_prompt 会触发排班/结算/生成素材
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       WORK_ENABLED=False):
        try:
            ch = DummyChannel()
            engine = Engine(ch)
            _llm.chat = lambda *a, **k: "刚从书里抬头\n\n《左传》看到襄公那段"
            # v0.103 批 2：impulse 要有话题弹药才发（素材池 50% 概率翻，多试几次必中）
            got = False
            for _ in range(40):
                _db.set_kv("work_shareables", json.dumps(
                    [{"day": "2026-10-10", "seq": 1, "text": "读到襄公那段挺有感觉", "used": 0}]))
                if engine._proactive("impulse") is True:
                    got = True
                    break
            assert got, "有弹药却没发出主动消息"
            assert ch.sent == ["刚从书里抬头", "《左传》看到襄公那段"], \
                f"主动消息没按换行分段：{ch.sent}"
            print("ok 主动消息分段（换行拆成两条连发）")
        finally:
            engine.stop()
            _llm.chat = real_chat


def test_proactive_pacing_and_stream():
    """主动消息段间停顿 + 意识流补记（v0.99 修：4 条同一秒拍出来、流里没影子）。"""
    import json
    import tempfile
    import time as _time
    import db as _db
    import llm as _llm
    import stream as _stream
    from core import Engine

    class DummyChannel:
        def __init__(self):
            self.sent = []
        def send(self, text):
            self.sent.append((_time.monotonic(), text))

    tmp = Path(tempfile.mkdtemp())
    real_chat = _llm.chat
    # WORK_ENABLED=False：隔离，否则 build_system_prompt 会触发排班/结算/生成素材
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       WORK_ENABLED=False, **_FAST_TIMING):
        try:
            ch = DummyChannel()
            engine = Engine(ch)
            _llm.chat = lambda *a, **k: "刚从书里抬头\n\n《左传》看到襄公那段"
            # impulse 要有话题弹药才发（素材池 50% 概率翻，多试几次必中）
            got = False
            for _ in range(40):
                _db.set_kv("work_shareables", json.dumps(
                    [{"day": "2026-10-10", "seq": 1, "text": "读到襄公那段挺有感觉", "used": 0}]))
                if engine._proactive("impulse") is True:
                    got = True
                    break
            assert got, "有弹药却没发出主动消息"
            assert [t for _, t in ch.sent] == ["刚从书里抬头", "《左传》看到襄公那段"], \
                f"主动消息没按换行分段：{ch.sent}"
            # 段间隔：第二段发出前要停一下（快档 SEND_MIN=0.01s，留裕量断言 ≥5ms）
            gap = ch.sent[1][0] - ch.sent[0][0]
            assert gap >= 0.005, f"两段之间没有停顿（间隔 {gap * 1000:.1f}ms）"
            # 意识流补记「我说：…」——主动消息也是自我叙事的一部分
            assert any(r["kind"] == "reply" and "我说：" in r["text"]
                       for r in _stream.recent(20)), "主动消息没进意识流"
            print("ok 主动消息段间停顿 + 意识流补记")
        finally:
            engine.stop()
            _llm.chat = real_chat


def test_share_impulse():
    """分享冲动（v0.103 批 2「念头驱动」）：素材产出即冲动 → 注入意识流 + 置待开口标记；
    每日预算内主动开口，预算尽只留念头（早/晚安或下次回复浮出）。"""
    import tempfile
    import db as _db
    import stream as _stream
    from core import Engine

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       SHARE_IMPULSE_ENABLED=True, SHARE_IMPULSE_DAILY_LIMIT=1,
                       CONSCIOUS_STREAM_ENABLED=True):

        class Ch:
            def send(self, text): pass

        eng = Engine(Ch())
        try:
            calls = []
            eng.proactive = lambda scenario: (calls.append(scenario) or True)

            # 无冲动标记 → 不开口
            eng.check_share_impulse()
            assert not calls, "没有冲动标记不该开口"

            # 素材产出 → 念头注入意识流 + 置待开口标记
            _stream.note_impulse("读到一句挺有感触的话")
            assert _db.get_kv("share_impulse_pending") == "1", "该置待开口标记"
            assert any(r["kind"] == "impulse" and "挺有感触" in r["text"]
                       for r in _stream.recent(20)), "念头该进意识流"

            # 预算内 → 主动开口（impulse 场景），标记清空
            eng.check_share_impulse()
            assert calls == ["impulse"], f"预算内该开口：{calls}"
            assert _db.get_kv("share_impulse_pending") == "", "开口后该清待开口标记"

            # 预算尽（limit=1，已用一次）→ 只清标记、不再开口
            _db.incr_counter("share_impulse_count")
            _stream.note_impulse("又想到一句")
            eng.check_share_impulse()
            assert calls == ["impulse"], f"预算尽不该再开口：{calls}"

            # 开关关 → 不置标记
            config.SHARE_IMPULSE_ENABLED = False
            _stream.note_impulse("关掉后不该有")
            assert _db.get_kv("share_impulse_pending") == "", "关开关后不该置标记"
        finally:
            config.SHARE_IMPULSE_ENABLED = True
            eng.stop()
    print("ok 分享冲动（素材产出即冲动 + 念头注入意识流 + 每日预算防刷屏）")


def test_interest_cultivation():
    """兴趣修炼（S4）：偏好从读/聊/刷三源动态养成，内核钉死不动。
    纯规则：信号词+主题词共现记线索，同一主题累计 N 次自动转正，不调 LLM。
    """
    import tempfile
    import db as _db
    import interests as _int
    import persona as _persona

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       INTEREST_PROMOTE_ENABLED=True):

        # 1) detect 保守：信号词 + 主题词共现才命中；只有主题词、或信号词带非兴趣对象不命中
        assert "历史" in _int.detect("我最近对历史挺感兴趣的，越看越上头"), \
            "信号+主题共现没命中"
        assert _int.detect("今天读了三章历史") == [], \
            "只有主题词没信号词不该算兴趣（防止把'读书'当'喜欢上'）"
        assert _int.detect("我有点上头") == [], \
            "只有信号词没主题对象不该命中"

        # 2) record 累计 + 阈值转正（默认 3 次）
        assert _int.record("科幻") is False and _int.record("科幻") is False
        assert _int.record("科幻") is True, "第 3 次该转正"
        assert _int.promoted() == [{"topic": "科幻", "count": 3}], _int.promoted()

        # 3) 转正后的兴趣进内核 prompt（追加，不是改内核）
        #    用真实内核渲染验证注入块，需要一张 persona 卡
        import persona_pool as _pool
        quiet = next(p for p in _pool.POOL if p["key"] == "quiet")
        _pool.save({
            "archetype": "quiet", "label": quiet["label"], "name": "闻屿",
            "quirk": "", "text": "x", "wake_hour": quiet["wake_hour"],
            "sleep_hour": quiet["sleep_hour"], "bubble_mult": quiet["bubble_mult"],
            "reading_pref": quiet["reading_pref"], "drawn_at": "2026-10-11T00:00:00",
            "kernel": _pool.KERNELS["quiet"],
        })
        compact = _persona.kernel_prompt("compact")
        assert "【我最近喜欢上的】科幻" in compact, \
            "转正的兴趣没进内核 prompt：" + compact[:120]
        assert "别把它们编成'你正在做的事'" in compact, "缺防编造生活约束"

        # 4) 未转正的主题不渲染
        _int.record("历史")  # 1 次，未到阈值
        assert "【我最近喜欢上的】" not in _persona.kernel_prompt("compact") \
            or "历史" not in _persona.kernel_prompt("compact"), "未转正的主题不该渲染"

        # 5) 开关关闭：检测/记录/渲染全部失效（保留旧行为）
        config.INTEREST_PROMOTE_ENABLED = False
        try:
            assert _int.detect("我对历史挺感兴趣") == []
            assert _int.record("历史") is False
            assert _int.prompt_hint() == ""
        finally:
            config.INTEREST_PROMOTE_ENABLED = True

        print("ok 兴趣修炼（词表检测 + 阈值转正 + 内核追加注入 + 防编造 + 开关）")


def test_emoji_pool():
    """表情包池（E 模块）：分桶库存 + 防重样 + 维护 + 注入尊重人格 emoji 偏好。"""
    import tempfile
    import db as _db
    import emoji_pool as _ep
    import persona as _persona
    import persona_pool as _pool

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       EMOJI_POOL_ENABLED=True):

        # 1) 内置 10 桶都在，每桶至少有几个表情，且每个表情带 text/emotion/img 字段
        bs = _ep.buckets()
        assert bs[:10] == ["开心", "吐槽", "无奈", "惊讶", "委屈", "得意", "无聊", "疲惫", "害羞", "生气"], bs
        for b in bs[:10]:
            pool = _ep.items(b)
            assert len(pool) >= 3, f"桶 {b} 表情太少：{pool}"
            for it in pool:
                assert set(("text", "emotion", "img")) <= set(it), f"表情字段不全：{it}"

        # 2) pick 防重样：把某桶全用过之后才退回重抽
        pool = _ep.items("开心")
        texts = {it["text"] for it in pool}
        got = _ep.pick("开心", seen=texts - {next(iter(texts))})
        assert got["text"] in texts, "pick 返回了桶外的表情"

        # 3) 维护：新增桶 / 新增表情 / 删表情 / 删桶
        ok, _ = _ep.add_bucket("治愈")
        assert ok and "治愈" in _ep.buckets(), "新增桶失败"
        ok, err = _ep.add_bucket("治愈")
        assert not ok, "重复新增桶该失败"
        ok, _ = _ep.add_emoji("治愈", "🌿", "被治愈了")
        assert ok and any(it["text"] == "🌿" for it in _ep.items("治愈")), "新增表情失败"
        ok, err = _ep.add_emoji("治愈", "🌿")
        assert not ok, "同桶重复表情该失败"
        ok, _ = _ep.add_emoji("开心", "🥳", "额外的开心")  # 往内置桶加
        assert any(it["text"] == "🥳" for it in _ep.items("开心")), "往内置桶加表情失败"
        ok, _ = _ep.remove_emoji("治愈", "🌿")
        assert ok and not any(it["text"] == "🌿" for it in _ep.items("治愈")), "删表情失败"
        ok, _ = _ep.remove_bucket("治愈")
        assert ok and "治愈" not in _ep.buckets(), "删桶失败"
        ok, _ = _ep.remove_bucket("开心")
        assert not ok, "内置桶不可删"

        # 4) 注入：用 sunshine 卡（emoji 频率"偏高"、禁止 ❤️😘）
        #    ⚠️ 不能用 quiet——它 voice.emoji 频率是"极低"，本就不该被注入表情库存。
        sun = next(p for p in _pool.POOL if p["key"] == "sunshine")
        _pool.save({
            "archetype": "sunshine", "label": sun["label"], "name": "小葵",
            "quirk": "", "text": "x", "wake_hour": sun["wake_hour"],
            "sleep_hour": sun["sleep_hour"], "bubble_mult": sun["bubble_mult"],
            "reading_pref": sun["reading_pref"], "drawn_at": "2026-10-11T00:00:00",
            "kernel": _pool.KERNELS["sunshine"],
        })
        hint = _ep.prompt_hint()
        assert hint and "表情库存" in hint, "表情库存没注入"
        assert "❤️" not in hint and "😘" not in hint, "禁止的 emoji 不该出现在库存里"
        assert "开心" in hint, "情绪桶该出现在库存里"

        # 5) 防重样：TA 最近发过 😂，注入里该提醒别重复
        _db.add_message("assistant", "哈哈哈哈 😂 笑死")
        hint2 = _ep.prompt_hint()
        assert "😂" in hint2 and "别重复" in hint2, f"最近用过的表情没被提醒别重复：{hint2}"

        # 6) 尊重人格：quiet（emoji 频率"极低"）不该被注入表情库存
        quiet = next(p for p in _pool.POOL if p["key"] == "quiet")
        _pool.save({
            "archetype": "quiet", "label": quiet["label"], "name": "闻屿",
            "quirk": "", "text": "x", "wake_hour": quiet["wake_hour"],
            "sleep_hour": quiet["sleep_hour"], "bubble_mult": quiet["bubble_mult"],
            "reading_pref": quiet["reading_pref"], "drawn_at": "2026-10-11T00:00:00",
            "kernel": _pool.KERNELS["quiet"],
        })
        assert _ep.prompt_hint() == "", "emoji 频率极低的人格不该被注入表情库存"

        # 7) 开关关闭：注入空串
        config.EMOJI_POOL_ENABLED = False
        try:
            assert _ep.prompt_hint() == "", "关开关后不该注入表情库存"
        finally:
            config.EMOJI_POOL_ENABLED = True

        print("ok 表情包池（分桶库存 + 防重样 + 维护 + 注入尊重人格 emoji 偏好）")


def test_emoji_img():
    """表情图（v0.70）：save_img 魔数判型 + add_emoji 带图 + 删表情/删桶连带删图 + 防目录穿越。"""
    import base64
    import tempfile
    import emoji_pool as _ep

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(BASE_DIR=tmp):
        # 1) 合法 PNG（1x1）存进去，文件名按内容 md5 + .png，文件真的落盘
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        name, err = _ep.save_img(base64.b64encode(png).decode())
        assert not err and name.endswith(".png"), f"存 PNG 失败：{name} {err}"
        assert _ep.img_file(name) is not None, "图片文件没落盘"
        assert _ep.img_type(name) == "image/png", "PNG 的 Content-Type 不对"

        # 2) 同图去重（md5 同名）+ 非法数据拒绝 + 目录穿越拒绝
        name2, _ = _ep.save_img(base64.b64encode(png).decode())
        assert name2 == name, "同图该去重同名"
        assert not _ep.save_img("not-base64!!")[0], "非法 base64 该被拒"
        assert not _ep.save_img(base64.b64encode(b"<script>evil").decode())[0], "非图片魔数该被拒"
        assert _ep.img_file("../emoji_pool.json") is None, "目录穿越该被拒"
        assert _ep.img_file("不存在的.png") is None

        # 3) add_emoji 带图 → payload 能看到；删表情连带删图文件
        ok, err = _ep.add_emoji(" test-bucket ", "熊猫头", "无语", name)
        assert ok, f"带图表情没存上：{err}"
        item = next(it for it in _ep.items("test-bucket") if it["text"] == "熊猫头")
        assert item["img"] == name, "表情的 img 字段不对"
        assert any(it.get("img") for b in _ep.payload() for it in b["items"] if it["text"] == "熊猫头")
        ok, _ = _ep.remove_emoji("test-bucket", "熊猫头")
        assert ok and _ep.img_file(name) is None, "删带图表情没连带删图文件"

        # 4) 删桶也连带删图
        ok, err = _ep.add_emoji("图桶", "再来一张", "", _ep.save_img(base64.b64encode(png).decode())[0])
        assert ok, err
        leftover = next(it["img"] for it in _ep.items("图桶"))
        ok, _ = _ep.remove_bucket("图桶")
        assert ok and _ep.img_file(leftover) is None, "删桶没连带删图文件"

        print("ok 表情图（save_img 魔数判型 + 维护连带删图 + 防目录穿越）")


def test_consciousness_stream():
    """自我意识流（S4，第一步核心闭环）：流表读写 + 状态行（含读到哪/精力/心情）+
    生活事件埋点 + 每轮注入。治"不知道今天干了啥"（实测 #36 #2/#3）。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import config as _cfg
    import db as _db
    import life as _life
    import stream as _stream

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       WORK_ENABLED=True, MOOD_ENABLED=True, WORK_SHAREABLE_ENABLED=False,
                       CONSCIOUS_STREAM_ENABLED=True):
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "reading_pref": "科幻",
            "drawn_at": "2026-10-09T00:00:00"}))
        # 手造今日生活（v2）：不依赖真实书架，测试要确定
        morning = _dt(2026, 10, 9, 10, 0)
        day = _mk_day("2026-10-09", morning,
                      [int(_life.NOMINAL_SESSION_MIN * _life.speed())],
                      book_title="测试书", warmup=10)
        first_start = _dt.fromisoformat(day["sessions"][0]["start"])

        # 1. 追加/读取：append 一条，recent 能查到且带生活日 day
        sid = _stream.append("wake", "我伸了懒腰，起床了", morning)
        assert sid > 0, "append 应返回自增 id"
        rows = _stream.recent(10)
        assert rows and rows[0]["text"] == "我伸了懒腰，起床了", rows
        assert rows[0]["day"] == "2026-10-09", rows[0]

        # 2. 状态行：忙/心流时有 phase/读到哪/精力/心情
        st = _stream.status_line(first_start + _td(minutes=2))
        assert st["phase"] and st["reading"] and st["mood"], st
        assert "读到《测试书》" in st["reading"], f"状态行该带读到哪：{st}"
        assert st["energy"] >= 95, f"刚开工精力该还满：{st}"

        # 3. 生活事件埋点：开工 / 读到哪章 各自只记一次（once 去重，不刷屏）
        _life.status(first_start + _td(minutes=2))
        _life.status(first_start + _td(minutes=5))
        work_lines = [r["text"] for r in _stream.recent(20) if r["kind"] == "work"]
        assert len(work_lines) == 1, f"开工只该记一次：{work_lines}"
        read_lines = [r["text"] for r in _stream.recent(20) if r["kind"] == "reading"]
        assert any("在读《测试书》" in t for t in read_lines), read_lines

        # 4. prompt 注入：含"读到哪/精力还剩/心情"；
        #    v0.89 §5.1 数字给屏幕——不再念"攒了多少/多少块"
        hint = _stream.prompt_hint(first_start + _td(minutes=6))
        assert "读到《测试书》" in hint and "精力还剩" in hint and "心情" in hint, hint
        assert "攒了多少" not in hint and "块" not in hint, \
            f"prompt 里念了数字账本（数字该给 HUD）：{hint}"

        # 5. 睡着 / 关工作系统 → 不注入（没有"自己的日子"就无从谈意识）
        _db.set_state("SLEEPING")
        assert _stream.prompt_hint(first_start) == "", "睡着不该注入意识流"
        _db.set_state("AWAKE")
        config.WORK_ENABLED = False
        assert _stream.prompt_hint(first_start) == "", "关工作系统不该注入意识流"
        config.WORK_ENABLED = True

        # 6. render_day：把当天流渲染成第一人称时间线（睡前浓缩的数据源）
        day_text = _stream.render_day("2026-10-09")
        assert "我伸了懒腰，起床了" in day_text, day_text
        assert "开始读今天圈的书了" in day_text, day_text

        # 7. 睡前浓缩：selfmem.review 收到当天流（mock LLM 捕获 system prompt 断言带流）
        import llm as _llm
        import selfmem as _self
        captured = {}
        real_chat = _llm.chat
        def _fake_chat(messages, *a, **k):
            captured["system"] = messages[0]["content"]
            return ('{"stances":[],"promises":[],"mistakes":[],"my_topics":[],'
                    '"relation":{},"state":{},"self_observation":""}')
        _llm.chat = _fake_chat
        try:
            _self.review("对方：在吗\n阿夜：在", day_text)
        finally:
            _llm.chat = real_chat
        assert "今天你自己是怎么过的" in captured.get("system", ""), \
            "selfmem.review 缺意识流数据源段：" + captured.get("system", "")
        assert "开始读今天圈的书了" in captured.get("system", ""), \
            "当天意识流没进 selfmem.review 的 prompt"

        print("ok 自我意识流（流表读写 + 状态行含读到哪/精力 + 生活事件埋点 + 每轮注入 + 睡前浓缩）")


def test_lore():
    """世界观（§1 架空设定）：来历骨架注入 + canon 设定集追加去重 + 防自相矛盾 + 开关。"""
    import json
    import tempfile
    import config as _cfg
    import db as _db
    import lore as _lore
    import persona as _persona

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       LORE_ENABLED=True):

        # 1. 骨架注入：来历（第一人称"我"）+ 节奏控制（指令层"你"）都在
        hint = _lore.prompt_hint()
        assert "我的来历" in hint, f"缺来历骨架：{hint}"
        assert "来自一个完全不同的世界" in hint, "缺'从哪来'骨架"
        assert "读书获取经验" in hint, "缺'靠什么成长'骨架"
        assert "不主动大段倾倒" in hint, "缺节奏控制"

        # 2. 骨架注入进 static system prompt（身份的一部分）
        sp = _persona.static_system_prompt()
        assert "我的来历" in sp, "世界观没进 static system prompt"

        # 3. canon 设定集：追加 + 去重 + 防自相矛盾
        assert _lore.add_canon("我那边的世界没有四季，只有明暗两季")
        assert _lore.add_canon("我那边的世界没有四季，只有明暗两季") is False, "相同细节不该重复入库"
        assert _lore.add_canon("   ") is False, "空细节不该入库"
        assert len(_lore.canon()) == 1

        # 4. 已确立的 canon 注入进 prompt，提醒别自相矛盾
        hint2 = _lore.prompt_hint()
        assert "明暗两季" in hint2, "canon 细节没注入：%s" % hint2
        assert "别跟这些对不上" in hint2, "缺防自相矛盾提醒"

        # 5. 开关关闭 → 不注入
        config.LORE_ENABLED = False
        assert _lore.prompt_hint() == ""
        config.LORE_ENABLED = True

        print("ok 世界观（来历骨架 + canon 设定集追加去重 + 防自相矛盾 + 开关）")
