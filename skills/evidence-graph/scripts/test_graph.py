"""graph.py 的单元测试。

每条校验规则各一正一反，另加 tree 与 render 的冒烟。只依赖标准库 unittest 与 PyYAML，
CI 里直接 `python3 -m unittest scripts/test_graph.py` 即可。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph  # noqa: E402


class GraphTestCase(unittest.TestCase):
    """每个用例在临时目录里新建一份图，避免互相污染。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "g.yaml")
        self.cli("init", "--goal", "下单接口 P99 延迟升高")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def cli(self, *args, expect_ok=True):
        """调用 graph.main，返回 (退出码, stdout, stderr)。"""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = graph.main([self.path, *args])
        if expect_ok:
            self.assertEqual(code, 0, msg=err.getvalue() + out.getvalue())
        else:
            self.assertNotEqual(code, 0, msg="预期失败，实际成功：" + out.getvalue())
        return code, out.getvalue(), err.getvalue()

    # ---------- add / set 基本流 ----------

    def test_add_and_confirm_fact(self):
        self.cli("add", "F1", "--kind", "fact", "--claim", "P99 统计包含获取连接的等待时间",
                 "--criterion", "读到计时起点那一行")
        self.cli("set", "F1", "--status", "confirmed", "--evidence", "metrics/middleware.py:48 start = time.monotonic()")
        self.cli("check")

    def test_confirmed_requires_evidence(self):
        self.cli("add", "F1", "--kind", "fact", "--claim", "x")
        self.cli("set", "F1", "--status", "confirmed", expect_ok=False)

    def test_refuted_requires_evidence(self):
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "DNS 缓存定时刷新阻塞请求")
        self.cli("set", "H1", "--status", "refuted", expect_ok=False)
        self.cli("set", "H1", "--status", "refuted", "--evidence", "关闭刷新后 5 轮 P99 为 262 ms")

    def test_blocked_requires_needs(self):
        self.cli("add", "H3", "--kind", "hypothesis", "--claim", "生产网络重传放大尾延迟")
        self.cli("set", "H3", "--status", "blocked", expect_ok=False)
        self.cli("set", "H3", "--status", "blocked", "--needs", "生产环境抓包")

    def test_experiment_done_requires_evidence(self):
        self.cli("add", "E1", "--kind", "experiment", "--claim", "连接池上限设为 50 压测 5 轮",
                 "--command", "loadtest run --scenario checkout --rounds 5")
        self.cli("set", "E1", "--status", "done", expect_ok=False)
        self.cli("set", "E1", "--status", "done", "--evidence", "5 轮 P99 均在 130 ms 以内，report/loadtest-0413.json")
        # 断言类节点不能用 done，实验类节点不能用 confirmed
        self.cli("add", "F1", "--kind", "fact", "--claim", "x")
        self.cli("set", "F1", "--status", "done", "--evidence", "y", expect_ok=False)
        self.cli("set", "E1", "--status", "confirmed", "--evidence", "y", expect_ok=False)

    # ---------- 依赖与环 ----------

    def test_unknown_dependency_rejected(self):
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "x", "--depends-on", "F9", expect_ok=False)

    def test_cycle_rejected_by_check(self):
        self.cli("add", "A", "--kind", "fact", "--claim", "a")
        self.cli("add", "B", "--kind", "fact", "--claim", "b", "--depends-on", "A")
        # 直接改文件制造环，绕过 add 的守卫，验证 check 能抓到
        data = graph.load(self.path)
        data["nodes"]["A"]["depends_on"] = ["B"]
        graph.save(self.path, data)
        self.cli("check", expect_ok=False)

    def test_open_node_must_not_depend_on_refuted(self):
        self.cli("add", "F1", "--kind", "fact", "--claim", "f")
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "h", "--depends-on", "F1")
        self.cli("set", "F1", "--status", "refuted", "--evidence", "反例")
        self.cli("check", expect_ok=False)

    # ---------- 报错文字 ----------

    def test_check_messages_state_the_status(self):
        """check 的报错直接陈述节点状态与缺少的字段。"""
        self.cli("add", "F1", "--kind", "fact", "--claim", "f1")
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "h1")
        self.cli("add", "F2", "--kind", "fact", "--claim", "f2")
        self.cli("add", "H2", "--kind", "hypothesis", "--claim", "h2", "--depends-on", "F2")
        self.cli("set", "F2", "--status", "refuted", "--evidence", "反例")
        data = graph.load(self.path)
        # 绕过 set 的守卫，直接制造缺字段的终态节点
        data["nodes"]["F1"]["status"] = "confirmed"
        data["nodes"]["H1"]["status"] = "blocked"
        graph.save(self.path, data)
        _, out, _ = self.cli("check", expect_ok=False)
        self.assertIn("R2 F1: 状态为已证实，缺少 evidence", out)
        self.assertIn("R2 H1: 状态为受阻，缺少 needs（写明缺什么才能继续）", out)
        self.assertIn("R3 H2: 状态为待查，依赖的 F2 已证伪；把 H2 判证伪，或去掉这条依赖", out)

    # ---------- 主线与换线 ----------

    def test_switch_requires_terminal_from(self):
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "h1")
        self.cli("add", "H2", "--kind", "hypothesis", "--claim", "h2")
        self.cli("switch", "--to", "H1", "--reason", "起点")
        # H1 仍是 open，不许换线
        self.cli("switch", "--to", "H2", "--reason", "想试试", expect_ok=False)
        self.cli("set", "H1", "--status", "refuted", "--evidence", "实测不成立")
        self.cli("switch", "--to", "H2", "--reason", "H1 已证伪")
        self.cli("check")

    def test_switch_requires_reason_and_open_target(self):
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "h1")
        self.cli("switch", "--to", "H1", expect_ok=False)
        self.cli("set", "H1", "--status", "blocked", "--needs", "生产环境抓包")
        self.cli("switch", "--to", "H1", "--reason", "r", expect_ok=False)

    def test_main_line_must_match_last_switch(self):
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "h1")
        self.cli("add", "H2", "--kind", "hypothesis", "--claim", "h2")
        self.cli("switch", "--to", "H1", "--reason", "起点")
        data = graph.load(self.path)
        data["main_line"] = "H2"  # 绕过 switch 直接改主线
        graph.save(self.path, data)
        self.cli("check", expect_ok=False)

    def test_terminal_main_line_flagged(self):
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "h1")
        self.cli("switch", "--to", "H1", "--reason", "起点")
        self.cli("set", "H1", "--status", "refuted", "--evidence", "e")
        # 主线已到终态且未换线：check 报错，提示换线或收尾
        self.cli("check", expect_ok=False)

    # ---------- 重开 ----------

    def test_reopen_refuted_requires_new_evidence(self):
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "h1")
        self.cli("set", "H1", "--status", "refuted", "--evidence", "e1")
        self.cli("set", "H1", "--status", "open", expect_ok=False)
        self.cli("set", "H1", "--status", "open", "--new-evidence", "换了参数后现象变了")
        self.cli("check")

    # ---------- ready / tree / render ----------

    def test_ready_lists_only_satisfied_open_nodes(self):
        self.cli("add", "F1", "--kind", "fact", "--claim", "f1")
        self.cli("add", "F2", "--kind", "fact", "--claim", "f2")
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "h1", "--depends-on", "F1")
        self.cli("add", "H2", "--kind", "hypothesis", "--claim", "h2", "--depends-on", "F2")
        self.cli("set", "F1", "--status", "confirmed", "--evidence", "e")
        _, out, _ = self.cli("ready")
        self.assertIn("H1", out)
        self.assertIn("F2", out)
        self.assertNotIn("H2", out)

    def test_tree_shows_status_and_main_line(self):
        self.cli("add", "F1", "--kind", "fact", "--claim", "f1")
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "h1", "--depends-on", "F1")
        self.cli("switch", "--to", "H1", "--reason", "起点")
        self.cli("set", "F1", "--status", "confirmed", "--evidence", "metrics/middleware.py:48")
        _, out, _ = self.cli("tree")
        self.assertIn("主线", out)
        self.assertIn("已证实", out)
        self.assertIn("metrics/middleware.py:48", out)
        self.assertIn("下单接口 P99 延迟升高", out)

    def test_render_mermaid_and_dot(self):
        self.cli("add", "F1", "--kind", "fact", "--claim", "f1")
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "h1", "--depends-on", "F1")
        self.cli("add", "H2", "--kind", "hypothesis", "--claim", "h2")
        self.cli("set", "H2", "--status", "refuted", "--evidence", "e")
        _, mm, _ = self.cli("render", "--format", "mermaid")
        self.assertTrue(mm.lstrip().startswith("graph TD"), mm)
        self.assertIn("H1 --> F1", mm)
        _, dot, _ = self.cli("render", "--format", "dot")
        self.assertIn("digraph", dot)
        self.assertIn("H1 -> F1", dot)
        # 折叠已证伪：H2 折叠进计数
        _, dot2, _ = self.cli("render", "--format", "dot", "--collapse-refuted")
        self.assertNotIn('H2 [', dot2)
        self.assertIn("已证伪 1", dot2)

    def test_render_focus_keeps_only_main_line_subgraph(self):
        self.cli("add", "F1", "--kind", "fact", "--claim", "f1")
        self.cli("add", "F2", "--kind", "fact", "--claim", "f2")
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "h1", "--depends-on", "F1")
        self.cli("add", "H2", "--kind", "hypothesis", "--claim", "h2", "--depends-on", "F2")
        self.cli("switch", "--to", "H1", "--reason", "起点")
        _, dot, _ = self.cli("render", "--format", "dot", "--focus")
        self.assertIn("H1 [", dot)
        self.assertIn("F1 [", dot)
        self.assertNotIn("H2 [", dot)
        self.assertNotIn("F2 [", dot)

    @unittest.skipUnless(shutil.which("dot"), "graphviz 未安装")
    def test_render_svg_via_graphviz(self):
        self.cli("add", "F1", "--kind", "fact", "--claim", "f1")
        svg = os.path.join(self.tmp, "g.svg")
        self.cli("render", "--format", "dot", "--out", svg)
        with open(svg, encoding="utf-8") as fh:
            self.assertIn("<svg", fh.read())

    # ---------- HTML 视图自动刷新 ----------

    def test_refresh_warns_when_graphviz_missing(self):
        """HTML 已存在而本机缺 dot：改图照常成功，stderr 给出提示，HTML 保持原样。
        HTML 不存在时不提示，因为这张图本来就不需要视图。"""
        with mock.patch.object(graph.shutil, "which", return_value=None):
            _, _, err = self.cli("add", "F1", "--kind", "fact", "--claim", "f1")
            self.assertNotIn("graphviz", err)
            html_path = graph.viewer_path(self.path)
            with open(html_path, "w", encoding="utf-8") as fh:
                fh.write("stale")
            _, _, err = self.cli("add", "F2", "--kind", "fact", "--claim", "f2")
        self.assertIn("缺 graphviz", err)
        with open(html_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "stale")


if __name__ == "__main__":
    unittest.main()
