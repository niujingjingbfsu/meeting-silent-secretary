#!/usr/bin/env python3
"""无声助手(会议秘书)专用入会方式——飞书逐字稿耳朵，不用豆包对混音音频做 ASR。

跟 main.py 现有的语音流(豆包耳朵)是两套完全独立的入口，互不影响、可以并存：
main.py 走 ByteView 实时音频 + 豆包 ASR，秘书角色下这条"耳朵"分不清"谁在说话"
（会场音频是一条混音流，豆包只能猜整体内容，猜不出说话人身份）；这个脚本改用飞书
自己的 vc +meeting-events 里的 transcript_received 事件——每句话服务端已经按参会人
分别转写好，自带准确的 speaker.user_name，换来"待办责任人"这类信息能准确归属到人，
代价是飞书自己转写有几秒延迟。秘书角色本来就不需要实时语音打断（永不开口），这个
代价可以接受——秘书角色本来就是"只听不说"，不需要实时打断，几秒延迟换来准确的责任人
归属，这笔账划得来。

判断/派发逻辑见 secretary_client.MeetingSecretaryClient。

用法：
    python3 secretary_transcript_main.py --meeting-no <9位会议号> [--config config_silent.yaml]
"""
import argparse
import asyncio
import json
from pathlib import Path

import yaml

from secretary_client import MeetingSecretaryClient


async def _lark_cli_json(args: list) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "lark-cli", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    try:
        return json.loads(out.decode(errors="ignore"))
    except Exception:
        raise RuntimeError(f"lark-cli 返回非JSON: {(out or err).decode(errors='ignore')[:300]}")


async def _join(meeting_no: str) -> str:
    data = await _lark_cli_json(
        ["vc", "+meeting-join", "--as", "bot", "--meeting-number", meeting_no, "--format", "json"])
    if not data.get("ok"):
        raise RuntimeError(f"入会失败: {str(data.get('error'))[:300]}")
    return data["data"]["meeting"]["id"]


async def _watch_meeting_ended(client: MeetingSecretaryClient, meeting_id: str, poll: float = 10.0):
    """独立轮询会议状态，非 ongoing 时置位收尾——transcript_ear 模式没有 ByteView 音频连接，
    不能靠"下行静默"这类信号判断会议是否结束，只能主动查一次会议状态。"""
    while not client._closed:
        await asyncio.sleep(poll)
        try:
            data = await _lark_cli_json(
                ["vc", "+meeting-events", "--as", "bot", "--meeting-id", meeting_id,
                 "--page-size", "1", "--format", "json"])
            status = ((data.get("data") or {}).get("meeting") or {}).get("status", "")
            if status and status != "ongoing":
                print(f"[secretary-ear] 会议状态={status}，收尾离会")
                client._closed = True
                break
        except Exception as e:
            print(f"[secretary-ear] 查会议状态异常（忽略，下轮重试）: {e}")


async def run(meeting_no: str, config_path: str):
    config = yaml.safe_load(Path(config_path).read_text())
    client = MeetingSecretaryClient()
    client.judge_model = config.get("judge_model")
    client.judge_backend = config.get("judge_backend", "cli")
    client.judge_api_key = config.get("judge_api_key", "")
    client.judge_api_model = config.get("judge_api_model", "")
    client.judge_base_url = config.get("judge_base_url", "")
    client.bot_name = config.get("bot_name", "小助手")
    client.bot_name_alt = config.get("bot_name_alt", "")
    client.owner_open_id = config.get("owner_open_id", "")
    client.owner_name_variants = tuple(config.get("owner_name_variants") or ())
    if config.get("remote_workdir"):
        client.remote_workdir = config["remote_workdir"]

    print("[secretary-ear] 入会中 ...")
    meeting_id = await _join(meeting_no)
    print(f"[secretary-ear] 已入会 meeting_id={meeting_id}")

    client.meeting_id = meeting_id
    client.meeting_no = meeting_no
    client.context = {
        "飞书会议ID": meeting_id,
        "飞书会议号": meeting_no,
        "结果回复群chat_id": config.get("reply_chat_id", ""),
    }

    judge_task = asyncio.ensure_future(client._init_judge())
    chat_loop_task = asyncio.ensure_future(client._meeting_chat_loop())
    watch_task = asyncio.ensure_future(_watch_meeting_ended(client, meeting_id))
    await judge_task

    opening_line = config.get(
        "opening_line",
        "👋 我是这场会的AI助手，加入会议来帮忙，只在后台听、不打断——"
        "喊我名字或发会中弹幕交代事情，我会先回「收到」，做完会发结果。")
    asyncio.ensure_future(client._send_barrage_reply(opening_line))

    try:
        while not client._closed:
            await asyncio.sleep(1)
    finally:
        chat_loop_task.cancel()
        watch_task.cancel()
        try:
            await _lark_cli_json(["vc", "+meeting-leave", "--as", "bot", "--meeting-id", meeting_id])
        except Exception as e:
            print(f"[secretary-ear] 离会异常（忽略）: {e}")
        print("[secretary-ear] 已收尾")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meeting-no", required=True)
    ap.add_argument("--config", default="config_silent.yaml")
    args = ap.parse_args()
    asyncio.run(run(args.meeting_no, args.config))


if __name__ == "__main__":
    main()
