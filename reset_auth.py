#!/usr/bin/env python3
"""管理 friend-ai 公网登录凭据。

用法（在服务器 /opt/friend-ai 目录下执行）：
  .venv/bin/python reset_auth.py                  只改密码（保留 TOTP，不用重新扫码）
  .venv/bin/python reset_auth.py -p "你的新密码"   指定密码，不随机生成
  .venv/bin/python reset_auth.py --all            密码 + TOTP + 会话密钥全换（需重新扫码）

改完记得：sudo systemctl restart friend-ai
脚本幂等：只覆盖 .env 里对应的 WEB_AUTH_* 行，其余配置原样保留。
"""
import argparse
import base64
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auth

ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
# 去掉肉眼易混的 l/1/o/0/i/j，全小写 + 数字，手输不出错
ALPHA = "abcdefghkmnpqrstuvwxyz23456789"


def gen_password() -> str:
    return "-".join(
        "".join(secrets.choice(ALPHA) for _ in range(4)) for _ in range(3)
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--password", help="指定新密码（不给则随机生成）")
    ap.add_argument("--all", action="store_true",
                    help="连 TOTP 密钥和会话密钥一起换（需重新扫码）")
    args = ap.parse_args()

    pw = args.password or gen_password()
    if len(pw) < 8:
        sys.exit("密码太短了，至少 8 位")

    new = {"WEB_AUTH_PASS_HASH": auth.hash_password(pw)}
    if args.all:
        new["WEB_AUTH_TOTP_SECRET"] = base64.b32encode(
            secrets.token_bytes(20)).decode().rstrip("=")
        new["WEB_AUTH_SESSION_SECRET"] = secrets.token_hex(32)

    lines = []
    if os.path.exists(ENV):
        with open(ENV, encoding="utf-8") as f:
            for ln in f:
                s = ln.strip()
                if s and not s.startswith("#") and s.split("=", 1)[0] in new:
                    continue
                lines.append(ln.rstrip("\n"))
    while lines and not lines[-1]:
        lines.pop()

    # 保证几个开关存在且打开
    for k, v in (("WEB_AUTH_ENABLED", "on"), ("WEB_AUTH_COOKIE_SECURE", "on")):
        if not any(l.startswith(k + "=") for l in lines):
            lines.append("%s=%s" % (k, v))
    for k, v in new.items():
        lines.append("%s=%s" % (k, v))

    with open(ENV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(ENV, 0o600)

    print("新密码: " + pw)
    if args.all:
        print("新 TOTP 密钥: " + new["WEB_AUTH_TOTP_SECRET"])
        print("（会话密钥也换了，所有已登录设备会被登出）")
    else:
        print("TOTP 密钥未变，手机验证器不用重新扫码")
    print("下一步: sudo systemctl restart friend-ai")


if __name__ == "__main__":
    main()
