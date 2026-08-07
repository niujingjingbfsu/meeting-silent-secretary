"""常驻 Claude 判断会话（给"真正主持人"用）。

每次起一个 `claude -p` 子进程要 ~4s（几乎全是启动开销）。这里改成：入会时起【一个】
claude 流式会话进程并预热，之后每条会场转写作为一轮发进去判断——热轮只要 ~1.7-2s。

注意：流式会话的上下文会随轮数累积，聊久了每轮变慢（实测从 ~2s 涨到 5-9s）。所以每
做 MAX_TURNS 轮就自动重启一次会话（重注入角色规则、清掉历史），把延迟压回 ~2s。
重启会丢"说过的话"记忆，故判断时由调用方把"已说过的话"一并塞进 turn 文本来防重复。

2026-07-10：应晶晶要求，去掉了 --strict-mcp-config/空 --mcp-config（原本故意剥掉工具+
记忆换速度），改成不限制 MCP、加 --dangerously-skip-permissions——现在这就是一个真正
【带完整记忆+全部技能】的 claude 会话，cwd 传主目录（不是 /tmp）才能读到 CLAUDE.md/
memory。代价：会更慢（多大没测过，她说先不管耗时）。

用法：
    judge = ClaudeJudge(model=None, cwd="/tmp")
    await judge.start(setup_prompt)        # 入会时调一次，付一次冷启动 + 注入角色/规则
    verdict = await judge.judge(turn_text)  # 每条转写调，返回 'PASS' 或 'SPEAK: ...'
    await judge.close()
"""

import asyncio
import json

MAX_TURNS = 0   # 0=禁用定期重启。实测连打 18 轮判断耗时无增长（2.03→1.93s），"上下文膨胀拖慢"不成立；
                # 而每次重启的冷启动正是"3 分钟后哑火"的元凶。卡死改由上层看门狗(12s)兜底，不再主动重启。


class ClaudeJudge:
    def __init__(self, model: str = None, cwd: str = "/tmp", effort: str = "low"):
        self.model = model
        self.cwd = cwd
        # 2026-07-23 真实复现：判断层本职是"PASS/SPEAK/DO/…这几个标签选一个"的轻量分类，不是
        # 深度推理任务，但默认 effort 下频繁触发扩展思考（哪怕是 PASS 这种最简单判断也常见
        # 3~7 轮 thinking_tokens、耗时到10s+），实测拖慢了响应。对比测试（同一批模糊/易误判的
        # 输入）：默认 effort 下出现过 2 次误触发技能开关（把"要不要整理一下"这类闲聊误判成
        # BOARD_ON/DO），改 --effort low 后这两次误判消失，判断更快也更准——分类任务本来就不
        # 需要"多想"，想多了反而更容易过度解读。默认 low，真需要更谨慎判断时可传别的档位覆盖。
        self.effort = effort
        self.proc = None
        self._lock = asyncio.Lock()   # 串行化各轮（共享同一对 stdin/stdout 管道）
        self._setup = ""              # 记下 setup，重启时重注入
        self._turns = 0               # 自上次（重）启以来的判断轮数

    async def _spawn(self):
        args = [
            "claude", "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",  # 2026-07-29：拿到逐字增量事件，供 judge() 的 on_delta 做流式播报——
                                            # 实测(/tmp/test_stream.py)首个文本增量比完整 result 早 1.3~1.4s(~45%)。
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )

    async def start(self, setup_prompt: str) -> str:
        """起常驻进程并发第一轮（注入角色/判断规则）——这一轮付掉冷启动开销。"""
        self._setup = setup_prompt
        await self._spawn()
        async with self._lock:
            self._turns = 0
            return await self._turn(setup_prompt, timeout=40)

    async def _kill_proc(self):
        if not self.proc:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.is_closing():
                self.proc.stdin.close()
            await asyncio.wait_for(self.proc.wait(), timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    async def _refresh(self):
        """会话累积太久 → 重启进程、重注入 setup，清掉膨胀历史（在 _lock 内调用）。"""
        await self._kill_proc()
        await self._spawn()
        self._turns = 0
        try:
            await self._turn(self._setup, timeout=40)  # 重新预热（重注入角色规则）
        except Exception:
            pass

    async def _send(self, text: str):
        self._seq = getattr(self, "_seq", 0) + 1
        print(f"[judge-diag] >>> send #{self._seq}: {text[:60]!r}")
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def _turn(self, text: str, timeout: float, on_delta=None) -> str:
        """发一轮、读到 result 事件为止，返回 assistant 文本。
        2026-07-10 诊断：真实会议里发现偶发"瞬间返回、内容跟当前轮不符"的污染，孤立测试
        复现不出来——加这段诊断打印，等下次真实发作时能从原始事件流里看到具体是哪一步错位。
        2026-07-29：on_delta(text_chunk) 可选——`--include-partial-messages` 打开后，模型还在
        生成时会先吐一串 stream_event/content_block_delta，每条带一小段增量文本，早于最终的
        result 事件到达（真实测过：SPEAK 类判断首个增量比 result 早 1.3~1.4s）。调用方拿这个
        回调可以在完整判断还没出来之前，就把已经生成出来的文本喂给嘴去念，省掉这部分等待。
        这里只做"転发增量"，不判断标签/不做任何决策——那些逻辑留给调用方。"""
        await self._send(text)
        seq = self._seq
        n = 0
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=timeout)
            if not line:
                print(f"[judge-diag] <<< #{seq} EOF after {n} events")
                return ""  # 进程 EOF
            n += 1
            try:
                ev = json.loads(line)
            except Exception:
                print(f"[judge-diag] #{seq} event#{n} 非JSON行: {line[:100]!r}")
                continue
            t = ev.get("type")
            if t == "result":
                res = (ev.get("result") or "").strip()
                print(f"[judge-diag] #{seq} event#{n} type=result subtype={ev.get('subtype')} "
                      f"session_id={str(ev.get('session_id'))[:8]} -> {res[:60]!r}")
                return res
            elif t == "stream_event" and on_delta:
                se = ev.get("event") or {}
                if se.get("type") == "content_block_delta":
                    delta = se.get("delta") or {}
                    chunk = delta.get("text") or ""
                    if chunk:
                        try:
                            on_delta(chunk)
                        except Exception as e:
                            print(f"[judge-diag] on_delta 回调出错（忽略，不影响判断本身）: {e}")
            else:
                print(f"[judge-diag] #{seq} event#{n} type={t} subtype={ev.get('subtype')}")

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def judge(self, turn_text: str, timeout: float = 30, on_delta=None) -> str:
        """发一条判断轮，返回模型输出（'PASS' / 'SPEAK: ...'）；进程死了或超时返回空串。
        on_delta：见 _turn() 说明，可选的流式增量回调。"""
        async with self._lock:
            try:
                # MAX_TURNS=0 时禁用定期重启（见顶部说明）；>0 时才在累积过多轮后刷新
                if MAX_TURNS and self._turns >= MAX_TURNS and self._setup:
                    await self._refresh()
                if not self.alive:
                    return ""
                res = await self._turn(turn_text, timeout=timeout, on_delta=on_delta)
                self._turns += 1
                return res
            except asyncio.TimeoutError:
                return ""
            except Exception:
                return ""

    async def close(self):
        await self._kill_proc()
