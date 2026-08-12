#!/usr/bin/env bash
# 安装前探测——在真正 clone 仓库之前，先看这台机器/这个 agent 能不能装这个 Skill。
# 不下载仓库、不写任何文件，纯只读检查，跑坏了也不会留垃圾。
#
# 用法：curl -fsSL .../preflight.sh | bash
set -uo pipefail

PASS="✅"; FAIL="❌"; ok=true

hard_check() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "$PASS $desc"
  else
    echo "$FAIL $desc"
    ok=false
  fi
}

echo "=== 安装前探测 ==="
hard_check "git 已安装" command -v git
if command -v python3 >/dev/null 2>&1; then
  pyver=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  if python3 -c 'import sys; exit(0 if sys.version_info>=(3,9) else 1)'; then
    echo "$PASS python3 已安装，版本 $pyver (>=3.9)"
  else
    echo "$FAIL python3 版本 $pyver，需要 3.9+"
    ok=false
  fi
else
  echo "$FAIL python3 未安装"
  ok=false
fi
hard_check "能连通 github.com（拉仓库要用）" curl -sS --max-time 6 -o /dev/null https://github.com
hard_check "能连通 open.feishu.cn（飞书开放平台要用）" curl -sS --max-time 6 -o /dev/null https://open.feishu.cn

if command -v lark-cli >/dev/null 2>&1; then
  echo "$PASS lark-cli 已安装（不装也不阻塞这一步，装完仓库后 onboarding_check.py 会细查）"
else
  echo "❓ lark-cli 还没装——不阻塞现在这步，装完仓库后必须装好才能继续"
fi

echo ""
if $ok; then
  echo "$PASS 基础环境满足，可以继续安装："
  echo "  curl -fsSL https://raw.githubusercontent.com/niujingjingbfsu/meeting-silent-secretary/main/install.sh | bash"
else
  echo "$FAIL 上面有 ❌，先解决这些再装，不然装到一半会卡住。"
  exit 1
fi
