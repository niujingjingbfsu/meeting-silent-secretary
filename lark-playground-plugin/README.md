# plugin-meeting-silent-secretary

会中无声助手（会议秘书）——[Lark Playground](https://open.feishu.cn/document/no_class/mcp-archive/lark-playground-installation-guide.md) 插件版，移植自本仓库的 python 实现（`secretary_client.py` / `MeetingSecretaryClient`）。

全程**不语音发言**、不打断会议，只在后台默默听（走飞书自带的会中字幕，不接第三方 ASR）；被人**明确叫到名字**时才根据语音指令静默执行一件事，结果通过会中弹幕发出来。能力范围跟 python 版一致，见仓库根目录 [`../SKILL.md`](../SKILL.md)——那份文档里"装之前必须讲清楚这是什么""能干什么/不能干什么"那两段照样适用，这个插件版没有放宽或收紧任何承诺。

## 安装

这是 Lark Playground 插件，不是独立脚本。先按 [安装指南](https://open.feishu.cn/document/no_class/mcp-archive/lark-playground-installation-guide.md) 把 `lark-playground` 装起来、绑好一个 Agent（Claude Code/Codex/TRAE/CodeM/OpenAI 兼容均可），再装这个插件：

```bash
# 开发调试：符号链接进 plugins/，改代码保存即热重载
lark-playground plugin link ./lark-playground-plugin

# 或者打包分发给别人
lark-playground plugin pack ./lark-playground-plugin   # → plugin-meeting-silent-secretary-0.1.0.zip
```

装完跑 `lark-playground init`（或让向导重新读一次配置）时会看到这几项：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `autoJoin` | `true` | 收到入会邀请时自动加入；关闭后要显式 `/meeting.join` |
| `wakeWord` | `小助手` | 唤醒词，字幕里喊到这个名字才会响应指令 |
| `wakeWordAlt` | （空） | 可选的第二个唤醒词，留空则只认 `wakeWord` |
| `judgeDebounceMs` | `3000` | 字幕稳定等待窗口，同一段话的中间态在这个时间内合并成一次判断；调大能降低判断触发频率（省 token），代价是响应变慢 |

## 前置条件

跟 python 版完全一致（见仓库根 [`../SKILL.md`](../SKILL.md) 的"依赖 & 前置条件"一节）：需要「智能体入会」灰度、会议开启「允许智能体入会」、飞书客户端 7.68+，以及应用后台「事件与回调」订阅 `vc.bot.meeting_invited_v1` / `vc.bot.meeting_activity_v1` / `vc.bot.meeting_ended_v1`（订阅方式选长连接）。这三个事件订阅是这个插件运作的硬前提，不是可选项。

## 跟 python 版的架构性差异（不是偷懒，是新平台原生就更好）

1. **判断层从"常驻会话+看门狗重建"简化成每次独立的 `ctx.llm.complete`**。python 版要维护一个长期存活的判断会话（`_init_judge`/`_rebuild_judge`），卡死了要检测+重连；`ctx.llm.complete` 本身就是无状态、一次一次独立调用的，角色规则每次完整拼进 prompt，反而不需要这一整套生命周期管理。**代价**：无状态意味着每次判断都要把约 1650 字的角色规则原文重新发一遍（python 版的常驻会话只在入会时发一次，后续只发增量），一场会触发几十次判断就重复发几十遍——这是真实的 token 开销，不是可以忽略的细节（2026-08-26 反馈）。`judgeDebounceMs` 默认调到 3000ms 就是为了压这个：字幕合并成更少的判断次数，同等时长内规则文本重复发送的次数也跟着降；平台没有给"常驻判断会话"这个能力，这条路目前只能靠调大防抖窗口缓解，不能根治。
2. **DO 任务不再依赖子任务自己发弹幕**。python 版要求执行任务的子 Claude 自己调 `vc +meeting-message-send` 把结果发出去，并输出 `BARRAGE_SENT: yes/no` 供上层核实——这个架构下"子任务生成了结果但忘了发/发送失败"是要专门兜底的一类真实 bug。插件版里 `session.dispatch()` 直接拿到最终文本（`TurnResult.text`），弹幕由插件本身确定性地发送，子任务不需要、也不允许自己调发送工具——整类失败模式在架构层面消失了，不需要再输出 `BARRAGE_SENT` 这个标记。
3. **字幕去重不用自己写**。python 版要手撕 ASR 中间态前缀匹配（"这句是不是上一句的流式增量"）和"是不是 bot 自己回声"的名字比对；插件版走 channel-sdk 的 `sentenceId` 覆盖语义（中间态被最终态原地替换）+ `selfEcho` 字段（bot 自己的话已经被平台标记出来），`state.ts` 里不需要重写这两段逻辑。
4. **DO 任务并发度**：python 版允许最多 5 个 DO 任务并行跑（各自独立的 `claude` 子进程）；插件版一个会议对应一个 `PluginSession`，`onBusy: 'queue'` 让 DO 任务排队串行执行，同一时刻只有一件事在跑。这是刻意的简化，不是遗漏——多个任务真并行需要为每个任务开独立的非会议 session，属于明显的未来扩展项，不是这次移植要解决的问题，先按队列语义把行为跑对。

## 已知限制

跟 python 版一致：不会语音说话（设计如此，不是故障）；不含实时 Slides 投屏/脑暴看板/持续记纪要这类常驻生成型能力；`RECONNECT` 判断只会回复"无法自动重连"，不会真的执行重连。

## 未验证事项

这份代码通过了 `tsc --noEmit --strict`（对着真实 `@larksuite/agentic-capabilities-playground` 类型声明），但**没有在真实 Lark Playground 实例里跑过一场真实会议**——本机没有安装 `lark-playground` CLI、没有已授权的应用凭据。第一次用的人务必按 python 版 SKILL.md 同样的要求，找一场真实会议冒烟测试一次：入会开场白弹幕是否发出、喊唤醒词后是否收到"收到任务N"确认弹幕、任务完成后是否收到带 ✅/❌ 的结果弹幕。
