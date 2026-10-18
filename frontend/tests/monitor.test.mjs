import assert from 'node:assert/strict';
import { test } from 'node:test';
import { sourceUrl } from './load-source.mjs';

const { REFRESH_INTERVALS, sampleSpeed, interruptSpeed, readRefreshRate, saveRefreshRate, startPolling } = await import(await sourceUrl('monitor'));
const { createWorkspaceReader } = await import(await sourceUrl('workspacePolling'));
const task = (fields = {}) => ({ id: 'a', kind: 'comments', status: 'running', main_count: 0, reply_count: 0, video_count: 0, updated_at: 1, unknown_authors: 0, ...fields });
const lastRate = history => history.points.at(-1).rate;
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };

test('三档刷新与本地偏好，禁用存储时仍可工作', () => {
  assert.deepEqual(REFRESH_INTERVALS, { low: 2000, normal: 1000, high: 500 });
  let value = 'invalid';
  globalThis.localStorage = { getItem: () => value, setItem: (_, next) => { value = next; } };
  assert.equal(readRefreshRate(), 'normal');
  saveRefreshRate('high');
  assert.equal(readRefreshRate(), 'high');
  globalThis.localStorage = { getItem: () => { throw Error(); }, setItem: () => { throw Error(); } };
  assert.equal(readRefreshRate(), 'normal');
  assert.doesNotThrow(() => saveRefreshRate('low'));
  delete globalThis.localStorage;
});

test('历史总数只作基线，主评论和回复按实际时间计算', () => {
  let history = sampleSpeed(null, task({ main_count: 1000 }), 0);
  assert.equal(lastRate(history), null);
  history = sampleSpeed(history, task({ main_count: 1003, reply_count: 2 }), 1250);
  assert.equal(lastRate(history), 4);
  assert.equal(lastRate(sampleSpeed(history, task({ main_count: 1003, reply_count: 2 }), 1750)), 0);
});

test('喜欢和推荐只计算新增视频，作者补齐不增加速度', () => {
  for (const kind of ['likes', 'recommendations']) {
    let history = sampleSpeed(null, task({ kind, video_count: 10 }), 0);
    history = sampleSpeed(history, task({ kind, video_count: 13 }), 500);
    assert.equal(lastRate(history), 6);
    history = sampleSpeed(history, task({ kind, video_count: 13, unknown_authors: 0, status: 'verifying' }), 1000);
    assert.equal(lastRate(history), 0);
  }
});

test('切换任务、结果回退、离开页面和断线不产生虚假尖峰', () => {
  const history = sampleSpeed(null, task({ main_count: 20 }), 0);
  assert.equal(lastRate(sampleSpeed(history, task({ id: 'b', main_count: 5000 }), 500)), null);
  assert.equal(lastRate(sampleSpeed(history, task({ main_count: 10 }), 500)), null);
  assert.equal(lastRate(sampleSpeed(history, task({ main_count: 5000 }), 6000)), null);
  assert.equal(lastRate(sampleSpeed(interruptSpeed(history), task({ main_count: 5000 }), 500)), null);
  assert.equal(lastRate(sampleSpeed(history, task({ main_count: 25 }), 0)), null);
});

test('完成前最后一页保留速度，停止后归零，恢复可继续计算', () => {
  let history = sampleSpeed(null, task(), 0);
  history = sampleSpeed(history, task({ status: 'completed', main_count: 8 }), 1000);
  assert.equal(lastRate(history), 8);
  history = sampleSpeed(history, task({ status: 'completed', main_count: 8 }), 2000);
  assert.equal(lastRate(history), 0);
  history = sampleSpeed(history, task({ main_count: 10 }), 3000);
  assert.equal(lastRate(history), 2);
});

test('曲线保留固定 60 秒，换档使用真实时间且内存有界', () => {
  let history = null;
  for (let time = 0; time <= 120000; time += 500) history = sampleSpeed(history, task({ main_count: time / 100 }), time);
  assert.equal(history.points.length, 121);
  assert.equal(history.points[0].time, 60000);
  assert.equal(lastRate(history), 10);
  history = sampleSpeed(history, task({ main_count: 1220 }), 122000);
  assert.equal(lastRate(history), 10);
});

function fixture(kind = 'comments') {
  const state = { task: task({ kind }), time: 0, calls: [], total: 0 };
  const read = async path => {
    state.calls.push(path);
    if (path === '/tasks') return [state.task];
    if (path === '/session') return { status: 'ready' };
    if (path === '/tasks/a') return state.task;
    return { items: [], total: state.total, page: 1, page_size: 50 };
  };
  return { state, read };
}

