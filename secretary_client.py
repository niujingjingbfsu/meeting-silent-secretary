"""会中无声助手（会议秘书）——独立、自包含的判断+派发+持续技能客户端。

从 lark-voice-agent 的 doubao_realtime.DoubaoRealtimeClient 里只抽取 role="secretary"
这条路径需要的代码：全程不说话、不接任何语音/ASR厂商，靠飞书自己的会中逐字稿
(vc +meeting-events 的 transcript_received 事件) + 会中弹幕当"耳朵"，被明确叫到名字才
执行一次性任务(DO)，结果发会中弹幕；支持6个可开关的持续性技能(barrage_answer/board/
prototype/minutes/slides/talkpoints)，开启后常驻循环、随讨论持续刷新内容、产出一个
HTML页面。

不包含：ByteView实时音频/豆包ASR/TTS出声（那是主持人等"会说话"角色专用的完全独立路径）、
议程跟进技能（未实现）、语音人设切换（会牵连独立的语音/TTS系统）。
"""
import asyncio
import os
import re
import time


CLAUDE_BIN = "claude"

_TASK_COMMITMENT_PHRASES = ("我来", "我这就", "我去", "我查", "我看", "我帮您", "我帮你", "帮您查",
                             "帮你查", "帮您看", "帮你看", "我写", "帮您写", "帮你写", "马上办", "这就去")


def _looks_like_task_commitment(speak_result: str) -> bool:
    """秘书角色偶尔把该回DO的一句话说成了SPEAK（比如"好的，我来帮您查一下..."）——
    这是简单的关键词粗筛，命中就额外按DO的流程真正派发这件事，不能只发一句空头承诺。"""
    return any(p in speak_result for p in _TASK_COMMITMENT_PHRASES)


