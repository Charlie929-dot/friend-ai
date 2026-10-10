#!/bin/bash
# 把本机已提交的代码同步到公网服务器。
# 走 SSH 直传 git bundle，不依赖服务器访问 GitHub（服务器直连 GitHub 经常超时）。
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

cd "$REPO"

echo "== 1/4 检查未提交的改动 =="
DIRTY="$(git status --porcelain)"
if [ -n "$DIRTY" ]; then
  echo "以下改动还没提交，不会被同步："
  echo "$DIRTY" | head -10
  echo "(确认不需要就继续；需要的话先 git add + git commit)"
fi
echo "本机 HEAD: $(git log --oneline -1)"

echo "== 2/4 打包 =="
rm -f "$BUNDLE"
git bundle create "$BUNDLE" main >/dev/null
ls -lh "$BUNDLE" | awk '{print "bundle 大小:", $5}'

echo "== 3/4 上传 =="
scp $SSHOPT "$BUNDLE" "$HOST:/tmp/"

echo "== 4/4 服务器端应用并重启 =="
ssh $SSHOPT "$HOST" "
  cd $REMOTE_DIR
  git checkout -- . 2>/dev/null || true
  git pull /tmp/friend-ai.bundle main 2>&1 | tail -3
  .venv/bin/pip install -q -r requirements.txt 2>&1 | tail -2 || true
  sudo systemctl restart friend-ai
  sleep 5
  echo -n '服务状态: '; systemctl is-active friend-ai
  curl -s -o /dev/null -w 'GET /login -> %{http_code}\n' -k --max-time 8 \
    --resolve derp.charlie2kabu.xyz:8443:127.0.0.1 \
    https://derp.charlie2kabu.xyz:8443/login
"
echo "完成：https://derp.charlie2kabu.xyz:8443/"
