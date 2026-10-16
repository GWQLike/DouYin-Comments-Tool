from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .db import Database
from .search import Filters, export_txt, export_likes_txt, select_comments, date_bound
from .urls import parse_input

ROOT = Path(__file__).resolve().parent.parent
ACTIVE = {'waiting_login', 'running', 'verifying'}


class WorkerManager:
    def __init__(self, db, data_dir):
        self.db, self.data_dir = db, Path(data_dir)
        self.process = None
        self.task_id = None
        self.lock = threading.RLock()
        self.log = None
        from .browser_session import DedicatedBrowser
        self.browser = DedicatedBrowser(self.data_dir)

    def check(self):
        with self.lock:
            if self.process is not None and self.process.poll() is not None:
                if self.task_id:
                    task = self.db.get_task(self.task_id)
                    if task['status'] in ACTIVE:
                        self.db.update_task(self.task_id, status='partial', error='采集进程已退出，已保留结果，可继续采集')
                elif self.db.session_get()['status'] == 'waiting':
                    self.db.session_set('error', '登录窗口已退出，请重新打开')
                if self.log:
                    self.log.close()
                    self.log = None
                self.process, self.task_id = None, None
            return self.process is not None

    def start(self, task_id=None, verify=False, *, validation=None):
        with self.lock:
            if self.check():
                raise HTTPException(409, '已有采集或登录窗口正在运行，请先停止当前操作')
            from .stable_policy import StableRequestPolicy
            control = StableRequestPolicy.public_state(self.data_dir / 'collection-control.json')
            import time
            if task_id and (control['control_status'] == 'error' or (control['control_status'] == 'paused' and control.get('cooldown_until', 0) > time.time())):
                raise HTTPException(409, control['control_reason'] or '采集正在冷却，请稍后手动继续')
            try:
                endpoint = self.browser.ensure()
            except Exception as exc:
                from .crawler import PageFailure
                raise HTTPException(503, str(exc) if isinstance(exc, PageFailure) else '专用 Chrome 未能启动') from None
            if task_id:
                self.db.update_task(task_id, stop_requested=False, error=None, status='verifying' if verify else 'running')
            else:
                self.db.session_set('waiting')
            command = [sys.executable, '-m', 'backend.crawler', '--db', self.db.path, '--profile', str(self.data_dir / 'chrome-profile')]
            if task_id:
                command += ['--task', task_id]
            if verify:
                command += ['--verify-only']
            if validation:
                command += ['--max-requests', str(validation['requests']), '--max-seconds', str(validation['seconds']), '--stop-on-challenge']
            env = os.environ.copy()
            env['PYTHONUTF8'] = '1'
            env['DOUYIN_CDP_URL'] = endpoint
            env.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / 'data' / 'playwright-browsers'))
            self.log = open(self.data_dir / 'worker.log', 'a', encoding='utf-8')
            try:
                self.process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=self.log, stderr=self.log,
                                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                self.task_id = task_id
            except Exception:
                self.log.close()
                self.log = None
                if task_id:
                    self.db.update_task(task_id, status='failed', error='采集进程无法启动，请检查项目环境')
                else:
                    self.db.session_set('error', '登录进程无法启动，请检查项目环境')
                raise HTTPException(500, '采集进程无法启动，请重新初始化环境') from None

    def stop(self):
        with self.lock:
            if self.check():
                if self.task_id:
                    self.db.update_task(self.task_id, stop_requested=True)
                else:
                    self.db.session_set('logged_out')
                try:
                    self.process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    if os.name == 'nt':
                        # Only the process tree we launched; avoid orphaned browser/driver processes.
                        subprocess.run(['taskkill', '/PID', str(self.process.pid), '/T', '/F'],
                                       capture_output=True, timeout=10,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=3)
                if self.task_id:
                    self.db.update_task(self.task_id, status='paused')
                self.check()

    def close_browser(self):
        self.browser.close()


