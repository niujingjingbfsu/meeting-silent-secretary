---
name: meeting-silent-secretary
description: 会中无声助手（会议秘书）——已打包成独立、自包含、可直接安装的能力包（本目录）：会中全程不语音发言，只默默听（走飞书自己的逐字稿，不接第三方 ASR），被明确叫到名字才根据语音指令执行一次性任务，结果发会中弹幕。当用户说"给XX也装一个无声助手/会议秘书能力"、"把这套装成Skill给别的Agent用"、"这个无声助手怎么移植"时使用。
---

# 会中无声助手 (meeting-silent-secretary)

会中一种角色：全程**不语音发言**、不打断会议，只在后台听，被人**明确叫到名字**时才响应，根据语音指令静默执行一件事，结果通过会中弹幕发出来。

本目录是一个**独立、自包含的能力包**（不依赖任何主项目），可以直接复制走使用——见 [`README.md`](./README.md) 的安装/运行步骤。核心实现在 `secretary_client.py`（`MeetingSecretaryClient` 类），入口脚本是 `secretary_transcript_main.py`。

秘书的"耳朵"走**飞书会议自带的逐字稿**（`vc +meeting-events` 的 `transcript_received` 事件），不接火山引擎/豆包等第三方语音大模型。原因：

- 飞书逐字稿服务端已经按参会人分别转写，`payload.speaker.user_name` 自带准确的说话人身份——这是"待办写清楚责任人是谁"这个需求的唯一可靠数据源；混音音频接第三方 ASR 从架构上就拿不到这个信息（一条混音流听不出是谁在说）。
- 零第三方账号依赖，装的人不需要额外开通任何语音厂商的账号/密钥。
- 代价是几秒延迟，但秘书本来就不需要实时语音打断，这个代价完全可以接受。

**范围明确收窄**：本能力包只做"听语音指令→派发子任务干活→回弹幕结果"这一件事，不包含脑暴看板/实时Slides/持续记纪要等常驻生成型的持续性技能——那是完全不同的一套更大范围的能力，刻意没有打包进来。

## 装之前：必须先跟用户讲清楚这是什么（不能跳过）

**装之前**（跑任何安装命令之前），把下面这段话讲给用户，不要用自己的话随意概括、不要只说一句"我来装一下"就直接动手：

> 这是「会中无声助手（会议秘书）」能力：
> - **是什么**：一个飞书会议机器人角色，全程只在后台默默听会议内容，不语音发言、不打断会议。
> - **能干什么**：被明确叫到名字（唤醒词）时，才根据语音指令去执行一件具体的事（查资料/写文档/建表格/查聊天记录等），结果通过会中弹幕发出来；平时只是听，不响应。
> - **靠什么听**：飞书会议自带的服务端逐字稿，不接火山引擎/豆包等第三方语音识别，不需要你额外开通任何语音厂商的账号/密钥。
> - **限制**：只做"听指令→干活→回结果"这一件事，不含实时Slides投屏/脑暴看板/持续记纪要这类常驻生成型能力；这个角色设计上永远不会说话，不能中途切换成会说话的角色。
> - **接下来会怎么装**：装前环境探测 → 拉取代码+装依赖 → 检查/自动装 lark-cli → 六层自检（本地环境/lark-cli本体/lark-cli具体命令有没被裁剪/执行大脑/配置文件/飞书会议权限）→ 生成安装报告 → 需要你填一下唤醒词配置 → 找一场真实会议做冒烟测试确认真的能用。

不要跳过这一步就直接开始跑安装命令——用户需要先知道"这是什么、能干什么、接下来会发生什么"，再决定要不要继续。

## 整体链路