test('高频刷新只查当前任务，未变更结果不重复查询，历史每五秒更新', async () => {
  const { state, read } = fixture();
  const reader = createWorkspaceReader(read, () => state.time);
  const signal = new AbortController().signal;
  await reader('a', false, 'page=1', signal);
  state.calls = []; state.time = 500;
  assert.equal((await reader('a', false, 'page=1', signal)).data, undefined);
  assert.deepEqual(state.calls, ['/tasks/a']);
  state.time = 5000; state.calls = [];
  await reader('a', false, 'page=1', signal);
  assert.deepEqual(state.calls, ['/tasks', '/session']);
});

test('三类任务的数字与结果同批返回，筛选、翻页及作者核实触发更新', async () => {
  for (const kind of ['comments', 'likes', 'recommendations']) {
    const { state, read } = fixture(kind);
    const reader = createWorkspaceReader(read, () => state.time);
    const signal = new AbortController().signal;
    const list = kind !== 'comments';
    await reader('a', list, 'page=1', signal);
    state.time = 500; state.task = { ...state.task, main_count: 5, video_count: 5 }; state.total = 5;
    const next = await reader('a', list, 'page=1', signal);
    assert.equal(next.task.main_count, 5); assert.equal(next.data.total, 5); assert.equal(next.sampledAt, 500);
    assert.ok(state.calls.at(-1).includes(list ? '/videos?' : '/comments?'));
    state.task = { ...state.task, updated_at: 2 };
    assert.ok((await reader('a', list, 'page=1', signal)).data);
    assert.ok((await reader('a', list, 'page=2&keyword=谢谢', signal)).data);
  }
});

test('结果读取未完成不发布新数字，取消和失败不更新缓存', async () => {
  const { state, read } = fixture();
  let resolvePage;
  let fail = false;
  const reader = createWorkspaceReader(path => path.includes('/comments?') ? (fail ? Promise.reject(Error('offline')) : new Promise(resolve => { resolvePage = resolve; })) : read(path), () => state.time);
  const controller = new AbortController();
  let published = false;
  const pending = reader('a', false, 'page=1', controller.signal).then(value => { published = true; return value; });
  await flush(); assert.equal(published, false);
  controller.abort(); resolvePage({ total: 10 });
  await assert.rejects(pending, { name: 'AbortError' });
  assert.equal(published, false);
  fail = true;
  await assert.rejects(reader('a', false, 'page=1', new AbortController().signal), /offline/);
  assert.equal(state.calls.filter(path => path === '/tasks').length, 2);
});

test('低、常规、高按同一个时钟调度，耗时从间隔中扣除', async context => {
  context.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  for (const interval of Object.values(REFRESH_INTERVALS)) {
    let calls = 0;
    const stop = startPolling(async () => { calls++; }, interval, assert.fail, Date.now);
    await flush(); assert.equal(calls, 1);
    context.mock.timers.tick(interval - 1); await flush(); assert.equal(calls, 1);
    context.mock.timers.tick(1); await flush(); assert.equal(calls, 2);
    stop();
  }
  let calls = 0;
  const stop = startPolling(async () => { calls++; await new Promise(resolve => setTimeout(resolve, 200)); }, 500, assert.fail, Date.now);
  context.mock.timers.tick(200); await flush();
  context.mock.timers.tick(299); await flush(); assert.equal(calls, 1);
  context.mock.timers.tick(1); await flush(); assert.equal(calls, 2);
  stop();
});

test('慢查询不重叠，停止取消正在读取的请求且不再刷新', async context => {
  context.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  let calls = 0, signal, resolve;
  const stop = startPolling(async nextSignal => { calls++; signal = nextSignal; await new Promise(done => { resolve = done; }); }, 500, assert.fail, Date.now);
  context.mock.timers.tick(2000); await flush(); assert.equal(calls, 1);
  stop(); assert.equal(signal.aborted, true); resolve(); await flush();
  context.mock.timers.tick(5000); await flush(); assert.equal(calls, 1);
});

test('超时取消后下一轮可恢复，不会卡住或累积请求', async context => {
  context.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  let calls = 0, errors = 0;
  const stop = startPolling(async signal => { calls++; if (calls === 1) await new Promise((_, reject) => signal.addEventListener('abort', () => reject(Error('timeout')))); }, 500, () => { errors++; }, Date.now);
  context.mock.timers.tick(10000); await flush(); assert.equal(errors, 1);
  context.mock.timers.tick(1); await flush(); assert.equal(calls, 2);
  stop();
});
