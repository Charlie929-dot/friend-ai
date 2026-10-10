#!/bin/bash
# friend-ai 服务器更新：从 GitHub 拉最新代码 -> 装依赖 -> 重启 -> 自检
# 前提：本机改动已经 push 到 GitHub main 分支
set -uo pipefail
cd /opt/friend-ai || exit 1

echo "== 1/5 备份数据库 =="
if [ -f friend.db ]; then
  cp -f friend.db "friend.db.bak.$(date +%m%d-%H%M)" && echo "已备份 friend.db"
fi

echo "== 2/5 拉取代码 =="
# 显式从 gitee 拉：服务器（腾讯云）直连 GitHub 经常超时，Gitee 国内秒连
if ! git pull --ff-only gitee main 2>&1 | tail -3; then
  echo "拉取失败：服务器本地有改动。"
  echo "确认这些改动不重要，可执行：cd /opt/friend-ai && git checkout -- . && git pull"
  exit 1
fi

echo "== 3/5 更新依赖 =="
.venv/bin/pip install -q -r requirements.txt 2>&1 | tail -3
echo "依赖就绪"

echo "== 4/5 重启服务 =="
sudo systemctl restart friend-ai
sleep 5

echo "== 5/5 自检 =="
echo -n "服务状态: "; systemctl is-active friend-ai
curl -s -o /dev/null -w "GET /login -> %{http_code}\n" -k --max-time 8 \
  --resolve derp.charlie2kabu.xyz:8443:127.0.0.1 \
  https://derp.charlie2kabu.xyz:8443/login
echo "--- 最近日志 ---"
sudo journalctl -u friend-ai -n 6 --no-pager 2>/dev/null | tail -6
