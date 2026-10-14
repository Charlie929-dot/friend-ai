"""记忆与档案（提取/衰减/长文/日记/自我记忆/事件源）（E4 拆分，v0.92）。

运行：`python run_tests.py memory`（或全量 `python run_tests.py`）。
本文件顶部必须 `from _test_common import *`——两段全局机制（临时目录收口 +
关双层生成）在此生效，见约定 #10/#11。
"""
from _test_common import *  # noqa: F401,F403


def test_stable_archive(tmp_path=None):
    """归档 V2：一个好朋友一份文件（覆盖更新）、别名不改文件名、归档日志。"""
    import tempfile
    import db as _db
    import persona_pool

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        # 朋友甲：聊过 + 人格卡
        _db.set_kv("relationship_stage", "new")
        _db.add_message("user", "你好甲")
        persona_pool.save({"name": "甲", "label": "阳光话痨", "text": "x",
                           "wake_hour": 7, "sleep_hour": 23, "bubble_mult": 1.0,
                           "quirk": "q", "drawn_at": "2026-10-06T00:00:00"})
        name1 = commands._archive_current()
        assert name1 == "甲", f"归档返回名不对：{name1}"
        archives1 = commands._archives()
        assert len(archives1) == 1
        first_at = commands._archive_display(archives1[0])[1]
        # 再归档一次：仍然只有一份（覆盖，不产生新文件）
        name1b = commands._archive_current()
        archives1b = commands._archives()
        assert len(archives1b) == 1, "重复归档产生了第二份文件"
        assert archives1b[0] == archives1[0], "归档文件名变了（应该稳定）"
        assert commands._archive_display(archives1b[0])[1] >= first_at

        # 朋友乙：换库（模拟 /new-friend 清空重来）
        # archived_at 精度到秒，跨一秒保证两人归档时刻可区分（真实使用必然间隔）
        import time as _t
        _t.sleep(1.1)
        _db.clear_all()
        _db.set_kv("relationship_stage", "new")
        _db.add_message("user", "你好乙")
        persona_pool.save({"name": "乙", "label": "老干部", "text": "x",
                           "wake_hour": 6, "sleep_hour": 22, "bubble_mult": 1.0,
                           "quirk": "q", "drawn_at": "2026-10-06T01:00:00"})
        commands._archive_current()
        assert len(commands._archives()) == 2

        # 换回甲 → 再换回来：切换不产生新快照
        r = commands.handle("/friend 1")
        assert "甲" in r, f"/friend 切换反馈不对：{r}"
        assert len(commands._archives()) == 2
        commands.handle("/friend 2")
        commands.handle("/friend 1")
        assert len(commands._archives()) == 2, "来回切换产生了重复归档"

        # /alias 只改库内记录，文件名不动
        target_before = commands._archives()[0].name
        r = commands.handle("/alias 1 老铁")
        assert "老铁" in r
        assert commands._archives()[0].name == target_before, "/alias 改了文件名"
        assert commands._archive_display(commands._archives()[0])[0] == "老铁"

        # /friends 输出带最后归档时间
        out = commands.handle("/friends")
        assert "老铁" in out and "最后归档" in out, f"/friends 输出：{out}"

        # 归档日志
        log = (tmp / "archive.log").read_text(encoding="utf-8")
        assert "归档 甲" in log and "归档 乙" in log, f"archive.log：{log}"
        print("ok 归档 V2（稳定文件/覆盖更新/别名不改名/归档日志）")


