import { Icon } from './icons';
import { dateTime } from './Results';
import { listSortLabel, VIDEO_LIST_LABELS } from './videoLists';
import type { VideoPage, Task, VideoListKind } from './types';

export function VideoListResults({ kind, task, data, loading, error, page, onPage, onExport, exporting, onRetry }: {
  kind: VideoListKind;
  task: Task | undefined;
  data: VideoPage | null;
  loading: boolean;
  error: string | null;
  page: number;
  onPage: (page: number) => void;
  onExport: () => void;
  exporting: boolean;
  onRetry: () => void;
}) {
  const listName = VIDEO_LIST_LABELS[kind];
  const resultsTitleId = `${kind}-results-title`;
  const pageSize = data?.page_size ?? 50;
  const pages = Math.max(1, Math.ceil((data?.total ?? 0) / pageSize));
  const isPrivate = task?.visibility === 'private' || task?.status === 'private';
  const isComplete = Boolean(data?.collection_complete);
  const failed = task?.status === 'failed' || task?.status === 'partial';
  const emptyTitle = !task ? '暂无视频' : isPrivate ? `该账号未公开${listName}列表` : loading && !data ? '正在读取本地视频…' : task.status === 'created' ? '等待开始采集' : failed ? `${listName}列表尚未获取成功` : isComplete ? `当前未返回可访问的${listName}视频` : `正在等待${listName}列表`;
  const emptyDescription = !task ? null : isPrivate ? `平台已明确提示${listName}列表未公开。` : task.status === 'created' ? '点击上方「开始采集」。' : failed ? `请查看上方任务提示。请求失败不能判断该账号是否公开了${listName}列表。` : isComplete ? '这里只展示平台实际返回的可访问视频。' : '采集完成前不能判断列表是否为空；如需登录，请在专用浏览器中处理。';

  return <section className="results" aria-labelledby={resultsTitleId}>
    <div className="section-heading result-heading">
      <div className="result-title"><h2 id={resultsTitleId}>{listName}的视频</h2><span className="count-badge">{data?.total.toLocaleString() ?? '—'}</span>{loading && data ? <span className="subtle-inline" role="status">更新中</span> : null}</div>
      <div className="result-actions"><span className="sort-label"><Icon name="clock" size={14} />{listSortLabel(kind, task?.list_order)}</span><button className="button secondary small" onClick={onExport} disabled={!data?.total || exporting || loading} title="导出当前批次全部已获取视频"><Icon name="download" size={16} />{exporting ? '正在导出' : '导出 TXT'}</button></div>
    </div>
    {task && data && !isComplete && !isPrivate ? <div className="notice compact"><Icon name="info" size={16} /><span>采集尚未完成，当前结果与导出仅包含已获取的视频。</span></div> : null}
    {isPrivate && data?.items.length ? <div className="notice compact"><Icon name="shield" size={16} /><span>该账号未公开{listName}列表；下方仅保留此批次先前已获取的视频。</span></div> : null}
    {error ? <div className="empty-state error-state" role="alert"><div className="empty-icon"><Icon name="info" size={28} /></div><h3>视频结果暂时无法读取</h3><p>{error}</p><button className="button secondary small" onClick={onRetry}>重新读取</button></div> : !data?.items.length ? <div className="empty-state"><div className={`empty-icon ${loading ? 'is-loading' : ''}`}><Icon name={isPrivate ? 'shield' : kind === 'recommendations' ? 'recommend' : 'heart'} size={32} /></div><h3>{emptyTitle}</h3>{emptyDescription ? <p>{emptyDescription}</p> : null}</div> : <>
      <div className="liked-video-list" aria-live="polite" aria-busy={loading}>
        {data.items.map((video, index) => <article className="liked-video-card" key={video.video_id}>
          <span className="liked-video-mark"><Icon name="play" size={19} /></span>
          <div className="liked-video-main"><div className="liked-video-heading"><h3>{video.title || '未提供'}</h3><span className="record-index">{String((page - 1) * pageSize + index + 1).padStart(2, '0')}</span></div>
            <dl className="liked-video-fields"><div><dt>作者名称</dt><dd>{video.author_name || '未提供'}</dd></div><div><dt>作者抖音号</dt><dd>{video.author_douyin_id || '未提供'}</dd></div><div><dt>发布时间</dt><dd>{video.time_text || dateTime(video.created_at)}</dd></div></dl>
            <div className="liked-video-link"><span>视频链接</span>{/^\d+$/.test(video.video_id) ? <a href={`https://www.douyin.com/video/${video.video_id}`} target="_blank" rel="noopener noreferrer">{`https://www.douyin.com/video/${video.video_id}`}<Icon name="external" size={12} /></a> : <span>未提供</span>}</div>
          </div>
        </article>)}
      </div>
      <div className="pagination"><span>第 {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, data.total)} 条，共 {data.total.toLocaleString()} 条</span><div><button className="icon-button page-button" aria-label="上一页" disabled={page <= 1 || loading} onClick={() => onPage(page - 1)}><Icon name="chevron" size={16} className="rotate-180" /></button><span>{page} / {pages}</span><button className="icon-button page-button" aria-label="下一页" disabled={page >= pages || loading} onClick={() => onPage(page + 1)}><Icon name="chevron" size={16} /></button></div></div>
    </>}
    {task ? <div className="results-footnote"><Icon name="clock" size={13} /><span>展示及导出的时间为视频发布时间 · 缺失信息显示「未提供」</span></div> : null}
  </section>;
}
