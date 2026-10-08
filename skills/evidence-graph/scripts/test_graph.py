"""graph.py 的单元测试。

每条校验规则各一正一反，另加 tree 与 render 的冒烟。只依赖标准库 unittest 与 PyYAML，
CI 里直接 `python3 -m unittest scripts/test_graph.py` 即可。
"""
import io
import json
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

    # ---------- 收尾 ----------

    def test_close_requires_terminal_main_line_and_reason(self):
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "h1")
        self.cli("close", "--reason", "结论", expect_ok=False)  # 没有主线
        self.cli("switch", "--to", "H1", "--reason", "起点")
        self.cli("close", "--reason", "结论", expect_ok=False)  # 主线仍待查
        self.cli("set", "H1", "--status", "confirmed", "--evidence", "e")
        self.cli("close", expect_ok=False)                      # 缺 --reason
        self.cli("close", "--reason", "  ", expect_ok=False)    # reason 为空白
        self.cli("close", "--reason", "H1 已证实")

    def test_closed_graph_passes_check_and_tree_shows_conclusion(self):
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "h1")
        self.cli("switch", "--to", "H1", "--reason", "起点")
        self.cli("set", "H1", "--status", "confirmed", "--evidence", "e")
        _, out, _ = self.cli("check", expect_ok=False)
        self.assertIn("R4 主线 H1 已到终态", out)
        self.cli("close", "--reason", "H1 已证实，根因是连接池上限 10")
        self.cli("check")
        _, out, _ = self.cli("tree")
        self.assertIn("已收尾", out)
        self.assertIn("H1 已证实，根因是连接池上限 10", out)

    def test_switch_after_close_restores_main_line_check(self):
        self.cli("add", "H1", "--kind", "hypothesis", "--claim", "h1")
        self.cli("switch", "--to", "H1", "--reason", "起点")
        self.cli("set", "H1", "--status", "confirmed", "--evidence", "e")
        self.cli("close", "--reason", "H1 已证实")
        self.cli("add", "H2", "--kind", "hypothesis", "--claim", "同一现象再次出现")
        self.cli("switch", "--to", "H2", "--reason", "现象复现，接着查")
        self.assertNotIn("closed", graph.load(self.path))
        self.cli("set", "H2", "--status", "refuted", "--evidence", "e2")
        _, out, _ = self.cli("check", expect_ok=False)
        self.assertIn("R4 主线 H2 已到终态", out)

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

    # ---------- HTML 视图 ----------

    def test_viewer_reports_missing_graphviz(self):
        """本机没有 dot 时，viewer 打印一行安装提示并以退出码 1 结束，不抛异常。"""
        self.cli("add", "F1", "--kind", "fact", "--claim", "f1")
        script = os.path.join(os.path.dirname(os.path.abspath(graph.__file__)), "viewer.py")
        empty = os.path.join(self.tmp, "no-dot")
        os.makedirs(empty)
        r = subprocess.run([sys.executable, script, self.path], capture_output=True, text=True,
                           env={**os.environ, "PATH": empty})
        self.assertEqual(r.returncode, 1)
        self.assertIn("没有 graphviz（dot）", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    @unittest.skipUnless(shutil.which("dot"), "graphviz 未安装")
    def test_viewer_without_main_line_shows_full_view(self):
        """主线未设时 viewer 照常出 HTML：只含全图，主线按钮置灰。"""
        self.cli("add", "F1", "--kind", "fact", "--claim", "f1")
        self.cli("viewer")
        with open(graph.viewer_path(self.path), encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("<svg", page)
        self.assertIn('<button id="v-focus" disabled>主线未设</button>', page)

    @unittest.skipUnless(shutil.which("dot"), "graphviz 未安装")
    def test_viewer_with_main_line_enables_focus_view(self):
        self.cli("add", "F1", "--kind", "fact", "--claim", "f1")
        self.cli("switch", "--to", "F1", "--reason", "起点")
        self.cli("viewer")
        with open(graph.viewer_path(self.path), encoding="utf-8") as fh:
            page = fh.read()
        # 节点数含目标框
        self.assertIn('<button id="v-focus">主线 F1（2 节点）</button>', page)

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


# SKILL.md 与本文件的相对位置：scripts/ 的上一级
SKILL_MD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "SKILL.md")


def skill_snippet(start: str, end: str) -> str:
    """从 SKILL.md 取出以 start 开头、到首个含 end 的行为止的连续行，测试的就是文档里的原文。"""
    with open(SKILL_MD, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    i = next(k for k, line in enumerate(lines) if line.startswith(start))
    j = next(k for k in range(i, len(lines)) if end in lines[k])
    return "\n".join(lines[i:j + 1]) + "\n"


class SkillSnippetTestCase(unittest.TestCase):
    """SKILL.md 里两段 shell 的行为：graph.py 路径解析与找图。依赖 bash、git、find。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_snippet(self, script: str, cwd: str | None = None, env: dict | None = None):
        path = os.path.join(self.tmp, "snippet.sh")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        return subprocess.run(["bash", path], cwd=cwd, env=env, capture_output=True, text=True)

    # ---------- graph.py 路径解析 ----------

    def resolve(self, home: str, claude_output: str | None, skill_dir: str | None = None) -> str:
        """在伪造的 HOME 与 claude 命令下运行路径解析段，返回打印出的 G。

        claude_output 为 None 时伪造的 claude 以退出码 127 结束，模拟本机没有 claude 命令。
        """
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        fake = os.path.join(bindir, "claude")
        with open(fake, "w", encoding="utf-8") as fh:
            if claude_output is None:
                fh.write("#!/bin/sh\nexit 127\n")
            else:
                fh.write("#!/bin/sh\ncat <<'EOF'\n" + claude_output + "\nEOF\n")
        os.chmod(fake, 0o755)
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_SKILL_DIR"}
        env["HOME"] = home
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        if skill_dir is not None:
            env["CLAUDE_SKILL_DIR"] = skill_dir
        script = skill_snippet('G="${CLAUDE_SKILL_DIR}', "未找到 graph.py") + 'echo "G=[$G]"\n'
        out = self.run_snippet(script, env=env).stdout
        return out[out.rindex("G=[") + 3:out.rindex("]")]

    def make_copies(self) -> tuple[str, str, str]:
        """伪造 HOME：一份过期缓存，一份同步副本；同步副本的 mtime 置为 1970 年，与 claude.ai 同步下来的文件一致。"""
        home = os.path.join(self.tmp, "home")
        stale = os.path.join(home, ".claude/plugins/cache/evidence-graph/evidence-graph/aaa")
        synced = os.path.join(home, ".claude/plugins/synced/acct/evidence-graph~g2")
        for root in (stale, synced):
            d = os.path.join(root, "skills/evidence-graph/scripts")
            os.makedirs(d)
            open(os.path.join(d, "graph.py"), "w").close()
        os.utime(os.path.join(synced, "skills/evidence-graph/scripts/graph.py"), (0, 0))
        return home, stale, synced

    def test_resolve_uses_substituted_skill_dir(self):
        home, _, _ = self.make_copies()
        skill = os.path.join(self.tmp, "skill")
        os.makedirs(os.path.join(skill, "scripts"))
        open(os.path.join(skill, "scripts/graph.py"), "w").close()
        self.assertEqual(self.resolve(home, None, skill_dir=skill), skill + "/scripts/graph.py")

    def test_resolve_prefers_enabled_plugin_over_stale_cache(self):
        home, _, synced = self.make_copies()
        listing = json.dumps([
            {"id": "other@x", "enabled": True, "installPath": "/nowhere"},
            {"id": "evidence-graph@synced", "enabled": True, "installPath": synced},
        ])
        self.assertEqual(self.resolve(home, listing), synced + "/skills/evidence-graph/scripts/graph.py")

    def test_resolve_searches_when_claude_is_unavailable(self):
        home, stale, synced = self.make_copies()
        g = self.resolve(home, None)
        self.assertIn(g, (stale + "/skills/evidence-graph/scripts/graph.py",
                          synced + "/skills/evidence-graph/scripts/graph.py"))

    def test_resolve_skips_trashed_copies(self):
        """claude.ai 同步移除或更新插件后，被替换的副本留在 .trash 下，搜索时跳过。"""
        home = os.path.join(self.tmp, "trash-home")
        for root in (".claude/plugins/.trash/1-a/evidence-graph/skills/evidence-graph/scripts",
                     ".claude/skills/.trash/2-b/evidence-graph/scripts"):
            os.makedirs(os.path.join(home, root))
            open(os.path.join(home, root, "graph.py"), "w").close()
        self.assertEqual(self.resolve(home, None), "")

    def test_resolve_reports_missing_install(self):
        home = os.path.join(self.tmp, "empty-home")
        os.makedirs(home)
        self.assertEqual(self.resolve(home, "[]"), "")

    # ---------- 找图 ----------

    def find_graphs(self, cwd: str) -> list[str]:
        script = skill_snippet("R=$(git rev-parse", "python3 -c")
        out = self.run_snippet(script, cwd=cwd).stdout
        return [os.path.basename(line) for line in out.splitlines() if line]

    def git(self, *args: str, cwd: str) -> None:
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                       cwd=cwd, check=True, capture_output=True)

    def test_find_covers_worktrees_and_dedups_symlinks(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, "docs/evidence-graph"))
        self.git("init", "-q", "-b", "main", cwd=repo)
        self.git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
        open(os.path.join(repo, "docs/evidence-graph/a-evidence-graph.yaml"), "w").close()
        sib = os.path.join(self.tmp, "sib")
        self.git("worktree", "add", "-q", sib, "-b", "sib", cwd=repo)
        os.makedirs(os.path.join(sib, "docs/evidence-graph"))
        open(os.path.join(sib, "docs/evidence-graph/b-evidence-graph.yaml"), "w").close()
        os.makedirs(os.path.join(repo, "node_modules/x"))
        open(os.path.join(repo, "node_modules/x/skip-evidence-graph.yaml"), "w").close()
        # 两条软链指向同一张图，结果里只出现一次
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        open(os.path.join(other, "c-evidence-graph.yaml"), "w").close()
        os.symlink(other, os.path.join(repo, "link1"))
        os.makedirs(os.path.join(repo, "sub/dir"))
        os.symlink(other, os.path.join(repo, "sub/link2"))
        want = ["a-evidence-graph.yaml", "b-evidence-graph.yaml", "c-evidence-graph.yaml"]
        self.assertEqual(sorted(self.find_graphs(os.path.join(repo, "sub/dir"))), want)
        # 从兄弟 worktree 调用同样覆盖主仓库目录，经它的软链可达的 c 也在内
        self.assertEqual(sorted(self.find_graphs(sib)), want)

    def test_find_outside_git_searches_current_directory(self):
        plain = os.path.join(self.tmp, "plain/docs/evidence-graph")
        os.makedirs(plain)
        open(os.path.join(plain, "x-evidence-graph.yaml"), "w").close()
        self.assertEqual(self.find_graphs(os.path.join(self.tmp, "plain")), ["x-evidence-graph.yaml"])


if __name__ == "__main__":
    unittest.main()
