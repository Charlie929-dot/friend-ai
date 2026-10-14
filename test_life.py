"""生活引擎（排班/离线/待办/打断成本/素材分享/关怀事件）（E4 拆分，v0.92）。

运行：`python run_tests.py life`（或全量 `python run_tests.py`）。
本文件顶部必须 `from _test_common import *`——两段全局机制（临时目录收口 +
关双层生成）在此生效，见约定 #10/#11。
"""
from _test_common import *  # noqa: F401,F403


def test_life_engine():
    """生活引擎（v0.89 §3）：唯一时钟排班跟人格作息、章节会话制、心流期、
    进度/经验/精力全派生、跨天重排、素材不重复取。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import db as _db
    import life as _life
    import persona as _persona

    tmp = Path(tempfile.mkdtemp())
    # WORK_SHAREABLE_ENABLED=False：章节结算要调 LLM，这里只测机制本身
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       BOOKS_DIR=tmp / "books", WORK_SHAREABLE_ENABLED=False):
        # 夜猫子人格：10 点起、凌晨 2 点睡（作息必须跟着人格走）
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "drawn_at": "2026-10-09T00:00:00"}))
        _mk_book(config.BOOKS_DIR, n_ch=6)

        morning = _dt(2026, 10, 9, 10, 0)
        # 预置今天的起床点（锚点前 5 分钟）：新逻辑"醒来才排班"——不预置的话
        # 赖床浮动可能让 10:00 整还没"醒"，排不出班（测试要与随机浮动解耦）
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_day": "2026-10-09", "wake_at": "2026-10-09T09:55:00"}))
        day = _life.today(morning)
        units = day["sessions"]
        assert units, "今天没排出会话"
        first_start = _dt.fromisoformat(units[0]["start"])
        assert first_start.hour >= 10, f"夜猫子 10 点才起，排班却从 {first_start} 开始"
        assert _dt.fromisoformat(units[-1]["end"]) <= _dt(2026, 10, 10, 2, 0), \
            f"排班越过了睡觉时间：{units[-1]['end']}"
        # 同一天幂等：再读一次不能换计划（否则它手头的事会一直乱跳）
        assert _life.today(morning)["sessions"] == units, "同一天重排出了新计划"

        # 章节边界 = 节奏：一章一段（每章 40min 名义时长，够长不合并）
        assert len(units) == 6, f"6 章该排 6 段会话：{len(units)}"
        # 睡前是唯一硬边界：没有"下班铃"，最后一段结束 = 全书圈定的章节读完
        assert day["plan"]["total_chars"] > 0 and not day["plan"]["done"]

        # 心流期 = 会话前 WORK_FLOW_RATIO 比例（这段打断代价最高）
        flow = _life.status(first_start + _td(minutes=2))
        assert flow["phase"] == "flow" and flow["flow"], f"刚开工应是心流期：{flow}"
        working = _life.status(first_start + _td(minutes=30))
        assert working["phase"] == "working" and not working["flow"], f"过了心流期：{working}"

        # HUD 与 prompt 注入（要趁它还在忙的时候测：状态是派生的但依赖 now，
        # 一旦过了所有会话窗口就回不到心流期）
        hud = _life.hud(first_start + _td(minutes=2))
        assert "刚读进去" in hud and "精力" in hud and "经验" in hud, \
            f"HUD 该带数字三维：{hud}"
        hint = _life.prompt_hint(first_start + _td(minutes=30))
        assert "我在读" in hint and "《" in hint, f"忙的时候没注入手头的事：{hint}"
        assert "今天圈了" in hint and "读完" in hint, f"缺章节坐标：{hint}"
        # 心流期也带书名（说的是"脑子全在这章上"，措辞不同但同样有具体书）
        assert "《测试书》" in _life.prompt_hint(first_start + _td(minutes=2))
        # §5.1 事实句：prompt 里**不念数字账本**（没有"第 3/6 件""攒了 60 块"）
        assert "件" not in hint and "块" not in hint and "%" not in hint, \
            f"prompt 里念了数字账本（数字该给 HUD）：{hint}"
        # 红线：工作状态注入自己不能踩「代价归因」（它只说自己的状态）
        for tpl in _life._FACT_TEMPLATES.values():
            filled = tpl.format(book="测试书", ch="第一章", total=6, done=2, cur=3)
            assert not _persona.has_cost_blame(filled), f"工作提示踩了反愧疚红线：{tpl}"

        # 进度 / 经验 / 精力全派生（不落库）：只跟着"读到哪"走，单调涨
        p1 = _life.status(first_start + _td(minutes=10))["progress"]
        p2 = _life.status(first_start + _td(minutes=30))["progress"]
        assert 0 < p1 < p2 < 100, f"进度该随时间单调涨：{p1} → {p2}"
        mid = _life.status(first_start + _td(minutes=30))
        assert mid["exp"] == mid["progress"], f"经验 = 进度百分比：{mid}"
        assert mid["energy"] < config.WORK_ENERGY_MAX, f"读了 30 分钟该掉精力：{mid}"
        assert mid["chapter"] == 1 and mid["total_chapters"] == 6, f"章节坐标不对：{mid}"

        # 惰性补账：电脑睡了一觉错过好几段 → 醒来 progress 直接到 100%（章节照读）
        late = _life.status(_dt(2026, 10, 10, 1, 0))   # 夜猫子的"今天深夜"
        assert late["progress"] == 100, f"错过的段落没补账：{late}"
        assert late["phase"] == "off", f"活都干完了还在忙：{late}"

        # 新的一天（睡醒）→ 重新排班、结算计数归零
        st = _life.today(_dt(2026, 10, 10, 12, 0))
        assert st["day"] == "2026-10-10" and st["settled_idx"] == 0, f"跨天没重排：{st}"

        # 素材池：取过的不再取（一条素材只分享一次）
        _db.set_kv("work_shareables", json.dumps(
            [{"day": "2026-10-09", "seq": 1, "text": "刚发现楼下那只猫会自己开门", "used": 0}]))
        assert _life.take_shareable() == "刚发现楼下那只猫会自己开门"
        assert _life.take_shareable() == "", "同一条素材被取了两次"
        print("ok 生活引擎（排班跟人格作息 + 章节会话制 + 心流期 + 派生投影 + 跨天重排 + 素材不重复）")


def test_lazy_life_plan():
    """醒来才排班（实测 #26）：没醒 sessions 空、进度条不涨；醒了从实际起床点后排；
    起得太晚可用时间变短 → 读不完（最后一段截断在睡前），而不是把班排进被窝。"""
    import json
    import tempfile
    from datetime import datetime as _dt

    import db as _db
    import life as _life

    tmp = Path(tempfile.mkdtemp())
    # WORK_SHAREABLE_ENABLED=False：章节结算要调 LLM，这里只测机制本身
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       BOOKS_DIR=tmp / "books", WORK_SHAREABLE_ENABLED=False):
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "drawn_at": "2026-10-09T00:00:00"}))
        _mk_book(config.BOOKS_DIR, n_ch=12)

        # 今天睡懒觉：12 点才醒（锚点 10 点）
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_days": {"2026-10-09": "2026-10-09T12:00:00"}}))
        now = _dt(2026, 10, 9, 10, 30)          # 锚点已过但还没醒
        assert _life.today(now)["sessions"] == [], "人没醒就排了班（班不能排进被窝）"
        st = _life.status(now)
        assert st["phase"] == "sleep", f"人没醒不该有自己的生活：{st}"
        assert st["progress"] == 0 and st["exp"] == 0, f"人没醒进度条不该涨：{st}"
        assert _life.hud(now) == "", "没醒不该显示进度条"

        # 醒了（12:30）：排班，起点在实际起床点之后（磨蹭另算）
        now2 = _dt(2026, 10, 9, 12, 30)
        units = _life.today(now2)["sessions"]
        assert units, "醒了却没排班"
        first_start = _dt.fromisoformat(units[0]["start"])
        assert first_start >= _dt(2026, 10, 9, 12, 0), \
            f"排班起点早于实际起床点（排进被窝了）：{first_start}"
        assert _dt.fromisoformat(units[-1]["end"]) <= _dt(2026, 10, 10, 2, 0), \
            "排班越过了睡觉时间"
        assert _life.today(now2)["sessions"] == units, "同一天重排出新计划"

        # 起得太晚（20:00 才醒）→ 睡前是唯一硬边界 → 读不完的那段截断（partial），
        # 不追进度：剩下的章节明天按字数重圈，不是加班补回来
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_days": {"2026-10-09": "2026-10-09T12:00:00",
                           "2026-10-10": "2026-10-10T20:00:00"}}))
        day2 = _life.today(_dt(2026, 10, 10, 20, 30))
        units2, planned2 = day2["sessions"], day2["plan"]["chapters"]
        assert units2, "醒了却没排班"
        assert _dt.fromisoformat(units2[0]["start"]) >= _dt(2026, 10, 10, 20, 0), \
            "晚起的班也不该排进被窝"
        assert _dt.fromisoformat(units2[-1]["end"]) <= _dt(2026, 10, 11, 2, 0), \
            "排班越过了睡觉时间"
        # 读不完不加班补：睡前列不下的章节要么干脆不排、要么末段截断在睡前
        # （两种都正常：取决于最后一段是"整段放不下"还是"放到一半撞上睡前"）
        covered = sum(len(s["chapters"]) for s in units2)
        assert covered < len(planned2) or units2[-1]["partial"], \
            f"起太晚该读不完（圈了 {len(planned2)} 章，排进去 {covered} 章且末段没截断）"
        # 对照：正常起床（12:00）时圈定的章节全排得下
        assert len(units) == len(_life.today(now2)["plan"]["chapters"]), \
            "正常起床时该把当天圈的章节都排完"
        print("ok 惰性排班（没醒不排 + 进度条不涨 + 赖床从实际起床点起 + 睡前截断不追进度）")


def test_offline_inbox_and_rhythm():
    """S3 休眠完整版：离线判定（睡/工作专注=离线）+ 离线留言 inbox + 作息自主浮动与幂等。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import config as _cfg
    import corpus as _corpus
    import db as _db
    import life as _life
    import offline as _off

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       BOOKS_DIR=tmp / "books"):
        # 老干部人格：6 点起、22 点睡（作息锚点明确，好断言浮动范围）
        _db.set_kv("persona_json", json.dumps({
            "archetype": "oldcadre", "label": "老干部", "name": "稳稳", "quirk": "",
            "text": "你作息规律。", "wake_hour": 6, "sleep_hour": 22,
            "bubble_mult": 0.8, "drawn_at": "2026-10-09T00:00:00"}))
        _mk_book(config.BOOKS_DIR, n_ch=4)

        morning = _dt(2026, 10, 9, 6, 0)
        # 预置今天的起床点（锚点前 5 分钟）：醒来才排班，与赖床随机浮动解耦
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_day": "2026-10-09", "wake_at": "2026-10-09T05:55:00"}))
        first_start = _dt.fromisoformat(_life.today(morning)["sessions"][0]["start"])
        # 工作单元进行中（flow / working）= 离线专注；开工前 = 在线
        assert _off.is_offline(first_start + _td(minutes=2)), "心流期应算离线专注"
        assert _off.is_offline(first_start + _td(minutes=30)), "working 应算离线专注"
        assert not _off.is_offline(first_start - _td(minutes=5)), "开工前应在线"

        # 睡觉 → 离线；reason 区分"睡"和"专注"
        _db.set_state("SLEEPING")
        assert _off.is_offline(), "睡觉应离线"
        assert _off.offline_reason() == "sleep"
        _db.set_state("AWAKE")

        # 离线留言 inbox：攒 → 取 → 清空
        assert _off.push_inbox("在吗") == 1
        assert _off.push_inbox("明天一起吃饭") == 2
        assert _off.pending_inbox_count() == 2
        items = _off.take_inbox()
        assert [m["text"] for m in items] == ["在吗", "明天一起吃饭"]
        assert _off.pending_inbox_count() == 0, "取走留言后应清空"

        # 作息自主：入睡/起床围绕人格锚点浮动，同一生活日幂等
        evening = _dt(2026, 10, 9, 20, 0)
        bed_anchor = _dt(2026, 10, 9, 22, 0)
        sleep_at = _off.tonight_sleep_at(evening)
        lo = bed_anchor - _td(minutes=_cfg.REST_SLEEP_EARLY_MAX)      # 早睡最多提前
        hi = bed_anchor + _td(minutes=_cfg.REST_SLEEP_LATE_MAX)       # 熬夜最多延后
        assert lo <= sleep_at <= hi, f"入睡浮动越界：{sleep_at}"
        assert _off.tonight_sleep_at(evening) == sleep_at, "同一晚入睡时间应幂等"

        wake_base = _dt(2026, 10, 10, 6, 0)
        wake_at = _off.next_wake_at(evening)
        wlo = wake_base - _td(minutes=_cfg.REST_WAKE_EARLY_MAX)       # 早醒最多提前
        whi = wake_base + _td(minutes=_cfg.REST_WAKE_LIE_IN_MAX * 2)  # 熬夜可叠加赖床
        assert wlo <= wake_at <= whi, f"起床浮动越界：{wake_at}"
        assert _off.next_wake_at(evening) == wake_at, "同一晚起床时间应幂等"

        # 语料：工作专注时的第一人称轻提示存在
        assert len(_corpus.OFFLINE_FOCUS_NOTICES) >= 3
        print("ok 离线留言 + 作息自主（离线判定/inbox 攒取/浮动与幂等）")


