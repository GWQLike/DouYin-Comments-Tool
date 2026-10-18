import type { Task, TaskStatus } from './types';

export const REFRESH_INTERVALS = { low: 2000, normal: 1000, high: 500 } as const;
export type RefreshRate = keyof typeof REFRESH_INTERVALS;
export const REFRESH_LABELS: Record<RefreshRate, string> = { low: '低 · 2 秒', normal: '常规 · 1 秒', high: '高 · 0.5 秒' };
export const HISTORY_MS = 60_000;
const PREFERENCE_KEY = 'douyin.refresh-rate.v1';

export function readRefreshRate(): RefreshRate {
  try {
    const value = localStorage.getItem(PREFERENCE_KEY);
    if (value === 'low' || value === 'normal' || value === 'high') return value;
  } catch { /* Storage can be disabled by the browser. */ }
  return 'normal';
}

export function saveRefreshRate(value: RefreshRate) {
  try { localStorage.setItem(PREFERENCE_KEY, value); } catch { /* Keep the current session preference. */ }
}

export interface SpeedPoint { time: number; rate: number | null }
interface Observation { time: number; count: number; status: TaskStatus }
export interface SpeedHistory {
  taskId: string;
  points: SpeedPoint[];
  previous?: Observation;
}

export function resultCount(task: Task): number {
  return task.kind === 'likes' || task.kind === 'recommendations' ? task.video_count : task.main_count + task.reply_count;
}

// Time comes from the monotonic browser clock, not the nominal polling interval.
export function sampleSpeed(history: SpeedHistory | null, task: Task, time: number): SpeedHistory {
  const current = { time, count: resultCount(task), status: task.status };
  const sameTask = history?.taskId === task.id;
  const previous = sameTask ? history.previous : undefined;
  const elapsed = previous ? time - previous.time : 0;
  let rate: number | null = null;
  if (previous && elapsed > 0 && elapsed <= 5000 && current.count >= previous.count) {
    // Include the last saved page when a running task transitions to a final state.
    const collecting = current.status === 'running' || previous.status === 'running';
    rate = collecting ? (current.count - previous.count) * 1000 / elapsed : 0;
  }
  const points = sameTask ? history.points.filter(point => point.time >= time - HISTORY_MS) : [];
  return { taskId: task.id, previous: current, points: [...points, { time, rate }].slice(-125) };
}

export function interruptSpeed(history: SpeedHistory | null): SpeedHistory | null {
  return history ? { ...history, previous: undefined } : null;
}

export function resultVersion(task: Task): string {
  return [task.id, task.updated_at, task.status, task.main_count, task.reply_count, task.video_count,
    task.unknown_authors, task.collection_complete, task.verification_complete, task.visibility].join('/');
}

// One request cycle at a time. Slow reads do not accumulate queued refreshes.
export function startPolling(work: (signal: AbortSignal) => Promise<void>, interval: number,
  onError: (error: unknown) => void, now = () => performance.now()) {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;
  async function tick() {
    const started = now();
    controller = new AbortController();
    const request = controller;
    const timeout = setTimeout(() => request.abort(), 10_000);
    try { await work(request.signal); }
    catch (error) { if (!stopped) onError(error); }
    finally {
      clearTimeout(timeout);
      if (!stopped) timer = setTimeout(() => void tick(), Math.max(0, interval - (now() - started)));
    }
  }
  void tick();
  return () => { stopped = true; controller?.abort(); clearTimeout(timer); };
}
