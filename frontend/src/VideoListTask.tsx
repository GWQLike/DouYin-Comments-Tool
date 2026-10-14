import { Icon } from './icons';
import { dateTime } from './Results';
import { listSortLabel, VIDEO_LIST_ACTIONS, VIDEO_LIST_LABELS } from './videoLists';
import type { Task, VideoListKind } from './types';

export function VideoListTask({ kind, task, active, anyActive, pending, statusLabel, onAction }: {
  kind: VideoListKind;
  task: Task;
  active: boolean;
  anyActive: boolean;
  pending: string | null;
  statusLabel: string;
  onAction: (action: 'start' | 'stop' | 'refresh') => void;
}) {
  const listName = VIDEO_LIST_LABELS[kind];
  const listAction = VIDEO_LIST_ACTIONS[kind];
  const isPrivate = task.visibility === 'private' || task.status === 'private';
  const limitReached = task.max_videos > 0 && task.video_count >= task.max_videos;
  const canRetryAuthors = (task.collection_complete || limitReached) && task.unknown_authors > 0;
  const visibility = isPrivate ? '未公开' : task.visibility === 'public' ? '已公开' : '待确认';
  const progress = isPrivate ? `该账号未公开${listName}列表` : task.status === 'running' ? `正在采集公开${listName}列表，结果实时更新` : task.status === 'verifying' ? '正在核实视频作者信息' : task.status === 'waiting_login' ? '等待浏览器登录或验证，完成后继续处理' : task.collection_complete ? `当前范围内可获取的${listName}视频已采集完成` : limitReached ? `已达到本次 ${task.max_videos} 条采集上限` : task.status === 'created' ? `准备就绪 · 开始后将核对公开抖音号及${listName}列表权限` : task.status === 'failed' ? '采集未完成，请根据下方提示处理后重试' : '进度已保存在本机，可继续采集';
  return <section className="task-card" aria-label={`当前${listName}列表任务`}>
    <div className="task-summary"><div className="video-thumbnail"><Icon name={kind === 'recommendations' ? 'recommend' : 'heart'} size={24} /></div><div className="video-info"><div className="video-heading"><h2>{task.target_nickname || `抖音号 ${task.target_account}`}</h2><span className={`status-badge status-${task.status}`}><span className="status-dot" />{statusLabel}</span></div><div className="video-detail"><span>抖音号 {task.target_account}</span><span>{listName}列表</span></div></div>
      <div className="task-controls">{active ? <button className="button secondary small" onClick={() => onAction('stop')} disabled={Boolean(pending)}><Icon name="stop" size={15} />{pending === 'stop' ? '正在停止' : '停止采集'}</button> : <button className="button primary small" onClick={() => onAction('start')} disabled={Boolean(pending) || anyActive || ((task.collection_complete || limitReached) && !canRetryAuthors) || isPrivate}><Icon name="play" size={15} />{pending === 'start' ? '正在启动' : isPrivate ? '列表未公开' : canRetryAuthors ? '继续核实作者' : limitReached ? '已达上限' : task.status === 'created' ? '开始采集' : task.collection_complete ? '采集已完成' : '继续采集'}</button>}<button className="icon-button refresh-task" title="按当前条件新建批次，保留已有结果" aria-label="重新采集" onClick={() => onAction('refresh')} disabled={Boolean(pending) || active}><Icon name="refresh" size={17} /></button></div>
    </div>
    <div className="stats-grid video-list-stats"><div><span>已获取视频</span><strong className="green-number">{(task.video_count ?? 0).toLocaleString()}<small>条{task.max_videos > 0 ? ` / ${task.max_videos}` : ''}</small></strong></div><div><span>{listName}列表权限</span><strong className="text-stat">{visibility}</strong></div><div><span>结果排序</span><strong className="text-stat">{listSortLabel(kind, task.list_order)}</strong></div></div>
    {task.start_time != null || task.end_time != null ? <div className="task-progress"><span>{listAction}时间：{task.start_time == null ? '不限' : dateTime(task.start_time)} — {task.end_time == null ? '不限' : dateTime(task.end_time)}</span></div> : null}
    <div className="task-progress"><span>{active ? <span className="pulse-dot" /> : <Icon name={isPrivate ? 'shield' : task.collection_complete ? 'check' : 'info'} size={14} />}{progress}</span></div>
    {canRetryAuthors && !isPrivate ? <div className="task-progress"><span><Icon name="info" size={14} />还有 {task.unknown_authors} 位作者的抖音号待核实，可继续处理已有结果。</span></div> : null}
    {task.error && task.error !== progress ? <div className="task-error"><Icon name="info" size={15} /><span>{task.error}</span></div> : null}
  </section>;
}
