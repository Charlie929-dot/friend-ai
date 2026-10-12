# AGENTS.md — 给 AI 协作者的改动维护清单

> 这是**朋友型 AI**（friend-ai）项目。核心特征：**主动**——它不等指令，有自己的作息、工作、记忆和生活节奏。  
> **每次改完代码，下面这些地方必须同步维护。只改代码不改这些 = 改动没做完。**  
> 完整需求见 `docs/需求概要-朋友型AI.md`，历史见 `docs/历史归档-版本与实测.md`。  
> **当前版本：v0.82**（文档可维护性优化·方案 A：各文档加职责声明 + 单一事实源规则 + README 修错）。

---

## 一、改完并测试完代码必做（按顺序，缺一不可）

| # | 做什么              | 具体位置                                               | 完成标准                                                          |
| - | ---------------- | -------------------------------------------------- | ------------------------------------------------------------- |
| 1 | **提交推送**         | `git add -A && git commit && git push origin main` | commit message 用中文，一句说清改了什么                                   |
| 2 | **同步服务器**        | `bash sync-to-server.sh`（见下方「服务器同步工作流」）            | 自检输出 HEAD=本机 commit、`GET /login -> 200`                       |
| 3 | **更新需求文档**       | `docs/需求概要-朋友型AI.md`                               | 顶部加一条版本记录（v0.x 递增）+ 需求池对应条目状态变了要改                             |
| 4 | **补实测反馈**        | append 到 `docs/历史归档-版本与实测.md`，并在主文档「附：实测反馈索引」加一行   | 有实测发现/踩坑才写，纯重构可跳过                                             |
| 5 | **更新 AGENTS.md** | 根目录 `AGENTS.md`                                    | 项目推进 → 同步「当前进度速查」；工作流变更 → 同步「改完并测试完代码必做」；文件对照表变化 → 同步 `docs/文件改动同步清单.md` |

---

## 〇、服务器同步工作流（自用阶段服务端跑服务器）

用户已把服务端部署到自己的服务器（腾讯云 ubuntu@150.158.147.51，密钥 `~/.ssh/tunnel_key`，代码目录 `/opt/friend-ai`，systemd 服务 `friend-ai`，网页 <https://derp.charlie2kabu.xyz:8443/>）。**改完代码必须同步到服务器**，本机只留开发副本。

- **同步命令**：`bash sync-to-server.sh`。流程 = ①检查未提交改动 → ②双推 GitHub+Gitee → ③服务器从 Gitee 拉 → ④装依赖+重启 → ⑤自检（HEAD + `GET /login`）。
- ⚠️ **必须先加载令牌**：脚本要 `GH_TOKEN` / `GITEE_TOKEN`（在 `~/.bash_profile`）。非交互 shell 不自动 source，直接跑会「跳过双推」、服务器拉不到代码。所以完整命令是：
  ```bash
  source ~/.bash_profile && bash sync-to-server.sh
  ```
- **数据库以服务器为准**：服务器 `friend.db` 是线上聊天记录的唯一真相。本地库只作开发调试，别拿本地库覆盖服务器。
- **embedding 模型同步**：语义召回模型（bge-small-zh-v1.5 ONNX，约 100MB）**不进 git**。⚠️ 服务器（腾讯云）连 hf-mirror 不通（curl 超时），所以走「**本机下载 → scp 上传**」——`sync-to-server.sh` 第 4 步会自动 scp（服务器没有 model.onnx 时才传）。本机首次下载（换电脑/新 clone 时跑一次）：
  ```bash
  mkdir -p models/bge-small-zh-v1.5 && cd models/bge-small-zh-v1.5
  curl -sL -o model.onnx https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/onnx/model.onnx
  curl -sL -o config.json https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/config.json
  curl -sL -o tokenizer.json https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/tokenizer.json
  curl -sL -o vocab.txt https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/vocab.txt
  ```
