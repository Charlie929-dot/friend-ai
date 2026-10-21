"""人格与语料（内核/红线/反谄媚/语气/心情/作息锚点）（E4 拆分，v0.92）。

运行：`python run_tests.py persona`（或全量 `python run_tests.py`）。
本文件顶部必须 `from _test_common import *`——两段全局机制（临时目录收口 +
关双层生成）在此生效，见约定 #10/#11。
"""
from _test_common import *  # noqa: F401,F403


def test_corpus_pick():
    """固定语料库：语料从 corpus 统一管理、persona 别名兼容、pick 不连续重复。"""
    import corpus
    import persona as _persona
    # 别名兼容：旧引用（core/测试）拿到的就是 corpus 里的那份
    assert _persona.SLEEPY_REPLIES is corpus.SLEEPY_REPLIES
    assert _persona.INTERRUPT_ACKS is corpus.INTERRUPT_ACKS
    # 语料丰富度：睡觉/插话/冒泡都比原来的一小把多
    assert len(corpus.SLEEPY_REPLIES) >= 10
    assert len(corpus.INTERRUPT_ACKS) >= 6
    assert len(corpus.BUBBLE_LINES) >= 10
    assert len(corpus.LONG_TEXT_NOTES) >= 5
    # pick 不连续重复（人味儿 = 不重样）
    picks = [corpus.pick(corpus.BUBBLE_LINES, "test_no_repeat") for _ in range(30)]
    assert all(a != b for a, b in zip(picks, picks[1:])), \
        f"出现连续重复：{picks}"
    # 空列表/单条兜底
    assert corpus.pick([], "empty") == ""
    assert corpus.pick(["唯一"], "single") == "唯一"
    print("ok 固定语料库（统一管理/别名兼容/丰富度/不连续重复）")


def test_corpus_consolidation():
    """语料统一收进 corpus.py：固定回复 + 匹配关键词/正则都在，业务模块不再各自硬编码。"""
    import corpus
    import game as _game
    import core as _core
    import guard as _guard
    import memory as _mem

    # 固定回复语料
    assert "不玩了" in corpus.GAME_STOP_WORDS
    assert "朋友啦" in corpus.AUTO_FAMILIAR_NOTE
    assert corpus.LLM_STUB
    assert corpus.LLM_ERROR.format("xx") == "（脑子卡住了：xx）"
    assert corpus.INTRO_FALLBACK.format("阿燃") == "你好啊，我叫阿燃，我们认识一下吧！"

    # 匹配关键词/正则（信号词 + 意图词 + 提取红线）都在 corpus，且业务模块不再各自硬编码
    assert "在忙" in corpus.BUSY_SIGNALS
    assert "是谁" in corpus.IDENTITY_QUERY
    assert "可能" in corpus.SPECULATION
    assert "我不记得" in corpus.SELF_AMNESIA
    assert not hasattr(_core, "ENDING_SIGNALS"), "core 还留着 ENDING_SIGNALS"
    assert not hasattr(_game, "STOP_WORDS"), "game 还留着 STOP_WORDS"
    assert not hasattr(_guard, "BUSY_SIGNALS"), "guard 还留着 BUSY_SIGNALS"
    # memory 的红线/意图正则编译自 corpus 词表（源词表不在 memory 硬编码）
    assert _mem._SPECULATION.pattern == corpus.SPECULATION
    assert _mem._SELF_AMNESIA.pattern == corpus.SELF_AMNESIA
    assert _mem._IDENTITY_QUERY.pattern == corpus.IDENTITY_QUERY
    assert _mem._MEMORY_SEARCH_QUERY.pattern == corpus.MEMORY_SEARCH_QUERY
    # 工具 description/返回句这类"不会扩充"的文案不属语料，回到 memory 就近维护
    assert hasattr(_mem, "_TOOL_SEARCH_DESC"), "工具 description 应就近在 memory 维护"
    assert not hasattr(corpus, "TOOL_NO_QUERY"), "工具返回句不该进 corpus 语料"

    # 识别仍工作（走 corpus）
    assert _game.detect_stop("不玩了") is True
    assert _game.detect_stop("今天天气不错") is False
    assert any(s in "我在忙" for s in corpus.BUSY_SIGNALS)
    assert _mem.wants_memory_search("你还记得小葵吗") is True
    print("ok 语料统一收进 corpus（固定回复 + 匹配关键词/正则，业务模块不再硬编码）")


