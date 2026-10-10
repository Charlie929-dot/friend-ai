"""网页端登录验证（S5：公网部署时给网页加一道门）。

本机自用（WEB_HOST=127.0.0.1）时完全不生效——`WEB_AUTH_ENABLED` 默认 off，
一行代码都不挡。只有在服务器上显式打开才启用，避免本地开发被自己挡住。

验证方式：**密码 + TOTP 动态口令**（手机验证器 App，和 Google Authenticator 兼容）。
密钥全部走环境变量，不进 git；这里只有验证逻辑，泄露了也进不去。

TOTP 用标准库实现（RFC 6238：HMAC-SHA1 / 30s / 6 位），故意不引 pyotp——
自用项目少一个依赖少一件事，且这段逻辑二十年不会变。
"""
import base64
import hashlib
import hmac
import os
import struct
import time

# --- 环境变量（全部可选，缺哪个就降级/关掉哪部分） ---
ENABLED = os.environ.get("WEB_AUTH_ENABLED", "off").lower() == "on"
PASS_HASH = os.environ.get("WEB_AUTH_PASS_HASH", "")       # pbkdf2_sha256$<salt>$<hash>
TOTP_SECRET = os.environ.get("WEB_AUTH_TOTP_SECRET", "")   # base32，验证器扫码用
SESSION_SECRET = os.environ.get("WEB_AUTH_SESSION_SECRET", "")
SECURE_COOKIE = os.environ.get("WEB_AUTH_COOKIE_SECURE", "off").lower() == "on"

COOKIE_NAME = "fa_session"
PBKDF2_ROUNDS = 200_000
TOTP_STEP = 30
TOTP_DIGITS = 6

# --- 暴力破解防护：内存计数，进程重启即清零（自用足够） ---
_FAILED: dict = {}          # ip -> [失败次数, 解禁时间戳]
_MAX_FAIL = 5
_LOCK_SEC = 600


def enabled() -> bool:
    """只有显式开启且设了密码哈希才真正生效。"""
    return ENABLED and bool(PASS_HASH)


def totp_enabled() -> bool:
    return enabled() and bool(TOTP_SECRET)


# ---------- 密码 ----------
def hash_password(pw: str, salt: bytes = None) -> str:
    """生成 `pbkdf2_sha256$<salt_hex>$<hash_hex>`，存进 .env 的 WEB_AUTH_PASS_HASH。"""
    salt = salt or os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${salt.hex()}${h.hex()}"


