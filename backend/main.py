"""服务入口。

生产模式是**单端口单进程**：FastAPI 既提供 /api，又把 ``frontend/dist/`` 作为
静态资源挂出去。目标机器上只需要 Python + ``pip install -r requirements.txt``，
不需要 Node、不需要 Nginx。

启动：
    <python> -m backend
或指定端口：
    <python> -m backend --port 9000
"""

from __future__ import annotations

import argparse
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import api, audit, config, jobs

log = logging.getLogger("pdfmerge")

SWEEP_INTERVAL_SECONDS = 3600


def _sweep_forever() -> None:
    """每小时回收一次过期产物。守护线程，随主进程一起退出。"""
    stop = threading.Event()
    while not stop.wait(SWEEP_INTERVAL_SECONDS):
        try:
            jobs.sweep()
        except Exception:
            log.exception("sweep failed; will retry next hour")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    audit.init()
    jobs.sweep()  # 启动先清一次：上次异常退出留下的中间产物不跨重启存活
    threading.Thread(target=_sweep_forever, name="sweep", daemon=True).start()
    log.info("存储根目录：%s", config.STORAGE_DIR)
    log.info("并发上限：%d", config.MAX_CONCURRENT_JOBS)
    yield


def build() -> FastAPI:
    app = api.create_app()
    app.router.lifespan_context = lifespan

    dist = config.BASE_DIR / "frontend" / "dist"
    if dist.is_dir():
        # 挂在最后：/api 的路由先匹配，剩下的才交给静态资源。
        app.mount("/", StaticFiles(directory=dist, html=True), name="static")
    else:
        log.warning(
            "未找到前端构建产物 %s —— 只提供 /api。"
            "开发时用 `npm run dev`（自带代理），或先执行 `npm run build`。",
            dist,
        )
    return app


app = build()


def main() -> None:
    parser = argparse.ArgumentParser(description="PDF 合并工具站")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--reload", action="store_true", help="开发用热重载")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    import uvicorn

    uvicorn.run(
        "backend.main:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        # 不走 uvloop（POSIX-only，Windows 上不可用），保持跨平台一致。
        loop="asyncio",
    )


if __name__ == "__main__":
    main()
