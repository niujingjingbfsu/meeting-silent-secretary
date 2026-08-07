# 会中无声助手（Meeting Silent Secretary）

一个飞书（Lark/Feishu）会中机器人能力：全程不语音发言、不打断会议，只在后台听；被明确
叫到名字时才执行一次性任务，结果通过会中弹幕发出来；支持 6 个可开关的"持续性技能"
（脑暴看板/口述建原型/会议纪要/实时Slides/分享要点卡片/持续答弹幕），开启后常驻运行、
随讨论持续刷新内容，产出可访问的 HTML 页面。

核心特点：**不接任何第三方语音识别（ASR）厂商**，靠飞书会议自带的服务端逐字稿
（`transcript_received` 事件）当"耳朵"——每句话自带准确的说话人身份，让"待办责任人
是谁"这类信息能可靠归属到人；代价是比接自建 ASR 多几秒延迟，秘书角色本来就不需要
实时语音打断，这个代价可以接受。

## 前置条件

1. 一个飞书自建应用（bot），已开通 VC（视频会议）Agent 会中能力——包括：
   - `vc:meeting.bot.join:write`（入会/离会）
   - `vc:meeting.meetingevent:read`（拉取会中事件：逐字稿/弹幕/投屏）
   - 发送会中消息所需的 scope
   
   这块能力目前处于开放/内测节奏中，具体以你租户当前的开通状态为准；如果 `lark-cli`
   调用报 `missing required scope(s)` 之类的错误，跟着 CLI 自带的错误提示（hint）处理，
   不要自己猜 scope 名字。
2. 本机安装并鉴权好 [`lark-cli`](https://github.com/)（`--as bot` 身份可用）。
3. 本机能运行 `claude`（Claude Code CLI），带 `--dangerously-skip-permissions` 权限——
   判断层常驻会话和每个一次性任务/持续技能的生成都靠它。
4. Python 3.9+，`pip install -r requirements.txt`（只需要 `pyyaml`）。
5. 常驻的 asyncio 事件循环——这个进程要作为长期运行的服务，不是一次性脚本。

**不需要**：任何语音/ASR厂商账号（火山引擎/豆包等）——`secretary_client.py` 完全不发起
任何语音相关的网络请求。

## 安装

```bash
cp config_silent.example.yaml config_silent.yaml
# 编辑 config_silent.yaml：至少确认 bot_name/bot_name_alt（唤醒词）
```

## 运行

```bash
python3 secretary_transcript_main.py --meeting-no <9位会议号> --config config_silent.yaml
```

会加入指定的进行中会议，发一条开场白弹幕，之后只听、不打断，直到会议结束或被要求离会。

## 配置项说明

| 配置项 | 必填 | 说明 |
|---|---|---|
| `bot_name` / `bot_name_alt` | 建议填 | 唤醒词，被明确叫到才响应 |
| `judge_model` / `judge_backend` | 建议填 | 判断层用的模型/后端 |
| `reply_chat_id` | 可选 | 弹幕发送失败时的兜底群 |
| `owner_open_id` / `owner_name_variants` | 可选 | 填了才有"会里提到你"私聊提醒 |
| `remote_workdir` | 可选 | 生成内容的工作目录，留空用当前目录 |
| `public_base_url` | 可选 | 产出物对外访问的根地址，留空则只本地保存 |
| `remote_publish_target` | 可选 | `user@host:/path`，配置了才会 scp 发布产出物 |

## 架构速览

```
入会 → 判断层预热(_init_judge)
     → 轮询 _meeting_chat_loop 拉 transcript_received/会中弹幕/投屏文档事件
     → 喂进 _feed_host_transcript（去重+防抖）
     → 判断层输出一个动作标签（PASS / DO: / 结论: / 技能开关 / …)
     → DO 由 _dispatch_secretary_do 派发一个带记忆+工具的子 Claude 去真正执行
     → 子任务完成后自己发会中弹幕（结果 + ✅/❌完成标记）
```

详细设计见 [`SKILL.md`](./SKILL.md)。

## 已知限制

- **议程跟进技能未实现**：判断层不会识别"帮我盯一下议程"这类请求，没有对应的持续技能。
- **不支持语音人设切换**：这个角色设计上就是"不说话"，没有切换成会说话角色的能力
  （那需要接入完全独立的实时语音/TTS系统）。
- **RECONNECT 只能提示，不能真的自动重连**：这个角色没有音频连接，收到"重连"类请求
  时只会回复"当前无法自动重连，请联系管理员处理"，不会真的执行任何重连动作。
- 曾在一次实测中观察到 `DELIVERABLE_LINK`（子任务汇报的产出物链接）在日志里被截断的
  现象，具体原因未查清，不影响任务完成判定本身，但可能导致链接展示不全。
