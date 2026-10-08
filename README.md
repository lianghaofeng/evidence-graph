# evidence-graph

排查问题时把「查明的事实、提出的假设、跑过的实验」立成一张落盘的证据图，脚本校验图是否变形，主会话每轮开工前先读图。目的是让模型基于已经查明的东西往下走：压缩上下文或换会话之后从图上接着查，当前主线判到终态之后才换方向。

灵感来自 Anthropic 2026 年 9 月发布的 Fermat 大定理形式化：Prove2Me 平台用一张定理语句的有向无环图协调几十个代理，早期失败的原因正是「代理很快丢失项目状态、协作失效」。这里把「定理语句」换成「可判定的断言」，把 Lean 的机器判定换成「证据字段非空且格式合规」的脚本校验。

## 1. 安装

### 1.1 在 claude.ai 添加

1. 打开 claude.ai 的 Customize > Plugins，选择 Add > Add marketplace，填入 `lianghaofeng/evidence-graph`。
2. 在这个 marketplace 里安装 evidence-graph 插件。
3. 打开这个 marketplace 的 Sync automatically，作者推送的更新会自动同步到你的账号。

用同一账号登录的 Claude Code（v2.1.273 及以上）在启动时同步插件。会话中出现 `Plugins changed. Run /reload-plugins to activate.` 时，运行 `/reload-plugins` 即可加载。

### 1.2 只用 Claude Code 命令行

```
/plugin marketplace add lianghaofeng/evidence-graph
/plugin install evidence-graph@evidence-graph
```

第三方 marketplace 默认不自动更新。在 `/plugin` 的 Marketplaces 中选中 evidence-graph，开启 Enable auto-update；或在需要时运行 `/plugin marketplace update evidence-graph`。

### 1.3 依赖

| 依赖 | 用途 | 是否必需 |
| --- | --- | --- |
| Python 3 + PyYAML | 脚本本体 | 必需，`pip3 install pyyaml` |
| graphviz（`dot`） | HTML 视图、`render` 出 SVG / PNG | 要看图就必需，`brew install graphviz` / `apt install graphviz`；缺少时 `add` / `set` 会提示 HTML 未刷新 |
| rsvg-convert | SVG 转 PNG，贴到不渲染 SVG 的文档工具 | 可选 |

### 1.4 自检

在仓库的 `skills/evidence-graph/` 目录下运行：

```bash
python3 -m unittest scripts/test_graph.py
```

## 2. 什么时候开

只在你显式输入 `/evidence-graph` 时触发，不按关键词自动挂上；其他命令占用了这个名字时，用全名 `/evidence-graph:evidence-graph`。适合的场景：缺陷排查、根因分析、性能诊断、调参、CI 轮次判读、多仓库联合验证。特征是结论靠证据一条条堆出来，且预计会超过十几次工具调用。单文件小修不值得开。

建议在排查刚开始时就开：模型自己意识不到在绕圈。

## 3. 一次排查的完整流程

### 3.1 开图

在项目仓库里说：

```
/evidence-graph 排查：订单服务把 HTTP 客户端库升到 3.x 后，下单接口 P99 延迟从 120 ms 升到 260 ms
```

模型按项目说明（CLAUDE.md 等）约定的位置建图；项目没有约定时，建在仓库根目录 `docs/evidence-graph/<日期>-<主题短名>-evidence-graph.yaml`，并提示这个目录未被 git 忽略、是否提交由你决定。随后把对话里已经知道的东西立成节点：有证据的当场判成已证实，只是印象的保持待查。

### 3.2 日常循环

每轮模型都会先跑 `tree` 和 `ready --main`，只在可开工的节点上干活。你能看到的行为变化：

- 查一件事时，先看到它 `add` 一个节点，写清判据。
- 拿到证据立刻 `set` 状态，证据是 `文件:行号 摘录` 或 `命令 -> 输出摘录`。
- 你提出新方向时，它先把新方向立成假设；当前主线还没走到终态时，它说明主线差什么，等主线判到终态再换线。要立即换，让它把当前主线判 blocked 并写明缺什么，再 switch。
- 已证伪的假设只有带新证据才会重开。

你随时可以自己看图：

