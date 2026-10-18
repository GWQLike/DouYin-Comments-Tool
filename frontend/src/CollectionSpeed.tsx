import { HISTORY_MS, REFRESH_INTERVALS, REFRESH_LABELS } from './monitor';
import type { RefreshRate, SpeedHistory } from './monitor';

export function CollectionSpeed({ history, refreshRate, onRefreshRate, stale }: {
  history: SpeedHistory | null;
  refreshRate: RefreshRate;
  onRefreshRate: (value: RefreshRate) => void;
  stale: boolean;
}) {
  const points = history?.points ?? [];
  const last = points.at(-1);
  const current = stale ? null : last?.rate ?? null;
  const peak = Math.max(0, ...points.map(point => point.rate ?? 0));
  const ceiling = Math.max(1, Math.ceil(peak / 5) * 5);
  const end = last?.time ?? 0;
  const width = 800, height = 120;
  const segments: string[][] = [];
  for (const point of points) {
    if (point.rate == null) { segments.push([]); continue; }
    if (!segments.length) segments.push([]);
    const x = width * (1 - (end - point.time) / HISTORY_MS);
    const y = height * (1 - point.rate / ceiling);
    segments[segments.length - 1].push(`${x.toFixed(2)},${y.toFixed(2)}`);
  }
  return <section className="speed-card" aria-labelledby="speed-title">
    <div className="speed-heading">
      <div className="speed-reading"><h2 id="speed-title">采集速度</h2><strong>{current == null ? '—' : current.toLocaleString('zh-CN', { maximumFractionDigits: 1 })}<small>条/秒</small></strong></div>
      <label className="refresh-rate"><span>实时刷新</span><select aria-label="实时刷新速度" value={refreshRate} onChange={event => onRefreshRate(event.target.value as RefreshRate)}>
        {(Object.keys(REFRESH_INTERVALS) as RefreshRate[]).map(value => <option key={value} value={value}>{REFRESH_LABELS[value]}</option>)}
      </select></label>
    </div>
    <div className="speed-scale"><span>{stale ? '连接中断，等待更新' : !history?.previous ? '等待采样' : '新增并保存的结果'}</span><span>{ceiling.toLocaleString()} 条/秒</span></div>
    <svg className="speed-chart" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label={`最近 60 秒采集速度，当前${current == null ? '暂无采样' : `${current.toFixed(1)} 条每秒`}`}>
      {[0, 1, 2, 3, 4].map(row => <line key={`h${row}`} x1="0" x2={width} y1={row * height / 4} y2={row * height / 4} className="speed-grid" />)}
      {Array.from({ length: 13 }, (_, column) => <line key={`v${column}`} x1={column * width / 12} x2={column * width / 12} y1="0" y2={height} className="speed-grid" />)}
      {segments.filter(segment => segment.length > 1).map((segment, index) => <g key={index}>
        <polygon points={`${segment[0].split(',')[0]},${height} ${segment.join(' ')} ${segment.at(-1)!.split(',')[0]},${height}`} className="speed-area" />
        <polyline points={segment.join(' ')} className="speed-line" vectorEffect="non-scaling-stroke" />
      </g>)}
    </svg>
    <div className="speed-axis"><span>60 秒前</span><span>现在</span></div>
  </section>;
}
