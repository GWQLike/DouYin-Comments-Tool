import { Icon } from './icons';
import type { CommentPage, Filters, Task } from './types';

export const dateTime = (value: number | null | undefined): string => {
  if (value == null || !Number.isFinite(value)) return '未提供';
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(value * 1000)).replaceAll('/', '-');
};

export function Results({ task, data, filters, loading, error, page, onPage, onExport, exporting, onRetry }: {
  task: Task | undefined;
  data: CommentPage | null;
  filters: Filters;
  loading: boolean;
  error: string | null;
  page: number;
  onPage: (value: number) => void;
  onExport: () => void;
  exporting: boolean;
  onRetry: () => void;
}) {
  const pages = Math.max(1, Math.ceil((data?.total ?? 0) / 50));
  const filtered = Boolean(filters.account || filters.keyword || filters.start || filters.end);
  const allComplete = Boolean(data?.collection_complete && (!filters.account || data.verification_complete));
  const emptyTitle = !task ? '从一条视频开始' : !data && loading ? '正在读取本地评论…' : filtered ? allComplete ? '当前可获取评论中未找到匹配' : '当前暂无匹配，尚未完成核实' : task.status === 'created' ? '任务已准备，开始采集评论吧' : '暂时还没有采集到评论';
  const emptyDescription = !task ? '粘贴抖音视频链接，采集评论后按账号、关键词和时间检索。' : filtered ? '可调整关键词或时间范围；未完成的采集与作者核实会影响搜索覆盖范围。' : task.status === 'created' ? '点击上方「开始采集」，主评论和楼中楼回复都会保存到本机。' : task.collection_complete ? '该视频当前没有返回可访问的评论。' : '采集过程中结果会自动出现；如需登录，请在打开的浏览器中完成扫码。';

  return <section className="results" aria-labelledby="results-title">
    <div className="section-heading result-heading">
      <div className="result-title"><h2 id="results-title">评论结果</h2><span className="count-badge">{data?.total.toLocaleString() ?? '—'}</span>{loading && data ? <span className="subtle-inline" role="status">更新中</span> : null}</div>
      <div className="result-actions"><span className="sort-label"><Icon name="clock" size={14} />时间从新到旧</span><button className="button secondary small" onClick={onExport} disabled={!data?.total || exporting || loading} title="导出当前筛选条件下全部匹配评论"><Icon name="download" size={16} />{exporting ? '正在导出' : '导出 TXT'}</button></div>
    </div>
    {task && data && !data.collection_complete ? <div className="notice compact"><Icon name="info" size={16} /><span>采集尚未完成，当前结果和导出仅包含已获取的评论。</span></div> : null}
    {task && data && (data.unknown_authors > 0 || data.missing_times > 0) ? <div className="coverage-note">{data.unknown_authors > 0 ? <span>{data.unknown_authors.toLocaleString()} 位作者的抖音号尚未核实{filters.account ? '，可能影响账号搜索' : ''}。</span> : null}{data.missing_times > 0 ? <span>{data.missing_times.toLocaleString()} 条评论没有发布时间{filters.start || filters.end ? '，已从时间筛选中排除' : ''}。</span> : null}</div> : null}
    {error ? <div className="empty-state error-state" role="alert"><div className="empty-icon"><Icon name="info" size={28} /></div><h3>评论暂时无法读取</h3><p>{error}</p><button className="button secondary small" onClick={onRetry}>重新读取</button></div> : !data?.items.length ? <div className="empty-state"><div className={`empty-icon ${loading ? 'is-loading' : ''}`}><Icon name={!task ? 'chat' : filtered ? 'search' : 'chat'} size={32} /></div><h3>{emptyTitle}</h3><p>{emptyDescription}</p>{!task ? <div className="empty-steps"><span><b>01</b> 添加视频</span><Icon name="arrow" size={16} /><span><b>02</b> 采集评论</span><Icon name="arrow" size={16} /><span><b>03</b> 检索与导出</span></div> : null}</div> : <>
      <div className="comment-list" aria-live="polite" aria-busy={loading}>
        {data.items.map((comment, index) => <article className="comment-card" key={comment.comment_id}>
          <span className={`avatar avatar-${(comment.nickname?.codePointAt(0) ?? 0) % 4}`}>{Array.from(comment.nickname || '评')[0]}</span>
          <div className="comment-main"><div className="comment-author"><strong>{comment.nickname || '未提供'}</strong>{comment.parent_id ? <span className="tiny-tag">回复</span> : null}{comment.match_type === 'fuzzy' ? <span className="tiny-tag fuzzy-tag">近似匹配 {comment.score != null ? `${Math.round(comment.score)} 分` : ''}</span> : null}<span className="record-index">{String((page - 1) * 50 + index + 1).padStart(2, '0')}</span></div>
            <div className="douyin-id">抖音号：<span>{comment.douyin_id || '未提供'}</span></div>
            <p className="comment-text">{comment.text || '（评论内容为空）'}</p>
            <div className="comment-meta"><span><Icon name="clock" size={13} />{comment.time_text || dateTime(comment.created_at)}</span><span className="meta-divider"/><span>IP 属地：{comment.ip_label || '未提供'}</span></div>
          </div>
        </article>)}
      </div>
      <div className="pagination"><span>第 {(page - 1) * 50 + 1}–{Math.min(page * 50, data.total)} 条，共 {data.total.toLocaleString()} 条</span><div><button className="icon-button page-button" aria-label="上一页" disabled={page <= 1 || loading} onClick={() => onPage(page - 1)}><Icon name="chevron" size={16} className="rotate-180" /></button><span>{page} / {pages}</span><button className="icon-button page-button" aria-label="下一页" disabled={page >= pages || loading} onClick={() => onPage(page + 1)}><Icon name="chevron" size={16} /></button></div></div>
    </>}
    <div className="results-footnote"><Icon name="shield" size={13} /><span>数据仅保存在本机 · TXT 包含用户名、抖音号、评论内容、时间及 IP 属地</span></div>
  </section>;
}
