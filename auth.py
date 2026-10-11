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
  * { box-sizing: border-box; margin:0; padding:0; }
  body { min-height:100vh; display:flex; align-items:center; justify-content:center;
         padding:20px;
         background:#f3f1ea;
         background-image:radial-gradient(80%% 60%% at 50%% 0%%, #fbfaf6 0%%, #f3f1ea 65%%);
         color:#40473f;
         font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
  .card { width:100%%; max-width:340px; padding:34px 28px 26px; border-radius:20px;
          background:#fbfaf6; border:1px solid #e4e1d5;
          box-shadow:0 20px 60px rgba(90,104,80,.14); }
  .mark { width:44px; height:44px; margin:0 auto 14px; border-radius:14px;
          background:#e3ebe1; color:#83a585;
          display:flex; align-items:center; justify-content:center;
          font-size:22px; }
  h1 { margin:0 0 4px; font-size:20px; font-weight:600; text-align:center; }
  .sub { margin:0 0 24px; font-size:13px; color:#98a096; text-align:center; }
  label { display:block; margin:0 0 6px; font-size:13px; color:#98a096; }
  .field { position:relative; margin:0 0 16px; }
  .field input { width:100%%; padding:12px 13px; border:1px solid #e4e1d5; border-radius:12px;
      background:#fff; color:#40473f; font-size:15px; outline:none; transition:border-color .15s, box-shadow .15s; }
  .field input:focus { border-color:#83a585; box-shadow:0 0 0 3px rgba(131,165,133,.18); }
  input[type=text] { letter-spacing:.35em; font-size:19px; text-align:center; }
  .peek { position:absolute; right:8px; top:50%%; transform:translateY(-50%%);
          background:none; border:0; color:#98a096; font-size:12px; padding:6px;
          cursor:pointer; user-select:none; }
  .peek:hover { color:#83a585; }
  .row { display:flex; align-items:center; gap:8px; margin:0 0 18px; font-size:13px; color:#98a096; }
  .row input { accent-color:#83a585; }
  button[type=submit] { width:100%%; padding:12px; border:0; border-radius:12px;
           background:#83a585; color:#fff; font-size:15px;
           font-weight:500; cursor:pointer; transition:filter .15s, transform .05s; }
  button[type=submit]:hover { filter:brightness(1.06); }
  button[type=submit]:active { transform:translateY(1px); }
  .err { margin:0 0 14px; padding:10px 12px; border-radius:10px; font-size:13px;
         background:#fbecec; color:#d97a7a; }
  .err:empty { display:none; }
  .foot { margin:14px 0 0; text-align:center; font-size:12px; color:#b3b9af; }
  /* --- 皮肤同步：与主页 localStorage('fa-skin') 一致（晨雾默认 / 晚霞 / 月光） --- */
  body.skin-peach { background:#fbf0e7;
    background-image:radial-gradient(80%% 60%% at 50%% 0%%, #fffaf5 0%%, #fbf0e7 65%%);
    color:#54473d; }
  body.skin-peach .card { background:#fffaf5; border-color:#f0ded1;
    box-shadow:0 20px 60px rgba(150,105,80,.14); }
  body.skin-peach .mark { background:#f6dfd1; color:#d89a80; }
  body.skin-peach .sub, body.skin-peach label, body.skin-peach .peek { color:#b09a8d; }
  body.skin-peach .field input { border-color:#f0ded1; background:#fff; color:#54473d; }
  body.skin-peach .field input:focus { border-color:#d89a80; box-shadow:0 0 0 3px rgba(216,154,128,.18); }
  body.skin-peach .peek:hover { color:#d89a80; }
  body.skin-peach .row input { accent-color:#d89a80; }
  body.skin-peach button[type=submit] { background:#d89a80; }
  body.skin-peach .err { background:#f9e8e2; color:#c96f52; }
  body.skin-peach .foot { color:#c8b4a7; }
  body.skin-night { background:#1c2127;
    background-image:radial-gradient(80%% 60%% at 50%% 0%%, #262c33 0%%, #1c2127 65%%);
    color:#edf0ea; }
  body.skin-night .card { background:#262c33; border-color:#39424d;
    box-shadow:0 20px 60px rgba(0,0,0,.4); }
  body.skin-night .mark { background:#364439; color:#a3c2a0; }
  body.skin-night .sub, body.skin-night label, body.skin-night .peek { color:#9ca8a6; }
  body.skin-night .field input { border-color:#39424d; background:#1c2127; color:#edf0ea; }
  body.skin-night .field input:focus { border-color:#a3c2a0; box-shadow:0 0 0 3px rgba(163,194,160,.16); }
  body.skin-night .peek:hover { color:#a3c2a0; }
  body.skin-night .row input { accent-color:#a3c2a0; }
  body.skin-night button[type=submit] { background:#a3c2a0; color:#16200f; }
  body.skin-night .err { background:#3a2626; color:#e08a8a; }
  body.skin-night .foot { color:#5d6a66; }
</style>
</head>
<body>
<script>
  /* 皮肤同步：读主页存的 localStorage('fa-skin')，登录页跟主页面一个皮肤 */
  try{
    var sk = localStorage.getItem('fa-skin');
    if(sk === 'peach' || sk === 'night') document.body.classList.add('skin-' + sk);
  }catch(e){}
</script>
  <form class="card" method="post" action="/api/login" id="f">
    <div class="mark">🌱</div>
    <h1>有生活的朋友</h1>
    <p class="sub">验证身份后才能继续</p>
    <div class="err">%(err)s</div>
    <label for="p">密码</label>
    <div class="field">
      <input type="password" id="p" name="password" autocomplete="current-password" autofocus>
      <span class="peek" data-for="p">显示</span>
    </div>
    %(totp_field)s
    <div class="row"><input type="checkbox" id="r" name="remember" checked>
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
