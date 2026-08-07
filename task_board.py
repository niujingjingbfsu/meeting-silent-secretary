"""本地任务看板状态 → 可选同步到一个静态页面托管地址（未配置时自动跳过，只写本地）。

只记"任务描述+粗粒度状态"（运行中/已完成/失败/已停止），不追踪精确进度/ETA——
跟 DO 任务本来就是 fire-and-forget、进度不可知这个现实一致，不假装知道自己不知道的事。
写本地 JSON 是同步的（快、不值得异步化），推到远程的 scp 是 fire-and-forget（网络慢/
服务器不通不该拖慢主循环，失败静默即可，看板本来就是辅助可视化，不是关键路径）。

REMOTE_TARGET 默认未配置（None）——这是从原始生产实现里抽出来的公共版本，原实现里硬编码了
一台特定服务器地址，那台服务器跟这份代码不是同一个所有者，不应该被继续写入。想要把看板发布
成一个可访问的静态页面，调用 configure_remote(user_host, remote_dir) 设置你自己的目标；
不配置的话，本地 JSON 照常写，只是不会尝试 scp 推送。
"""
import asyncio
import difflib
import json
import os
import re
import time

LOCAL_DIR = os.path.join(os.path.dirname(__file__), "task_board_data")

# 未配置时 = None，_scp_push* 系列函数会直接跳过、只打一行说明日志。
# 用 configure_remote() 设置，格式跟 scp 目标一致："user@host:/remote/dir/"（末尾带斜杠）。
REMOTE_TARGET = None


def configure_remote(target: str):
    """设置远程发布地址，比如 configure_remote("user@your-server:/var/www/site/task-board/data/")。
    传空字符串/None 等价于不配置（关闭远程发布，只写本地）。"""
    global REMOTE_TARGET
    REMOTE_TARGET = target or None


_meetings = {}   # meeting_id -> {"label": str, "updated_at": float, "tasks": {task_key: {...}}}


def _load_persisted():
    """进程重启后重新加载本地已落盘的历史会议记录，避免 index.json 每次都从空内存态重写，
    把之前会议（虽然各自的 <meeting_id>.json 还在磁盘上）从看板下拉框里悄悄丢掉。"""
    idx_path = os.path.join(LOCAL_DIR, "index.json")
    if not os.path.exists(idx_path):
        return
    try:
        with open(idx_path) as f:
            idx = json.load(f)
    except Exception:
        return
    for m in idx.get("meetings", []):
        mid = m.get("meeting_id")
        if not mid:
            continue
        entry = _meetings.setdefault(mid, {"label": m.get("label", mid), "tasks": {}})
        entry["label"] = m.get("label", mid)
        entry["updated_at"] = m.get("updated_at", time.time())
        mpath = os.path.join(LOCAL_DIR, f"{mid}.json")
        if not os.path.exists(mpath):
            continue
        try:
            with open(mpath) as f:
                data = json.load(f)
        except Exception:
            continue
        for i, t in enumerate(data.get("tasks", [])):
            entry["tasks"].setdefault(f"_persisted_{i}", t)


def _get_meeting(meeting_id: str, label: str = None):
    m = _meetings.setdefault(meeting_id, {"label": label or meeting_id, "tasks": {}})
    if label:
        m["label"] = label
    return m


def _write_local():
    os.makedirs(LOCAL_DIR, exist_ok=True)
    index = {
        "meetings": [
            {"meeting_id": mid, "label": m["label"], "updated_at": m["updated_at"]}
            for mid, m in _meetings.items()
        ]
    }
    with open(os.path.join(LOCAL_DIR, "index.json"), "w") as f:
        json.dump(index, f, ensure_ascii=False)
    for mid, m in _meetings.items():
        with open(os.path.join(LOCAL_DIR, f"{mid}.json"), "w") as f:
            json.dump({"updated_at": m["updated_at"], "tasks": list(m["tasks"].values())}, f, ensure_ascii=False)


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
        print(f"[task_board] 未配置远程发布地址，跳过发布，内容已保存本地 {LOCAL_DIR}")
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
        pass  # 同步失败静默——看板是辅助可视化，不能反过来拖累/搞崩主循环