def test_wake_semantics_and_tick():
    """起床双语义（实测 #26）：today_wake_at=「今天该几点起」不 +1 天；
    next_wake_at=夜里问「下次几点起」→ 明天，且 wake_days 按日缓存夜里反复问不抖动（#27）；
    被旧 bug 污染的缓存（时刻落在别的日期）自愈；tick 处理赖床醒/熬夜睡/reason 缺失自愈。"""
    import json
    import tempfile
    from datetime import datetime as _dt

    import db as _db
    import offline as _off

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        _db.set_kv("persona_json", json.dumps({
            "archetype": "oldcadre", "label": "老干部", "name": "稳稳", "quirk": "",
            "text": "你作息规律。", "wake_hour": 6, "sleep_hour": 22,
            "bubble_mult": 0.8, "drawn_at": "2026-10-09T00:00:00"}))

        # 早晨问"今天该几点起"：必须落在今天（老 bug：+1 天 → 永远赖床不醒）
        morning = _dt(2026, 10, 10, 6, 30)
        t = _off.today_wake_at(morning)
        assert t.date() == morning.date(), f"今天的起床算到了 {t.date()}（老 bug 又犯了）"
        assert _dt(2026, 10, 10, 5, 30) <= t <= _dt(2026, 10, 10, 7, 30), \
            f"起床浮动越界：{t}"
        assert _off.today_wake_at(morning) == t, "同一早晨的起床时刻应幂等"

        # 夜里问"下次几点起" → 明天；反复问同一时刻（夜里 tick 每分钟都问）
        evening = _dt(2026, 10, 10, 20, 0)
        w = _off.next_wake_at(evening)
        assert w.date() == _dt(2026, 10, 11).date(), f"夜里该问明天的：{w}"
        assert _off.next_wake_at(evening) == w, "夜里反复问，起床点在抖（单槽缓存被翻转）"
        # 且不破坏今天的缓存（wake_days 两天共存）
        assert _off.today_wake_at(evening) == t, "问明天把今天的起床点弄丢了"

        # 旧 bug 污染的缓存自愈：日期对但时刻落在明天 → 作废重算
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_day": "2026-10-12", "wake_at": "2026-10-13T06:10:00",
             "wake_days": {"2026-10-12": "2026-10-13T06:10:00"}}))
        m2 = _dt(2026, 10, 12, 6, 30)
        t2 = _off.today_wake_at(m2)
        assert t2.date() == m2.date(), f"被污染的缓存没自愈：{t2}"

        # tick：SLEEPY 赖床（lie_in）早晨没到起床点不动、过点醒
        class FakeEng:
            def __init__(self):
                self.calls = []
            def _do_wake(self):
                self.calls.append("wake")
            def _do_sleep(self):
                self.calls.append("sleep")
            def flush_inbox(self):
                self.calls.append("flush")

        eng = FakeEng()
        _db.set_state("SLEEPY")
        _db.set_kv("sleepy_reason", "lie_in")
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_days": {"2026-10-10": "2026-10-10T06:30:00"}}))
        _off.tick(eng, _dt(2026, 10, 10, 6, 10))
        assert not eng.calls, f"没到起床点不该动：{eng.calls}"
        _off.tick(eng, _dt(2026, 10, 10, 6, 45))
        assert eng.calls == ["wake"], f"赖床到点没醒：{eng.calls}"

        # SLEEPY 熬夜（stay_up）没到入睡点不动、过点睡
        eng.calls.clear()
        _db.set_kv("sleepy_reason", "stay_up")
        _db.set_kv("rest_schedule", json.dumps(
            {"day": "2026-10-10", "sleep_at": "2026-10-10T22:10:00",
             "wake_days": {"2026-10-10": "2026-10-10T06:30:00"}}))
        _off.tick(eng, _dt(2026, 10, 10, 22, 5))
        assert not eng.calls, f"没到入睡点不该动：{eng.calls}"
        _off.tick(eng, _dt(2026, 10, 10, 22, 30))
        assert eng.calls == ["sleep"], f"熬夜到点没睡：{eng.calls}"

        # 深夜保护：凌晨的 SLEEPY（reason 缺失的异常状态）睡回去，不能被叫醒
        # （凌晨的生活日还是昨天，today_wake_at 查到的是昨天已过的点）
        eng.calls.clear()
        _db.set_state("SLEEPY")
        _db.set_kv("sleepy_reason", "")
        _db.set_kv("rest_schedule", json.dumps(
            {"day": "2026-10-10", "sleep_at": "2026-10-10T22:10:00",
             "wake_days": {"2026-10-10": "2026-10-10T06:30:00",
                           "2026-10-11": "2026-10-11T06:30:00"}}))
        _off.tick(eng, _dt(2026, 10, 11, 1, 0))
        assert eng.calls == ["sleep"], f"深夜的 SLEEPY 被叫醒了（该睡回去）：{eng.calls}"

        # 白天 reason 缺失（被旧 bug 卡死的状态）：过了今天的起床点 → 醒，自愈
        eng.calls.clear()
        _off.tick(eng, _dt(2026, 10, 11, 14, 0))
        assert eng.calls == ["wake"], f"白天卡死的 SLEEPY 没自愈醒来：{eng.calls}"

        # SLEEPING 睡过头兜底（错过 cron / 电脑睡到中午）：今天的起床点过了就醒
        # （不能等 next_wake_at——早晨生活日翻篇后"下次"就是明天，会睡一整天）
        eng.calls.clear()
        _db.set_state("SLEEPING")
        _off.tick(eng, _dt(2026, 10, 11, 2, 0))
        assert not eng.calls, f"深夜的 SLEEPING 不该被叫醒：{eng.calls}"
        _off.tick(eng, _dt(2026, 10, 11, 12, 0))
        assert eng.calls == ["wake"], f"睡过头没兜底醒来：{eng.calls}"
        print("ok 起床双语义（today/next 分开 + 按日缓存不抖 + 污染自愈 + tick 赖床/熬夜/深夜保护）")