class NewTask(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    verify_authors: bool = Field(default=False, strict=True)


class NewLikesTask(BaseModel):
    account: str = Field(min_length=1, max_length=129)
    max_videos: int = Field(default=0, ge=0, le=100000, strict=True)
    start: str = Field(default='', max_length=64)
    end: str = Field(default='', max_length=64)
    verify_authors: bool = Field(default=False, strict=True)


class TaskOptions(BaseModel):
    verify_authors: bool = Field(strict=True)


def query_filters(account: str = Query('', max_length=128), keyword: str = Query('', max_length=1000),
                  mode: str = 'contains', threshold: float = 85, start: str = '', end: str = ''):
    try:
        return Filters(account, keyword, mode, threshold, start, end)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


def create_app(data_dir=None, manager_factory=WorkerManager):
    data_dir = Path(data_dir or os.environ.get('DOUYIN_DATA_DIR', ROOT / 'data')).resolve()
    db = Database(data_dir / 'comments.sqlite3')
    manager = manager_factory(db, data_dir)

    @asynccontextmanager
    async def lifespan(app):
        db.recover_interrupted()
        yield
        manager.stop()
        if hasattr(manager, 'close_browser'):
            manager.close_browser()

    app = FastAPI(title='抖音评论检索工作台', lifespan=lifespan)
    app.state.db, app.state.manager = db, manager
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])

    @app.middleware('http')
    async def local_origin(request: Request, call_next):
        origin = request.headers.get('origin')
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin:
            parsed = urlparse(origin)
            if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.port not in (8765, 5173) or parsed.scheme != 'http':
                return JSONResponse({'detail':'仅允许从本机工具页面操作'}, status_code=403)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'detail':'输入参数无效，请检查链接、筛选条件或页码'}, status_code=400)

    def task_or_404(task_id):
        manager.check()
        try:
            return db.get_task(task_id)
        except KeyError:
            raise HTTPException(404, '任务不存在或已删除') from None

    @app.get('/api/health')
    def health():
        return {'status':'ok','app':'douyin-comments'}

    @app.get('/api/session')
    def session():
        manager.check()
        from .stable_policy import StableRequestPolicy
        return {**db.session_get(), **StableRequestPolicy.public_state(data_dir / 'collection-control.json')}

    @app.post('/api/session/login')
    def login():
        with manager.lock:
            manager.start()
            return db.session_get()

    @app.post('/api/session/close')
    def close():
        with manager.lock:
            manager.stop()
            if hasattr(manager, 'close_browser'):
                manager.close_browser()
            db.session_set('logged_out')
            return db.session_get()

    @app.get('/api/tasks')
    def tasks():
        manager.check()
        return db.list_tasks()

    @app.post('/api/tasks')
    def create(body: NewTask):
        try:
            url, video_id = parse_input(body.url)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return db.create_task(url, video_id, verify_authors=body.verify_authors)

    def create_account_list(body: NewLikesTask, kind: str):
        account = body.account.strip().removeprefix('@')
        if not account or len(account) > 128 or any(c.isspace() or c in '/\\:?#@' or ord(c) < 32 for c in account):
            raise HTTPException(400, '请输入有效的公开抖音号，不要输入用户名或主页链接')
        try:
            start_time, end_time = date_bound(body.start), date_bound(body.end, True)
            if start_time is not None and end_time is not None and start_time > end_time:
                raise ValueError('开始时间不能晚于结束时间')
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return db.create_task('', kind=kind, target_account=account, max_videos=body.max_videos,
                              start_time=start_time, end_time=end_time, list_order='activity', verify_authors=body.verify_authors)

    @app.post('/api/likes/tasks')
    def create_likes(body: NewLikesTask):
        return create_account_list(body, 'likes')

    @app.post('/api/recommendations/tasks')
    def create_recommendations(body: NewLikesTask):
        return create_account_list(body, 'recommendations')

    @app.post('/api/tasks/{task_id}/start')
    def start_task(task_id: str):
        with manager.lock:
            task_or_404(task_id)
            manager.start(task_id)
            return db.get_task(task_id)

    @app.patch('/api/tasks/{task_id}')
    def task_options(task_id: str, body: TaskOptions):
        with manager.lock:
            task = task_or_404(task_id)
            if task['status'] in ACTIVE or (manager.task_id == task_id and manager.check()):
                raise HTTPException(409, '请先停止任务，再修改作者核实选项')
            db.update_task(task_id, verify_authors=body.verify_authors)
            return db.get_task(task_id)

    @app.post('/api/tasks/{task_id}/stop')
    def stop_task(task_id: str):
        with manager.lock:
            task_or_404(task_id)
            if manager.task_id == task_id:
                manager.stop()
            else:
                db.update_task(task_id, stop_requested=True)
            return db.get_task(task_id)

    @app.post('/api/tasks/{task_id}/refresh')
    def refresh(task_id: str):
        old = task_or_404(task_id)
        return db.create_task(old['source_url'], old['video_id'], kind=old['kind'], target_account=old['target_account'],
                              max_videos=old['max_videos'], start_time=old['start_time'], end_time=old['end_time'], list_order=old['list_order'], verify_authors=old['verify_authors'])

    @app.post('/api/tasks/{task_id}/verify')
    def verify(task_id: str):
        with manager.lock:
            task = task_or_404(task_id)
            if task['status'] in ACTIVE or manager.check():
                raise HTTPException(409, '请先停止当前操作，再核实作者')
            if not task['unknown_authors']:
                return task
            manager.start(task_id, verify=True)
            return db.get_task(task_id)

    @app.delete('/api/tasks/{task_id}')
    def delete(task_id: str):
        with manager.lock:
            task_or_404(task_id)
            if manager.task_id == task_id and manager.check():
                raise HTTPException(409, '请先停止该任务，再删除本地数据')
            db.delete_task(task_id)
            return {'ok':True}

    @app.get('/api/tasks/{task_id}/comments')
    def comments(task_id: str, filters: Filters = Depends(query_filters), page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
        task = task_or_404(task_id)
        if task['kind'] != 'comments':
            raise HTTPException(400, '该任务是视频列表，请切换到视频结果')
        offset = (page-1)*page_size
        items, total, missing = db.read_comment_page(task_id, filters, limit=page_size, offset=offset)
        return {'items':items, 'total':total, 'page':page, 'page_size':page_size,
                'unknown_authors':task['unknown_authors'], 'missing_times':missing,
                'collection_complete':task['collection_complete'], 'verification_complete':task['verification_complete']}

    @app.get('/api/tasks/{task_id}/videos')
    def liked_videos(task_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
        task = task_or_404(task_id)
        if task['kind'] not in ('likes', 'recommendations'):
            raise HTTPException(400, '该任务是评论采集，请切换到评论结果')
        offset = (page - 1) * page_size
        rows, total = db.read_liked_video_page(task_id, limit=page_size, offset=offset)
        fields = ('video_id', 'title', 'author_name', 'author_douyin_id', 'created_at', 'time_text', 'video_url')
        return {'items':[{key: row[key] for key in fields} for row in rows],
                'total':total, 'page':page, 'page_size':page_size,
                'collection_complete':task['collection_complete'], 'visibility':task['visibility']}

    @app.get('/api/tasks/{task_id}/export.txt')
    def export(task_id: str, filters: Filters = Depends(query_filters)):
        task = task_or_404(task_id)
        if task['kind'] in ('likes', 'recommendations'):
            items = db.read_liked_videos(task_id)
            if task['visibility'] == 'private' and not items:
                label = '推荐' if task['kind'] == 'recommendations' else '喜欢'
                raise HTTPException(409, f'该账号未公开{label}列表，没有可导出的记录')
            payload = export_likes_txt(items, task['collection_complete'])
            filename = f"douyin-{task['kind']}-{task_id[:12]}.txt"
        else:
            items = select_comments(db.read_comments(task_id), filters)
            payload = export_txt(items, task['collection_complete'])
            filename = f"douyin-comments-{task['video_id'] or task_id[:12]}.txt"
        return Response(payload, media_type='text/plain; charset=utf-8', headers={'Content-Disposition':f'attachment; filename="{filename}"','Cache-Control':'no-store'})

    dist = ROOT / 'frontend' / 'dist'
    if (dist / 'assets').is_dir():
        app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='assets')

    @app.get('/')
    def index():
        if not (dist / 'index.html').is_file():
            return JSONResponse({'detail':'前端尚未构建，请运行初始化环境.cmd'}, status_code=503)
        return FileResponse(dist / 'index.html')

    @app.get('/favicon.svg')
    def favicon():
        icon = dist / 'favicon.svg'
        return FileResponse(icon) if icon.is_file() else Response(status_code=204)

    return app


app = create_app()

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8765, log_level='info')
