"""登录验证配置向导：生成要写进服务器 .env 的那几行，并给出验证器扫码图。

用法（在要跑服务的那台机器上执行）：
    python auth_setup.py

会依次输出：
  1. WEB_AUTH_PASS_HASH       —— 密码的哈希（不会存明文密码）
  2. WEB_AUTH_TOTP_SECRET     —— 动态口令密钥，扫码后存进手机验证器
  3. WEB_AUTH_SESSION_SECRET  —— 给登录态 cookie 签名用的随机串

这三个值只存在于服务器的 .env 里，不进 git。
"""
import base64
import getpass
import secrets
import sys

from auth import hash_password, totp_code

ACCOUNT = "friend-ai"
ISSUER = "YouLiveFriend"


def main():
    print("=" * 62)
    print("网页登录验证配置向导")
    print("=" * 62)

    while True:
        pw = getpass.getpass("设置登录密码（输入时不显示）: ")
        if len(pw) < 8:
            print("  太短了，至少 8 位。")
            continue
        pw2 = getpass.getpass("再输一次确认: ")
        if pw != pw2:
            print("  两次不一致，重来。")
            continue
        break

    session_secret = secrets.token_hex(32)
    totp_secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    pass_hash = hash_password(pw)

    print("\n" + "=" * 62)
    print("把下面三行写进服务器的 .env（替换掉原来的）：")
    print("=" * 62)
    print(f"WEB_AUTH_ENABLED=on")
    print(f"WEB_AUTH_PASS_HASH={pass_hash}")
    print(f"WEB_AUTH_TOTP_SECRET={totp_secret}")
    print(f"WEB_AUTH_SESSION_SECRET={session_secret}")

    print("\n" + "=" * 62)
    print("用手机验证器 App（Google Authenticator / 微软验证器 / 1Password 都行）扫下面这个码：")
    print("=" * 62)
    uri = (f"otpauth://totp/{ISSUER}:{ACCOUNT}?secret={totp_secret}"
           f"&issuer={ISSUER}&digits=6&period=30")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(uri)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print("（没装 qrcode 库，用文字版：pip install qrcode 后重跑可以出二维码）")
    print("\n扫不了就手动输密钥（选「手动输入 / 密钥」类型，时间同步）：")
    print(f"  {totp_secret}")
    print(f"\notpauth 链接：{uri}")

    print("\n" + "=" * 62)
    print("现在手机上的 6 位数字是多少？输进来验一下（直接回车跳过）：")
    code = input("> ").strip()
    if code:
        from auth import verify_totp
        import os
        os.environ["WEB_AUTH_TOTP_SECRET"] = totp_secret
        print("  正确 ✓" if verify_totp(code) else
              f"  不对 ✗ —— 服务器当前算出来是 {totp_code(totp_secret)}，"
              "多半是手机时间没同步，去验证器里点「校正时间」")
    print("\n改完 .env 记得重启服务：sudo systemctl restart friend-ai")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