def test_care_events():
    """S4 事件记忆与关怀触发：承诺/约定提取 → 到点提醒（醒着发/睡着醒来补/过期错过）。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import db as _db
    import events as _ev
    import memory as _mem

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):

        # 未来才存 / 幂等
        assert _ev.add_care("提醒睡觉", "2020-01-01 12:00") is None, "过期的不该存"
        when = _ev.add_care("提醒睡觉", (_dt.now() + _td(hours=1)).strftime("%Y-%m-%d %H:%M"))
        assert when, "未来事件该存"
        assert _ev.add_care("提醒睡觉", when) is None, "同 what+when 应幂等"
        assert len(_ev.pending_cares()) == 1

        # 醒着到点 → care_remind 触发（missed=False），标记 done
        calls = []
        engine = types.SimpleNamespace(
            care_remind=lambda ev, missed=False: (calls.append((ev, missed)) or True))
        _db.set_kv("care_events", json.dumps([{
            "what": "提醒睡觉",
            "when": (_dt.now() - _td(minutes=5)).strftime("%Y-%m-%d %H:%M"),
            "kind": "提醒", "status": "pending",
            "created_at": _dt.now().isoformat(timespec="seconds"),
        }], ensure_ascii=False))
        _ev.tick(engine)
        assert len(calls) == 1 and calls[0][1] is False, "醒着到点该提醒"
        assert all(c["status"] == "done" for c in _ev.cares()), "提醒后应 done"

        # 睡着 → tick 跳过；醒来 flush_missed 补发（missed=True）
        calls.clear()
        _db.set_kv("care_events", json.dumps([{
            "what": "提醒睡觉",
            "when": (_dt.now() - _td(minutes=5)).strftime("%Y-%m-%d %H:%M"),
            "kind": "提醒", "status": "pending",
            "created_at": _dt.now().isoformat(timespec="seconds"),
        }], ensure_ascii=False))
        _db.set_state("SLEEPING")
        _ev.tick(engine)
        assert not calls, "睡着不该 tick 提醒"
        _db.set_state("AWAKE")
        _ev.flush_missed(engine)
        assert len(calls) == 1 and calls[0][1] is True, "醒来该补发错过"
        assert all(c["status"] == "done" for c in _ev.cares())

        # 过期超 24h → 标记 missed，不再提
        calls.clear()
        _db.set_kv("care_events", json.dumps([{
            "what": "提醒睡觉",
            "when": (_dt.now() - _td(hours=25)).strftime("%Y-%m-%d %H:%M"),
            "kind": "提醒", "status": "pending",
            "created_at": _dt.now().isoformat(timespec="seconds"),
        }], ensure_ascii=False))
        _ev.flush_missed(engine)
        assert not calls, "过期超24h不该再提醒"
        assert all(c["status"] == "missed" for c in _ev.cares())

        # memory 提取带 care 字段 → 落一条"我答应过"记忆 + 落一条关怀事件
        _db.set_kv("care_events", "[]")
        future = (_dt.now() + _td(hours=2)).strftime("%Y-%m-%d %H:%M")
        ok = _mem._apply({
            "content": "计划：提醒对方睡觉", "category": "计划",
            "time": future[:10], "theme": "健康作息",
            "care": {"what": "提醒对方睡觉", "when": future, "kind": "提醒"},
        })
        assert ok, "带 care 的记忆该落库"
        assert any("答应过" in m["content"] for m in _db.all_memories()), \
            "承诺应落一条'我答应过'计划记忆"
        assert len(_ev.pending_cares()) == 1, "care 应落一条关怀事件"

        print("ok 关怀事件（承诺提取/幂等/醒着提醒/睡着醒来补/过期错过）")


def test_interruption_cost():
    """S3 招牌：打断成本 = R(phase 阻力) × (1+F(精力疲劳)) → welcomed/tolerated/costly。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import config as _cfg
    import cost as _cost
    import db as _db
    import life as _life
    import persona as _persona

    tmp = Path(tempfile.mkdtemp())
    # WORK_SHAREABLE_ENABLED=False：章节结算要调 LLM，这里只测机制本身
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       WORK_SHAREABLE_ENABLED=False):
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "drawn_at": "2026-10-09T00:00:00"}))

        # 手造今日生活（v2）：不依赖真实书架，测试要确定
        morning = _dt(2026, 10, 9, 10, 0)
        day = _mk_day("2026-10-09", morning,
                      [int(_life.NOMINAL_SESSION_MIN * _life.speed())], warmup=10)
        first_start = _dt.fromisoformat(day["sessions"][0]["start"])

        # 阻力系数映射：心流最高、收工为零
        assert _cost.resistance("flow") == 1.0
        assert _cost.resistance("working") == 0.7
        assert _cost.resistance("off") == 0.0

        # 疲劳系数：精力满 = 0，精力空 = 1
        assert _cost.fatigue(100) == 0.0
        assert _cost.fatigue(0) == 1.0

        # 心流期（刚进入状态）恒为 costly——打断代价最高
        flow = _cost.state(first_start + _td(minutes=2))
        assert flow["level"] == "costly" and flow["cost_index"] >= 1.0, flow

        # 忙自己的事（精力还满）→ tolerated
        working = _cost.state(first_start + _td(minutes=30))
        assert working["level"] == "tolerated", working

        # 忙完了 → welcomed（完全欢迎，无论精力）
        off = _cost.state(_dt(2026, 10, 10, 1, 0))   # 夜猫子深夜，活都干完
        assert off["level"] == "welcomed" and off["cost_index"] == 0.0, off

        # 精力耗尽时，working 也应升到 costly（累 + 忙 = 双重代价）。
        # ⚠️ 精力是**派生**的（不能写字段造假）：把这段会话拉长到 200 分钟，
        # 读到 150 分钟处精力自然见底。
        day2 = _mk_day("2026-10-09", morning, [int(200 * _life.speed())], warmup=10)
        s2 = _dt.fromisoformat(day2["sessions"][0]["start"])
        tired = _cost.state(s2 + _td(minutes=150))
        assert tired["phase"] == "working" and tired["level"] == "costly", tired

        # 语态提示：三态都非空、且不踩反愧疚红线（只说自己的状态）
        for lv in ("welcomed", "tolerated", "costly"):
            assert _cost.LEVEL_HINTS[lv], lv
            assert not _persona.has_cost_blame(_cost.LEVEL_HINTS[lv]), \
                f"打断成本语态踩了反愧疚红线：{_cost.LEVEL_HINTS[lv]}"
        # HUD 徽章文案齐全
        for lv in ("welcomed", "tolerated", "costly", "sleep"):
            assert _cost.LEVEL_BADGE[lv], lv

        print("ok 打断成本（R×(1+F) 三态 + 语态不踩红线 + 徽章文案）")


