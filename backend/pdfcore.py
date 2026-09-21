"""合并管线 —— 整个项目的核心。

## 三条处理路径

每个源文件按「是否需要保签名外观」分流：

``direct``
    直接 ``insert_pdf``。最快、保矢量、保文本层。**没有签名的文件走这条。**

``bake``
    把注释与表单字段**烤进页面内容流**，然后删掉注释对象。签名图样（它是
    注释）从此成为页面的一部分，不再依赖签名域来渲染，所以外观必定保留；
    同时原页面的矢量与文本层**不受影响**。这是「虚拟打印」想要的效果，
    但输出质量更高。

``raster``
    ``bake`` 不可用或失败时的兜底：整页光栅化成位图后重建 PDF。与 ``bake``
    在「保住外观」这个目标上等价，代价是丢文本层、体积膨胀 5~20 倍。
    这条路**永不失败**。

## 为什么不用虚拟打印

用户最初的设想是走 Windows 虚拟打印机。实测与推理都表明不该这么做：

- 字面意义的「虚拟打印」保住签名外观的唯一原因是它**光栅化**了整页 ——
  而 ``bake`` / ``raster`` 在纯 Python 里达到同样的视觉结果，不需要打印
  子系统、不需要 pywin32、不需要 Windows。
- 打印方案会引入串行阻塞、纸张尺寸导致的轻微缩放、偶发弹窗挂住服务，
  并且把整个项目锁死在 Windows 上。而它要求部署环境「可能不同」。

结论：**保外观的目标照做，打印作为手段被替换掉。**

## 签名检测

判据取自 PDF 规范，不做图像启发式（误报率太高）：

- ``/SigFlags`` —— AcroForm 的签名标志位
- AcroForm 里的 ``/Sig`` 类型字段 —— 真正被签署过的表单域
- ``/Perms`` 里的 ``/DocMDP`` —— 认证签名（certification signature）

任一条命中即判定「含签名」，走保外观路径。检测必然有漏报（盖章图片、已被
拍平过的旧签名、畸形文件），所以前端保留一个手动强制开关兜底。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import pymupdf

from . import config

log = logging.getLogger("pdfmerge.core")


class MergeError(Exception):
    """可安全展示给用户的失败原因。不得包含服务端路径。"""


@dataclass
class ProbeResult:
    page_count: int
    has_signature: bool
    needs_password: bool


@dataclass
class MergeReport:
    actions: list[str] = field(default_factory=list)  # 与 sources 一一对应
    page_counts: list[int] = field(default_factory=list)

    def summary(self) -> str:
        tally: dict[str, int] = {}
        for action in self.actions:
            tally[action] = tally.get(action, 0) + 1
        return " ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "empty"


# ------------------------------------------------------------------ 能力探测
# `bake()` 是较新的 API。为了「部署到别的机器」这个要求，这里做能力探测而不是
# 直接调用 —— 老一点的 PyMuPDF 上会自动退回栅格化，而不是崩在 AttributeError。
_HAS_BAKE = hasattr(pymupdf.Document, "bake")


# ------------------------------------------------------------------ 探测
def probe(path: Path) -> ProbeResult:
    """读页数、判签名、看是否加密。不修改任何东西。"""
    try:
        with pymupdf.open(path) as doc:
            if doc.needs_pass:
                return ProbeResult(0, False, needs_password=True)
            return ProbeResult(doc.page_count, _has_signature(doc), False)
    except Exception as exc:
        raise MergeError(f"无法读取该 PDF：{exc}") from exc


def _has_signature(doc) -> bool:
    try:
        if (flags := doc.get_sigflags()) is not None and flags > 0:
            return True
    except Exception:
        pass

    try:
        catalog = doc.pdf_catalog()
        # 认证签名（certification signature）—— 文档级、不是表单域级的。
        if doc.xref_get_key(catalog, "Perms")[0] != "null":
            return True
        if _acroform_has_sig(doc, catalog):
            return True
    except Exception:
        pass

    return False


def _acroform_has_sig(doc, catalog) -> bool:
    kind, value = doc.xref_get_key(catalog, "AcroForm")
    if kind == "null":
        return False
    try:
        acro_xref = int(value.split()[0]) if kind == "xref" else doc.pdf_catalog()
    except (ValueError, IndexError):
        return False

    fields = doc.xref_get_key(acro_xref, "Fields")
    if fields[0] == "null":
        return False
    return _walk_fields_for_sig(doc, fields[1], depth=0)


def _walk_fields_for_sig(doc, raw: str, depth: int) -> bool:
    """在 AcroForm 字段树里找 ``/FT /Sig``。字段可以嵌套，所以递归。"""
    if depth > 8:  # 畸形文件防护：环路或超深嵌套
        return False

    for token in raw.replace("[", " ").replace("]", " ").split():
        if not token.endswith(" 0 R") and not token.endswith(" R"):
            continue
        try:
            xref = int(token.split()[0])
            obj = doc.xref_object(xref)
        except Exception:
            continue
        if "/Sig" in obj and "/FT" in obj:
            return True
        # Kids 子字段树
        if "/Kids" in obj:
            kind, value = doc.xref_get_key(xref, "Kids")
            if kind != "null" and _walk_fields_for_sig(doc, value, depth + 1):
                return True
    return False


# ------------------------------------------------------------------ 保外观
def _flatten(src, dpi: int):
    """返回一个「外观已固化」的 Document（调用方负责关闭）。

    先试 ``bake``（保矢量、保文本层），失败则退回光栅化（保底但丢文本层）。
    返回 ``(doc, action)``。
    """
    if _HAS_BAKE:
        try:
            doc = pymupdf.open()
            doc.insert_pdf(src, annots=True)
            doc.bake()
            return doc, "bake"
        except Exception as exc:
            log.warning("bake 失败，退回栅格化：%s", exc)

    return _rasterize(src, dpi), "raster"


def _rasterize(src, dpi: int):
    """整页光栅化重建。

    显式按 ``page.rect`` 建页再贴图，而不是用 ``pix.tobytes("pdf")`` ——
    后者按像素尺寸推断页面大小，会得到尺寸错误的页。这样写页面尺寸精确等于
    原页，且旋转页也正确（``page.rect`` 与 ``get_pixmap`` 都按旋转后的可见
    范围计算，两者天然一致）。
    """
    out = pymupdf.open()
    for page in src:
        rect = page.rect
        pix = page.get_pixmap(dpi=dpi)
        new_page = out.new_page(width=rect.width, height=rect.height)
        new_page.insert_image(rect, pixmap=pix)
    return out


# ------------------------------------------------------------------ 书签
def _bookmark_entries(doc, offset: int) -> list[list]:
    """把 ``doc`` 的书签整体下移一层，并加上 ``offset`` 页偏移。

    ``offset`` 是这份文件在合并结果里的起始页下标。下移一层是为了给每个源
    文件让出顶层位置 —— 几十个文件合并后，用户需要「按文件」这一级导航。
    """
    entries = []
    for item in doc.get_toc():
        level, title, page = item[0], item[1], item[2]
        if page <= 0:  # 无目标的条目，PyMuPDF 用页码 0 表示
            continue
        entries.append([level + 1, title, page + offset])
    return entries


def _normalize_levels(entries: list[list]) -> list[list]:
    """压平跳级的层级。

    ``set_toc`` 会在层级跳跃（1 → 3）时抛 ValueError。真实世界的书签树里跳级
    并不罕见（尤其是被别的工具生成过的文件），而书签是整份写入的 —— 一条坏
    记录会让**全部**书签丢失。宁可层级略有出入，也不要用户拿到一本没有目录的
    合并件。
    """
    out: list[list] = []
    previous = 1
    for level, title, page in entries:
        level = max(1, min(int(level), previous + 1))
        out.append([level, title, page])
        previous = level
    return out


# ------------------------------------------------------------------ 主流程
def merge(
    *,
    sources: Sequence[tuple[Path, str]],
    force_flatten: set[Path],
    out_path: Path,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> MergeReport:
    """按给定顺序合并。

    ``sources`` 是 ``(磁盘路径, 展示用文件名)`` 的序列，顺序即最终页序。
    ``force_flatten`` 里的路径无条件走保外观路径（前端的手动开关）。
    """
    out = pymupdf.open()
    report = MergeReport()
    toc: list[list] = []
    total = len(sources)

    for index, (path, display_name) in enumerate(sources):
        if on_progress:
            on_progress(index, total, display_name)

        try:
            with pymupdf.open(path) as src:
                if src.needs_pass:
                    raise MergeError(
                        f"「{display_name}」已加密，需要密码才能合并。"
                        "请先解除密码保护后重新上传。"
                    )
                if src.page_count == 0:
                    raise MergeError(f"「{display_name}」不含任何页面。")

                start_page = out.page_count
                must_flatten = path in force_flatten or _has_signature(src)

                if must_flatten:
                    flat, action = _flatten(src, config.RASTER_DPI)
                    try:
                        out.insert_pdf(flat)
                    finally:
                        flat.close()
                else:
                    action = "direct"
                    out.insert_pdf(src, annots=True)

                # 每个源文件一个顶层书签，其内部结构整体挂在它下面。
                toc.append([1, display_name, start_page + 1])
                toc.extend(_bookmark_entries(src, start_page))

                report.actions.append(action)
                report.page_counts.append(src.page_count)

        except MergeError:
            raise
        except Exception as exc:
            raise MergeError(f"「{display_name}」处理失败：{exc}") from exc

    if out.page_count == 0:
        raise MergeError("合并结果为空，请检查上传的文件。")

    try:
        out.set_toc(_normalize_levels(toc))
    except Exception as exc:  # 书签失败不该让整次合并作废
        log.warning("写入书签失败，继续输出：%s", exc)

    # 元数据保持中性：不写服务器路径、不写时间戳以外的环境信息。
    out.set_metadata(
        {
            "title": out_path.stem,
            "producer": "PDF Merge Tool",
            "creator": "PDF Merge Tool",
        }
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(
        str(out_path),
        garbage=4,  # 清掉被删注释留下的孤儿对象，否则 bake 过的文件会白白变大
        deflate=True,
        clean=True,
    )
    out.close()

    if on_progress:
        on_progress(total, total, "")

    return report


def render_thumb(path: Path, width: int) -> bytes:
    """首页缩略图的 PNG 字节。"""
    with pymupdf.open(path) as doc:
        if doc.needs_pass or doc.page_count == 0:
            raise MergeError("无法生成预览图。")
        page = doc[0]
        zoom = width / page.rect.width if page.rect.width else 1.0
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return pix.tobytes("png")