```bash
G=$(find ~/.claude/plugins ~/.claude/skills -path '*/evidence-graph/scripts/graph.py' -exec ls -t {} + 2>/dev/null | head -1)
Y=docs/evidence-graph/2026-10-08-checkout-latency-evidence-graph.yaml
python3 $G $Y tree                 # 文本大纲
python3 $G $Y ready --main         # 下一步该干什么
python3 $G $Y check                # 图有没有变形
python3 $G $Y render --focus --out /tmp/focus.png            # 只看主线
python3 $G $Y render --collapse-refuted --out /tmp/full.png  # 全图，已证伪折叠
python3 $G $Y viewer                                          # 出 HTML 视图，见下一节
```

### 3.2.1 HTML 视图

静态 PNG / SVG 有两个问题：节点标签截在 40 字，判据、全部证据、状态历程都看不到；图大了之后，Typora / Notion 这类按文档宽度排版的工具会把它等比缩到看不清，也不提供对单张图的缩放。

`viewer` 出一份单文件 HTML 解决这两件事，浏览器直接打开，不联网、不依赖浏览器扩展。`viewer` 同时画全图和主线视图，图上用 `switch` 设好主线之后才能运行，主线未设时报错退出：

```bash
python3 $G $Y viewer                 # 写到 <图名>-viewer.html
python3 $G $Y viewer --watch         # 手改 YAML 也跟着刷新，Ctrl-C 结束
```

| 能做什么 | 怎么用 |
| --- | --- |
| 缩放 | 滚轮（以光标为锚）、双击、工具栏 ＋ −、键盘 `+` `-`；`0` 适应窗口 |
| 平移 | 按住拖拽 |
| 看全文 | 点任一节点，右侧抽屉列出断言、判据、全部证据、依赖与被依赖、状态历程，内容直接取自 YAML，不截断 |
| 找节点 | 搜索框，搜索面覆盖全文；命中描红，其余压暗。`/` 聚焦，`Esc` 清空 |
| 切视图 | 全图（已证伪折叠成一块，点开可列出）↔ 主线 |

生成过一次之后，`add` / `set` / `switch` 保存时会自动重新生成这份 HTML；浏览器里已打开的页面手动刷新即可看到最新内容。删掉这个文件即停止自动生成。手改 YAML 不经过脚本，用 `--watch`。

### 3.3 压缩或换会话之后

新会话里再输一次 `/evidence-graph`，模型先按项目约定的位置找图，再在当前仓库及它的全部 git worktree 内按 `-evidence-graph.yaml` 后缀搜索，读 `tree`，从图上的主线接着走。压缩摘要与图冲突时以图为准，因为图上的结论带证据。

### 3.4 收尾

主线判到终态后：`tree --status confirmed` 摘出结论，`render` 出两张图（主线视图、全图折叠已证伪），连同结论写进图所在目录的报告。

## 4. 图长什么样

`skills/evidence-graph/references/example.yaml` 是一份虚构的完整范例（七个节点、一次换线），`references/graph-format.md` 是字段、证据格式、建模约定和五条校验规则的说明。

## 5. 常见问题

**图变大之后看不清。** 先用 `viewer` 出 HTML 视图，它能自由缩放并点开看全文；要静态图就用 `render --focus` 只画主线相关节点、`--collapse-refuted` 把已证伪折叠成一个计数。节点多而层数浅时用 `--rankdir LR`，图的长宽比接近方形；默认的 TB 会把这类图排成扁长条。模型每轮读的是 `tree` 文本大纲。

**模型把推断写进了证据。** `check` 只检查有没有证据，不检查证据内容。看到 evidence 里出现「应该」「按理说」，让它把节点改回待查并去跑一条命令。

**中间态校验报错。** `add` / `set` / `switch` 保存后会顺手跑 check，错误只提示不阻断。比如刚把 F1 判证伪，依赖 F1 的 H1 还待查，R3 会报；下一步把 H1 也判掉，这条报错随之消除。

## 6. 目录

```
.claude-plugin/marketplace.json     marketplace 清单
.claude-plugin/plugin.json          插件清单
skills/evidence-graph/
  SKILL.md                          模型读的操作主线
  scripts/graph.py                  图的读写、校验、渲染
  scripts/viewer.py                 出可缩放、可搜索、点节点看全文的单文件 HTML
  scripts/test_graph.py             单元测试，标准库 unittest
  references/graph-format.md        字段、证据格式、校验规则
  references/example.yaml           虚构范例
README.md                           本文件，给人看的使用流程
LICENSE                             MIT
```
