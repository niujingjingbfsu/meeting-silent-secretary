# 会中无声助手（Meeting Silent Secretary）

一个飞书（Lark/Feishu）会中机器人能力：全程不语音发言、不打断会议，只在后台听；被明确
叫到名字时才执行一次性任务（根据语音指令干活），结果通过会中弹幕发出来。

核心特点：**不接任何第三方语音识别（ASR）厂商**，靠飞书会议自带的服务端逐字稿
（`transcript_received` 事件）当"耳朵"——每句话自带准确的说话人身份，让"待办责任人
是谁"这类信息能可靠归属到人；代价是比接自建 ASR 多几秒延迟，秘书角色本来就不需要
实时语音打断，这个代价可以接受。

## 前置条件

1. **一个飞书自建应用（bot）**。没有的话先去开放平台建：登录
   [open.feishu.cn](https://open.feishu.cn)（或 [open.larksuite.com](https://open.larksuite.com)
   海外版）开发者后台 → 创建「企业自建应用」→ 在应用能力里添加「机器人」→ 拿到
   `App ID` / `App Secret`（后面第2步要用）。
2. **该应用已开通 VC（视频会议）Agent 会中能力**——包括：
   - `vc:meeting.bot.join:write`（入会/离会）
   - `vc:meeting.meetingevent:read`（拉取会中事件：逐字稿/弹幕/投屏）
   - 发送会中消息所需的 scope

   在应用后台的「权限管理」里申请这几个 scope。这块能力目前处于开放/内测节奏中，具体
   以你租户当前的开通状态为准；如果 `lark-cli` 调用报 `missing required scope(s)` 之类
   的错误，跟着 CLI 自带的错误提示（hint）处理，不要自己猜 scope 名字。
3. **本机安装并绑定好 [`lark-cli`](https://github.com/larksuite/cli)**（bot 身份可用）：
   ```bash
   # 用第1步拿到的 App ID / App Secret 绑定（Secret 走 stdin，不进 shell 历史）
   echo "<你的 App Secret>" | lark-cli config init --app-id <你的 App ID> --app-secret-stdin
   ```
   跑完 `lark-cli auth status --json`，`identities.bot.status` 应该是 `"ready"`。
4. **一个能真正执行任务的"大脑"**——**默认是本机的 `claude`（Claude Code CLI，带
   `--dangerously-skip-permissions`）**，判断层常驻会话和每个一次性任务都靠它。
   装好二进制**还不够**，必须已经登录/配好可用的 API key（`claude --version` 只能证明
   二进制存在，不能证明能真的调通模型；实测方式是随便跑一句 `claude -p "1+1"` 看有没
   有报认证错误）。用默认 Claude 需要本机网络能连通 Anthropic（大部分中国大陆网络环境
   需要能出海）。**如果你想接别的 agent（不是 Claude），见下面"接入非 Claude 的
   agent"一节**——执行层和判断层都是可插拔的，不是非 Claude 不可，只是出厂默认值是
   Claude Code。
5. Python 3.9+，`pip install -r requirements.txt`（只需要 `pyyaml`）。
6. 本机网络能连通飞书开放平台（`open.feishu.cn`/`open.larksuite.com`）——这是硬要求，
   跟用哪个执行层无关。

以上都是运行这个进程本身要满足的条件，不是一个额外要单独搭建的东西——运行方式见下面
"运行"一节，进程本身就是常驻的（不是跑一次就退出的脚本），会一直挂着直到会议结束。

**不需要**：任何语音/ASR厂商账号（火山引擎/豆包等）——`secretary_client.py` 完全不发起
任何语音相关的网络请求。

## 安装

装之前，先花几秒确认基础环境够不够（不下载任何东西，纯只读检查）：

```bash
curl -fsSL https://raw.githubusercontent.com/niujingjingbfsu/meeting-silent-secretary/main/preflight.sh | bash
```

确认过了再装。一条命令（clone + 装依赖 + 生成配置模板 + 跑一遍自检 + 写安装报告；
系统 Python 是 externally-managed 的话会自动改用本地 `.venv`，不动系统 Python）：

```bash
curl -fsSL https://raw.githubusercontent.com/niujingjingbfsu/meeting-silent-secretary/main/install.sh | bash
```

装完会在安装目录下生成 `INSTALL_REPORT.md`——里面是自检原始结果+具体的下一步指引，
不用回来翻这份 README 找下一步该干什么。

或者手动装：

```bash
git clone https://github.com/niujingjingbfsu/meeting-silent-secretary.git
cd meeting-silent-secretary
pip install -r requirements.txt
cp config_silent.example.yaml config_silent.yaml
# 编辑 config_silent.yaml：至少确认 bot_name/bot_name_alt（唤醒词）
python3 onboarding_check.py
# 已经换掉出厂默认 Claude Code、接了自己的 agent 的话：
python3 onboarding_check.py --no-claude-check
# 装不通、需要找人帮忙排查的话，额外打包一份诊断信息（不含任何密钥/token）：
python3 onboarding_check.py --dump-diagnostics report.json
```

会逐层检查本地环境（Python/pyyaml）→ lark-cli 与 bot 身份 → 执行层大脑（默认 Claude
CLI）→ 配置文件，每项标 ✅/❌ 并给出具体修复命令。**飞书 VC Agent 会中权限**这一层
没有已知的零副作用方式能提前验证（读接口都要内部 `meeting_id`，只有真的入会才能拿
到），脚本会明确标成 ❓ 而不是假装通过——这一层只能靠真实冒烟测试验证，见下面"运行"
一节，报错跟着 lark-cli 自带的 hint 处理。

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
| `remote_workdir` | 可选 | 判断层/任务子进程的工作目录，留空用当前目录 |

## 架构速览

```
入会 → 判断层预热(_init_judge)
     → 轮询 _meeting_chat_loop 拉 transcript_received/会中弹幕/投屏文档事件
     → 喂进 _feed_host_transcript（去重+防抖）
     → 判断层输出一个动作标签（PASS / DO: / 结论: / LEAVE: / BARRAGE_REPLY: / RECONNECT)
     → DO 由 _dispatch_secretary_do 派发一个带记忆+工具的子 Claude 去真正执行
     → 子任务完成后自己发会中弹幕（结果 + ✅/❌完成标记）
```

详细设计见 [`SKILL.md`](./SKILL.md)。

## 接入非 Claude 的 agent

本包默认用 Claude Code 干活，但"判断"和"干活"这两层都做成了可插拔接口（`executors.py`），
不是必须用 Claude：

```python
from secretary_client import MeetingSecretaryClient
from executors import TaskExecutor

class MyAgentExecutor(TaskExecutor):
    async def run(self, instruction: str, cwd: str) -> str:
        # 用你自己的 agent 执行 instruction（必须有等价的工具调用能力：读写文件/跑shell/
        # 联网搜索/调 lark-cli 操作飞书——这不是一次纯文本生成，是真的要能"去执行"），
        # 返回它的最终文本输出。这段文本里必须包含 instruction 里要求的
        # BARRAGE_SENT/TASK_DONE/SPOKEN_ANSWER/DELIVERABLE_LINK 这几个标记，
        # MeetingSecretaryClient 靠这几个标记解析任务是否真的完成。
        return await your_agent.run_with_tools(instruction, cwd=cwd)

def my_judge_factory():
    # 返回一个实现了 start/judge/close/alive 这4个方法的对象，接口约定见 executors.py 顶部。
    return MyAgentJudge(...)

client = MeetingSecretaryClient(task_executor=MyAgentExecutor(), judge_factory=my_judge_factory)
```

**接执行层要注意**：这不是简单换一个 chat completion API 端点——`_run_async_task` 里
的任务本质是"查资料/写文档/建表格/调飞书API"这类需要真实执行能力的事，你接的 agent 必须
自带等价的工具调用/文件读写/shell执行能力，纯文本生成模型接不上去。

## 已知限制

- **只做"听指令→干活→回结果"这一件事**：不包含脑暴看板/实时Slides/持续记纪要等常驻
  生成型能力，那是完全不同的一套更大范围的能力，本包刻意不含。
- **不支持语音人设切换**：这个角色设计上就是"不说话"，没有切换成会说话角色的能力
  （那需要接入完全独立的实时语音/TTS系统）。
- **RECONNECT 只能提示，不能真的自动重连**：这个角色没有音频连接，收到"重连"类请求
  时只会回复"当前无法自动重连，请联系管理员处理"，不会真的执行任何重连动作。
- 曾在一次实测中观察到 `DELIVERABLE_LINK`（子任务汇报的产出物链接）在日志里被截断的
  现象，具体原因未查清，不影响任务完成判定本身，但可能导致链接展示不全。
