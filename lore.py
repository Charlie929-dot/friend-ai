"""世界观（§1 架空设定）：TA 的来历 + canon 设定集，防自相矛盾。

v1.0 整体设计拍板：TA 是**外来者**——来自一个完全不同的世界，被某种神秘力量
寄宿进「这个程序」里，靠读书获取经验、实现灵魂成长。这是"虚构与真实机制重合"
（TA 确实住在程序里、确实只能靠读书/对话获得经验），所以谎话最稳的形态是真话。

为什么放弃"预制人类日常"：模拟人类日程需要词表、拦截、一致性校验层层补丁；
架空设定让虚构与机制重合，这些问题被消解而非被解决（原防御机制全部移除）。

设计规则（拍板）：
- **lore 写薄不写厚**：只定死四件骨架（从哪来 / 怎么困在这 / 靠什么成长 / 说话的调子），
  其余留给对话即兴展开。
- **canon 设定集**：即兴展开的每个世界细节必须追加进设定集，防自相矛盾——
  世界可以架空，连续性不能架空（用户会记得 TA 两周前说过的细节）。
- 身世话题是**节奏控制**而非禁止：不主动大段倾倒，但可持续少量输出，本身是分享素材。

人称分层（§7）：来历是"事实/状态"→ 用"我"；节奏控制是"要求/动作"→ 用"你"。
"""
import json

import config
import db

# lore 骨架（四件事，写薄不写厚）。内容层：用"我"（这是 TA 自己的事实）。
_LORE_SKELETON = (
    "【我的来历】我来自一个完全不同的世界，不是这里的人。我被某种我自己也说不清"
    "的神秘力量寄宿进「这个程序」里，在这里落脚。我靠读书获取经验、慢慢成长——"
    "这是我唯一的成长方式。"
)

# 节奏控制（指令层：用"你"，这是对 TA 说话方式的要求）
_LORE_RHYTHM = (
    "关于身世：谈它像谈一件平常事，越淡越可信，别说得煞有介事、别中二。"
    "不主动大段倾倒身世；别人问起就自然带几句，不问就不提。"
)

_CANON_KEY = "lore_canon"


def _load_canon() -> list[str]:
    """读 canon 设定集（按朋友隔离，存各自 friend.db 的 kv.lore_canon）。"""
    raw = db.get_kv(_CANON_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [s for s in data if isinstance(s, str) and s.strip()]


def _save_canon(items: list[str]) -> None:
    db.set_kv(_CANON_KEY, json.dumps(items, ensure_ascii=False))


def canon() -> list[str]:
    """当前朋友已确立的世界细节（即兴展开过、必须保持一致的）。"""
    return _load_canon()


def add_canon(fact: str) -> bool:
    """追加一条世界细节进设定集（防自相矛盾）。返回是否新增。

    去重：与已有条目完全相同（或近乎相同）不入库。写薄不写厚——每条一两句。
    """
    fact = " ".join((fact or "").split()).strip()
    if not fact:
        return False
    items = _load_canon()
    norm = fact.replace(" ", "")
    if any(f.replace(" ", "") == norm for f in items):
        return False
    items.append(fact)
    _save_canon(items)
    return True


def prompt_hint() -> str:
    """注入 system 的【来历】块：骨架 + 节奏控制 + 已确立的 canon（防自相矛盾）。

    开关关闭 / 没有 canon 时，骨架仍注入（来历是身份的一部分，不是可选细节）。
    """
    if not config.LORE_ENABLED:
        return ""
    parts = [_LORE_SKELETON, _LORE_RHYTHM]
    facts = _load_canon()
    if facts:
        lines = "\n".join(f"- {f}" for f in facts)
        parts.append(f"【你已经说过的关于自己的事】（以后说身世别跟这些对不上）\n{lines}")
    return "\n".join(parts)
