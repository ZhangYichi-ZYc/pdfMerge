"""冒烟测试 —— 用真实的 PyMuPDF 现场构造夹具，跑完整管线，逐条断言。

为什么是脚本而不是 pytest：这个项目的测试价值不在「覆盖率」，而在**构造出
那几种刁钻的 PDF 并确认管线没吃错**。一个能自己造夹具、跑完整流程、打印断言
的脚本就够用，而且零额外依赖 —— 任何目标机器上都能直接跑。

最重要的一条断言是 **T5：外观保全**。它把源文件的一页和合并结果里对应的
那一页渲染成位图逐像素比较。签名图样「有没有消失」这个用户最关心的问题，
只有这个测试能回答 —— 检查注释数量、检查内容流长度都只是代理指标，像素才是
最终事实。

运行：
    <python> -m scripts.smoke_test
或：
    <python> scripts/smoke_test.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402

from backend import pdfcore  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "_fixtures"

_passed = 0
_failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed.append(name)
        print(f"  FAIL  {name}")
        if detail:
            for line in detail.splitlines():
                print(f"        {line}")


def section(title: str) -> None:
    print(f"\n{title}")


# --------------------------------------------------------------- 夹具构造
def _body(page, label: str) -> None:
    page.insert_text((72, 90), label, fontsize=18)
    page.insert_text((72, 130), "Clause 1. This document is a test fixture.", fontsize=11)
    page.insert_text((72, 150), "Clause 2. Body text exists so the text layer", fontsize=11)
    page.insert_text((72, 170), "can be checked after merging.", fontsize=11)


def make_plain(path: Path, pages: int = 2) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        _body(doc.new_page(), f"PLAIN PAGE {i + 1}")
    doc.save(path)
    doc.close()
    return path


def make_bookmarked(path: Path) -> Path:
    doc = pymupdf.open()
    for i in range(3):
        _body(doc.new_page(), f"CHAPTER PAGE {i + 1}")
    doc.set_toc([[1, "Chapter One", 1], [2, "Section 1.1", 2], [1, "Chapter Two", 3]])
    doc.save(path)
    doc.close()
    return path


def make_signed(path: Path) -> Path:
    """带签名域 + 可见签章注释的文件。

    同时制造「注释型签章」与「真正被签署过的表单域」两种特征，因为检测逻辑
    对它们走的是不同的判据。
    """
    doc = pymupdf.open()
    page = doc.new_page()
    _body(page, "SIGNED CONTRACT")

    # 可见的签章：矩形注释（框）+ 文字注释。用注释而不是 draw_rect，是因为
    # 页面内容流里的图形永远不会丢，测不出「注释有没有被固化」这件事。
    box = page.add_rect_annot(pymupdf.Rect(300, 300, 520, 360))
    box.set_colors(stroke=(0.8, 0.1, 0.1))
    box.set_border(width=2)
    box.update()

    seal = page.add_freetext_annot(
        pymupdf.Rect(310, 315, 510, 350), "SEAL-OF-APPROVAL", fontsize=11
    )
    seal.update()

    # 真正的签名字段：这条才是签名检测该命中的判据。
    widget = pymupdf.Widget()
    widget.field_name = "SignatureField1"
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    widget.rect = pymupdf.Rect(72, 400, 260, 460)
    widget.field_label = "Authorised signatory"
    try:
        page.add_widget(widget)
    except Exception as exc:
        print(f"        (note: 无法创建签名域，检测将依赖 /Perms 判据: {exc})")

    doc.save(path)
    doc.close()
    return path


def make_encrypted(path: Path) -> Path:
    doc = pymupdf.open()
    _body(doc.new_page(), "ENCRYPTED PAGE")
    doc.save(
        path,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
    )
    doc.close()
    return path


def make_owner_only(path: Path) -> Path:
    """只有 owner 密码、user 密码为空 —— 这类文件应当能直接打开。"""
    doc = pymupdf.open()
    _body(doc.new_page(), "OWNER PASSWORD ONLY")
    doc.save(
        path,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="",
    )
    doc.close()
    return path


# --------------------------------------------------------------- 断言辅助
def render(page, dpi: int = 100):
    import numpy as np

    pix = page.get_pixmap(dpi=dpi)
    arr = np.frombuffer(pix.samples, dtype=np.uint8)
    return arr.reshape(pix.height, pix.width, pix.n)


def visual_diff(a, b) -> float:
    """两张位图的平均绝对差，归一化到 0~1。0 = 完全相同。"""
    import numpy as np

    h = min(a.shape[0], b.shape[0])
    w = min(a.shape[1], b.shape[1])
    ca = a[:h, :w, :3].astype(np.int16)
    cb = b[:h, :w, :3].astype(np.int16)
    return float(np.abs(ca - cb).mean() / 255.0)


# --------------------------------------------------------------- 测试
def t1_detection(work: Path) -> dict[str, Path]:
    section("T1  签名检测：plain 不该误报，signed 必须命中")

    plain = make_plain(work / "plain.pdf")
    bookmarked = make_bookmarked(work / "bookmarked.pdf")
    signed = make_signed(work / "signed.pdf")
    encrypted = make_encrypted(work / "encrypted.pdf")
    owner_only = make_owner_only(work / "owner_only.pdf")

    p_plain = pdfcore.probe(plain)
    p_signed = pdfcore.probe(signed)
    p_enc = pdfcore.probe(encrypted)
    p_owner = pdfcore.probe(owner_only)
    p_book = pdfcore.probe(bookmarked)

    check("plain 页数为 2", p_plain.page_count == 2, f"got {p_plain.page_count}")
    check("plain 不误判为含签名", not p_plain.has_signature)
    check("bookmarked 不误判为含签名", not p_book.has_signature)

    print(f"        signed 探测结果: signature={p_signed.has_signature}")
    check("signed 命中签名检测", p_signed.has_signature)

    check("需要密码的文件被识别", p_enc.needs_password)
    check("仅 owner 密码的文件可正常读取", not p_owner.needs_password, str(p_owner))
    check("仅 owner 密码的文件页数为 1", p_owner.page_count == 1)

    return {
        "plain": plain,
        "bookmarked": bookmarked,
        "signed": signed,
        "encrypted": encrypted,
        "owner_only": owner_only,
    }


def t2_direct_path(fx: dict[str, Path], work: Path) -> None:
    section("T2  直通路径：无签名的文件不被降级")

    out = work / "out_direct.pdf"
    report = pdfcore.merge(
        sources=[(fx["plain"], "plain.pdf")],
        force_flatten=set(),
        out_path=out,
    )
    check("走了 direct 路径", report.actions == ["direct"], str(report.actions))
    check("输出文件已生成", out.exists())
    check("输出页数正确", report.page_counts == [2], str(report.page_counts))

    with pymupdf.open(out) as doc:
        check("输出文档页数为 2", doc.page_count == 2)
        text = doc[0].get_text()
    # 直通路径必须保住文本层 —— 这是它相对栅格化的全部价值。
    check("直通路径保住文本层", "Clause 1." in text, repr(text[:120]))


def t3_flatten_path(fx: dict[str, Path], work: Path) -> None:
    section("T3  含签名的文件走保外观路径，且注释被固化")

    out = work / "out_signed.pdf"
    report = pdfcore.merge(
        sources=[(fx["signed"], "signed.pdf")],
        force_flatten=set(),
        out_path=out,
    )
    action = report.actions[0]
    print(f"        处理路径: {action}")
    check("走了保外观路径 (bake 或 raster)", action in ("bake", "raster"), action)

    with pymupdf.open(out) as doc:
        page = doc[0]
        remaining = len(list(page.annots() or []))
        check(
            "注释已从文档中移除（说明已被固化进页面）",
            remaining == 0,
            f"仍有 {remaining} 个注释",
        )
        check("输出页数为 1", doc.page_count == 1)


def t4_force_flatten(fx: dict[str, Path], work: Path) -> None:
    section("T4  手动强制开关：普通文件也能被强制保外观")

    out = work / "out_forced.pdf"
    report = pdfcore.merge(
        sources=[(fx["plain"], "plain.pdf")],
        force_flatten={fx["plain"]},
        out_path=out,
    )
    check(
        "强制开关生效",
        report.actions[0] in ("bake", "raster"),
        str(report.actions),
    )


def t5_appearance_preserved(fx: dict[str, Path], work: Path) -> None:
    section("T5  外观保全（像素级）—— 本项目最重要的一条断言")

    out = work / "out_appearance.pdf"
    report = pdfcore.merge(
        sources=[(fx["signed"], "signed.pdf")],
        force_flatten=set(),
        out_path=out,
    )
    print(f"        处理路径: {report.actions[0]}")

    with pymupdf.open(fx["signed"]) as src, pymupdf.open(out) as dst:
        diff = visual_diff(render(src[0]), render(dst[0]))

    print(f"        平均像素差: {diff:.6f}  (0 = 完全一致)")
    # 阈值 2%：bake 路径应当接近 0；栅格化路径因缩放重采样会有微小差异。
    # 而「签名图样消失」这种情况的差异会远高于此。
    check("合并后页面外观与原文一致", diff < 0.02, f"平均像素差 {diff:.6f}")


def t6_bookmarks(fx: dict[str, Path], work: Path) -> None:
    section("T6  书签：每份文件一个顶层条目，原有结构保留在下一层")

    out = work / "out_toc.pdf"
    pdfcore.merge(
        sources=[(fx["plain"], "第一个文件.pdf"), (fx["bookmarked"], "第二个文件.pdf")],
        force_flatten=set(),
        out_path=out,
    )

    with pymupdf.open(out) as doc:
        toc = doc.get_toc()
        check("输出总页数为 5", doc.page_count == 5, str(doc.page_count))

    print("        书签树:")
    for level, title, page in toc:
        print(f"          {'  ' * (level - 1)}L{level}  {title}  -> 第 {page} 页")

    top = [t for t in toc if t[0] == 1]
    check("顶层书签数 = 源文件数", len(top) == 2, f"got {len(top)}")
    check(
        "顶层书签按文件名命名",
        [t[1] for t in top] == ["第一个文件.pdf", "第二个文件.pdf"],
        str([t[1] for t in top]),
    )
    check("顶层书签指向正确起始页", [t[2] for t in top] == [1, 3], str([t[2] for t in top]))
    check(
        "源文件内部书签被保留下沉",
        any(t[0] == 2 and t[1] == "Chapter One" and t[2] == 3 for t in toc),
        str(toc),
    )


def t7_encrypted(fx: dict[str, Path], work: Path) -> None:
    section("T7  加密文件：必须明确报错，绝不静默跳过")

    out = work / "out_enc.pdf"
    raised: Exception | None = None
    try:
        pdfcore.merge(
            sources=[(fx["encrypted"], "保密合同.pdf")],
            force_flatten=set(),
            out_path=out,
        )
    except Exception as exc:
        raised = exc

    check("加密文件触发了异常", raised is not None, "竟然合并成功了")
    if raised is not None:
        msg = str(raised)
        print(f"        错误信息: {msg}")
        check("错误信息点名了具体文件", "保密合同.pdf" in msg, msg)
        check("错误信息不含服务端路径", "storage" not in msg.lower(), msg)
    check("失败时没有留下半成品输出", not out.exists())

    # 仅 owner 密码的文件应当能正常合并 —— 否则「加密文件一律拒绝」会误伤
    # 一大批其实能正常打开的 PDF。
    out2 = work / "out_owner_only.pdf"
    report = pdfcore.merge(
        sources=[(fx["owner_only"], "仅owner密码.pdf")],
        force_flatten=set(),
        out_path=out2,
    )
    check("仅 owner 密码的文件可以正常合并", out2.exists() and report.page_counts == [1])
    with pymupdf.open(out2) as doc:
        check(
            "仅 owner 密码的文件内容可读",
            "OWNER PASSWORD ONLY" in doc[0].get_text(),
        )


def t8_page_sizes(work: Path) -> None:
    section("T8  混合页面尺寸：不归一化，各页保持原样")

    a4 = pymupdf.open()
    a4.new_page(width=595, height=842)
    a4_path = work / "a4.pdf"
    a4.save(a4_path)
    a4.close()

    letter = pymupdf.open()
    letter.new_page(width=612, height=792)
    letter_path = work / "letter.pdf"
    letter.save(letter_path)
    letter.close()

    # 一个旋转 90 度的页面，确认 insert_pdf 不会把它掰回去。
    rotated = pymupdf.open()
    p = rotated.new_page(width=595, height=842)
    p.set_rotation(90)
    rotated_path = work / "rotated.pdf"
    rotated.save(rotated_path)
    rotated.close()

    out = work / "out_sizes.pdf"
    pdfcore.merge(
        sources=[(a4_path, "a4.pdf"), (letter_path, "letter.pdf"), (rotated_path, "r.pdf")],
        force_flatten=set(),
        out_path=out,
    )

    with pymupdf.open(out) as doc:
        sizes = [(round(p.rect.width), round(p.rect.height)) for p in doc]
        rotations = [p.rotation for p in doc]

    print(f"        各页尺寸: {sizes}")
    print(f"        各页旋转: {rotations}")
    check("A4 尺寸保持不变", sizes[0] == (595, 842), str(sizes))
    check("Letter 尺寸保持不变", sizes[1] == (612, 792), str(sizes))
    check("旋转页的可见尺寸被正确保留", sizes[2] == (842, 595), str(sizes))


def t9_multi_signature_mixed(fx: dict[str, Path], work: Path) -> None:
    section("T9  混合批次：三种路径在同一个 job 里共存")

    out = work / "out_mixed.pdf"
    report = pdfcore.merge(
        sources=[
            (fx["plain"], "普通.pdf"),
            (fx["signed"], "带签名.pdf"),
            (fx["bookmarked"], "带书签.pdf"),
            (fx["owner_only"], "仅owner.pdf"),
        ],
        force_flatten=set(),
        out_path=out,
    )

    print(f"        各文件路径: {report.actions}")
    print(f"        各文件页数: {report.page_counts}")
    check("普通文件走 direct", report.actions[0] == "direct", str(report.actions))
    check("带签名文件走保外观路径", report.actions[1] in ("bake", "raster"))
    check("带书签文件走 direct", report.actions[2] == "direct", str(report.actions))
    # 2(plain) + 1(signed) + 3(bookmarked) + 1(owner_only) = 7
    check(
        "总页数等于各文件页数之和",
        sum(report.page_counts) == 7,
        str(report.page_counts),
    )

    with pymupdf.open(out) as doc:
        check("输出文档页数正确", doc.page_count == 7, str(doc.page_count))
        check("书签数量合理", len(doc.get_toc()) >= 4, str(len(doc.get_toc())))
        # 合并结果的元数据里不该出现服务端路径
        meta = doc.metadata
        blob = " ".join(str(v) for v in meta.values()).lower()
        check("输出元数据不含服务端路径痕迹", "storage" not in blob, blob)

    check("产出文件体积合理", out.stat().st_size > 1000)


def t10_progress(fx: dict[str, Path], work: Path) -> None:
    section("T10  进度回调：前端轮询拿到的进度必须真实")

    seen: list[tuple[int, int, str]] = []
    out = work / "out_progress.pdf"
    pdfcore.merge(
        sources=[(fx["plain"], "a.pdf"), (fx["bookmarked"], "b.pdf"), (fx["signed"], "c.pdf")],
        force_flatten=set(),
        out_path=out,
        on_progress=lambda d, t, n: seen.append((d, t, n)),
    )

    print(f"        回调序列: {seen}")
    check("进度回调被调用", len(seen) > 0)
    check("总数为 3", all(t == 3 for _, t, _ in seen), str(seen))
    check("起始下标为 0", seen[0][0] == 0, str(seen))
    check("最终进度到达总数", seen[-1][0] == 3, str(seen[-1]))


# --------------------------------------------------------------- 入口
def main() -> int:
    import numpy  # noqa: F401  提前导入，缺了就在开头报错而不是测到一半

    print("=" * 68)
    print("PDF 合并管线冒烟测试")
    print(f"PyMuPDF {pymupdf.__version__}  |  bake 可用: {pdfcore._HAS_BAKE}")
    print("=" * 68)

    if FIXTURES.exists():
        shutil.rmtree(FIXTURES)
    FIXTURES.mkdir(parents=True)

    try:
        fx = t1_detection(FIXTURES)
        t2_direct_path(fx, FIXTURES)
        t3_flatten_path(fx, FIXTURES)
        t4_force_flatten(fx, FIXTURES)
        t5_appearance_preserved(fx, FIXTURES)
        t6_bookmarks(fx, FIXTURES)
        t7_encrypted(fx, FIXTURES)
        t8_page_sizes(FIXTURES)
        t9_multi_signature_mixed(fx, FIXTURES)
        t10_progress(fx, FIXTURES)
    except Exception:
        print("\n\n测试执行中断：")
        traceback.print_exc()
        return 2

    print("\n" + "=" * 68)
    total = _passed + len(_failed)
    if _failed:
        print(f"结果：{_passed}/{total} 通过，{len(_failed)} 条失败")
        for name in _failed:
            print(f"  - {name}")
        print("=" * 68)
        return 1

    print(f"结果：{total}/{total} 全部通过")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