_load_persisted()


# ── 实时逐字稿：中间栏"对话流"是会中转写原文，一个字一个字滚动 ──────
# 数据模型：每场会议一条"正在说的话"(current，随 ASR partial 不断被更新/替换，不产生新行）+
# 一份已经说完、提交进历史的行列表(history)。区分"还在长" vs "换了一句"靠简单前缀关系
# （ASR 流式结果本身就是同一句话不断变长/小幅修正），没有显式的"这句话说完了"事件，所以用
# 这个启发式，足够撑起"实时逐字稿"的观感。
_transcripts = {}   # meeting_id -> {"current": str, "current_ts": float, "history": [{"text","ts"}]}
_transcript_last_push = {}   # meeting_id -> 上次真正 scp 推送的时间戳，节流用
TRANSCRIPT_MAX_HISTORY = 200
TRANSCRIPT_COMMIT_IDLE_SEC = 6     # current 这么久没再被新内容更新，就当这句话已经说完
TRANSCRIPT_PUSH_MIN_INTERVAL = 1.0  # ASR partial 频率可能到每秒好几条，scp 节流别每条都推

# 同一场会同一时间只放一个 scp 在跑，跑的时候又有新内容就只记个"待办"标记、等它跑完立刻用
# 最新数据再推一次，不再无限并发——否则网络抖动时旧数据可能后写入把新数据盖掉。
_transcript_push_inflight = {}  # meeting_id -> bool
_transcript_push_pending = {}   # meeting_id -> bool

# ASR 事件流里混着两种结果——不带标点、持续变长的"中间态"结果，和每隔几个字才出现一次、带
# 逗号句号的"精修定稿"结果（后者还会顺手纠正同音字、去掉"呃"这类语气词）。原来的 startswith
# 前缀判断只能识别"同一版继续变长"，一旦标点/纠错让新结果不再是旧结果的严格前缀，就会被误判
# 成"另一句话"，把同一句话说完的过程拆成一堆头尾重叠的半截重复行提交进 history。
# 用去标点后的前缀/相似度关系识别"是不是同一句话"，避免这种误拆；同一句话内取信息量更大的
# 版本（优先带标点的精修版，其次去标点后更长的版本）。
_TRANSCRIPT_PUNCT_RE = re.compile(r"[，。！？、,.\!?~～…\s]+")
_TRANSCRIPT_SENT_PUNCT = ("，", "。", "！", "？", ",", ".", "!", "?")


def _transcript_normalize(s: str) -> str:
    return _TRANSCRIPT_PUNCT_RE.sub("", s or "")


def _transcript_is_same_utterance(a: str, b: str) -> bool:
    na, nb = _transcript_normalize(a), _transcript_normalize(b)
    if not na or not nb:
        return False
    if na == nb or na.startswith(nb) or nb.startswith(na):
        return True
    # 标点/同音字纠正可能打破严格前缀关系，容忍这种小幅出入：重叠窗口内相似度够高也算同一句
    short, longv = (na, nb) if len(na) <= len(nb) else (nb, na)
    ratio = difflib.SequenceMatcher(None, short, longv[: len(short) + 6]).ratio()
    return ratio >= 0.6


def _transcript_prefer(a: str, b: str) -> str:
    """同一句话的两版结果，选更适合展示的那版。"""
    a_final = any(p in a for p in _TRANSCRIPT_SENT_PUNCT)
    b_final = any(p in b for p in _TRANSCRIPT_SENT_PUNCT)
    if a_final != b_final:
        finalized, other = (a, b) if a_final else (b, a)
        nf, no = _transcript_normalize(finalized), _transcript_normalize(other)
        # 带标点的精修版基本追上了另一版的信息量（没明显更短，比如还没纠完就被截断）才采用
        if len(nf) >= len(no) * 0.85:
            return finalized
    return a if len(_transcript_normalize(a)) >= len(_transcript_normalize(b)) else b


