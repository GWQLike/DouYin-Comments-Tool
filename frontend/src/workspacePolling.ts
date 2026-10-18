import { resultVersion } from './monitor';
import type { CommentPage, Session, Task, VideoPage } from './types';

type ReadApi = <T>(path: string, options?: RequestInit) => Promise<T>;

export function createWorkspaceReader(read: ReadApi, now = () => performance.now()) {
  let overviewAt = -Infinity;
  let previousKey = '';
  let previousVersion = '';
  return async (id: string | null, listMode: boolean, query: string, signal: AbortSignal) => {
    const overviewDue = now() - overviewAt >= 5000;
    // High-frequency reads only recount the selected task, not every historical result.
    const [tasks, session, selected] = await Promise.all([
      overviewDue ? read<Task[]>('/tasks', { signal }) : undefined,
      overviewDue ? read<Session>('/session', { signal }) : undefined,
      id && !overviewDue ? read<Task>(`/tasks/${encodeURIComponent(id)}`, { signal }) : undefined,
    ]);
    const task = selected ?? tasks?.find(item => item.id === id);
    const sampledAt = now();
    const key = `${id}/${listMode}/${query}`;
    const version = task ? resultVersion(task) : '';
    let data: CommentPage | VideoPage | undefined;
    if (task && (key !== previousKey || version !== previousVersion)) {
      data = await read<CommentPage | VideoPage>(`/tasks/${encodeURIComponent(task.id)}/${listMode ? 'videos' : 'comments'}?${query}`, { signal });
    }
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
    if (overviewDue) overviewAt = sampledAt;
    previousKey = key;
    previousVersion = version;
    return { tasks, session, task, data, sampledAt };
  };
}
