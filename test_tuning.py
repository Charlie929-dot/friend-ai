"""调优参数面板（E4 拆分，v0.92）。

运行：`python run_tests.py tuning`（或全量 `python run_tests.py`）。
本文件顶部必须 `from _test_common import *`——两段全局机制（临时目录收口 +
关双层生成）在此生效，见约定 #10/#11。
"""
from _test_common import *  # noqa: F401,F403


def test_tuning_panel():
    """调优参数面板（G 模块 S4，方案 §10）：清单完整对齐 config、校验拒绝非法值、
    save 按「写 .env + force reload」序列热生效（模型设置同款机制）。"""
    import importlib as _il
    import tuning as _tuning
    import auth as _auth

    # 1) 清单完整性：9 组 / 键唯一 / 类型·层级合法 / 每个键都在 config 里有定义
    cats = _tuning.catalog()
    assert len(cats) == 9, f"该有 9 组，实际 {len(cats)}"
    items = [it for g in cats for it in g["items"]]
    keys = [it["key"] for it in items]
    assert len(keys) == len(set(keys)), "env key 重复"
    for it in items:
        assert it["type"] in ("int", "float", "time", "bool", "str"), \
            f"类型非法：{it['key']}"
        assert it["level"] in ("L1", "L2"), f"层级非法：{it['key']}"
        assert hasattr(config, it["key"]), f"config 缺 {it['key']}"
    assert sum(1 for it in items if it["level"] == "L1") > 0, "L1 首屏不能为空"
    assert sum(1 for it in items if it["level"] == "L2") > 0, "L2 高级不能为空"

    # 2) 当前值覆盖全部登记键；默认值**单头真相**（v0.88 E1）：
    #    catalog 的 default 直接取 config 当前常量，清单里不再手写
    cv = _tuning.current_values()
    assert set(cv) == set(keys), "current_values 漏了键"
    for it in items:
        raw = getattr(config, it["key"])
        if it["type"] == "float":
            want = f"{float(raw):g}"
        elif it["type"] == "int":
            want = str(int(raw))
        elif it["type"] == "bool":
            want = "on" if raw else "off"
        else:                                   # str（含 SOURCES_FILE 这种 Path）
            want = str(raw)
        assert it["default"] == want, \
            f"default 没从 config 取（双头维护会漂移）：{it['key']} = {it['default']} vs {want}"

    # 3) 非法值被拒（_coerce 抛错在 write_env 之前，不落盘）
    def _raises(key, val):
        try:
            _tuning.save_values({key: val})
            return False
        except ValueError:
            return True
    assert _raises("READING_DAILY_CHARS", "abc"), "非整数没被拒"
    assert _raises("BUBBLE_FIXED_PROB", "x"), "非数字没被拒"
    assert _raises("COST_R_FLOW", "5"), "越界（>max）没被拒"
    assert _raises("NUDGE_PROBABILITY", "1.5"), "概率越界没被拒"
    assert _raises("NO_SUCH_KEY", "1"), "未登记键没被拒"

    # time 类型的校验（面板里已无 time 键——SCHED_* 整族随 v0.89 退役，
    # 但类型支持留着，直接验 _coerce 的 time 分支）
    _time_item = {"k": "X_TIME", "l": "测试时间", "t": "time"}
    for bad in ("25:00", "8:60", "830"):
        try:
            _tuning._coerce(_time_item, bad)
            raise AssertionError(f"非法时间没被拒：{bad}")
        except ValueError:
            pass
    assert _tuning._coerce(_time_item, "9:05") == "09:05", "时间该规范化成 HH:MM"

    # bool / str 类型（v0.94 信息获取新增：开关 / 适配器名这类非数值参数）
    assert _tuning._coerce({"k": "X_B", "l": "测试开关", "t": "bool"}, "ON") == "on"
    assert _tuning._coerce({"k": "X_B", "l": "测试开关", "t": "bool"}, "off") == "off"
    for bad in ("maybe", ""):
        try:
            _tuning._coerce({"k": "X_B", "l": "测试开关", "t": "bool"}, bad)
            raise AssertionError(f"非法 bool 没被拒：{bad!r}")
        except ValueError:
            pass
    assert _tuning._coerce({"k": "X_S", "l": "测试串", "t": "str"}, "  abc ") == "abc"

    # 退役参数不能还挂在面板上（v0.89 批 A：20 → 11 个参数）
    retired = {"SCHED_WORK_START", "SCHED_LUNCH_START", "SCHED_LUNCH_END",
               "SCHED_WORK_END", "SCHED_HALF_PERIOD_MIN", "SCHED_INTRA_BREAK_MIN",
               "SCHED_INTER_BREAK_MIN", "SCHED_MORNING_PERIODS", "SCHED_AFTERNOON_PERIODS",
               "WORK_DAILY_QUOTA", "WORK_PAY_PER_UNIT", "WORK_UNIT_MINUTES",
               "WORK_BREAK_MINUTES", "WORK_READ_PER_UNIT", "READING_ENABLED"}
    left = set(keys) & retired
    assert not left, f"退役参数还在面板里：{left}"

    # 4) save 序列：write_env 收到规范化字符串 + 触发 load_dotenv(force=True) + reload(config)
    written, reloaded = {}, []
    real_write, real_ld, real_reload = _auth.write_env, config.load_dotenv, _il.reload

    def _fake_write(updates):
        written.update(updates)

    def _fake_ld(force=False):
        reloaded.append(("load", force))

    def _fake_reload(mod):
        reloaded.append(("reload", mod.__name__))

    _auth.write_env, config.load_dotenv, _il.reload = _fake_write, _fake_ld, _fake_reload
    try:
        _tuning.save_values({
            "READING_DAILY_CHARS": "12000",
            "BUBBLE_FIXED_PROB": 0.5,
            "WORK_WARMUP_MIN": "70",
        })
    finally:
        _auth.write_env, config.load_dotenv, _il.reload = real_write, real_ld, real_reload

    assert written == {
        "READING_DAILY_CHARS": "12000",
        "BUBBLE_FIXED_PROB": "0.5",
        "WORK_WARMUP_MIN": "70",
    }, f"write_env 收到的规范化值不对：{written}"
    assert ("load", True) in reloaded, "该 load_dotenv(force=True) 热生效"
    assert ("reload", "config") in reloaded, "该 reload(config) 刷新常量"
    print(f"ok 调优参数面板（清单 {len(cats)} 组 / {len(items)} 参数对齐 config + 校验拒非法 + save 写 .env 热生效序列）")
