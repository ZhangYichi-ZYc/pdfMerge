"""一次性诊断：在移动端视口下量出真实的布局问题，而不是靠肉眼看截图。

不属于交付物。用法：
    <装有 playwright 的 python> scripts/probe_mobile.py <base_url> <fixture_dir>
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:51229"
FIX = Path(sys.argv[2] if len(sys.argv) > 2 else ".")
FILES = [FIX / n for n in (
    "合同正本.pdf", "附件一（带签名）.pdf", "附录A.pdf", "补充协议.pdf", "报价单.pdf",
)]

PROBE = """
() => {
  const out = {problems: [], report: {}};

  // 1. 整页横向溢出
  const de = document.documentElement;
  if (de.scrollWidth > window.innerWidth + 1) {
    out.problems.push(`横向溢出：scrollWidth=${de.scrollWidth} > innerWidth=${window.innerWidth}`);
    // 找出是谁溢出的
    for (const el of document.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (r.right > window.innerWidth + 1 || r.left < -1) {
        out.problems.push(`  溢出元素: ${el.tagName}.${el.className||''} right=${r.right.toFixed(0)} left=${r.left.toFixed(0)}`);
      }
    }
  }

  // 2. 清单行：行高是否失控、行尾控件是否与文件名同处一条视觉线
  const rows = [...document.querySelectorAll('.row')];
  out.report.rowCount = rows.length;
  if (rows.length) {
    const first = rows[0];
    const rowRect = first.getBoundingClientRect();
    const byClass = {};
    for (const k of first.children) {
      const r = k.getBoundingClientRect();
      const key = (k.className || k.tagName).split(' ')[0];
      if (r.width > 0) byClass[key] = {top: Math.round(r.top), bottom: Math.round(r.bottom),
                                       left: Math.round(r.left), w: Math.round(r.width),
                                       h: Math.round(r.height)};
    }
    out.report.firstRowChildren = Object.entries(byClass).map(([cls, b]) => ({cls, ...b}));
    out.report.rowHeight = Math.round(rowRect.height);
    out.report.rowTop = Math.round(rowRect.top);

    // 行高不该超过缩略图高度 + 上下内边距太多（超出说明有东西掉进了隐式行）
    const thumbH = byClass.thumb ? byClass.thumb.h : 0;
    if (thumbH && rowRect.height > thumbH + 30)
      out.problems.push(`行高失控: ${Math.round(rowRect.height)}px（缩略图才 ${thumbH}px）—— 有元素落进了隐式行`);
    // 行尾控件必须和文件名在同一条视觉线上
    if (byClass.name && byClass['cell-end'] &&
        Math.abs(byClass.name.top - byClass['cell-end'].top) > 20)
      out.problems.push(`行尾控件错位: name.top=${byClass.name.top} 但 cell-end.top=${byClass['cell-end'].top}`);
    // 所有子元素都必须落在行的矩形内
    for (const [cls, b] of Object.entries(byClass)) {
      if (b.bottom > Math.round(rowRect.bottom) + 1 || b.top < Math.round(rowRect.top) - 1)
        out.problems.push(`子元素越出行边界: ${cls} top=${b.top} bottom=${b.bottom} 行=[${Math.round(rowRect.top)},${Math.round(rowRect.bottom)}]`);
    }
  }

  // 3. 触屏上没有 hover：移除按钮是否可见/可点
  const drop = document.querySelector('.drop-file');
  if (drop) {
    const cs = getComputedStyle(drop);
    const r = drop.getBoundingClientRect();
    out.report.removeBtn = {opacity: cs.opacity, w: Math.round(r.width), h: Math.round(r.height)};
    if (parseFloat(cs.opacity) < 0.5)
      out.problems.push(`移除按钮在触屏上不可见：opacity=${cs.opacity}`);
    if (r.width < 32 || r.height < 32)
      out.problems.push(`移除按钮触控目标过小：${Math.round(r.width)}x${Math.round(r.height)}`);
  } else {
    out.problems.push('页面上找不到 .drop-file 移除按钮（可能只在 hover 时挂载）');
  }

  // 4. 可点目标尺寸。伪元素撑出来的触控区域量不到，用 ::after 的 inset 补正。
  out.report.tapTargets = [];
  for (const el of document.querySelectorAll('.linkish, .handle, .drop-file, .check, button.primary')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0) continue;
    let h = r.height, w = r.width;
    const after = getComputedStyle(el, '::after');
    if (after && after.inset && after.inset !== 'auto') {
      const parts = after.inset.split(' ').map(v => parseFloat(v) || 0);
      const dy = (parts.length >= 3 ? parts[0] : parts[0]) + (parts.length >= 3 ? parts[2] : parts[0]);
      const dx = (parts.length >= 2 ? parts[1] : parts[0]) + (parts.length >= 4 ? parts[3] : parts[1]);
      h += Math.abs(dy); w += Math.abs(dx);
    }
    const label = (el.textContent || '').trim().slice(0, 12) || el.className;
    out.report.tapTargets.push(`${label}=${Math.round(w)}x${Math.round(h)}`);
    if (h < 32) out.problems.push(`可点目标高度不足: ${el.className} "${label}" = ${Math.round(w)}x${Math.round(h)}`);
  }

  // 5. 文字被截断（省略号）的元素
  for (const el of document.querySelectorAll('.name, .metric, .mark, h1')) {
    if (el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0)
      out.problems.push(`文字被截断: ${el.className} "${(el.textContent||'').trim().slice(0,20)}" scroll=${el.scrollWidth} client=${el.clientWidth}`);
  }

  // 6. 投放区高度（手机上不该占满屏）
  const dropEl = document.querySelector('.drop');
  if (dropEl) out.report.dropHeight = Math.round(dropEl.getBoundingClientRect().height);

  // 7. 关键控件的尺寸
  for (const sel of ['button.primary', '.input-wrap']) {
    const el = document.querySelector(sel);
    if (el) { const r = el.getBoundingClientRect();
      out.report[sel] = `${Math.round(r.width)}x${Math.round(r.height)}`; }
  }

  out.report.viewport = `${window.innerWidth}x${window.innerHeight}`;
  return out;
}
"""

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    for name, w, h in [("iPhoneSE", 320, 568), ("iPhone14", 390, 844)]:
        ctx = browser.new_context(
            viewport={"width": w, "height": h}, device_scale_factor=2,
            is_mobile=True, has_touch=True, color_scheme="light",
        )
        page = ctx.new_page()
        print(f"\n{'='*66}\n{name}  {w}x{h}\n{'='*66}")

        page.goto(BASE)
        page.wait_for_timeout(500)
        r = page.evaluate(PROBE)
        print(f"  [空状态] viewport={r['report'].get('viewport')} 投放区高度={r['report'].get('dropHeight')}px")
        for pb in r["problems"]:
            print(f"  ! {pb}")
        if not r["problems"]:
            print("  (无问题)")

        page.set_input_files("input[type=file]", [str(f) for f in FILES])
        page.wait_for_timeout(2500)
        r = page.evaluate(PROBE)
        print(f"\n  [文件清单] 行数={r['report'].get('rowCount')} 行高={r['report'].get('rowHeight')}px")
        print(f"  首行子元素位置:")
        for b in r["report"].get("firstRowChildren", []):
            print(f"    {b['cls']:<14} top={b['top']:>4} left={b['left']:>4} w={b['w']:>4} h={b['h']:>3}")
        if r["report"].get("removeBtn"):
            print(f"  移除按钮: {r['report']['removeBtn']}")
        for k in ("button.primary", ".input-wrap"):
            if r["report"].get(k):
                print(f"  {k}: {r['report'][k]}")
        print(f"  可点目标: {r['report'].get('tapTargets')}")
        print("  问题:")
        for pb in r["problems"]:
            print(f"  ! {pb}")
        if not r["problems"]:
            print("  (无问题)")

        ctx.close()
    browser.close()
