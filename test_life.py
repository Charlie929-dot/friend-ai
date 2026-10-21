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
        # v0.112：全书坐标（HUD 用）与今日计划口径是两套数，不能混
        assert mid["book_chapter"] == 1 and mid["book_total"] == 6, f"全书坐标不对：{mid}"

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


def test_book_chapter_position():
    """v0.112：HUD 全书坐标。一天只圈 1 章、计划从第 4 章开始时，旧写法永远显示
    「第 1/1 章」——用户看不出它其实在往后读。今日计划口径保留给 prompt。"""
    import tempfile
    from datetime import datetime as _dt

    import db as _db
    import life as _life

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       BOOKS_DIR=tmp / "books", WORK_SHAREABLE_ENABLED=False):
        _mk_book(config.BOOKS_DIR, n_ch=6)
        _mk_day("2026-10-09", _dt(2026, 10, 9, 10, 0), [4000], start_ch=3)
        s = _life.status(_dt(2026, 10, 9, 10, 30))
        assert s["chapter"] == 1 and s["total_chapters"] == 1, \
            f"今日计划口径变了（prompt 的「今天圈了 N 章」靠它）：{s}"
        assert s["book_chapter"] == 4 and s["book_total"] == 6, \
            f"全书坐标不对（该是第 4/6 章，不是今日 1/1）：{s}"
        # 旧 day_state 没存 book_total → 按书 id 补算一次，也要正确
        assert "book_total" not in _life.today(_dt(2026, 10, 9, 10, 30))["plan"]
        print("ok 全书章节坐标（今日计划口径不变 + 旧 day_state 补算 + HUD 用全书位置）")


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