```
入会 → 常驻判断层预热(_init_judge)
     → 轮询 _meeting_chat_loop 拉 transcript_received/会中弹幕/投屏文档事件
     → 按 speaker.user_name 拼行喂进 _feed_host_transcript（去重+防抖）
     → 判断层输出一个动作标签（PASS / DO: / 结论: / LEAVE: / BARRAGE_REPLY: / RECONNECT)
     → DO 由 _dispatch_secretary_do 派发一个带记忆+工具的子 Claude 去真正执行
     → 子任务完成后自己发会中弹幕（结果 + ✅/❌完成标记）
```

默认是**手动模式**：`secretary_transcript_main.py --meeting-no <会议号>` 要显式给会议号
才会入会。想要"别人把 bot 拉进会议就自动触发"，见下面「核心机制五」。

## 核心机制五：被邀请入会自动触发（`secretary_invite_listener.py`，可选）

常驻监听 `vc.bot.meeting_invited_v1`（"bot 被拉进会议"）事件，收到后自动 spawn
`secretary_transcript_main.py` 去加入那场会，不用再手动敲会议号。

**这条路走得比想象中曲折**：先试了 `lark-cli event consume vc.bot.meeting_invited_v1`，
实测 `lark-cli event list` 里根本没收录这个 EventKey（这个命令的目录里只有 user 身份的
`vc.meeting.participant_meeting_*` 系列）；换查飞书官方 Python SDK（`lark-oapi`）自带的
类型化事件处理器，同样没有预置这个事件的 handler。这不是"这个事件不存在"——我们自己生产
环境的 Node.js bridge 用同一个事件 key、走原始 `dispatcher.register()` 注册方式确认过
真实可用，`payload.meeting.meeting_no` 能直接拿到会议号——只是 lark-cli 的事件目录和
lark-oapi 的类型化处理器目录都还没收录它。

最终用 `lark-oapi` 提供的通用逃生舱口 `register_p2_customized_event(事件key字符串, handler)`
绕开类型化目录限制，按原始 key 直接订阅。装这个能力需要额外 `pip install lark-oapi`
（不在 requirements.txt 的默认依赖里），并且要在 config 里单独填一份 `app_id`/`app_secret`
——这个监听器走独立的 WebSocket 长连接，跟 lark-cli 已绑定的凭证是两码事，读不到也不该
去读 lark-cli 的私有凭证存储。

⚠️ **诚实说明**：这份代码验证过 `lark-oapi` 的相关类/方法在当前 SDK 版本里真实存在（构造
签名、`register_p2_customized_event`、`CustomizedEvent.event` 是原始 dict），事件处理器
的去重/spawn 逻辑也用模拟事件测过；但没有拿真实 `app_secret` 跑通一次真实的 WebSocket
连接+真实收到一次邀请事件——因为 app_secret 不在我能读取的范围内。第一次用这个能力的人
请自己冒烟测试一次：把 bot 拉进一场真实会议，确认真的自动 spawn 出了对应进程。

## 核心机制一：判断层角色契约（`secretary_client.py` 的 `_init_judge`）

只回下面几类标签之一，不解释、不寒暄：

- `PASS` —— 还在讨论/闲聊，没有新东西要记
- `DO: <一句可执行指令>` —— 有人**直接叫你的名字**要你去做一件事（查/搜/建文档/总结…）
- `结论: <一句话>` —— 刚达成一个新结论/决定（只记新增的，不重复）
- `LEAVE:` —— 有人要你退会
- `BARRAGE_REPLY: <文字>` —— 有人只是确认"你在不在听"，不是要你干活；秘书没有嘴，这是唯一能确认存在感的出口
- `RECONNECT` —— 有人要求重连；这个能力包没有音频连接，收到后只会回复"无法自动重连，请联系管理员"，不会真的执行重连

唤醒词通过 `bot_name`/`bot_name_alt` 两个实例属性配置，装到另一个 bot 上只需要改这两个值，不用碰 prompt 正文。

## 核心机制二：飞书逐字稿耳朵（`_meeting_chat_loop` + `_feed_host_transcript`）

