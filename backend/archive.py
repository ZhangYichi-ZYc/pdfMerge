"""留档区（archive）—— 监管要求的唯一落点。

要求是「所有合并前的原文件必须保留，按上传文件名 + 时间戳重命名」。

三条不可违反的规则：

1. **归档件永不覆盖。** 命名里带 job 短 ID。同一个人在同一秒拖进两个不同文件夹
   里的《合同.pdf》是完全正常的操作，纯「文件名 + 时间戳」会让它们互相覆盖 ——
   留档系统里发生覆盖是致命缺陷，多 8 个字符买的是「永不覆盖」。

2. **归档件是合并的唯一输入源。** 合并阶段不再持有上传流，只读归档路径。
   这样「留下的那份」和「被合并的那份」在物理上就是同一个字节序列，
   不存在留档件与处理件不一致的可能。

3. **本模块的任何信息都不得流入 API 响应。** 前端用户永远不知道留档的存在 ——
   不出现在 JSON 字段里、不出现在错误信息里、不出现在生成 PDF 的元数据里。
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from . import config

# Windows 与 POSIX 都不接受的文件名字符，外加全部控制字符。
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows 保留设备名：叫这个名字的文件在 Windows 上根本创建不出来。
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_STEM_BUDGET = 80  # 给时间戳（15 字符）与短 ID（10 字符）留出长度预算

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class StoredFile:
    """一件已落盘的归档原件。拿到它就等于拿到合并阶段需要的一切。"""

    original_name: str  # 用户上传时的原始文件名，原样保真，用于审计
    archived_name: str  # 磁盘上的改名后文件名
    rel_path: str  # 相对 storage/ 的路径，落库存这个（换机器也能解析）
    sha256: str  # 非抵赖锚点
    size_bytes: int

    @property
    def abs_path(self) -> Path:
        return config.STORAGE_DIR / self.rel_path


def sanitize_stem(name: str, fallback: str = "document") -> str:
    """把用户提供的文件名压成可安全落盘的词干。

    只影响磁盘上的名字 —— 审计表里另存 ``original_name`` 保真，所以这里的
    任何改动都不会让留档失去可追溯性。
    """
    stem = Path(name.replace("\\", "/")).name  # 去掉任何目录成分
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    stem = _UNSAFE.sub("_", stem).strip(" .")
    stem = stem[:_STEM_BUDGET]
    if not stem or stem.upper() in _RESERVED:
        return fallback
    return stem


def archived_name(original_name: str, moment: datetime, job_id: str) -> str:
    """``<原名>__<时间戳>__<job短ID>.pdf``。

    统一落成小写 ``.pdf``：上传入口只接受 PDF（已校验魔数），归一扩展名可以
    避免 ``.PDF`` / ``.pdf`` 在磁盘上被当成两种东西。
    """
    return f"{sanitize_stem(original_name)}__{moment:%Y%m%d-%H%M%S}__{job_id[:8]}.pdf"


def archive_dir(moment: datetime) -> Path:
    """按天分片，避免单目录堆几万个文件后 NTFS 检索退化。"""
    return config.ARCHIVE_DIR / f"{moment:%Y}" / f"{moment:%m}" / f"{moment:%d}"


class TooLarge(Exception):
    """上传体积超过上限。"""


def store(
    src: BinaryIO,
    original_name: str,
    job_id: str,
    moment: datetime,
    max_bytes: int | None = None,
) -> StoredFile:
    """把一个上传流写进留档区，边写边算 sha256。

    调用方拿到返回的 :class:`StoredFile` 后应当**丢弃上传流**，后续一律从
    ``abs_path`` 读 —— 规则 2。

    ``max_bytes`` 在写入过程中实时校验：不能只信 ``Content-Length``，那是客户端
    说了算的。超限时删掉半截文件再抛错，不留残骸。
    """
    target_dir = archive_dir(moment)
    target_dir.mkdir(parents=True, exist_ok=True)

    target = target_dir / archived_name(original_name, moment, job_id)
    # 走到这里说明发生了真实碰撞（同秒 + 同 job + 同名，基本只可能是重试）。
    # 留档区的覆盖是致命缺陷，所以宁可多一次 stat 也不赌。
    if target.exists():
        target = target_dir / f"{target.stem}__{secrets.token_hex(2)}{target.suffix}"

    digest = hashlib.sha256()
    size = 0
    try:
        with target.open("wb") as dst:
            while chunk := src.read(_CHUNK):
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise TooLarge(original_name)
                digest.update(chunk)
                dst.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise

    return StoredFile(
        original_name=original_name,
        archived_name=target.name,
        rel_path=target.relative_to(config.STORAGE_DIR).as_posix(),
        sha256=digest.hexdigest(),
        size_bytes=size,
    )


def purge_expired(days: int, now: datetime) -> int:
    """删除超过 ``days`` 天的归档日目录。``days <= 0`` 时什么都不做。

    只在配置里显式设定了正数天数时才会被调用 —— 见 ``config`` 里对
    ``ARCHIVE_RETENTION_DAYS`` 默认值 0 的说明。
    """
    if days <= 0 or not config.ARCHIVE_DIR.exists():
        return 0

    cutoff = now.date().toordinal() - days
    removed = 0
    for day_dir in sorted(config.ARCHIVE_DIR.glob("*/*/*")):
        try:
            ordinal = datetime.strptime(
                "/".join(day_dir.parts[-3:]), "%Y/%m/%d"
            ).date().toordinal()
        except ValueError:
            continue  # 不是日目录，跳过
        if ordinal >= cutoff:
            continue
        for victim in day_dir.iterdir():
            if victim.is_file():
                victim.unlink(missing_ok=True)
                removed += 1
        day_dir.rmdir()
    return removed
