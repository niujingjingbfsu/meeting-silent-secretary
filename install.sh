#!/usr/bin/env bash
# 会中无声助手（meeting-silent-secretary）单命令安装器。
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/niujingjingbfsu/meeting-silent-secretary/main/install.sh | bash
#   # 或指定安装目录：
#   curl -fsSL .../install.sh | bash -s -- my-secretary-dir
#
# 做的事：装前探测 → clone 仓库 → 装 Python 依赖 → 生成配置模板 → 跑一遍
# onboarding_check.py → 写一份 INSTALL_REPORT.md（结果+下一步指引）。
# 不会自动帮你建飞书应用/开权限——那两步官方就是控制台交互设计，这个安装器不碰。
set -euo pipefail

REPO_URL="https://github.com/niujingjingbfsu/meeting-silent-secretary.git"
TARGET_DIR="${1:-meeting-silent-secretary}"

if [ -d "$TARGET_DIR" ]; then
  echo "❌ 目录 $TARGET_DIR 已存在，换个目录名或先删掉/移走它再重跑。" >&2
  exit 1
fi

echo "=== 装前探测 ==="
preflight_ok=true
command -v git >/dev/null 2>&1 && echo "✅ git 已安装" || { echo "❌ 没找到 git"; preflight_ok=false; }
command -v python3 >/dev/null 2>&1 && echo "✅ python3 已安装" || { echo "❌ 没找到 python3"; preflight_ok=false; }
curl -sS --max-time 6 -o /dev/null https://github.com \
  && echo "✅ 能连通 github.com" || { echo "❌ 连不上 github.com"; preflight_ok=false; }
curl -sS --max-time 6 -o /dev/null https://open.feishu.cn \
  && echo "✅ 能连通 open.feishu.cn" || { echo "❌ 连不上 open.feishu.cn"; preflight_ok=false; }
if ! $preflight_ok; then
  echo "❌ 装前探测有没过的项，先解决再重跑本脚本。" >&2
  exit 1
fi

echo ""
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

echo "==> 检查 lark-cli"
if ! command -v lark-cli >/dev/null 2>&1; then
  echo "==> 没找到 lark-cli，尝试自动装（真实来源：@larksuite/cli 这个 npm 包）"
  if command -v npm >/dev/null 2>&1; then
    if ! npm install -g @larksuite/cli 2>/tmp/npm_err_$$.log; then
      echo "⚠️ 全局装失败（很可能是权限受限——沙盒/受限环境常见，比如没有写系统目录的权限），"
      echo "   原始报错见下，改用当前用户可写的本地目录重试："
      cat /tmp/npm_err_$$.log
      rm -f /tmp/npm_err_$$.log
      npm config set prefix "$HOME/.npm-global" 2>/dev/null || true
      export PATH="$HOME/.npm-global/bin:$PATH"
      if ! npm install -g @larksuite/cli; then
        echo "❌ 本地目录方式也装不上，这个环境可能连自己 home 目录下写文件都不允许。" >&2
        echo "   没法自动装 lark-cli，需要人工确认这个环境到底能不能装任何东西。" >&2
        exit 1
      fi
      echo "==> 装到了 $HOME/.npm-global，记得以后调用前先："
      echo '   export PATH="$HOME/.npm-global/bin:$PATH"'
    fi
  else
    echo "❌ 没找到 npm，没法自动装 lark-cli——先装 Node.js/npm（装完自己跑一遍："
    echo "   npm install -g @larksuite/cli），再重跑本脚本。" >&2
    exit 1
  fi
else
  echo "==> lark-cli 已安装，跑一次自更新，避免用旧版本"
  # 自更新失败不当硬性失败：受限/沙盒环境（比如某些企业agent运行时）可能干脆不允许
  # 改动已安装的东西，这种情况下老实继续用当前版本，好过直接卡死装不下去。
  if ! lark-cli update; then
    echo "⚠️ lark-cli update 失败——如果这个环境本身就不让改已安装的东西（常见于沙盒/"
    echo "   企业agent运行时），这是预期内的，不阻塞安装，继续用当前版本往下走。"
  fi
fi
if command -v lark-cli >/dev/null 2>&1; then
  echo "==> lark-cli 就绪：$(command -v lark-cli)（$(lark-cli --version 2>/dev/null)）"
  echo "    如果这台机器上不止一个 lark-cli（比如另一个目录手动放过旧版本），"
  echo "    上面这行路径/版本号就是实际会被调用的那一个，跟预期不符就去查 PATH 顺序。"
else
  echo "❌ lark-cli 装完还是找不到，可能装到了不在 PATH 里的目录，检查 npm 全局 bin 目录"
  echo "   是否在 PATH 里（npm config get prefix）。" >&2
  exit 1
fi

echo "==> 跑环境自检（onboarding_check.py）并生成安装报告"
set +e
$PY onboarding_check.py --dump-diagnostics diagnostics.json > onboarding_output.log 2>&1
CHECK_EXIT=$?
set -e
cat onboarding_output.log

{
  echo "# 安装报告"
  echo ""
  echo "- 安装时间：$(date '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null || date)"
  echo "- 安装目录：$(pwd)"
  echo "- 使用的 Python：$PY"
  if [ $CHECK_EXIT -eq 0 ]; then
    echo "- 自检结果：能自动验证的项都过了（可能仍有 ❓ 需要真实冒烟测试，见下方原文）"
  else
    echo "- 自检结果：有 ❌ 未解决，见下方原文"
  fi
  echo ""
  echo "## 自检原始输出"
  echo '```'
  cat onboarding_output.log
  echo '```'
  echo ""
  echo "## 下一步"
  if [ $CHECK_EXIT -ne 0 ]; then
    echo "1. 按上面 ❌ 逐条修，改完重跑：\`$PY onboarding_check.py\`"
    echo "2. 全部通过（或只剩 ❓）后再往下走。"
  else
    echo "1.（可选）唤醒词默认「小助手」不填也能用；想用自己的名字，编辑 \`config_silent.yaml\` 的 \`bot_name_alt\`。"
    echo "2. 找一场你自己能加入的真实进行中的会议。"
    echo "3. 跑：\`$PY secretary_transcript_main.py --meeting-no <会议号> --config config_silent.yaml\`"
    echo "4. 会里喊配置的唤醒词说一件具体的事，确认收到\"收到任务N\"弹幕、以及任务完成后带 ✅/❌ 的结果弹幕。"
  fi
  echo ""
  echo "卡住了？把 \`diagnostics.json\` 发给能帮你排查的人，里面不含任何密钥/token。"
} > INSTALL_REPORT.md

echo ""
echo "==================================================================="
echo "安装到：$(pwd)"
echo "详细报告+下一步指引已写入：$(pwd)/INSTALL_REPORT.md"
if [ "$PY" != "python3" ]; then
  echo "依赖装在本地虚拟环境里，后面所有命令都要用 $PY 而不是 python3，比如："
  echo "  $PY onboarding_check.py"
  echo "  $PY secretary_transcript_main.py --meeting-no <会议号>"
else
  echo "上面 ❌ 的项按提示逐条修；改完重跑：python3 onboarding_check.py"
  echo "都通过后冒烟测试：python3 secretary_transcript_main.py --meeting-no <会议号>"
fi
echo "==================================================================="