def test_memory_append_timeline():
    """追加式时间线记忆：同主题不合并全保留；原文相同才跳过；
    时间标签参与召回（相对词换算日期）；同组更新历史带出；/memories 分组展示。"""
    import tempfile
    from datetime import datetime
    import commands as _cmd
    import db as _db
    import memory as _mem

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        # prompt 新规则：绝对日期 + 追加式，不再有合并规则
        assert "YYYY-MM-DD" in _mem.EXTRACT_PROMPT
        assert "一件事只留一条" not in _mem.EXTRACT_PROMPT
        # 红线代码化（2026-10-07 用户拍板）：确定性规则移出 prompt，只留一句兜底
        assert "时序铁律" not in _mem.EXTRACT_PROMPT
        assert "这条对方还没回应" in _mem.EXTRACT_PROMPT   # 结构化标注的说明保留
        # 身份清洗（2026-10-07 用户拍板）：prompt 全项目不出现 AI 身份——TA 就是抽到的角色本人
        assert "AI" not in _mem.EXTRACT_PROMPT
        import persona as _persona
        assert "AI" not in _persona.COMMON_RULES

        # _validate：推测/元信息/承诺安全网（丢弃）+ 自称改写（不丢弃）
        assert _mem._validate("可能明天要考试", False) is None                       # 推测
        assert _mem._validate("用户没有主动告知生日", False) is None                 # 元信息
        assert _mem._validate("小徐答应告诉我吃了什么", True) is None                # 尾部未回应→丢
        assert _mem._validate("小徐答应告诉我吃了什么", False) == "小徐答应告诉我吃了什么"
        assert _mem._validate("小徐答应告诉AI吃了什么", False) == "小徐答应告诉我吃了什么"  # 改写
        assert _mem._validate("小徐对小徐（AI）说「我喜欢你」", False) == "小徐对小徐说「我喜欢你」"  # 注释自称
        assert _mem._validate("小徐的AI项目稳步推进", False) == "小徐的AI项目稳步推进"      # AI话题合法
        assert _mem._validate("小徐觉得AI很可爱", False) == "小徐觉得AI很可爱"            # 评价AI技术=合法

        # _format_convo：用户最后发言之后的"我"的消息标注未回应
        rows = [{"role": "assistant", "content": "先去吃个午饭"},
                {"role": "user", "content": "嗯嗯"},
                {"role": "assistant", "content": "吃完跟我说吃了啥"}]
        convo, tail = _mem._format_convo(rows)
        assert "我（这条对方还没回应）：吃完跟我说吃了啥" in convo
        assert "我：先去吃个午饭" in convo
        assert tail is True
        convo2, tail2 = _mem._format_convo(rows[:2])
        assert "还没回应" not in convo2 and tail2 is False

        # 1) 同主题换说法 → 两条都保留（追加，不合并）
        _db.add_memory("小徐的AI项目正在稳步推进", "事件", theme="技术")
        _mem._apply({"content": "小徐的AI项目规模很大，每天只能推进一部分",
                     "category": "事件", "theme": "技术"})
        assert _db.memory_count() == 2, "同主题新进展应追加而非合并"
        # 2) 原文完全相同 → 跳过
        _mem._apply({"content": "小徐的AI项目正在稳步推进", "category": "事件", "theme": "技术"})
        assert _db.memory_count() == 2, "完全相同的原文不应重复记"

        # 3) 记忆量超过全量阈值后，时间标签参与召回：
        #    query 说"今天下午" 应命中今天日期+下午的标签
        today = datetime.now().date().isoformat()
        _db.add_memory("小徐计划先写图像作业再弄项目", "计划",
                       time_label=f"{today} 下午", theme="学业")
        _db.add_memory("小徐打算周日去爬山", "计划", theme="出行")
        _db.add_memory("小徐爬山改到周六了", "计划", theme="出行")
        for i in range(10):  # 填充无关记忆，超过全量返回阈值（10 条）
            _db.add_memory(f"无关记忆填充第{i}号关于天气", "事件", theme="其他")
        hits = _mem.recall("我今天下午该干嘛来着", top_k=5)
        assert any("图像作业" in m["content"] for m in hits), \
            f"时间标签未参与召回：{[m['content'] for m in hits]}"

        # 4) 同组更新历史：命中旧计划时带出同组更新的那条（改期）
        hits = _mem.recall("周日去爬山的计划", top_k=5)
        contents = [m["content"] for m in hits]
        assert any("改到周六" in c for c in contents), f"未带出同组更新：{contents}"

        # 5) /memories 分组展示：同 (category, theme) 归一组，组内旧→新；
        #    每条带存储时间（记于MM-DD HH:MM），与事发时间标签是两个维度
        out = _cmd.handle("/memories")
        assert "▸ 计划·出行" in out, f"缺分组标题：{out}"
        i_old, i_new = out.index("周日去爬山"), out.index("改到周六")
        assert i_old < i_new, "组内应按时间正序（新的在后面）"
        assert "记于" in out, f"缺存储时间：{out}"
        assert f"@{today} 下午" in out, f"缺事发时间标签：{out}"
        print("ok 追加式时间线记忆（追加/相同跳过/时间标签召回/同组带出/分组展示+双时间维度）")


def test_memory_v2():
    """记忆系统 v2：历史回填（断点/历史时间锚定）+ 删除待确认 + status 列迁移。"""
    import json
    import sqlite3
    import tempfile
    import commands as _cmd
    import db as _db
    import memory as _mem

    tmp = Path(tempfile.mkdtemp())
    real_chat = _mem.llm.chat
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        try:
            # 0) status 列迁移：模拟旧库（删掉 status 列）后 init 应自动补回
            with sqlite3.connect(str(config.DB_PATH)) as c:
                c.execute("ALTER TABLE memories DROP COLUMN status")
            _db.init()
            with sqlite3.connect(str(config.DB_PATH)) as c:
                cols = [r[1] for r in c.execute("PRAGMA table_info(memories)")]
            assert "status" in cols, "旧库迁移未补 status 列"

            # 1) 历史回填：旧消息分批提取，参照时间用消息当天而非回填当天
            for i, (role, text) in enumerate([
                    ("user", "我们今天下午聊聊SVD图像压缩吧"),
                    ("assistant", "好啊，SVD 就是把矩阵分解成三个矩阵……"),
                    ("user", "懂了！你讲得比老师清楚"),
                    ("assistant", "哈哈那明天继续"),
            ]):
                _db.add_message(role, text)
            with sqlite3.connect(str(config.DB_PATH)) as c:  # 改成历史时间 10-03
                c.execute("UPDATE messages SET ts='2026-10-03T15:00:00'")
            calls = []

            def fake_chat(messages, temperature=0.7, **k):
                calls.append(messages[-1]["content"])
                return json.dumps([{"content": "我给小徐详细讲过SVD图像压缩的原理",  # 新红线：第一人称
                                    "category": "事件", "theme": "学业",
                                    "time": "2026-10-03 下午"}], ensure_ascii=False)

            _mem.llm.chat = fake_chat
            added = _mem.backfill_history(progress=lambda *a: None)
            # 断言最终状态而非精确条数：早期测试可能残留后台线程晚触发 llm.chat（竞态），
            # 会抢先 apply 同一条记忆导致 added=0，但终态一致
            assert any("【现在时间】2026-10-03" in c for c in calls), \
                f"参照时间应为消息当天：{calls[:2]}"
            mems = _db.all_memories()
            svd = [m for m in mems if "SVD" in m["content"]]
            assert svd, f"回填未产生记忆：{[m['content'] for m in mems]}"
            assert svd[0]["time_label"] == "2026-10-03 下午", \
                f"时间标签未锚定历史日期：{svd[0]['time_label']}"
            # 断点：再跑一次不重复回填
            assert _mem.backfill_history(progress=lambda *a: None) == 0, "断点失效，重复回填"

            # 2) 删除待确认：AI 提议 → 标记不真删 → recall 排除 → keepmem 恢复 → delmem 真删
            _mem.llm.chat = real_chat  # 后续不再需要 LLM
            _mem._apply({"content": "小徐被AI称为技术宅", "category": "画像"})
            _mem._apply({"content": "技术宅", "delete": True})
            pend = _db.pending_delete_memories()
            assert len(pend) == 1 and "技术宅" in pend[0]["content"], "删除提议未标记"
            assert _db.memory_count() == 2, "提议删除不应真删"
            assert all("技术宅" not in m["content"] for m in _mem.recall("小徐 技术")), \
                "待删除记忆不应参与召回"
            out = _cmd.handle("/memories")
            assert "待你确认" in out and "技术宅" in out, f"/memories 缺待确认区：{out}"
            assert "留着" in _cmd.handle(f"/keepmem {pend[0]['id']}")
            assert not _db.pending_delete_memories(), "keepmem 未恢复"
            assert "已忘掉" in _cmd.handle(f"/delmem {pend[0]['id']}")
            assert all("技术宅" not in m["content"] for m in _db.all_memories()), "delmem 未真删"
            print("ok 记忆 v2（迁移/回填+历史时间锚定/断点/删除待确认全流程）")
        finally:
            _mem.llm.chat = real_chat


