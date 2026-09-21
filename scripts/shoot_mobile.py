"""一次性脚本：在移动端视口下截图，用于人工核对响应式表现。

不属于交付物 —— 依赖 playwright，而它不在 requirements.txt 里。

用法：
    <装有 playwright 的 python> scripts/shoot_mobile.py <base_url> <fixture_dir> <out_dir>
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:51229"
FIX = Path(sys.argv[2] if len(sys.argv) > 2 else ".")
OUT = Path(sys.argv[3] if len(sys.argv) > 3 else ".")
OUT.mkdir(parents=True, exist_ok=True)

FILES = [
    FIX / "合同正本.pdf",
    FIX / "附件一（带签名）.pdf",
    FIX / "附录A.pdf",
    FIX / "补充协议.pdf",
    FIX / "报价单.pdf",
]

# 覆盖从小屏手机到大屏手机/小平板
VIEWPORTS = [
    ("iphone-se", 320, 568),  # 最窄仍在用的机型，尺寸最紧张
    ("iphone-14", 390, 844),  # 主流尺寸
    ("pixel-wide", 412, 915),
]

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")

    for name, w, h in VIEWPORTS:
        # is_mobile + has_touch 才能真正复现触屏行为：没有 :hover，
        # 任何「悬停才出现」的控件在这里都必须证明自己是可见的。
        ctx = browser.new_context(
            viewport={"width": w, "height": h},
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            color_scheme="light",
        )
        page = ctx.new_page()
        page.goto(BASE)
        page.wait_for_timeout(700)
        page.screenshot(path=str(OUT / f"{name}-a-empty.png"), full_page=True)
        print(f"  {name}-a-empty.png")

        page.set_input_files("input[type=file]", [str(f) for f in FILES])
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / f"{name}-b-list.png"), full_page=True)
        print(f"  {name}-b-list.png")

        # 触屏上没有 hover，这里截图看「移除」按钮是否可见
        if name == "iphone-14":
            page.screenshot(path=str(OUT / f"{name}-c-nohover.png"), full_page=True)
            # 合并 + 结果
            page.tap("button.primary")
            page.wait_for_selector(".result", timeout=120_000)
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / f"{name}-d-done.png"), full_page=True)
            print(f"  {name}-c-nohover.png / -d-done.png")

        ctx.close()

    browser.close()

print("done")
