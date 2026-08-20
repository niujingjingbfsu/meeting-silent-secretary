import type { MeetingSession, PluginContext, PluginSession } from '@larksuite/agentic-capabilities-playground';
import {
  beginTask,
  endTask,
  findSimilarInFlight,
  initState,
  lastUtterance,
  rememberSaid,
  upsertTranscript,
  type SecretaryStateData,
} from './state';
import { judge } from './judge';

export interface SecretaryConfig {
  autoJoin: boolean;
  wakeWord: string;
  wakeWordAlt: string;
  judgeDebounceMs: number;
}

/**
 * 会中无声助手（会议秘书）——移植自 GitHub niujingjingbfsu/meeting-silent-secretary
 * 的 secretary_client.py（MeetingSecretaryClient）。三条链路跟原版一一对应：
 * - 入会 + 听字幕：持续流，只写 state + 防抖触发判断，不走 turn（高频事件不能次次 dispatch）
 * - 判断层命中 DO：一次有输入的 system turn，交给平台配置好的 agent 真正去执行
 * - 会散：无需处理，秘书不出纪要（范围明确收窄，同原版 SKILL.md）
 *
 * 与 python 原版的架构性差异（不是偷懒，是新平台给的能力本来就更好，见各处注释）：
 * - 判断层从"常驻会话+看门狗重建"简化成每次独立的 ctx.llm.complete（本来就无状态设计）
 * - DO 任务不再要求子任务自己发弹幕+吐 BARRAGE_SENT 标记，改由本插件在拿到 dispatch()
 *   的返回文本后自己发弹幕——消灭了"子任务忘记发/发送失败但没兜底"这整类失败模式
 * - 字幕去重靠 channel-sdk 的 sentenceId 覆盖语义 + selfEcho 字段，不用自己再写
 *   ASR-partial 前缀匹配和"是不是bot自己回声"的名字匹配
 */
