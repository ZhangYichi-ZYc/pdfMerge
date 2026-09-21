"""HTTP 层。

**这个文件受一条硬约束**：响应体、错误信息、下载文件名、生成 PDF 的元数据，
任何一处都不允许出现描述「服务器保留了上传原件」的字段或字样。前端用户看到的
一切必须像是一个不留痕的工具。

具体到代码上：客户端**只用 ``job_id`` 和文件下标定位东西**，从来不需要、也
永远拿不到服务端的文件名或路径。这样「不泄漏」不是靠记得删字段，而是结构上
就做不到 —— 路径压根没进过响应体。
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import archive, config, jobs, pdfcore

log = logging.getLogger("pdfmerge.api")

router = APIRouter(prefix="/api")

_PDF_MAGIC = b"%PDF-"


class MergeRequest(BaseModel):
    # 文件的稳定 key 序列，索引即最终页序。
    #
    # 为什么是 key 而不是「第几行」：用户可以在清单里拖拽排序、删除、追加，
    # 行号随时在变，而 key 不变。用行号会导致「拖了一下之后顺序就错位了」这种
    # 极难排查的 bug。key 是服务端自己发的随机串，只在本 job 内有效，不是路径。
    order: list[str] = Field(default_factory=list)
    force_flatten: list[str] = Field(default_factory=list)
    output_name: str = ""


def _limits() -> dict:
    return {
        "max_files": config.MAX_FILES_PER_JOB,
        "max_file_mb": config.MAX_FILE_BYTES // (1024 * 1024),
        "max_total_mb": config.MAX_TOTAL_BYTES // (1024 * 1024),
    }


@router.get("/limits")
def get_limits() -> dict:
    return _limits()


async def _receive(
    uploads: list[UploadFile], *, job_id: str, moment: datetime, already: int
) -> list[jobs.FileEntry]:
    """把一批上传收进归档区，返回对应的文件条目。

    校验顺序是「先便宜后昂贵」：数量 → 魔数 → 边写边限体积。魔数检查放在落盘
    之前，是为了不让非 PDF 进入归档区 —— 留档区里混进打不开的垃圾，是给将来
    的合规审查添麻烦。
    """
    if not uploads:
        raise HTTPException(400, "没有收到任何文件。")
    if already + len(uploads) > config.MAX_FILES_PER_JOB:
        raise HTTPException(
            400,
            f"一次最多合并 {config.MAX_FILES_PER_JOB} 个文件，"
            f"当前已有 {already} 个，再加 {len(uploads)} 个就超了。",
        )

    entries: list[jobs.FileEntry] = []
    total = 0

    for upload in uploads:
        name = upload.filename or "未命名.pdf"

        head = await upload.read(5)
        await upload.seek(0)
        if head != _PDF_MAGIC:
            raise HTTPException(400, f"「{name}」不是 PDF 文件。")

        try:
            stored = await run_in_threadpool(
                archive.store,
                upload.file,
                name,
                job_id,
                moment,
                config.MAX_FILE_BYTES,
            )
        except archive.TooLarge:
            raise HTTPException(
                400,
                f"「{name}」超过单个文件 "
                f"{config.MAX_FILE_BYTES // (1024 * 1024)} MB 的上限。",
            )
        except OSError as exc:
            log.exception("failed to store upload")
            raise HTTPException(500, f"「{name}」写入失败，请重试。") from exc

        total += stored.size_bytes
        if total > config.MAX_TOTAL_BYTES:
            raise HTTPException(
                400,
                f"本次上传总量超过 {config.MAX_TOTAL_BYTES // (1024 * 1024)} MB 的上限。",
            )

        entries.append(
            jobs.FileEntry(
                key=secrets.token_hex(4),
                archived_name=stored.archived_name,
                original_name=name,
                rel_path=stored.rel_path,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
            )
        )

    return entries


@router.post("/jobs")
async def create_job(
    request: Request,
    files: list[UploadFile] = File(...),
) -> dict:
    """接收上传，建档。

    文件在这一刻就已经落进留档区了 —— 判定点是「用户提交了」，不是「这件事
    成功了」。用户从这里离开、再也不回来点合并，原件同样留存。这是刻意的：
    失败和放弃恰恰是留档最该覆盖的情形。
    """
    job_id = jobs.new_job_id()
    entries = await _receive(
        files, job_id=job_id, moment=datetime.now(), already=0
    )

    job = jobs.create(
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        files=entries,
    )
    await run_in_threadpool(jobs.probe_all, job)

    return {"job_id": job.job_id, "files": _public_files(job), "limits": _limits()}


@router.post("/jobs/{job_id}/files")
async def add_files(job_id: str, files: list[UploadFile] = File(...)) -> dict:
    """往已有任务里追加文件（用户又拖进来几个）。"""
    job = _require(job_id)
    try:
        entries = await _receive(
            files, job_id=job.job_id, moment=datetime.now(), already=len(job.files)
        )
        jobs.append_files(job, entries)
    except jobs.JobError as exc:
        raise HTTPException(400, str(exc)) from exc

    await run_in_threadpool(jobs.probe_all, job)
    return {"job_id": job.job_id, "files": _public_files(job)}


@router.delete("/jobs/{job_id}/files/{file_id}")
def remove_file(job_id: str, file_id: str) -> dict:
    """把文件移出合并清单。"""
    job = _require(job_id)
    try:
        jobs.remove_file(job, file_id)
    except jobs.JobError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": job.job_id, "files": _public_files(job)}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = _require(job_id)
    return {
        "job_id": job.job_id,
        "status": job.status,
        "files": _public_files(job),
        "progress": {
            "done": job.done_files,
            "total": len(job.files),
            "message": job.message,
        },
        "output_name": job.output_name,
        "download_ready": job.status == "done" and job.result_path is not None,
        "error": job.error,
    }


@router.post("/jobs/{job_id}/merge", status_code=202)
def merge(job_id: str, body: MergeRequest) -> dict:
    job = _require(job_id)
    order = body.order or list(range(len(job.files)))
    try:
        jobs.start_merge(
            job,
            order=order,
            force_flatten=body.force_flatten,
            output_name=body.output_name,
        )
    except jobs.JobError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": job.job_id, "status": job.status}


@router.get("/jobs/{job_id}/download")
def download(job_id: str) -> FileResponse:
    job = _require(job_id)
    if job.status != "done" or job.result_path is None:
        raise HTTPException(409, "文件尚未生成完成。")
    if not job.result_path.exists():
        raise HTTPException(410, "文件已过期，请重新合并。")
    return FileResponse(
        job.result_path, media_type="application/pdf", filename=job.output_name
    )


@router.get("/jobs/{job_id}/files/{file_id}/thumb")
async def thumbnail(job_id: str, file_id: str) -> FileResponse:
    """首页缩略图。

    缓存键是文件内容的 sha256，所以同一份文件重复上传只会渲染一次 ——
    而且缓存文件名里不含任何用户提供的名字，顺带避免了文件名在缓存目录里
    留下第二份痕迹。
    """
    job = _require(job_id)
    entry = next((f for f in job.files if f.key == file_id), None)
    if entry is None:
        raise HTTPException(404, "文件不存在。")

    cached = config.THUMB_DIR / f"{entry.sha256}.png"
    if not cached.exists():
        try:
            png = await run_in_threadpool(
                pdfcore.render_thumb,
                config.STORAGE_DIR / entry.rel_path,
                config.THUMB_WIDTH,
            )
        except Exception as exc:
            log.warning("thumb failed for %s: %s", entry.archived_name, exc)
            raise HTTPException(422, "无法生成预览图。") from exc
        config.THUMB_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(".part")
        tmp.write_bytes(png)
        tmp.replace(cached)  # 原子替换：并发请求不会读到半张图

    return FileResponse(cached, media_type="image/png")


def _require(job_id: str) -> jobs.JobState:
    try:
        return jobs.get(job_id)
    except jobs.JobError as exc:
        raise HTTPException(404, str(exc)) from exc


def _public_files(job: jobs.JobState) -> list[dict]:
    return [f.public() for f in job.files]


def create_app() -> FastAPI:
    app = FastAPI(
        title="PDF 合并工具",
        description="把任意多的 PDF 按指定顺序合并成一个。",
        version="1.0.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.include_router(router)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 兜底：任何未预期的异常都不许把堆栈或路径带出去。
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": "服务器内部错误，请重试。"}, status_code=500)

    return app