def verify_password(pw: str) -> bool:
    if not PASS_HASH:
        return False
    try:
        algo, salt_hex, hash_hex = PASS_HASH.split("$")
        if algo != "pbkdf2_sha256":
            return False
        h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                                bytes.fromhex(salt_hex), PBKDF2_ROUNDS)
        return hmac.compare_digest(h.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ---------- TOTP（RFC 6238，与 Google Authenticator / 微软验证器兼容） ----------
def _b32decode(secret: str) -> bytes:
    s = secret.upper().replace(" ", "").replace("-", "")
    pad = "=" * ((8 - len(s) % 8) % 8)
    return base64.b32decode(s + pad)


def totp_code(secret: str, at: int = None) -> str:
    """当前时刻的 6 位动态码（调试/自测用）。"""
    at = at if at is not None else int(time.time())
    counter = at // TOTP_STEP
    key = _b32decode(secret)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    off = digest[19] & 0x0F
    val = struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF
    return str(val % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def verify_totp(code: str, secret: str | None = None) -> bool:
    """校验动态码，容忍前后各一个时间窗（±30s），抵消手机和服务器的时间差。
    secret 缺省用当前已启用的密钥；传参 = 校验"还没启用"的新密钥（绑定确认流程）。"""
    s = TOTP_SECRET if secret is None else secret
    code = (code or "").strip().replace(" ", "")
    if not s or not code.isdigit() or len(code) != TOTP_DIGITS:
        return False
    now = int(time.time())
    for delta in (-TOTP_STEP, 0, TOTP_STEP):
        if hmac.compare_digest(totp_code(s, now + delta), code):
            return True
    return False


# ---------- 会话 cookie ----------
def _sign(payload: str) -> str:
    if not SESSION_SECRET:
        return ""
    return hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def issue_cookie(days: int = 30) -> str:
    exp = int(time.time()) + days * 86400
    payload = str(exp)
    return f"{payload}.{_sign(payload)}"


def check_cookie(cookie_header: str) -> bool:
    if not SESSION_SECRET:
        return False
    for part in (cookie_header or "").split(";"):
        part = part.strip()
        if not part.startswith(COOKIE_NAME + "="):
            continue
        val = part[len(COOKIE_NAME) + 1:]
        if "." not in val:
            continue
        payload, sig = val.rsplit(".", 1)
        if not payload.isdigit():
            continue
        if not hmac.compare_digest(_sign(payload), sig):
            continue
        if int(payload) < time.time():
            continue
        return True
    return False


def cookie_header(days: int = 30) -> str:
    attrs = f"{COOKIE_NAME}={issue_cookie(days)}; Path=/; HttpOnly; SameSite=Lax; Max-Age={days * 86400}"
    if SECURE_COOKIE:
        attrs += "; Secure"
    return attrs


# ---------- 限流 ----------
def locked(ip: str) -> bool:
    rec = _FAILED.get(ip)
    return bool(rec and rec[0] >= _MAX_FAIL and time.time() < rec[1])


def note_failure(ip: str):
    rec = _FAILED.get(ip, [0, 0])
    if time.time() > rec[1]:      # 上一段锁已过期，重新计
        rec = [0, 0]
    rec[0] += 1
    if rec[0] >= _MAX_FAIL:
        rec[1] = time.time() + _LOCK_SEC
    _FAILED[ip] = rec


def note_success(ip: str):
    _FAILED.pop(ip, None)


# ---------- 运行时更新（网页账号管理用） ----------
# 凭据仍以 .env 为唯一真相；这里做的是"验证 + 写回 .env + 热生效"，
# 改密码/重绑 TOTP 不用再上服务器跑脚本、不用重启服务。

ISSUER = "friend-ai"
ACCOUNT_LABEL = "有生活的朋友"


def write_env(updates: dict) -> None:
    """把 WEB_AUTH_* 的更新写回 .env（其他行原样保留，别碰 API key）。"""
    path = _env_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, seen = [], set()
    for line in lines:
        key = (line.split("=", 1)[0].strip()
               if "=" in line and not line.lstrip().startswith("#") else None)
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for k in sorted(set(updates) - seen):
        out.append(f"{k}={updates[k]}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _env_path():
    import config
    return config.BASE_DIR / ".env"


def _refresh() -> None:
    """环境变量 → 本模块内存值（热生效）。"""
    global ENABLED, PASS_HASH, TOTP_SECRET, SESSION_SECRET
    ENABLED = os.environ.get("WEB_AUTH_ENABLED", "off").lower() == "on"
    PASS_HASH = os.environ.get("WEB_AUTH_PASS_HASH", "")
    TOTP_SECRET = os.environ.get("WEB_AUTH_TOTP_SECRET", "")
    SESSION_SECRET = os.environ.get("WEB_AUTH_SESSION_SECRET", "")


def _apply_env(updates: dict) -> None:
    for k, v in updates.items():
        os.environ[k] = v
    write_env(updates)
    _refresh()


def change_password(new_pw: str) -> None:
    _apply_env({"WEB_AUTH_PASS_HASH": hash_password(new_pw)})


def rotate_session_secret() -> str:
    """换会话签名密钥：改密码后调用，所有旧会话 cookie 立即失效（要重新登录）。"""
    secret = base64.b32encode(os.urandom(24)).decode().rstrip("=")
    _apply_env({"WEB_AUTH_SESSION_SECRET": secret})
    return secret


def new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def otpauth_uri(secret: str) -> str:
    """验证器扫码用的 otpauth:// 链接（Google Authenticator / 微软验证器兼容）。"""
    from urllib.parse import quote
    return (f"otpauth://totp/{quote(ACCOUNT_LABEL)}?secret={secret}"
            f"&issuer={quote(ISSUER)}&digits=6&period=30")


def activate_totp(secret: str) -> None:
    _apply_env({"WEB_AUTH_TOTP_SECRET": secret.strip().replace(" ", "").upper()})


def disable_totp() -> None:
    _apply_env({"WEB_AUTH_TOTP_SECRET": ""})


# ---------- 登录页 ----------
LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · 有生活的朋友</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#0e1016;
         background-image:radial-gradient(60%% 50%% at 50%% 0%%, #1a2233 0%%, #0e1016 70%%);
         color:#e8e6e3;
         font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
  .card { width:100%%; max-width:340px; padding:32px 26px 26px; border-radius:16px;
          background:#1b1e26; border:1px solid #2a2e39;
          box-shadow:0 12px 40px rgba(0,0,0,.5); }
  .mark { width:40px; height:40px; margin:0 auto 14px; border-radius:12px;
          background:linear-gradient(135deg,#4a6cf7,#7a5cff);
          display:flex; align-items:center; justify-content:center;
          font-size:20px; color:#fff; }
  h1 { margin:0 0 4px; font-size:19px; font-weight:600; text-align:center; }
  .sub { margin:0 0 24px; font-size:13px; color:#8b8f9a; text-align:center; }
  label { display:block; margin:0 0 6px; font-size:13px; color:#a8adb8; }
  .field { position:relative; margin:0 0 16px; }
  .field input { width:100%%; padding:11px 12px; border:1px solid #2e323d; border-radius:10px;
      background:#12141a; color:#e8e6e3; font-size:15px; outline:none; transition:border-color .15s; }
  .field input:focus { border-color:#4a6cf7; box-shadow:0 0 0 3px rgba(74,108,247,.18); }
  input[type=text] { letter-spacing:.35em; font-size:19px; text-align:center; }
  .peek { position:absolute; right:6px; top:50%%; transform:translateY(-50%%);
          background:none; border:0; color:#8b8f9a; font-size:12px; padding:6px;
          cursor:pointer; user-select:none; }
  .peek:hover { color:#c8ccd6; }
  .row { display:flex; align-items:center; gap:8px; margin:0 0 18px; font-size:13px; color:#a8adb8; }
  button[type=submit] { width:100%%; padding:12px; border:0; border-radius:10px;
           background:linear-gradient(135deg,#4a6cf7,#5b7bff); color:#fff; font-size:15px;
           font-weight:500; cursor:pointer; transition:filter .15s, transform .05s; }
  button[type=submit]:hover { filter:brightness(1.1); }
  button[type=submit]:active { transform:translateY(1px); }
  .err { margin:0 0 14px; padding:9px 12px; border-radius:8px; font-size:13px;
         background:rgba(255,86,86,.12); color:#ff8080; }
  .err:empty { display:none; }
  .foot { margin:14px 0 0; text-align:center; font-size:12px; color:#5d616c; }
</style>
</head>
<body>
  <form class="card" method="post" action="/api/login" id="f">
    <div class="mark">🌙</div>
    <h1>有生活的朋友</h1>
    <p class="sub">验证身份后才能继续</p>
    <div class="err">%(err)s</div>
    <label for="p">密码</label>
    <div class="field">
      <input type="password" id="p" name="password" autocomplete="current-password" autofocus>
      <span class="peek" data-for="p">显示</span>
    </div>
    %(totp_field)s
    <div class="row"><input type="checkbox" id="r" name="remember" checked style="width:auto">
      <label for="r" style="margin:0">记住这台设备 30 天</label></div>
    <button type="submit">进 入</button>
    <p class="foot">密码 + 动态口令双保险</p>
  </form>
<script>
  document.querySelectorAll('.peek').forEach(function(el){
    el.addEventListener('click', function(){
      var inp = document.getElementById(el.dataset.for);
      var show = inp.type === 'password';
      inp.type = show ? 'text' : 'password';
      el.textContent = show ? '隐藏' : '显示';
    });
  });
</script>
</body>
</html>"""

TOTP_FIELD = """<label for="t">动态口令</label>
    <div class="field">
      <input type="text" id="t" name="totp" inputmode="numeric" pattern="[0-9]*"
             maxlength="6" placeholder="6 位" autocomplete="one-time-code">
    </div>"""


def login_page(err: str = "") -> str:
    return LOGIN_PAGE % {"err": err, "totp_field": TOTP_FIELD if totp_enabled() else ""}
