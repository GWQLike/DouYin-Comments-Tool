"""Command-line entry point for the MediaCrawler worker."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from pathlib import Path
import signal
import sys
import threading

from backend.errors import ManualActionRequired, PageFailure, StopRequested
from backend.runtime_control import POLL_SECONDS, OwnershipError, ProcessLease, StopSignal, owner_alive


async def worker(args: argparse.Namespace) -> int:
    directory = Path(args.db).resolve().parent
    owner_token = getattr(args, 'service_token', None)
    try:
        with ExitStack() as leases:
            if owner_token:
                if not owner_alive(directory, owner_token):
                    raise OwnershipError('所属服务已退出，未启动采集。')
            else:
                # Standalone CLI runs must not compete with a running service.
                leases.enter_context(ProcessLease(directory, 'service'))
            leases.enter_context(ProcessLease(directory, 'worker'))
            if owner_token and not owner_alive(directory, owner_token):
                raise OwnershipError('所属服务已退出，未启动采集。')
            return await _worker_owned(args, (lambda: owner_alive(directory, owner_token)) if owner_token else None)
    except OwnershipError as exc:
        print(str(exc), flush=True)
        return 1


async def _worker_owned(args: argparse.Namespace, owner_check) -> int:
    # Load the database and engine only after taking the process leases.
    from backend.db import Database
    from backend.engine import run_engine

    db = Database(str(Path(args.db).resolve()))
    stop = StopSignal(lambda: db.should_stop(args.task) if args.task else
                      db.session_get().get('status') == 'logged_out', owner=owner_check)
    if getattr(args, 'stop_pipe', False):
        def watch_pipe():
            try:
                sys.stdin.buffer.read(1)
            finally:
                stop.request()

        threading.Thread(target=watch_pipe, name='service-stop', daemon=True).start()
    verifying = args.verify_only

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop.request)

    def waiting(wait: bool, message: str | None = None) -> None:
        nonlocal verifying
        db.session_set("waiting" if wait else "ready", message)
        if args.task:
            current = db.get_task(args.task)
            verifying = verifying or current['status'] == 'verifying'
            if wait:
                db.transition_task(args.task, 'waiting', message=message, verify_only=verifying)
            else:
                db.transition_task(args.task, 'resume', verify_only=verifying)

    try:
        db.session_set("waiting")
        pending = asyncio.create_task(run_engine(db, args, stop, waiting))
        try:
            while not pending.done():
                if stop():
                    raise StopRequested()
                await asyncio.wait({pending}, timeout=POLL_SECONDS)
            await pending
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        return 0
    except StopRequested as exc:
        if args.task:
            db.transition_task(args.task, 'worker_pause', message=getattr(exc, 'public_reason', None))
        else:
            db.session_set("logged_out")
        return 0
    except Exception as exc:
        # Do not expose upstream exception bodies, browser command lines or login tokens.
        message = "采集进程未能启动或浏览器已关闭。请检查安装、Node.js 和浏览器后重试。"
        if isinstance(exc, (PageFailure, ManualActionRequired)):
            message = str(exc)
        db.session_set("error", message)
        if args.task:
            db.transition_task(args.task, 'worker_failed', message=message)
        return 1
    finally:
        if stop.parent_lost or (owner_check is not None and not owner_check()):
            db.session_set('logged_out', '所属服务已退出，采集已停止并保留断点。')


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Douyin comment collection worker")
    parser.add_argument("--db", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--task")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--max-requests", type=int, default=0)
    parser.add_argument("--max-seconds", type=float, default=0)
    parser.add_argument("--stop-on-challenge", action="store_true")
    parser.add_argument("--service-token")
    parser.add_argument("--stop-pipe", action="store_true")
    return asyncio.run(worker(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