def test_life_content_injection():
    """工作内容注入（实测 #28）：忙/心流时 prompt_hint 带「正在读什么」，
    TA 才答得上「你在忙啥」的追问，不会含糊回避、瞎编"读文献"。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import db as _db
    import life as _life

    tmp = Path(tempfile.mkdtemp())
    # WORK_SHAREABLE_ENABLED=False：章节结算要调 LLM，这里只测机制本身
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       WORK_SHAREABLE_ENABLED=False):
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "reading_pref": "科幻",
            "drawn_at": "2026-10-09T00:00:00"}))
        # 手造今日生活（v2）：不依赖真实书架，测试要确定
        morning = _dt(2026, 10, 9, 10, 0)
        day = _mk_day("2026-10-09", morning,
                      [int(_life.NOMINAL_SESSION_MIN * _life.speed())],
                      book_title="三体", warmup=10)
        first_start = _dt.fromisoformat(day["sessions"][0]["start"])

        # 忙（working）时：注入"正在读什么"，且带书名号（有具体书名）
        hint = _life.prompt_hint(first_start + _td(minutes=30))
        assert "我在读" in hint and "《三体》" in hint, f"忙的时候没注入读的具体内容：{hint}"
        # 章节坐标也在（今天圈了几章 / 读完几章 / 正在第几章）
        assert "今天圈了" in hint and "读完" in hint, f"缺章节坐标：{hint}"
        # 心流（flow）时同样注入
        flow_hint = _life.prompt_hint(first_start + _td(minutes=2))
        assert "《三体》" in flow_hint, f"心流期没注入读的内容：{flow_hint}"

        # 忙完了（off）不再注入"读什么"（注入只在手头有活的时候）
        off = _life.prompt_hint(_dt(2026, 10, 10, 1, 0))
        assert "我在读" not in off, f"忙完了还挂着读的内容：{off}"

        # 红线：注入的是"自己的状态"，不踩反愧疚归因
        assert not _persona_has_blame(hint), f"工作内容注入踩了红线：{hint}"
        print("ok 工作内容注入（忙/心流带「正在读什么」，忙完不注入，不踩红线）")


def test_material_share_injection():
    """素材主动分享（实测 #28）+ v0.89 §6「无弹药不发」：random 场景会翻素材池的
    读后感想当话题，翻过即消费；五源皆空则不发（治「车怠速」空转发）。"""
    import json
    import tempfile

    import context as _ctx
    import db as _db
    import life as _life

    tmp = Path(tempfile.mkdtemp())
    # WORK_ENABLED=False：隔离，否则 build_system_prompt 会触发生活排班/结算；
    # TODOS/SELF_MEMORY=False：隔离其他话题源，只测素材池这一源
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       WORK_ENABLED=False, TODOS_ENABLED=False, SELF_MEMORY_ENABLED=False):
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "drawn_at": "2026-10-09T00:00:00"}))
        _db.set_kv("work_shareables", json.dumps(
            [{"day": "2026-10-10", "seq": 1,
              "text": "读到苏轼那句「也无风雨也无晴」，愣了半天", "used": 0}]))

        # 50% 概率翻素材（有素材必翻）；循环多次必然命中
        seen = False
        for _ in range(40):
            msgs = _ctx.build_proactive_messages("AWAKE", "random")
            if msgs and "也无风雨也无晴" in msgs[-1]["content"]:
                seen = True
                break
        assert seen, "random 场景没翻到素材当话题"

        # 翻过的素材标记 used，不会再被取到
        fresh = [x for x in _life._shareables() if not x.get("used")]
        assert not fresh, f"翻过的素材没被消费：{_life._shareables()}"

        # §6 无弹药不发：五源皆空 → build_proactive_messages 返回 None
        # （core 据此不调 LLM、不发——旧实现会让 LLM 自由发挥编环境）
        _db.set_kv("work_shareables", json.dumps([]))
        assert _ctx.build_proactive_messages("AWAKE", "random") is None, \
            "没有话题弹药就不该生成主动消息"
        print("ok 素材主动分享（random 翻读后感想当话题 + 翻过即消费 + 无弹药不发）")


def test_todos():
    """待办队列（v0.80，实测 #42 #1）：入队/去重/独白解析/打勾/注入，
    治"素材颠来倒去就一句"——说过就打勾，打勾的不再当话题。"""
    import json
    import tempfile
    import config as _cfg
    import db as _db
    import stream as _stream
    import todos as _todos

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       TODOS_ENABLED=True):
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "reading_pref": "科幻",
            "drawn_at": "2026-10-09T00:00:00"}))

        # 1. 入队 + 相同内容去重
        i1 = _todos.add("《雾都孤儿》里那句关于雾的描写", source="reading")
        assert i1 > 0
        assert _todos.add("《雾都孤儿》里那句关于雾的描写") == 0, "同一条不该重复入队"
        assert len(_todos.pending()) == 1

        # 2. 独白解析：【我自己的话题】非空即入队；"没有"不入
        raw = ("【真实看法】同意\n"
               "【我自己的话题】想跟TA聊聊雾都孤儿里的雾\n"
               "【先说立场】没什么")
        assert _todos.from_monologue(raw) == "想跟TA聊聊雾都孤儿里的雾"
        assert _todos.from_monologue("【我自己的话题】没有") is None
        topic = _todos.from_monologue(raw)
        assert topic and _todos.add(topic, source="monologue") > 0
        assert len(_todos.pending()) == 2

        # 3. 注入：没说的列出来说话题
        hint = _todos.prompt_hint()
        assert "想说还没说的" in hint and "雾都孤儿" in hint, hint

        # 4. 打勾：回复里说到了就 done，并往意识流追加一条
        said = "对了，我最近读《雾都孤儿》，那句关于雾的描写真是绝了"
        hit = _todos.mark_done(said)
        assert hit == ["《雾都孤儿》里那句关于雾的描写"], hit
        assert len(_todos.pending()) == 1, "说过的该退出待办"
        assert len(_todos.done_items()) == 1
        todo_lines = [r["text"] for r in _stream.recent(20) if r["kind"] == "todo"]
        assert any("跟" in t and "说了" in t for t in todo_lines), todo_lines

        # 5. 注入块：打勾的标 ✅ 提醒别再原样说第二遍；没说的还在
        hint2 = _todos.prompt_hint()
        assert "✅" in hint2, hint2
        assert "想跟TA聊聊雾都孤儿里的雾" in hint2, hint2

        # 6. 打过的勾不再重复打
        assert _todos.mark_done(said) == []

        # 7. 关开关 → 不注入
        config.TODOS_ENABLED = False
        assert _todos.prompt_hint() == ""
        config.TODOS_ENABLED = True

        # 8. reset 清空
        _todos.reset()
        assert _todos.pending() == []

        print("ok 待办队列（入队去重 + 独白解析 + 打勾 + 注入✅ + 开关 + reset）")


def _persona_has_blame(text: str) -> bool:
    import persona as _persona
    return _persona.has_cost_blame(text)
