"""Build bounded TXT exports before sending any response bytes."""
from __future__ import annotations

import asyncio
from contextlib import closing, nullcontext
import json
from pathlib import Path
import re
import stat
import threading
import uuid

import anyio
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response

from .search import INCOMPLETE_NOTICE, record_block


class ExportCancelled(Exception):
    pass


def regular_path(path, *, directory=False):
    """Do not follow symlinks or Windows junctions during cleanup."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)


class ExportJob:
    def __init__(self, directory):
        self.directory = directory
        self.path = directory / 'payload.tmp'
        self.cancelled = threading.Event()

    def check_cancel(self):
        if self.cancelled.is_set():
            raise ExportCancelled()


class ExportStore:
    """A service-owned directory; recovery runs only under the service lease."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if not regular_path(self.root, directory=True):
            raise OSError('Export directory must be a regular directory')

    @staticmethod
    def marker(name):
        return {'owner': 'douyin-txt-export', 'version': 1, 'job': name}

    def create(self):
        if not regular_path(self.root, directory=True):
            raise OSError('Export directory must be a regular directory')
        directory = self.root / ('job-' + uuid.uuid4().hex)
        directory.mkdir()
        job = ExportJob(directory)
        try:
            with (directory / 'owner.json').open('x', encoding='utf-8') as output:
                json.dump(self.marker(directory.name), output)
        except BaseException:
            # This process just created the directory exclusively. A failed
            # marker write is known to be ours even though recovery must reject
            # malformed markers from earlier runs.
            try:
                if (regular_path(directory, directory=True)
                        and all(p.name == 'owner.json' and regular_path(p) for p in directory.iterdir())):
                    (directory / 'owner.json').unlink(missing_ok=True)
                    directory.rmdir()
            except OSError:
                pass
            raise
        return job

    def cleanup(self, job):
        directory = job.directory
        try:
            if (directory.parent != self.root or not re.fullmatch(r'job-[0-9a-f]{32}', directory.name)
                    or not regular_path(self.root, directory=True) or not regular_path(directory, directory=True)):
                return
            entries = list(directory.iterdir())
            if (not entries or any(p.name not in ('owner.json', 'payload.tmp') or not regular_path(p) for p in entries)):
                return
            marker = directory / 'owner.json'
            if marker.stat().st_size > 256 or json.loads(marker.read_text(encoding='utf-8')) != self.marker(directory.name):
                return
            job.path.unlink(missing_ok=True)
            marker.unlink()
            directory.rmdir()
        except (OSError, ValueError):
            # Locked or unrecognised files stay in place; a later startup can
            # retry owned files without touching saved downloads or other data.
            return

    def recover(self):
        for directory in self.root.iterdir():
            self.cleanup(ExportJob(directory))


def write_batches(path, batches, fields, collection_complete, check_cancel):
    """Preserve the existing BOM, escaping, separators and final CRLF."""
    batch_context = closing(batches) if hasattr(batches, 'close') else nullcontext(batches)
    with batch_context, path.open('xb') as output:
        check_cancel()
        output.write(b'\xef\xbb\xbf')
        has_block = not collection_complete
        if has_block:
            output.write(INCOMPLETE_NOTICE.encode('utf-8'))
        for batch in batches:
            for item in batch:
                check_cancel()
                if has_block:
                    output.write(b'\r\n\r\n')
                output.write(record_block(item, fields).encode('utf-8'))
                has_block = True
        if has_block:
            output.write(b'\r\n')
        check_cancel()


class ExportResponse(Response):
    def __init__(self, store, generate):
        super().__init__()
        self.store, self.generate = store, generate

    async def _generate(self, job):
        # Shield the thread's task so cancellation cannot leave a writer using
        # a deleted file or an open SQLite snapshot after this response exits.
        task = asyncio.create_task(anyio.to_thread.run_sync(self.generate, job))
        try:
            return await asyncio.shield(task)
        finally:
            if not task.done():
                job.cancelled.set()
                with anyio.CancelScope(shield=True):
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            # A parent awaiting this ASGI task can cancel it
                            # again while its AnyIO scope is unwinding.
                            continue
                        except ExportCancelled:
                            break

    async def __call__(self, scope, receive, send):
        try:
            job = self.store.create()
        except OSError:
            return await JSONResponse({'detail': '无法创建导出文件，请检查本地磁盘空间和写入权限'}, status_code=500)(scope, receive, send)
        try:
            async with anyio.create_task_group() as group:
                async def watch_disconnect():
                    while (await receive())['type'] != 'http.disconnect':
                        pass
                    job.cancelled.set()
                    group.cancel_scope.cancel()

                group.start_soon(watch_disconnect)
                try:
                    try:
                        filename = await self._generate(job)
                    except HTTPException as exc:
                        return await JSONResponse({'detail': exc.detail}, status_code=exc.status_code, headers=exc.headers)(scope, receive, send)
                    except OSError:
                        return await JSONResponse({'detail': '无法生成导出文件，请检查本地磁盘空间和写入权限'}, status_code=500)(scope, receive, send)
                    job.check_cancel()
                    # Generation has closed its transaction and output handle.
                    # Send chunks ourselves: no range or deferred path-send
                    # changes to the existing download contract.
                    headers = Response(media_type='text/plain; charset=utf-8', headers={
                        'Content-Disposition': f'attachment; filename="{filename}"',
                        'Cache-Control': 'no-store', 'Content-Length': str(job.path.stat().st_size),
                    }).raw_headers
                    source = await anyio.open_file(job.path, 'rb')
                    try:
                        await send({'type': 'http.response.start', 'status': 200, 'headers': headers})
                        while chunk := await source.read(64 * 1024):
                            await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
                        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
                    finally:
                        with anyio.CancelScope(shield=True):
                            await source.aclose()
                except ExportCancelled:
                    pass
                finally:
                    group.cancel_scope.cancel()
        finally:
            job.cancelled.set()
            self.store.cleanup(job)
