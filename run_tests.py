"""测试驱动器（E4 拆分，v0.92）。

原单文件 `test_p1_features.py` 的 71 个用例已按域拆到 `test_*.py`；本驱动器负责
**自动发现 + 按域顺序跑 + 汇总结果**，替代原来手写的 `__main__` 调用列表
（老坑：新增 `test_*` 忘了加进列表就**静默跳过**）。

用法：
  python run_tests.py              跑全量（默认）
  python run_tests.py memory       只跑某一域（按模块名子串匹配）
  python run_tests.py engine life  可给多个域

`test_p1_features.py` 保留为转发本驱动器的兼容 shim（旧命令/文档/CI 引用不断）。
"""
import importlib
import inspect
import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# 显式模块顺序：与原单文件 __main__ 里各域**首次出现的先后**一致；模块内部的用例
# 顺序也沿用原 __main__ 的相对次序（拆分时已按此排序）。新增一个域只在这里加一行。
MODULES = [
    "test_engine", "test_memory", "test_persona", "test_reading",
    "test_life", "test_proactive", "test_tuning", "test_info",
]

# 防漏闸门：全量期望用例数（E4 拆分时 = 71；v0.94 批 2 → 76；批 3 → 80；
# v0.98 批 4-5 加 test_info 6 项 → 86；v0.111 实测 #53 三个 bug 加 2 项 → 94；
# v0.112 全书章节坐标 1 项 → 95）。数量对不上就直接报错——避免"某个文件被漏收集"或
# "新用例所在模块没进 MODULES"这类静默跳过。
EXPECTED_TOTAL = 95


def _collect(module):
    """收集模块内**定义在本模块**的 test_*，按定义顺序返回 [(名字, 函数)]。

    只认 `obj.__module__ == 本模块` —— 防止 `from x import *` 带进来的别家用例被重复收。
    """
    fns = []
    for name, obj in vars(module).items():
        if not name.startswith("test_") or not inspect.isfunction(obj):
            continue
        if obj.__module__ != module.__name__:
            continue
        fns.append((obj.__code__.co_firstlineno, name, obj))
    fns.sort()
    return [(n, f) for _, n, f in fns]


def main(argv):
    wanted = [m for m in MODULES if any(a in m for a in argv)]
    if argv and not wanted:
        print(f"没有匹配的域：{argv}；可选："
              f"{[m.replace('test_', '') for m in MODULES]}")
        return 2
    mods = wanted or MODULES

    total = passed = 0
    failures = []
    for mod_name in mods:
        module = importlib.import_module(mod_name)
        cases = _collect(module)
        print(f"\n=== {mod_name}（{len(cases)} 项）===")
        for name, fn in cases:
            total += 1
            try:
                fn()
                passed += 1
            except Exception:                       # noqa: BLE001 —— 测试就是要把失败收齐
                failures.append((mod_name, name))
                print(f"[失败] {name}")
                traceback.print_exc()

    if not wanted and total != EXPECTED_TOTAL:
        print(f"\n[闸门] 全量用例数 {total} != 期望 {EXPECTED_TOTAL}：有模块没被收集，"
              f"或新用例没同步到 MODULES / EXPECTED_TOTAL。")
        return 1

    print(f"\n通过 {passed}/{total}")
    if failures:
        print("失败用例：")
        for m, n in failures:
            print(f"  - {m}.{n}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
