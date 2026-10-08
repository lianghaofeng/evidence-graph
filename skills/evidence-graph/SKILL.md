---
name: evidence-graph
description: Use only when the user explicitly invokes this skill for an investigation (defect triage, root-cause analysis, performance diagnosis, parameter tuning, CI run interpretation). Never self-trigger on keywords.
---

# evidence-graph：排查用证据图

## 原则

排查状态不能只活在上下文里。每条查明的事实、每个假设、每次实验都立成图上的节点，状态由证据决定，图落盘、脚本校验。三条硬规则：

1. **断言先立后证**：要查什么，先把它写成一句可判定的话加进图，再去查。
2. **状态只认证据**：判已证实 / 已证伪必须附 `文件:行号 摘录` 或 `命令 -> 输出摘录`。推断不进 evidence。
3. **主线不到终态不换线**：当前主线只有判成已证实 / 已证伪 / 受阻（写明缺什么）之后才能 switch，每次换线记原因。

违反字面就是违反本意。「先试试再说」「摘要里说好像是」「用户说八成是」「这个太简单不用立节点」「我记得之前查过」都是换线或重查的借口，出现即停，回到图上。

## 路径

```bash
G="${CLAUDE_SKILL_DIR}/scripts/graph.py"
[ -f "$G" ] || G="$(claude plugin list --json 2>/dev/null | python3 -c 'import json, sys; print(next((p["installPath"] for p in json.load(sys.stdin) if p.get("id", "").startswith("evidence-graph@") and p.get("enabled")), ""))' 2>/dev/null)/skills/evidence-graph/scripts/graph.py"
[ -f "$G" ] || G=$(find ~/.claude/plugins ~/.claude/skills -name .trash -prune -o -path '*/evidence-graph/scripts/graph.py' -print 2>/dev/null | head -1)
[ -f "$G" ] || echo "未找到 graph.py，确认 evidence-graph 插件已安装"
Y=<图的路径，见「图的位置」>
```

`G` 取 skill 所在目录；占位符未被替换时，向 Claude Code 查询已启用的 evidence-graph 插件的安装目录；两者都拿不到时按文件名搜索，跳过 `.trash` 下的旧副本。不写某台机器上的固定路径。

## 图的位置

- 项目说明（CLAUDE.md 等）规定了排查文档的位置时，图放在规定的位置。
- 没有规定时，图放在仓库根目录 `docs/evidence-graph/<日期>-<主题短名>-evidence-graph.yaml`，主题短名用小写字母、数字和连字符。首次建这个目录时告诉用户：该目录未被 git 忽略，是否提交由用户决定。
- 续图时先查项目规定的位置，再用下面的命令按后缀搜索。搜索范围是当前仓库及它的全部 git worktree；命令跟随软链，跳过 `.git` 与 `node_modules`，按真实路径去重。找到多张时列出来由用户选。

```bash
R=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
{ echo "$R"; git worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p'; } \
  | while IFS= read -r d; do find -L "$d" \( -name .git -o -name node_modules \) -prune -o -name '*-evidence-graph.yaml' -print 2>/dev/null; done \
  | python3 -c 'import os, sys; sys.stdout.write("".join(p + "\n" for p in sorted({os.path.realpath(l.strip()) for l in sys.stdin if l.strip()})))'
```

## 流程

| 时机 | 做什么 |
| --- | --- |
| 开图 / 续图 | 有图就 `tree`；没有就 `init --goal "<一句话问题>"`。把上下文里已知的东西先立成节点，有证据的当场判掉，没证据的保持待查 |
| 每轮开工前 | `tree` 再 `ready --main`。只在 ready 列出的节点上干活；主线没设就先 `switch --to` |
| 主线设好之后 | 跑一次 `viewer`，在图旁边出 `<图名>-viewer.html`；主线未设时 `viewer` 报错退出。出过之后每次改图会自动重新生成它，开发者刷新浏览器就能看到最新内容，并可缩放、搜索、点节点看全文。只需跑这一次 |
| 要查一件事 | `add <ID> --kind fact/hypothesis/experiment --claim "…" --criterion "…" [--depends-on …]`。假设依赖它的判定实验；实验必带 `--command` |
| 拿到证据 | 立刻 `set <ID> --status confirmed/refuted/done --evidence "…"`；受阻用 `--status blocked --needs "…"` |
| 想换方向 | 先把当前主线判到终态，再 `switch --to <ID> --reason "…"`。用户要求换方向也走这一步，把当前主线判 blocked 并写 needs |
| 想重提已证伪的 | `set <ID> --status open --new-evidence "…"`，没有新证据就不许重开 |
| 每次改图后 | `check`。不通过就先修图，修好才能继续 |
| 派子代理 | 把节点的 claim 与 criterion 原样给它，让它只回报证据。写图的只有主会话 |
| 压缩 / 换会话后 | 第一件事 `tree`。摘要与图冲突时以图为准，图上的结论带证据 |
| 收尾 | `tree --status confirmed` 摘出结论；`render --focus --out <日期>-<主题短名>-graph-focus.png` 与 `render --collapse-refuted --out <日期>-<主题短名>-graph.png` 出静态图，写进图所在目录的报告。HTML 视图随改图自动重新生成，不用另外刷 |

节点编号约定：F 事实、H 假设、E 实验，加序号。ID 一旦立了不改。

## 常见错误

| 错误 | 修正 |
| --- | --- |
| 把摘要里的「好像」直接当事实用 | 立成待查节点，跑一条命令判掉，再用 |
| 用户提了新方向就跟着走 | 新方向先 add 成假设；主线没到终态就不 switch，向用户说明主线差什么 |
| 实验跑完只在对话里说结果 | 结果进 `set E<n> --status done --evidence`，再判依赖它的假设 |
| 把一个节点写成两件事 | 一节点一断言；「A 且 B」拆成两个节点 |
| 证据写成推断（「应该是」「按理说」） | 只写读到的行和跑出来的输出；推断写进 criterion 或对话，不进 evidence |

细则、字段与五条校验规则见 `references/graph-format.md`，完整范例见 `references/example.yaml`（虚构内容，只示意格式）。