def test_profile_autofill():
    """AI 自动维护 profile：写入/稳定字段冲突待确认/可变字段更新/手动字段不覆盖/证据留痕。"""
    import tempfile
    import commands as _cmd
    import db as _db
    import memory as _mem

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        ev = "对方：我最近都熬夜到凌晨\n我：原来你是夜猫子"
        # 1) 提取结果带 profile 字段 → 写进清单并锁定 + 证据留痕
        _mem._apply({"content": "小徐是夜猫子作息", "category": "画像",
                     "profile": "作息习惯：夜猫子，凌晨才睡"}, evidence=ev)
        assert _db.get_kv("profile_locked") == "1", "自动填写应锁定清单"
        assert "作息习惯：夜猫子，凌晨才睡" in _db.get_kv("profile_text"), \
            f"清单没写进去：{_db.get_kv('profile_text')!r}"
        pe = _mem.profile_evidence()
        assert "作息习惯" in pe and pe["作息习惯"].get("long_id"), "应留证据映射"
        # A 库记忆挂了证据链接（long_id → B 库原文）
        mem = [m for m in _db.all_memories() if m["content"] == "小徐是夜猫子作息"][0]
        assert mem["long_id"] == pe["作息习惯"]["long_id"], "A 库应挂 B 库证据链接"
        # /profile 展示证据标记；/profile why 取当时对话原文
        out = _cmd.handle("/profile")
        assert "证据#" in out, f"/profile 缺证据标记：{out}"
        why = _cmd.handle("/profile why 作息习惯")
        assert "夜猫子" in why, f"/profile why 缺证据原文：{why}"

        # 2) 稳定字段（作息习惯）遇到冲突新值 → 不静默覆盖，标 pending 待确认
        _mem._apply({"content": "小徐最近开始早睡了", "category": "画像",
                     "profile": "作息习惯：最近在调整，十一点前就睡"},
                    evidence="对方：我昨晚十一点就睡了")
        assert "凌晨才睡" in _db.get_kv("profile_text"), \
            f"稳定字段不应被静默覆盖：{_db.get_kv('profile_text')!r}"
        assert "十一点前就睡" not in _db.get_kv("profile_text"), "冲突值不应写入"
        pending = _mem.profile_pending()
        assert "作息习惯" in pending and "十一点前就睡" in pending["作息习惯"]["value"], \
            f"冲突应标 pending：{pending}"

        # 2b) 用户接受 pending → 覆盖旧值并转为正式证据
        assert _mem.accept_pending_profile("作息习惯") == "最近在调整，十一点前就睡"
        assert "十一点前就睡" in _db.get_kv("profile_text") \
            and "凌晨才睡" not in _db.get_kv("profile_text")
        assert "作息习惯" not in _mem.profile_pending(), "accept 后应清掉 pending"

        # 2c) 用户否决 pending：保留旧值，丢弃新说法
        _mem._apply({"content": "小徐说他要早睡", "category": "画像",
                     "profile": "作息习惯：每天九点睡"}, evidence="对方：我以后九点睡")
        assert _mem.reject_pending_profile("作息习惯") == "每天九点睡"
        assert "每天九点睡" not in _db.get_kv("profile_text"), "reject 后不应写入新值"

        # 3) 可变字段（在忙的事）→ 可直接更新（自然变动）
        _mem._apply({"content": "小徐在写图像作业", "category": "画像",
                     "profile": "在忙的事：写图像作业"})
        _mem._apply({"content": "小徐开始做项目了", "category": "画像",
                     "profile": "在忙的事：做AI朋友项目"})
        assert "做AI朋友项目" in _db.get_kv("profile_text") \
            and "写图像作业" not in _db.get_kv("profile_text"), \
            f"可变字段未更新：{_db.get_kv('profile_text')!r}"

        # 4) 用户手动加的字段 → AI 不覆盖
        _db.set_kv("profile_text", _db.get_kv("profile_text") + "\n称呼：小徐")
        _mem._apply({"content": "小徐让大家叫他徐哥", "category": "画像",
                     "profile": "称呼：徐哥"})
        assert "称呼：小徐" in _db.get_kv("profile_text"), \
            f"手动字段被覆盖：{_db.get_kv('profile_text')!r}"

        # 5) 无"字段：值"结构的 profile 不写入
        _mem._apply_profile("没有结构的一整句话")
        assert "没有结构" not in _db.get_kv("profile_text")
        # 6) 记忆本身照常入库（profile 是附加动作）
        assert any(m["content"] == "小徐是夜猫子作息" for m in _db.all_memories())
        print("ok AI 自动维护 profile（写入/稳定冲突待确认/可变更新/不覆盖手动/证据留痕）")


