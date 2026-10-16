import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, errorMessage, filterQuery, responseError } from './api';
import { Icon } from './icons';
import { Results, dateTime } from './Results';
import { VideoListResults } from './VideoListResults';
import { VideoListTask } from './VideoListTask';
import { AuthorVerification } from './AuthorVerification';
import { VIDEO_LIST_ACTIONS, VIDEO_LIST_LABELS } from './videoLists';
import type { CommentPage, Filters, VideoPage, VideoListDraft, VideoListKind, Session, Task, TaskKind, TaskStatus } from './types';

const DEFAULT_FILTERS: Filters = { account: '', keyword: '', mode: 'contains', threshold: 85, start: '', end: '' };
const STATUS_LABELS: Record<TaskStatus, string> = { created: '待开始', waiting_login: '等待登录', running: '采集中', verifying: '核实作者中', paused: '已暂停', completed: '已完成', partial: '部分完成', failed: '采集失败', private: '列表未公开' };
const ACTIVE_STATUSES: TaskStatus[] = ['running', 'verifying', 'waiting_login'];
const DEFAULT_LIST_DRAFT: VideoListDraft = { account: '', maxVideos: '', start: '', end: '' };
const WORKSPACE_MODES = [
  { kind: 'comments', label: '视频评论检索', icon: 'search' },
  { kind: 'likes', label: '公开喜欢列表', icon: 'heart' },
  { kind: 'recommendations', label: '公开推荐列表', icon: 'recommend' },
] as const;

function taskTitle(task: Task) { if (task.kind === 'likes' || task.kind === 'recommendations') return `${task.target_nickname || task.target_account} · ${VIDEO_LIST_LABELS[task.kind]}列表`; return task.title || (task.video_id ? `视频 ${task.video_id}` : task.status === 'running' ? '正在解析视频' : '待解析分享链接'); }
function safeVideoUrl(task: Task): string | undefined {
  if (task.video_id && /^\d+$/.test(task.video_id)) return `https://www.douyin.com/video/${task.video_id}`;
  try { const url = new URL(task.source_url); return url.protocol === 'https:' && (url.hostname === 'douyin.com' || url.hostname.endsWith('.douyin.com')) ? url.href : undefined; } catch { return undefined; }
}

