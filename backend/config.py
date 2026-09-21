"""全局配置。

所有可调项集中在这里，且**每一项都能用环境变量覆盖** —— 换一台机器部署时
不需要改任何代码。代码里不存在任何绝对路径常量，存储根目录默认取项目根下的
``./storage``，可用 ``PDFMERGE_STORAGE`` 整体挪走。
"""

from __future__ import annotations

import os
from pathlib import Path

# 项目根目录（本包的上一级）。不用 os.getcwd()，因为它取决于启动时的工作目录。
BASE_DIR = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"环境变量 {name} 必须是整数，当前值：{raw!r}")


# ---------------------------------------------------------------- 存储布局
STORAGE_DIR = _env_path("PDFMERGE_STORAGE", BASE_DIR / "storage")
ARCHIVE_DIR = STORAGE_DIR / "archive"  # 上传原件留档区，永不自动删除
OUTPUT_DIR = STORAGE_DIR / "output"  # 合并结果，按 job 分目录
THUMB_DIR = STORAGE_DIR / "thumb"  # 首页缩略图缓存，按内容 sha256 命名
TMP_DIR = STORAGE_DIR / "tmp"  # 栅格化中间产物，任务结束即删
AUDIT_DB = STORAGE_DIR / "audit.sqlite3"  # 审计日志

# ---------------------------------------------------------------- 上传上限
# 「任意多的 PDF」在工程上必须落成具体数字：栅格化路线的内存峰值约为原文件的
# 10~40 倍，没有上限的话一次合并就能把服务器打爆。
MAX_FILE_BYTES = _env_int("PDFMERGE_MAX_FILE_MB", 200) * 1024 * 1024
MAX_FILES_PER_JOB = _env_int("PDFMERGE_MAX_FILES", 50)
MAX_TOTAL_BYTES = _env_int("PDFMERGE_MAX_TOTAL_MB", 1024) * 1024 * 1024
JOB_TIMEOUT_SECONDS = _env_int("PDFMERGE_JOB_TIMEOUT", 600)  # 10 分钟

# ---------------------------------------------------------------- 并发
# 同时运行的 job 数。这个数字就是内存上限的旋钮 —— 2 个 200MB 文件同时在
# 栅格化，峰值可能到 GB 级。局域网工具站同时用的人是个位数，2 已足够。
MAX_CONCURRENT_JOBS = _env_int("PDFMERGE_CONCURRENCY", 2)

# ---------------------------------------------------------------- 生命周期
OUTPUT_RETENTION_HOURS = _env_int("PDFMERGE_OUTPUT_RETENTION_HOURS", 24)
UPLOAD_RETENTION_HOURS = _env_int("PDFMERGE_UPLOAD_RETENTION_HOURS", 24)
THUMB_RETENTION_DAYS = _env_int("PDFMERGE_THUMB_RETENTION_DAYS", 30)
# 留档区保留天数。0 = 永久保留。
#
# 默认值是 0 而非某个天数，是刻意的：留档系统的自动删除是危险默认值。
# 合规要求的「至少 N 年」不等于「到期就删」，到期销毁应当是显式的人工动作，
# 不该由代码在某个凌晨悄悄执行。确有强制销毁期限时才把它设成正数。
ARCHIVE_RETENTION_DAYS = _env_int("PDFMERGE_ARCHIVE_RETENTION_DAYS", 0)

# ---------------------------------------------------------------- 渲染
RASTER_DPI = _env_int("PDFMERGE_RASTER_DPI", 200)
THUMB_WIDTH = _env_int("PDFMERGE_THUMB_WIDTH", 240)

# ---------------------------------------------------------------- 服务
HOST = os.environ.get("PDFMERGE_HOST", "0.0.0.0").strip() or "0.0.0.0"
PORT = _env_int("PDFMERGE_PORT", 8000)


def ensure_dirs() -> None:
    """创建全部存储目录。幂等，服务启动时调用一次。"""
    for path in (STORAGE_DIR, ARCHIVE_DIR, OUTPUT_DIR, THUMB_DIR, TMP_DIR):
        path.mkdir(parents=True, exist_ok=True)
