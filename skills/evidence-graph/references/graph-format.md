# 证据图格式与校验规则

## 顶层字段

| 字段 | 含义 |
| --- | --- |
| `goal` | 这次排查要回答的问题，一句话 |
| `created` | 建图时间 |
| `main_line` | 当前主线节点编号，只能由 `switch` 改 |
| `nodes` | 节点表，键是编号 |
| `switches` | 换线记录，每条含 `at`、`from`、`from_status`、`to`、`reason` |
| `closed` | 收尾记录，含 `at`、`reason`（一句话结论）；由 `close` 写入，`switch` 清除；没有收尾时不出现 |

## 节点字段

| 字段 | 含义 | 何时必填 |
| --- | --- | --- |
| `kind` | `fact` 事实 / `hypothesis` 假设 / `experiment` 实验 | 总是 |
| `claim` | 断言原文，一句可判定的话；实验写「跑什么、看什么」 | 总是 |
| `status` | 断言类：`open` 待查 / `confirmed` 已证实 / `refuted` 已证伪 / `blocked` 受阻；实验类：`open` / `done` 已执行 / `blocked` | 总是 |
| `criterion` | 怎样算证实或证伪 | 建议总是写 |
| `command` | 实验跑的命令 | 实验类 |
| `depends_on` | 依赖的节点编号 | 有依赖时 |
| `evidence` | 依据列表，格式见下 | confirmed / refuted / done |
| `needs` | 缺什么才能继续 | blocked |
| `history` | 状态变更记录，脚本自动维护 | 自动 |

## 证据格式

只收两种：

- 读到的代码：`文件:行号 摘录`，例如 `metrics/middleware.py:48 start = time.monotonic()`
- 跑过的命令：`命令 -> 输出摘录`，例如 `curl -s localhost:8080/debug/config -> http_pool.max_size: 10`

压测或 CI 的结果写报告文件与指标字段，例如 `report/loadtest-0412.json p99_ms=262`。推断、回忆、文档里的推荐值都不是证据。

## 建模约定

- 一个节点一条断言。「A 且 B」拆成两个节点。
- 假设依赖它的判定实验：`H2 depends_on [F2, E2]`。这样 `ready` 列出的永远是下一步的具体动作（先跑 E2），E2 判 done 之后 H2 才可判。
- 事实节点记「系统现在是什么样」，假设节点记「为什么会这样」，实验节点记「跑什么来分辨」。
- 受阻节点的 `needs` 写具体缺什么（哪份抓包、哪台机器、哪个人的答复），不写「待定」。
- 编号前缀 F / H / E 加序号，立了不改，编号不复用。

## 校验规则（`check`）

| 编号 | 规则 | 违反时的典型场景 |
| --- | --- | --- |
| R0 | kind、status 在词表内；claim 非空；依赖的节点存在 | 手改 YAML 写错 |
| R1 | 依赖无环 | A 依赖 B、B 又依赖 A |
| R2 | confirmed / done / refuted 必有 evidence；blocked 必有 needs | 凭印象判状态 |
| R3 | 待查节点不能依赖已证伪节点 | 前提被推翻了，建在它上面的假设还挂着 |
| R4 | 主线指向待查节点（已用 `close` 收尾的图除外）；主线与最后一次换线的 `to` 一致；换线时 `from` 已到终态；换线有 reason | 主线到终态既没换线也没收尾；绕过 switch 手改 main_line；没走到底就换方向 |
| R5 | 曾被证伪的节点重开必须带新证据 | 过几轮又把老假设当新想法提出来 |

改图命令（add / set / switch / close）保存后会顺手跑一遍 check，错误打到 stderr，命令照常完成，因为有些中间态是合法过渡（刚把事实判证伪，依赖它的假设下一步才跟着判）。单独跑 `check` 时不通过退出码为 1。

## 投影

| 命令 | 用途 |
| --- | --- |
| `tree` | 按依赖缩进的文本大纲，主线优先，重复出现的节点写「（见 ID）」；模型每轮开工前读这个 |
| `tree --status confirmed` | 只列已证实，收尾摘结论用 |
| `ready` / `ready --main` | 待查且依赖全满足的节点；`--main` 只看主线子树 |
| `render --format dot --focus` | 只画主线及其祖先与依赖 |
| `render --format dot --collapse-refuted` | 已证伪节点折叠成一个计数 |
| `render --format mermaid` | 贴进 markdown 用 |
| `render --out x.svg` / `x.png` | 需要 graphviz；png 由 SVG 经 rsvg-convert 转出，本机没有 rsvg-convert 时用 `dot -Tpng` |
