#!/usr/bin/env python3
"""evidence-graph 的图操作与校验脚本。

一份排查用的证据图存成 YAML：节点是可判定的断言（事实 / 假设）或一次实验，边是依赖。
脚本负责三件事：改图（init / add / set / switch）、读图（ready / tree）、
出图（render），以及把五条硬规则做成确定性校验（check），让主会话每次改图后能机械地
判断图有没有变形，不靠自觉。

只依赖标准库与 PyYAML。用法见 `graph.py -h`，格式说明见 references/graph-format.md。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

# ---------- 词表 ----------

ASSERTION_KINDS = ("fact", "hypothesis")
KINDS = ASSERTION_KINDS + ("experiment",)
# 断言类节点与实验类节点各有一套状态；open 与 blocked 共用，终态命名分开，
# 免得「实验跑完了」和「断言被证实了」混成一个词。
ASSERTION_STATUSES = ("open", "confirmed", "refuted", "blocked")
EXPERIMENT_STATUSES = ("open", "done", "blocked")
SATISFIED = ("confirmed", "done")  # 作为依赖时算「已满足」
STATUS_ZH = {
    "open": "待查",
    "confirmed": "已证实",
    "refuted": "已证伪",
    "blocked": "受阻",
    "done": "已执行",
}
KIND_ZH = {"fact": "事实", "hypothesis": "假设", "experiment": "实验"}
# 渲染用的填充色：绿证实、红证伪、黄待查、灰受阻、蓝已执行
FILL = {
    "open": "#fff4cc",
    "confirmed": "#d8f0d8",
    "refuted": "#f0d8d8",
    "blocked": "#e6e6e6",
    "done": "#d8e6f0",
}


def now() -> str:
    """本地时间，分钟精度，写进 history 与 switches。"""
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M")


# ---------- 读写 ----------

def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    data.setdefault("nodes", {})
    data.setdefault("switches", [])
    data.setdefault("main_line", None)
    return data


def viewer_path(path: str) -> str:
    """约定的 HTML 视图路径：与图同目录、同名，后缀换成 -viewer.html。"""
    return os.path.splitext(path)[0] + "-viewer.html"


def refresh_viewer(path: str, quiet: bool = True) -> bool:
    """改完图顺手重出 HTML 视图，让它与 YAML 保持同步。

    只在 HTML 已经存在时刷新：没生成过视图的图不额外生成文件。
    刷新失败不影响改图本身，图已经落盘。
    缺 graphviz 时在 stderr 打印一行提示，说明 HTML 未刷新、图已保存。
    """
    out = viewer_path(path)
    if not os.path.exists(out):
        return False
    if not shutil.which("dot"):
        print(f"提示：{os.path.basename(out)} 没刷新，本机缺 graphviz（dot），图本身已保存", file=sys.stderr)
        return False
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer.py")
    try:
        subprocess.run([sys.executable, script, path, "--out", out],
                       check=True, capture_output=True, timeout=120)
    except Exception as exc:
        print(f"提示：{os.path.basename(out)} 没刷新成功（{exc}），图本身已保存", file=sys.stderr)
        return False
    if not quiet:
        print("已刷新 " + out)
    return True


def save(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False, width=120)
    refresh_viewer(path)


class GraphError(Exception):
    """命令级错误：打印一行中文原因，退出码 1。"""


# ---------- 图算法 ----------

def find_cycle(nodes: dict) -> list[str] | None:
    """三色 DFS 找环；找到返回环上的节点序列，没有返回 None。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in nodes}
    stack: list[str] = []

    def visit(u: str) -> list[str] | None:
        color[u] = GRAY
        stack.append(u)
        for v in nodes[u].get("depends_on", []) or []:
            if v not in nodes:
                continue
            if color[v] == GRAY:
                return stack[stack.index(v):] + [v]
            if color[v] == WHITE:
                cyc = visit(v)
                if cyc:
                    return cyc
        stack.pop()
        color[u] = BLACK
        return None

    for k in nodes:
        if color[k] == WHITE:
            cyc = visit(k)
            if cyc:
                return cyc
    return None


def descendants(nodes: dict, root: str) -> set[str]:
    """root 及其传递依赖。"""
    seen, todo = set(), [root]
    while todo:
        u = todo.pop()
        if u in seen or u not in nodes:
            continue
        seen.add(u)
        todo.extend(nodes[u].get("depends_on", []) or [])
    return seen


