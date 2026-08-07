"""直连 Anthropic Messages API 的判断层（给"真正主持人"用，低延迟版）。

与 claude_judge.ClaudeJudge 同接口（start/judge/close/alive），可在 _init_judge 里二选一：
  - ClaudeJudge：走本地 `claude` CLI 常驻会话，每轮 ~2s（瓶颈是 CLI 会话往返开销）。
  - ApiJudge：直连 https://api.anthropic.com/v1/messages，单次 HTTPS 往返，目标亚秒~1s。

设计要点：
  - 2026-07-15 之前：无状态判断，每轮只发当前 turn 文本，不积累 history——图快，但代价是这个判断层
    对"10分钟前/50分钟前聊了什么"完全没有记忆，只能看调用方塞进 turn 里的最近几句转写窗口。
    晶晶实测发现这不符合预期（"不能开着会就把之前聊的事情忘掉"），改成累积整场对话历史：
    self._history 存下每一轮的 user/assistant 消息，每次请求把历史 + 新一轮一起发。
  - 累积历史 + prompt caching 配合使用：每次请求把 self._history 最后一条消息标记 cache_control，
    这段之前的内容会命中缓存，只有本轮新增内容是真正需要重新处理的部分——不会因为历史变长就跟着
    线性变慢（但仍然会比无历史时略慢，因为要读取的缓存本身也在变大）。
    代价/边界（诚实披露）：① ephemeral 缓存 TTL 5 分钟，如果中间隔太久没触发判断（比如一段很长的
    纯人类讨论没人叫它），缓存会过期，下一轮要重新处理全部历史，会有一次延迟尖峰；
    ② 为防止超长会议（1-2小时+）历史无限增长拖爆 context/延迟，设了 MAX_HISTORY_MESSAGES 上限，
    超过后丢最老的一截——这意味着"完整记住整场"在极端超长会议下也有边界，不是绝对无限。
  - 纯 stdlib（urllib）+ asyncio.to_thread，不引第三方依赖；HTTP 调用丢线程池里跑、不阻塞事件循环。
"""

import asyncio
import json
import urllib.request
import urllib.error

DEFAULT_MODEL = "claude-haiku-4-5"   # 判断是轻量二分，用快模型；可在 config judge_api_model 覆盖
DEFAULT_BASE = "https://api.anthropic.com/v1/messages"
MAX_TOKENS = 120
MAX_HISTORY_MESSAGES = 240  # 120 轮（user+assistant各一条）——安全上限，防止超长会议历史无限膨胀


class ApiJudge:
    def __init__(self, api_key: str, model: str = None, base_url: str = None):
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL
        self.base_url = self._normalize_base(base_url) or DEFAULT_BASE
        self._system = ""
        self._ok = bool(api_key)
        self._history = []  # 累积的 user/assistant 消息，让直连 API 也有"整场记忆"

    @staticmethod
    def _normalize_base(base_url: str) -> str:
        """允许 config 只给主机（如 https://api.aicodewith.com），自动补 /v1/messages。
        已含 /messages 路径的原样使用。"""
        if not base_url:
            return ""
        b = base_url.rstrip("/")
        if b.endswith("/messages"):
            return b
        if b.endswith("/v1"):
            return b + "/messages"
        return b + "/v1/messages"

    async def start(self, setup_prompt: str) -> str:
        """记下 setup 作为 system prompt。做一次连通性探测（也顺便预热 TLS）。"""
        self._system = setup_prompt
        if not self._ok:
            return ""
        try:
            return await self.judge("（连通性测试，请回 READY）", timeout=20)
        except Exception as e:
            print(f"[api-judge] start 探测失败: {e}")
            return ""

    def _post(self, turn_text: str, timeout: float) -> str:
        # system 用带 cache_control 的数组形式（而不是纯字符串）开启 prompt caching：setup 这段
        # 每轮都完全相同，不开缓存的话每轮都要重新处理一遍这部分 token，setup 越塞越多（身份/演示/
        # 能力知识…）每轮延迟就跟着涨。开了之后第 2 轮起命中缓存，只有 messages 里的新内容才是"新钱"。
        messages = list(self._history)
        if messages:
            # 给"历史前缀"打一个缓存断点：标在当前历史的最后一条消息上，代表"到这里为止都可以复用缓存"，
            # 本轮真正新增、需要重新处理的只有下面新 append 的这一条 user 消息。
            last = dict(messages[-1])
            content = last.get("content")
            if isinstance(content, str):
                last["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
            messages[-1] = last
        messages.append({"role": "user", "content": turn_text})
        body = json.dumps({
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": [{"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }).encode()
        req = urllib.request.Request(
            self.base_url, data=body, method="POST",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")[:200]
            raise RuntimeError(f"HTTP {e.code}: {detail}")
        # Messages API: data.content = [{type:text, text:...}, ...]
        parts = [c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"]
        result = "".join(parts).strip()
        # 把这轮真正发生的 user/assistant 对话（不带 cache_control 标记，标记只在发请求时临时加）
        # 存进历史，供下一轮复用——整场会议持续积累，让 API 判断层也有连续记忆。
        self._history.append({"role": "user", "content": turn_text})
        self._history.append({"role": "assistant", "content": result})
        if len(self._history) > MAX_HISTORY_MESSAGES:
            self._history = self._history[-MAX_HISTORY_MESSAGES:]
        return result

    @property
    def alive(self) -> bool:
        return self._ok

    async def judge(self, turn_text: str, timeout: float = 30) -> str:
        if not self._ok:
            return ""
        try:
            return await asyncio.to_thread(self._post, turn_text, timeout)
        except Exception as e:
            print(f"[api-judge] judge 失败: {e}")
            return ""

    async def close(self):
        pass
