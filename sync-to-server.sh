#!/bin/bash
# 把本机已提交的代码同步到公网服务器。
#
# 两条路径，自动选：
#   A. 服务器从 Gitee 拉（国内秒连，首选）—— 所以要先双推到 GitHub + Gitee
#   B. Gitee 拉失败时，退回 SSH 直传 git bundle（完全不依赖外网）
#
# 用法：在本机 Git Bash 里执行   bash sync-to-server.sh
# 前提：改动已经 commit 到 main 分支（未提交的改动不会被同步）
set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
KEY="$HOME/.ssh/tunnel_key"
HOST="ubuntu@150.158.147.51"
REMOTE_DIR="/opt/friend-ai"
BUNDLE="/tmp/friend-ai.bundle"
SSHOPT="-i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"

# 凭据从环境变量读，避免写死在脚本里（被公开就麻烦了）
GH_TOKEN="${GH_TOKEN:-}"
GITEE_TOKEN="${GITEE_TOKEN:-}"

cd "$REPO"

echo "== 1/5 检查未提交的改动 =="
DIRTY="$(git status --porcelain)"
if [ -n "$DIRTY" ]; then
  echo "以下改动还没提交，不会被同步："
  echo "$DIRTY" | head -10
  echo "(确认不需要就继续；需要的话先 git add + git commit)"
fi
echo "本机 HEAD: $(git log --oneline -1)"

echo "== 2/5 推送到 GitHub + Gitee =="
if [ -n "$GH_TOKEN" ]; then
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 \
  git -c http.proxy=http://127.0.0.1:7897 push \
    "https://Charlie929-dot:${GH_TOKEN}@github.com/Charlie929-dot/friend-ai.git" main 2>&1 | tail -2
else
  echo "跳过 GitHub（未设 GH_TOKEN，如：export GH_TOKEN=ghp_xxx）"
fi
if [ -n "$GITEE_TOKEN" ]; then
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 \
  git push "https://Charlie929-dot:${GITEE_TOKEN}@gitee.com/Charlie929-dot/friend-ai.git" main 2>&1 | tail -2
else
  echo "跳过 Gitee（未设 GITEE_TOKEN，如：export GITEE_TOKEN=xxx）"
fi

echo "== 3/5 服务器从 Gitee 拉取 =="
PULL_OK=1
ssh $SSHOPT "$HOST" "cd $REMOTE_DIR && git checkout -- . 2>/dev/null; git pull --ff-only gitee main >/tmp/pull.out 2>&1; rc=\$?; tail -3 /tmp/pull.out; exit \$rc" || PULL_OK=0

if [ "$PULL_OK" != "1" ]; then
  echo "Gitee 拉取失败，退回 SSH 直传 bundle"
  echo "== 3b 打包上传 =="
  rm -f "$BUNDLE"
  git bundle create "$BUNDLE" main >/dev/null
  scp $SSHOPT "$BUNDLE" "$HOST:/tmp/"
  ssh $SSHOPT "$HOST" "cd $REMOTE_DIR && git pull /tmp/friend-ai.bundle main 2>&1 | tail -3"
fi

echo "== 4/5 装依赖 + 同步 embedding 模型 + 重启 =="
ssh $SSHOPT "$HOST" "
  cd $REMOTE_DIR
  .venv/bin/pip install -q -r requirements.txt 2>&1 | tail -2 || true
  mkdir -p models/bge-small-zh-v1.5
"
# 语义召回模型（bge-small-zh-v1.5 ONNX，约 100MB）不进 git。
# ⚠️ 服务器（腾讯云）连 hf-mirror 不通（curl 超时），所以不走"服务器自己下载"，
# 改从本机 scp 直传（本机 models/ 已下好；没有就先在本机跑 download 一步）。
if ssh $SSHOPT "$HOST" "test -f $REMOTE_DIR/models/bge-small-zh-v1.5/model.onnx"; then
  echo 'embedding 模型已存在，跳过上传'
else
  echo '上传 embedding 模型（scp 直传）…'
  scp $SSHOPT \
    "$REPO/models/bge-small-zh-v1.5/model.onnx" \
    "$REPO/models/bge-small-zh-v1.5/config.json" \
    "$REPO/models/bge-small-zh-v1.5/tokenizer.json" \
    "$REPO/models/bge-small-zh-v1.5/vocab.txt" \
    "$HOST:$REMOTE_DIR/models/bge-small-zh-v1.5/"
fi
ssh $SSHOPT "$HOST" "sudo systemctl restart friend-ai"

echo "== 5/5 自检 =="
ssh $SSHOPT "$HOST" "
  sleep 5
  echo -n '服务状态: '; systemctl is-active friend-ai
  echo -n 'HEAD: '; git -C $REMOTE_DIR log --oneline -1
  curl -s -o /dev/null -w 'GET /login -> %{http_code}\n' -k --max-time 8 \
    --resolve derp.charlie2kabu.xyz:8443:127.0.0.1 \
    https://derp.charlie2kabu.xyz:8443/login
"
echo "完成：https://derp.charlie2kabu.xyz:8443/"