def _transcript_commit_line(history: list, line: str, ts: float, speaker: str = "floor"):
    """把一句"说完的话"提交进 history；如果跟上一条其实是同一句话的重复/小修正
    （比如迟到的标点精修版），原地替换而不是再追加一条重复行。
    只在同一说话人时才做这种合并——不然"会场刚说到一半"跟"小助手插进来说的一整句"
    凑巧文本相似，会被错误地拼成一行，把两个人说的话糊在一起。"""
    if history and history[-1].get("speaker", "floor") == speaker \
            and _transcript_is_same_utterance(history[-1]["text"], line):
        history[-1] = {"text": _transcript_prefer(history[-1]["text"], line), "ts": ts, "speaker": speaker}
        return
    history.append({"text": line, "ts": ts, "speaker": speaker})


def _load_persisted_transcripts():
    """跟 _load_persisted() 同理：进程重启后把本地已落盘的逐字稿历史读回内存，只读 history，
    不读 current——重启前"正在说的那半句"本来就已经过时，等新内容来了自然会开新的一行。"""
    if not os.path.isdir(LOCAL_DIR):
        return
    for fname in os.listdir(LOCAL_DIR):
        if not fname.endswith("_transcript.json"):
            continue
        mid = fname[: -len("_transcript.json")]
        try:
            with open(os.path.join(LOCAL_DIR, fname)) as f:
                data = json.load(f)
        except Exception:
            continue
        _transcripts[mid] = {"current": "", "current_ts": time.time(), "history": data.get("history", [])}


_load_persisted_transcripts()


def append_transcript(meeting_id: str, text: str, speaker: str = "floor"):
    """speaker: "floor"=会场真人发言（ASR 转写，走下面的 partial 累积/去重状态机）；
    "assistant"=小助手自己念出/发出的话（本身已经是说完的整句，不存在"还在变长"的中间态，
    直接提交一行，不进 current 累积逻辑）。"""
    if not meeting_id or not text:
        return
    now = time.time()
    t = _transcripts.setdefault(meeting_id, {"current": "", "current_ts": now, "history": []})
    if speaker != "floor":
        # 插入一句非会场发言（小助手），先把还没提交的会场半截话冲掉，保住时间线先后顺序
        prev = t["current"]
        if prev:
            _transcript_commit_line(t["history"], prev, t["current_ts"], "floor")
            t["history"] = t["history"][-TRANSCRIPT_MAX_HISTORY:]
            t["current"] = ""
        _transcript_commit_line(t["history"], text, now, speaker)
        t["history"] = t["history"][-TRANSCRIPT_MAX_HISTORY:]
        _write_transcript_local(meeting_id)
        _transcript_last_push[meeting_id] = now
        _schedule_transcript_push(meeting_id)
        return
    prev = t["current"]
    committed_new_line = False
    # 放太久没再更新的 current，视为早就说完了，先提交进历史再处理这条新内容
    if prev and (now - t["current_ts"]) > TRANSCRIPT_COMMIT_IDLE_SEC:
        _transcript_commit_line(t["history"], prev, t["current_ts"], "floor")
        t["history"] = t["history"][-TRANSCRIPT_MAX_HISTORY:]
        prev = ""
        committed_new_line = True
    if prev and _transcript_is_same_utterance(prev, text):
        t["current"] = _transcript_prefer(prev, text)  # 同一句话继续变长/被标点精修，原地替换，不产生新行
    else:
        if prev:
            _transcript_commit_line(t["history"], prev, t["current_ts"], "floor")
            t["history"] = t["history"][-TRANSCRIPT_MAX_HISTORY:]
            committed_new_line = True
        t["current"] = text
    t["current_ts"] = now
    _write_transcript_local(meeting_id)
    # 换行（一句话说完开始下一句）是天然的检查点，不管节流直接推；句子中途变长的高频更新走节流
    if committed_new_line:
        _transcript_last_push[meeting_id] = now
        _schedule_transcript_push(meeting_id)
    elif now - _transcript_last_push.get(meeting_id, 0) >= TRANSCRIPT_PUSH_MIN_INTERVAL:
        _transcript_last_push[meeting_id] = now
        _schedule_transcript_push(meeting_id)