def test_proactive_guard_and_energy_rest():
    """guard 离线拦截（v0.99：专注时不主动闲聊，早安/晚安豁免）
    + 精力挂进休息策略（低于歇点 → 歇到回血再开工）。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import db as _db
    import guard as _guard
    import life as _life

    tmp = Path(tempfile.mkdtemp())
    # WORK_ENERGY_REST_BELOW=90：抬高歇点 → 每段读完都触发回血（默认 40 只偶尔触发）
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       BOOKS_DIR=tmp / "books", WORK_SHAREABLE_ENABLED=False,
                       WORK_ENERGY_REST_BELOW=90):
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "drawn_at": "2026-10-09T00:00:00"}))
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_day": "2026-10-09", "wake_at": "2026-10-09T09:55:00"}))
        _mk_book(config.BOOKS_DIR, n_ch=6)
        day = _life.today(_dt(2026, 10, 9, 10, 0))
        units = day["sessions"]
        assert units, "今天没排出会话"

        # 1. guard：会话进行中（离线专注）拦闲聊类主动消息；早安/晚安是作息锚点不拦
        mid = _dt.fromisoformat(units[0]["start"]) + _td(minutes=10)
        ok, reason = _guard.can_send_proactive(now=mid, scenario="impulse")
        assert not ok and "离线" in reason, f"专注时该拦主动闲聊：{ok} {reason}"
        ok, _ = _guard.can_send_proactive(now=mid, scenario="night")
        assert ok, "晚安是作息锚点事件，不该被离线拦截"
        # 休息相位不拦——这就是天然口子：歇着的时候照常说话
        ok, _ = _guard.can_send_proactive(
            now=_dt.fromisoformat(units[0]["end"]) + _td(minutes=1), scenario="impulse")
        assert ok, "休息相位不该拦（歇着的时候照常说话）"

        # 2. 精力歇点：歇点 90 → 之后每次开工精力都该补回「歇点 + 一段消耗」（封顶上限）
        target = min(config.WORK_ENERGY_MAX,
                     config.WORK_ENERGY_REST_BELOW + config.WORK_ENERGY_COST)
        for u in units[1:]:
            e = _life.energy(day, _dt.fromisoformat(u["start"]))
            assert e >= target - 1, \
                f"精力 {e} 低于歇点没歇够（目标 {target}）：开工 {u['start']}"
        print("ok guard 离线拦截（早/晚安豁免）+ 精力歇点回血")


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


def test_just_slept_not_woken():
    """v0.111（实测 #53）：刚说完晚安躺下，不能被"睡过头兜底"立刻叫醒。

    线上连着六晚：23:24 入睡 → 23:25 起床 → 一分钟后一条「早！窗子那边光…」，
    于是整夜 AWAKE（凌晨两点还在陪聊）。根因：SLEEPING 兜底只看「生活日还是当天
    + 今天该几点起已过」，而刚躺下时这两条天然成立。修法：起床点必须**晚于入睡
    时刻**——睡之前那个起床点不算数。
    """
    import json
    import tempfile
    from datetime import datetime as _dt

    import db as _db
    import offline as _off
    import stream as _stream

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        _db.set_kv("persona_json", json.dumps({
            "archetype": "oldcadre", "label": "老干部", "name": "稳稳", "quirk": "",
            "text": "你作息规律。", "wake_hour": 8, "sleep_hour": 23,
            "bubble_mult": 0.8, "drawn_at": "2026-10-09T00:00:00"}))

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
        # 10-19 夜：22:41 该睡、实际 23:24 躺下；今天（10-19）的起床点 08:42 早过了
        _db.set_kv("rest_schedule", json.dumps(
            {"day": "2026-10-19", "sleep_at": "2026-10-19T22:41:00",
             "wake_days": {"2026-10-19": "2026-10-19T08:42:00",
                           "2026-10-20": "2026-10-20T08:36:00"}}))
        _db.set_kv("sleep_started_at", "2026-10-19T23:24:05")
        _db.set_state("SLEEPING")
        _off.tick(eng, _dt(2026, 10, 19, 23, 25))
        assert not eng.calls, f"刚躺下一分钟就被叫醒（老 bug 复发）：{eng.calls}"
        _off.tick(eng, _dt(2026, 10, 19, 23, 59))
        assert not eng.calls, f"入睡当天深夜也不该被叫醒：{eng.calls}"

        # 睡过头兜底不能废：第二天的起床点过了照常醒（错过 cron / 电脑睡到中午）
        _off.tick(eng, _dt(2026, 10, 20, 9, 0))
        assert eng.calls == ["wake"], f"第二天睡过头没兜底醒来：{eng.calls}"

        # kv 没有（部署时正睡着）→ 退回意识流最后一条 sleep，同样不该被叫醒
        eng.calls.clear()
        _db.set_kv("sleep_started_at", "")
        _stream.append("sleep", "我困了，去睡了", _dt(2026, 10, 19, 23, 24, 5))
        _db.set_state("SLEEPING")
        _off.tick(eng, _dt(2026, 10, 19, 23, 30))
        assert not eng.calls, f"退回意识流的入睡时刻没生效：{eng.calls}"

        # v0.113（实测 #55）：连入睡记录都没有（老库/外部改状态）时，已经过了今晚
        # 入睡点还在睡，也不许被"今天早些时候该起了"兜底叫醒。
        eng.calls.clear()
        _db.set_kv("sleep_started_at", "")
        _db.set_kv("rest_schedule", json.dumps(
            {"day": "2026-10-21", "sleep_at": "2026-10-21T22:41:00",
             "wake_days": {"2026-10-21": "2026-10-21T08:30:00",
                           "2026-10-22": "2026-10-22T08:30:00"}}))
        _db.set_state("SLEEPING")
        _off.tick(eng, _dt(2026, 10, 21, 23, 25))
        assert not eng.calls, f"没有入睡记录时深夜被兜底叫醒（时间闸失效）：{eng.calls}"
        # 第二天早上照常醒（闸门只管"今晚入睡点之后"）
        _off.tick(eng, _dt(2026, 10, 22, 9, 0))
        assert eng.calls == ["wake"], f"时间闸把第二天早晨也拦了：{eng.calls}"
        print("ok 刚入睡不被叫醒（起床点须晚于入睡时刻 + 无 kv 退意识流 + 睡过头兜底仍在）")


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


def test_calendar_events():
    """日历（v0.103 批 3 + v0.104 修正）：触点概率随临近递增 + 聊过沉淀（不取消）
    + 冷却 + 准备状态 + 年重复 + 触点注入念头 + 聊到的内容进下次开口的弹药。

    触点时刻本身是随机的（提醒日当天挑个时间），所以断言的项一律**手动指定**
    next_touch——否则随机时刻擦到过去就会偶发失败。
    """
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import db as _db
    import events as _ev
    import stream as _stream

    def _stored(what: str) -> dict:
        return next(e for e in _ev.calendar_events() if e["what"] == what)

    def _only(what: str, when: _dt, **extra) -> None:
        """日历项**只留这一条**直接写 kv（跳过 add_calendar 的触点随机化）。"""
        ev = {"what": what, "when": when.strftime("%Y-%m-%d %H:%M"), "kind": "plan",
              "prepare": "", "status": "pending", "annual": False, "talked": 0,
              "talked_at": "", "mute_until": "", "notes": [],
              "created_at": _dt.now().isoformat(timespec="seconds")}
        ev.update(extra)
        _db.set_kv("calendar_events", json.dumps([ev], ensure_ascii=False))

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       CALENDAR_ENABLED=True, CONSCIOUS_STREAM_ENABLED=True,
                       CALENDAR_TOUCH_PROB_FAR=1.0, CALENDAR_TOUCH_PROB_NEAR=1.0,
                       CALENDAR_TALKED_MUTE_DAYS=2, CALENDAR_NOTE_MAX=3):
        now = _dt.now()
        # 1) 非年重复：未来才存、同 what 幂等、算好 next_touch
        when = _ev.add_calendar("小徐明天晚上要ppt汇报",
                                (now + _td(days=2)).strftime("%Y-%m-%d %H:%M"), kind="plan")
        assert when, "未来日历项该存"
        assert _ev.add_calendar("小徐明天晚上要ppt汇报",
                                (now + _td(days=3)).strftime("%Y-%m-%d %H:%M")) is None, "同 what 应幂等"
        assert _ev.pending_calendar()[0]["next_touch"], "该算出 next_touch"

        # 2) 准备状态随对话更新
        assert _ev.set_prepare("小徐明天晚上要ppt汇报", "在做了") is True
        assert _stored("小徐明天晚上要ppt汇报")["prepare"] == "在做了"

        # 3) 聊过 = 沉淀，不是取消：记动态记录 + 起冷却，next_touch 原样保留
        before_touch = _stored("小徐明天晚上要ppt汇报")["next_touch"]
        assert _ev.note_talked("小徐明天晚上要ppt汇报", "还没开始做") is True
        ev2 = _stored("小徐明天晚上要ppt汇报")
        assert ev2["talked"] == 1, "该记下聊过"
        assert ev2["notes"] and ev2["notes"][-1]["text"] == "还没开始做", "该沉淀动态记录"
        assert ev2["prepare"] == "还没开始做", "准备状态该跟着这次聊到的更新"
        assert ev2["next_touch"] == before_touch, "聊过不该取消后续触点（只是这几天不追着问）"
        assert ev2["mute_until"], "该起冷却"

        # 4) 年重复：MM-DD 生成今年/明年事件
        _ev.add_calendar("国庆节", "10-01", kind="holiday", annual=True)
        ann = [e for e in _ev.calendar_events() if e["what"] == "国庆节"]
        assert ann and ann[0]["annual"] is True and ann[0]["month"] == 10 and ann[0]["day"] == 1

        # 5) 聊到的内容进下次开口的弹药（不是从零问一遍）
        import context as _ctx
        _db.set_kv("calendar_pending", json.dumps(
            _stored("小徐明天晚上要ppt汇报"), ensure_ascii=False))
        msgs = [{"role": "system", "content": "人设"}, {"role": "user", "content": "开口"}]
        assert _ctx._inject_calendar_material(msgs) is True
        assert "还没开始做" in msgs[-1]["content"], "提起时该带着上次聊到的上下文"

        # 6) 记忆抽取里「聊到惦记的事」→ 沉淀进那件事（信息从对话流向日历）
        import memory as _mem
        _mem._apply({"content": "小徐说ppt还没开始做，打算今晚赶", "category": "计划",
                     "talked": {"what": "小徐明天晚上要ppt汇报", "note": "打算今晚赶"}})
        ev = _stored("小徐明天晚上要ppt汇报")
        assert ev["notes"][-1]["text"] == "打算今晚赶", "聊到的进展该沉淀进动态记录"
        assert len(ev["notes"]) <= 3, f"动态记录该有上限：{len(ev['notes'])}"

        # 7) 触点到期 → 注入念头 + 主动开口（概率全开时必开口）
        _only("周五交论文", now + _td(days=2),
              next_touch=(now - _td(minutes=1)).isoformat(timespec="seconds"))
        calls = []
        engine = types.SimpleNamespace(proactive=lambda sc: (calls.append(sc) or True))
        _db.set_state("AWAKE")
        _ev.calendar_tick(engine)
        assert "calendar" in calls, f"触点到期该触发 calendar 开口：{calls}"
        lines = [r["text"] for r in _stream.recent(20) if r["kind"] == "calendar"]
        assert any("周五交论文" in t for t in lines), f"触点到期该往意识流注入念头：{lines}"
        # 触完要推进（不是原地打转），事件没到就还 pending
        assert _stored("周五交论文")["status"] == "pending", "事件没过去不该收"

        # 8) 冷却中：念头照留，但不追着开口（刚聊过还问 = 没在听）
        _only("刚聊过的事", now + _td(days=1),
              talked=1, talked_at=now.isoformat(timespec="seconds"),
              next_touch=(now - _td(minutes=1)).isoformat(timespec="seconds"),
              mute_until=(now + _td(days=2)).isoformat(timespec="seconds"),
              notes=[{"at": now.isoformat(timespec="seconds"), "text": "还没开始"}])
        calls.clear()
        _ev.calendar_tick(engine)
        assert not calls, "冷却中不该靠触点开口"
        lines = [r["text"] for r in _stream.recent(20) if r["kind"] == "calendar"]
        assert any("刚聊过的事" in t for t in lines), "冷却期念头照常留（TA想起就能自己提）"
        # 冷却过了 → 又能开口（说明是冷却，不是取消）
        _only("刚聊过的事", now + _td(days=1),
              talked=1, talked_at=now.isoformat(timespec="seconds"),
              next_touch=(now - _td(minutes=1)).isoformat(timespec="seconds"),
              mute_until="", notes=[{"at": now.isoformat(timespec="seconds"), "text": "还没开始"}])
        calls.clear()
        _ev.calendar_tick(engine)
        assert "calendar" in calls, "冷却过了就该还能开口（不是取消）"

        # 9) 事件时刻过了才收（不是触完四个触点就收）
        _only("已经过去的事", now - _td(hours=2),
              next_touch=(now - _td(minutes=1)).isoformat(timespec="seconds"))
        _ev.calendar_tick(engine)
        assert _stored("已经过去的事")["status"] == "done", "时间截止才收"

        # 10) 生日年重复：从 profile 的「生日」字段自动生成年事件
        _db.set_kv("profile_locked", "1")
        _db.set_kv("profile_text", "生日：03-15\n城市：上海")
        assert _ev.ensure_birthday(now) is not None
        bday = [e for e in _ev.calendar_events() if e["what"] == "TA的生日"]
        assert bday and bday[0]["annual"] and bday[0]["month"] == 3 and bday[0]["day"] == 15, \
            f"生日该生成年重复事件：{bday}"

    # 11) 提起概率：越临近事件越高（纯函数，不掷骰子）
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       CALENDAR_TOUCH_PROB_FAR=0.0, CALENDAR_TOUCH_PROB_NEAR=1.0):
        now = _dt.now()
        ev = {"what": "一件事", "when": (now + _td(days=10)).strftime("%Y-%m-%d %H:%M")}
        d = _dt.strptime(ev["when"], "%Y-%m-%d %H:%M")
        p_far = _ev._touch_prob(ev, d - _td(days=7), now)     # 前一周
        p_mid = _ev._touch_prob(ev, d - _td(days=3), now)     # 前三天
        p_near = _ev._touch_prob(ev, d, now)                  # 事件当天
        assert p_far == 0.0 and p_near == 1.0, f"概率锚点不对：{p_far}/{p_near}"
        assert p_far < p_mid < p_near, f"概率该随临近递增：{p_far}/{p_mid}/{p_near}"
        print("ok 日历（触点概率递增/聊过沉淀不取消/冷却/准备状态/年重复/念头）")


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
            msgs = _ctx.build_proactive_messages("AWAKE", "impulse")
            if msgs and "也无风雨也无晴" in msgs[-1]["content"]:
                seen = True
                break
        assert seen, "impulse 场景没翻到素材当话题"

        # 翻过的素材标记 used，不会再被取到
        fresh = [x for x in _life._shareables() if not x.get("used")]
        assert not fresh, f"翻过的素材没被消费：{_life._shareables()}"

        # §6 无弹药不发：五源皆空 → build_proactive_messages 返回 None
        # （core 据此不调 LLM、不发——旧实现会让 LLM 自由发挥编环境）
        _db.set_kv("work_shareables", json.dumps([]))
        assert _ctx.build_proactive_messages("AWAKE", "impulse") is None, \
            "没有话题弹药就不该生成主动消息"
        print("ok 素材主动分享（impulse 翻读后感想当话题 + 翻过即消费 + 无弹药不发）")


def test_todos():
    """待办队列（v0.80，实测 #42 #1）：入队/去重/独白解析/打勾/注入，
    治"素材颠来倒去就一句"——说过就打勾，打勾的不再当话题。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

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

        # 8. v0.112：独白自己判"否"的不入库；状态碎片也不入库
        assert _todos.from_monologue(
            "【我自己的话题】早上那阵光今天终于照进来了\n【要不要主动起话题】否") is None
        assert _todos.from_monologue("【我自己的话题】书也合上了。") is None
        assert _todos.from_monologue(
            "【我自己的话题】早上那阵光今天终于照进来了\n【要不要主动起话题】是") is not None

        # 9. 近似去重：同一件事的不同说法只排一条（旧版排了 6 条"光"）
        _todos.reset()
        assert _todos.add("早上那阵光，今天终于照进来了。") > 0
        assert _todos.add("早上那阵光今天终于照进来了，我边翻书边看它挪位置。") == 0

        # 10. 过期：昨天的念头第二天不再注入（"光"从 10-16 挂到 10-21 的根因）
        _todos.reset()
        old = _dt.now() - _td(hours=30)
        assert _todos.add("昨天想到的一件事，今天不该再冒出来", now=old) > 0
        assert _todos.prompt_hint() == ""

        # 11. 每轮最多注入 3 条（旧版 40 条全倒进 prompt，模型只会在同一条上打转）
        _todos.reset()
        for t in ("想跟你说说今天读到的那段雾",
                  "楼下的猫今天又蹲在窗台上",
                  "刚刷到一条讲攀岩受伤的新闻",
                  "《时间机器》结尾那段我还没缓过来",
                  "明天想早起去把书还了"):
            _todos.add(t, now=_dt.now())
        hint3 = _todos.prompt_hint()
        assert hint3.count("\n- ") == 3, hint3

        # 12. reset 清空
        _todos.reset()
        assert _todos.pending() == []

        print("ok 待办队列（入队去重 + 独白解析 + 打勾 + 注入✅ + 开关 + reset）")


def _persona_has_blame(text: str) -> bool:
    import persona as _persona
    return _persona.has_cost_blame(text)
