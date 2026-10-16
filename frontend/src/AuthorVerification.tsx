import type { Task } from './types';

export function AuthorVerification({ task, active, anyActive, pending, onChange, onVerify }: {
  task: Task;
  active: boolean;
  anyActive: boolean;
  pending: string | null;
  onChange: (enabled: boolean) => void;
  onVerify: () => void;
}) {
  return <div className="task-progress author-options">
    <label><input type="checkbox" checked={task.verify_authors} disabled={active || Boolean(pending)} onChange={event => onChange(event.target.checked)} />采集后核实作者抖音号</label>
    {task.unknown_authors > 0 ? <button className="text-button" disabled={anyActive || Boolean(pending)} onClick={onVerify}>{pending === 'verify' ? '正在启动' : '核实作者'}</button> : null}
  </div>;
}
