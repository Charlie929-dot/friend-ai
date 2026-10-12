# 网页端账号管理 · 需求详情

> 归属：需求概要 G 模块。主表一句话见 [需求概要-朋友型AI.md](../需求概要-朋友型AI.md)。

公网部署的门禁配套（用户已部署到服务器）：`/account` 页改密码 + TOTP 动态口令（Google Authenticator 兼容，标准库 RFC 6238 实现）；**验旧凭据 → 写回 .env → 热生效**（不用重启不用手动跑脚本）；改密后轮换 `WEB_AUTH_SESSION_SECRET`（旧会话全失效强制重登）；TOTP 绑定走「生成密钥 → 扫码 → 输码确认」两步；登录页美化（渐变背景/密码显隐/focus ring）。内嵌开源 qrcode-generator（MIT）生成二维码，零后端依赖