def _schedule_transcript_push(meeting_id: str):
    """请求推送一次逐字稿；同一场会已经有一个 scp 在跑就不再并发开新的，只标记"跑完再补推一次"
    （本地文件已经是最新内容，补推自然带上跑这段时间里积累的全部更新，不丢数据）。"""
    if not REMOTE_TARGET:
        return
    if _transcript_push_inflight.get(meeting_id):
        _transcript_push_pending[meeting_id] = True
        return
    _transcript_push_inflight[meeting_id] = True
    try:
        asyncio.ensure_future(_scp_push_transcript(meeting_id))
    except RuntimeError:
        _transcript_push_inflight[meeting_id] = False


def _write_transcript_local(meeting_id: str):
    os.makedirs(LOCAL_DIR, exist_ok=True)
    t = _transcripts.get(meeting_id)
    if not t:
        return
    with open(os.path.join(LOCAL_DIR, f"{meeting_id}_transcript.json"), "w") as f:
        json.dump({
            "updated_at": time.time(),
            "current": t["current"],
            "current_ts": t["current_ts"],
            "history": t["history"],
        }, f, ensure_ascii=False)


async def _scp_push_transcript(meeting_id: str):
    if not REMOTE_TARGET:
        _transcript_push_inflight[meeting_id] = False
        return
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "scp", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no", "-q",
            os.path.join(LOCAL_DIR, f"{meeting_id}_transcript.json"),
            REMOTE_TARGET,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        # 光 wait_for 超时放弃等待、不杀进程的话，这个 scp 还在后台占着一条到远端的连接，
        # 跟后面新发起的 scp 一起挤在同一台慢/抖动的服务器上，反而让拥堵更难恢复。
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
    except Exception:
        pass
    finally:
        _transcript_push_inflight[meeting_id] = False
        if _transcript_push_pending.pop(meeting_id, False):
            _schedule_transcript_push(meeting_id)


def list_do_tasks(meeting_id: str) -> list:
    """给"收到新指令时罗列本场会任务清单"这个弹幕功能用——直接读现成的 task_board 状态
    （start_do/finish_do 已经在维护），不用另建一套台账。按收到顺序返回
    [{"desc":..., "status": "运行中"/"已完成"/"失败"}, ...]（dict 插入顺序即时间顺序）。"""
    if not meeting_id or meeting_id not in _meetings:
        return []
    return list(_meetings[meeting_id]["tasks"].values())


def find_similar_running_task(meeting_id: str, desc: str, threshold: float = 0.6) -> dict:
    """真要派发前，先看本场会有没有一个还在"运行中"的任务跟这次描述高度相似（用 difflib 算
    文本相似度，不追求语义理解，够用就好），有就返回那条已有任务，调用方据此跳过重复派发，
    不是靠 prompt 单独硬防——避免同一件事被闲聊+直接指令各触发一次 DO、派出两个几乎相同的
    并行任务，互相占用执行槽位。"""
    if not meeting_id or meeting_id not in _meetings:
        return None
    for t in _meetings[meeting_id]["tasks"].values():
        if t.get("status") != "运行中":
            continue
        ratio = difflib.SequenceMatcher(None, t.get("desc", ""), desc).ratio()
        if ratio >= threshold:
            return t
    return None


def start_do(meeting_id: str, task_key: str, desc: str, label: str = None):
    if not meeting_id:
        return
    m = _get_meeting(meeting_id, label)
    m["tasks"][task_key] = {"desc": desc, "status": "运行中", "ts": time.time()}
    _sync(meeting_id)


def finish_do(meeting_id: str, task_key: str, ok: bool, link: str = None, reply: str = None):
    if not meeting_id or meeting_id not in _meetings:
        return
    t = _meetings[meeting_id]["tasks"].get(task_key)
    if not t:
        return
    t["status"] = "已完成" if ok else "失败"
    if link:
        t["link"] = link
    if reply:
        t["reply"] = reply
    _sync(meeting_id)


def set_skill(meeting_id: str, skill_key: str, desc: str, running: bool, label: str = None):
    if not meeting_id:
        return
    m = _get_meeting(meeting_id, label)
    m["tasks"][skill_key] = {"desc": desc, "status": "运行中" if running else "已停止", "kind": "skill", "ts": time.time()}
    _sync(meeting_id)
