#!/usr/bin/env python3
"""把证据图渲染成一个可缩放、可搜索、点节点看全文的单文件 HTML。

为什么要有这个：graphviz 出的 SVG 在 Typora / Notion 这类按文档宽度排版的工具里
会被等比缩到看不清，而这些工具不提供对单张图的缩放；图上的标签又被截到 40 字，
判据、全部证据、状态历程都看不到。这里把 SVG 连同节点全文一起内联进一张自带
平移缩放的页面，浏览器直接打开即可，不联网、不依赖任何插件。

边界：只读 YAML，不改图。全图视图固定折叠已证伪节点（点那个折叠块可列出它们），
主线视图等价于 render --focus。
"""
import argparse
import html
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("eg_graph", os.path.join(_HERE, "graph.py"))
graph = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(graph)


def _svg(data: dict, focus: bool, collapse: bool, rankdir: str) -> str:
    """出一张 SVG 并剥掉文档头，只留 <svg> 元素本身，便于内联。"""
    dot = graph.render_dot(data, focus, collapse, rankdir)
    with tempfile.NamedTemporaryFile("w", suffix=".dot", delete=False, encoding="utf-8") as fh:
        fh.write(dot)
        src = fh.name
    try:
        out = subprocess.run(["dot", "-Tsvg", src], check=True, capture_output=True, text=True).stdout
    finally:
        os.unlink(src)
    out = out[out.index("<svg"):]
    # graphviz 写的是 pt，1pt 正好对应一个 viewBox 单位，这里换成同数值的 px。
    # 不能整个删掉：内联 SVG 没有 width/height 会退化成 width:100%，而父容器是
    # position:absolute 的自适应宽度，两边互相要尺寸，整张图会塌成 0 宽不显示。
    vb = re.search(r'viewBox="[-0-9.]+ [-0-9.]+ ([0-9.]+) ([0-9.]+)"', out)
    if not vb:
        raise graph.GraphError("SVG 里没找到 viewBox，dot 版本可能变了")
    return re.sub(r'\s(?:width|height)="[0-9.]+pt"', "", out, count=2).replace(
        "<svg", f'<svg width="{vb.group(1)}" height="{vb.group(2)}"', 1)


