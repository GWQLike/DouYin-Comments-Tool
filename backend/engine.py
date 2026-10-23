"""MediaCrawler lifecycle bridge used by the worker (no legacy fallback)."""
import json
import time
from pathlib import Path

from backend.upstream import ENGINE_VERSION, module
from backend.preflight import check_task_conditions


class WorkbenchRuntime:
    def __init__(self, db, args, stop, waiting, *, transport_factory=None):
        if transport_factory is None:
            from backend.douyin_adapter import DouyinAdapter
            transport_factory = DouyinAdapter
        self.db, self.args, self.stop, self.waiting = db, args, stop, waiting
        self.transport_factory = transport_factory
        self.verify_only = args.verify_only
        self.extension = self.transport = None
        self.started = time.perf_counter()
        self.ready_at = None

    def open_transport(self):
        self.transport = self.transport_factory(Path(self.args.profile).resolve(), self.stop, self.waiting)
        self.transport.session_closed = lambda: self.db.session_set(
            'logged_out', '专用浏览器已关闭，请重新打开登录窗口。')
        self.transport.max_requests = getattr(self.args, 'max_requests', 0)
        seconds = getattr(self.args, 'max_seconds', 0)
        self.transport.deadline = time.monotonic() + seconds if seconds else None
        self.transport.stop_on_challenge = getattr(self.args, 'stop_on_challenge', False)
        return self.transport

    async def ready(self, engine, transport):
        transport.policy.check_control()
        if transport.policy.paused:
            await transport.wait_for_manual()
        self.ready_at = time.perf_counter()

    def create_extension(self, engine, transport):
        if not self.args.task:
            return None
        from backend.engine_tasks import CommentTask, LikesTask, RecommendationsTask
        cls = {'comments': CommentTask, 'likes': LikesTask, 'recommendations': RecommendationsTask}[self.db.get_task(self.args.task)['kind']]
        self.extension = cls(self.db, transport, self.args.task, stop=self.stop)
        self.extension.engine = engine
        return self.extension

    def report(self):
        now = time.perf_counter()
        report = dict(getattr(self.extension, 'metrics', {}))
        if self.transport and hasattr(self.transport, 'performance_summary'):
            report.update(self.transport.performance_summary())
        return {**report, 'engine': 'MediaCrawler.DouYinCrawler', 'engine_version': ENGINE_VERSION,
                'task_id': self.args.task, 'startup_seconds': round((self.ready_at or now) - self.started, 3),
                'collection_seconds': round(now - self.ready_at, 3) if self.ready_at else 0,
                'worker_elapsed_seconds': round(now - self.started, 3)}


async def run_engine(db, args, stop, waiting, *, runtime=None):
    if args.task:
        check_task_conditions(db.get_task(args.task), verify_only=args.verify_only)
    runtime = runtime or WorkbenchRuntime(db, args, stop, waiting)
    engine = module('media_platform.douyin.core').DouYinCrawler(runtime=runtime)
    try:
        await engine.start()
    finally:
        print('PERFORMANCE ' + json.dumps(runtime.report(), ensure_ascii=False, sort_keys=True), flush=True)