`_meeting_chat_loop` 轮询 `vc +meeting-events`，同一条轮询处理三种事件：`transcript_received`（逐字稿，带真实说话人）、会中弹幕（`chat_received_items`）、投屏文档（`magic_share_started`）。拼出来的 `[姓名]: 原话` 直接喂进 `_feed_host_transcript`，下游 DO/结论 逻辑天然就能拿到"是谁说的"这个信息。

`_feed_host_transcript` 里有一段 ASR-partial 去重逻辑：如果新一句是上一条的前缀/超集（同一句话的流式增量），原地替换而不是追加，避免一句长话把整个 8 条滑动窗口塞满几乎相同的片段。飞书逐字稿本身整句一次性返回、不太会触发这个坑，但逻辑保留不影响正确性。

## 核心机制三：DO 任务派发与完成契约（`_dispatch_secretary_do` + `_run_async_task`）

`_dispatch_secretary_do(task, debounce_wait, judge_dt)` 做三件事：

1. 去重：`task_board.find_similar_running_task` 查有没有已经在跑的高度相似任务，有就只回一句提醒，不重复派发。
2. 立即发一条"收到任务N：<原文>"弹幕（跟任务清单拼成一条消息一起发，避免两条异步弹幕谁先到不确定的问题）。
3. `asyncio.ensure_future(self._run_async_task(...))` 派发一个带完整记忆+工具的子 Claude 去真正执行，fire-and-forget，不阻塞判断循环。

子任务收到的指令里，**必须**在结尾要求原样输出这三/四个标记（这是完成播报唯一可信的数据源，不能靠猜）：

- `BARRAGE_SENT: yes/no` —— 会中弹幕真的发出去了吗
- `TASK_DONE: yes/no` —— 任务本身的目标**实质性**达成了吗（不是"进程有没有崩"，是"事到底办成没办成"——权限不够、信息缺失导致没做成，哪怕如实汇报了也要写 `no`）
- `SPOKEN_ANSWER: <20字以内口语化总结>` —— 给完成播报用，不能带 URL/路径/代码/markdown 符号
- `DELIVERABLE_LINK: <url>`（可选）—— 任务产出了长期可访问链接时才输出

**身份要求**：子任务的指令里显式要求它自己发起的所有 `lark-cli` 调用都必须带 `--as bot`，
不能依赖默认身份。2026-08-12 用豆包企业版实测踩过这个坑：那个环境里同时绑了操作者本人的
个人身份，子任务发完成弹幕时默认走了个人身份，不是 bot 身份——这不是 CLI 的问题，是指令
本身没有显式指定；已经在 `_run_async_task` 的指令模板里补上这条硬性要求。

**已知问题**：曾在一次实测中观察到 `DELIVERABLE_LINK` 在日志里被截断成半截 URL 的现象，具体原因未查清，不影响任务完成判定本身。

## 核心机制四：判断层与执行层都是可插拔的（`executors.py`）

**出厂默认用 Claude Code**（`ClaudeCodeExecutor` 调本机 `claude` CLI，判断层默认 `claude_judge.ClaudeJudge`），但没有硬编码死——`MeetingSecretaryClient(task_executor=..., judge_factory=...)` 两个构造参数可以整体换掉这两层的"大脑"，接入非 Claude 的 agent。

要点：
- 执行层接口只有一个方法：`async def run(instruction: str, cwd: str) -> str`——传入完整任务指令，返回最终文本（文本里必须包含 `BARRAGE_SENT`/`TASK_DONE`/`SPOKEN_ANSWER`/`DELIVERABLE_LINK` 这几个标记，`_run_async_task` 靠这几个标记解析结果，跟谁执行的无关）。
- **接执行层的agent必须有真实的工具调用能力**（读写文件/跑shell命令/调lark-cli/联网搜索），不是纯 chat completion——任务本身要求"能真的去执行"，不是"生成一段建议文字"。
- 判断层接口是 4 个方法/属性：`start(setup)`/`judge(turn)`/`close()`/`alive`，`claude_judge.py`/`api_judge.py` 都已经按这个约定写好（互为参考实现）。
- 详细用法+代码示例见 [`README.md`](./README.md) 的"接入非 Claude 的 agent"一节。