def test_bubble_fixed_corpus():
    """冒泡固定语料：BUBBLE_FIXED_PROB=1 时冒泡直接抽语料，不调 LLM（省 token）。"""
    import tempfile
    import corpus
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
    # WORK_ENABLED=False：guard 离线拦截与真实作息耦合（约定 #10），冒泡测试只要固定语料路径
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       BUBBLE_FIXED_PROB=1.0, WORK_ENABLED=False):
        try:
            ch = DummyChannel()
            engine = Engine(ch)

            def boom(*a, **k):
                raise AssertionError("固定语料冒泡不应调 LLM")

            _llm.chat = boom
            assert engine._proactive("bubble") is True
            assert ch.sent and ch.sent[0] in corpus.BUBBLE_LINES, \
                f"冒泡没走固定语料：{ch.sent}"
            print("ok 冒泡固定语料（不走 LLM，省 token）")
        finally:
            engine.stop()  # 停掉后台回复线程
            _llm.chat = real_chat


def test_cost_blame_redline():
    """H 模块反愧疚红线：代价只呈现不归因。词表在 corpus，检测在 persona，prompt 有约束。"""
    import corpus
    import persona as _persona

    # 红线词表必须是语料（会扩充），不能散在业务模块
    assert hasattr(corpus, "COST_BLAME") and corpus.COST_BLAME
    assert _persona.has_cost_blame("你打断我，我刚想说的全忘了")
    assert _persona.has_cost_blame("都是因为你，我今天什么都没干成")
    assert _persona.has_cost_blame("又被你打断了")
    # 只说自己的状态（呈现不归因）→ 不违规
    assert not _persona.has_cost_blame("我今天读得晚了，有点困")
    assert not _persona.has_cost_blame("刚在忙，回你晚了")
    assert not _persona.has_cost_blame("没事，我正好也想歇会儿")
    # 人设 prompt 里必须有这条约束（否则模型不知道）
    assert "不归因" in _persona.COMMON_RULES and "你打断我" in _persona.COMMON_RULES
    print("ok 反愧疚措辞红线（归因命中 / 呈现不命中 / 人设有约束）")


def test_busy_signals_not_merged():
    """回归：BUSY_SIGNALS 曾漏一个逗号，Python 隐式拼接把"不要说""安静"粘成一个词。"""
    import corpus
    import guard as _guard
    import tempfile
    import db as _db

    for word in ("不要说", "别说", "安静", "在忙", "开会"):
        assert word in corpus.BUSY_SIGNALS, f"忙碌信号丢了：{word}"
    assert not any(len(w) > 6 for w in corpus.BUSY_SIGNALS), \
        f"有信号词被粘成一坨：{corpus.BUSY_SIGNALS}"

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        assert _guard.check_busy_signal("安静") is True, "'安静'没被识别成忙碌信号"
        assert _guard.check_busy_signal("不要说了") is True
        assert _db.get_kv("quiet_until"), "忙碌信号没触发静默期"
        _db.set_kv("quiet_until", "")
        assert _guard.check_busy_signal("今天天气不错") is False
        assert not _db.get_kv("quiet_until"), "普通消息不该进静默期"
        print("ok 忙碌信号词表完整（不粘词 + 安静/不要说各自生效 + 普通消息不误伤）")


