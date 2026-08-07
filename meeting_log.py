"""结构化会中日志——给一个可选的「日志看板」用。

跟 task_board.py 记的是两码事：task_board 记"DO任务/技能这种一次性动作的粗粒度状态"，
这里记的是"判断层每一轮真实做了什么判断、花了多久"，用于事后复盘一场会到底哪里判断错了、
延迟卡在哪一层。同一种同步模式（本地写 JSON + 可选 scp 推送到一个静态页面托管地址，
fire-and-forget，推送失败静默、不拖累主循环），跟 task_board.py 互相独立的文件，互不干扰。

REMOTE_TARGET 默认未配置（None）——想要把日志发布成一个可访问的静态页面，调用
configure_remote(target) 设置你自己的目标；不配置的话，本地 JSON 照常写，只是不会
尝试 scp 推送。
"""
import asyncio
import json
import os
import time

LOCAL_DIR = os.path.join(os.path.dirname(__file__), "meeting_log_data")
REMOTE_TARGET = None
MAX_ENTRIES = 500  # 单场会日志上限，超出丢最早的——只为撑起"这场会大致发生了什么"，不做永久归档

_meetings = {}  # meeting_id -> {"label": str, "updated_at": float, "meta": {...}, "entries": [...]}


def configure_remote(target: str):
    """设置远程发布地址，比如 configure_remote("user@your-server:/var/www/site/meeting-log/data/")。
    传空字符串/None 等价于不配置（关闭远程发布，只写本地）。"""
    global REMOTE_TARGET
    REMOTE_TARGET = target or None


def _get_meeting(meeting_id: str, label: str = None):
    m = _meetings.setdefault(meeting_id, {"label": label or meeting_id, "meta": {}, "entries": []})
    if label:
        m["label"] = label
    return m


def set_meta(meeting_id: str, label: str = None, persona: str = "", provider: str = "",
             role: str = "", judgment_logic: str = ""):
    """会议一入会就调一次：记下"这场会是以什么人设加入的、判断逻辑大致是怎样"，
    对应看板顶部那一块。"""
    if not meeting_id:
        return
    m = _get_meeting(meeting_id, label)
    m["meta"] = {
        "persona": persona, "provider": provider, "role": role,
        "judgment_logic": judgment_logic, "started_at": time.time(),
    }
    _sync(meeting_id)


def log_turn(meeting_id: str, speaker: str = "", utterance: str = "", judgment: str = "",
             source: str = "", latency=None, reply: str = "", latency_breakdown: dict = None):
    """记一轮判断：谁说了什么(utterance) → Agent 做了什么判断(judgment，一句话人话结论，不要把
    耗时明细拼进这句话——耗时明细走 latency_breakdown 单独结构化字段，看板才能分开渲染成一眼能
    扫到的小标签，不然judgment会变成又长又绕的大段文字)
    → 判断来自哪一层(source: local_heuristic/fast_gate/heavy_judge/do_dispatch/do_complete 等)
    → 耗时多久(latency，秒，None 表示零网络开销的本地判断，这是"总耗时"，展示成主标签)
    → latency_breakdown：可选，更细的分段耗时字典，如 {"ASR→出声": 0.02, "出声→播报完成": 0.65}，
    键是环节名、值是秒数，看板会渲染成一串小标签
    → 如果真开口/真发了什么，说了什么(reply)。"""
    if not meeting_id:
        return
    m = _get_meeting(meeting_id)
    m["entries"].append({
        "ts": time.time(), "speaker": speaker, "utterance": utterance,
        "judgment": judgment, "source": source, "latency": latency, "reply": reply,
        "latency_breakdown": latency_breakdown or {},
    })
    m["entries"] = m["entries"][-MAX_ENTRIES:]
    _sync(meeting_id)


def _write_local():
    os.makedirs(LOCAL_DIR, exist_ok=True)
    # 孤立跑的测试脚本进程内存里只有它自己碰过的那一场会，如果这里直接用内存态整体覆盖
    # index.json，会把其它进程/其它会议早就写好的条目全部冲掉。改成跟磁盘上现有 index 合并，
    # 只更新/新增本进程实际碰过的会议，不删除本进程不知道的其它条目。
    idx_path = os.path.join(LOCAL_DIR, "index.json")
    merged = {}
    if os.path.exists(idx_path):
        try:
            with open(idx_path) as f:
                for m in json.load(f).get("meetings", []):
                    if m.get("meeting_id"):
                        merged[m["meeting_id"]] = m
        except Exception:
            pass
    for mid, m in _meetings.items():
        merged[mid] = {"meeting_id": mid, "label": m["label"], "updated_at": m.get("updated_at", 0)}
    with open(idx_path, "w") as f:
        json.dump({"meetings": list(merged.values())}, f, ensure_ascii=False)
    for mid, m in _meetings.items():
        with open(os.path.join(LOCAL_DIR, f"{mid}.json"), "w") as f:
            json.dump({
                "updated_at": m.get("updated_at", 0),
                "meta": m.get("meta", {}),
                "entries": m.get("entries", []),
            }, f, ensure_ascii=False)


def _sync(meeting_id: str):
    _meetings[meeting_id]["updated_at"] = time.time()
    _write_local()
    if not REMOTE_TARGET:
        return
    try:
        asyncio.ensure_future(_scp_push(meeting_id))
    except RuntimeError:
        pass  # 不在事件循环里调用（比如测试脚本）时跳过推送，本地文件已经写了


async def _scp_push(meeting_id: str):
    if not REMOTE_TARGET:
        print(f"[meeting_log] 未配置远程发布地址，跳过发布，内容已保存本地 {LOCAL_DIR}")
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "scp", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no", "-q",
            os.path.join(LOCAL_DIR, "index.json"),
            os.path.join(LOCAL_DIR, f"{meeting_id}.json"),
            REMOTE_TARGET,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except Exception:
        pass  # 同步失败静默——日志看板是辅助复盘工具，不能反过来拖累/搞崩主循环