## 依赖 & 前置条件

- Feishu VC Agent 会中能力——`vc:meeting.bot.join:write`（入会/离会）+ `vc:meeting.meetingevent:read`（拉 `transcript_received`/弹幕事件）+ 发会中消息所需的 scope。装的时候如果 `lark-cli` 报 `missing required scope(s)`，跟着 CLI 自带的 `hint` 走，不要自己猜 scope 名字。
- 一个能真正执行任务的 agent——出厂默认是 Claude CLI（`claude` 命令，带 `--dangerously-skip-permissions`），不想用 Claude 见上面"可插拔"一节自己接。
- 常驻 asyncio 事件循环（进程必须是长期运行的架构，不能是一次性 subprocess）。
- **"lark-cli 已安装"不等于"这个 Skill 需要的命令都在"**：2026-08-13 发现的真实场景——有的运行环境（比如豆包企业版这类企业 agent 客户端）会在自己的沙箱文件夹下自带一份 lark-cli，且做过裁剪/命令黑名单，跟正常渠道装的完整版不是一回事。`onboarding_check.py` 会具体检查 `vc +meeting-join`/`+meeting-leave`/`+meeting-events`/`+meeting-message-send`/`im +messages-send` 这几个命令在当前解析到的 lark-cli 里是否真的存在，而不是只看版本号——如果被裁剪，报错会指向"环境自带的受限副本"而不是让人误以为自己没装对。
- **不需要**：任何第三方语音/ASR 账号（火山引擎/豆包等）——本能力包完全不发起任何语音相关的网络请求。

## 安装步骤

见 [`README.md`](./README.md)：复制本目录 → 填 `config_silent.yaml` → 跑 `python3 onboarding_check.py` 自检本地环境/lark-cli/执行层大脑/配置文件（VC scope 这一层没法零副作用预检，脚本会明确标 ❓，靠下一步冒烟测试验证）→ 冒烟测试（找一场真实进行中的会议，说出唤醒词+一件具体的事，确认收到"收到任务N"弹幕 + 完成后带 ✅/❌ 的弹幕，报错跟着 hint 走）→ 测试完记得 kill 掉进程，不要留残留常驻进程占着已结束的会议轮询。

## 装完之后：必须给用户一份完整安装报告（不能只说"装完了"）

跑完 `onboarding_check.py`（或 `install.sh`，它会自动生成 `INSTALL_REPORT.md`）之后，**把结果转述给用户**，不能只回一句"已安装/可以用了"。至少要包含这几块：

1. **六层自检结果逐条**：本地环境、lark-cli 本体+身份、lark-cli 具体命令是否被裁剪、执行层大脑、配置文件、飞书会议权限——每一项是 ✅/❌/❓，❌ 的项要带上具体是什么问题；❓ 的项要说清楚"这个没法自动验证，需要真实测试才知道"，不能略过不提、也不能说成"已验证通过"。
2. **还需要用户做什么**：比如 `config_silent.yaml` 里的唤醒词有没有填、飞书应用有没有开通对应权限。
3. **接下来怎么做真实验证**：具体的冒烟测试步骤（找一场真实会议、说唤醒词、看有没有收到弹幕）——不能说"已经核验完整可用"就当作测试完成，`onboarding_check.py` 本身明确说了 VC 会议权限这一层没法自动验证，必须实测。
4. **已知限制**：不会说话、不含持续性技能等，见 [`README.md`](./README.md) 的"已知限制"一节。

如果生成了 `INSTALL_REPORT.md`/`diagnostics.json`，直接把这两个文件的内容转述/贴给用户，不要自己重新概括丢失细节。
