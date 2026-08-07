"""会中无声助手（会议秘书）——独立、自包含的判断+派发客户端。

全程不说话、不接任何语音/ASR厂商，靠飞书自己的会中逐字稿(vc +meeting-events 的
transcript_received 事件，带真实说话人身份) + 会中弹幕当"耳朵"，被明确叫到名字才
执行一次性任务(DO)，任务由一个带记忆+工具的子 Claude 去真正执行，结果发会中弹幕。

不包含：ByteView实时音频/豆包ASR/TTS出声（那是主持人等"会说话"角色专用的完全独立路径）、
任何持续性技能（看板/Slides等，那是另一套更大的能力，这里只做"根据语音指令干活"这一件事）、
语音人设切换（会牵连独立的语音/TTS系统）。
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
    """会中无声助手：只听、只在被明确叫到名字时根据语音指令执行一次性任务。"""

    MAX_CONCURRENT_DO_TASKS = 5

    _VALID_VERDICT_PREFIXES = ("PASS", "SPEAK", "DO", "LEAVE", "BARRAGE_REPLY", "结论", "RECONNECT")

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

        self.remote_workdir = os.getcwd()      # 判断层/任务子进程的 cwd

        self._transcript_ear = True
        self._seen_transcript_ids = set()

        self._host_transcript = []    # 会场最近转写(8条滑动窗口)，喂判断层用
        self._host_said = []          # 秘书自己发过的弹幕内容，去重用
        self._sec_posted = set()      # 已发过的结论，去重

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
                "如果有人明确说'重连'/'帮我重连一下'/类似要求重新连接音频的话，回：RECONNECT（系统会"
                "去处理，不需要你自己额外确认）；只是随口聊到'重连'这个词但不是明确指令，不要触发。\n"
                "【会中弹幕】'会场最近的对话'里出现\"[会中弹幕|某某]: 内容\"格式的，是文字弹幕不是"
                "语音，同样按上面规则判断要不要产出结论/DO；如果只是想直接回一条弹幕文字（不是记结论）"
                "→ 回：BARRAGE_REPLY: 后跟文字内容。\n"
                "判断铁律：**宁缺毋滥**——只在真有【新】结论、【新】DO 指令、或有人直接确认存在感时才"
                "产出，其余一律 PASS；绝不重复已记过的、已办过的。\n"
                "每轮严格只回 PASS / DO:… / 结论:… / LEAVE: / BARRAGE_REPLY:… / RECONNECT，"
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
        """每条转写：去重+累积到滑动窗口+触发判断。"""
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

        if self.meeting_id:
            try:
                import task_board
                task_board.append_transcript(self.meeting_id, text)
            except Exception:
                pass

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

    # ---------------------------------------------------------------- 输出

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

    # ---------------------------------------------------------------- 收尾

    async def close(self):
        """关掉判断会话。"""
        if self._judge:
            try:
                await self._judge.close()
            except Exception:
                pass