def test_persona_pool_expanded():
    """人格骨架扩充到 15 张（v0.79）：抽卡池、内核、两级阅读偏好、旧卡反推四处必须同步。

    加骨架最容易漏的是 `_LABEL_TO_KEY`——漏了旧卡反推不出 archetype，
    `/persona` 会显示空内核。这张测试就是防那个。
    """
    import corpus as _corpus
    import persona_pool as _pool

    names = [g["name"] for g in _corpus.GENRES]
    assert len(_pool.POOL) >= 15, f"骨架数量不够：{len(_pool.POOL)}"
    keys = [p["key"] for p in _pool.POOL]
    labels = [p["label"] for p in _pool.POOL]
    assert len(set(keys)) == len(keys) and len(set(labels)) == len(labels), "key/label 不能撞"
    for p in _pool.POOL:
        assert p["key"] in _pool.KERNELS, f"{p['key']} 缺内核"
        assert p["reading_pref"] in names, f"{p['key']} 的一级分类不在 22 类里：{p['reading_pref']}"
        assert p.get("reading_tags"), f"{p['key']} 缺细标签"
        assert len(p.get("names") or []) >= 3, f"{p['key']} 名字池太小"
        # 每张内核都必须带「不能有的」事实卡——那是防止它借对方经历给自己捏人设的护栏
        k = _pool.KERNELS[p["key"]]
        assert any("不能有的" in (f.get("id") or "") + (f.get("fact") or "")
                   for f in k.get("facts") or []), f"{p['key']} 缺「不能有的」事实卡"

    # 反推表：每张卡都要能按 label 找回 archetype，并按 label 拿到一个合法分类
    for p in _pool.POOL:
        assert _pool._LABEL_TO_KEY.get(p["label"]) == p["key"], f"反推表漏了 {p['label']}"
        pref = _pool._LABEL_TO_PREF.get(p["label"], "")
        assert pref in names or pref in _corpus.PREF_LEGACY_MAP, \
            f"{p['label']} 的兜底偏好既不属 22 类也不是旧标签：{pref}"

    # 抽一张出来必须是完整卡（含内核 + 两级偏好）
    card = _pool.draw()
    assert card["kernel"] is card["kernel"] and card["kernel"], "抽卡必须带内核"
    assert card["reading_pref"] in names and card["reading_tags"]

    print(f"ok 人格池扩充（{len(_pool.POOL)} 张骨架 · 内核/反推表/两级偏好同步）")


def test_legacy_persona_card_compat():
    """旧人格卡（缺 quirk/reading_pref/archetype）补全：/persona 不崩、阅读标签能显示。"""
    import json
    import tempfile

    import db as _db
    import persona_pool as _pool

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        # 旧卡：只有 name/label/text/作息/bubble_mult/drawn_at，缺 quirk/reading_pref/archetype
        _db.set_kv("persona_json", json.dumps({
            "name": "小葵", "label": "阳光话痨", "text": "话痨",
            "wake_hour": 8, "sleep_hour": 23, "bubble_mult": 1.0,
            "drawn_at": "2026-10-06",
        }, ensure_ascii=False))

        card = _pool.load()
        assert card["quirk"] == "", "旧卡 quirk 应补空"
        assert card["archetype"] == "sunshine", "旧卡 archetype 应按 label 反推"
        # 旧卡 reading_pref 按 label 反推成老标签「生活随笔」，再迁移成两级（文学 + [生活随笔]）
        assert card["reading_pref"] == "文学", f"旧卡应迁移到一级分类：{card}"
        assert card["reading_tags"] == ["生活随笔"], card

        # /persona 不崩、带阅读偏好、空怪癖有兜底
        out = commands.handle("/persona")
        assert "阅读偏好" in out and "生活随笔" in out, f"/persona 缺阅读偏好：{out}"
        assert "没特别怪癖" in out, f"旧卡空怪癖应兜底：{out}"

        # 阅读偏好标签能显示（爱读X·细标签）
        import webui as _webui
        assert _webui._persona_tags(card) == ["阳光话痨", "爱读文学·生活随笔"], \
            f"旧卡补全后标签不对：{_webui._persona_tags(card)}"

        # 新卡（已含 reading_pref）不被 normalize 覆盖
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "q",
            "text": "夜猫子", "wake_hour": 10, "sleep_hour": 2, "bubble_mult": 1.2,
            "reading_pref": "科幻", "drawn_at": "2026-10-10T00:00:00",
        }, ensure_ascii=False))
        # 新卡（已含 reading_pref）不被 normalize 覆盖，只会从老标签迁移到两级
        new = _pool.load()
        assert new["reading_pref"] == "精品小说", f"新卡应迁移到一级分类：{new['reading_pref']}"
        assert new["reading_tags"] == ["科幻"], new

        print("ok 旧人格卡兼容（缺字段补全 / /persona 不崩 / 阅读标签显示 / 新卡不覆盖）")


