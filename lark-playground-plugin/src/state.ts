import type { MeetingEventMap } from '@larksuite/agentic-capabilities-playground';

type Transcript = Parameters<MeetingEventMap['transcript']>[0];

/** 判断层只需要"最近几句"，不是全场记录——跟 python 版 `_host_transcript[-8:]` 同一个尺度 */
const JUDGE_WINDOW_CAP = 12;

/**
 * 会话级状态：字幕滑窗（供判断层用）+ 已说过的话（防重复）+ 正在跑的 DO 任务（防重复派发）。
 * 存于 session.state<SecretaryStateData>()，随 session（会议）回收。
 */
export interface SecretaryStateData {
  meetingId: string;
  meetingNo: string;
  chatId?: string;
  /** sentenceId → 定稿句，覆盖语义同 sentence_id（ASR 中间态被最终态原地替换，不重复累积） */
  sentences: Record<string, { speaker: string; text: string }>;
  order: string[];
  /** 秘书自己说过的弹幕，供 prompt 里"别重复"用，同 python 版 _host_said */
  said: string[];
  /** 已经记过的结论摘要（前 40 字去重键），同 python 版 _sec_posted */
  postedConclusions: Set<string>;
  /** 正在跑的 DO 任务，供去重判断用；任务结束（成功/失败）后自行移除 */
  inFlight: Array<{ id: string; text: string; startedAt: number }>;
  /** 下一个任务编号（仅用于弹幕里的「收到任务N」标号，从 1 开始递增，不回收） */
  nextTaskNumber: number;
  lastEventAt: number;
}

export function initState(data: SecretaryStateData, meetingId: string, meetingNo: string): void {
  data.meetingId = meetingId;
  data.meetingNo = meetingNo;
  data.sentences = {};
  data.order = [];
  data.said = [];
  data.postedConclusions = new Set();
  data.inFlight = [];
  data.nextTaskNumber = 1;
  data.lastEventAt = Date.now();
}

export function upsertTranscript(data: SecretaryStateData, t: Transcript): void {
  if (t.selfEcho) return; // channel-sdk 已做自身回声过滤，秘书自己的话不进判断窗口
  const text = (t.text || '').trim();
  if (!text) return;
  if (!(t.sentenceId in data.sentences)) {
    data.order.push(t.sentenceId);
    if (data.order.length > JUDGE_WINDOW_CAP) {
      const evicted = data.order.splice(0, data.order.length - JUDGE_WINDOW_CAP);
      for (const id of evicted) delete data.sentences[id];
    }
  }
  data.sentences[t.sentenceId] = { speaker: t.speaker.name ?? t.speaker.id, text };
  data.lastEventAt = Date.now();
}

/** 判断层用的"会场最近的对话"文本，格式对齐 python 版：`[姓名]: 原话` 每行一句 */
export function recentConvo(data: SecretaryStateData): string {
  const lines = data.order
    .map((id) => data.sentences[id])
    .filter((s): s is NonNullable<typeof s> => Boolean(s))
    .map((s) => `[${s.speaker}]: ${s.text}`);
  return lines.length ? lines.join('\n') : '（暂时没人发言）';
}

export function lastUtterance(data: SecretaryStateData): string {
  const id = data.order[data.order.length - 1];
  return id ? data.sentences[id]?.text ?? '' : '';
}

export function rememberSaid(data: SecretaryStateData, text: string): void {
  data.said.push(text);
  data.said = data.said.slice(-6);
}

/** 简单粗暴的重复度判断：按空白/标点切词，Jaccard 交并比 > 阈值即视为同一件事。
 * 不追求精确匹敌 NLP 相似度，只挡住"同一件事被判断层拆成两条几乎一样的 DO"这种真实场景。 */
function similar(a: string, b: string, threshold = 0.6): boolean {
  const tokenize = (s: string) => new Set(s.replace(/[，。！？~?!,.\s]+/g, ' ').split(' ').filter(Boolean));
  const sa = tokenize(a);
  const sb = tokenize(b);
  if (!sa.size || !sb.size) return false;
  let inter = 0;
  for (const t of sa) if (sb.has(t)) inter += 1;
  const union = sa.size + sb.size - inter;
  return union > 0 && inter / union >= threshold;
}

export function findSimilarInFlight(
  data: SecretaryStateData,
  task: string,
): { id: string; text: string } | undefined {
  return data.inFlight.find((t) => similar(t.text, task));
}

export function beginTask(data: SecretaryStateData, task: string): { id: string; number: number } {
  const id = `do-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  const number = data.nextTaskNumber;
  data.nextTaskNumber += 1;
  data.inFlight.push({ id, text: task, startedAt: Date.now() });
  return { id, number };
}

export function endTask(data: SecretaryStateData, id: string): void {
  data.inFlight = data.inFlight.filter((t) => t.id !== id);
}