def test_identity_recall():
    """身份询问（"记得XX吗/XX是谁"）时，人际/介绍类记忆应优先于琐事。"""
    import tempfile
    import db as _db
    import memory as _mem

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        _db.add_memory("小徐拉了一个群，把初遇朋友和小葵拉进来互相认识",
                       "人际", person="小葵、初遇朋友")
        for i in range(12):
            _db.add_memory(f"小葵琐事{i}号：拿快递晾衣服", "事件", person="小葵")

        hits = _mem.recall("你还记得小葵吗", top_k=3)
        contents = [m["content"] for m in hits]
        assert any("拉进来互相认识" in c for c in contents), \
            f"身份记忆没进 top：{contents}"
        assert "拉进来互相认识" in "".join(contents[:2]), \
            f"身份记忆未排在琐事前面：{contents}"
        print("ok 身份询问召回（人际/介绍类优先于琐事）")


def test_semantic_recall():
    """语义召回（S4 首位）：字面对不上但同义（熬夜 ↔ 几点睡）也能召回来。

    治「几点睡召不回熬夜」——纯字面 bigram 对不上就是零分；本地 bge-small-zh-v1.5
    把"熬夜""凌晨才睡""几点睡"归到同一语义，向量相似度补上字面的空档。
    embedding 模型/依赖不可用时优雅跳过（语义召回是增强、不是必须）。
    """
    import tempfile
    import db as _db
    import memory as _mem
    import embeddings as _emb

    if not _emb.available():
        print("skip 语义召回（embedding 模型/依赖不可用）")
        return

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        # 目标记忆：与 query 语义相关、但字面 bigram 几乎不重叠
        _db.add_memory("小徐平时熬夜，凌晨一两点才睡", "画像", person="小徐")
        # 填充 12 条无关记忆，凑过「≤10 条直接全量返回」的分支，逼它走打分
        for i in range(12):
            _db.add_memory(f"小徐周末喜欢去公园散步{i}号", "偏好", person="小徐")

        hits = _mem.recall("小徐一般几点睡", top_k=3)
        contents = [m["content"] for m in hits]
        assert any("熬夜" in c for c in contents), \
            f"语义召回应带回「熬夜」记忆：{contents}"
        print("ok 语义召回（熬夜 ↔ 几点睡）")


