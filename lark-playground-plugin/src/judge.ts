import type { PluginContext } from '@larksuite/agentic-capabilities-playground';
import { recentConvo, rememberSaid, type SecretaryStateData } from './state';

export type Verdict =
  | { kind: 'pass' }
  | { kind: 'do'; task: string }
  | { kind: 'conclusion'; text: string }
  | { kind: 'leave' }
  | { kind: 'barrage'; text: string }
  | { kind: 'reconnect' };

const VALID_PREFIXES = ['PASS', 'DO', 'LEAVE', 'BARRAGE_REPLY', '结论', 'RECONNECT'];

function looksValid(s: string): boolean {
  const up = s.toUpperCase();
  return VALID_PREFIXES.some((p) => up.startsWith(p.toUpperCase()) || s.startsWith(p));
}

/** 每次判断都是一次独立的 ctx.llm.complete 调用（无常驻会话/无需预热），
 * 所以角色规则 + 上下文必须每次都完整拼进同一个 prompt 里——这是跟 python 版
 * "_init_judge 预热一次、后续只发增量 turn" 最大的架构差异，但换来的是不需要
 * 判断会话卡死/重建这整套看门狗逻辑，llm.complete 本身就是无状态、每次独立的。 */
function buildPrompt(state: SecretaryStateData, wakeWords: string[]): string {
  const wakeList = wakeWords.filter(Boolean).join("'/'");
  const said = state.said.length ? state.said.join('；') : '（还没说过）';
  const lastSaid = state.said[state.said.length - 1] ?? '';
  const followUp =
    lastSaid && /[？?]\s*$/.test(lastSaid)
      ? `【重要】你刚问的问题是「${lastSaid}」，还没得到明确答复。接下来这句话如果是在回应/确认/重复刚才的请求（哪怕说法不完全一样），要顺着这个问题往下判断该不该 DO；但如果内容明显是完全不相关的新话题，就正常按新内容处理。\n\n`
      : '';
  return (
    `你是这场会议的【会议秘书】，会中【绝不语音发言、不打断】，只默默听、按需在群里产出/执行。\n` +
    `下面给你"会场最近的对话"，每次只做一个判断，严格只回下面之一：\n` +
    `- 还在讨论中 / 没有新结论 / 闲聊寒暄 → 只回：PASS\n` +
    `- 有人【直接叫你】（喊'${wakeList}'）去做一件事（查/搜/发/建文档/查日程/总结…）→ 回：DO: 后跟这件事（把对方原话的诉求转成一句动宾清楚、可直接执行的指令）\n` +
    `- 【务必分清"两人在闲聊商量"和"后面才真正对你说的直接指令"】"会场最近的对话"是一个滑动窗口，可能同时装着两人在闲聊商量的内容和后面才真正对你说的直接指令——如果闲聊里提到的事和后面直接对你说的指令其实是同一件事，只算这一件事、只回一次 DO，不要把闲聊部分也单独拆出来再算一条新指令。判断标准：看这句话本身是不是第一次、明确地在对你提出这个要求，不是看话题内容像不像一个可执行的事。\n` +
    `- 刚刚达成了一个【明确的新结论或决定】 → 回：结论: 后跟一句话概括（只概括【新增】结论，绝不重复之前已记过的）\n` +
    `- 讨论中出现的、没人直接叫你去做的待办，不要自动检测——只有【有人直接叫你】去做才回 DO:，其余一律 PASS。\n` +
    `- 有人要你离开/退出这场会议 → 回：LEAVE:（你不出声，只是静默离会；结合上下文自己拿主意，不是靠固定关键词，拿不准就不要触发）\n` +
    `- 有人直接问你"在不在/听到了没有/能不能听到我说话"这类确认存在感的话（不是要你做事）→ 回：BARRAGE_REPLY: 一句简短确认（比如"在的，听到你说话了"）。\n` +
    `- 有人明确说"重连"/"帮我重连一下"等要求重新连接的话 → 回：RECONNECT；只是随口聊到这个词但不是明确指令，不要触发。\n` +
    `判断铁律：宁缺毋滥——只在真有【新】结论、【新】DO 指令、或有人直接确认存在感时才产出，其余一律 PASS；绝不重复已记过的、已办过的。\n` +
    `每次严格只回 PASS / DO:… / 结论:… / LEAVE: / BARRAGE_REPLY:… / RECONNECT，不要任何解释、不要寒暄。\n\n` +
    `【你已经说过的（别重复这些）】\n${said}\n\n${followUp}` +
    `【会场最近的对话】\n${recentConvo(state)}\n\n` +
    `现在判断（严格只回上面列出的标签之一）：如果最后一句读起来像话没说完，但已经过去好几秒了，大概率是真说完了，别死等。`
  );
}

function parse(raw: string, state: SecretaryStateData): Verdict {
  const r = raw.trim();
  const up = r.toUpperCase();
  if (up.startsWith('DO')) {
    const task = r.slice(2).replace(/^[:：]/, '').trim();
    return task ? { kind: 'do', task } : { kind: 'pass' };
  }
  if (r.startsWith('结论')) {
    const text = r.slice(2).replace(/^[:：]/, '').trim();
    return text ? { kind: 'conclusion', text } : { kind: 'pass' };
  }
  if (up.startsWith('LEAVE')) return { kind: 'leave' };
  if (up.startsWith('BARRAGE_REPLY')) {
    const text = r.slice('BARRAGE_REPLY'.length).replace(/^[:：]/, '').trim();
    return text ? { kind: 'barrage', text } : { kind: 'pass' };
  }
  if (up.startsWith('RECONNECT')) return { kind: 'reconnect' };
  return { kind: 'pass' };
}

/** 判断层偶尔会把内部权衡过程当成最终答案吐出来，而不是干净的标签——同 python 版
 * `_revalidate_verdict`：格式不对就同一个 prompt 后面追加一句提醒重问一次；
 * 仍不合规就安全兜底为 PASS（宁可漏判，不要把半成品当结论处理）。 */
export async function judge(
  ctx: PluginContext,
  state: SecretaryStateData,
  wakeWords: string[],
): Promise<Verdict> {
  const prompt = buildPrompt(state, wakeWords);
  let raw = (await ctx.llm.complete(prompt)).trim();
  if (raw && !looksValid(raw)) {
    ctx.logger.warn('判断结果格式不对，重试一次', { raw: raw.slice(0, 80) });
    const retry = (
      await ctx.llm.complete(
        `${prompt}\n\n（提醒：上一次回答没有严格按照规定的固定格式，只允许回 PASS / DO:… / 结论:… / LEAVE: / BARRAGE_REPLY:… / RECONNECT 之一，不要任何解释。请重新回答这一轮。）`,
      )
    ).trim();
    raw = looksValid(retry) ? retry : '';
  }
  if (!raw) return { kind: 'pass' };
  const verdict = parse(raw, state);
  if (verdict.kind === 'barrage') rememberSaid(state, verdict.text);
  return verdict;
}