export function activate(ctx: PluginContext): void {
  const live = new Map<string, { session: PluginSession; m: MeetingSession }>();
  const pendingInvites: string[] = [];

  const grayReady = (): boolean => {
    if (ctx.capabilities.has('lark.vc.bot.join')) return true;
    void ctx.notify('warn', `会中无声助手不可用：${ctx.capabilities.why('lark.vc.bot.join') ?? '灰度未开通（申请制）'}`);
    return false;
  };
  if (!ctx.capabilities.has('lark.vc.bot.join')) {
    void ctx.notify(
      'warn',
      `会中无声助手暂不可用：${ctx.capabilities.why('lark.vc.bot.join') ?? '灰度未开通（申请制）'}。插件已就绪，能力开放后自动生效。`,
    );
  }

  ctx.hooks.on('resolveSession', (e) => {
    if (e.source !== 'meeting') return undefined;
    return { key: `meeting:${e.meetingId}`, kind: 'secretary-meeting', onBusy: 'queue' as const, ttlMs: Infinity };
  });

  // 秘书没有嘴，唯一的对外产出通道是会中弹幕：约束 DO 任务子 turn 的输出形态。
  ctx.hooks.on('buildPrompt', (p) => {
    if (p.session.kind !== 'secretary-meeting') return;
    p.append(
      '你在代表一个正在参会的静默机器人执行一件被指派的任务：直接输出任务结果的纯文本（不要 JSON、不要 markdown、不要代码块），面向"念出来给所有参会人听"的场景写，不要提脚本文件名/路径/内部实现细节。',
    );
  });

  ctx.channel.on('meeting.invited', (e) => {
    const raw = e.raw as { meetingNo?: string; chatId?: string; callId?: string } | undefined;
    const meetingNo = String(raw?.meetingNo ?? e.meetingId ?? '');
    const chatId = e.chatId ?? raw?.chatId;
    const callId = raw?.callId;
    if (!meetingNo || !grayReady()) return;
    if (!ctx.config.get<SecretaryConfig>().autoJoin) {
      pendingInvites.push(meetingNo);
      void ctx.notify('info', `收到入会邀请（${meetingNo}），autoJoin 已关，执行 /meeting.join 加入`);
      return;
    }
    void join(meetingNo, chatId, callId);
  });

  ctx.registry.command('meeting.join', {
    title: '手动入会（无声助手）',
    scope: 'owner',
    args: { type: 'object', properties: { meetingNo: { type: 'string' } } },
    run: async (a) => {
      const no = (a as { meetingNo?: string })?.meetingNo ?? pendingInvites.shift();
      if (!no) throw new Error('没有待加入的会议，请提供 meetingNo');
      await join(no);
      return `已加入会议 ${no}`;
    },
  });

  // 断线检测：同 plugin-meeting 的 backfill 提醒，秘书这条链路没有纪要可补，只提醒排障方向。
  ctx.timers.every(30_000, async () => {
    for (const [, entry] of live) {
      const st = entry.session.state<SecretaryStateData>();
      if (Date.now() - st.lastEventAt > 120_000) {
        ctx.logger.warn('入会后长时间收不到会议事件', { meetingId: st.meetingId });
        void ctx.notify(
          'warn',
          '入会后收不到会议事件（字幕/弹幕）。请检查应用后台「事件与回调」是否已订阅：vc.bot.meeting_invited_v1、vc.bot.meeting_activity_v1、vc.bot.meeting_ended_v1（订阅方式选长连接）',
        );
      }
    }
  });

  async function join(meetingNo: string, chatId?: string, callId?: string): Promise<void> {
    let m: MeetingSession;
    try {
      m = await ctx.channel.joinMeeting(meetingNo, { callId });
    } catch (e) {
      void ctx.notify('warn', `入会失败（${meetingNo}）：${String(e instanceof Error ? e.message : e)}`);
      return;
    }
    const key = `meeting:${m.meetingId}`;
    const session = ctx.sessions.open(key, { kind: 'secretary-meeting', onBusy: 'queue', ttlMs: Infinity });
    live.set(key, { session, m });
    const st = session.state<SecretaryStateData>();
    initState(st, m.meetingId, m.meetingNo);
    st.chatId = chatId;

    const cfg = ctx.config.get<SecretaryConfig>();
    void m.sendMessage(
      `👋 我是这场会的无声助手，全程只默默听、不会说话。喊"${cfg.wakeWord}"说事情就行。`,
    );

    const debounceKey = `secretary-judge:${m.meetingId}`;

    m.on('transcript', (t) => {
      // 高频事件只写 state，判断用防抖收口——同一段话的字幕中间态在窗口内反复更新，
      // 只在字幕稳定下来之后判断一次，不是每个字幕增量都触发一次 llm.complete。
      upsertTranscript(session.state<SecretaryStateData>(), t);
      ctx.timers.debounce(debounceKey, ctx.config.get<SecretaryConfig>().judgeDebounceMs, () => {
        void runJudge(key);
      });
    });

    m.on('end', () => {
      live.delete(key);
      void session.close();
    });

    ctx.onDeactivate(() => void m.leave());
  }

  async function runJudge(key: string): Promise<void> {
    const entry = live.get(key);
    if (!entry) return;
    const { session, m } = entry;
    const state = session.state<SecretaryStateData>();
    const cfg = ctx.config.get<SecretaryConfig>();
    const wakeWords = [cfg.wakeWord, cfg.wakeWordAlt];
    const verdict = await judge(ctx, state, wakeWords);

    switch (verdict.kind) {
      case 'pass':
        return;
      case 'do':
        void dispatchDo(key, verdict.task);
        return;
      case 'conclusion': {
        const dedupeKey = verdict.text.slice(0, 40);
        if (state.postedConclusions.has(dedupeKey)) return;
        state.postedConclusions.add(dedupeKey);
        ctx.logger.info('记结论', { meetingId: state.meetingId, text: verdict.text });
        return;
      }
      case 'leave':
        ctx.logger.info('判断层决定离会（静默）', { meetingId: state.meetingId });
        await m.leave();
        live.delete(key);
        await session.close();
        return;
      case 'barrage':
        rememberSaid(state, verdict.text);
        await m.sendMessage(verdict.text);
        return;
      case 'reconnect':
        await m.sendMessage('收到，但当前无法自动重连，请联系管理员处理。');
        return;
    }
  }

  async function dispatchDo(key: string, task: string): Promise<void> {
    const entry = live.get(key);
    if (!entry) return;
    const { session, m } = entry;
    const state = session.state<SecretaryStateData>();

    const dup = findSimilarInFlight(state, task);
    if (dup) {
      await m.sendMessage(`（这件事看起来已经在处理了：「${dup.text.slice(0, 50)}」，不重复派发）`);
      return;
    }

    const { id, number } = beginTask(state, task);
    await m.sendMessage(`收到任务${number}：${task}`);

    const utterance = lastUtterance(state) || task;
    const instr =
      `[来自会议的语音指令]\n${task}\n\n` +
      `背景：这条指令来自会议号 ${state.meetingNo}（会议ID=${state.meetingId}），发出指令时的原话是「${utterance}」。\n\n` +
      `请完成它（可使用你已有的记忆和工具）。完成后，最后单独另起两行、原样输出下面两个标记（不要省略、不要改写）：\n` +
      `1) \`TASK_DONE: yes\` 或 \`TASK_DONE: no\`——这个任务本身实质性的目标有没有真正达成（不是"有没有输出点什么"，是"被要求的事到底办成没办成"）：因权限不够/信息缺失/任何原因没能真正完成、只是如实汇报情况的，这里必须写 no。\n` +
      `2) 如果这个任务产出了一个可长期访问的产出物链接（新建/编辑的文档、表格、画板等），额外输出 \`DELIVERABLE_LINK: <完整url>\`；没有产出物链接就不要输出这行。\n` +
      `除了结尾这两行标记之外的正文，就是要念给会场听的结果内容本身——如果任务本质是生成一段文字/文档内容（写JD/文案/总结/方案这类），正文必须直接包含生成出来的完整内容，不能只回一句"已经生成"就完了。`;

    let result: { done: boolean; body: string; link?: string };
    try {
      const r = await session.dispatch({ kind: 'system', intent: 'do_task', interruptible: false, text: instr, hints: { effort: 'high' } });
      if (r.status !== 'done' || !r.text) {
        result = { done: false, body: r.error?.userMessage || r.error?.message || `任务未完成（状态：${r.status}）` };
      } else {
        result = parseDoResult(r.text);
      }
    } catch (e) {
      result = { done: false, body: String(e instanceof Error ? e.message : e) };
    } finally {
      endTask(state, id);
    }

    const header = result.done ? `✅ 任务${number}完成` : `❌ 任务${number}未完成`;
    const link = result.link ? `\n\n${result.link}` : '';
    await m.sendMessage(`${header}\n\n${result.body}${link}`);
  }

  function parseDoResult(text: string): { done: boolean; body: string; link?: string } {
    const lines = text.split('\n');
    let done: boolean | undefined;
    let link: string | undefined;
    const bodyLines: string[] = [];
    for (const line of lines) {
      const s = line.trim();
      const up = s.toUpperCase();
      if (up.startsWith('TASK_DONE:')) {
        done = s.slice(s.indexOf(':') + 1).trim().toUpperCase().startsWith('Y');
        continue;
      }
      if (up.startsWith('DELIVERABLE_LINK:')) {
        link = s.slice(s.indexOf(':') + 1).trim();
        continue;
      }
      bodyLines.push(line);
    }
    const body = bodyLines.join('\n').trim() || text.trim();
    return { done: done ?? false, body, link };
  }
}
