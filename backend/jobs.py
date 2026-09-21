"""job 调度与状态机。

一次合并 = 一个 job。状态流转：

    draft ──(用户点「合并」)──> queued ──(拿到并发槽)──> processing ──> done
                                                                  └──> failed

**为什么必须分两阶段**：用户拖进 12 个文件之后，要先看到它们、调整顺序、看到
哪几个带签名，然后才决定合。合并不能在文件清单出现之前开始 —— 这个约束来自
产品语义（可排序、可勾选强制扁平），不是实现细节，所以状态机必须显式建模它。

**并发模型**：一个 job 占一个线程，job 内部逐文件串行。``ThreadPoolExecutor``
的 ``max_workers`` 天然就是并发上限 —— 超出的 job 排在队列里，状态显示
``queued``。这个数字同时是内存上限的旋钮（见 ``config.MAX_CONCURRENT_JOBS``）。

**状态存放在两个地方**，各司其职：

- 内存中的 :class:`JobState` —— 给前端轮询用的实时进度，进程重启即丢，无所谓
- SQLite 里的审计表 —— 留档证据，进程重启后仍在，是「发生过什么」的唯一权威

两者不做同步，因为它们回答的不是同一个问题。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import archive, audit, config, pdfcore

log = logging.getLogger("pdfmerge.jobs")


class JobError(Exception):
    """可以安全地展示给用户看的错误。

    ``str(exc)`` 的内容会原样进入 API 响应 —— 所以**绝不允许**在抛它的时候
    带上归档路径、存储目录、堆栈之类的东西。
    """


@dataclass
class FileEntry:
    """job 里的一件文件。前端能看到的关于文件的全部信息就是这些字段。

    ``key`` 是文件在这份 job 内的**稳定标识**：用户在清单里拖拽排序、删除、
    再追加，它都不变。这一点很关键 —— 如果前端用「第几行」来指代文件，用户
    一拖动，所有的指代就全错位了。所以客户端只认 ``key``（API 里叫 ``id``），
    排序、删除、缩略图、强制开关全部按 key 走。
    """

    key: str  # 稳定标识；API 响应里暴露为 id
    archived_name: str  # 内部标识，不直接出现在响应里
    original_name: str
    rel_path: str
    sha256: str
    size_bytes: int
    page_count: int | None = None
    has_signature: bool = False
    action: str | None = None  # direct | bake | raster
    error: str | None = None

    def public(self) -> dict:
        """转成 API 响应里的文件对象。

        这里刻意**不含** ``archived_name`` / ``rel_path`` / ``sha256`` —— 它们
        描述的是服务器上的留档，属于前端用户不该知道的那部分。
        """
        return {
            "id": self.key,
            "name": self.original_name,
            "size": self.size_bytes,
            "pages": self.page_count,
            "signed": self.has_signature,
            "action": self.action,
            "error": self.error,
        }


@dataclass
class JobState:
    job_id: str
    created_at: datetime
    output_name: str = ""
    status: str = "draft"
    files: list[FileEntry] = field(default_factory=list)
    order: list[str] = field(default_factory=list)  # archived_name 序列
    force_flatten: set[str] = field(default_factory=set)
    done_files: int = 0
    message: str = ""
    error: str | None = None
    result_path: Path | None = None
    started_at: float | None = None
    finished_at: float | None = None

    # 合并开始后，用户回传的顺序就固定下来了；此前一直按上传顺序。
    def effective_order(self) -> list[str]:
        return self.order or [f.archived_name for f in self.files]

    def entry(self, archived_name: str) -> FileEntry:
        for f in self.files:
            if f.archived_name == archived_name:
                return f
        raise KeyError(archived_name)


_jobs: dict[str, JobState] = {}
_lock = threading.RLock()
_pool: ThreadPoolExecutor | None = None


def _executor() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(
            max_workers=config.MAX_CONCURRENT_JOBS, thread_name_prefix="pdfmerge"
        )
    return _pool


def new_job_id() -> str:
    return uuid.uuid4().hex[:16]


# --------------------------------------------------------------------- 建档
def create(
    *, client_ip: str | None, user_agent: str | None, files: list[FileEntry]
) -> JobState:
    """上传完成、文件已进留档区。此时还不合并，等用户确认顺序。"""
    job = JobState(job_id=new_job_id(), created_at=datetime.now(), files=files)
    with _lock:
        _jobs[job.job_id] = job

    # 落审计表用 (原名, 归档名, 相对路径, sha256, 大小) 五元组。
    audit.create_job(
        job_id=job.job_id,
        client_ip=client_ip,
        user_agent=user_agent,
        files=[
            (f.original_name, f.archived_name, f.rel_path, f.sha256, f.size_bytes)
            for f in files
        ],
    )
    return job


def get(job_id: str) -> JobState:
    with _lock:
        job = _jobs.get(job_id)
    if job is None:
        raise JobError("任务不存在或已过期，请重新上传文件。")
    return job


def probe_all(job: JobState) -> None:
    """逐个探测页数与签名，回填到内存状态与审计表。

    在留档之后、用户点合并之前做 —— 前端的文件卡片要显示「47 页 · 含签名」，
    探测结果必须在清单渲染时就已经就绪。已探测过的跳过，所以可以安全地
    在每次追加文件后重复调用。
    """
    for f in job.files:
        if f.page_count is not None or (f.error and "加密" in f.error):
            continue
        try:
            result = pdfcore.probe(config.STORAGE_DIR / f.rel_path)
            f.page_count = result.page_count
            f.has_signature = result.has_signature
            if result.needs_password:
                f.error = "该文件已加密，需要密码才能合并。"
        except Exception as exc:  # 探测失败不阻断上传，留给合并阶段报错
            f.error = str(exc)
            log.warning("probe failed for %s: %s", f.archived_name, exc)
        audit.set_probe(
            job_id=job.job_id,
            archived_name=f.archived_name,
            page_count=f.page_count or 0,
            has_signature=f.has_signature,
        )


# --------------------------------------------------------------------- 排期
def append_files(job: JobState, files: list[FileEntry]) -> None:
    """往草稿状态的 job 里追加文件。

    为什么需要它：用户的 PDF 常常散在不同文件夹里（合同在一处、附件在另一处），
    要求一次选中全部文件是没必要的折磨。追加与首次上传走的是同一条归档路径，
    所以留档语义完全一致。
    """
    with _lock:
        if job.status != "draft":
            raise JobError("该任务已经开始处理，无法再添加文件。")
        if len(job.files) + len(files) > config.MAX_FILES_PER_JOB:
            raise JobError(
                f"一次最多合并 {config.MAX_FILES_PER_JOB} 个文件，"
                f"再加就超了（当前 {len(job.files)} 个）。"
            )
        job.files.extend(files)

    audit.add_job_files(
        job_id=job.job_id,
        files=[
            (f.original_name, f.archived_name, f.rel_path, f.sha256, f.size_bytes)
            for f in files
        ],
    )


def remove_file(job: JobState, key: str) -> FileEntry:
    """把文件移出**合并清单**。

    注意它不碰留档区：文件照旧留在归档里。这是对的 —— 监管要求留的是
    「用户上传过的原件」，而不是「最终参与了合并的文件」。审计表里这一行仍然
    存在，只是 ``ordinal`` 为 NULL、``action`` 为空，如实记录了「传了但没合」。
    """
    with _lock:
        if job.status != "draft":
            raise JobError("该任务已经开始处理，无法再修改文件清单。")
        for i, entry in enumerate(job.files):
            if entry.key == key:
                return job.files.pop(i)
        raise JobError("文件不存在。")


def start_merge(
    job: JobState,
    *,
    order: list[str],
    force_flatten: list[str],
    output_name: str,
) -> None:
    """校验用户提交的顺序，转 ``queued`` 并把活派给线程池。

    顺序用**文件的稳定 key** 表达，服务端把它翻译成 ``archived_name``。永远
    不接受客户端直接传文件名或路径 —— 那是路径穿越的入口。
    """
    with _lock:
        if job.status != "draft":
            raise JobError("该任务已经开始处理，无法重复提交。")
        if not job.files:
            raise JobError("没有可合并的文件。")

        known = {f.key: f.archived_name for f in job.files}
        chosen = order or [f.key for f in job.files]

        unknown = [k for k in chosen if k not in known]
        if unknown:
            raise JobError("提交的文件清单与任务不符，请刷新页面后重试。")
        # 顺序必须恰好是全部文件的一个排列。少传会静默丢文件，多传会重复
        # 计入页数 —— 两种都不能接受。
        if len(set(chosen)) != len(chosen) or set(chosen) != set(known):
            raise JobError("提交的文件清单不完整，请刷新页面后重试。")

        job.output_name = _safe_output_name(output_name)
        job.order = [known[k] for k in chosen]
        job.force_flatten = {known[k] for k in force_flatten if k in known}
        job.status = "queued"
        job.message = "排队中…"

    audit.begin_merge(
        job_id=job.job_id,
        output_name=job.output_name,
        order=job.order,
        force_flatten=sorted(job.force_flatten),
    )
    _executor().submit(_run, job.job_id)


def _safe_output_name(name: str) -> str:
    stem = archive.sanitize_stem(name, fallback="merged")
    return f"{stem}.pdf"


def _run(job_id: str) -> None:
    """线程池里执行的实际合并。任何异常都必须在这里被吃掉并落成 failed 状态，
    绝不能逃逸到 Future 里 —— 那会让 job 永远停在 processing。"""
    with _lock:
        try:
            job = _jobs[job_id]
        except KeyError:
            return
        job.status = "processing"
        job.started_at = time.monotonic()
        job.message = "正在合并…"

    try:
        out_dir = config.OUTPUT_DIR / job.job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / job.output_name

        order = job.effective_order()
        sources = [job.entry(name) for name in order]

        def on_progress(done: int, total: int, name: str) -> None:
            with _lock:
                job.done_files = done
                job.message = f"正在处理 {done}/{total}：{name}"
            _check_timeout(job)

        force_paths = {
            config.STORAGE_DIR / job.entry(name).rel_path
            for name in job.force_flatten
        }

        report = pdfcore.merge(
            sources=[
                (config.STORAGE_DIR / f.rel_path, f.original_name) for f in sources
            ],
            force_flatten=force_paths,
            out_path=out_path,
            on_progress=on_progress,
        )

        with _lock:
            for entry, action in zip(sources, report.actions):
                entry.action = action
            job.done_files = len(sources)
            job.result_path = out_path
            job.status = "done"
            job.finished_at = time.monotonic()
            job.message = "合并完成"

        for entry in sources:
            audit.set_action(
                job_id=job.job_id,
                archived_name=entry.archived_name,
                action=entry.action or "direct",
            )
        audit.finish_job(
            job_id=job.job_id,
            status="done",
            engine=report.summary(),
            duration_ms=int((job.finished_at - job.started_at) * 1000),
        )
        log.info("job %s done: %s", job.job_id, report.summary())

    except Exception as exc:
        with _lock:
            job.status = "failed"
            job.finished_at = time.monotonic()
            job.error = str(exc)
            job.message = "合并失败"
        # 失败也要留痕：审计表里必须有这一条，否则「哪些文件参与过合并」会有缺口。
        audit.finish_job(
            job_id=job.job_id,
            status="failed",
            error=str(exc),
            duration_ms=(
                int((job.finished_at - job.started_at) * 1000)
                if job.started_at
                else None
            ),
        )
        log.exception("job %s failed", job.job_id)


def _check_timeout(job: JobState) -> None:
    if job.started_at is None:
        return
    if time.monotonic() - job.started_at > config.JOB_TIMEOUT_SECONDS:
        raise JobError(
            f"任务超过 {config.JOB_TIMEOUT_SECONDS // 60} 分钟仍未完成，已中止。"
            "请减少文件数量或页数后重试。"
        )


# --------------------------------------------------------------------- 清理
def sweep() -> None:
    """回收过期产物。服务启动时跑一次，之后每小时一次。

    **不碰留档区** —— 除非 ``ARCHIVE_RETENTION_DAYS`` 被显式设成正数
    （见 config 里对默认值 0 的说明）。
    """
    now = datetime.now()
    removed = {"output": 0, "tmp": 0, "thumb": 0, "state": 0}

    deadline = now - timedelta(hours=config.OUTPUT_RETENTION_HOURS)
    if config.OUTPUT_DIR.exists():
        for job_dir in config.OUTPUT_DIR.iterdir():
            if job_dir.is_dir() and _older_than(job_dir, deadline):
                _rmtree(job_dir)
                removed["output"] += 1

    # 中间产物：任何残留都是异常退出的痕迹，一律清掉。
    if config.TMP_DIR.exists():
        for victim in config.TMP_DIR.iterdir():
            _rmtree(victim) if victim.is_dir() else victim.unlink(missing_ok=True)
            removed["tmp"] += 1

    thumb_deadline = now - timedelta(days=config.THUMB_RETENTION_DAYS)
    if config.THUMB_DIR.exists():
        for path in config.THUMB_DIR.glob("*.png"):
            if _older_than(path, thumb_deadline):
                path.unlink(missing_ok=True)
                removed["thumb"] += 1

    # 内存状态：用户上传后再没回来点合并的草稿，从内存里丢掉，省得无限增长。
    # （它们在留档区里的原件不受影响 —— 那是 Q14(a) 的既定语义。）
    state_deadline = now - timedelta(hours=config.UPLOAD_RETENTION_HOURS)
    with _lock:
        for job_id, job in list(_jobs.items()):
            terminal = job.status in ("done", "failed")
            if job.created_at < state_deadline and (terminal or job.status == "draft"):
                del _jobs[job_id]
                removed["state"] += 1

    if n := archive.purge_expired(config.ARCHIVE_RETENTION_DAYS, now):
        log.warning("归档区按配置的保留期删除了 %d 个文件", n)

    if any(removed.values()):
        log.info("sweep: %s", removed)


def _older_than(path: Path, deadline: datetime) -> bool:
    return datetime.fromtimestamp(path.stat().st_mtime) < deadline


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)