def test_memory_decay():
    """艾宾浩斯遗忘：到期压短一级；被想起越多撑得越久；够多次永久；压够级数彻底忘掉。

    保真优先：模型压出来的比原文长/踩红线 → 本次跳过，绝不把记忆改味。
    """
    import tempfile
    import datetime as _dt
    import db as _db
    import memory as _mem

    tmp = Path(tempfile.mkdtemp())
    old = _dt.datetime.now() - _dt.timedelta(days=100)
    # 衰减参数取小值好测
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       MEMORY_DECAY_IDLE_DAYS=30, MEMORY_DECAY_RECALL_BONUS=0.5,
                       MEMORY_DECAY_MAX_LEVEL=3, MEMORY_DECAY_PIN_RECALLS=8):
        # 1) 很久没被想起 → 进待淡忘列表
        mid = _db.add_memory("小徐说过他小时候养过一只叫团子的猫，后来送人了还要过很久", "事件")
        _db.add_memory("小徐喜欢吃学校门口那家麻辣烫，尤其加麻酱", "偏好")
        _db.touch_memory(mid)          # 想起过一次 → 有效期 30×1.5=45 天
        with _db._conn() as c:
            c.execute("UPDATE memories SET recalled_at=? WHERE id=?",
                      (old.isoformat(timespec="seconds"), mid))
        # 第二条：从未被想起（用 created_at）
        with _db._conn() as c:
            c.execute("UPDATE memories SET created_at=?, recalled_at='' WHERE content LIKE '小徐喜欢吃%'",
                      (old.isoformat(timespec="seconds"),))
        due = _mem.forget_preview()
        assert len(due) == 2, f"两条都过期未想起：{due}"

        # 2) 有效期随召回次数变长：想起 8 次以上 → 永久，不再待淡忘
        many = _db.add_memory("小徐是夜猫子，经常凌晨一两点才睡", "画像")
        for _ in range(9):
            _db.touch_memory(many)
        with _db._conn() as c:
            c.execute("UPDATE memories SET recalled_at=? WHERE id=?",
                      (old.isoformat(timespec="seconds"), many))
        assert _mem._pin_long_remembered() == 1, "想起够多次应升为永久"
        assert many not in [m["id"] for m in _mem.forget_preview()], "永久记忆不该待淡忘"

        # 3) 压短一级（模型返回更短且合规）
        real_chat = _mem.llm.chat
        _mem.llm.chat = lambda *a, **k: ('[{"id": %d, "content": "小徐小时候养过猫团子"},'
                                         '{"id": %d, "content": "小徐爱吃门口那家麻辣烫"}]'
                                         % (mid, mid + 1))
        try:
            out = _mem.forget_pass()
        finally:
            _mem.llm.chat = real_chat
        assert "淡忘 2 条" in out, f"应淡忘两条：{out}"
        m1 = [x for x in _db.all_memories(limit=10) if x["id"] == mid][0]
        assert m1["decay_level"] == 1, f"等级应变 1：{m1}"
        assert m1["content"] == "小徐小时候养过猫团子", f"内容应被压短：{m1['content']}"

        # 4) 保真：模型压得比原文长 → 跳过不改
        before = m1["content"]
        _mem.llm.chat = lambda *a, **k: ('[{"id": %d, "content": "%s并且他还说过很多很多很多别的事情哦"}]'
                                         % (mid, before))
        try:
            out = _mem.forget_pass()
        finally:
            _mem.llm.chat = real_chat
        assert "没动" in out, f"压长了应跳过：{out}"
        m1b = [x for x in _db.all_memories(limit=10) if x["id"] == mid][0]
        assert m1b["content"] == before, "跳过时内容不能变"

        # 5) 压到上限 → 彻底遗忘（连带 B 库长文一起删）
        long_id = _db.add_long_memory("长文摘要", "全文" * 100)
        m2 = _db.add_memory("小徐讲过一段很长的往事细节", "事件", long_id=long_id)
        with _db._conn() as c:
            c.execute("UPDATE memories SET created_at=?, recalled_at='' WHERE id=?",
                      (old.isoformat(timespec="seconds"), m2))
        _db.set_memory_decay(m2, config.MEMORY_DECAY_MAX_LEVEL - 1, "小徐讲过一段往事")
        _mem.llm.chat = lambda *a, **k: '[{"id": %d, "content": "小徐讲过往事"}]' % m2
        try:
            out = _mem.forget_pass()
        finally:
            _mem.llm.chat = real_chat
        assert "彻底忘了 1 条" in out, f"到上限应删除：{out}"
        assert _db.find_memory_by_keyword("小徐讲过往事") is None, "A 库行应删掉"
        assert _db.get_long_memory(long_id) is None, "挂的 B 库长文应一起删"
        print("ok 艾宾浩斯记忆衰减（压短/永久/保真跳过/彻底遗忘）")


def test_long_memory():
    """A/B 两级库：长文存 B 库（全文+摘要）、A 库挂 long_id 伪链接；追问细节时展开。"""
    import tempfile
    import db as _db
    import memory as _mem
    import context as _ctx

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):

        # 1) 长文：_apply 分流——摘要进 A 库、全文进 B 库、long_id 关联
        full_text = "小徐从头讲了奇异值分解怎么压缩图像，先把图切成 8x8 小块，再对每块做分解，" * 8
        ok = _mem._apply({"content": "小徐给我详细讲了SVD图像压缩的完整思路",
                          "category": "事件", "long": True, "full": full_text,
                          "theme": "技术"}, assistant_tail=False)
        assert ok, "长文落库失败"
        assert _db.memory_count() == 1, "A 库应有 1 条摘要记忆"
        assert _db.long_memory_count() == 1, "B 库应有 1 条长文"
        m = _db.all_memories(limit=1)[0]
        assert m["long_id"] > 0, f"A 库记忆应带 long_id 伪链接：{m}"
        long = _db.get_long_memory(m["long_id"])
        assert long and long["content"] == full_text, "B 库全文应与原文一致"

        # 2) 短记忆照旧：无 long_id
        _db.add_memory("小徐爱吃辣", "偏好")
        short = [x for x in _db.all_memories(limit=5) if x["content"] == "小徐爱吃辣"][0]
        assert short["long_id"] == 0, "短记忆不应挂 long_id"

        # 3) 召回默认不展开（只给摘要，不附全文）
        hits = _mem.recall("SVD图像压缩", top_k=3)
        hit = [h for h in hits if h.get("long_id")][0]
        assert "long_content" not in hit, "默认召回不该展开全文"

        # 4) 追问细节时展开全文
        hits2 = _mem.recall("你详细讲讲SVD那事", top_k=3, expand_long=True)
        hit2 = [h for h in hits2 if h.get("long_id")][0]
        assert "long_content" in hit2, "追问细节应展开全文"
        assert hit2["long_content"] == full_text, "展开的全文应与原文一致"

        # 5) expand_long_memory 接口
        assert _mem.expand_long_memory(m) == full_text
        assert _mem.expand_long_memory(short) is None, "短记忆展开应返回 None"

        # 6) 渲染：_memory_line 带长文标记；build_chat_messages 追问细节注入全文
        line = _ctx._memory_line(m)
        assert "有长文" in line, f"长记忆缺展开提示：{line}"
        _db.add_message("user", "你详细讲讲SVD那事")
        msgs = _ctx.build_chat_messages("AWAKE", ["你详细讲讲SVD那事"])
        joined = "\n".join(x["content"] for x in msgs)
        assert "详情全文" in joined, "追问细节时上下文应含长文全文"
        assert "先把图切成" in joined, "全文应真正注入上下文"
        print("ok A/B 两级库（长文落库 + 伪链接 + 追问展开 + 渲染标记）")


