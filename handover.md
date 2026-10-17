<!-- dsh-taskclock-handover:start -->
# 任务交接

> 更新于 2026-10-17 20:15:00 · 修订 1 · 2 个会话小节 · 项目规则见 AGENTS.md

<!-- dsh-taskclock-handover:snapshot:start -->
<!-- dsh-taskclock-handover:session:infra -->
### 会话 `infra` · 2026-10-17 20:15:00

> 第 0 轮 · 修订 1（基于 0） · 触发：目录合并收尾

## 当前进度
- 目标：项目根固定到 `C:\Users\HP\AI_Companion`，从新根继续维护。
- 已完成：三份副本合并（旧副本归档 `C:\Users\HP\AI_Companion_archive`）；venv 随目录移动可用（`sys.prefix`/`BASE_DIR` 均为新根）；web 从新根恢复（8787，`GET /` 200、`/api/state` 200）；全量 92/92；修复 `autostart._startup_vbs` 多一个引号的 bug（Startup VBS 已验证能拉起 daemon）。
- 正在进行：（无）
- 剩余：①push→`sync-to-server.sh`→手机二次验收 ②旧计划任务 `friend-ai-web` 需管理员删/重注册（当前自启走 Startup VBS，二选一）③`C:\Users\HP\AI_Companion_archive` 确认无用后可删。
- 下一步：`source ~/.bash_profile && bash sync-to-server.sh`，然后手机二次验收。

<!-- dsh-taskclock-handover:session:seed -->
### 会话 `seed` · 2026-10-17 19:50:00

> 第 0 轮 · 修订 1（基于 0） · 触发：旧交接迁移（旧文件已删）

## 当前进度
- 目标：v0.108 移动端实测打磨已落地并已 commit（含本次交接迁移提交），待 push→同步服务器 + 手机二次验收。
- 已完成：v0.108 五项——记忆管理全屏化（`bk-dialog` ≤640 铺满）、消息操作轻触才显示（`hover:none` 下 `.msg-actions` 默认隐藏、轻触该条加 `.show-acts`）、目录独立窗口（修 `#btnOutline` 内联 `display:none` 压过媒体查询的根因）、顶栏收拢（≤640 缩间距/≤520 隐藏状态行）、输入区瘦身 + `visualViewport` 键盘顶起（`--kb`）；全量 92/92 过，本地起 web 自检 `GET /` 200、页面关键元素齐全、div 标签平衡。
- 正在进行：（无）
- 剩余：①push→`sync-to-server.sh`→手机二次验收（用户做）②两个低频偶发未修（`test_life.test_proactive_guard_and_energy_rest`、`test_persona.test_tone_main_track`，疑同源于 `life._make_sessions` 的 random 抖动，动它前先复现）③「项目进度维护」路线待拍板；「用户状态感知 LLM」候选暂不做。
- 下一步：`source ~/.bash_profile && bash sync-to-server.sh` 推送并同步服务器，再做手机二次验收（五条：记忆管理全屏、操作轻触、目录按钮/全屏窗口、顶栏不挤、键盘顶起）。
<!-- dsh-taskclock-handover:snapshot:end -->
<!-- dsh-taskclock-handover:end -->
