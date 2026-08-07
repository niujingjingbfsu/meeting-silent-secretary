"""可插拔的"干活后端"接口——让本 Skill 不绑死在 Claude Code 上。

`MeetingSecretaryClient` 需要两类"大脑"：
1. **判断层（judge）**：常驻、多轮的会话，每轮收到"会场最近对话"文本，吐出一个动作标签
   （PASS/DO:.../结论:.../…）。接口约定（duck typing，不强制继承）：
   - `async def start(setup_prompt: str) -> str`——入会时调一次，注入角色规则。
   - `async def judge(turn_text: str, on_delta=None) -> str`——每轮转写调一次，返回判断结果。
   - `async def close()`
   - `alive`（property，bool）——常驻会话是否还活着。
   本包自带两份实现：`claude_judge.ClaudeJudge`（本地 `claude` CLI 常驻会话）、
   `api_judge.ApiJudge`（直连 Anthropic API）。都是 Claude 家族模型。

2. **任务执行器（task executor）**：一次性、【带完整工具调用能力】（读写文件、跑 shell
   命令、调 lark-cli 操作飞书、联网搜索……）执行一件具体任务，返回最终文本（文本里必须
   包含 `_run_async_task` 要求的 `BARRAGE_SENT:`/`TASK_DONE:`/`SPOKEN_ANSWER:`/
   `DELIVERABLE_LINK:` 这几个标记，`MeetingSecretaryClient` 靠这几个标记解析结果）。
   这一层**没有内置的非 Claude 实现**——本包只带了 `ClaudeCodeExecutor`（默认，调本机
   `claude` CLI）。想接别的 agent（GPT/Gemini/别的内部agent框架…），继承下面的
   `TaskExecutor` 抽象类实现 `run()` 即可，你的 agent 必须具备等价的工具调用能力
   （不是单纯的一次文本生成/chat completion，任务本身要求"能真的去执行"）。

用法：
    client = MeetingSecretaryClient(task_executor=YourCustomExecutor())
    # judge 层同理可传 judge_factory=lambda: YourCustomJudge(...)
"""
import asyncio
import time
from abc import ABC, abstractmethod


class TaskExecutor(ABC):
    """一次性、带工具调用能力的任务执行后端。"""

    @abstractmethod
    async def run(self, instruction: str, cwd: str) -> str:
        """执行 instruction（一段完整的自然语言任务指令+输出格式要求），返回最终文本。
        执行失败/超时应该 raise 一个异常（调用方 _run_async_task 会捕获，当作任务失败处理），
        不要静默返回空字符串或部分结果，否则调用方没法区分"真的没做成"和"做成了但没内容"。"""
        raise NotImplementedError


class ClaudeCodeExecutor(TaskExecutor):
    """默认实现：调本机 `claude` CLI（Claude Code），带完整记忆+工具（skills/Bash/Read/
    Write/WebSearch等），`--dangerously-skip-permissions` 免交互确认。这是本 Skill 出厂
    自带的唯一执行后端；换别的 agent 执行时不要改这个类，写一个新的 TaskExecutor 子类。"""

    CLAUDE_BIN = "claude"

    def __init__(self, timeout: float = 240.0, nice: int = 19):
        self.timeout = timeout
        self.nice = nice

    async def run(self, instruction: str, cwd: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "nice", "-n", str(self.nice), self.CLAUDE_BIN, "-p", "--output-format", "text",
            "--dangerously-skip-permissions", instruction,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"claude -p 子进程超时({self.timeout:.0f}s)未响应，已终止")
        if proc.returncode != 0:
            raise RuntimeError(f"claude -p 子进程异常退出(code={proc.returncode}): "
                                f"{(out or err).decode(errors='ignore')[:300]}")
        return (out or err).decode(errors="ignore")