def _payload(data: dict) -> dict:
    """页面要用的全量数据：节点全文、反向依赖、已证伪清单、统计。

    图上只放截断标签，全文一律从这里取，保证点开看到的和 YAML 里一模一样。
    """
    nodes = data.get("nodes", {}) or {}
    rev: dict[str, list[str]] = {k: [] for k in nodes}
    for k, n in nodes.items():
        for d in n.get("depends_on") or []:
            if d in rev:
                rev[d].append(k)
    out = {}
    for k, n in nodes.items():
        out[k] = {
            "kind": graph.KIND_ZH.get(n.get("kind", ""), n.get("kind", "")),
            "status": n.get("status", ""),
            "statusZh": graph.STATUS_ZH.get(n.get("status", ""), n.get("status", "")),
            "claim": n.get("claim", ""),
            "criterion": n.get("criterion", ""),
            "dependsOn": list(n.get("depends_on") or []),
            "dependedBy": rev.get(k, []),
            "evidence": list(n.get("evidence") or []),
            "needs": n.get("needs", ""),
            "history": [{"at": h.get("at", ""), "status": graph.STATUS_ZH.get(h.get("status", ""), h.get("status", "")),
                         "note": h.get("note", "")} for h in (n.get("history") or [])],
        }
    tally: dict[str, int] = {}
    for n in nodes.values():
        z = graph.STATUS_ZH.get(n.get("status", ""), "未知")
        tally[z] = tally.get(z, 0) + 1
    return {
        "goal": data.get("goal", ""),
        "mainLine": data.get("main_line"),
        "created": data.get("created", ""),
        "total": len(nodes),
        "tally": tally,
        "refuted": [k for k, n in nodes.items() if n.get("status") == "refuted"],
        "nodes": out,
    }


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  :root { --bar: #f5f5f5; --line: #ddd; --ink: #222; --dim: #777; --sel: #06c; }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; font-family: "PingFang SC", -apple-system, sans-serif; color: var(--ink); }
  body { display: flex; flex-direction: column; overflow: hidden; }
  #bar { flex: 0 0 auto; display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
         padding: 8px 12px; background: var(--bar); border-bottom: 1px solid var(--line); font-size: 13px; }
  #bar .sep { width: 1px; height: 20px; background: var(--line); }
  button { font: inherit; padding: 4px 10px; border: 1px solid var(--line); background: #fff;
           border-radius: 4px; cursor: pointer; }
  button:hover { background: #ececec; }
  button.on { background: var(--ink); color: #fff; border-color: var(--ink); }
  #q { font: inherit; padding: 4px 8px; border: 1px solid var(--line); border-radius: 4px; width: 200px; }
  #pct { min-width: 52px; text-align: center; color: var(--dim); font-variant-numeric: tabular-nums; }
  #hits { color: var(--dim); min-width: 74px; }
  .legend { display: flex; align-items: center; gap: 6px; color: var(--dim); }
  .legend i { width: 12px; height: 12px; border: 1px solid #999; display: inline-block; }
  #main { flex: 1 1 auto; display: flex; min-height: 0; }
  #stage { flex: 1 1 auto; position: relative; overflow: hidden; background: #fff; cursor: grab; min-width: 0; }
  #stage.drag { cursor: grabbing; }
  #pan { position: absolute; top: 0; left: 0; transform-origin: 0 0; }
  #pan svg { display: block; }
  .view { display: none; }
  .view.on { display: block; }
  g.node { cursor: pointer; }
  /* 搜索命中描红、未命中连边一起压暗；选中的节点描蓝，两者可叠加 */
  g.node.miss { opacity: .18; }
  .view.searching g.edge { opacity: .18; }
  g.node.hit > path, g.node.hit > polygon, g.node.hit > ellipse { stroke: #d33 !important; stroke-width: 3 !important; }
  g.node.sel > path, g.node.sel > polygon, g.node.sel > ellipse { stroke: var(--sel) !important; stroke-width: 3.5 !important; }
  #tip { position: absolute; right: 10px; bottom: 8px; color: var(--dim); font-size: 12px;
         background: rgba(255,255,255,.85); padding: 2px 8px; border-radius: 4px; }
  /* 详情抽屉：盖在画布上，不改变画布尺寸，省得每次开关都要重算缩放 */
  #panel { flex: 0 0 440px; max-width: 50%; position: relative;
           background: #fff; border-left: 1px solid var(--line);
           overflow-y: auto; padding: 14px 16px 28px; display: none; font-size: 13px; line-height: 1.7; }
  #panel.on { display: block; }
  #panel h2 { margin: 0 0 4px; font-size: 17px; display: flex; align-items: center; gap: 8px; }
  #panel .badge { font-size: 12px; font-weight: 400; padding: 1px 8px; border-radius: 10px; border: 1px solid #ccc; }
  #panel .kind { color: var(--dim); font-size: 12px; font-weight: 400; }
  #panel h3 { margin: 16px 0 4px; font-size: 12px; color: var(--dim); font-weight: 600; letter-spacing: .04em; }
  #panel p { margin: 0; }
  #panel ul { margin: 0; padding-left: 18px; }
  #panel li { margin-bottom: 4px; word-break: break-word; }
  #panel code, #panel .mono { font-family: "SF Mono", Menlo, monospace; font-size: 12px; }
  #panel .chip { display: inline-block; margin: 2px 4px 2px 0; padding: 1px 8px; border: 1px solid var(--line);
                 border-radius: 10px; cursor: pointer; background: #fafafa; }
  #panel .chip:hover { background: #ececec; border-color: #bbb; }
  #panel .hist { color: var(--dim); font-size: 12px; }
  #panel .none { color: var(--dim); }
  #close { position: absolute; top: 10px; right: 12px; border: none; background: none; font-size: 20px;
           line-height: 1; color: var(--dim); padding: 2px 6px; }
</style>
</head>
<body>
<div id="bar">
  <button id="v-full" class="on">全图（__N_FULL__ 节点）</button>
  <button id="v-focus">主线 __MAIN__（__N_FOCUS__ 节点）</button>
  <span class="sep"></span>
  <input id="q" type="search" placeholder="搜节点：F2 / 连接池 / DNS">
  <span id="hits"></span>
  <span class="sep"></span>
  <button id="out">−</button><span id="pct">100%</span><button id="in">＋</button>
  <button id="fit">适应窗口</button><button id="one">1:1</button>
  <span class="sep"></span>
  <span class="legend"><i style="background:#d8f0d8"></i>已证实<i style="background:#fff4cc"></i>待查
    <i style="background:#f0d8d8"></i>已证伪<i style="background:#e6e6e6"></i>受阻<i style="background:#d8e6f0"></i>已执行</span>
</div>
<div id="main">
  <div id="stage">
    <div id="pan">
      <div class="view on" id="view-full">__SVG_FULL__</div>
      <div class="view" id="view-focus">__SVG_FOCUS__</div>
    </div>
    <div id="tip">点节点看全文；滚轮缩放，拖拽平移，双击放大；+ − 0 / 快捷键</div>
  </div>
  <aside id="panel"><button id="close" title="关闭（Esc）">×</button><div id="body"></div></aside>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
(function () {
  var DATA = JSON.parse(document.getElementById('data').textContent);
  var stage = document.getElementById('stage'), pan = document.getElementById('pan');
  var panel = document.getElementById('panel'), pbody = document.getElementById('body');
  var k = 1, tx = 0, ty = 0;                       // 当前缩放与平移
  var MIN = 0.05, MAX = 12;

  function apply() {
    pan.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + k + ')';
    document.getElementById('pct').textContent = Math.round(k * 100) + '%';
  }
  function active() { return document.querySelector('.view.on > svg'); }
  // 取当前视图 SVG 的固有尺寸：viewBox 与 px 宽高同值，读哪个都一样
  function box() {
    var vb = active().getAttribute('viewBox').split(/[ ,]+/);
    return { w: parseFloat(vb[2]), h: parseFloat(vb[3]) };
  }
  function fit() {
    var b = box(), r = stage.getBoundingClientRect(), pad = 24;
    k = Math.max(MIN, Math.min(MAX, Math.min((r.width - pad) / b.w, (r.height - pad) / b.h)));
    tx = (r.width - b.w * k) / 2; ty = (r.height - b.h * k) / 2;
    apply();
  }
  // 以某个屏幕点为锚缩放，保证光标底下的内容不跑
  function zoomAt(px, py, factor) {
    var k2 = Math.max(MIN, Math.min(MAX, k * factor));
    if (k2 === k) return;
    tx = px - (px - tx) * (k2 / k); ty = py - (py - ty) * (k2 / k);
    k = k2; apply();
  }
  function zoomCenter(f) { var r = stage.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, f); }

  stage.addEventListener('wheel', function (e) {
    e.preventDefault();
    var r = stage.getBoundingClientRect();
    // 触控板双指缩放带 ctrlKey，步长要小得多，否则一下冲到头
    var step = e.ctrlKey ? 0.01 : 0.0022;
    zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * step));
  }, { passive: false });

  // 拖拽平移。按下到抬起位移小于 5px 视为点击，交给详情面板，避免拖一下就弹窗。
  // 两处要小心：一是命中的节点必须在 pointerdown 时就记下来——等到 pointerup 再
  // closest 可能已经被指针捕获重定向到 stage，取不到节点；二是指针捕获只在确认
  // 是拖拽之后才抢，纯点击全程不捕获，免得把 target 改掉。
  var down = null, moved = 0, hitNode = null, captured = false;
  function endGesture(e) {
    if (!down) return;
    var wasDrag = moved > 5, g = hitNode;
    down = null; hitNode = null; stage.classList.remove('drag');
    if (captured) {
      try { stage.releasePointerCapture(e.pointerId); } catch (err) { /* 已经自动释放 */ }
      captured = false;
    }
    if (wasDrag || !g) return;
    open(g.querySelector('title').textContent.trim());
  }
  stage.addEventListener('pointerdown', function (e) {
    hitNode = e.target.closest ? e.target.closest('g.node') : null;
    down = { x: e.clientX - tx, y: e.clientY - ty, sx: e.clientX, sy: e.clientY };
    moved = 0; captured = false;
  });
  stage.addEventListener('pointermove', function (e) {
    if (!down) return;
    moved = Math.max(moved, Math.abs(e.clientX - down.sx) + Math.abs(e.clientY - down.sy));
    if (!captured && moved > 5) {
      stage.classList.add('drag');
      try { stage.setPointerCapture(e.pointerId); captured = true; } catch (err) { /* 捕获失败不影响平移 */ }
    }
    tx = e.clientX - down.x; ty = e.clientY - down.y; apply();
  });
  stage.addEventListener('pointerup', endGesture);
  stage.addEventListener('pointercancel', endGesture);
  stage.addEventListener('dblclick', function (e) {
    var r = stage.getBoundingClientRect();
    zoomAt(e.clientX - r.left, e.clientY - r.top, 1.8);
  });

  document.getElementById('in').onclick = function () { zoomCenter(1.25); };
  document.getElementById('out').onclick = function () { zoomCenter(0.8); };
  document.getElementById('fit').onclick = fit;
  document.getElementById('one').onclick = function () { k = 1; apply(); };

  var bFull = document.getElementById('v-full'), bFocus = document.getElementById('v-focus');
  function show(which) {
    document.getElementById('view-full').classList.toggle('on', which === 'full');
    document.getElementById('view-focus').classList.toggle('on', which !== 'full');
    bFull.classList.toggle('on', which === 'full');
    bFocus.classList.toggle('on', which !== 'full');
    search(); mark(); fit();
  }
  bFull.onclick = function () { show('full'); };
  bFocus.onclick = function () { show('focus'); };

  var q = document.getElementById('q'), hits = document.getElementById('hits');
  function search() {
    var s = q.value.trim().toLowerCase(), n = 0;
    var nodes = document.querySelectorAll('.view.on g.node');
    for (var i = 0; i < nodes.length; i++) {
      var g = nodes[i], id = g.querySelector('title').textContent.trim(), d = DATA.nodes[id];
      // 搜索面覆盖全文，不只是图上那行截断标签
      var hay = (g.textContent + ' ' + (d ? [d.claim, d.criterion, d.needs].concat(d.evidence).join(' ') : '')).toLowerCase();
      var on = s && hay.indexOf(s) >= 0;
      g.classList.toggle('hit', !!on);
      g.classList.toggle('miss', !!s && !on);
      if (on) n++;
    }
    var v = document.querySelector('.view.on');
    if (v) v.classList.toggle('searching', !!s);
    hits.textContent = s ? (n + ' 个命中') : '';
  }
  q.addEventListener('input', search);

  // ---------- 详情面板 ----------
  var cur = null;
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function chips(ids) {
    if (!ids || !ids.length) return '<p class="none">无</p>';
    return '<p>' + ids.map(function (i) {
      return '<span class="chip" data-go="' + esc(i) + '">' + esc(i) + '</span>';
    }).join('') + '</p>';
  }
  function list(items, cls) {
    if (!items || !items.length) return '<p class="none">无</p>';
    return '<ul>' + items.map(function (t) {
      return '<li class="' + (cls || '') + '">' + esc(t) + '</li>';
    }).join('') + '</ul>';
  }
  function findNode(id) {
    var all = document.querySelectorAll('.view.on g.node');
    for (var i = 0; i < all.length; i++) {
      if (all[i].querySelector('title').textContent.trim() === id) return all[i];
    }
    return null;
  }
  function mark() {
    var all = document.querySelectorAll('g.node');
    for (var i = 0; i < all.length; i++) {
      all[i].classList.toggle('sel', cur != null && all[i].querySelector('title').textContent.trim() === cur);
    }
  }
  function center(id) {
    var g = findNode(id);
    if (!g) return;
    var r = stage.getBoundingClientRect(), b = g.getBoundingClientRect();
    tx += (r.left + r.width / 2) - (b.left + b.width / 2);
    ty += (r.top + r.height / 2) - (b.top + b.height / 2);
    apply();
  }
  // 面板一开就占掉画布右侧一块，刚点的节点可能被挤出可视区；只在真挤出去时才居中，
  // 已经看得见的就别动，免得每点一下画面都跳
  function ensureVisible(id) {
    var g = findNode(id);
    if (!g) return;
    var r = stage.getBoundingClientRect(), b = g.getBoundingClientRect();
    if (b.left >= r.left && b.right <= r.right && b.top >= r.top && b.bottom <= r.bottom) return;
    center(id);
  }
  function open(id) {
    var wasOpen = panel.classList.contains('on');
    cur = id;
    var h = '';
    if (id === 'goal') {
      h = '<h2>目标</h2><p>' + esc(DATA.goal) + '</p>'
        + '<h3>主线</h3><p>' + (DATA.mainLine ? '<span class="chip" data-go="' + esc(DATA.mainLine) + '">'
            + esc(DATA.mainLine) + '</span>' : '<span class="none">未设</span>') + '</p>'
        + '<h3>统计</h3><p>共 ' + DATA.total + ' 个节点：'
        + Object.keys(DATA.tally).map(function (t) { return esc(t) + ' ' + DATA.tally[t]; }).join('、') + '</p>'
        + (DATA.created ? '<h3>建图时间</h3><p class="mono">' + esc(DATA.created) + '</p>' : '');
    } else if (id === 'refuted_bin') {
      h = '<h2>已证伪 ' + DATA.refuted.length + ' 条</h2>'
        + '<p class="none">全图视图里折叠成一块，点下面任一条看它自己的全文。</p>'
        + '<h3>清单</h3>' + chips(DATA.refuted);
    } else {
      var d = DATA.nodes[id];
      if (!d) { h = '<h2>' + esc(id) + '</h2><p class="none">这个节点不在 YAML 里。</p>'; }
      else {
        h = '<h2>' + esc(id) + '<span class="badge">' + esc(d.statusZh) + '</span>'
          + '<span class="kind">' + esc(d.kind) + (id === DATA.mainLine ? ' · 主线' : '') + '</span></h2>'
          + '<h3>断言</h3><p>' + esc(d.claim) + '</p>'
          + '<h3>判据</h3><p>' + (d.criterion ? esc(d.criterion) : '<span class="none">无</span>') + '</p>'
          + (d.needs ? '<h3>缺什么</h3><p>' + esc(d.needs) + '</p>' : '')
          + '<h3>证据（' + d.evidence.length + ' 条）</h3>' + list(d.evidence, 'mono')
          + '<h3>依赖</h3>' + chips(d.dependsOn)
          + '<h3>被谁依赖</h3>' + chips(d.dependedBy)
          + '<h3>历程</h3><ul class="hist">' + d.history.map(function (x) {
              return '<li>' + esc(x.at) + ' → ' + esc(x.status) + (x.note ? '：' + esc(x.note) : '') + '</li>';
            }).join('') + '</ul>';
      }
    }
    pbody.innerHTML = h;
    panel.classList.add('on');
    mark();
    if (!wasOpen) ensureVisible(id);
  }
  function close() { panel.classList.remove('on'); cur = null; mark(); }
  document.getElementById('close').onclick = close;
  pbody.addEventListener('click', function (e) {
    var go = e.target.getAttribute && e.target.getAttribute('data-go');
    if (!go) return;
    open(go);
    center(go);            // 目标在当前视图里就顺带居中，不在（比如已折叠）就只开面板
  });

  document.addEventListener('keydown', function (e) {
    if (e.target === q) { if (e.key === 'Escape') { q.value = ''; search(); q.blur(); } return; }
    if (e.key === 'Escape') close();
    else if (e.key === '+' || e.key === '=') zoomCenter(1.25);
    else if (e.key === '-') zoomCenter(0.8);
    else if (e.key === '0') fit();
    else if (e.key === '/') { e.preventDefault(); q.focus(); }
  });

  window.addEventListener('resize', fit);
  fit();
})();
</script>
</body>
</html>
"""


def build(yaml_path: str, out_path: str, rankdir: str) -> None:
    data = graph.load(yaml_path)
    full = _svg(data, focus=False, collapse=True, rankdir=rankdir)
    # 主线视图节点少、多为一条链，固定用 TB
    focus = _svg(data, focus=True, collapse=False, rankdir="TB")
    payload = json.dumps(_payload(data), ensure_ascii=False).replace("</", "<\\/")
    page = (TEMPLATE
            .replace("__TITLE__", html.escape(graph._short(data.get("goal", "证据图"), 40)))
            .replace("__MAIN__", html.escape(str(data.get("main_line") or "未设")))
            .replace("__N_FULL__", str(full.count('class="node"')))
            .replace("__N_FOCUS__", str(focus.count('class="node"')))
            .replace("__SVG_FULL__", full)
            .replace("__SVG_FOCUS__", focus)
            .replace("__DATA__", payload))
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)


def default_out(yaml_path: str) -> str:
    """约定的输出路径：与图同目录、同名，后缀换成 -viewer.html。"""
    return os.path.splitext(yaml_path)[0] + "-viewer.html"


def main() -> int:
    ap = argparse.ArgumentParser(description="把证据图出成可缩放、可搜索、点节点看全文的单文件 HTML")
    ap.add_argument("path", help="图文件路径（YAML）")
    ap.add_argument("--out", help="输出的 .html；省略则与图同名加 -viewer.html")
    ap.add_argument("--rankdir", default="LR", choices=("TB", "LR"), help="全图布局方向，默认 LR")
    ap.add_argument("--watch", action="store_true", help="盯着 YAML，改一次重出一次，Ctrl-C 结束")
    args = ap.parse_args()
    out = args.out or default_out(args.path)
    if not shutil.which("dot"):
        print("错误：" + graph.NO_GRAPHVIZ, file=sys.stderr)
        return 1

    build(args.path, out, args.rankdir)
    print("已写 " + out)
    if not args.watch:
        return 0

    # 手改 YAML 不经过 graph.py，钩子挂不上，只能靠轮询 mtime
    print("盯着 " + args.path + "，Ctrl-C 结束")
    last = os.path.getmtime(args.path)
    try:
        while True:
            time.sleep(1)
            cur = os.path.getmtime(args.path)
            if cur == last:
                continue
            last = cur
            try:
                build(args.path, out, args.rankdir)
                print(time.strftime("%H:%M:%S") + " 已刷新 " + out)
            except Exception as exc:
                print(time.strftime("%H:%M:%S") + " 刷新失败：" + str(exc), file=sys.stderr)
    except KeyboardInterrupt:
        print("\n停止")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except graph.GraphError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        sys.exit(1)