function StatusBadge({ status }: { status: TaskStatus }) {
  return <span className={`status-badge status-${status}`}><span className="status-dot" />{STATUS_LABELS[status]}</span>;
}

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [session, setSession] = useState<Session>({ status: 'logged_out', error: null });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [mode, setMode] = useState<TaskKind>('comments');
  const [source, setSource] = useState('');
  const [listDrafts, setListDrafts] = useState<Record<VideoListKind, VideoListDraft>>({ likes: { ...DEFAULT_LIST_DRAFT }, recommendations: { ...DEFAULT_LIST_DRAFT } });
  const [draft, setDraft] = useState<Filters>({ ...DEFAULT_FILTERS });
  const [filters, setFilters] = useState<Filters>({ ...DEFAULT_FILTERS });
  const [page, setPage] = useState(1);
  const [resultState, setResultState] = useState<{ key: string; data: CommentPage } | null>(null);
  const [videoResultState, setVideoResultState] = useState<{ key: string; data: VideoPage } | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [resultError, setResultError] = useState<string | null>(null);
  const [serviceError, setServiceError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [filterError, setFilterError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [reload, setReload] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const sourceRef = useRef<HTMLTextAreaElement>(null);
  const accountRef = useRef<HTMLInputElement>(null);
  const task = tasks.find(item => item.id === selectedId && (item.kind ?? 'comments') === mode);
  const visibleTasks = tasks.filter(item => (item.kind ?? 'comments') === mode);
  const listKind = mode === 'comments' ? null : mode;
  const listMode = listKind !== null;
  const listDraft = listKind ? listDrafts[listKind] : DEFAULT_LIST_DRAFT;
  const listName = listKind ? VIDEO_LIST_LABELS[listKind] : '';
  const listAction = listKind ? VIDEO_LIST_ACTIONS[listKind] : '';
  const active = Boolean(task && ACTIVE_STATUSES.includes(task.status));
  const resultQuery = listMode ? `page=${page}&page_size=50` : filterQuery(filters, page);
  const resultKey = selectedId ? `${mode}/${selectedId}?${resultQuery}` : '';
  const results = resultState?.key === resultKey ? resultState.data : null;
  const videos = videoResultState?.key === resultKey ? videoResultState.data : null;
  const videoUrl = task ? safeVideoUrl(task) : undefined;
  const dirtyFilters = JSON.stringify(draft) !== JSON.stringify(filters);
  const anyActive = tasks.some(item => ACTIVE_STATUSES.includes(item.status));

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const [nextTasks, nextSession] = await Promise.all([api<Task[]>('/tasks', { signal: controller.signal }), api<Session>('/session', { signal: controller.signal })]);
        if (controller.signal.aborted) return;
        setTasks(nextTasks);
        setSession(nextSession);
        setSelectedId(current => current && nextTasks.some(item => item.id === current && (item.kind ?? 'comments') === mode) ? current : nextTasks.find(item => (item.kind ?? 'comments') === mode)?.id ?? null);
        setServiceError(null);
      } catch (error) {
        if (!controller.signal.aborted) setServiceError(errorMessage(error));
      } finally {
        if (!controller.signal.aborted) { setLoaded(true); timer = setTimeout(poll, 2500); }
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [reload, mode]);

  useEffect(() => {
    if (!selectedId) { setLoading(false); setResultError(null); return; }
    const controller = new AbortController();
    setLoading(true);
    setResultError(null);
    async function poll() {
      try {
        const endpoint = listMode ? 'videos' : 'comments';
        const data = await api<CommentPage | VideoPage>(`/tasks/${encodeURIComponent(selectedId!)}/${endpoint}?${resultQuery}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (listMode) setVideoResultState({ key: resultKey, data: data as VideoPage });
        else setResultState({ key: resultKey, data: data as CommentPage });
        setResultError(null);
        const maxPage = Math.max(1, Math.ceil(data.total / data.page_size));
        if (page > maxPage) setPage(maxPage);
      } catch (error) { if (!controller.signal.aborted) setResultError(errorMessage(error)); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    }
    void poll();
    return () => { controller.abort(); };
  }, [selectedId, page, resultKey, resultQuery, listMode, task?.updated_at, reload]);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 5500);
    return () => clearTimeout(timer);
  }, [notice]);

  const rememberTask = useCallback((nextTask: Task) => {
    setTasks(current => [nextTask, ...current.filter(item => item.id !== nextTask.id)].sort((a, b) => b.created_at - a.created_at));
    setSelectedId(nextTask.id);
    setMode(nextTask.kind ?? 'comments');
    setPage(1);
    setReload(value => value + 1);
  }, []);

  async function createTask(event: FormEvent) {
    event.preventDefault();
    if (!(listMode ? listDraft.account.trim() : source.trim()) || pending) return;
    const maxVideos = Number(listDraft.maxVideos || 0);
    if (listMode && (!/^\d*$/.test(listDraft.maxVideos) || !Number.isSafeInteger(maxVideos) || maxVideos < 0 || maxVideos > 100000)) { setActionError('采集数量请输入 0–100000 的整数；留空或 0 表示不限。'); return; }
    if (listMode && listDraft.start && listDraft.end && listDraft.start > listDraft.end) { setActionError('开始时间不能晚于结束时间。'); return; }
    setPending('create'); setActionError(null);
    try {
      const nextTask = await api<Task>(listKind ? `/${listKind}/tasks` : '/tasks', { method: 'POST', body: JSON.stringify(listMode ? { account: listDraft.account.trim(), max_videos: maxVideos, start: listDraft.start, end: listDraft.end } : { url: source.trim() }) });
      rememberTask(nextTask); if (!listMode) setSource(''); setNotice('任务已创建，可开始采集。');
    } catch (error) { setActionError(errorMessage(error)); }
    finally { setPending(null); }
  }

  async function taskAction(action: 'start' | 'stop' | 'refresh' | 'verify') {
    if (!task || pending) return;
    setPending(action); setActionError(null);
    try {
      const nextTask = await api<Task>(`/tasks/${encodeURIComponent(task.id)}/${action}`, { method: 'POST' });
      if (action === 'refresh') rememberTask(nextTask);
      else {
        setTasks(current => current.map(item => item.id === nextTask.id ? nextTask : item));
        setReload(value => value + 1);
      }
      if (action === 'refresh') setNotice('已创建新的采集批次，历史结果仍保留。点击「开始采集」启动。');
      if (action === 'stop') setNotice('已发送停止请求，当前页保存后暂停。');
    } catch (error) { setActionError(errorMessage(error)); }
    finally { setPending(null); }
  }

  async function changeVerification(enabled: boolean) {
    if (!task || pending || active) return;
    setPending('options'); setActionError(null);
    try {
      const nextTask = await api<Task>(`/tasks/${encodeURIComponent(task.id)}`, { method: 'PATCH', body: JSON.stringify({ verify_authors: enabled }) });
      setTasks(current => current.map(item => item.id === nextTask.id ? nextTask : item));
    } catch (error) { setActionError(errorMessage(error)); }
    finally { setPending(null); }
  }

  async function login(action: 'login' | 'close' = 'login') {
    if (pending) return;
    setPending(action); setActionError(null);
    try {
      setSession(await api<Session>(`/session/${action}`, { method: 'POST' }));
      setReload(value => value + 1);
      if (action === 'login') setNotice('请在打开的专用浏览器中扫码登录；验证码也在该窗口处理。');
    } catch (error) { setActionError(errorMessage(error)); }
    finally { setPending(null); }
  }

  async function deleteTask(id: string) {
    setPending('delete'); setActionError(null);
    try {
      await api<{ ok: boolean }>(`/tasks/${encodeURIComponent(id)}`, { method: 'DELETE' });
      setTasks(current => current.filter(item => item.id !== id));
      if (selectedId === id) { setSelectedId(visibleTasks.find(item => item.id !== id)?.id ?? null); setPage(1); }
      setDeleteId(null); setReload(value => value + 1); setNotice('本机任务和对应结果已删除。');
    } catch (error) { setActionError(errorMessage(error)); }
    finally { setPending(null); }
  }

  function applyFilters(event: FormEvent) {
    event.preventDefault();
    if (draft.start && draft.end && draft.start > draft.end) { setFilterError('开始时间不能晚于结束时间。'); return; }
    setFilterError(null);
    setFilters({ ...draft, account: draft.account.trim(), keyword: draft.keyword.trim() }); setPage(1);
  }

  function resetFilters() { setDraft({ ...DEFAULT_FILTERS }); setFilters({ ...DEFAULT_FILTERS }); setPage(1); setFilterError(null); }

  async function exportResults() {
    if (!task || exporting) return;
    setExporting(true); setActionError(null);
    try {
      const response = await fetch(`/api/tasks/${encodeURIComponent(task.id)}/export.txt${listMode ? '' : `?${filterQuery(filters)}`}`);
      if (!response.ok) throw new Error(await responseError(response));
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url; link.download = `${listMode ? `抖音${listName}列表` : '抖音评论'}_${(listMode ? task.target_account : task.video_id) || task.id}_${new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date())}.txt`;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      setNotice(listMode ? 'TXT 已导出，包含此批次全部已获取的视频。' : 'TXT 已导出，包含当前条件下的全部匹配评论。');
    } catch (error) { setActionError(errorMessage(error)); }
    finally { setExporting(false); }
  }

  function selectTask(id: string) { const selected = tasks.find(item => item.id === id); if (selected) setMode(selected.kind ?? 'comments'); setSelectedId(id); setPage(1); setDeleteId(null); setActionError(null); }
  function switchMode(nextMode: TaskKind) {
    setMode(nextMode); setSelectedId(tasks.find(item => (item.kind ?? 'comments') === nextMode)?.id ?? null);
    setPage(1); setDeleteId(null); setActionError(null); setResultError(null);
  }
  function focusSource() { const input = listMode ? accountRef.current : sourceRef.current; input?.focus(); input?.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
  function updateFilter<K extends keyof Filters>(key: K, value: Filters[K]) { setDraft(current => ({ ...current, [key]: value })); }
  function updateListDraft(key: keyof VideoListDraft, value: string) {
    if (!listKind) return;
    setListDrafts(current => ({ ...current, [listKind]: { ...current[listKind], [key]: value } }));
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <nav className="workspace-modes" aria-label="工作台模式">{WORKSPACE_MODES.map(item => <button key={item.kind} className={`workspace-nav ${mode === item.kind ? 'active' : ''}`} onClick={() => switchMode(item.kind)} aria-current={mode === item.kind ? 'page' : undefined}><Icon name={item.icon} size={18} /><span>{item.label}</span>{mode === item.kind ? <span className="nav-active-dot" /> : null}</button>)}</nav>
      <div className="history-heading"><span>历史任务 <b>{visibleTasks.length}</b></span><button className="icon-button" onClick={focusSource} aria-label={listMode ? `添加${listName}列表任务` : '添加视频任务'}><Icon name="plus" size={17} /></button></div>
      <nav className="task-history" aria-label="历史采集任务">
        {!visibleTasks.length ? <div className="history-empty"><Icon name="clock" size={23} /><p>{loaded ? '还没有采集任务' : '正在读取任务…'}</p><span>创建后，任务会保存在这里</span></div> : visibleTasks.map(item => <div className={`history-item ${selectedId === item.id ? 'selected' : ''}`} key={item.id}>
          <button className="history-select" onClick={() => selectTask(item.id)} aria-current={selectedId === item.id ? 'true' : undefined}><div className="history-title">{taskTitle(item)}</div><div className="history-meta"><span className={`history-dot dot-${item.status}`} /><span>{STATUS_LABELS[item.status]}</span><span className="history-count">{(item.kind === 'likes' || item.kind === 'recommendations' ? item.video_count ?? 0 : item.main_count + item.reply_count).toLocaleString()} 条</span></div><time className="history-date">{dateTime(item.created_at)}</time></button>
          <button className="icon-button history-delete" aria-label={`删除任务 ${taskTitle(item)}`} title="删除任务" disabled={Boolean(pending) || ACTIVE_STATUSES.includes(item.status)} onClick={() => setDeleteId(deleteId === item.id ? null : item.id)}><Icon name="trash" size={14} /></button>
          {deleteId === item.id ? <div className="delete-confirm"><p>删除此任务及本机结果？</p><div><button onClick={() => setDeleteId(null)} disabled={Boolean(pending)}>取消</button><button className="danger-text" onClick={() => void deleteTask(item.id)} disabled={Boolean(pending)}>{pending === 'delete' ? '删除中' : '确认删除'}</button></div></div> : null}
        </div>)}
      </nav>
      <div className="session-card"><div className="session-heading"><span className={`session-indicator ${session.status}`} /><strong>{session.control_status === 'verification' ? '等待人工验证' : session.status === 'ready' ? '登录已就绪' : session.status === 'waiting' ? '等待扫码登录' : session.status === 'error' ? '登录需要处理' : '连接抖音账号'}</strong></div><p>{session.control_status === 'verification' ? '请在专用浏览器中完成验证' : session.status === 'ready' ? '专用浏览器已保存登录状态' : session.status === 'waiting' ? '请在打开的浏览器中完成登录' : '使用专用浏览器扫码，登录态留在本机'}</p>{session.control_status && session.control_status !== 'ready' ? <p className="session-error" role="status">{session.control_status === 'protected' ? '保护期内，已降低请求频率' : session.control_status === 'verification' ? '等待人工验证' : session.control_status === 'paused' ? '采集已暂停，冷却后请继续' : session.control_status === 'cooldown' ? '采集冷却中' : session.control_reason}{session.cooldown_until && session.cooldown_until * 1000 > Date.now() ? ` · 至 ${new Date(session.cooldown_until * 1000).toLocaleTimeString('zh-CN', { hour12: false })}` : ''}</p> : null}{session.error ? <p className="session-error">{session.error}</p> : null}<button className="button session-button" onClick={() => void login(session.status === 'waiting' ? 'close' : 'login')} disabled={Boolean(pending) || (anyActive && session.status === 'ready')}><Icon name={session.status === 'waiting' ? 'close' : 'login'} size={17} />{pending === 'login' ? '正在打开浏览器' : session.status === 'waiting' ? '取消登录' : session.status === 'ready' ? '重新扫码登录' : '扫码登录'}</button></div>
    </aside>

    <main id="main" className="main-content">
      <h1 className="sr-only">{listMode ? `公开${listName}列表` : '视频评论检索'}</h1>

      {serviceError ? <div className="notice error" role="alert"><Icon name="info" size={18} /><span>{serviceError}</span><button className="text-button" onClick={() => setReload(value => value + 1)}>重试</button></div> : null}
      {actionError ? <div className="notice error" role="alert"><Icon name="info" size={18} /><span>{actionError}</span><button className="icon-button" aria-label="关闭错误提示" onClick={() => setActionError(null)}><Icon name="close" size={15} /></button></div> : null}
      {notice ? <div className="notice success" role="status"><Icon name="check" size={18} /><span>{notice}</span><button className="icon-button" aria-label="关闭操作提示" onClick={() => setNotice(null)}><Icon name="close" size={15} /></button></div> : null}

      <section className="source-card" aria-labelledby="source-title">
        <div className="section-heading"><h2 id="source-title"><span className="step-label">01</span>{listMode ? '添加抖音号' : '添加视频'}</h2><span className="section-caption">{listMode ? '使用公开抖音号，非昵称' : '完整链接、短链接、分享文本均可'}</span></div>
        <form onSubmit={event => void createTask(event)}>
          <div className="source-form"><div className="source-input"><Icon name={listMode ? 'user' : 'link'} size={20} />{listMode ? <input ref={accountRef} aria-label="目标账号的公开抖音号" value={listDraft.account} onChange={event => updateListDraft('account', event.target.value)} placeholder="输入准确的抖音号，支持 @ 前缀" autoComplete="off" maxLength={129} required /> : <textarea ref={sourceRef} aria-label="抖音视频链接或分享文本" rows={1} value={source} onChange={event => setSource(event.target.value)} placeholder="粘贴抖音视频链接，或直接粘贴分享文本…" required />}</div><button className="button primary" type="submit" disabled={!(listMode ? listDraft.account.trim() : source.trim()) || Boolean(pending)}>{pending === 'create' ? '正在创建…' : '创建任务'}<Icon name="arrow" size={17} /></button></div>
          {listMode ? <><div className="list-options">
            <label className="field"><span>采集数量<span className="field-hint">最近{listAction}在前</span></span><input type="number" inputMode="numeric" min="0" max="100000" step="1" value={listDraft.maxVideos} onChange={event => updateListDraft('maxVideos', event.target.value)} placeholder="不限" aria-describedby="list-options-help" /></label>
            <label className="field"><span>{listAction}开始时间<span className="field-hint">北京时间</span></span><input type="datetime-local" step="1" value={listDraft.start} onChange={event => updateListDraft('start', event.target.value)} /></label>
            <label className="field"><span>{listAction}结束时间<span className="field-hint">包含此时刻</span></span><input type="datetime-local" step="1" value={listDraft.end} onChange={event => updateListDraft('end', event.target.value)} /></label>
          </div><p className="source-help" id="list-options-help">留空或数量 0 表示不限；时间筛选需平台提供可核验的{listAction}时间。</p></> : null}
        </form>
      </section>

      {task && listKind ? <VideoListTask kind={listKind} task={task} active={active} anyActive={anyActive} pending={pending} statusLabel={STATUS_LABELS[task.status]} onAction={action => void taskAction(action)} onVerificationChange={enabled => void changeVerification(enabled)} /> : null}

      {task && !listMode ? <section className="task-card" aria-label="当前采集任务"><div className="task-summary"><div className="video-thumbnail"><Icon name="play" size={24} /></div><div className="video-info"><div className="video-heading"><h2 title={taskTitle(task)}>{taskTitle(task)}</h2><StatusBadge status={task.status} /></div><div className="video-detail"><span>{task.video_id ? `视频 ID ${task.video_id}` : '短链接将在开始采集后解析，可先打开原视频核对'}</span>{videoUrl ? <a href={videoUrl} target="_blank" rel="noopener noreferrer">打开原视频<Icon name="external" size={12} /></a> : null}</div></div><div className="task-controls">{active ? <button className="button secondary small" onClick={() => void taskAction('stop')} disabled={Boolean(pending)}><Icon name="stop" size={15} />{pending === 'stop' ? '正在停止' : '停止采集'}</button> : <button className="button primary small" onClick={() => void taskAction('start')} disabled={Boolean(pending) || (anyActive && !active) || task.collection_complete}><Icon name="play" size={15} />{pending === 'start' ? '正在启动' : task.status === 'created' ? '开始采集' : task.collection_complete ? '采集已完成' : '继续采集'}</button>}<button className="icon-button refresh-task" title="新建批次重新采集，保留当前结果" aria-label="重新采集" onClick={() => void taskAction('refresh')} disabled={Boolean(pending) || active}><Icon name="refresh" size={17} /></button></div></div>
        <div className="stats-grid"><div><span>主评论</span><strong>{task.main_count.toLocaleString()}<small>条</small></strong></div><div><span>楼中楼回复</span><strong>{task.reply_count.toLocaleString()}<small>条</small></strong></div><div><span>当前匹配</span><strong className="green-number">{results?.total.toLocaleString() ?? '—'}<small>条</small></strong></div><div><span>待核实作者</span><strong>{task.unknown_authors.toLocaleString()}<small>位</small></strong></div></div>
        <div className="task-progress"><span>{active ? <span className="pulse-dot" /> : <Icon name={task.collection_complete ? 'check' : 'info'} size={14} />}{task.status === 'running' ? '正在逐页采集主评论与回复，结果实时更新' : task.status === 'verifying' ? '正在核实作者抖音号，已有结果可继续检索' : task.status === 'waiting_login' ? '等待浏览器登录或验证，完成后继续处理' : task.collection_complete ? `当前可获取评论已采集完成${task.verification_complete ? '，作者信息已核实' : '，部分作者信息待核实'}` : task.status === 'created' ? '准备就绪 · 采集前请确认已登录抖音' : '进度已保存在本机，可继续采集'} </span></div>
        <AuthorVerification task={task} active={active} anyActive={anyActive} pending={pending} onChange={enabled => void changeVerification(enabled)} onVerify={() => void taskAction('verify')} />
        {task.error ? <div className="task-error"><Icon name="info" size={15} /><span>{task.error}</span></div> : null}
      </section> : null}

      {!listMode ? <section className="filter-card" aria-labelledby="filter-title"><div className="section-heading"><h2 id="filter-title"><span className="step-label">02</span>筛选评论</h2><span className="section-caption">条件可组合 · 繁简自动匹配</span></div><form onSubmit={applyFilters}><div className="filter-main-row"><label className="field account-field"><span>抖音号<span className="field-hint">精确匹配</span></span><input value={draft.account} onChange={event => updateFilter('account', event.target.value)} placeholder="输入作者的公开抖音号" autoComplete="off" /></label><label className="field keyword-field"><span>评论关键词<span className="field-hint">繁简字形兼容</span></span><div className="input-with-icon"><Icon name="search" size={17} /><input value={draft.keyword} onChange={event => updateFilter('keyword', event.target.value)} placeholder="输入想查找的评论内容" /></div></label><fieldset className="mode-field"><legend>匹配方式</legend><div className="segmented"><label className={draft.mode === 'contains' ? 'checked' : ''}><input type="radio" name="mode" value="contains" checked={draft.mode === 'contains'} onChange={() => updateFilter('mode', 'contains')} />包含匹配</label><label className={draft.mode === 'fuzzy' ? 'checked' : ''}><input type="radio" name="mode" value="fuzzy" checked={draft.mode === 'fuzzy'} onChange={() => updateFilter('mode', 'fuzzy')} />近似匹配</label></div></fieldset></div>
        {draft.mode === 'fuzzy' ? <div className="fuzzy-settings"><label htmlFor="threshold">相似度阈值 <strong>{draft.threshold}</strong></label><input id="threshold" type="range" min="70" max="100" value={draft.threshold} onChange={event => updateFilter('threshold', Number(event.target.value))} /><span>数值越高越严格；1–3 字关键词仅做包含匹配</span></div> : null}
        <div className="filter-second-row"><div className="date-fields"><label className="field"><span>开始时间<span className="field-hint">北京时间</span></span><input type="datetime-local" step="1" value={draft.start} onChange={event => updateFilter('start', event.target.value)} /></label><span className="date-separator">—</span><label className="field"><span>结束时间<span className="field-hint">包含此时刻</span></span><input type="datetime-local" step="1" value={draft.end} onChange={event => updateFilter('end', event.target.value)} /></label></div><div className="filter-buttons"><button type="button" className="text-button reset-button" onClick={resetFilters}>重置</button><button type="submit" className="button primary" disabled={!task || loading}><Icon name="search" size={16} />搜索评论</button></div></div>
        <div className="filter-footer"><span>留空表示不限；多个条件同时满足才会显示。</span>{filterError ? <span className="field-error" role="alert">{filterError}</span> : dirtyFilters ? <span className="dirty-hint">条件已修改，点击搜索后生效</span> : <span>显示原始评论内容</span>}</div></form></section> : null}

      {listKind ? <VideoListResults kind={listKind} task={task} data={videos} loading={loading} error={resultError} page={page} onPage={setPage} onExport={() => void exportResults()} exporting={exporting} onRetry={() => setReload(value => value + 1)} /> : <Results task={task} data={results} filters={filters} loading={loading} error={resultError} page={page} onPage={setPage} onExport={() => void exportResults()} exporting={exporting} onRetry={() => setReload(value => value + 1)} />}
    </main>
  </div>;
}