def test_memory_tool_calling():
    """回忆/追问信号下 AI 主动调记忆检索工具（function calling），普通聊天走快速路径。"""
    import tempfile
    import db as _db
    import llm as _llm
    import memory as _mem
    from core import Engine

    class DummyChannel:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)

    tmp = Path(tempfile.mkdtemp())
    real_ct = _llm.chat_with_tools
    real_chat = _llm.chat
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       MEMORY_TOOLS_ENABLED=True):  # 本测试显式开启工具调用
        try:
            _db.add_memory("小徐拉了一个群，把初遇朋友和小葵拉进来互相认识", "人际", person="小葵")
            ch = DummyChannel()
            engine = Engine(ch)

            # 1) 信号判断：含"记得"→ 启用工具；普通闲聊 → 不启用
            assert _mem.wants_memory_search("你还记得小葵吗") is True
            assert _mem.wants_memory_search("今天天气不错") is False

            # 2) 回忆信号下，_generate_reply_text 走 tool loop（mock chat_with_tools）
            calls = []

            def fake_ct(messages, tools, temperature=0.8):
                if not any(m.get("role") == "tool" for m in messages):
                    calls.append("tool_round")
                    return {"content": "", "tool_calls": [
                        {"id": "call_1", "type": "function",
                         "function": {"name": "search_memories",
                                      "arguments": '{"query": "小葵"}'}}
                    ]}
                calls.append("final_round")
                return {"content": "哦对，小葵就是群里拉进来认识的那位", "tool_calls": []}

            _llm.chat_with_tools = fake_ct
            reply = engine._generate_reply_text(
                [{"role": "system", "content": "人设"},
                 {"role": "user", "content": "你还记得小葵吗"}],
                ["你还记得小葵吗"])
            assert "小葵" in reply and "拉进来" in reply, f"工具结果没被 AI 采用：{reply}"
            assert calls == ["tool_round", "final_round"], f"工具循环轮次不对：{calls}"

            # 3) 普通聊天（无回忆信号）→ 走 llm.chat，不碰 chat_with_tools
            _llm.chat = lambda *a, **k: "普通回复"
            _llm.chat_with_tools = lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("普通聊天不该调工具"))
            assert engine._generate_reply_text([{"role": "user", "content": "在吗"}],
                                               ["在吗"]) == "普通回复"

            # 4) 开关关闭 → 即使有回忆信号也走 chat
            config.MEMORY_TOOLS_ENABLED = False
            _llm.chat = lambda *a, **k: "普通回复2"
            assert engine._generate_reply_text([{"role": "user", "content": "你记得吗"}],
                                               ["你记得吗"]) == "普通回复2"
            config.MEMORY_TOOLS_ENABLED = True

            print("ok 记忆工具调用（回忆信号分流 + tool loop 回填 + 普通聊天快速路径）")
        finally:
            _llm.chat_with_tools = real_ct
            _llm.chat = real_chat


def test_diary_daily_review():
    """P2 日记系统：每日回顾复盘 + 写日记 + 补记/修正记忆 + 幂等 + 命令。"""
    import tempfile
    import db as _db
    import diary as _diary

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        # 让 add_message 的时间戳落在"生活日"上：否则跨午夜（00:00~WAKE_HOUR）时
        # add_message 按墙钟写进"明天"，而 daily_review 按生活日读昨天 → 读到空，
        # 测试在深夜会误挂（"今天还没怎么聊"）。
        from datetime import datetime as _dt
        day = _diary._life_day_str()

        class _Clock:
            @classmethod
            def now(cls):
                return _dt.fromisoformat(day + "T12:00:00")

        real_dt = _db.datetime
        _db.datetime = _Clock
        try:
            _db.add_message("user", "今天好累啊")
            _db.add_message("assistant", "辛苦啦，早点休息")
        finally:
            _db.datetime = real_dt
        _db.add_memory("小徐在准备考研", "事件")

        real_chat = _diary.llm.chat
        _diary.llm.chat = lambda *a, **k: (
            '{"reflection": "今天有点没接住你的累",'
            '"diary": "今天小徐说好累，我让他早点休息。有点心疼。",'
            '"mood": "心疼",'
            '"memory_add": ["小徐今天很累"],'
            '"memory_delete": ["考研"]}')
        try:
            out = _diary.daily_review(force=True)
            assert "日记写好啦" in out, f"返回：{out}"
            entry = _db.get_diary(day)
            assert entry and "心疼" in entry["content"], f"日记没落库：{entry}"
            assert "没接住" in entry["reflection"], f"复盘没落库：{entry}"
            assert entry["mood"] == "心疼"
            assert _db.find_memory_by_keyword("小徐今天很累"), "memory_add 没补记"
            pend = _db.pending_delete_memories()
            assert any("考研" in m["content"] for m in pend), "memory_delete 没标记待确认"
        finally:
            _diary.llm.chat = real_chat

        # 幂等：第二次不重写（也不该再调 LLM）
        _diary.llm.chat = lambda *a, **k: "第二次不该调用"
        out2 = _diary.daily_review()
        assert "已经写过" in out2, f"应该跳过：{out2}"
        _diary.llm.chat = real_chat

        # /diary /diaries 命令
        assert "心疼" in commands.handle("/diary"), "/diary 没显示日记"
        assert day in commands.handle("/diaries"), "/diaries 没列出日期"
        print("ok 日记系统（复盘/写日记/补记/提议删/幂等/命令）")


