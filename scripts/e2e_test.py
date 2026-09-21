"""端到端测试 —— 真起一个服务，用真的 HTTP 请求走完整流程。

冒烟测试覆盖的是合并算法本身（``pdfcore``）；这个脚本覆盖它之上的**集成**：
HTTP 契约、留档落盘、审计入库、并发与清理。两者缺一不可。

它还专门检查一条硬约束：**任何响应体都不许泄漏留档的存在**。这条约束没法靠
「写代码时小心」来保证 —— 只能靠每次改完都拿真实的响应体检一遍。

运行：
    <python> scripts/e2e_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
PORT = int(os.environ.get("PDFMERGE_TEST_PORT", "8791"))
BASE = f"http://127.0.0.1:{PORT}"

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
            for line in str(detail).splitlines():
                print(f"        {line}")


def section(title: str) -> None:
    print(f"\n{title}")


# ------------------------------------------------------------------ HTTP
def request(method: str, path: str, *, data=None, headers=None, timeout=60):
    req = urllib.request.Request(BASE + path, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, res.read(), _headers(res.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), _headers(exc.headers)


def _headers(raw) -> dict:
    """HTTP 头名大小写不敏感，统一转小写再查 —— 否则一个 'Content-Type'
    和 'content-type' 的差别就能让测试谎报失败。"""
    return {k.lower(): v for k, v in raw.items()}


def header(headers: dict, name: str) -> str:
    return headers.get(name.lower(), "")


def post_json(path: str, payload: dict):
    body = json.dumps(payload).encode()
    return request(
        "POST", path, data=body, headers={"Content-Type": "application/json"}
    )


def upload(path: str, files: list[tuple[str, bytes]]):
    boundary = "----pdfmerge" + uuid.uuid4().hex
    buf = bytearray()
    for name, blob in files:
        # 中文文件名按 UTF-8 直接写进 header —— 这正是浏览器发出来的样子。
        buf += f"--{boundary}\r\n".encode()
        buf += (
            f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'
        ).encode("utf-8")
        buf += b"Content-Type: application/pdf\r\n\r\n"
        buf += blob
        buf += b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return request(
        "POST",
        path,
        data=bytes(buf),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )


# ------------------------------------------------------------------ 夹具
def make_pdf(pages: int, label: str, *, signed: bool = False) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"{label} PAGE {i + 1}", fontsize=16)
    if signed:
        page = doc[0]
        page.add_rect_annot(pymupdf.Rect(300, 300, 500, 360))
        w = pymupdf.Widget()
        w.field_name = "Sig1"
        w.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
        w.rect = pymupdf.Rect(72, 400, 260, 460)
        try:
            page.add_widget(w)
        except Exception:
            pass
    blob = doc.tobytes()
    doc.close()
    return blob


# ------------------------------------------------------------------ 主流程
def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="pdfmerge_e2e_"))
    storage = work / "storage"
    fixtures = work / "fixtures"
    fixtures.mkdir(parents=True)

    # 用独立的存储根目录，绝不碰项目里的 storage/。
    env = {
        **os.environ,
        "PDFMERGE_STORAGE": str(storage),
        "PDFMERGE_PORT": str(PORT),
        "PYTHONIOENCODING": "utf-8",
    }
    log = open(work / "server.log", "wb")
    server = subprocess.Popen(
        [PYTHON, "-m", "backend"],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    try:
        _wait_for_server(server, work)

        plain = make_pdf(2, "PLAIN")
        signed = make_pdf(1, "SIGNED", signed=True)
        big = make_pdf(3, "THREE")

        # ---------------------------------------------------- 上传
        section("E1  上传建任务（含中文文件名）")
        status, body, _ = upload(
            "/api/jobs",
            [
                ("合同正本.pdf", plain),
                ("附件一.pdf", signed),
                ("附录.pdf", big),
            ],
        )
        check("上传返回 200", status == 200, f"{status} {body[:300]}")
        data = json.loads(body)
        job_id = data.get("job_id")
        check("返回了 job_id", bool(job_id), str(data)[:200])
        check("返回了 3 个文件", len(data["files"]) == 3, str(data.get("files")))

        names = [f["name"] for f in data["files"]]
        check(
            "中文文件名原样回传",
            names == ["合同正本.pdf", "附件一.pdf", "附录.pdf"],
            str(names),
        )
        check("每个文件都有稳定 id", all(f.get("id") for f in data["files"]))
        check(
            "页数被探测出来",
            [f["pages"] for f in data["files"]] == [2, 1, 3],
            str([f["pages"] for f in data["files"]]),
        )
        check(
            "含签名的文件被标记",
            [f["signed"] for f in data["files"]] == [False, True, False],
            str([f["signed"] for f in data["files"]]),
        )

        # ---------------------------------------------------- 泄漏检查
        section("E2  响应体不得泄漏留档的存在（硬约束）")
        forbidden = [
            "archive",
            "archived",
            "retain",
            "storage",
            "归档",
            "存档",
            "留存",
            "保留原件",
            "sha256",
        ]
        blob = body.decode("utf-8", "ignore").lower()
        hits = [w for w in forbidden if w.lower() in blob]
        check("上传响应无泄漏词", not hits, f"命中: {hits}\n{body[:400]}")

        # 轮询响应同样要干净 —— 它会被前端反复请求，是最容易无意中带上
        # 内部字段的地方。
        _, poll_body, _ = request("GET", f"/api/jobs/{job_id}")
        poll_blob = poll_body.decode("utf-8", "ignore").lower()
        poll_hits = [w for w in forbidden if w.lower() in poll_blob]
        check("轮询响应无泄漏词", not poll_hits, f"命中: {poll_hits}\n{poll_body[:400]}")

        # ---------------------------------------------------- 追加与移除
        section("E3  追加文件与移出清单")
        f0, f1, f2 = data["files"]
        status, body, _ = upload(f"/api/jobs/{job_id}/files", [("追加件.pdf", plain)])
        check("追加返回 200", status == 200, f"{status} {body[:200]}")
        data = json.loads(body)
        check("追加后有 4 个文件", len(data["files"]) == 4, str(len(data["files"])))
        check(
            "追加的文件排在最末",
            data["files"][-1]["name"] == "追加件.pdf",
            str([f["name"] for f in data["files"]]),
        )

        extra_id = data["files"][-1]["id"]
        status, body, _ = request("DELETE", f"/api/jobs/{job_id}/files/{extra_id}")
        check("移除返回 200", status == 200, f"{status}")
        data = json.loads(body)
        check("移除后剩 3 个", len(data["files"]) == 3, str(len(data["files"])))
        check(
            "移除的是指定文件",
            "追加件.pdf" not in [f["name"] for f in data["files"]],
            str([f["name"] for f in data["files"]]),
        )

        # ---------------------------------------------------- 合并
        section("E4  提交合并：顺序按稳定 id 生效")
        # 故意倒序提交，验证顺序真的被尊重 —— 如果服务端按上传顺序合并，
        # 这个测试才会失败。
        order = [f2["id"], f1["id"], f0["id"]]
        status, body, _ = post_json(
            f"/api/jobs/{job_id}/merge",
            {
                "order": order,
                "force_flatten": [],
                "output_name": "季度合同汇总",
            },
        )
        check("合并请求返回 202", status == 202, f"{status} {body[:300]}")

        state = _poll(job_id)
        check("任务最终 done", state["status"] == "done", str(state.get("error")))
        check(
            "进度到达总数",
            state["progress"]["done"] == state["progress"]["total"] == 3,
            str(state["progress"]),
        )
        check("输出名被记录", state["output_name"] == "季度合同汇总.pdf", state["output_name"])

        # ---------------------------------------------------- 产物
        section("E5  下载产物并校验内容")
        status, blob, headers = request("GET", f"/api/jobs/{job_id}/download")
        check("下载返回 200", status == 200, str(status))
        check(
            "Content-Type 是 PDF",
            "application/pdf" in header(headers, "content-type"),
            header(headers, "content-type"),
        )
        out = work / "merged.pdf"
        out.write_bytes(blob)
        # 中文文件名会被 Starlette 按 RFC 5987 编成 filename*=utf-8''%E5%AD%A3…
        # 所以要解码后再比对 —— 否则测的是编码方式，不是文件名本身。
        disposition = header(headers, "content-disposition")
        decoded = urllib.parse.unquote(disposition)
        check(
            "下载头部的文件名是用户指定的名字",
            "季度合同汇总.pdf" in decoded,
            f"原始: {disposition}\n解码后: {decoded}",
        )
        check(
            "下载头部不泄漏服务器路径",
            "storage" not in decoded.lower() and "__" not in decoded,
            decoded,
        )
        with pymupdf.open(out) as doc:
            check("产物页数为 6", doc.page_count == 6, str(doc.page_count))
            text = "\n".join(p.get_text() for p in doc)
            # 倒序提交：附录(3页) → 附件一(1页) → 合同正本(2页)
            order_of_labels = [
                lbl
                for lbl in ["THREE", "SIGNED", "PLAIN"]
                if lbl in text
            ]
            check(
                "页序与提交顺序一致（倒序生效）",
                order_of_labels == ["THREE", "SIGNED", "PLAIN"],
                f"出现顺序: {order_of_labels}",
            )
            check("第一个文件是 3 页的附录", text.index("THREE") < text.index("PLAIN"))
            toc = doc.get_toc()
        check(
            "每份文件一个顶层书签",
            len([t for t in toc if t[0] == 1]) == 3,
            str(toc),
        )
        check(
            "顶层书签按提交顺序排列",
            [t[1] for t in toc if t[0] == 1]
            == ["附录.pdf", "附件一.pdf", "合同正本.pdf"],
            str([t[1] for t in toc if t[0] == 1]),
        )

        # ---------------------------------------------------- 缩略图
        section("E6  缩略图")
        status, png, headers = request("GET", f"/api/jobs/{job_id}/files/{f1['id']}/thumb")
        check("缩略图返回 200", status == 200, str(status))
        check("返回的是 PNG", png[:8] == b"\x89PNG\r\n\x1a\n", str(png[:16]))
        check(
            "Content-Type 是 image/png",
            "image/png" in header(headers, "content-type"),
            header(headers, "content-type"),
        )
        status, _, _ = request("GET", f"/api/jobs/{job_id}/files/deadbeef/thumb")
        check("未知文件 id 返回 404", status == 404, str(status))

        # ---------------------------------------------------- 留档（服务端视角）
        section("E7  留档区与审计日志（服务端侧验证）")
        archived = list(storage.glob("archive/*/*/*/*.pdf"))
        check("归档区有 4 个文件（含被移除的那个）", len(archived) == 4, str(len(archived)))
        check(
            "归档文件名含时间戳与 job 短 ID",
            all("__" in p.stem for p in archived),
            str([p.name for p in archived][:3]),
        )
        check(
            "归档文件名保留了原始文件名",
            any("合同正本" in p.name for p in archived),
            str([p.name for p in archived]),
        )

        import sqlite3

        conn = sqlite3.connect(storage / "audit.sqlite3")
        conn.row_factory = sqlite3.Row
        jobs_rows = conn.execute("SELECT * FROM jobs").fetchall()
        files_rows = conn.execute("SELECT * FROM job_files").fetchall()
        conn.close()

        check("审计表有 1 条任务记录", len(jobs_rows) == 1, str(len(jobs_rows)))
        check("审计表有 4 条文件记录", len(files_rows) == 4, str(len(files_rows)))
        row = dict(jobs_rows[0])
        check("审计记录了状态 done", row["status"] == "done", str(row["status"]))
        check("审计记录了处理耗时", row["duration_ms"] is not None, str(row["duration_ms"]))
        check("审计记录了处理路径", bool(row["engine"]), str(row["engine"]))
        print(f"        引擎统计: {row['engine']}")
        check(
            "审计记录了排序位置",
            sorted(r["ordinal"] for r in (dict(x) for x in files_rows) if r["ordinal"] is not None)
            == [0, 1, 2],
            str([dict(x)["ordinal"] for x in files_rows]),
        )
        check(
            "被移除的文件 ordinal 为空（如实记录「传了但没合」）",
            any(dict(x)["ordinal"] is None for x in files_rows),
            str([dict(x)["ordinal"] for x in files_rows]),
        )
        check(
            "每个归档件都有 sha256",
            all(len(dict(x)["sha256"]) == 64 for x in files_rows),
            "存在长度不为 64 的哈希",
        )
        check(
            "被合并的文件记录了处理路径",
            any(dict(x)["action"] for x in files_rows),
            str([dict(x)["action"] for x in files_rows]),
        )

        # ---------------------------------------------------- 拒绝路径
        section("E8  错误路径：非 PDF、未知任务、重复提交")
        status, body, _ = upload(
            "/api/jobs",
            [("假的.pdf", b"this is definitely not a pdf file")],
        )
        check("非 PDF 被拒绝（400）", status == 400, f"{status} {body[:200]}")
        check(
            "错误信息点名了文件",
            "假的.pdf" in body.decode("utf-8", "ignore"),
            body[:200].decode("utf-8", "ignore"),
        )

        status, body, _ = request("GET", "/api/jobs/deadbeefdeadbeef")
        check("未知任务返回 404", status == 404, str(status))

        status, body, _ = post_json(
            f"/api/jobs/{job_id}/merge", {"order": [], "force_flatten": []}
        )
        check("重复提交被拒绝（400）", status == 400, f"{status} {body[:200]}")

        # 顺序必须恰好是全部文件的一个排列
        status2, body2, _ = post_json(
            "/api/jobs/deadbeefdeadbeef/merge", {"order": [f0["id"]], "force_flatten": []}
        )
        check("未知任务提交合并返回 404", status2 == 404, str(status2))

        # ---------------------------------------------------- 前端静态资源
        section("E9  单端口同时提供前端与 API")
        status, page, headers = request("GET", "/")
        check("根路径返回前端页面", status == 200, str(status))
        check(
            "返回的是 HTML",
            b"<div id=\"app\">" in page or b"<html" in page.lower(),
            page[:120].decode("utf-8", "ignore"),
        )
        status, body, _ = request("GET", "/api/limits")
        check("API 仍然可达", status == 200 and b"max_files" in body, str(status))

    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()

    print("\n" + "=" * 68)
    total = _passed + len(_failed)
    if _failed:
        print(f"结果：{_passed}/{total} 通过，{len(_failed)} 条失败")
        for name in _failed:
            print(f"  - {name}")
        print(f"\n服务端日志：{work / 'server.log'}")
        print("=" * 68)
        return 1

    print(f"结果：{total}/{total} 全部通过")
    shutil.rmtree(work, ignore_errors=True)
    print("=" * 68)
    return 0


def _wait_for_server(server: subprocess.Popen, work: Path, timeout: float = 40) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server.poll() is not None:
            sys.stdout.write(
                (work / "server.log").read_text("utf-8", "ignore")[-3000:]
            )
            raise SystemExit("服务启动即退出，见上面的日志。")
        try:
            urllib.request.urlopen(f"{BASE}/api/limits", timeout=1).read()
            return
        except Exception:
            time.sleep(0.3)
    raise SystemExit("服务在超时内没有就绪。")


def _poll(job_id: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, body, _ = request("GET", f"/api/jobs/{job_id}")
        state = json.loads(body)
        if state["status"] in ("done", "failed"):
            return state
        time.sleep(0.3)
    raise SystemExit("任务在超时内没有结束。")


if __name__ == "__main__":
    raise SystemExit(main())