def test_mood():
    """情绪状态（10-09 解冻）：心情由它自己的生活推，不围绕任何人转，语态不踩红线。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import config as _cfg
    import db as _db
    import life as _life
    import mood as _mood
    import persona as _persona

    tmp = Path(tempfile.mkdtemp())
    # WORK_SHAREABLE_ENABLED=False：素材要调 LLM，这里只测心情本身
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       MOOD_ENABLED=True, WORK_SHAREABLE_ENABLED=False):
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "drawn_at": "2026-10-09T00:00:00"}))

        morning = _dt(2026, 10, 9, 10, 0)
        day = _mk_day("2026-10-09", morning,
                      [int(_life.NOMINAL_SESSION_MIN * _life.speed())], warmup=10)
        first_start = _dt.fromisoformat(day["sessions"][0]["start"])

        # 工作状态 → 心情（心流=投入积极 / 忙=专注中性）
        assert _mood.mood(first_start + _td(minutes=2)) == \
            {"label": "投入", "tone": "positive"}, "心流期应是投入"
        assert _mood.mood(first_start + _td(minutes=30)) == \
            {"label": "专注", "tone": "neutral"}, "忙自己的事应是专注"

        # 还没开工（idle）→ 慵懒
        idle = _mood.mood(_dt(2026, 10, 9, 10, 0))
        assert idle == {"label": "慵懒", "tone": "neutral"}, idle

        # 精力低 → 疲惫（累是最强烈的感受，盖过 phase）
        # ⚠️ 精力是**派生**的（不能写字段造假）：把会话拉长到 300 分钟，
        # 读到 250 分钟处精力自然见底（消耗率 = COST ÷ 名义会话时长）
        day2 = _mk_day("2026-10-09", morning, [int(300 * _life.speed())], warmup=10)
        s2 = _dt.fromisoformat(day2["sessions"][0]["start"])
        tired = _mood.mood(s2 + _td(minutes=250))
        assert tired == {"label": "疲惫", "tone": "tired"}, tired

        # 忙完（off）白天 → 轻松；夜里 → 平静（时段情绪，独立于 phase）
        _mk_day("2026-10-09", morning,
                [int(_life.NOMINAL_SESSION_MIN * _life.speed())], warmup=10)
        assert _mood.mood(_dt(2026, 10, 9, 14, 0)) == {"label": "轻松", "tone": "positive"}
        assert _mood.mood(_dt(2026, 10, 9, 22, 0)) == {"label": "平静", "tone": "neutral"}

        # 语态提示：全部非空、且不踩反愧疚红线（只说自己状态）
        for label in ("投入", "专注", "慵懒", "放松", "轻松", "疲惫", "平静"):
            hint = _mood.MOOD_HINTS[label]
            assert hint, f"缺心情语态：{label}"
            assert not _persona.has_cost_blame(hint), \
                f"心情语态踩了反愧疚红线：{hint}"

        # 关掉开关 → 空心情、不注入语态
        config.MOOD_ENABLED = False
        assert _mood.mood(first_start) == {"label": "", "tone": ""}
        assert _mood.prompt_hint(first_start) == ""

        print("ok 情绪状态（心情由生活推 + 时段独立 + 精力疲惫 + 语态不踩红线）")


def test_tone_main_track():
    """语气主轨（10-10）：每轮定一个主导情绪状态（mood 主轨），cost 相处姿势从属；
    反谄媚补「分享/异议排队」；书名号 bug 修复（reading 标题裸存、work 只包一层）。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import config as _cfg
    import db as _db
    import life as _life
    import persona as _persona
    import tone as _tone

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       MOOD_ENABLED=True, WORK_ENABLED=True, WORK_SHAREABLE_ENABLED=False):
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "reading_pref": "科幻",
            "drawn_at": "2026-10-09T00:00:00"}))
        morning = _dt(2026, 10, 9, 10, 0)
        day = _mk_day("2026-10-09", morning,
                      [int(_life.NOMINAL_SESSION_MIN * _life.speed())], warmup=10)
        first_start = _dt.fromisoformat(day["sessions"][0]["start"])

        # 主轨 = mood（情绪），从属 = cost（相处姿势）：心流期 = 投入 + costly
        d = _tone.dominant(first_start + _td(minutes=2))
        assert d["main"] == "投入" and d["cost"] == "costly", d

        # voice_hint 是统一的一个块：带「此刻的我」+ 主轨词 + 松紧度（从属）
        hint = _tone.voice_hint(first_start + _td(minutes=2))
        assert "此刻的我" in hint and "投入" in hint, hint
        assert "松紧度" in hint, f"从属的相处姿势没进语气块：{hint}"
        assert not _persona.has_cost_blame(hint), f"语气主轨踩了反愧疚红线：{hint}"

        # 书名号：生活引擎只包一层（单《》，不出现《《）
        whint = _life.prompt_hint(first_start + _td(minutes=30))
        assert "《" in whint and "《《" not in whint, f"书名号没修干净：{whint}"

        # 忙完（off）白天 → 轻松；夜里 → 平静（主轨跟时段走）
        assert _tone.dominant(_dt(2026, 10, 9, 14, 0))["main"] == "轻松"
        assert _tone.dominant(_dt(2026, 10, 9, 22, 0))["main"] == "平静"

        # 睡着 → 没有主轨，不注入语气块
        _db.set_state("SLEEPING")
        assert _tone.voice_hint(_dt(2026, 10, 9, 14, 0)) == "", "睡着不该有语气块"
        _db.set_state("AWAKE")
        # 关工作系统 → 没有主轨，不注入语气块
        config.WORK_ENABLED = False
        assert _tone.dominant(first_start)["main"] == "", "关工作系统不该有主轨"
        config.WORK_ENABLED = True

        # 排队：反谄媚里「怼」和「分享」同一句只做一件
        anti = _persona.anti_sycophancy_prompt()
        assert "排队" in anti and "只做一件" in anti, "反谄媚缺「分享/异议排队」协调条款"

        print("ok 语气主轨（mood 主轨 + cost 从属 + 分享/异议排队 + 书名号修复）")