def ancestors(nodes: dict, root: str) -> set[str]:
    """传递地依赖 root 的节点（含 root）。"""
    rev: dict[str, list[str]] = {k: [] for k in nodes}
    for k, n in nodes.items():
        for d in n.get("depends_on", []) or []:
            rev.setdefault(d, []).append(k)
    seen, todo = set(), [root]
    while todo:
        u = todo.pop()
        if u in seen:
            continue
        seen.add(u)
        todo.extend(rev.get(u, []))
    return seen


def is_terminal(n: dict) -> bool:
    return n.get("status") != "open"


def is_ready(nodes: dict, nid: str) -> bool:
    """待查且所有依赖都已满足的节点才能开工。"""
    n = nodes[nid]
    if n.get("status") != "open":
        return False
    return all(nodes.get(d, {}).get("status") in SATISFIED for d in n.get("depends_on", []) or [])


# ---------- 校验 ----------

def check(data: dict) -> list[str]:
    """返回错误列表；空列表表示图合法。规则编号与 references/graph-format.md 对应。"""
    errs: list[str] = []
    nodes = data.get("nodes", {}) or {}

    # R0 词表与引用
    for nid, n in nodes.items():
        kind = n.get("kind")
        st = n.get("status")
        if kind not in KINDS:
            errs.append(f"R0 {nid}: kind 非法 {kind!r}")
            continue
        allowed = EXPERIMENT_STATUSES if kind == "experiment" else ASSERTION_STATUSES
        if st not in allowed:
            errs.append(f"R0 {nid}: {KIND_ZH[kind]}节点的 status 不能是 {st!r}")
        if not (n.get("claim") or "").strip():
            errs.append(f"R0 {nid}: claim 为空")
        for d in n.get("depends_on", []) or []:
            if d not in nodes:
                errs.append(f"R0 {nid}: 依赖的节点 {d} 不存在")

    # R1 无环
    cyc = find_cycle(nodes)
    if cyc:
        errs.append("R1 依赖成环：" + " -> ".join(cyc))

    # R2 终态必须带依据
    for nid, n in nodes.items():
        st = n.get("status")
        ev = [e for e in (n.get("evidence") or []) if str(e).strip()]
        if st in ("confirmed", "done", "refuted") and not ev:
            errs.append(f"R2 {nid}: 状态为{STATUS_ZH.get(st, st)}，缺少 evidence")
        if st == "blocked" and not (n.get("needs") or "").strip():
            errs.append(f"R2 {nid}: 状态为受阻，缺少 needs（写明缺什么才能继续）")

    # R3 待查节点不能依赖已证伪的节点
    for nid, n in nodes.items():
        if n.get("status") != "open":
            continue
        for d in n.get("depends_on", []) or []:
            if nodes.get(d, {}).get("status") == "refuted":
                errs.append(f"R3 {nid}: 状态为待查，依赖的 {d} 已证伪；把 {nid} 判证伪，或去掉这条依赖")

    # R4 主线唯一、指向待查节点、与换线记录一致
    main = data.get("main_line")
    switches = data.get("switches") or []
    if main is not None:
        if main not in nodes:
            errs.append(f"R4 main_line 指向不存在的节点 {main}")
        elif is_terminal(nodes[main]):
            errs.append(f"R4 主线 {main} 已到终态（{STATUS_ZH[nodes[main]['status']]}），要么 switch 换线，要么收尾")
    if switches:
        last_to = switches[-1].get("to")
        if main != last_to:
            errs.append(f"R4 main_line={main!r} 与最后一次换线的 to={last_to!r} 不一致；主线只能通过 switch 改")
    for i, s in enumerate(switches, 1):
        if not (s.get("reason") or "").strip():
            errs.append(f"R4 第 {i} 次换线没写 reason")
        frm = s.get("from")
        if frm is not None and s.get("from_status") == "open":
            errs.append(f"R4 第 {i} 次换线时 {frm} 仍是待查，没走到终态就换线了")

    # R5 已证伪的节点重开必须带新证据
    for nid, n in nodes.items():
        hist = n.get("history") or []
        if n.get("status") == "open" and any(h.get("status") == "refuted" for h in hist):
            last = hist[-1] if hist else {}
            if last.get("status") != "open" or not (last.get("note") or "").strip():
                errs.append(f"R5 {nid}: 曾被证伪，重开时没有记录新证据")
    return errs


# ---------- 命令 ----------

