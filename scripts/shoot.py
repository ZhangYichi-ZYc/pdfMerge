"""一次性脚本：驱动真实浏览器截图，用于人工核对视觉效果。

不属于交付物的一部分 —— 它依赖 playwright，而 playwright 不在 requirements.txt
里。放在这里是为了让「界面长什么样」这件事可复现：改完样式后重跑一遍就能看到
真实渲染结果，而不是靠想象。

用法：
    <装有 playwright 的 python> scripts/shoot.py <base_url> <out_dir>
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8792"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else ".")
OUT.mkdir(parents=True, exist_ok=True)


def shoot(page, name):
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    print(f"  wrote {name}.png")


with sync_playwright() as p:
    # 用系统已装的 Chrome，不下载 playwright 自带的浏览器（省 150MB）。
    browser = p.chromium.launch(channel="chrome")

    for scheme in ("light", "dark"):
        ctx = browser.new_context(
            viewport={"width": 1180, "height": 900},
            device_scale_factor=2,
            color_scheme=scheme,
        )
        page = ctx.new_page()

        # ---- 空状态 ----
        page.goto(BASE)
        page.wait_for_timeout(600)
        shoot(page, f"empty-{scheme}")

        # ---- 拖入文件 ----
        page.set_input_files(
            "input[type=file]",
            [
                str(OUT / "_fixtures" / "合同正本.pdf"),
                str(OUT / "_fixtures" / "附件一（带签名）.pdf"),
                str(OUT / "_fixtures" / "附录A.pdf"),
                str(OUT / "_fixtures" / "补充协议.pdf"),
                str(OUT / "_fixtures" / "报价单.pdf"),
            ],
        )
        page.wait_for_timeout(2500)
        shoot(page, f"list-{scheme}")

        # ---- 悬停某一行，露出移除按钮 ----
        page.hover(".row:nth-child(3)")
        page.wait_for_timeout(400)
        shoot(page, f"hover-{scheme}")

        # ---- 合并过程与结果 ----
        if scheme == "light":
            page.click("button.primary")
            page.wait_for_timeout(700)
            page.screenshot(path=str(OUT / "during-light.png"), full_page=True)
            print("  wrote during-light.png")
            page.wait_for_selector(".result", timeout=120_000)
            page.wait_for_timeout(500)
            shoot(page, "done-light")

        ctx.close()

    browser.close()

print("done")
