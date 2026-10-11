"""阅读素材库（S3 工作系统的内容源）：TA 的工作 = 读书。

内置真实书摘/短文（公版经典为主），按阅读偏好（genre）分类。每个工作单元 =
读一篇；读的顺序按 TA 的人格阅读偏好优先（爱读科幻的先把科幻读完），顺序推进、
进度持久化（kv），一轮读完再从头。

**分类和书库统一**（2026-10-11）：库里 genre 仍是旧的细标签（散文/科幻/热血…），
对外统一翻译成 22 个一级分类（`corpus.PREF_LEGACY_MAP`），原标签降级成 `tag`——
这样内置书摘和用户导入的真书共用同一套分类，人格两级偏好（分类 + 细标签）两头都能命中。

产出：每读完一篇 → 1 条可分享素材（摘抄金句 + TA 人格化的读后感），供 C 模块
主动分享当内容源（work._gen_shareable 读这里的 quote 当"摘抄"）。
"""
import json

import corpus
import db

# 素材库：genre（阅读偏好大类，对应 persona 卡的 reading_pref）/ title / source /
# text（值得读的一段原文）/ quote（预摘金句，供"摘抄"用）。
# 内容以公版经典为主（鲁迅/老舍/朱自清/梭罗/凡尔纳/威尔斯/雪莱 + 古文古诗），
# 短小真实、可考，避免版权问题。
LIBRARY = [
    # ---- 科幻（夜猫子爱读：深夜感性、奇怪想法） ----
    {"genre": "科幻", "title": "海底两万里", "source": "儒勒·凡尔纳",
     "text": "尼摩船长带阿龙纳斯教授漫游海底，穿过珊瑚海、探访沉船、抵达南极。他说，海面之上有暴政与奴役，海面之下才是真正的自由。",
     "quote": "大海不属于暴君。"},
    {"genre": "科幻", "title": "时间机器", "source": "H·G·威尔斯",
     "text": "时间旅行者去到遥远的未来，发现人类已分化成两个物种：地面的埃洛伊与地下的莫洛克，前者优雅却退化，后者劳苦却主宰。",
     "quote": "人类不会永远停在顶峰，只会继续分化。"},
    {"genre": "科幻", "title": "弗兰肯斯坦", "source": "玛丽·雪莱",
     "text": "科学家用尸体拼出一个「人」，却因它的丑陋而恐惧逃走。怪物孤独地学会了语言与情感，只求一个同伴，最后只换来怨恨。",
     "quote": "我本应是你的亚当，却成了堕落的魔鬼。"},
    {"genre": "科幻", "title": "逍遥游", "source": "庄子",
     "text": "北冥有鱼，其名为鲲。鲲之大，不知其几千里也；化而为鸟，其名为鹏。鹏之背，不知其几千里也。",
     "quote": "水击三千里，抟扶摇而上者九万里。"},

    # ---- 散文（温柔治愈爱读：细腻、轻） ----
    {"genre": "散文", "title": "匆匆", "source": "朱自清",
     "text": "燕子去了，有再来的时候；杨柳枯了，有再青的时候；桃花谢了，有再开的时候。但是，聪明的，你告诉我，我们的日子为什么一去不复返呢？",
     "quote": "我的日子滴在时间的流里，没有声音，也没有影子。"},
    {"genre": "散文", "title": "济南的冬天", "source": "老舍",
     "text": "一个老城，有山有水，全在天底下晒着阳光，暖和安适地睡着，只等春风来把它们唤醒，这是不是个理想的境界？",
     "quote": "一个老城，有山有水，全在天底下晒着阳光。"},
    {"genre": "散文", "title": "瓦尔登湖", "source": "梭罗",
     "text": "我步入丛林，因为我希望活得从容，只面对生命的基本事实，看看我是否能学会它要教给我的东西，而不至于临死时才发觉自己从未真正活过。",
     "quote": "我愿意深深地扎入生活，吮尽生活的骨髓。"},
    {"genre": "散文", "title": "爱莲说", "source": "周敦颐",
     "text": "予独爱莲之出淤泥而不染，濯清涟而不妖，中通外直，不蔓不枝，香远益清，亭亭净植，可远观而不可亵玩焉。",
     "quote": "出淤泥而不染，濯清涟而不妖。"},

    # ---- 历史（老干部爱读：稳、讲道理） ----
    {"genre": "历史", "title": "报任安书", "source": "司马迁",
     "text": "人固有一死，或重于泰山，或轻于鸿毛，用之所趋异也。文王拘而演周易，仲尼厄而作春秋。",
     "quote": "人固有一死，或重于泰山，或轻于鸿毛。"},
    {"genre": "历史", "title": "史记·项羽本纪", "source": "司马迁",
     "text": "项王军壁垓下，兵少食尽，汉军及诸侯兵围之数重。夜闻汉军四面皆楚歌，项王乃大惊曰：汉皆已得楚乎？是何楚人之多也。",
     "quote": "力拔山兮气盖世，时不利兮骓不逝。"},
    {"genre": "历史", "title": "左传", "source": "左丘明",
     "text": "居安思危，思则有备，有备无患。敢以此规。",
     "quote": "居安思危，思则有备，有备无患。"},
    {"genre": "历史", "title": "诫子书", "source": "诸葛亮",
     "text": "夫君子之行，静以修身，俭以养德。非淡泊无以明志，非宁静无以致远。夫学须静也，才须学也。",
     "quote": "非淡泊无以明志，非宁静无以致远。"},

    # ---- 杂文（高冷毒舌爱读：爱吐槽、敢说） ----
    {"genre": "杂文", "title": "故乡", "source": "鲁迅",
     "text": "我想：希望是本无所谓有，无所谓无的。这正如地上的路；其实地上本没有路，走的人多了，也便成了路。",
     "quote": "其实地上本没有路，走的人多了，也便成了路。"},
    {"genre": "杂文", "title": "热风·随感录四十一", "source": "鲁迅",
     "text": "愿中国青年都摆脱冷气，只是向上走，不必听自暴自弃者流的话。能做事的做事，能发声的发声。有一分热，发一分光。",
     "quote": "有一分热，发一分光。"},
    {"genre": "杂文", "title": "记念刘和珍君", "source": "鲁迅",
     "text": "真的猛士，敢于直面惨淡的人生，敢于正视淋漓的鲜血。这是怎样的哀痛者和幸福者？",
     "quote": "真的猛士，敢于直面惨淡的人生。"},
    {"genre": "杂文", "title": "狂人日记", "source": "鲁迅",
     "text": "我翻开历史一查，这历史没有年代，歪歪斜斜的每页上都写着「仁义道德」几个字。我横竖睡不着，仔细看了半夜，才从字缝里看出字来，满本都写着两个字是「吃人」。",
     "quote": "从来如此，便对么？"},

    # ---- 热血（中二乐天爱读：史诗感、燃） ----
    {"genre": "热血", "title": "周易·乾卦", "source": "易经",
     "text": "天行健，君子以自强不息；地势坤，君子以厚德载物。",
     "quote": "天行健，君子以自强不息。"},
    {"genre": "热血", "title": "滕王阁序", "source": "王勃",
     "text": "老当益壮，宁移白首之心？穷且益坚，不坠青云之志。酌贪泉而觉爽，处涸辙以犹欢。",
     "quote": "穷且益坚，不坠青云之志。"},
    {"genre": "热血", "title": "满江红", "source": "岳飞",
     "text": "三十功名尘与土，八千里路云和月。莫等闲，白了少年头，空悲切。",
     "quote": "莫等闲，白了少年头，空悲切。"},
    {"genre": "热血", "title": "孟子·告子下", "source": "孟子",
     "text": "故天将降大任于是人也，必先苦其心志，劳其筋骨，饿其体肤，空乏其身，行拂乱其所为，所以动心忍性，曾益其所不能。",
     "quote": "生于忧患，而死于安乐也。"},

    # ---- 生活随笔（阳光话痨爱读：轻松、分享欲） ----
    {"genre": "生活随笔", "title": "饮酒·其五", "source": "陶渊明",
     "text": "采菊东篱下，悠然见南山。山气日夕佳，飞鸟相与还。此中有真意，欲辨已忘言。",
     "quote": "采菊东篱下，悠然见南山。"},
    {"genre": "生活随笔", "title": "定风波", "source": "苏轼",
     "text": "莫听穿林打叶声，何妨吟啸且徐行。竹杖芒鞋轻胜马，谁怕？一蓑烟雨任平生。",
     "quote": "回首向来萧瑟处，归去，也无风雨也无晴。"},
    {"genre": "生活随笔", "title": "增广贤文", "source": "佚名",
     "text": "一年之计在于春，一日之计在于晨，一家之计在于和，一生之计在于勤。",
     "quote": "一日之计在于晨，一年之计在于春。"},
    {"genre": "生活随笔", "title": "山居秋暝", "source": "王维",
     "text": "空山新雨后，天气晚来秋。明月松间照，清泉石上流。竹喧归浣女，莲动下渔舟。",
     "quote": "明月松间照，清泉石上流。"},
]