def cmd_init(path: str, args) -> int:
    if os.path.exists(path):
        raise GraphError(f"{path} 已存在，不覆盖")
    data = {
        "goal": args.goal,
        "created": now(),
        "main_line": None,
        "nodes": {},
        "switches": [],
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    save(path, data)
    print(f"已建图：{path}")
    return 0


def cmd_add(path: str, args) -> int:
    data = load(path)
    nodes = data["nodes"]
    if args.id in nodes:
        raise GraphError(f"节点 {args.id} 已存在")
    deps = [d.strip() for d in (args.depends_on or "").split(",") if d.strip()]
    for d in deps:
        if d not in nodes:
            raise GraphError(f"依赖的节点 {d} 不存在，先 add 它")
    if args.kind == "experiment" and not args.command:
        raise GraphError("实验节点必须带 --command（跑的是什么）")
    node = {
        "kind": args.kind,
        "claim": args.claim,
        "status": "open",
        "criterion": args.criterion or "",
        "depends_on": deps,
        "evidence": [],
        "history": [{"at": now(), "status": "open", "note": "新建"}],
    }
    if args.kind == "experiment":
        node["command"] = args.command
    nodes[args.id] = node
    save(path, data)
    print(f"已加节点 {args.id}（{KIND_ZH[args.kind]}）")
    return _after_write(data)


def cmd_set(path: str, args) -> int:
    data = load(path)
    nodes = data["nodes"]
    if args.id not in nodes:
        raise GraphError(f"节点 {args.id} 不存在")
    n = nodes[args.id]
    kind = n["kind"]
    allowed = EXPERIMENT_STATUSES if kind == "experiment" else ASSERTION_STATUSES
    st = args.status
    if st not in allowed:
        raise GraphError(f"{KIND_ZH[kind]}节点的状态只能是 {'/'.join(allowed)}，不能是 {st}")
    evidence = [e for e in (args.evidence or []) if e.strip()]
    if st in ("confirmed", "done", "refuted") and not evidence and not n.get("evidence"):
        raise GraphError(f"判 {STATUS_ZH[st]} 必须带 --evidence（命令与输出摘录，或 文件:行号）")
    if st == "blocked" and not (args.needs or "").strip():
        raise GraphError("判受阻必须带 --needs（缺什么才能继续）")
    was_refuted = n.get("status") == "refuted" or any(
        h.get("status") == "refuted" for h in n.get("history") or [])
    note = args.note or ""
    if st == "open" and was_refuted:
        if not (args.new_evidence or "").strip():
            raise GraphError(f"{args.id} 曾被证伪，重开必须带 --new-evidence 说明新证据是什么")
        note = args.new_evidence
        evidence.append("重开依据：" + args.new_evidence)
    n["status"] = st
    n.setdefault("evidence", []).extend(evidence)
    if st == "blocked":
        n["needs"] = args.needs
    if args.criterion:
        n["criterion"] = args.criterion
    n.setdefault("history", []).append({"at": now(), "status": st, "note": note})
    save(path, data)
    print(f"{args.id} -> {STATUS_ZH[st]}")
    return _after_write(data)


def cmd_switch(path: str, args) -> int:
    data = load(path)
    nodes = data["nodes"]
    if not (args.reason or "").strip():
        raise GraphError("换线必须带 --reason")
    to = args.to
    if to not in nodes:
        raise GraphError(f"节点 {to} 不存在")
    if nodes[to]["status"] != "open":
        raise GraphError(f"只能把主线切到待查节点，{to} 现在是 {STATUS_ZH[nodes[to]['status']]}")
    frm = data.get("main_line")
    if frm is not None and frm in nodes and not is_terminal(nodes[frm]):
        raise GraphError(
            f"当前主线 {frm} 仍是待查，没走到终态不许换线。先把它判成 已证实 / 已证伪 / 受阻（写清缺什么）")
    data["switches"].append({
        "at": now(),
        "from": frm,
        "from_status": nodes[frm]["status"] if frm in nodes else None,
        "to": to,
        "reason": args.reason,
    })
    data["main_line"] = to
    save(path, data)
    print(f"主线：{frm} -> {to}")
    return _after_write(data)


def _after_write(data: dict) -> int:
    """改图后顺手跑一遍校验。校验错误只提示不阻断，因为有些中间态是合法的过渡
    （比如刚把一个事实判证伪，依赖它的假设下一步才会跟着判）。"""
    errs = check(data)
    if errs:
        print(f"校验：{len(errs)} 条待处理", file=sys.stderr)
        for e in errs:
            print("  " + e, file=sys.stderr)
    return 0


def cmd_check(path: str, args) -> int:
    errs = check(load(path))
    if errs:
        print(f"校验不通过，{len(errs)} 条：")
        for e in errs:
            print("  " + e)
        return 1
    print("校验通过")
    return 0


def cmd_ready(path: str, args) -> int:
    data = load(path)
    nodes = data["nodes"]
    main = data.get("main_line")
    scope = descendants(nodes, main) if (args.main and main) else set(nodes)
    ready = [k for k in nodes if k in scope and is_ready(nodes, k)]
    if not ready:
        print("没有可开工的节点" + ("（主线范围内）" if args.main else ""))
        return 0
    # 主线子树里的排前面
    in_main = descendants(nodes, main) if main else set()
    ready.sort(key=lambda k: (k not in in_main, k))
    for k in ready:
        n = nodes[k]
        tag = " << 主线" if k == main else ("  （主线依赖）" if k in in_main else "")
        crit = f"  判据：{n['criterion']}" if n.get("criterion") else ""
        print(f"{k} [{KIND_ZH[n['kind']]}] {n['claim']}{crit}{tag}")
    return 0


def _node_line(nid: str, n: dict, main: str | None) -> str:
    st = n["status"]
    parts = [f"[{STATUS_ZH[st]}] {nid} {KIND_ZH[n['kind']]}：{n['claim']}"]
    if st in ("confirmed", "done", "refuted") and n.get("evidence"):
        parts.append("依据：" + _short(n["evidence"][-1]))
    elif st == "blocked":
        parts.append("缺：" + _short(n.get("needs", "")))
    elif st == "open" and n.get("criterion"):
        parts.append("判据：" + _short(n["criterion"]))
    if n["kind"] == "experiment" and n.get("command"):
        parts.append("命令：" + _short(n["command"]))
    line = "  ".join(parts)
    if nid == main:
        line += "  << 主线"
    return line


def _short(s: str, n: int = 90) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def cmd_tree(path: str, args) -> int:
    data = load(path)
    nodes = data["nodes"]
    main = data.get("main_line")
    print(f"目标：{data.get('goal', '')}")
    if args.status:
        for k, n in nodes.items():
            if n["status"] == args.status:
                print(_node_line(k, n, main))
        return 0
    depended = {d for n in nodes.values() for d in n.get("depends_on", []) or []}
    roots = [k for k in nodes if k not in depended]
    roots.sort(key=lambda k: (k != main, k))
    printed: set[str] = set()

    def walk(nid: str, depth: int) -> None:
        indent = "  " * depth
        if nid in printed:
            print(f"{indent}(见 {nid})")
            return
        printed.add(nid)
        print(indent + _node_line(nid, nodes[nid], main))
        for d in nodes[nid].get("depends_on", []) or []:
            if d in nodes:
                walk(d, depth + 1)

    for r in roots:
        walk(r, 1)
    counts = {}
    for n in nodes.values():
        counts[n["status"]] = counts.get(n["status"], 0) + 1
    summary = "，".join(f"{STATUS_ZH[s]} {c}" for s, c in counts.items())
    print(f"合计 {len(nodes)} 节点：{summary or '空'}")
    print(f"主线：{main or '未设'}；换线 {len(data.get('switches') or [])} 次")
    if data.get("switches"):
        s = data["switches"][-1]
        print(f"最近一次换线：{s.get('from')} -> {s.get('to')}，原因：{s.get('reason')}")
    return 0


# ---------- 渲染 ----------

def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _select(data: dict, focus: bool, collapse: bool) -> tuple[dict, int]:
    """按投影参数选出要画的节点，返回 (节点子集, 被折叠的已证伪数)。"""
    nodes = data["nodes"]
    main = data.get("main_line")
    keep = set(nodes)
    if focus:
        if not main:
            raise GraphError("--focus 需要先用 switch 设主线")
        keep = descendants(nodes, main) | ancestors(nodes, main)
    collapsed = 0
    if collapse:
        refuted = {k for k in keep if nodes[k]["status"] == "refuted"}
        collapsed = len(refuted)
        keep -= refuted
    return {k: nodes[k] for k in nodes if k in keep}, collapsed


def render_dot(data: dict, focus: bool, collapse: bool, rankdir: str = "TB") -> str:
    sub, collapsed = _select(data, focus, collapse)
    main = data.get("main_line")
    out = [
        "digraph evidence {",
        f"  rankdir={rankdir};",
        '  node [shape=box, style="rounded,filled", fontname="PingFang SC", fillcolor=white];',
        '  edge [fontname="PingFang SC"];',
        f'  goal [label="目标：{_esc(data.get("goal", ""))}", shape=box, fillcolor=white];',
    ]
    depended = {d for n in sub.values() for d in n.get("depends_on", []) or []}
    for k, n in sub.items():
        label = f"{k} {STATUS_ZH[n['status']]}\\n{_esc(_short(n['claim'], 40))}"
        if n["status"] in ("confirmed", "done", "refuted") and n.get("evidence"):
            label += "\\n" + _esc(_short(n["evidence"][-1], 40))
        elif n["status"] == "blocked":
            label += "\\n缺：" + _esc(_short(n.get("needs", ""), 40))
        pen = ", penwidth=2" if k == main else ""
        out.append(f'  {k} [label="{label}", fillcolor="{FILL[n["status"]]}"{pen}];')
        if k not in depended:
            out.append(f"  goal -> {k};")
    if collapsed:
        out.append(f'  refuted_bin [label="已证伪 {collapsed} 条（已折叠）", fillcolor="{FILL["refuted"]}"];')
        out.append("  goal -> refuted_bin;")
    for k, n in sub.items():
        for d in n.get("depends_on", []) or []:
            if d in sub:
                out.append(f"  {k} -> {d};")
    out.append("}")
    return "\n".join(out) + "\n"


def render_mermaid(data: dict, focus: bool, collapse: bool, rankdir: str = "TB") -> str:
    sub, collapsed = _select(data, focus, collapse)
    main = data.get("main_line")
    # mermaid 把自上而下写作 TD，graphviz 写作 TB，这里统一收口，对外只认 TB / LR
    out = [f"graph {'TD' if rankdir == 'TB' else rankdir}", f'  goal["目标：{_esc(data.get("goal", ""))}"]']
    depended = {d for n in sub.values() for d in n.get("depends_on", []) or []}
    for k, n in sub.items():
        label = f"{k} {STATUS_ZH[n['status']]}<br/>{_esc(_short(n['claim'], 40))}"
        out.append(f'  {k}["{label}"]:::{n["status"]}')
        if k not in depended:
            out.append(f"  goal --> {k}")
    if collapsed:
        out.append(f'  refuted_bin["已证伪 {collapsed} 条（已折叠）"]:::refuted')
        out.append("  goal --> refuted_bin")
    for k, n in sub.items():
        for d in n.get("depends_on", []) or []:
            if d in sub:
                out.append(f"  {k} --> {d}")
    for st, color in FILL.items():
        out.append(f"  classDef {st} fill:{color},stroke:#333;")
    if main and main in sub:
        out.append(f"  style {main} stroke-width:3px;")
    return "\n".join(out) + "\n"


def cmd_viewer(path: str, args) -> int:
    """生成 HTML 视图。生成过一次之后，后续 add / set / switch 会自动刷新它。"""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer.py")
    argv = [sys.executable, script, path, "--out", args.out or viewer_path(path),
            "--rankdir", args.rankdir]
    if args.watch:
        argv.append("--watch")
    return subprocess.call(argv)


def cmd_render(path: str, args) -> int:
    data = load(path)
    text = render_dot(data, args.focus, args.collapse_refuted, args.rankdir) if args.format == "dot" \
        else render_mermaid(data, args.focus, args.collapse_refuted, args.rankdir)
    if not args.out:
        sys.stdout.write(text)
        return 0
    ext = os.path.splitext(args.out)[1].lower()
    if ext in (".svg", ".png"):
        if args.format != "dot":
            raise GraphError("出 svg/png 只支持 --format dot")
        if not shutil.which("dot"):
            raise GraphError("没有 graphviz（dot）。macOS：brew install graphviz；Debian/Ubuntu：apt install graphviz")
        with tempfile.NamedTemporaryFile("w", suffix=".dot", delete=False, encoding="utf-8") as fh:
            fh.write(text)
            dot_path = fh.name
        try:
            if ext == ".svg":
                subprocess.run(["dot", "-Tsvg", dot_path, "-o", args.out], check=True)
            else:
                # 先出 SVG 再用 rsvg-convert 转 PNG，本机没有 rsvg-convert 时用 dot -Tpng
                svg = args.out[:-4] + ".svg"
                subprocess.run(["dot", "-Tsvg", dot_path, "-o", svg], check=True)
                if shutil.which("rsvg-convert"):
                    subprocess.run(["rsvg-convert", "-w", "1760", "-b", "white", svg, "-o", args.out], check=True)
                else:
                    subprocess.run(["dot", "-Tpng", dot_path, "-o", args.out], check=True)
        finally:
            os.unlink(dot_path)
    else:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(f"已写 {args.out}")
    return 0


# ---------- 入口 ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="graph.py",
        description="evidence-graph：排查用证据图的读写、校验与渲染。第一个参数是 YAML 路径。",
    )
    p.add_argument("path", help="图文件路径（YAML）")
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("init", help="新建一张图")
    s.add_argument("--goal", required=True, help="这次排查要回答的问题，一句话")

    s = sp.add_parser("add", help="加一个节点，初始状态待查")
    s.add_argument("id", help="节点编号，建议 F 事实 / H 假设 / E 实验 加序号")
    s.add_argument("--kind", required=True, choices=KINDS)
    s.add_argument("--claim", required=True, help="断言原文，必须可判定")
    s.add_argument("--criterion", help="怎样算证实或证伪")
    s.add_argument("--command", help="实验节点：跑的命令")
    s.add_argument("--depends-on", help="逗号分隔的依赖节点编号")

    s = sp.add_parser("set", help="改一个节点的状态")
    s.add_argument("id")
    s.add_argument("--status", required=True, choices=sorted(set(ASSERTION_STATUSES + EXPERIMENT_STATUSES)))
    s.add_argument("--evidence", action="append", help="依据：命令与输出摘录，或 文件:行号；可重复")
    s.add_argument("--needs", help="受阻时：缺什么才能继续")
    s.add_argument("--new-evidence", help="重开已证伪节点时：新证据是什么")
    s.add_argument("--criterion", help="顺手补判据")
    s.add_argument("--note", help="备注，进 history")

    s = sp.add_parser("switch", help="换主线；当前主线必须已到终态")
    s.add_argument("--to", required=True)
    s.add_argument("--reason", required=True)

    s = sp.add_parser("ready", help="列出可开工的节点（待查且依赖全满足）")
    s.add_argument("--main", action="store_true", help="只看主线子树")

    sp.add_parser("check", help="跑五条硬规则校验，失败退出码 1")

    s = sp.add_parser("tree", help="按依赖缩进的文本大纲，模型每轮开工前读这个")
    s.add_argument("--status", choices=sorted(set(ASSERTION_STATUSES + EXPERIMENT_STATUSES)),
                   help="只列某个状态的节点")

    s = sp.add_parser("viewer", help="出可缩放、可搜索、点节点看全文的单文件 HTML；出过之后改图会自动刷新")
    s.add_argument("--out", help="输出的 .html；省略则与图同名加 -viewer.html")
    s.add_argument("--rankdir", default="LR", choices=("TB", "LR"), help="全图布局方向，默认 LR")
    s.add_argument("--watch", action="store_true", help="盯着 YAML，手改也跟着刷新，Ctrl-C 结束")

    s = sp.add_parser("render", help="出 dot / mermaid；--out 以 .svg/.png 结尾时调 graphviz")
    s.add_argument("--format", default="dot", choices=("dot", "mermaid"))
    s.add_argument("--focus", action="store_true", help="只画主线及其祖先与依赖")
    s.add_argument("--collapse-refuted", action="store_true", help="已证伪节点折叠成一个计数")
    # LR 自左向右排布，节点多而层数浅时长宽比接近方形；TB 在这类图上排成扁长条，按文档宽度排版的工具（Typora、Notion）会把它缩到看不清
    s.add_argument("--rankdir", default="TB", choices=("TB", "LR"), help="布局方向：TB 自上而下（默认），LR 自左向右")
    s.add_argument("--out", help="输出文件；省略则打到 stdout")
    return p


COMMANDS = {
    "init": cmd_init,
    "add": cmd_add,
    "set": cmd_set,
    "switch": cmd_switch,
    "ready": cmd_ready,
    "check": cmd_check,
    "tree": cmd_tree,
    "render": cmd_render,
    "viewer": cmd_viewer,
}


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # argparse 的用法错误也走统一的退出码，方便被当库调用
        return int(e.code or 0)
    try:
        return COMMANDS[args.cmd](args.path, args)
    except GraphError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"错误：找不到文件 {e.filename}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