def test_diary_purge_reviewed_messages():
    """回顾线彻底删除：PURGE_REVIEWED_MESSAGES=on 时复盘后清掉当日原始对话。"""
    import tempfile
    import db as _db
    import diary as _diary

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       PURGE_REVIEWED_MESSAGES=True):
        # 同 test_diary_daily_review：让消息时间戳落在"生活日"，跨午夜不误挂
        from datetime import datetime as _dt
        day = _diary._life_day_str()

        class _Clock:
            @classmethod
            def now(cls):
                # 用零点而非中午：purge 的 cutoff 用真实墙钟 now()（未 mock），
                # 若 mock 成"当天 12:00"而真实时间在上午，12:00 > now → 删不掉、误挂。
                # 零点一定早于当天任意真实时刻，保证"回顾线删干净"断言稳定。
                return _dt.fromisoformat(day + "T00:00:00")

        real_dt = _db.datetime
        _db.datetime = _Clock
        try:
            _db.add_message("user", "今天聊点什么")
            _db.add_message("assistant", "随便聊呀")
        finally:
            _db.datetime = real_dt
        assert len(_db.today_messages(day)) == 2
        _diary.llm.chat = lambda *a, **k: '{"diary": "聊了聊", "memory_add": [], "memory_delete": []}'
        _diary.daily_review(force=True)
        assert len(_db.today_messages(day)) == 0, "回顾线没删干净"
        assert _db.get_diary(_diary._life_day_str()) is not None, "日记应保留"
        print("ok 回顾线彻底删除（PURGE_REVIEWED_MESSAGES=on）")


def test_random_memory_about_user():
    """random_memory 只抽关于用户的记忆（实测 #26：把别人的事安到对方头上）。"""
    import json
    import tempfile

    import db as _db
    import memory as _mem

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        # 当前朋友对用户的称呼是"小徐"（profile 清单）
        _db.set_kv("profile_locked", "1")
        _db.set_kv("profile_text", "称呼：小徐")
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "drawn_at": "2026-10-09T00:00:00"}))
        _db.add_memory("小徐在写图像作业", category="事件", person="小徐")
        _db.add_memory("小徐爱喝奶茶", category="画像", person="对方")
        _db.add_memory("想收拾桌子只削了个苹果", category="事件", person="小葵")  # 自己的事
        _db.add_memory("阿燃周末要去爬山", category="事件", person="阿燃")        # 别人的事

        picked = set()
        for _ in range(40):
            m = _mem.random_memory()
            assert m, "库里明明有关于用户的记忆却抽不到"
            assert m["person"] in ("小徐", "对方"), \
                f"抽到了不是关于用户的记忆：{m['person']} {m['content']}"
            picked.add(m["content"])
        assert "小徐在写图像作业" in picked and "小徐爱喝奶茶" in picked, \
            "两类关于用户的记忆都该抽得到"
        print("ok 主动话题弹药（random_memory 只抽关于用户的，别人的事不安头上）")