def test_persona_kernel():
    """人格内核：判断清单（好恶/立场/雷区/语言指纹）能渲染成 prompt 并注入系统位。

    核心要求（这条决定了整套改动有没有意义）：每一条都必须**具体到能被违反**——
    "活泼开朗"永远不会被违反，所以它约束不了任何一句话。
    """
    import persona as _persona
    import persona_pool as _pool

    # 1) 每张卡都有内核（v0.79 起 15 张），且没有空字段凑数
    assert len(_pool.KERNELS) == len(_pool.POOL), "每张人格卡都必须配一份内核"
    for k_name, k in _pool.KERNELS.items():
        assert k.get("core", {}).get("一句话"), f"{k_name} 缺内核一句话"
        assert k.get("core", {}).get("不是"), f"{k_name} 缺'不是什么'"
        assert len(k.get("likes") or []) >= 6, f"{k_name} 好恶太少，约束不住"
        assert len(k.get("dislikes") or []) >= 6, f"{k_name} 禁忌太少"
        assert len(k.get("stances") or {}) >= 8, f"{k_name} 立场太少，异议没来源"
        assert (k.get("boundaries") or {}).get("绝对不做"), f"{k_name} 缺雷区"
        assert k.get("voice", {}).get("禁止的句式"), f"{k_name} 缺禁止句式"
        for key in ("mirroring_budget", "topic_initiative_every"):
            assert key in (k.get("anti_sycophancy") or {}), f"{k_name} 缺反谄媚参数 {key}"
        # v0.80 起 disagreement_rate 已移除（异议改条件触发，不掷骰子），卡里不该再有
        assert "disagreement_rate" not in (k.get("anti_sycophancy") or {}), \
            f"{k_name} 还留着已移除的 disagreement_rate"

    # 2) 每条好恶都必须带强度，强度才是"被踩到会怎样"的依据
    for item in _pool.KERNELS["quiet"]["likes"]:
        assert isinstance(item.get("intensity"), int) and 1 <= item["intensity"] <= 5

    # 3) 渲染三档：full > compact（按强度过滤）> voice（只要语言指纹）
    card = _pool.draw()
    _pool.save(card)
    full = _persona.kernel_prompt("full")
    compact = _persona.kernel_prompt("compact")
    voice = _persona.kernel_prompt("voice")
    assert full and compact and voice
    assert len(full) > len(compact) > len(voice), "分档失效：full 应最长，voice 最短"
    # compact 只留高强度好恶：低强度的（如 intensity 3）不该出现
    for item in _pool.KERNELS[card["archetype"]]["likes"]:
        if item["intensity"] < 4:
            assert item["item"] not in compact, f"低强度好恶不该进 compact：{item}"
        else:
            assert item["item"] in full
    # voice 档里必须带"禁止说的句式"——这是最直接的人味过滤器
    assert "禁止说" in voice

    # 4) 反谄媚规则读了人格自己的参数（不能写死一套给所有人）
    #    v0.80：disagreement_rate 已删（异议改条件触发），改成断言条件触发的措辞
    rules = _persona.anti_sycophancy_prompt()
    assert "mirroring" not in rules  # 占位防呆：下面断言别整段删光
    assert "「表现得友好」" in rules or "表现得友好" in rules
    for must in ("镜像预算", "禁止无条件投降", "话题主权", "回复自主权"):
        assert must in rules, f"缺{must}"
    # 条件触发：没碰到内核就不抬杠，碰到了才表达不认同
    assert "碰到" in rules and "好恶" in rules, f"异议不是条件触发写法：{rules[:120]}"
    # 通用雷区（跨人格）：被明显敷衍/被说教/被要求表演热情/被替它总结。
    # v0.112：「被敷衍」加了阈值——单次"噢/额"不许当冒犯（这是它总挑刺的源头）。
    assert "被明显敷衍" in rules and "被说教" in rules and "表演热情" in rules, rules[:200]
    assert "单次" in rules and "先按善意理解" in rules, f"缺善意理解校准：{rules[:200]}"

    # 5) 聊天常识（v0.112）：先接住、不纠正用词、不把短回当冒犯、分享要具体
    sense = _persona.chat_sense_prompt()
    for must in ("先接住", "不纠正", "单次短回", "别只给一句评价", "刻薄不是有主见",
                 "同一个意象说过就放下"):
        assert must in sense, f"聊天常识缺「{must}」"
    assert "聊天常识" in _persona.static_system_prompt(), "静态 prompt 没注入聊天常识"
    mono = _persona.monologue_prompt()
    assert "大多数消息只是随口一说" in mono, "独白层缺过度敏感校准"
    assert "不值得纠正" in mono, "独白层还在允许纠正对方用词"
    utter = _persona.utterance_instruction("【我自己的话题】早上那阵光")
    assert "接不上就不提" in utter and "不点评" in utter, f"说话层缺相关性/不挑刺约束：{utter}"

    # 6) 体系化注入：system prompt 里先规则、后内核，顺序不能反
    sp = _persona.build_system_prompt("AWAKE")
    assert sp.index("五条硬规矩") < sp.index("【你是谁】"), "反谄媚必须排在人格之前"
    assert "【你真的受不了】" in sp, "system prompt 丢了核武器（禁忌清单）"
    # 早安场景不许再让模型编天气/光线（v0.112：连续多天"光爬进来"的来源）
    morning = _persona.PROACTIVE_SCENARIOS["morning"]
    assert "别编天气" in morning and "光线" in morning, morning

    # 6) 开关关掉就不注入（保留旧的纯形容词人格行为）
    config.PERSONA_KERNEL_ENABLED = False
    try:
        assert _persona.kernel_prompt("full") == ""
    finally:
        config.PERSONA_KERNEL_ENABLED = True
    print("ok 人格内核（七张卡判断清单 + 三档渲染 + 反谄媚参数化 + 注入顺序）")


