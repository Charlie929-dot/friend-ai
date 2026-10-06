"""一次性迁移（2026-10-06）：旧时间戳快照归档 → 稳定 friend_id 归档。
删除任何文件前先验证重复/子集关系，不符即中止。备份在 backups/20261006-pre-merge/。
"""
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent


def kv_set(p: Path, k: str, v: str) -> None:
    c = sqlite3.connect(str(p))
    c.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    c.commit()
    c.close()


def messages(p: Path) -> set:
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    r = {(row["ts"], row["role"], row["content"])
         for row in c.execute("SELECT ts, role, content FROM messages")}
    c.close()
    return r


new_id = lambda: uuid.uuid4().hex[:8]

# --- 0. 安全验证：不符就中止，什么都不动 ---
kui_a = messages(BASE / "friend-20261006-004938-小葵.db")
kui_b = messages(BASE / "friend-20261006-115352-小葵.db")
kui_cur = messages(BASE / "friend.db")
if not (kui_a == kui_b == kui_cur):
    sys.exit("中止：小葵三个库消息不一致，需人工核对")
legacy = messages(BASE / "friend-20261006-111256.db")
if not messages(BASE / "friend-20261005-234947-初遇朋友.db") <= legacy:
    sys.exit("中止：初遇朋友(234947) 不是 111256 的子集")
if not messages(BASE / "friend-20261006-121132.db") <= legacy:
    sys.exit("中止：未命名(121132) 不是 111256 的子集")
print("验证通过：小葵三库完全相同；初遇/121132 均为 111256 的子集")

# --- 1. 小葵：当前 friend.db 为正主（内容三处相同） ---
fid_kui = new_id()
kv_set(BASE / "friend.db", "friend_id", fid_kui)
kv_set(BASE / "friend.db", "created_at", "2026-10-06T00:49:38")   # 首次归档时刻
kv_set(BASE / "friend.db", "archived_at", "2026-10-06T11:53:52")  # 最后一次归档时刻
shutil.copyfile(str(BASE / "friend.db"), str(BASE / f"friend-{fid_kui}.db"))
print(f"小葵 → friend-{fid_kui}.db")

# --- 2. 稳稳 ---
fid_wen = new_id()
wen = BASE / "friend-20261006-002611-稳稳.db"
kv_set(wen, "friend_id", fid_wen)
kv_set(wen, "created_at", "2026-10-06T00:26:11")
kv_set(wen, "archived_at", "2026-10-06T00:26:11")
wen.rename(BASE / f"friend-{fid_wen}.db")
print(f"稳稳 → friend-{fid_wen}.db")

# --- 3. 老朋友：111256 为正主，继承别名「初遇朋友」 ---
fid_leg = new_id()
leg = BASE / "friend-20261006-111256.db"
kv_set(leg, "friend_id", fid_leg)
kv_set(leg, "alias", "初遇朋友")
kv_set(leg, "created_at", "2026-10-05T23:49:47")   # 最早一次归档
kv_set(leg, "archived_at", "2026-10-06T11:12:56")
leg.rename(BASE / f"friend-{fid_leg}.db")
print(f"老朋友(初遇朋友) → friend-{fid_leg}.db")

# --- 4. 删除重复快照（验证已通过，备份已在 backups/） ---
for name in ("friend-20261006-004938-小葵.db", "friend-20261006-115352-小葵.db",
             "friend-20261005-234947-初遇朋友.db", "friend-20261006-121132.db"):
    (BASE / name).unlink()
    print(f"已删除重复快照 {name}")

# --- 5. 归档日志补登迁移事件 ---
with open(BASE / "archive.log", "a", encoding="utf-8") as f:
    f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 迁移：旧时间戳快照 → 稳定 friend_id 归档；"
            f"小葵={fid_kui} 稳稳={fid_wen} 初遇朋友={fid_leg}；"
            "删除 4 份重复快照（备份于 backups/20261006-pre-merge/）\n")
print("迁移完成")