def test_self_memory():
    """自我记忆：记得"我"说过什么/答应过什么；一致性检查有弹药；且不被遗忘吞掉。

    没有这一层，它每次对话都是第一次——立场前后矛盾却不自知，是最像 AI 的地方。
    """
    import tempfile
    import datetime as _dt
    import db as _db
    import memory as _mem
    import selfmem as _self

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        # 1) 写入按类型分组，同内容幂等（同一句立场不重复堆）
        a = _self.add("self_stance", "熬夜：熬夜不是坏习惯，那是唯一属于自己的时间")
        b = _self.add("self_stance", "熬夜：熬夜不是坏习惯，那是唯一属于自己的时间")
        assert a and b and a == b, f"同内容应幂等，却新建了一条：{a} vs {b}"
        _self.add("self_promise", "答应过今晚陪对方熬到零点")
        _self.add("self_mistake", "说在忙读书实际说成在读文献，被当场戳穿（嘴硬了）")
        _self.add("self_topic", "想聊海底两万里那句'大海不属于暴君'")
        assert len(_self.recent()) == 4, f"自我记忆条数不对：{_self.recent()}"

        # 2) 不会被当成"关于对方的事"套进主动话题（person 填的是它自己）
        uname = persona.user_name()
        about_user = [m for m in _self.recent() if (m.get("person") or "") in ("对方", "TA")
                      or (uname and uname in (m.get("person") or ""))]
        assert not about_user, f"自我记忆被误判成关于对方的记忆：{about_user}"

        # 3) 一致性检查：相关查询能把"我说过什么"召回来
        mems = _self.recall_self("熬夜到底好不好")
        assert any("熬夜" in m["content"] for m in mems), f"召回不到自己的立场：{mems}"
        hint = _self.prompt_hint("熬夜到底好不好")
        assert "关于我自己的记忆" in hint and "别前后矛盾" in hint

        # 4) 自我记忆不参与艾宾浩斯遗忘——忘了就是人格断裂
        old = _dt.datetime.now() - _dt.timedelta(days=200)
        with _db._conn() as c:
            c.execute("UPDATE memories SET created_at=?, recalled_at='' WHERE tags LIKE 'self_%'",
                      (old.isoformat(timespec="seconds"),))
        due = _mem.forget_preview()
        assert all(not str(m.get("tags", "")).startswith("self_") for m in due), \
            f"自我记忆不该进遗忘候选：{due}"

        # 5) 内部状态持久化（跨会话延续的最基础一层）
        _self.set_state({"mood": 2, "energy": 2, "warmth": 5, "patience": 7, "why": "很累"})
        assert _self.state()["mood"] == 2, f"状态没存住：{_self.state()}"
        # 6) 复盘：mock LLM 产出结构化自我备忘录 → 写回自我记忆
        import llm as _llm
        real_chat = _llm.chat
        _llm.chat = lambda *a, **k: (
            '{"stances": [{"topic": "熬夜", "what_i_said": "熬夜是唯一属于自己的时间", '
            '"intensity": 4, "changed_from_previous": false, "note": ""}],'
            '"promises": [{"what": "答应今晚陪对方熬到零点", "redeemed": false, "note": "今晚零点"}],'
            '"mistakes": [], "my_topics": [{"topic": "想聊深海的自由", "why_missed": "岔开了"}],'
            '"relation": {"note": "今天聊得挺深，感觉近了一点", "warmth_delta": 1, '
            '"trigger": "一起熬到很晚"},'
            '"state": {"mood": 7, "energy": 5, "warmth": 6, "patience": 7, "why": "聊得开心"},'
            '"self_observation": "最像自己的是坚持了熬夜的立场"}')
        try:
            note = _self.review("对方：你不该熬夜\n岑屿：我就是不想睡")
        finally:
            _llm.chat = real_chat
        assert "立场1" in note and "承诺1" in note, f"复盘结果不对：{note}"
        # 状态要更新，且向人格中位值回归一点（不能几天就漂到极端回不来）
        after = _self.state()["mood"]
        assert 6.5 < after < 7, f"状态应更新并向中位(6)回归一点，不该全盘照收 7：{after}"

        # 7) 给人看：/selfmem 有输出
        out = _self.dump()
        assert "立场" in out and "承诺" in out
    print("ok 自我记忆（按类型写入/幂等/一致性检查/抗遗忘/状态延续/复盘写回）")


def test_unified_event_source():
    """统一事件源（§5 拍板 #6）：用户消息进意识流 + 待办素材 id 打勾 + 承诺到点进意识流。"""
    import json
    import tempfile
    from datetime import datetime as _dt

    import config as _cfg
    import db as _db
    import stream as _stream
    import todos as _todos
    import life as _life

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       TODOS_ENABLED=True):
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "reading_pref": "科幻",
            "drawn_at": "2026-10-09T00:00:00"}))

        # 1) 待办素材 id 化：add 返回 id → mark_done_by_id 按 id 打勾
        tid = _todos.add("读到雾都孤儿里的雾", source="reading")
        assert tid > 0
        assert _todos.mark_done_by_id(tid) == "读到雾都孤儿里的雾", "按 id 打勾应命中"
        assert _todos.mark_done_by_id(tid) is None, "已打勾不该重复"
        assert _todos.mark_done_by_id(9999) is None, "不存在的 id 返回 None"
        assert len(_todos.pending()) == 0

        # 2) 素材条目带 todo_id：take_shareable_item 返回 (text, todo_id)
        tid2 = _todos.add("又想到一句", source="reading")
        _db.set_kv("work_shareables", json.dumps(
            [{"day": "2026-10-09", "seq": 1, "text": "又想到一句", "used": 0,
              "todo_id": tid2}]))
        text, got_id = _life.take_shareable_item()
        assert text == "又想到一句" and got_id == tid2, (text, got_id)

        # 3) 承诺到点 → 意识流注入条目（events 统一事件源）
        import events as _ev
        _db.set_kv("care_events", json.dumps([{
            "what": "提醒睡觉",
            "when": (_dt.now().strftime("%Y-%m-%d %H:%M")),
            "kind": "提醒", "status": "pending",
            "created_at": _dt.now().isoformat(timespec="seconds"),
        }], ensure_ascii=False))
        # 用 -5min 的到点事件确保 _due 命中
        _db.set_kv("care_events", json.dumps([{
            "what": "提醒睡觉",
            "when": (_dt.now().replace(microsecond=0)).strftime("%Y-%m-%d %H:%M"),
            "kind": "提醒", "status": "pending",
            "created_at": _dt.now().isoformat(timespec="seconds"),
        }], ensure_ascii=False))
        calls = []
        engine = types.SimpleNamespace(
            care_remind=lambda ev, missed=False: (calls.append(ev) or True))
        _ev.tick(engine)
        assert len(calls) == 1
        care_lines = [r["text"] for r in _stream.recent(20) if r["kind"] == "care"]
        assert any("提醒睡觉" in t for t in care_lines), \
            f"承诺到点应往意识流注入条目：{care_lines}"

        print("ok 统一事件源（用户消息进意识流 + 待办 id 打勾 + 承诺到点进意识流）")