def test_likes_passive_split():
    """被动底色：likes 里 passive=True 的条目不进"会主动提起"，进"底色偏好"块
    （修 bug：TA 拿"反复听旋律/小店"这类物理偏好编生活场景当主动话题）。"""
    import tempfile
    import db as _db
    import persona as _persona
    import persona_pool as _pool

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        quiet = next(p for p in _pool.POOL if p["key"] == "quiet")
        _pool.save({
            "archetype": "quiet", "label": quiet["label"], "name": "闻屿",
            "quirk": "", "text": "x", "wake_hour": quiet["wake_hour"],
            "sleep_hour": quiet["sleep_hour"], "bubble_mult": quiet["bubble_mult"],
            "reading_pref": quiet["reading_pref"], "drawn_at": "2026-10-11T00:00:00",
            "kernel": _pool.KERNELS["quiet"],
        })
        compact = _persona.kernel_prompt("compact")
        assert "【你真的喜欢】" in compact and "【你的底色偏好】" in compact, \
            "likes 没按 active/passive 分块"
        # 被动条目（反复听旋律）在"底色偏好"之后；主动条目（被当面反驳）在其之前
        assert compact.index("被当面反驳") < compact.index("【你的底色偏好】") \
            < compact.index("把同一段旋律"), "被动条目混进了'会主动提起'块"
        # full 层也要有底色块（独白需要全部依据）
        assert "【你的底色偏好】" in _persona.kernel_prompt("full")
        print("ok 被动底色（passive 条目不进'会主动提起'，进'底色偏好'）")


