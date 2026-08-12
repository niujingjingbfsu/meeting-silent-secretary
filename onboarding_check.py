#!/usr/bin/env python3
"""装完本能力包后先跑这个，自动检测能不能跑通，卡在哪一层就告诉你怎么修。

设计原则（跟 README/latency 治理是同一条原则）：只报告真的能验证的结果；不能在
不产生副作用的前提下验证的项目，明确标成"❓ 无法预检"，并给出该怎么去真实验证，
不假装通过、也不静默跳过。

用法：
    python3 onboarding_check.py [--config config_silent.yaml] [--no-claude-check]
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PASS, FAIL, UNKNOWN = "✅", "❌", "❓"
HERE = Path(__file__).resolve().parent


def report(items):
    """items: list of (level, title, detail). 打印并返回是否有 FAIL。"""
    has_fail = False
    for level, title, detail in items:
        print(f"{level} {title}")
        if detail:
            for line in detail.splitlines():
                print(f"    {line}")
        if level == FAIL:
            has_fail = True
    return has_fail


def check_python():
    ok = sys.version_info >= (3, 9)
    level = PASS if ok else FAIL
    detail = "" if ok else (
        f"当前 {sys.version.split()[0]}，需要 3.9+。装个新版本 Python 再重试。")
    return [(level, f"Python 版本 {sys.version.split()[0]}", detail)]


def check_pyyaml():
    try:
        import yaml  # noqa: F401
        return [(PASS, "pyyaml 已安装", "")]
    except ImportError:
        return [(FAIL, "pyyaml 未安装",
                  f"运行：pip install -r {HERE / 'requirements.txt'}")]


def check_lark_cli():
    items = []
    path = shutil.which("lark-cli")
    if not path:
        items.append((FAIL, "lark-cli 未安装/不在 PATH 里",
                       "先装 lark-cli：https://github.com/larksuite/lark-cli（或你租户内部分发渠道），"
                       "装完确认 `lark-cli --version` 能跑通再重跑本脚本。"))
        return items
    items.append((PASS, f"lark-cli 已安装 ({path})", ""))

    try:
        proc = subprocess.run(["lark-cli", "auth", "status", "--json"],
                               capture_output=True, text=True, timeout=15)
        import json
        data = json.loads(proc.stdout)
    except Exception as e:
        items.append((FAIL, "lark-cli auth status 调用失败",
                       f"异常: {e}\n先确认 lark-cli 有没有正常配置/绑定过 app，跑一遍 "
                       "`lark-cli auth status` 看原始输出。"))
        return items

    bot = (data.get("identities") or {}).get("bot") or {}
    if bot.get("status") == "ready":
        items.append((PASS, f"bot 身份已就绪 (app_id={data.get('appId', '?')})", ""))
    else:
        items.append((FAIL, "bot 身份未就绪，本能力包全程只用 bot 身份，这个必须先解决",
                       f"lark-cli 给的原始提示: {bot.get('message', '(无)')}\n"
                       f"hint: {bot.get('hint', '(无)')}\n"
                       "一般是没在飞书开放平台创建自建应用/没把 app_id+app_secret 配进 "
                       "lark-cli，参考 lark-cli 自己的绑定文档处理。"))
    return items


def check_task_executor(skip_claude: bool):
    if skip_claude:
        return [(UNKNOWN, "执行层：已声明使用自定义 TaskExecutor，跳过 Claude CLI 检测",
                  "你需要自己确认接入的 agent 真的有工具调用能力（读写文件/跑shell/调"
                  "lark-cli），不是纯文本生成——这条没法用脚本帮你验证，责任在你这边，"
                  "细节见 README.md「接入非 Claude 的 agent」一节。")]
    path = shutil.which("claude")
    if not path:
        return [(FAIL, "执行层：出厂默认的 claude CLI 未安装/不在 PATH 里",
                  "两个选择：①装 Claude Code CLI（`npm install -g @anthropic-ai/claude-code`"
                  " 或官方安装方式），能用但需要能访问 Anthropic；②如果装不了/不想用，"
                  "改接自己的 agent，见 README.md「接入非 Claude 的 agent」一节，然后带 "
                  "--no-claude-check 重跑本脚本跳过这一项。")]
    try:
        proc = subprocess.run(["claude", "--version"], capture_output=True,
                               text=True, timeout=15)
        ver = (proc.stdout or proc.stderr).strip()
        return [(PASS, f"执行层：claude CLI 可用 ({ver})", "")]
    except Exception as e:
        return [(FAIL, "执行层：claude 二进制存在但跑不起来",
                  f"异常: {e}\n跑一遍 `claude --version` 看原始报错。")]


def check_config(config_path: Path):
    if not config_path.exists():
        return [(FAIL, f"配置文件 {config_path} 不存在",
                  f"运行：cp config_silent.example.yaml {config_path.name}，"
                  "再编辑里面的 bot_name/bot_name_alt。")]
    import yaml
    try:
        cfg = yaml.safe_load(config_path.read_text()) or {}
    except Exception as e:
        return [(FAIL, f"配置文件 {config_path} 解析失败", f"YAML 语法错误: {e}")]

    items = [(PASS, f"配置文件 {config_path} 存在且能解析", "")]
    bot_name = (cfg.get("bot_name") or "").strip()
    bot_name_alt = (cfg.get("bot_name_alt") or "").strip()
    if not bot_name or not bot_name_alt or bot_name_alt == "你的Bot正式名字":
        items.append((FAIL, "唤醒词 bot_name/bot_name_alt 还是空的或没改过模板占位符",
                       "编辑配置文件，把这两项填成你自己 bot 的称呼/正式名字。"))
    else:
        items.append((PASS, f"唤醒词已配置：{bot_name} / {bot_name_alt}", ""))
    return items


def check_vc_scope():
    """2026-08-12 实测踩坑：本想加一个"传个正在进行的会议号，脚本帮你实测"的功能，
    结果发现飞书 VC 相关读接口（+meeting-events / meeting get）全部要内部长 meeting_id，
    不是用户手头的9位会议号；能把会议号换成meeting_id的方式(+meeting-join/被邀请入会
    的事件回调)本身就有真实副作用（会真的把bot拉进会议音频通道），不适合放进一个"检查"
    脚本里悄悄触发。所以这一层老实放弃预检，不假装能查，直接指路到真实冒烟测试。"""
    return [(UNKNOWN, "飞书 VC Agent 会中权限（入会/听事件/发弹幕）：无法零副作用预检",
              "这几个 scope（vc:meeting.bot.join:write / vc:meeting.meetingevent:read / "
              "发消息相关 scope）没有已知的、不产生副作用的方式能提前查到 bot 身份是否"
              "已开通——lark-cli 的 auth check/auth scopes 目前只支持 user token，查不到 "
              "bot token 的授权状态；能查到的读接口又都要内部 meeting_id（不是9位会议号），"
              "换取 meeting_id 的方式本身就要求 bot 真的入会，没法在检查脚本里零副作用做。\n"
              "真实验证方式：找一场你自己在里面、正在进行的会议，直接冒烟测试——"
              "`python3 secretary_transcript_main.py --meeting-no <会议号> --config <配置>`，"
              "如果报 missing required scope(s)，跟着报错自带的 hint 去开通，不要自己猜 "
              "scope 名字。")]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config_silent.yaml")
    ap.add_argument("--no-claude-check", action="store_true",
                     help="已接入自定义 TaskExecutor（不用 Claude CLI）时加这个跳过检测")
    args = ap.parse_args()

    sections = [
        ("本地环境（所有 agent 通用）", check_python() + check_pyyaml()),
        ("lark-cli 与飞书 bot 身份（所有 agent 通用）", check_lark_cli()),
        ("执行层「大脑」（出厂默认 Claude Code，可替换）",
         check_task_executor(args.no_claude_check)),
        ("配置文件", check_config(HERE / args.config)),
        ("飞书 VC Agent 会中权限", check_vc_scope()),
    ]

    any_fail = False
    any_unknown = False
    for title, items in sections:
        print(f"\n=== {title} ===")
        fail = report(items)
        any_fail = any_fail or fail
        any_unknown = any_unknown or any(lvl == UNKNOWN for lvl, _, _ in items)

    print("\n" + "=" * 40)
    if any_fail:
        print(f"{FAIL} 还有没解决的问题，按上面的提示逐条修，改完重跑本脚本。")
        sys.exit(1)
    elif any_unknown:
        print(f"{UNKNOWN} 能自动检测的都过了，但还有几项没法零副作用预检，"
              "按上面提示做真实冒烟测试再确认。")
        sys.exit(0)
    else:
        print(f"{PASS} 全部检测通过。")
        sys.exit(0)


if __name__ == "__main__":
    main()