_KEY = "reading_progress"


def _load() -> dict:
    raw = db.get_kv(_KEY)
    if not raw:
        return {}
    try:
        p = json.loads(raw)
    except ValueError:
        return {}
    return p if isinstance(p, dict) else {}


def _save(progress: dict) -> None:
    db.set_kv(_KEY, json.dumps(progress, ensure_ascii=False))


def category_of(genre: str) -> str:
    """旧细标签（散文/科幻/热血…）→ 22 个一级分类；新分类原样返回。"""
    return corpus.PREF_LEGACY_MAP.get((genre or "").strip(), (genre or "").strip())


def _enrich(article: dict) -> dict:
    """把一篇素材翻译成对外口径：genre=一级分类、tag=原细标签（和 library 同构）。"""
    out = dict(article)
    out["tag"] = article.get("genre", "")
    out["genre"] = category_of(article.get("genre", ""))
    out["category"] = out["genre"]
    return out


def _rank(i: int, pref: str, pref_tags: list[str]) -> int:
    """这篇在 TA 口味里的排位：0 一级分类命中 → 1 细标签命中 → 2 其他。"""
    g = LIBRARY[i]["genre"]
    if pref and category_of(g) == pref:
        return 0
    if g in pref_tags:
        return 1
    return 2


def _order() -> list[int]:
    """TA 的阅读顺序：人格两级偏好命中排前面、其余按库序，首次读时定死并缓存。"""
    progress = _load()
    if progress.get("order"):
        return progress["order"]
    import persona
    params = persona.behavior_params()
    pref = params.get("reading_pref", "")
    pref_tags = params.get("reading_tags") or []
    order = list(range(len(LIBRARY)))
    order.sort(key=lambda i: (_rank(i, pref, pref_tags), i))
    progress["order"] = order
    progress["pos"] = 0
    _save(progress)
    return order


def next_article() -> dict | None:
    """取下一篇要读的（顺序推进、一轮读完再从头），返回素材 dict，并推进进度。

    返回的 genre 是一级分类（给 work/interests 用），原细标签在 tag 里。
    """
    if not LIBRARY:
        return None
    order = _order()
    progress = _load()
    pos = progress.get("pos", 0) % len(order)
    article = _enrich(LIBRARY[order[pos]])
    progress["pos"] = pos + 1
    _save(progress)
    return article


def current_article() -> dict | None:
    """当前正在读（还没推进）的那一篇——工作单元进行中给 prompt 用。"""
    if not LIBRARY:
        return None
    order = _order()
    progress = _load()
    pos = progress.get("pos", 0) % len(order)
    return _enrich(LIBRARY[order[pos]])