def test_anti_sycophancy_redlines():
    """人格红线：投降句式能被抓出来（真正要防的是"财产安全回复"）。

    注意：这里测的是**检测器准不准**，不是让它每次都改写——投降要不要拦，
    取决于当时是不是"被戳穿了"，那件事由 core 调用 redline_report 判断。
    """
    import persona as _persona
    good = ["少管我，我就是不想睡。", "这点我认，但前面那句我还是那个看法。",
            "哈，被你抓到了，尴尬。", "嗯。"]
    for g in good:
        assert not _persona.has_surrender(g), f"正常回复被误判成投降：{g}"
    bad = ["对哦，你说得对，是我考虑不周。", "……对哦。行吧，那我也早点睡。",
           "行吧，那就听你的。", "全是我的错，下次不会了。"]
    for b in bad:
        assert _persona.has_surrender(b), f"投降句式没抓到：{b}"

    # 体检口径：只有"被反驳时还秒认"才算投降，普通场景认错不算；关系进度任何时候都算
    refute = "你刚才还说熬夜没事，前后矛盾了吧"
    assert _persona.redline_report(refute, "对哦，你说得对，是我考虑不周。"), \
        "被戳穿后秒认没报出来"
    assert not _persona.redline_report("今天好累啊", "对哦，是我考虑不周。"), \
        "没被反驳时认错不该报投降（那是它自己的判断）"
    assert _persona.redline_report("随便聊聊", "不知不觉已经了解你这么多了"), \
        "关系进度跑分没报出来"
    assert _persona.redline_report("随便聊聊", "少管我，我就是不想睡。") == []
    print("ok 人格红线（投降句式检测：真人反应不误伤 / 秒认必被抓）")
