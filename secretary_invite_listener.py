#!/usr/bin/env python3
"""监听"被拉进会议"事件（vc.bot.meeting_invited_v1），自动触发秘书入会——装完这个之后，
不用再手动敲 `--meeting-no`，别人在会里手动把这个 bot 拉进去，就会自动 spawn
secretary_transcript_main.py 去加入那场会。

2026-08-13 真实踩过的坑：一开始想用 `lark-cli event consume vc.bot.meeting_invited_v1`
去监听，实测 `lark-cli event list` 里根本没有这个 EventKey（只有 user 身份的
vc.meeting.participant_meeting_* 系列）；换查飞书官方 Python SDK（lark-oapi）自带的
类型化事件处理器，同样没有预置这个事件的 handler——这不是"这个事件不存在"，是 lark-cli
的事件目录和 lark-oapi 的类型化处理器目录都还没收录它（我们自己生产环境的 Node.js
bridge 用同一个事件 key、走 `dispatcher.register({key: handler})` 原始注册方式确认过
真实可用、payload 里 `meeting.meeting_no` 字段可以直接拿到会议号）。

这里用 lark-oapi 提供的通用逃生舱口 `register_p2_customized_event(事件key字符串, handler)`
绕开类型化目录的限制，直接按原始 key 订阅——不是编造的接口，是该 SDK 源码里真实存在、
专门为"目录里没收录的事件"设计的注册方法。

依赖：pip install lark-oapi（这是一个新的可选依赖，只有用这个监听能力才需要装，主流程
secretary_transcript_main.py 不需要它，requirements.txt 里没有强制加进去）。

需要 config 里单独提供 app_id/app_secret——这个监听器走的是独立的 WebSocket 长连接
订阅，跟 lark-cli 自己已经绑定好的凭证是两码事，lark-cli 的凭证本来就不对外暴露明文，
没法复用，必须在 config_silent.yaml 里单独填一份（跟你在飞书开放平台建应用时拿到的是
同一对 app_id/app_secret，不是要另建一个应用）。

⚠️ 诚实说明：这个脚本我验证过 lark-oapi 这几个类/方法在当前安装的 SDK 版本里真实存在
（Client 构造签名、register_p2_customized_event、CustomizedEvent.event 是原始 dict），
但没有拿真实 app_secret 跑通一次真实的 WebSocket 连接+真实收到一次邀请事件——因为
app_secret 不在我能读取的范围内。装这个能力的人第一次用时，请自己冒烟测试一次：
把 bot 拉进一场真实会议，确认真的自动 spawn 出了 secretary_transcript_main.py。

用法：
    pip install lark-oapi
    python3 secretary_invite_listener.py --config config_silent.yaml
常驻运行，收到邀请事件后自动 spawn 秘书入会；重复邀请/已在跑的会议会被去重跳过。
"""
import argparse
import subprocess
import sys
from pathlib import Path

import yaml

try:
    import lark_oapi as lark
except ImportError:
    print("缺 lark-oapi，先跑：pip install lark-oapi", file=sys.stderr)
    sys.exit(1)

HERE = Path(__file__).resolve().parent
_seen_event_ids = set()
_joining = set()


def already_running(meeting_no: str) -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"secretary_transcript_main.py.*--meeting-no {meeting_no}"],
            capture_output=True, text=True)
        return bool(out.stdout.strip())
    except Exception:
        return False


def spawn_secretary(meeting_no: str, config_path: str):
    """2026-08-13 真实bug：这里如果硬编码 python3，装机时如果 install.sh 的 pip 因为
    externally-managed 系统环境自动 fallback 到了 .venv（这台机器上就真的触发过），
    这个监听器本身可能是用 .venv/bin/python3 起的，但 shell 里再起一个新进程调用
    "python3" 会走 PATH 解析到系统 python3——那个环境里没装 pyyaml，spawn 出来的
    子进程会静默 ImportError 死掉，而这条路径本来就是脱离终端、看不见输出的后台
    进程，出问题不容易察觉。改用 sys.executable，保证跟当前解释器用的是同一个。"""
    log_file = f"/tmp/secretary-invite-{meeting_no}.log"
    print(f"🚀 触发自动入会：meeting_no={meeting_no}，日志：{log_file}")
    subprocess.Popen(
        f"cd {HERE} && setsid nohup {sys.executable} secretary_transcript_main.py "
        f"--meeting-no {meeting_no} --config {config_path} "
        f"> {log_file} 2>&1 < /dev/null & disown",
        shell=True, executable="/bin/bash")


def make_handler(config_path: str):
    def on_invite(event):
        event_id = getattr(event.header, "event_id", None) if event.header else None
        if event_id:
            if event_id in _seen_event_ids:
                return
            _seen_event_ids.add(event_id)
        raw = event.event or {}
        meeting_no = (raw.get("meeting") or {}).get("meeting_no")
        inviter = (raw.get("inviter") or {}).get("user_name", "?")
        print(f"📩 收到会议邀请：来自 {inviter}，meeting_no={meeting_no}")
        if not meeting_no:
            print("⚠️ 事件里没有 meeting_no，跳过")
            return
        if meeting_no in _joining or already_running(meeting_no):
            print(f"⚠️ {meeting_no} 已经在处理/运行中，跳过重复入会")
            return
        _joining.add(meeting_no)
        try:
            spawn_secretary(meeting_no, config_path)
        finally:
            _joining.discard(meeting_no)

    return on_invite


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config_silent.yaml")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text()) or {}
    app_id = cfg.get("app_id") or ""
    app_secret = cfg.get("app_secret") or ""
    if not app_id or not app_secret:
        print("❌ config 里缺 app_id/app_secret——这个监听器走独立 WebSocket 长连接订阅，"
              "跟 lark-cli 已绑定的凭证是分开两套，必须在配置文件里单独填一份。",
              file=sys.stderr)
        sys.exit(1)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_customized_event("vc.bot.meeting_invited_v1", make_handler(args.config))
        .build()
    )
    client = lark.ws.Client(app_id, app_secret, event_handler=handler,
                             log_level=lark.LogLevel.INFO)
    print("👂 开始监听「被拉进会议」事件，等待邀请...")
    client.start()


if __name__ == "__main__":
    main()
