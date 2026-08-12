#!/usr/bin/env bash
# 会中无声助手（meeting-silent-secretary）单命令安装器。
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/niujingjingbfsu/meeting-silent-secretary/main/install.sh | bash
#   # 或指定安装目录：
#   curl -fsSL .../install.sh | bash -s -- my-secretary-dir
#
# 做的事：clone 仓库 → 装 Python 依赖 → 生成配置模板 → 跑一遍 onboarding_check.py。
# 不会自动帮你建飞书应用/开权限——那两步官方就是控制台交互设计，这个安装器不碰。
set -euo pipefail

REPO_URL="https://github.com/niujingjingbfsu/meeting-silent-secretary.git"
TARGET_DIR="${1:-meeting-silent-secretary}"

if [ -d "$TARGET_DIR" ]; then
  echo "❌ 目录 $TARGET_DIR 已存在，换个目录名或先删掉/移走它再重跑。" >&2
  exit 1
fi

command -v git >/dev/null 2>&1 || { echo "❌ 没找到 git，先装 git 再重跑。" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "❌ 没找到 python3，先装 Python 3.9+ 再重跑。" >&2; exit 1; }

echo "==> 克隆仓库到 ./$TARGET_DIR"
git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
cd "$TARGET_DIR"

PY=python3
echo "==> 安装 Python 依赖 (pip install -r requirements.txt)"
if ! python3 -m pip install -r requirements.txt 2>/tmp/pip_err_$$.log; then
  if grep -qi "externally-managed-environment" /tmp/pip_err_$$.log; then
    echo "==> 系统 Python 是 externally-managed（较新 Debian/Ubuntu 常见），"
    echo "    自动改用本地虚拟环境 ./.venv，不动系统 Python"
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    PY=".venv/bin/python3"
  else
    echo "❌ pip install 失败，原始报错："
    cat /tmp/pip_err_$$.log
    rm -f /tmp/pip_err_$$.log
    exit 1
  fi
fi
rm -f /tmp/pip_err_$$.log

if [ ! -f config_silent.yaml ]; then
  cp config_silent.example.yaml config_silent.yaml
  echo "==> 已生成 config_silent.yaml（模板），记得编辑 bot_name/bot_name_alt 再用"
fi

echo "==> 跑环境自检（onboarding_check.py）"
$PY onboarding_check.py || true

echo ""
echo "==================================================================="
echo "安装到：$(pwd)"
if [ "$PY" != "python3" ]; then
  echo "依赖装在本地虚拟环境里，后面所有命令都要用 $PY 而不是 python3，比如："
  echo "  $PY onboarding_check.py"
  echo "  $PY secretary_transcript_main.py --meeting-no <会议号>"
else
  echo "上面 ❌ 的项按提示逐条修；改完重跑：python3 onboarding_check.py"
  echo "都通过后冒烟测试：python3 secretary_transcript_main.py --meeting-no <会议号>"
fi
echo "==================================================================="