- **服务器操作备忘**：
  - SSH：`ssh -i ~/.ssh/tunnel_key ubuntu@150.158.147.51`
  - 查库（服务器无 sqlite3 CLI，用 `.venv/bin/python -c "import sqlite3; ..."`）
  - 查服务：`systemctl status friend-ai`；重启：`sudo systemctl restart friend-ai`
  - 改 `.env` 后 `systemctl restart` 生效（`config.py` 自带 `load_dotenv`，启动时读）
  - 备份 db：`cp friend.db friend.db.bak.<日期-时分>`（平铺在项目根，无 backups/ 目录）

---


## 二、改了哪个文件 → 要同步哪里

📄 **完整对照表单独维护 → [docs/文件改动同步清单.md](./docs/文件改动同步清单.md)**（按文件逐行查"必须同步什么"，表尾有「通用规则」）。

用法：
1. 改完某个文件 → 先查表，把该行"必须同步"里的项做掉（测试 / 文档 / 语料 / 配置示例…）
2. **表里没有的文件** → 按该文档「通用规则」处理，并顺手给表补一行（目标是覆盖所有常改文件）
3. AGENTS.md 本身只在**工作流/进度**变化时才改；文件行不再往这加

---

## 三、项目硬性约定（违反就是 bug）

1. **一视同仁（宗旨）** —— AI friend 是独立个体，**不知道"用户/主人"这种特殊身份**，平等对待所有其他人（包括你）。记忆、日记、称呼都不围绕用户特殊化：TA 记的是对自己有趣/重要/相关的事；日记写的是 TA 自己的一天，不是"和你相处的一天"。代码里别出现"为了你""为你定制"这类主语。
2. **工具拟人化 + 两层分离** —— 工具不是外部 API，是 TA 自己的"身体组分"（记忆检索 = 大脑回忆、行为接口 = 作息意愿）。描述用"我可以回想"、返回用"想起来了/回忆不起来了"，**不出现"搜索/检索/数据库/工具"类词**。  
   **但拟人化只管体感外壳**：function calling 的契约内核——何时调、调哪个、参数枚举、返回结构、标记 token（如长文标记）——必须保留清晰。**不能为了拟人把契约词改掉**，否则 AI 找不到触发条件、调用失败。
