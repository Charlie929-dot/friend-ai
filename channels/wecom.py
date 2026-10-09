"""企业微信自建应用通道（备用通道）。

⚠️ 自用阶段**不再是主载体**（v0.30 定档：自用用网页端 Web UI，服务端常驻本机），
本通道保留作可选备用，不再有新功能投入。

- 发送：应用消息 API（access_token 自动缓存刷新）
- 接收：FastAPI 回调（GET 验证 + POST 收消息），AES-CBC 加解密按官方算法实现
  文档：https://developer.work.weixin.qq.com/document/path/90238

注意：调用发送 API 需要在企业微信后台配置「企业可信 IP」（云服务器公网 IP）。
"""
import base64
import hashlib
import struct
import time
import xml.etree.ElementTree as ET

import httpx

import config
from channels.base import Channel

_API = "https://qyapi.weixin.qq.com/cgi-bin"


# ---------------- 加解密（WXBizMsgCrypt 精简实现） ----------------
class _Crypto:
    def __init__(self, token: str, encoding_aes_key: str, corpid: str):
        from Crypto.Cipher import AES  # pycryptodome，仅本通道用到，延迟导入
        self._aes = AES
        self.token = token
        self.key = base64.b64decode(encoding_aes_key + "=")
        self.iv = self.key[:16]
        self.corpid = corpid

    def _signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        return hashlib.sha1("".join(sorted(
            [self.token, timestamp, nonce, encrypt])).encode()).hexdigest()

    def encrypt(self, reply: str, nonce: str, timestamp: str) -> str:
        """明文 → 加密 XML 中的 Encrypt 字段。"""
        text = (struct.pack("!I", len(nonce.encode())) + nonce.encode()
                + struct.pack("!I", len(reply.encode())) + reply.encode()
                + self.corpid.encode())
        pad = 32 - len(text) % 32
        text += bytes([pad]) * pad
        cipher = self._aes.new(self.key, self._aes.MODE_CBC, self.iv)
        encrypt = base64.b64encode(cipher.encrypt(text)).decode()
        return encrypt

    def decrypt(self, encrypt: str) -> str:
        cipher = self._aes.new(self.key, self._aes.MODE_CBC, self.iv)
        plain = cipher.decrypt(base64.b64decode(encrypt))
        pad = plain[-1]
        plain = plain[:-pad]
        # 结构: 16字节随机串 + 4字节msg_len + msg + corpid
        msg_len = struct.unpack("!I", plain[16:20])[0]
        return plain[20:20 + msg_len].decode()

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str,
                   echostr: str) -> str:
        if self._signature(timestamp, nonce, echostr) != msg_signature:
            raise ValueError("签名校验失败")
        return self.decrypt(echostr)

    def check_signature(self, msg_signature: str, timestamp: str, nonce: str,
                        encrypt: str) -> None:
        if self._signature(timestamp, nonce, encrypt) != msg_signature:
            raise ValueError("签名校验失败")


# ---------------- 通道实现 ----------------
class WecomChannel(Channel):
    def __init__(self):
        self._token_cache: tuple[str, float] | None = None
        self._crypto = _Crypto(config.WECOM_TOKEN,
                               config.WECOM_ENCODING_AES_KEY,
                               config.WECOM_CORPID)

    # --- 发送 ---
    def _access_token(self) -> str:
        if self._token_cache and self._token_cache[1] > time.time() + 60:
            return self._token_cache[0]
        resp = httpx.get(f"{_API}/gettoken", params={
            "corpid": config.WECOM_CORPID,
            "corpsecret": config.WECOM_CORPSECRET,
        }, timeout=15).json()
        if resp.get("errcode") != 0:
            raise RuntimeError(f"gettoken 失败: {resp}")
        self._token_cache = (resp["access_token"], time.time() + resp["expires_in"])
        return self._token_cache[0]

    def send(self, text: str) -> None:
        resp = httpx.post(
            f"{_API}/message/send",
            params={"access_token": self._access_token()},
            json={
                "touser": config.WECOM_USER_ID,
                "msgtype": "text",
                "agentid": int(config.WECOM_AGENTID),
                "text": {"content": text},
            }, timeout=15).json()
        if resp.get("errcode") != 0:
            print(f"[wecom] 发送失败: {resp}")

    # --- 接收：FastAPI 应用 ---
    def run(self, on_message) -> None:
        import uvicorn
        from fastapi import FastAPI, Query, Request
        from fastapi.responses import PlainTextResponse

        app = FastAPI()
        crypto = self._crypto

        @app.get("/callback")
        def verify(msg_signature: str = Query(...), timestamp: str = Query(...),
                   nonce: str = Query(...), echostr: str = Query(...)):
            return PlainTextResponse(
                crypto.verify_url(msg_signature, timestamp, nonce, echostr))

        @app.post("/callback")
        async def receive(request: Request, msg_signature: str = Query(...),
                          timestamp: str = Query(...), nonce: str = Query(...)):
            body = await request.body()
            root = ET.fromstring(body)
            encrypt = root.findtext("Encrypt", "")
            crypto.check_signature(msg_signature, timestamp, nonce, encrypt)
            msg = ET.fromstring(crypto.decrypt(encrypt))
            if msg.findtext("MsgType") == "text":
                text = msg.findtext("Content", "")
                # 异步处理，立即回 success（避免 5 秒超时重推）
                import threading
                threading.Thread(target=on_message, args=(text,), daemon=True).start()
            return PlainTextResponse("success")

        uvicorn.run(app, host="0.0.0.0", port=8000)