class MeetingSecretaryClient:
    """会中无声助手：只听、只在被明确叫到名字时执行一次性任务，支持6个可开关的持续技能。"""

    MAX_CONCURRENT_DO_TASKS = 5

    _VALID_VERDICT_PREFIXES = ("PASS", "SPEAK", "DO", "LEAVE", "BARRAGE_REPLY", "结论",
                                "BARRAGE_ANSWER_ON", "BARRAGE_ANSWER_OFF",
                                "BOARD_ON", "BOARD_OFF", "PROTOTYPE_ON", "PROTOTYPE_OFF",
                                "MINUTES_ON", "MINUTES_OFF", "SLIDES_ON", "SLIDES_OFF",
                                "TALKPOINTS_ON", "TALKPOINTS_OFF",
                                "SKILLS_ON", "SKILLS_OFF", "RECONNECT")

    _SKILL_KEYS = {
        "barrage_answer": ("持续答弹幕", "_answering_barrage", "_barrage_answer_task", "_barrage_answer_loop"),
        "board": ("脑暴看板", "_board_active", "_board_task", "_board_loop"),
        "prototype": ("口述建原型", "_prototype_active", "_prototype_task", "_prototype_loop"),
        "minutes": ("会议纪要", "_minutes_active", "_minutes_task", "_minutes_loop"),
        "slides": ("实时会议Slides", "_slides_active", "_slides_task", "_slides_loop"),
        "talkpoints": ("分享要点卡片", "_talkpoints_active", "_talkpoints_task", "_talkpoints_loop"),
    }
    _SKILL_NAME_ALIASES = {
        "持续答弹幕": "barrage_answer", "答弹幕": "barrage_answer", "弹幕": "barrage_answer",
        "脑暴看板": "board", "脑暴": "board",
        "口述建原型": "prototype", "原型": "prototype", "建原型": "prototype",
        "会议纪要": "minutes", "纪要": "minutes",
        "实时会议Slides": "slides", "Slides": "slides", "slides": "slides", "幻灯片": "slides", "PPT": "slides",
        "分享要点卡片": "talkpoints", "分享要点": "talkpoints", "分享逻辑": "talkpoints",
        "观点卡片": "talkpoints", "金句卡片": "talkpoints",
    }

    _FILLER_LEAD_RE = re.compile(r"^(好的|好嘞|好呀|好啊|好|嗯|哦|OK|ok|这就|那我|那就|这样|那)[，。！,\s]*")

    def __init__(self):
        self.role = "secretary"
        self.bot_name = "小助手"          # 主叫名——建议用 ASR/转写容易听准的中文称呼
        self.bot_name_alt = "Seraphina"   # 备用名——bot 在飞书里注册的正式名字

        # 部署者身份（可选）：留空则"提到你"私聊提醒功能自动跳过，不影响其余能力。
        self.owner_open_id = ""
        self.owner_name_variants = ()

        # 判断后端配置
        self.judge_model = None
        self.judge_effort = "low"
        self.judge_backend = "cli"    # "cli"=本地 claude 常驻会话 / "api"=直连 Anthropic API
        self.judge_api_key = ""
        self.judge_api_model = ""
        self.judge_base_url = ""

        # 会议标识
        self.meeting_id = ""
        self.meeting_no = ""
        self.context = {}

        # 部署环境相关（可选，留空=对应发布功能自动跳过）
        self.remote_workdir = os.getcwd()      # 生成子进程的 cwd + HTML 产出物的本地根目录
        self.public_base_url = None            # 配置了才会把产出物拼成对外可访问 URL
        self.remote_publish_target = None      # 形如 "user@host:/path"，配置了才会 scp 发布

        self._transcript_ear = True
        self._seen_transcript_ids = set()

        self._host_transcript = []    # 会场最近转写(8条滑动窗口)，喂判断层用
        self._host_said = []          # 秘书自己发过的弹幕内容，去重用
        self._sec_posted = set()      # 已发过的结论/待办，去重

        self._secretary_transcript = []       # 持续技能用的长缓冲区(200条)
        self._secretary_transcript_count = 0  # 单调递增总计数，不随裁剪重置

        self._closed = False
        self._on_end = None           # 可选：收尾后的回调

        self._judge = None
        self._judge_ready = False
        self._judge_pending = False
        self._judge_init_started = False
        self._host_judging = False
        self._rebuilding = False
        self._acted_this_turn = False
        self._pending_judge_task = None
        self._pending_since = 0.0
        self._last_debounce_wait = 0.0
        self._last_asr_ts = 0.0
        self._asr_ended_event = asyncio.Event()
        self._last_owner_cue_ts = 0.0
        self._last_speak_started_ts = 0.0
        self._mouth = None             # 秘书永远没有嘴，恒为 None

        self._async_running = 0
        self._pending_do_queue = []

        self._shared_doc_url = ""
        self._shared_doc_title = ""
        self._shared_doc_content = ""

        # 6个持续技能各自的状态
        self._answering_barrage = False
        self._barrage_answer_pending = []
        self._barrage_answer_task = None

        self._board_active = False
        self._board_dirty = False
        self._board_task = None

        self._prototype_active = False
        self._prototype_dirty = False
        self._prototype_task = None

        self._minutes_active = False
        self._minutes_dirty = False
        self._minutes_task = None

        self._slides_active = False
        self._slides_dirty = False
        self._slides_task = None
        self._slides_topic = ""
        self._slides_transcript_baseline = 0
        self._slides_body_html = ""

        self._talkpoints_active = False
        self._talkpoints_dirty = False
        self._talkpoints_task = None
        self._talkpoints_transcript_baseline = 0
        self._talkpoints_body_html = ""
        self._talkpoints_url_announced = False

    # ---------------------------------------------------------------- 判断层

    async def _init_judge(self):
        """入会时起一个常驻判断会话并预热（注入角色+规则，付掉一次冷启动）。"""
        if self._judge_init_started:
            return
        self._judge_init_started = True
        try:
            setup = (
                "你是这场会议的【会议秘书】，会中【绝不语音发言、不打断】，只默默听、按需在群里产出/执行。\n"
                "我会反复把'会场最近的对话'发给你，每次只做一个判断，严格只回下面之一：\n"
                "- 还在讨论中 / 没有新结论 / 闲聊寒暄 → 只回：PASS\n"
                f"- 有人【直接叫你】（喊'{self.bot_name}'/'助手'）去做一件事（查/搜/发/建文档/查日程/总结…）"
                "→ 回：DO: 后跟这件事（把对方原话的诉求转成一句动宾清楚、可直接执行的指令）\n"
                "- 【务必分清'两人在闲聊商量'和'后面才真正对你说的直接指令'】'会场最近的对话'是一个滑动"
                "窗口，可能同时装着两人在闲聊商量的内容和后面才真正对你说的直接指令——如果闲聊里提到的"
                "事和后面直接对你说的指令其实是同一件事（比如先聊'具体数字你还记得吗/要不让小助手查一"
                "下'，紧接着才真正说'小助手，帮我查一下xxx'），只算这一件事、只回一次 DO，不要把闲聊"
                "部分也单独拆出来再算一条新指令——会导致重复拆出两条几乎相同的任务，同时占用两个执行"
                "槽位，白白变慢。判断标准：看这句话本身是不是第一次、明确地在对你提出这个要求，不是看"
                "话题内容像不像一个可执行的事。\n"
                "- 刚刚达成了一个【明确的新结论或决定】 → 回：结论: 后跟一句话概括（只概括【新增】结论，绝不重复之前已记过的）\n"
                "- 讨论中出现的、没人直接叫你去做的待办，不要自动检测、不要输出'待办:'这个标签——只有"
                "【有人直接叫你】去做才回 DO:，其余一律 PASS，不要自己去猜/记录别人话里潜在的待办事项。\n"
                "- 有人要你离开/退出这场会议 → 回：LEAVE:（你不出声，只是静默离会；这个判断结合"
                "上下文自己拿主意，不是靠固定关键词，拿不准就不要触发）\n"
                "- 有人直接问你'在不在/听到了没有/能不能听到我说话'这类确认存在感的话（不是要你做事，"
                "只是想验证你在监听）→ 回：BARRAGE_REPLY: 一句简短确认（比如'在的，听到你说话了'），"
                "不要回 PASS 也不要凭空编一句 SPEAK——你没有嘴，SPEAK 这个标签对你不生效、系统根本不会"
                "处理，说了等于白说，对方会以为你完全没反应；只有 BARRAGE_REPLY 才是你唯一能确认存在感"
                "的出口。\n"
                "- 你也有以下 6 个可以开关的持续技能（默认关，都不需要你额外语音/口头确认——开关本身就"
                "会自动发一条弹幕告诉对方）：\n"
                "  ①持续答弹幕：对方要求'你去持续回答弹幕里的问题' → BARRAGE_ANSWER_ON；不用回了 → BARRAGE_ANSWER_OFF。\n"
                "  ②脑暴看板：对方要求'把脑暴内容整理成一个看板/页面' → BOARD_ON；先不整理了 → BOARD_OFF。\n"
                "  ③口述建原型：对方在口头描述产品/页面需求，要求'画成原型' → PROTOTYPE_ON；先不画了 → PROTOTYPE_OFF。\n"
                "  ④会议纪要：对方要求'持续记一下会议纪要' → MINUTES_ON；不用记了 → MINUTES_OFF。\n"
                "  ⑤实时会议Slides：对方要求'把讨论做成幻灯片/Slides/PPT，跟着聊天实时更新' → SLIDES_ON"
                "（如果这句话里明确说了这次要围绕的主题，回 SLIDES_ON: <主题原文>；没说主题就只回"
                "SLIDES_ON，不要自己编主题）；先不用了 → SLIDES_OFF。唯一能真正打开这个技能的方式就是"
                "精确回这个标签，系统看到这个标签才会真的去创建/更新页面——不要用 SPEAK/BARRAGE_REPLY"
                "去'确认'或'解释'这件事，开不开这个技能只取决于你有没有精确输出这个标签本身。\n"
                "  ⑥分享要点卡片：对方要求'听一下分享者讲的，把核心观点和逻辑整理成卡片/幻灯片'这类"
                "请求（重点是蒸馏一个分享者的论述，不是记录整场讨论） → TALKPOINTS_ON；先不整理了 →"
                " TALKPOINTS_OFF。\n"
                "【务必分清这几个 _ON 标签 vs DO】只有对方明确要求开启一个【持续跟着会议进行不断更新】"
                "的能力时，才回 _ON 标签——如果是明确叫你去做的【一次性】任务（比如'帮我写一份今天这场"
                "会的会议纪要，写关键结论和待办就行，不用太长'/'顺便帮我把这个销售数据做成一个HTML看"
                "板'这种指定了具体范围、做完就结束的请求，不是要你从现在开始一直跟着记/跟着更新），应该"
                "回 DO: 走一次性执行流程，不要激活任何持续技能。判断标准很简单：这句话里有没有'持续/一"
                "直/跟着/别停'这类要求长期跟随的词——没有的话，哪怕内容听起来跟某个持续技能的主题相关"
                "（比如提到了'纪要''看板'这些词），也默认只是要一次性的产出，回 DO 不回 _ON。拿不准就"
                "默认 DO，不要轻易开持续技能——持续技能一旦真开了，会一直在后台跑、占用资源，比多做一"
                "次 DO 的代价大得多。\n"
                "如果对方一次性要求打开/关掉多个技能（比如'把这几个都打开'）→ 回一次性的批量标签："
                "SKILLS_ON: board,prototype,minutes（技能key用逗号分隔，从 barrage_answer/board/"
                "prototype/minutes/slides/talkpoints 这几个英文key里选），不要说'我一个个帮你开'"
                "（每轮只能输出一个标签，这种话没有对应的执行机制）。关闭多个同理用 SKILLS_OFF。\n"
                "如果有人明确说'重连'/'帮我重连一下'/类似要求重新连接音频的话，回：RECONNECT（系统会"
                "去处理，不需要你自己额外确认）；只是随口聊到'重连'这个词但不是明确指令，不要触发。\n"
                "【会中弹幕】'会场最近的对话'里出现\"[会中弹幕|某某]: 内容\"格式的，是文字弹幕不是"
                "语音，同样按上面规则判断要不要产出结论/待办/DO；如果只是想直接回一条弹幕文字"
                "（不是记结论/待办）→ 回：BARRAGE_REPLY: 后跟文字内容。\n"
                "判断铁律：**宁缺毋滥**——只在真有【新】结论、【新】DO 指令、有人直接"
                "确认存在感、或要开关某个持续技能时才产出，其余一律 PASS；绝不重复已记过的、已办过的。\n"
                "每轮严格只回 PASS / DO:… / 结论:… / LEAVE: / BARRAGE_REPLY:… / "
                "BARRAGE_ANSWER_ON / BARRAGE_ANSWER_OFF / BOARD_ON / BOARD_OFF / PROTOTYPE_ON / "
                "PROTOTYPE_OFF / MINUTES_ON / MINUTES_OFF / SLIDES_ON / SLIDES_OFF / "
                "TALKPOINTS_ON / TALKPOINTS_OFF / SKILLS_ON:… / SKILLS_OFF:… / RECONNECT，"
                "不要任何解释、不要寒暄。听明白回 READY。"
            )
            self._judge = self._make_judge()
            r = await self._judge.start(setup)
            print(f"[secretary] 判断会话已预热: {r[:30]!r}")
        finally:
            self._judge_ready = True
            if self._judge_pending:
                self._judge_pending = False
                asyncio.ensure_future(self._host_judge_and_drive())

    def _make_judge(self):
        """按 judge_backend 选判断后端：api=直连 Anthropic(快) / cli=本地 claude 常驻会话。"""
        if self.judge_backend == "api" and self.judge_api_key:
            from api_judge import ApiJudge
            print(f"[secretary] 判断后端 = Anthropic API（直连，端点={self.judge_base_url or 'api.anthropic.com'}）")
            return ApiJudge(api_key=self.judge_api_key, model=(self.judge_api_model or None),
                             base_url=(self.judge_base_url or None))
        from claude_judge import ClaudeJudge
        print(f"[secretary] 判断后端 = 本地 claude CLI 常驻会话（effort={self.judge_effort}）")
        return ClaudeJudge(cwd=self.remote_workdir, model=self.judge_model, effort=self.judge_effort)

    async def _revalidate_verdict(self, result: str) -> str:
        """判断层偶尔会把内部权衡过程当成最终答案吐出来，而不是干净的标签——校验+重试一次：
        格式不对就在同一个会话里追问一遍；重试仍不合规就原样返回交给上层处理（安全默认 PASS）。"""
        r = (result or "").strip()
        if not r or r.upper().startswith(self._VALID_VERDICT_PREFIXES):
            return result
        print(f"[secretary] ⚠ 判断结果格式不对（疑似把思考过程当成了答案，重试一次）: {r[:80]!r}")
        try:
            retry = (await asyncio.wait_for(
                self._judge.judge("刚才那句没有严格按照一开始规定的固定格式回答（多说了解释/思考"
                                   "过程）。请只按最初设定的格式重新回答这一轮，不要任何解释。"),
                timeout=15,
            )).strip()
        except Exception:
            return result
        if retry and retry.upper().startswith(self._VALID_VERDICT_PREFIXES):
            print(f"[secretary] ✓ 重试后拿到规范结果: {retry[:80]!r}")
            return retry
        print(f"[secretary] ⚠ 重试后仍不合规，按原样处理（安全默认）: {retry[:80]!r}")
        return result

    async def _rebuild_judge(self):
        """看门狗：判断会话卡死时彻底重建。"""
        if self._rebuilding:
            return
        self._rebuilding = True
        self._judge_ready = False
        self._host_judging = False
        self._judge_pending = False
        old = self._judge
        self._judge = None
        try:
            if old:
                await old.close()
        except Exception:
            pass
        try:
            self._judge_init_started = False
            await self._init_judge()
            print("[secretary] ✅ 判断会话已重建")
        finally:
            self._rebuilding = False

    # ---------------------------------------------------------------- 耳朵

    def _feed_host_transcript(self, text: str, is_owner_author: bool = False, is_barrage: bool = False):
        """每条转写：去重+累积到滑动窗口+累积到持续技能长缓冲区+触发判断。"""
        text = (text or "").strip()
        if not text:
            return
        if self._closed:
            return
        if self._host_transcript and text == self._host_transcript[-1]:
            return
        # ASR-partial 去重：新文本是上一条的前缀/超集(同一句话的流式增量)，原地替换而不是追加，
        # 避免一句长话把整个滑动窗口塞满几乎相同的片段。
        _norm_new = re.sub(r"[，。！？~?!,.\s]+$", "", text)
        if self._host_transcript:
            _norm_last = re.sub(r"[，。！？~?!,.\s]+$", "", self._host_transcript[-1])
            if _norm_new.startswith(_norm_last) or _norm_last.startswith(_norm_new):
                self._host_transcript[-1] = text
            else:
                self._host_transcript.append(text)
        else:
            self._host_transcript.append(text)
        self._host_transcript = self._host_transcript[-8:]

        if (not is_owner_author) and self.owner_open_id and self.owner_name_variants and \
                any(v in text for v in self.owner_name_variants) and \
                (time.time() - self._last_owner_cue_ts) > 120:
            self._last_owner_cue_ts = time.time()
            asyncio.ensure_future(self._notify_owner_mentioned(text))
        self._last_asr_ts = time.time()

        self._secretary_transcript.append(text)
        self._secretary_transcript = self._secretary_transcript[-200:]
        self._secretary_transcript_count += 1
        if self.meeting_id:
            try:
                import task_board
                task_board.append_transcript(self.meeting_id, text)
            except Exception:
                pass
        if self._board_active:
            self._board_dirty = True
        if self._prototype_active:
            self._prototype_dirty = True
        if self._minutes_active:
            self._minutes_dirty = True
        if self._slides_active:
            self._slides_dirty = True
        if self._talkpoints_active:
            self._talkpoints_dirty = True

        if is_barrage:
            # 弹幕不进防抖队列——发出来那一刻本身就是完整的，不需要等，直接单独触发一次判断。
            self._last_debounce_wait = 0.0
            asyncio.ensure_future(self._host_judge_and_drive())
            return
        if self._pending_judge_task and not self._pending_judge_task.done():
            self._pending_judge_task.cancel()
        else:
            self._pending_since = time.time()
            self._asr_ended_event.clear()
        self._pending_judge_task = asyncio.ensure_future(self._debounced_judge())

    async def _debounced_judge(self, debounce: float = 0.7, max_wait: float = 4.0):
        """判断防抖：一句话中途会连续吐好几条增量，每条都立刻判断会答非所问；
        改成每来一条新内容就往后推一点，真停顿够久了才真正判断一次，撑到 max_wait 硬上限。"""
        def _looks_done(text: str) -> bool:
            t = (text or "").strip()
            return bool(t) and t[-1] in "。！？~?!"
        try:
            while True:
                elapsed = time.time() - self._pending_since if self._pending_since else 0.0
                wait = min(debounce, max(0.0, max_wait - elapsed))
                try:
                    await asyncio.wait_for(self._asr_ended_event.wait(), timeout=wait)
                    break
                except asyncio.TimeoutError:
                    pass
                elapsed = time.time() - self._pending_since if self._pending_since else 0.0
                last = self._host_transcript[-1] if self._host_transcript else ""
                if not _looks_done(last) and elapsed < max_wait:
                    continue
                break
        except asyncio.CancelledError:
            return
        if self._closed:
            return
        self._last_debounce_wait = time.time() - self._last_asr_ts if self._last_asr_ts else 0.0
        self._pending_since = 0.0
        if self._pending_judge_task is asyncio.current_task():
            self._pending_judge_task = None
        await self._host_judge_and_drive()

    async def _meeting_chat_loop(self):
        """轮询 vc +meeting-events：处理飞书自己的逐字稿(transcript_received，带真实说话人)、
        会中弹幕(chat_received_items)、投屏文档(magic_share_started) 三种事件。"""
        await asyncio.sleep(3)
        page_token = ""
        seen_ids = set()
        while True:
            try:
                if self.meeting_id:
                    args = ["lark-cli", "vc", "+meeting-events", "--meeting-id", str(self.meeting_id),
                            "--as", "bot", "--json", "--page-size", "50"]
                    if page_token:
                        args += ["--page-token", page_token]
                    proc = await asyncio.create_subprocess_exec(
                        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
                    import json as _json
                    data = _json.loads(out.decode(errors="ignore"))
                    if data.get("ok"):
                        d = data.get("data") or {}
                        page_token = d.get("page_token") or ""
                        for ev in (d.get("events") or []):
                            if ev.get("event_type") == "transcript_received" and self._transcript_ear:
                                for titem in ((ev.get("payload") or {}).get("transcript_received_items") or []):
                                    sid = titem.get("sentence_id") or ""
                                    if sid and sid in self._seen_transcript_ids:
                                        continue
                                    if sid:
                                        self._seen_transcript_ids.add(sid)
                                    text = titem.get("text", "")
                                    if not text:
                                        continue
                                    spk = titem.get("speaker") or {}
                                    spk_name = spk.get("user_name") or "未知"
                                    is_owner_author = bool(self.owner_open_id) and spk.get("id") == self.owner_open_id
                                    line = f"[{spk_name}]: {text}"
                                    self._feed_host_transcript(line, is_owner_author=is_owner_author)
                                continue
                            if ev.get("event_type") == "magic_share_started":
                                for sitem in ((ev.get("payload") or {}).get("magic_share_started_items") or []):
                                    share_doc = sitem.get("share_doc") or {}
                                    s_url = share_doc.get("url", "")
                                    s_title = share_doc.get("title", "")
                                    if s_url and s_url != self._shared_doc_url:
                                        self._shared_doc_url = s_url
                                        asyncio.ensure_future(self._fetch_shared_doc(s_url, s_title))
                                continue
                            for item in (ev.get("chat_received_items") or ev.get("payload", {}).get("chat_received_items") or []):
                                mid = item.get("message_id") or ""
                                if mid and mid in seen_ids:
                                    continue
                                if mid:
                                    seen_ids.add(mid)
                                if item.get("message_type") != 1:
                                    continue
                                operator = item.get("operator") or {}
                                sender = operator.get("user_name", "") or "未知"
                                content = item.get("content", "")
                                if not content:
                                    continue
                                line = f"[会中弹幕|{sender}]: {content}"
                                is_owner_author = bool(self.owner_open_id) and operator.get("id") == self.owner_open_id
                                self._feed_host_transcript(line, is_owner_author=is_owner_author, is_barrage=True)
                                if self._answering_barrage:
                                    self._barrage_answer_pending.append(line)
                    else:
                        print(f"[secretary] 弹幕轮询返回失败: {str(data.get('error'))[:120]}")
            except Exception as e:
                print(f"[secretary] 弹幕轮询异常（跳过本轮）: {e}")
            await asyncio.sleep(3)

    async def _fetch_shared_doc(self, url: str, title: str):
        """会中有人投屏共享文档时，把文档内容读出来存进 self._shared_doc_content，
        让判断层每轮都能看到，回答"投屏那份材料在哪/讲了什么"这类问题。"""
        SHARED_DOC_MAX_CHARS = 2000
        m = re.search(r'/docx/([A-Za-z0-9]+)', url)
        token = m.group(1) if m else ""
        if not token:
            print(f"[secretary] 📎 投屏文档链接抠不出 token，跳过: {url[:80]}")
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "lark-cli", "docs", "+fetch", "--doc", token, "--scope", "full",
                "--doc-format", "markdown", "--as", "bot", "--json",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
            import json as _json
            data = _json.loads(out.decode(errors="ignore"))
            if data.get("ok"):
                content = ((data.get("data") or {}).get("document") or {}).get("content", "")
                trimmed = content[:SHARED_DOC_MAX_CHARS]
                if len(content) > SHARED_DOC_MAX_CHARS:
                    trimmed += "\n...(内容较长已截断，如需完整内容用 DO 现查)"
                self._shared_doc_title = title or "（无标题）"
                self._shared_doc_content = trimmed
                print(f"[secretary] 📎 投屏文档已读取:《{self._shared_doc_title}》")
            else:
                print(f"[secretary] 📎 投屏文档读取失败: {str(data.get('error'))[:120]}")
        except Exception as e:
            print(f"[secretary] 📎 投屏文档读取异常: {e}")

    # ---------------------------------------------------------------- 输出/看板

    async def _send_barrage_reply(self, text: str) -> bool:
        """直接发一条会中弹幕文字回复，不经过语音。返回真实发送结果。"""
        if not text or not self.meeting_id:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "lark-cli", "vc", "+meeting-message-send", "--meeting-id", str(self.meeting_id),
                "--msg-type", "text", "--text", text, "--as", "bot", "--json",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
            import json as _json
            try:
                ok = bool(_json.loads(out.decode(errors="ignore") or "{}").get("ok"))
            except Exception:
                ok = False
            if ok:
                print(f"[secretary] 💬 弹幕回复已发: {text[:60]}")
            else:
                detail = (out or err).decode(errors="ignore").strip()[:200]
                print(f"[secretary] 弹幕回复发送失败(API返回失败): {detail}")
            return ok
        except Exception as e:
            print(f"[secretary] 弹幕回复发送失败: {e}")
            return False

    def _render_task_roster(self) -> str:
        """本场会目前的任务清单（直接读 task_board 的真实状态）。只有1个任务时不返回内容。"""
        try:
            import task_board
            tasks = task_board.list_do_tasks(self.meeting_id or "")
        except Exception:
            return ""
        if len(tasks) < 2:
            return ""
        _status_emoji = {"运行中": "⏳", "已完成": "✅", "失败": "❌"}
        lines = [f"📋 任务清单（{len(tasks)}个）："]
        for i, t in enumerate(tasks, 1):
            emoji = _status_emoji.get(t.get("status", ""), "•")
            lines.append(f"{i}. {emoji} {t.get('desc', '')}")
        return "\n".join(lines)

    def _sync_skill_board(self, skill_key: str, desc: str, running: bool):
        """把持续技能的开关状态同步进本地任务看板——失败静默，看板只是辅助可视化。"""
        try:
            import task_board
            task_board.set_skill(self.meeting_id or "", skill_key, desc, running,
                                  label=self.meeting_no or self.meeting_id)
        except Exception:
            pass

    def _resolve_skill_key(self, raw: str) -> str:
        raw = raw.strip()
        if raw in self._SKILL_KEYS:
            return raw
        return self._SKILL_NAME_ALIASES.get(raw, "")

    def _activate_skill(self, key: str, topic: str = "") -> bool:
        """开启单个持续技能：state+常驻循环+task_board同步。返回是否真的发生了"关→开"的变化。
        topic：仅 slides 用——如果开口时明确说了主题，原样传进来锁定。"""
        info = self._SKILL_KEYS.get(key)
        if not info:
            return False
        label, active_attr, task_attr, loop_name = info
        if getattr(self, active_attr):
            return False
        setattr(self, active_attr, True)
        if key == "board":
            self._board_dirty = True
        elif key == "prototype":
            self._prototype_dirty = True
        elif key == "minutes":
            self._minutes_dirty = True
        elif key == "slides":
            self._slides_topic = topic.strip()
            self._slides_dirty = False
            self._slides_transcript_baseline = self._secretary_transcript_count
            self._slides_body_html = ""
            asyncio.ensure_future(self._slides_regen_async())
        elif key == "talkpoints":
            self._talkpoints_dirty = False
            self._talkpoints_transcript_baseline = self._secretary_transcript_count
            self._talkpoints_body_html = ""
            self._talkpoints_url_announced = False
            asyncio.ensure_future(self._talkpoints_regen_async())
        task = getattr(self, task_attr)
        if not task or task.done():
            loop_fn = getattr(self, loop_name)
            setattr(self, task_attr, asyncio.ensure_future(loop_fn()))
        self._sync_skill_board(key, label, True)
        print(f"[secretary] 🧩 开启技能: {label}")
        return True

    def _deactivate_skill(self, key: str) -> bool:
        info = self._SKILL_KEYS.get(key)
        if not info:
            return False
        label, active_attr, _, _ = info
        if not getattr(self, active_attr):
            return False
        setattr(self, active_attr, False)
        self._sync_skill_board(key, label, False)
        print(f"[secretary] 🧩 关闭技能: {label}")
        return True

    async def _notify_owner_mentioned(self, text: str):
        """会里提到部署者本人时，私聊提醒一句。owner_open_id 为空时不会走到这里。"""
        meeting_no = self.meeting_no or self.meeting_id or "某场会议"
        msg = f"📣 会议（{meeting_no}）里刚才提到你了：「{text[:60]}」"
        try:
            proc = await asyncio.create_subprocess_exec(
                "lark-cli", "im", "+messages-send", "--user-id", self.owner_open_id,
                "--text", msg, "--as", "bot", "--json",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=10)
            print(f"[secretary] 📣 已私聊提醒被提到: {text[:40]}")
        except Exception as e:
            print(f"[secretary] 📣 提醒失败: {e}")

    async def _notify_task_cancelled(self, task_label: str):
        """会议结束前最后交代的任务还没跑完就被硬取消——弹幕这条路走不通了，私聊告知。"""
        if not self.owner_open_id:
            return
        meeting_no = self.meeting_no or self.meeting_id or "某场会议"
        msg = (f"⚠️ 会议（{meeting_no}）结束前，你交代的这件事还没确认办完就被取消了："
               f"「{task_label}」。会议已经结束，没法再往会里发结果了，麻烦你自己确认一下"
               f"这件事最终有没有做成。")
        try:
            proc = await asyncio.create_subprocess_exec(
                "lark-cli", "im", "+messages-send", "--user-id", self.owner_open_id,
                "--text", msg, "--as", "bot", "--json",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=10)
        except Exception as e:
            print(f"[secretary] ⚠️ 私聊告知任务取消失败: {e}")

    async def _schedule_proactive(self):
        """秘书角色不做主动推进，永远 no-op（这里保留是为了跟共享代码的调用点兼容）。"""
        return

    @staticmethod
    def _first_clause(text: str, max_len: int = 28, min_len: int = 6) -> str:
        """取文本的第一个自然分句，给 DO 类一次性任务当短标签/看板描述用。"""
        t = (text or "").strip()
        t = MeetingSecretaryClient._FILLER_LEAD_RE.sub("", t).strip() or t
        if len(t) <= max_len:
            return t
        short_fallback = ""
        for i, ch in enumerate(t):
            if ch in "，。！？；、" and 0 < i <= max_len:
                if i >= min_len:
                    return t[:i]
                short_fallback = short_fallback or t[:i]
        out = ""
        for tok in t.split(" "):
            candidate = f"{out} {tok}".strip() if out else tok
            if len(candidate) > max_len and out:
                break
            out = candidate
            if len(out) >= max_len:
                break
        if len(out) > max_len:
            out = out[:max_len]
        if out:
            return out if out == t else out + "…"
        return short_fallback or t[:max_len]

    async def _host_finish(self, line: str):
        """收尾：置 _closed(后续一律 PASS)，触发离会回调。秘书没有嘴，line 参数保留仅为兼容。"""
        if self._closed:
            return
        self._closed = True
        print(f"[secretary] 🏁 收尾: {line}")
        if self._on_end:
            try:
                self._on_end()
            except Exception as e:
                print(f"[secretary] 自动离会触发失败: {e}")

    # ---------------------------------------------------------------- 判断+派发主循环

    async def _host_judge_and_drive(self, proactive: bool = False):
        debounce_wait = self._last_debounce_wait
        self._last_debounce_wait = 0.0
        _utterance_end_ts = time.time() - debounce_wait
        if self._closed:
            return
        if not self._judge_ready:
            self._judge_pending = True
            return
        if self._host_judging:
            self._judge_pending = True
            return
        self._host_judging = True
        self._acted_this_turn = False
        result = ""
        judge_dt = 0.0
        t0 = time.time()
        try:
            convo = "\n".join(self._host_transcript) or "（暂时没人发言）"
            said = "；".join(self._host_said[-6:]) if self._host_said else "（还没说过）"
            said_ctx = f"【你已经说过的（别重复这些）】\n{said}\n\n"
            last_said = self._host_said[-1] if self._host_said else ""
            if last_said and last_said.rstrip()[-1:] in ("？", "?"):
                said_ctx += (
                    f"【重要】你刚问的问题是「{last_said}」，还没得到明确答复。"
                    f"接下来这句话如果是在回应/确认/重复刚才的请求（哪怕说法不完全一样），"
                    f"要顺着这个问题往下判断该不该 DO，不要当成全新的、无关的内容重新判断；"
                    f"但如果内容明显是完全不相关的新话题，就正常按新内容处理。\n\n"
                )
            if self._shared_doc_content:
                said_ctx += (
                    f"【当前/最近一次会中投屏的文档：《{self._shared_doc_title}》】\n{self._shared_doc_content}\n\n"
                )
            _url_skills = [
                ("_board_active", "board.html", "脑暴看板"),
                ("_prototype_active", "prototype.html", "口述建原型"),
                ("_minutes_active", "minutes.html", "会议纪要"),
                ("_slides_active", "slides.html", "实时会议Slides"),
                ("_talkpoints_active", "talkpoints.html", "分享要点卡片"),
            ]
            _skill_status_lines = []
            for _flag, _fname, _label in _url_skills:
                if getattr(self, _flag, False):
                    _local = os.path.join(self.remote_workdir, "live_pages",
                                           self.meeting_id or "unknown", _fname)
                    if os.path.exists(_local):
                        if self.public_base_url:
                            _url = f"{self.public_base_url.rstrip('/')}/{self.meeting_id}/{_fname}"
                            _skill_status_lines.append(f"「{_label}」：已开启，已有实质内容部署上线，地址是 {_url}")
                        else:
                            _skill_status_lines.append(f"「{_label}」：已开启，已有实质内容生成，保存在本地（未配置公开访问地址）")
                    else:
                        _skill_status_lines.append(f"「{_label}」：已开启，但还没生成出真正的内容，还没有可发的地址")
            if _skill_status_lines:
                said_ctx += (
                    "【你身上开着的持续技能真实状态——有人问链接/进度时必须照这个如实回答，"
                    "不许自己猜/编造\"没有这个能力\"，这些能力真实存在】\n"
                    + "\n".join(_skill_status_lines) + "\n\n"
                )
            sil_gap = time.time() - self._last_asr_ts if self._last_asr_ts else 0
            turn = (
                said_ctx +
                f"【会场最近的对话（最后一句说完到现在已经过去约{sil_gap:.0f}秒）】\n{convo}\n\n"
                "现在判断（严格只回上面列出的标签之一）：如果最后一句读起来像话没说完，但已经过去"
                "好几秒了，大概率是真说完了，别死等。"
            )
            if self._judge and self._judge.alive:
                try:
                    judge_future = asyncio.ensure_future(self._judge.judge(turn))
                    ACK_THRESHOLD = 6.0
                    try:
                        result = (await asyncio.wait_for(asyncio.shield(judge_future), timeout=ACK_THRESHOLD)).strip()
                    except asyncio.TimeoutError:
                        result = (await asyncio.wait_for(judge_future, timeout=20 - ACK_THRESHOLD)).strip()
                    result = await self._revalidate_verdict(result)
                except asyncio.TimeoutError:
                    print("[secretary] ⚠ 判断卡死 >20s，后台重建判断会话，本轮跳过")
                    asyncio.ensure_future(self._rebuild_judge())
                    result = ""
            else:
                # 常驻判断会话不可用：本轮当 PASS，不做无常驻会话的一次性回退（避免引入额外依赖）。
                result = "PASS"
            judge_dt = time.time() - t0
            if self.judge_backend == "cli" and result and judge_dt < 0.3:
                print(f"[secretary] ⚠ 判断异常快({judge_dt:.2f}s<0.3s)，疑似管道失步，丢弃结果并重建会话: {result[:60]!r}")
                asyncio.ensure_future(self._rebuild_judge())
                result = "PASS"
            print(f"[secretary] Claude 判断({judge_dt:.1f}s): {result[:100]}")

            # 兜底：不管前面主要动作是什么，只要 result 全文里任意位置出现了技能开关标签，
            # 都顺便真正执行一次（_activate_skill/_deactivate_skill 本身幂等，重复调用无害）。
            _TAG_TO_SKILL = {"BARRAGE_ANSWER": "barrage_answer", "BOARD": "board",
                              "PROTOTYPE": "prototype", "MINUTES": "minutes", "SLIDES": "slides",
                              "TALKPOINTS": "talkpoints"}
            for _tag_prefix, _skill_key in _TAG_TO_SKILL.items():
                if _skill_key != "slides" and re.search(rf"\b{_tag_prefix}_ON\b", result.upper()):
                    self._activate_skill(_skill_key)
                if re.search(rf"\b{_tag_prefix}_OFF\b", result.upper()):
                    self._deactivate_skill(_skill_key)
            if not result.upper().startswith("SLIDES_ON"):
                _slides_scan_m = re.search(r"SLIDES_ON:?\s*([^\n]*)", result, re.IGNORECASE)
                if _slides_scan_m:
                    self._activate_skill("slides", topic=_slides_scan_m.group(1).strip())

            if result.upper().startswith("DO"):
                task = result[2:].lstrip(":：").strip()
                if task:
                    self._dispatch_secretary_do(task, debounce_wait, judge_dt)
            elif result.upper().startswith("SPEAK") and _looks_like_task_commitment(result):
                # 判断层偶尔口误把明确的任务请求回成了 SPEAK——照常发这句话当弹幕，
                # 但额外真正按DO的流程把这件事派发出去(用真实原话，不用模型转述)。
                reply = result[5:].lstrip(":：").strip()
                if reply:
                    self._host_said.append(reply); self._host_said = self._host_said[-6:]
                    asyncio.ensure_future(self._send_barrage_reply(reply))
                real_task = self._host_transcript[-1] if self._host_transcript else ""
                if real_task:
                    self._dispatch_secretary_do(real_task, debounce_wait, judge_dt)
            elif result.startswith("结论"):
                item = result[2:].lstrip(":：").strip()
                if item and item[:40] not in self._sec_posted:
                    self._sec_posted.add(item[:40])
                    print(f"[secretary] 📌 记结论: {item[:50]}")
            elif result.upper().startswith("LEAVE"):
                print("[secretary] 🚪 判断层决定离会（静默）")
                asyncio.ensure_future(self._host_finish(""))
            elif result.upper().startswith("BARRAGE_REPLY"):
                reply = result[13:].lstrip(":：").strip()
                if reply:
                    self._host_said.append(reply); self._host_said = self._host_said[-6:]
                    asyncio.ensure_future(self._send_barrage_reply(reply))
            elif result.upper().startswith("SPEAK"):
                # 秘书没有嘴，SPEAK 对它不生效——代码层兜底当 BARRAGE_REPLY 处理，
                # 保证哪怕判断层用错标签，这句话也不会被真的吞掉。
                reply = result[5:].lstrip(":：").strip()
                if reply:
                    self._host_said.append(reply); self._host_said = self._host_said[-6:]
                    asyncio.ensure_future(self._send_barrage_reply(reply))
            elif result.upper().startswith("SLIDES_ON"):
                _slides_topic = result[len("SLIDES_ON"):].lstrip(":：").strip()
                if self._activate_skill("slides", topic=_slides_topic):
                    ack = "🖥 已开启实时 Slides，跟着讨论更新，有更新会发弹幕。"
                    if _slides_topic:
                        ack += f"主题锁定：{_slides_topic}"
                    asyncio.ensure_future(self._send_barrage_reply(ack))
            elif result.upper().startswith("SLIDES_OFF"):
                if self._deactivate_skill("slides"):
                    asyncio.ensure_future(self._send_barrage_reply("🖥 已停止更新 Slides。"))
            elif result.upper().startswith("TALKPOINTS_ON"):
                if self._activate_skill("talkpoints"):
                    asyncio.ensure_future(self._send_barrage_reply("🗣 已开启分享要点卡片，跟着分享内容更新，有更新会发弹幕。"))
            elif result.upper().startswith("TALKPOINTS_OFF"):
                if self._deactivate_skill("talkpoints"):
                    asyncio.ensure_future(self._send_barrage_reply("🗣 已停止更新分享要点卡片。"))
            elif result.upper().startswith("BARRAGE_ANSWER_ON"):
                if self._activate_skill("barrage_answer"):
                    asyncio.ensure_future(self._send_barrage_reply("💬 已开启持续答弹幕。"))
            elif result.upper().startswith("BARRAGE_ANSWER_OFF"):
                if self._deactivate_skill("barrage_answer"):
                    asyncio.ensure_future(self._send_barrage_reply("💬 弹幕先不持续回了。"))
            elif result.upper().startswith("BOARD_ON"):
                if self._activate_skill("board"):
                    asyncio.ensure_future(self._send_barrage_reply("📋 已开启脑暴看板，跟着讨论更新，有更新会发弹幕。"))
            elif result.upper().startswith("BOARD_OFF"):
                if self._deactivate_skill("board"):
                    asyncio.ensure_future(self._send_barrage_reply("📋 已停止更新看板。"))
            elif result.upper().startswith("PROTOTYPE_ON"):
                if self._activate_skill("prototype"):
                    asyncio.ensure_future(self._send_barrage_reply("🎨 已开启口述建原型，跟着需求描述更新，有更新会发弹幕。"))
            elif result.upper().startswith("PROTOTYPE_OFF"):
                if self._deactivate_skill("prototype"):
                    asyncio.ensure_future(self._send_barrage_reply("🎨 已停止更新原型。"))
            elif result.upper().startswith("MINUTES_ON"):
                if self._activate_skill("minutes"):
                    asyncio.ensure_future(self._send_barrage_reply("🗒 已开始持续记纪要，有新结论/待办会发弹幕。"))
            elif result.upper().startswith("MINUTES_OFF"):
                if self._deactivate_skill("minutes"):
                    asyncio.ensure_future(self._send_barrage_reply("🗒 纪要先不记了。"))
            elif result.upper().startswith("SKILLS_ON") or result.upper().startswith("SKILLS_OFF"):
                turning_on = result.upper().startswith("SKILLS_ON")
                raw_list = result.split(":", 1)[1].strip() if ":" in result else ""
                keys = [self._resolve_skill_key(k) for k in raw_list.split(",")]
                keys = [k for k in keys if k]
                changed_labels = []
                for k in keys:
                    ok = self._activate_skill(k) if turning_on else self._deactivate_skill(k)
                    if ok:
                        changed_labels.append(self._SKILL_KEYS[k][0])
                if changed_labels:
                    verb = "打开" if turning_on else "关掉"
                    asyncio.ensure_future(self._send_barrage_reply(f"已经帮你把{'、'.join(changed_labels)}都{verb}了。"))
            elif result.upper().startswith("RECONNECT"):
                # 没有音频连接，没法真的自动重连——如实告知，不调用任何不存在的重连API。
                asyncio.ensure_future(self._send_barrage_reply("收到，但当前无法自动重连，请联系管理员处理。"))
        finally:
            self._host_judging = False
            try:
                if result and not result.upper().startswith("DO"):
                    import meeting_log
                    meeting_log.log_turn(
                        self.meeting_id or "", speaker="会场",
                        utterance=(self._host_transcript[-1] if self._host_transcript else ""),
                        judgment=f"重型判断层输出: {result[:150]}",
                        source="heavy_judge", latency=round(judge_dt, 2),
                        latency_breakdown={"防抖等待(转写完成→开始判断)": round(debounce_wait, 2),
                                            "重型判断层处理耗时": round(judge_dt, 2)})
            except Exception:
                pass
            if self._judge_pending and not self._acted_this_turn:
                self._judge_pending = False
                if not self._pending_judge_task or self._pending_judge_task.done():
                    asyncio.ensure_future(self._host_judge_and_drive())
            else:
                self._judge_pending = False

    # ---------------------------------------------------------------- DO任务派发

    def _dispatch_secretary_do(self, task: str, debounce_wait: float, judge_dt: float):
        """真正派发一件DO任务：查重→发"收到"确认弹幕（合并任务清单）→派发/排队执行。"""
        _dup_task = None
        try:
            import task_board
            _dup_task = task_board.find_similar_running_task(self.meeting_id or "", task)
        except Exception:
            _dup_task = None
        if _dup_task:
            asyncio.ensure_future(self._send_barrage_reply(
                f"（这件事看起来已经在处理了：「{_dup_task.get('desc','')[:50]}」，不重复派发）"))
            return
        busy = self._async_running >= self.MAX_CONCURRENT_DO_TASKS
        task_label = self._first_clause(task)
        print(f"[secretary] [{time.strftime('%H:%M:%S')}] 🔇 静默执行({'排队/忙' if busy else '派发'}): {task[:60]}")
        task_id = f"do-{int(time.time() * 1000)}"
        task_number = 1
        try:
            import task_board
            task_board.start_do(self.meeting_id or "", task_id,
                                 task + ("（排队中）" if busy else ""),
                                 label=self.meeting_no or self.meeting_id)
            task_number = len(task_board.list_do_tasks(self.meeting_id or "")) or 1
        except Exception:
            pass
        ack = f"收到任务{task_number}：{task}" + ("（手头有别的任务，稍后处理）" if busy else "")
        roster = self._render_task_roster()
        combined = f"{ack}\n\n{roster}" if roster else ack
        asyncio.ensure_future(self._send_barrage_reply(combined))
        real_utterance = self._host_transcript[-1] if self._host_transcript else ""
        _judge_breakdown = {"防抖等待(转写完成→开始判断)": round(debounce_wait, 2),
                             "重型判断层处理耗时": round(judge_dt, 2)}
        if not busy:
            asyncio.ensure_future(self._run_async_task(task, task_label, announce_done=False, task_id=task_id,
                                                         real_utterance=real_utterance, judge_breakdown=_judge_breakdown,
                                                         task_number=task_number))
        else:
            self._pending_do_queue.append((task, task_label, task_id, real_utterance, _judge_breakdown, task_number))

    async def _run_async_task(self, task: str, task_label: str = None, announce_done: bool = True,
                               task_id: str = None, real_utterance: str = "", judge_breakdown: dict = None,
                               task_number: int = None):
        """执行类任务：后台另起一个带记忆+skills的 Claude 干活，结果发会中弹幕。
        与判断会话完全解耦、fire-and-forget，不阻塞判断循环。"""
        task_label = task_label or self._first_clause(task)
        reply_chat = (self.context or {}).get("结果回复群chat_id", "")
        meeting_id = self.meeting_id or ""
        meeting_no = self.meeting_no or ""
        instr = (
            f"[来自会议的语音指令]\n{task}\n\n"
            f"背景：这条指令来自会议号 {meeting_no or '未知'}（会议ID={meeting_id or '未知'}），"
            f"如果任务是把其它 bot/演员拉进同一场会，要用这个会议号，不要凭空猜一个。\n\n"
            f"【重要禁止项】绝不要尝试用 lark-cli 让自己（这个正在参会的 bot）退出/离开这场会议——"
            f"哪怕任务描述听起来像是让你去'办'离会这件事。离会必须由主进程统一走它自己的收尾流程处理，"
            f"不能由你这个后台子任务越过主进程直接调 API 退会。如果任务确实是'让bot离会/退出会议'这类，"
            f"什么 API 都不要调，只需要在结果里如实说明'这个交给主流程处理，我不该越权执行'。\n\n"
            f"请完成它（可使用你的记忆和 lark 等技能）。完成后：\n"
            f"用 lark-cli 的 `vc +meeting-message-send --meeting-id {meeting_id} --msg-type text "
            f"--text \"<结果>\"` 把结果作为一条会中弹幕发出去（这是唯一的主要出口，务必发，结果简洁、"
            f"面向人读；这条弹幕所有会议参与者都能看到，不要提脚本文件名/路径/内部实现细节这类技术信息，"
            f"只说人话结论）。\n"
            f"这条弹幕正文最前面必须先给一句简短的结论提示，跟你后面要输出的 TASK_DONE 判断保持一致，"
            f"不能自己矛盾：这件事本身真的做成了 → 开头写「✅ {'任务' + str(task_number) + '完成' if task_number else '任务完成'}」；"
            f"没有真正做成（不管是权限不够/信息缺失/任何其它原因）→ 开头写"
            f"「❌ {'任务' + str(task_number) + '未完成' if task_number else '任务未完成'}」。这句"
            f"提示单独占一行，跟后面的具体内容之间空一行。\n"
            f"如果这个任务的本质是【生成一段文字/文档内容】（比如写一份JD、文案、总结、方案这类），"
            f"弹幕里必须直接包含生成出来的完整内容本身，不能只发一句'已经生成'的确认、也不能只建一个"
            f"文档/文件然后只发链接——只有内容长到明显没法塞进一条消息时，才退而求其次先发一段摘要+文档链接。\n"
            f"如果这个任务是【写会议纪要/总结】这类，内容必须按【先结论、后待办】的顺序组织：结论部分"
            f"只列这场会真正达成的决定/结果；待办部分每一条都要写清楚【谁】负责、【什么时间节点】完成、"
            f"【具体做什么事】——这三样里任何一样会场里没有明确说，就在那个位置老实写\"待定\"，不要自己"
            f"瞎编一个负责人或时间，也不要为了看起来完整就把这一条省略不写。\n"
            f"【重要】这场会不一定绑定了群，不要无条件再往固定群发一遍——只有当这条弹幕命令真的执行"
            f"失败（比如报权限/scope错误）时，才用 lark-cli 把同样的结果发一条文本消息到飞书群"
            f"chat_id={reply_chat} 兜底，避免结果彻底丢失；弹幕发送成功就不要再发群。\n\n"
            f"最后必须单独另起三行、原样输出下面这三个标记（不要省略、不要改写），供上层准确播报：\n"
            f"1) `BARRAGE_SENT: yes` 或 `BARRAGE_SENT: no`——第 1 步（会中弹幕）确认发送成功"
            f"（lark-cli 命令真的返回成功）才写 yes，否则/不确定一律写 no。\n"
            f"2) `TASK_DONE: yes` 或 `TASK_DONE: no`——这个任务本身实质性的目标有没有真正达成（不是"
            f"'子进程有没有跑完/弹幕有没有发出去'，是'你被要求做的那件事到底做没做成'）：如果因为权限"
            f"不够、身份缺失、参数不合法等任何原因没能真正完成任务、只是如实汇报了情况，这里必须写 no，"
            f"哪怕弹幕已经成功发出去了。只有确实把任务本身要求的事做成了，才写 yes。\n"
            f"3) 不管这个任务是回答问题还是执行动作，都必须额外输出 "
            f"`SPOKEN_ANSWER: <口语化总结，一句话，20字以内>`——只说人话结论，绝不能有任何URL、文件"
            f"路径、代码、命令行、markdown标记符号，也不要带'详情看弹幕'这类引导语。\n"
            f"4) 如果这个任务产出了一个可长期访问的产出物链接（比如新建/编辑的文档、表格、画板等云"
            f"文档链接），额外输出 `DELIVERABLE_LINK: <完整url>`——只放一个最主要的链接，没有产出物"
            f"链接就不要输出这行。"
        )
        self._async_running += 1
        t_do_dispatch = time.time()
        real_utterance = real_utterance or (self._host_transcript[-1] if self._host_transcript else "")
        try:
            import meeting_log
            meeting_log.log_turn(self.meeting_id or "", speaker="会场", utterance=real_utterance,
                                  judgment=f"重型判断层判定为可执行任务，派发后台子agent执行: {task_label}",
                                  source="do_dispatch", latency_breakdown=(judge_breakdown or None))
        except Exception:
            pass
        ok = False
        barrage_ok = False
        spoken_answer = ""
        task_done = None
        deliverable_link = ""
        try:
            proc = await asyncio.create_subprocess_exec(
                "nice", "-n", "19", CLAUDE_BIN, "-p", "--output-format", "text",
                "--dangerously-skip-permissions", instr,
                cwd=self.remote_workdir,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            ok = proc.returncode == 0
            out_text = (out or err).decode(errors="ignore")
            barrage_ok = "BARRAGE_SENT: YES" in out_text.strip().upper()
            for ln in out_text.splitlines():
                s = ln.strip().upper()
                if s.startswith("TASK_DONE:"):
                    val = s.split(":", 1)[1].strip() if ":" in s else ""
                    task_done = val.startswith("Y")
                    break
            for ln in out_text.splitlines():
                if ln.strip().upper().startswith("SPOKEN_ANSWER:"):
                    spoken_answer = ln.split(":", 1)[1].strip() if ":" in ln else ""
                    break
            for ln in out_text.splitlines():
                s = ln.strip()
                if s.upper().startswith("DELIVERABLE_LINK:"):
                    deliverable_link = s.split(":", 1)[1].strip() if ":" in s else ""
                    break
            print(f"[secretary] [{time.strftime('%H:%M:%S')}] 异步任务{'完成' if ok else '失败'}"
                  f"（弹幕={'发了' if barrage_ok else '未确认'}）: {out_text.strip()[:120]}")
        except asyncio.CancelledError:
            print("[secretary] 异步任务被取消（会议大概率已经结束）")
            asyncio.ensure_future(self._notify_task_cancelled(task_label))
            raise
        except Exception as e:
            ok = False
            barrage_ok = False
            spoken_answer = ""
            print(f"[secretary] 异步任务失败: {e}")
        finally:
            self._async_running = max(0, self._async_running - 1)
            if not ok:
                line = f"「{task_label}」没弄成，原因发到群里了。"
            elif task_done is False:
                line = spoken_answer or f"「{task_label}」没能真正完成，原因发到弹幕里了。"
            elif task_done is True:
                line = spoken_answer or (f"「{task_label}」办完了，弹幕里发了结果。" if barrage_ok
                                          else f"「{task_label}」办完了，弹幕没发出去，结果发在群里了。")
            elif spoken_answer:
                line = spoken_answer
            else:
                line = f"「{task_label}」处理完了，具体结果你看一下弹幕。"
            if task_id:
                try:
                    import task_board
                    real_done = ok and (task_done is not False)
                    task_board.finish_do(self.meeting_id or "", task_id, real_done,
                                          link=deliverable_link or None, reply=line)
                except Exception:
                    pass
            try:
                import meeting_log
                meeting_log.log_turn(
                    self.meeting_id or "", speaker="会场", utterance=real_utterance,
                    judgment=(f"后台子agent执行结果：进程{'正常退出' if ok else '异常/崩溃'}，"
                              f"任务本身{'真正完成' if task_done is True else ('未真正完成' if task_done is False else '完成状态不确定')}，"
                              f"弹幕{'已发送' if barrage_ok else '未确认发送'}"),
                    source="do_complete", reply=line,
                    latency_breakdown={"后台任务耗时(派发→完成)": round(time.time() - t_do_dispatch, 2)})
            except Exception:
                pass
            if self._pending_do_queue and self._async_running < self.MAX_CONCURRENT_DO_TASKS:
                next_task, next_label, next_task_id, next_utterance, next_breakdown, next_task_number = self._pending_do_queue.pop(0)
                print(f"[secretary] → 排队任务出队执行: {next_task[:60]}")
                asyncio.ensure_future(self._run_async_task(next_task, next_label, task_id=next_task_id,
                                                             real_utterance=next_utterance, judge_breakdown=next_breakdown,
                                                             task_number=next_task_number))
            # announce_done and self._mouth：秘书永远没有嘴（self._mouth 恒为 None），
            # 这里天然是no-op，保留这行只是跟共享逻辑的调用签名保持一致。
            if announce_done and self._mouth and not self._closed:
                try:
                    self._host_said.append(line); self._host_said = self._host_said[-6:]
                except Exception:
                    pass

    # ---------------------------------------------------------------- 6个持续技能

    async def _barrage_answer_loop(self, interval: float = 15.0):
        try:
            while self._answering_barrage and not self._closed:
                await asyncio.sleep(interval)
                if not self._answering_barrage or self._closed:
                    break
                if not self._barrage_answer_pending:
                    continue
                pending = list(self._barrage_answer_pending)
                self._barrage_answer_pending = []
                await self._barrage_answer_review_async(pending)
        except asyncio.CancelledError:
            pass

    async def _barrage_answer_review_async(self, pending: list):
        """持续答弹幕：独立子进程，逐条回答攒下的弹幕。"""
        meeting_id = self.meeting_id or ""
        convo = "\n".join(pending)
        instr = (
            f"[持续答弹幕]\n这是刚攒下的、还没回答的会中弹幕：\n{convo}\n\n"
            "逐条回答这些弹幕问题（不知道答案的可以用你的记忆/技能查，不要凭空编）。每条答案都用"
            f" lark-cli 的 `vc +meeting-message-send --meeting-id {meeting_id} --msg-type text "
            "--text \"<答案>\"` 发一条会中弹幕回复。全部处理完后另起一行原样输出 "
            "`BARRAGE_ANSWERED: <已回答条数>`。"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "nice", "-n", "19", CLAUDE_BIN, "-p", "--output-format", "text",
                "--dangerously-skip-permissions",
                cwd=self.remote_workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(input=instr.encode()), timeout=150)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError("claude -p 子进程超时(150s)未响应，已终止")
            out_text = (out or err).decode(errors="ignore")
            print(f"[secretary] 💬 持续答弹幕完成: {out_text.strip()[:120]}")
        except Exception as e:
            print(f"[secretary] 💬 持续答弹幕失败（这批弹幕丢弃，等下一批）: {e}")

    def _template_path(self, filename: str) -> str:
        return os.path.join(os.path.dirname(__file__), "templates", filename)

    async def _publish_page(self, meeting_id: str, local_path: str, remote_name: str, label: str) -> str:
        """发布产出物：配置了 remote_publish_target 才会真的 scp 推送；没配置就只保存本地，
        返回可用于弹幕文案的一句"地址"描述（有公开URL时是URL，否则是本地路径说明）。"""
        if self.remote_publish_target:
            try:
                host, remote_dir_base = self.remote_publish_target.split(":", 1)
                remote_dir = f"{remote_dir_base.rstrip('/')}/{meeting_id}"
                mkdir_proc = await asyncio.create_subprocess_exec(
                    "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                    host, "mkdir", "-p", remote_dir,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(mkdir_proc.wait(), timeout=10)
                proc = await asyncio.create_subprocess_exec(
                    "scp", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no", "-q",
                    local_path, f"{host}:{remote_dir}/{remote_name}",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(proc.wait(), timeout=15)
            except Exception as e:
                print(f"[secretary] {label}同步到服务器失败: {e}")
        if self.public_base_url:
            return f"{self.public_base_url.rstrip('/')}/{meeting_id}/{remote_name}"
        print(f"[secretary] 未配置远程发布地址，{label}已保存本地: {local_path}")
        return f"（已保存本地: {local_path}）"

    async def _board_loop(self, quiet_sec: float = 8.0, poll: float = 2.0):
        pending_since = None
        try:
            while self._board_active and not self._closed:
                await asyncio.sleep(poll)
                if not self._board_active or self._closed:
                    break
                if self._board_dirty:
                    self._board_dirty = False
                    pending_since = time.time()
                    continue
                if pending_since is not None and (time.time() - pending_since) >= quiet_sec:
                    pending_since = None
                    await self._board_regen_async()
        except asyncio.CancelledError:
            pass

    async def _board_regen_async(self):
        """重新生成整版脑暴看板 HTML（全量重生成），推送后发弹幕通知。"""
        meeting_id = self.meeting_id or "unknown"
        convo = "\n".join(self._secretary_transcript[-200:])
        rel_dir = f"live_pages/{meeting_id}"
        rel_path = f"{rel_dir}/board.html"
        template_path = self._template_path("board_style_template.html")
        with open(template_path, encoding="utf-8") as f:
            template_html = f.read()
        style_match = re.search(r"<style>.*?</style>", template_html, re.S)
        BOARD_STYLE_TEMPLATE = style_match.group(0) if style_match else ""
        instr = (
            f"[脑暴看板更新]\n这是最新的会议转写内容（可能包含之前已经生成过的部分）：\n{convo}\n\n"
            "把这些脑暴内容整理成一个单文件 HTML 看板页面。下面是必须原样复用的 <style>，禁止重新"
            f"设计/改配色/改字号/删减 class：\n{BOARD_STYLE_TEMPLATE}\n\n"
            f"完整的参考页面结构（含真实示例内容，仅供你理解每个 class 怎么组合使用，正文内容要"
            f"换成本轮实际讨论的东西，不要照抄示例文字）：{template_path}（用 Read 工具读取这个"
            f"文件参考 <body> 部分的结构）。\n\n"
            "结构要点（每个明确 topic 一个独立页面，页面间可下拉切换，每个 topic 页面内固定按"
            "「发散→收敛→待办」三段展示）：\n"
            "1. header：.eyebrow 固定写「脑暴看板 · 实时更新」，h1 是本轮议题概括，.sub 是一句话"
            "背景说明。\n"
            "2. header 内紧接着放 .topic-switcher：一个 <select id=topicSelect "
            "onchange=\"switchTopic(this.value)\">，每个明确 topic 一个 <option>；旁边 "
            ".badge-dot#switcherDot 显示当前 topic 的配色。之后是 .status-legend 放两个 "
            "status-tag 图例（pending/confirmed）。\n"
            "3. 每个 topic 一个 <section class=\"topic-page topic-a\" data-topic=\"a\">（第一个"
            "topic 额外带 active class，其余不带）：.topic-head + .topic-desc + .stage-block"
            "（发散：.sticky-grid 包多个 .sticky 卡片；收敛：.converge-card；待办：.todo-list），"
            "如实反映讨论进展，没有结论/待办就如实写\"暂无\"，不要编。\n"
            "4. footer 固定一句话说明按议题切换 + 三段呈现会持续更新。\n"
            "5. 页面末尾必须带 <script> 定义 switchTopic(id) 函数：把所有 .topic-page 的 active "
            "class 按 data-topic 是否等于 id 来 toggle，并同步更新 #switcherDot 的背景色。\n"
            f"先执行 `mkdir -p {rel_dir}`，再把完整 HTML 写入相对路径文件 `{rel_path}`（cwd 已经是"
            f"{self.remote_workdir}，用相对路径写，不要用绝对路径）。写之前如果这个文件已存在，先 "
            "Read 一次拿到最新内容再 Write，避免'文件已被修改'报错。\n"
            "写完后必须另起一行，原样输出 `UPDATE_SUMMARY: <20字以内，说清这次新增/调整了什么>`。"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "nice", "-n", "19", CLAUDE_BIN, "-p", "--output-format", "text",
                "--dangerously-skip-permissions",
                cwd=self.remote_workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(input=instr.encode()), timeout=150)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError("claude -p 子进程超时(150s)未响应，已终止")
            out_text = (out or err).decode(errors="ignore")
            summary = ""
            for ln in out_text.splitlines():
                if ln.strip().upper().startswith("UPDATE_SUMMARY:"):
                    summary = ln.split(":", 1)[1].strip() if ":" in ln else ""
                    break
            local_path = os.path.join(self.remote_workdir, rel_path)
            if not os.path.exists(local_path):
                print(f"[secretary] 🗂 看板生成子进程没有写出文件，跳过本轮: {out_text.strip()[:120]}")
                return
            addr = await self._publish_page(meeting_id, local_path, "board.html", "脑暴看板")
            print(f"[secretary] 🗂 看板已更新: {summary}")
            await self._send_barrage_reply(f"📋 脑暴看板更新了：{summary or '内容有更新'}，{addr}")
        except Exception as e:
            print(f"[secretary] 🗂 看板生成失败（跳过本轮，等下次触发）: {e}")

    async def _prototype_loop(self, quiet_sec: float = 8.0, poll: float = 2.0):
        pending_since = None
        try:
            while self._prototype_active and not self._closed:
                await asyncio.sleep(poll)
                if not self._prototype_active or self._closed:
                    break
                if self._prototype_dirty:
                    self._prototype_dirty = False
                    pending_since = time.time()
                    continue
                if pending_since is not None and (time.time() - pending_since) >= quiet_sec:
                    pending_since = None
                    await self._prototype_regen_async()
        except asyncio.CancelledError:
            pass

    async def _prototype_regen_async(self):
        """重新生成整版口述原型 HTML（全量重生成）。要求生成后自查一遍渲染效果。"""
        meeting_id = self.meeting_id or "unknown"
        convo = "\n".join(self._secretary_transcript[-200:])
        rel_dir = f"live_pages/{meeting_id}"
        rel_path = f"{rel_dir}/prototype.html"
        instr = (
            f"[口述原型更新]\n这是最新的会议转写内容（口头描述的产品/页面需求，可能包含之前已经"
            f"画过的部分）：\n{convo}\n\n"
            "把这些口头描述的需求画成一个单文件、可点击交互的 HTML 原型（多屏用 JS 切换，不要真的"
            "跳转链接；线框级还原即可，不用像素级精修）。"
            f"先执行 `mkdir -p {rel_dir}`，把完整 HTML 写入相对路径文件 `{rel_path}`（cwd 已经是"
            f"{self.remote_workdir}，用相对路径写）。写完后【必须】用无头浏览器把这个文件渲染截图"
            "自查一遍（看有没有文字溢出/元素重叠/裁切问题），如果发现问题就改完再自查一次（最多2轮），"
            "不能只写完文件就当完成。\n"
            "自查通过后另起一行，原样输出 `UPDATE_SUMMARY: <20字以内，说清这次新增/调整了什么>`。"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "nice", "-n", "19", CLAUDE_BIN, "-p", "--output-format", "text",
                "--dangerously-skip-permissions",
                cwd=self.remote_workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(input=instr.encode()), timeout=150)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError("claude -p 子进程超时(150s)未响应，已终止")
            out_text = (out or err).decode(errors="ignore")
            summary = ""
            for ln in out_text.splitlines():
                if ln.strip().upper().startswith("UPDATE_SUMMARY:"):
                    summary = ln.split(":", 1)[1].strip() if ":" in ln else ""
                    break
            local_path = os.path.join(self.remote_workdir, rel_path)
            if not os.path.exists(local_path):
                print(f"[secretary] 🎨 原型生成子进程没有写出文件，跳过本轮: {out_text.strip()[:120]}")
                return
            addr = await self._publish_page(meeting_id, local_path, "prototype.html", "原型")
            print(f"[secretary] 🎨 原型已更新: {summary}")
            await self._send_barrage_reply(f"🎨 原型更新了：{summary or '内容有更新'}，{addr}")
        except Exception as e:
            print(f"[secretary] 🎨 原型生成失败（跳过本轮，等下次触发）: {e}")

    async def _minutes_loop(self, quiet_sec: float = 8.0, poll: float = 2.0):
        pending_since = None
        try:
            while self._minutes_active and not self._closed:
                await asyncio.sleep(poll)
                if not self._minutes_active or self._closed:
                    break
                if self._minutes_dirty:
                    self._minutes_dirty = False
                    pending_since = time.time()
                    continue
                if pending_since is not None and (time.time() - pending_since) >= quiet_sec:
                    pending_since = None
                    await self._minutes_regen_async()
        except asyncio.CancelledError:
            pass

    async def _minutes_regen_async(self):
        """重新生成整份会议纪要 HTML（事实/决议/待办三模块，全量重生成）。"""
        meeting_id = self.meeting_id or "unknown"
        convo = "\n".join(self._secretary_transcript[-200:])
        rel_dir = f"live_pages/{meeting_id}"
        rel_path = f"{rel_dir}/minutes.html"
        instr = (
            f"[会议纪要更新]\n这是最新的会议转写内容（可能包含之前已经生成过的部分）：\n{convo}\n\n"
            "把这些内容整理/更新成一份结构清晰的单文件 HTML 会议纪要页面。标题下面不要放任何"
            "描述性说明文字，也不要展示会话ID/session id 这类内部标识，只留会议主题。正文严格分"
            "三个模块，不要加多余模块：\n"
            "1. 【事实 Facts】只收录已经在对话里被明确确认/核实过的内容；能确认是谁说的就标注，"
            "不确定发言人身份就如实写\"发言人身份未核实\"，不要编一个名字。\n"
            "2. 【决议 Decisions】每条必须写清楚是谁和谁达成一致/谁拍板决定的，没有明确决策人的"
            "内容不要放进决议模块。\n"
            "3. 【待办 Todos】每条必须写清楚责任人+截止时间（时间没明确说就写\"时间未定\"，责任人"
            "没明确说就写\"责任人未定\"，不要编）。\n"
            f"先执行 `mkdir -p {rel_dir}`，把完整 HTML 写入相对路径文件 `{rel_path}`（cwd 已经是"
            f"{self.remote_workdir}，用相对路径写）。\n"
            "然后看看这段内容里有没有产生【真正新增】的决议事项或待办（宁缺毋滥）：如果有，另起一行"
            "原样输出 `NEW_ITEMS: <逐条列出新增的决议/待办原文，多条用「；」分隔，口语化能直接念的"
            "表达>`；如果没有真正新增的内容，就不要输出这一行。"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "nice", "-n", "19", CLAUDE_BIN, "-p", "--output-format", "text",
                "--dangerously-skip-permissions",
                cwd=self.remote_workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(input=instr.encode()), timeout=150)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError("claude -p 子进程超时(150s)未响应，已终止")
            out_text = (out or err).decode(errors="ignore")
            new_items = ""
            for ln in out_text.splitlines():
                if ln.strip().upper().startswith("NEW_ITEMS:"):
                    new_items = ln.split(":", 1)[1].strip() if ":" in ln else ""
                    break
            local_path = os.path.join(self.remote_workdir, rel_path)
            if not os.path.exists(local_path):
                print(f"[secretary] 🗒 纪要生成子进程没有写出文件，跳过本轮: {out_text.strip()[:120]}")
                return
            addr = await self._publish_page(meeting_id, local_path, "minutes.html", "纪要")
            print(f"[secretary] 🗒 纪要已更新（新增内容={'有' if new_items else '无'}）: {new_items}")
            if new_items:
                await self._send_barrage_reply(f"🗒 纪要新增：{new_items}（完整纪要 {addr}）")
        except Exception as e:
            print(f"[secretary] 🗒 纪要生成失败（跳过本轮，等下次触发）: {e}")

    async def _slides_loop(self, poll: float = 5.0):
        try:
            while self._slides_active and not self._closed:
                await asyncio.sleep(poll)
                if not self._slides_active or self._closed:
                    break
                if self._slides_dirty:
                    self._slides_dirty = False
                    await self._slides_regen_async()
        except asyncio.CancelledError:
            pass

    async def _slides_regen_async(self):
        """重新生成实时会议 Slides 的 <body> 内容（每个议题一页、可翻页）。样式锁定复用
        templates/slides_style_template.html，只让 AI 生成会变的 body 部分，外壳由 Python 拼接。"""
        meeting_id = self.meeting_id or "unknown"
        _new_lines = self._secretary_transcript_count - self._slides_transcript_baseline
        convo = "\n".join(self._secretary_transcript[-_new_lines:]) if _new_lines > 0 else ""
        rel_dir = f"live_pages/{meeting_id}"
        rel_path = f"{rel_dir}/slides.html"
        template_path = self._template_path("slides_style_template.html")
        with open(template_path, encoding="utf-8") as f:
            template_html = f.read()
        style_match = re.search(r"<style>.*?</style>", template_html, re.S)
        STYLE_TEMPLATE = style_match.group(0) if style_match else ""
        script_match = re.search(r"<script>.*?</script>", template_html, re.S)
        SCRIPT_TEMPLATE = script_match.group(0) if script_match else ""
        HEAD_PREFIX = template_html[:style_match.start()] if style_match else ""
        if style_match and script_match:
            _after_style = template_html[style_match.end():script_match.start()]
            _body_open_m = re.search(r"^.*?<body>", _after_style, re.S)
            HEAD_SUFFIX = _body_open_m.group(0) if _body_open_m else "\n</head>\n<body>\n"
            TAIL = template_html[script_match.end():]
        else:
            HEAD_SUFFIX, TAIL = "\n</head>\n<body>\n", "\n</body>\n</html>\n"
        is_first_gen = not self._slides_body_html.strip()
        is_placeholder = not convo.strip() and is_first_gen
        if not convo.strip() and not is_first_gen:
            self._slides_dirty = True
            print("[secretary] 🖥 Slides 本轮没有新转写，跳过（保留现有内容不动）")
            return
        instr = (
            f"[实时会议 Slides 更新]\n这是当前已经生成的 <body> 内容（不是最终页面，只是这部分"
            f"你现在能看到的最新状态；如果显示'还没有生成过'，说明这是第一次生成）：\n"
            f"{self._slides_body_html or '（还没有生成过任何内容）'}\n\n"
            f"这是最新的会议转写内容（可能包含之前已经生成过的部分）：\n"
            f"{convo or '（目前还是空的——技能刚开启，还没有真实转写内容）'}\n\n"
            + ("【这次是占位生成】上面转写内容还是空的——不要编内容、不要瞎猜会议在聊什么。只生成"
               "一页标题页：标题写\"会议进行中\"，副标题写\"内容整理中，会跟着讨论持续更新\"。\n\n"
               if is_placeholder else "") +
            ("【这是本次会议第一次生成，禁止用 NO_UPDATE 跳过】不管上面的转写内容看起来多零散，这次"
             "都必须真正产出内容（哪怕只是一页标题页 + 一句大致归纳的背景说明）。\n\n"
             if is_first_gen and not is_placeholder else "") +
            "把会场内容整理成一份可翻页幻灯片的 <body> 内容（只是 body 部分，外面的"
            "<style>/<script>/<head> 由代码那边直接拼接，你不用输出）。视觉样式已锁定，下面这份"
            f"<style>/<script> 只是让你了解 class 体系怎么用，你的输出里不需要包含这两段：\n"
            f"{STYLE_TEMPLATE}\n\n{SCRIPT_TEMPLATE}\n\n"
            f"完整的参考页面结构：{template_path}（如果需要可以用 Read 工具读取参考 <body> 部分的"
            "结构；大多数情况下靠上面的示例就足够）。\n\n"
            "结构要点：\n"
            "1. 第一页固定是标题页：本轮会议/议题的整体标题 + 一句话背景。标题页文字一旦有实质"
            "内容，都必须写本轮讨论真正得出的论点/结论本身，不要写\"正在讨论\"这类描述生成状态的话。\n"
            "2. 之后每个明确的 topic/议题一页：标题 + 正文要点，如实反映讨论进展，没有结论就写"
            "\"讨论中，暂无结论\"，不要编。【一页一个观点】不要把好几个并列、不相关的观点塞进同一页；"
            "内容多就拆成同一议题下的多页，不要为了省页数硬挤。\n"
            "3. 如果这轮内容里有可以量化呈现的数据/进展，可以用图表呈现；没有真实数据支撑时不要编数字。\n"
            "4. 页码角标由 <script> 自动维护，不需要手写。\n"
            "5. 【主题锁定】" + (
                f"这份 Slides 的整体主题已经被明确指定为「{self._slides_topic}」，直接写进标题页，"
                "不要自己再重新归纳。之后每一轮，会场内容只要跟这个主题明显不相关，都不收录。\n"
                if self._slides_topic else
                "这份 Slides 服务于一个明确的整体主题——上面已经给出了当前的 body 内容，看它定的是"
                "什么主题：这次只把新增的、跟这个已定主题相关的讨论内容补充进去。只有这次是【第一次"
                "生成】时，才需要你自己从会场内容里归纳出整体主题，写进标题页；这次如果是自己归纳的"
                "主题，另起一行原样输出 `TOPIC: <归纳出的主题，几个字概括>`。\n"
            ) +
            "6. 【内容要完整再写，不用固定秒数去猜】（这一条只管【已经有内容存在之后】的后续更新——"
            "如果上面已经说过\"这是第一次生成\"，这一条不适用。）上面的会场转写可能在一句话说到一半"
            "的地方被截断，你要自己读一遍判断：最后一段如果读起来明显不完整，就先不要写进任何卡片。"
            "如果这次转写里完全没有任何新增的、读起来完整的内容，就不用产出任何 body 内容，直接原样"
            "输出 `NO_UPDATE: <一句话说明为什么这轮跳过>`，然后直接结束。\n"
            "输出格式：如果有实质更新，先输出完整的新 body 内容（全量替换），前后用这两行包裹：\n"
            "===SLIDES_BODY_START===\n<这里是完整的新 body 内容>\n===SLIDES_BODY_END===\n"
            "包裹结束后另起一行，原样输出 `UPDATE_SUMMARY: <20字以内，说清这次新增/调整了什么>`。"
            "如果没有实质更新，就只输出 `NO_UPDATE: <原因>` 这一行。\n"
            "全程不需要调用任何工具，直接把上述内容作为你的最终文字回答。"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "nice", "-n", "19", CLAUDE_BIN, "-p", "--output-format", "text",
                "--dangerously-skip-permissions",
                cwd=self.remote_workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(input=instr.encode()), timeout=150)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError("claude -p 子进程超时(150s)未响应，已终止")
            out_text = (out or err).decode(errors="ignore")
            summary = ""
            no_update_reason = ""
            topic_line = ""
            body_match = re.search(r"===SLIDES_BODY_START===\s*(.*?)\s*===SLIDES_BODY_END===", out_text, re.S)
            for ln in out_text.splitlines():
                s = ln.strip()
                if s.upper().startswith("UPDATE_SUMMARY:"):
                    summary = s.split(":", 1)[1].strip() if ":" in s else ""
                elif s.upper().startswith("NO_UPDATE:"):
                    no_update_reason = s.split(":", 1)[1].strip() if ":" in s else "内容还不完整"
                elif s.upper().startswith("TOPIC:"):
                    topic_line = s.split(":", 1)[1].strip() if ":" in s else ""
            if no_update_reason or not body_match:
                if not no_update_reason:
                    no_update_reason = "未识别到有效的 body 输出"
                self._slides_dirty = True
                print(f"[secretary] 🖥 Slides 本轮内容不完整，跳过（{no_update_reason}）")
                return
            new_body = body_match.group(1)
            self._slides_body_html = new_body
            if topic_line and not self._slides_topic:
                self._slides_topic = topic_line
            title_for_head = self._slides_topic or "实时会议 Slides"
            head_prefix_titled = re.sub(r"<title>.*?</title>", f"<title>{title_for_head}</title>",
                                         HEAD_PREFIX, count=1, flags=re.S)
            full_html = head_prefix_titled + STYLE_TEMPLATE + HEAD_SUFFIX + new_body + "\n" + SCRIPT_TEMPLATE + TAIL
            local_path = os.path.join(self.remote_workdir, rel_path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="utf-8") as f:
                f.write(full_html)
            addr = await self._publish_page(meeting_id, local_path, "slides.html", "Slides")
            print(f"[secretary] 🖥 Slides 已更新: {summary}")
            await self._send_barrage_reply(f"🖥 Slides 更新了：{summary or '内容有更新'}，{addr}")
        except Exception as e:
            print(f"[secretary] 🖥 Slides 生成失败（跳过本轮，等下次触发）: {e}")

    async def _talkpoints_loop(self, poll: float = 5.0):
        try:
            while self._talkpoints_active and not self._closed:
                await asyncio.sleep(poll)
                if not self._talkpoints_active or self._closed:
                    break
                if self._talkpoints_dirty:
                    self._talkpoints_dirty = False
                    await self._talkpoints_regen_async()
        except asyncio.CancelledError:
            pass

    async def _talkpoints_regen_async(self):
        """分享要点卡片：跟 _slides_regen_async 共用同一套外壳拼接/发布/弹幕通知，区别是内容
        结构专门蒸馏"一个分享者的完整论述"，视觉样式用 templates/paper_ink_style_template.html。"""
        meeting_id = self.meeting_id or "unknown"
        _new_lines = self._secretary_transcript_count - self._talkpoints_transcript_baseline
        convo = "\n".join(self._secretary_transcript[-_new_lines:]) if _new_lines > 0 else ""
        rel_dir = f"live_pages/{meeting_id}"
        rel_path = f"{rel_dir}/talkpoints.html"
        template_path = self._template_path("paper_ink_style_template.html")
        with open(template_path, encoding="utf-8") as f:
            template_html = f.read()
        style_match = re.search(r"<style>.*?</style>", template_html, re.S)
        STYLE_TEMPLATE = style_match.group(0) if style_match else ""
        script_match = re.search(r"<script>.*?</script>", template_html, re.S)
        SCRIPT_TEMPLATE = script_match.group(0) if script_match else ""
        HEAD_PREFIX = template_html[:style_match.start()] if style_match else ""
        if style_match and script_match:
            _after_style = template_html[style_match.end():script_match.start()]
            _body_open_m = re.search(r"^.*?<body>", _after_style, re.S)
            HEAD_SUFFIX = _body_open_m.group(0) if _body_open_m else "\n</head>\n<body>\n"
            TAIL = template_html[script_match.end():]
        else:
            HEAD_SUFFIX, TAIL = "\n</head>\n<body>\n", "\n</body>\n</html>\n"
        if not self._talkpoints_body_html.strip():
            _deployed_path = os.path.join(self.remote_workdir, rel_path)
            if os.path.exists(_deployed_path):
                with open(_deployed_path, encoding="utf-8") as f:
                    _deployed_html = f.read()
                _pres_m = re.search(r'<div class="presentation">.*?</div>\s*(?=<script>|\Z)', _deployed_html, re.S)
                if _pres_m:
                    self._talkpoints_body_html = _pres_m.group(0)
        is_first_gen = not self._talkpoints_body_html.strip()
        is_placeholder = not convo.strip() and is_first_gen
        if not convo.strip() and not is_first_gen:
            self._talkpoints_dirty = True
            print("[secretary] 🗣 分享要点卡片本轮没有新转写，跳过（保留现有内容不动）")
            return
        instr = (
            f"[分享要点卡片更新]\n这是当前已经生成的 <body> 内容（不是最终页面；如果显示'还没有"
            f"生成过'，说明这是第一次生成）：\n{self._talkpoints_body_html or '（还没有生成过任何内容）'}\n\n"
            f"这是最新的会场转写内容：\n{convo or '（目前还是空的——技能刚开启，还没有真实转写内容）'}\n\n"
            + ("【这次是占位生成】上面转写内容还是空的——不要编内容、不要瞎猜分享者在讲什么。只生成"
               "一页标题页：标题写\"分享进行中\"，副标题写\"核心观点整理中，会跟着分享内容持续更新\"。\n\n"
               if is_placeholder else "") +
            ("【这是本次第一次生成，禁止用 NO_UPDATE 跳过】不管上面的转写内容看起来多零散，这次都必须"
             "真正产出内容。\n\n" if is_first_gen and not is_placeholder else "") +
            "【核心任务，务必想清楚再动笔】这不是'会议纪要/讨论记录'，是专门蒸馏【一个分享者的完整"
            "论述】——从上面的转写里识别出分享者当前正在讲的核心观点是什么，以及支撑这个观点的理由/"
            "论述是什么（转写里夹杂的别人的提问/插话/寒暄不是分享者本人的论述，不要当成观点收录）。\n"
            "视觉样式已锁定，下面这份 <style>/<script> 只是让你了解 class 体系怎么用，你的输出里不"
            f"需要包含这两段：\n{STYLE_TEMPLATE}\n\n{SCRIPT_TEMPLATE}\n\n"
            f"完整的参考页面结构：{template_path}（大多数情况下靠下面的结构要点就够）。\n\n"
            "结构要点：\n"
            "1. 【封面页，第一页固定是这个】承载的是整场分享活动的主题，不是某一个具体观点——通常来"
            "自主持人/开场白的介绍。只知道零星寒暄、还没听到活动名/主题时，先保留占位文案，不要瞎编"
            "凑数。一旦转写里出现了明确的活动名/主题，立刻替换封面标题。封面页不写分享嘉宾姓名——现场"
            "多人共用同一话筒，没有可靠的说话人分离信息，没法准确判断谁是嘉宾。\n"
            "2. 之后每个核心观点独立一页，不要把多个观点挤在同一页。观点本身一句话讲清楚，字要少够"
            "醒目；支撑理由用1-3条，同样简洁；不要给观点页标讲者姓名（同上理由）。如果有分享者原话里"
            "特别值得摘出来的金句，可以作为支撑理由附在页末，或分量足够时单独成一页金句页。没有明确"
            "金句/理由就不要编。\n"
            "3. 页码角标由 <script> 自动维护，不需要手写。\n"
            "4. 页数跟着分享者实际讲了几个核心观点走，不要为了凑页数拆分/合并观点。\n"
            "5. 【内容要完整再写，不用固定秒数去猜】上面的会场转写可能在一句话说到一半的地方被截断，"
            "你要自己读一遍判断完整性；这次转写里完全没有任何新增的、读起来完整的观点内容时，直接原样"
            "输出 `NO_UPDATE: <一句话说明为什么这轮跳过>`（这一条只适用于往后的更新，第一次生成不适用）。\n"
            "输出格式：如果有实质更新，先输出完整的新 body 内容（全量替换），前后用这两行包裹：\n"
            "===TALKPOINTS_BODY_START===\n<这里是完整的新 body 内容>\n===TALKPOINTS_BODY_END===\n"
            "包裹结束后另起一行，原样输出 `UPDATE_SUMMARY: <20字以内，说清这次新增了哪个观点/金句>`。"
            "如果没有实质更新，就只输出 `NO_UPDATE: <原因>`。\n"
            "全程不需要调用任何工具，直接把上述内容作为你的最终文字回答。"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "nice", "-n", "19", CLAUDE_BIN, "-p", "--output-format", "text",
                "--dangerously-skip-permissions",
                cwd=self.remote_workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(input=instr.encode()), timeout=150)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError("claude -p 子进程超时(150s)未响应，已终止")
            out_text = (out or err).decode(errors="ignore")
            summary = ""
            no_update_reason = ""
            body_match = re.search(r"===TALKPOINTS_BODY_START===\s*(.*?)\s*===TALKPOINTS_BODY_END===", out_text, re.S)
            for ln in out_text.splitlines():
                s = ln.strip()
                if s.upper().startswith("UPDATE_SUMMARY:"):
                    summary = s.split(":", 1)[1].strip() if ":" in s else ""
                elif s.upper().startswith("NO_UPDATE:"):
                    no_update_reason = s.split(":", 1)[1].strip() if ":" in s else "内容还不完整"
            if no_update_reason or not body_match:
                if not no_update_reason:
                    no_update_reason = "未识别到有效的 body 输出"
                self._talkpoints_dirty = True
                print(f"[secretary] 🗣 分享要点卡片本轮内容不完整，跳过（{no_update_reason}）")
                return
            new_body = body_match.group(1)
            self._talkpoints_body_html = new_body
            _cover_h1_m = re.search(r'class="slide[^"]*slide-title[^"]*".*?<h1[^>]*>(.*?)</h1>', new_body, re.S)
            _page_title = re.sub(r"<[^>]+>", "", _cover_h1_m.group(1)).strip() if _cover_h1_m else ""
            head_prefix_titled = re.sub(r"<title>.*?</title>",
                                         f"<title>{_page_title or '分享要点卡片'}</title>",
                                         HEAD_PREFIX, count=1, flags=re.S)
            full_html = head_prefix_titled + STYLE_TEMPLATE + HEAD_SUFFIX + new_body + "\n" + SCRIPT_TEMPLATE + TAIL
            local_path = os.path.join(self.remote_workdir, rel_path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="utf-8") as f:
                f.write(full_html)
            addr = await self._publish_page(meeting_id, local_path, "talkpoints.html", "分享要点卡片")
            if is_placeholder:
                reply = None
            elif not self._talkpoints_url_announced:
                self._talkpoints_url_announced = True
                reply = f"🗣 分享要点卡片已生成：{addr}"
            else:
                reply = f"🗣 分享要点卡片更新了：{summary or '内容有更新'}"
            print(f"[secretary] 🗣 分享要点卡片已更新: {summary}")
            if reply:
                await self._send_barrage_reply(reply)
        except Exception as e:
            print(f"[secretary] 🗣 分享要点卡片生成失败（跳过本轮，等下次触发）: {e}")

    # ---------------------------------------------------------------- 收尾

    async def close(self):
        """取消所有还活着的持续技能循环，关掉判断会话。"""
        for _label, _active_attr, task_attr, _loop_name in self._SKILL_KEYS.values():
            task = getattr(self, task_attr, None)
            if task:
                task.cancel()
        if self._judge:
            try:
                await self._judge.close()
            except Exception:
                pass