3. **语料只装"会扩充"的两类** —— 固定回复语 + 匹配关键词/正则。不会扩充的固定文案（工具 description、工具返回固定句）就近在业务模块维护，**不要塞进 corpus**。业务模块不硬编码匹配词表。
4. **称呼不写死** —— 每个朋友有自己 profile 里的"称呼"字段（事实便签"叫我X" > profile 称呼 > `config.USER_NAME` 兜底），不同朋友可以有个性称呼。
5. **代价只呈现、不归因** —— costly 状态只说自己的状态（"今天读得晚"），**禁止"你打断我/因为你/又被你/我为了你"** 这类把代价指向对方的表达。这是硬红线。
6. **内部 LLM 调用不拼完整人设** —— 记忆提取、素材生成这类"后台"调用一律用 `persona.core_identity_prompt()`，**不要用 `build_system_prompt()`**。完整人设里塞的动态段（时间/状态/手头的事）只要有一个会触发新的 LLM 调用就成环递归爆栈（实测 #17 已踩）。
7. **Windows 上别用 `os.kill(pid, 0)` 探活** —— 它不等于 POSIX 的信号 0，CPython 会直接 `TerminateProcess`，**是杀人不是探活**。查进程活着用 `autostart._pid_alive()`（`OpenProcess` + `GetExitCodeProcess`）。
8. **注册计划任务别用 `schtasks.exe`** —— 部分终端（含受限会话）里它连 `/query` 都返回 "The system cannot find the path specified"。统一走 `autostart._ps()`（PowerShell 的 `ScheduledTasks` cmdlet）。
9. **profile 自动填写要留痕、稳定字段不静默覆盖** —— AI 自动填 profile 字段时，上下文证据存 **B 库**（`long_memories`），**A 库记忆挂 `long_id` 链接**；`/profile why <字段>` 看证据原文。**稳定字段**（`memory.STABLE_PROFILE_FIELDS`：作息/生日/城市/称呼等身份级）遇到与旧值冲突的新说法**不静默覆盖**，标 `pending` 待用户确认（`/profile pending/accept/reject`），并由 `persona._pending_profile_hint()` 注入提示让 AI 主动跟用户核实（实测 #21）。可变字段（在忙的事等）可照旧更新。
10. **测试必须与"现在几点"解耦**（实测 #27 的教训）—— fresh DB 在工作时段必然判"离线专注"（消息进 inbox 不回话），所以：测 Engine 交互（打字/插话）的用例先 `config.WORK_ENABLED = False`（finally 恢复）；用锚点时刻断言排班的用例预置 `rest_schedule`（wake 早于锚点）与随机浮动解耦；手工构造 `work_state` fixture 必须带 `"planned": 1`（缺了会被新逻辑当"没排过班"重排覆盖）。
11. **测试临时产物不许落项目根** —— 测试统一走 `test_p1_features.py` 顶部的 `_TEST_TMP_ROOT`（固定目录 `.test-tmp/`，已 gitignore、**启动即清 + 退出清**双保险）；**别再裸用 `tempfile.mkdtemp()` 当根目录**——某些环境 `%TEMP%` 不可写会回退到**当前工作目录**，跑几次就在项目根撒一地 `tmpXXXX`（历史债：曾攒到 1100+ 个 / 56MB）。
12. **需求池「状态」打勾纪律**（2026-10-11 用户明确；起因：「表情包池 img 仅预留字段却整条 ✅」「引用对话已实现却仍挂待做」两处账目错）——每次改代码更新需求文档时：①**逐子项核对**：条目说明里每一句都要与状态对得上，说明含「**预留 / 待做 / 遗留 / TODO / 占位 / 下一步**」字样的部分**不许整条打 ✅**——要么拆成独立子条目、要么标「✅ 部分（剩余是 …）」；②**反向也查**：顺手扫一遍相关模块，已实现却还挂「待做」的改掉；③**版本记录只写做了的事**：预留字段 ≠ 实现功能，记录里必须写明「本版仅预留、功能未实现」；④状态列用复合标注（如「✅ 文字+图」「✅ 承诺提醒」）时，未做的部分要在说明里显式列出。

---

## 五、当前进度速查

| 阶段            | 主题        | 状态              |
| ------------- | --------- | --------------- |
| S1 能聊         | 先跑起来      | ✅ 已收官           |
| S2 像人         | 说话像、记得住   | ✅ 已收官           |
| **S3 有自己的生活** | 它有边界、有日子过 | ✅ 已收尾           |
| S4 越来越懂你      | 召回准、会关心   | 🔵 进行中（承诺提醒/语义召回/人格优化/语气主轨/自我意识流 已做） |
| S5 底线与边界      | 合规护栏      | ⏸ 自用阶段暂缓        |

**S3 建议顺序**：①✅ 紧急联系触发语义 → ②✅ 工作系统（阅读系统，`reading.py` 素材库 + `work.py` 读一篇产素材）→ ③✅ 打断成本 + HUD（含心情维度）→ ④✅ 对话中主动延续（人设 prompt 引导）→ ⑤✅ 休眠完整版（离线留言 inbox + 离线=工作专注 + 作息自主，`offline.py`）。**S3 主线收尾**。S4 已做：承诺提醒（`events.py`）→ 语义化召回（`embeddings.py` 本地向量）→ 今日上下文 + 艾宾浩斯衰减 → 人格内核 V3 + 反谄媚 + 双层生成 + 自我记忆（`selfmem.py`）→ **语气主轨（`tone.py`，v0.56）**→ **自我意识流（`stream.py`，v0.57 第一步 + v0.58 睡前浓缩）**。**S4 已做（v0.77）：书库分类改版（22 类映射表 + 网页端书架批量上传/选标签 + 人格阅读偏好两级）**。**S4 下一步：收敛散件（把 work/mood/selfmem/独白 统一成一条流，属优化、会牵动主动消息路径）→ 表情包池（E）→ self_status 工具（F）**；并与**网页端 Web UI** 并行推进。
