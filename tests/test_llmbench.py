# -*- coding: utf-8 -*-
"""llmbench-cn 单元测试：python -m unittest discover -s tests -v

注意：所有会写数据文件的测试都必须把 cli.USER_DATA_FILE / cli.USER_DATA_DIR
重定向到临时目录，绝不能写到仓库里的 llmbench/data/providers.json
（否则测试会污染工作区，且 CI 上可能因只读权限失败）。
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llmbench  # noqa: E402
from llmbench import cli  # noqa: E402

FIB_OK = '''
```python
def fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
```
'''

FIB_BAD = '''
```python
def fib(n):
    return n  # 错的
```
'''


class TempUserDataMixin:
    """把用户数据层重定向到临时目录，避免测试写脏仓库/用户主目录。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = cli.USER_DATA_DIR
        self._old_file = cli.USER_DATA_FILE
        cli.USER_DATA_DIR = Path(self._tmp.name)
        cli.USER_DATA_FILE = Path(self._tmp.name) / "providers.json"
        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)

    def _restore(self):
        cli.USER_DATA_DIR = self._old_dir
        cli.USER_DATA_FILE = self._old_file


class TestSSE(unittest.TestCase):
    def test_parse_stream(self):
        lines = [
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"!"}}]}\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}\n',
            b'data: [DONE]\n',
        ]
        content, first_ms, usage = cli.parse_sse_lines(iter(lines), 0.0)
        self.assertEqual(content, "ok!")
        self.assertEqual(usage["completion_tokens"], 2)
        self.assertIsNotNone(first_ms)

    def test_parse_ignore_junk(self):
        lines = [b'event: ping\n', b'data: not-json\n',
                 b'data: {"choices":[{"delta":{"content":"a"}}]}\n']
        content, _, _ = cli.parse_sse_lines(iter(lines), 0.0)
        self.assertEqual(content, "a")

    def test_parse_usage_alternate_field_names(self):
        """部分网关返回 input_tokens/output_tokens，应被归一化。"""
        lines = [b'data: {"choices":[{"delta":{"content":"a"}}],'
                 b'"usage":{"input_tokens":5,"output_tokens":7}}\n']
        _, _, usage = cli.parse_sse_lines(iter(lines), 0.0)
        self.assertEqual(usage, {"prompt_tokens": 5, "completion_tokens": 7})


class TestChecks(unittest.TestCase):
    def test_fib_ok(self):
        self.assertTrue(cli.check_py_fib(FIB_OK))

    def test_fib_bad(self):
        self.assertFalse(cli.check_py_fib(FIB_BAD))

    def test_fib_no_code(self):
        self.assertFalse(cli.check_py_fib("没有代码"))

    def test_fib_lambda_accepted(self):
        """合法的非 def 写法也应被接受。"""
        code = "```python\nfib = lambda n: n if n < 2 else fib(n-1) + fib(n-2)\n```"
        self.assertTrue(cli.check_py_fib(code))

    def test_fib_dangerous_import_blocked(self):
        """模型若尝试 import os 执行命令，应被沙箱拦下（不执行、判失败）。"""
        code = ("```python\nimport os\n"
                "def fib(n):\n    os.system('echo pwned')\n    return n\n```")
        self.assertFalse(cli.check_py_fib(code))

    def test_json_keys(self):
        ok = '{"name": "张三", "city": "北京", "count": 5}'
        self.assertTrue(cli.check_json_keys(ok, {"name": "str", "city": "str", "count": "int"}))
        bad_type = '{"name": "张三", "city": "北京", "count": "五"}'
        self.assertFalse(cli.check_json_keys(bad_type, {"count": "int"}))

    def test_json_bool_is_not_int(self):
        """bool 是 int 的子类，但 count: true 不应算通过 int 校验。"""
        self.assertFalse(cli.check_json_keys('{"count": true}', {"count": "int"}))
        self.assertTrue(cli.check_json_keys('{"count": 5}', {"count": "int"}))

    def test_json_with_trailing_prose(self):
        """JSON 后面跟解释文字时也应能提取（原来贪婪正则会失败）。"""
        text = '```json\n{"name": "张三", "count": 5}\n```\n以上是抽取结果。'
        self.assertTrue(cli.check_json_keys(text, {"name": "str", "count": "int"}))

    def test_json_nested_object(self):
        text = '结果如下：{"name": "李四", "meta": {"k": [1,2]}, "count": 3} 完毕。'
        self.assertTrue(cli.check_json_keys(text, {"name": "str", "count": "int"}))

    def test_contains(self):
        self.assertTrue(cli.check_contains("答案是 14", {"keywords": ["14"]}))
        self.assertFalse(cli.check_contains("答案是 15", {"keywords": ["14"]}))

    def test_extract_code_block(self):
        self.assertIn("def fib", cli.extract_code_block(FIB_OK))
        self.assertEqual(cli.extract_code_block("print(1)"), "print(1)")

    def test_evaluate_empty_content_is_fail_not_unknown(self):
        """任务有校验但模型返回空内容 → 判未通过(False)，不是不可判定(None)。

        否则空回复会被排除在分母外，抬高通过率。
        """
        task = {"id": "math", "check": "contains", "check_params": {"keywords": ["14"]}}
        self.assertIs(cli.evaluate(task, ""), False)
        self.assertIs(cli.evaluate(task, "答案是 14"), True)

    def test_evaluate_no_check_returns_none(self):
        """没有客观校验的任务 → 不可判定(None)。"""
        self.assertIsNone(cli.evaluate({"id": "sql", "check": None}, "SELECT 1"))


class TestCostAndTokens(unittest.TestCase):
    def test_estimate_tokens(self):
        self.assertGreater(cli.estimate_tokens("你好世界"), 3)
        self.assertGreater(cli.estimate_tokens("hello world"), 1)

    def test_cost(self):
        provider = {"currency": "CNY", "models": [{"id": "m", "input": 2.0, "output": 3.0}]}
        usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
        text, _ = cli.calc_cost(provider, "m", usage, estimated=False)
        self.assertEqual(text, "¥0.0050")
        text_est, _ = cli.calc_cost(provider, "m", usage, estimated=True)
        self.assertTrue(text_est.endswith("≈"))

    def test_cost_no_price(self):
        provider = {"currency": "CNY", "models": [{"id": "m", "input": None, "output": None}]}
        text, _ = cli.calc_cost(provider, "m", {"prompt_tokens": 1, "completion_tokens": 1}, False)
        self.assertEqual(text, "见官网")

    def test_cost_usd_symbol(self):
        provider = {"currency": "USD", "models": [{"id": "m", "input": 1.0, "output": 1.0}]}
        text, _ = cli.calc_cost(provider, "m", {"prompt_tokens": 1000, "completion_tokens": 1000}, False)
        self.assertTrue(text.startswith("$"))


class TestPairs(unittest.TestCase):
    def _ns(self, provider, model=None):
        return type("NS", (), {"provider": provider, "model": model})()

    def test_pair_with_inline_key(self):
        pairs = cli.parse_pairs(self._ns(["deepseek:sk-abc"], ["deepseek-chat"]))
        self.assertEqual(pairs, [("deepseek", "deepseek-chat", "sk-abc")])

    def test_pair_default_model(self):
        pairs = cli.parse_pairs(self._ns(["qwen"]))
        self.assertEqual(pairs[0][0], "qwen")
        self.assertEqual(pairs[0][1], "qwen-plus")
        self.assertIsNone(pairs[0][2])

    def test_pair_empty_model_falls_back_to_default(self):
        """-m 传了空串时应回退到供应商默认模型，而不是用空模型名。"""
        pairs = cli.parse_pairs(self._ns(["qwen"], [""]))
        self.assertEqual(pairs[0][1], "qwen-plus")


class TestCommands(unittest.TestCase):
    def test_list_tasks(self):
        out = cli.cmd_list(type("NS", (), {"tasks": True, "json": False, "provider": None})())
        self.assertIn("code-fib", out)
        self.assertIn("执行验证", out)

    def test_list_json(self):
        out = cli.cmd_list(type("NS", (), {"tasks": False, "json": True, "provider": None})())
        data = json.loads(out)
        self.assertIn("providers", data)
        self.assertIn("deepseek", data["providers"])

    def test_list_provider_detail(self):
        out = cli.cmd_list(type("NS", (), {"tasks": False, "json": False, "provider": "deepseek"})())
        self.assertIn("deepseek-chat", out)
        self.assertIn("¥", out)

    def test_tasks_data(self):
        tasks = cli.load_json(cli.TASKS_FILE)["tasks"]
        ids = [t["id"] for t in tasks]
        self.assertEqual(len(ids), len(set(ids)))
        for t in tasks:
            self.assertIn("prompt", t)


class TestMarkdownReport(unittest.TestCase):
    def test_render_markdown(self):
        report = {"version": cli.VERSION, "generated_at": "2026-10-28 10:00",
                  "repeat": 3, "rows": [
                      {"pid": "deepseek", "model": "deepseek-chat", "task": "code-fib",
                       "runs": 3, "ok_runs": 3, "first_ms": 412, "total_ms": 2300,
                       "usage_in": 89, "usage_out": 128, "tps": 55.6,
                       "cost_str": "¥0.0007", "passed": "3/3"}]}
        md = cli.render_markdown(report)
        self.assertIn("| 供应商 |", md)
        self.assertIn("deepseek-chat", md)
        self.assertIn("55.6", md)


class TestRunValidation(unittest.TestCase):
    def test_repeat_zero_rejected(self):
        ns = type("NS", (), {"provider": ["deepseek"], "model": None, "task": ["math"],
                              "repeat": 0, "max_tokens": 0, "base_url": None,
                              "timeout": 10, "retries": 0, "json": True, "save": None,
                              "markdown": None, "verbose": False})()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit):
                cli.cmd_run(ns)

    def test_max_tokens_override(self):
        """--max-tokens 必须真正覆盖任务的 max_tokens（原实现解析了却没用）。"""
        task = {"id": "x", "prompt": "p", "max_tokens": 512}
        ns = type("NS", (), {"max_tokens": 99})()
        self.assertEqual(cli.resolve_max_tokens(task, ns), 99)
        ns0 = type("NS", (), {"max_tokens": 0})()
        self.assertEqual(cli.resolve_max_tokens(task, ns0), 512)


class TestDataLayer(TempUserDataMixin, unittest.TestCase):
    """用户数据层：refresh 不得写包目录，合并时保留内置价格。"""

    def test_refresh_writes_user_layer_not_package(self):
        providers = cli.load_providers()
        cli.write_user_providers({"gateway": {"base_url": "http://x/v1", "models": [
            {"id": "m1", "input": None, "output": None}]}})
        self.assertTrue(cli.USER_DATA_FILE.exists())
        self.assertFalse(cli.USER_DATA_FILE.samefile(cli.PACKAGE_DATA_FILE))
        # 内置数据文件内容未被改动
        self.assertIn("deepseek", cli.load_json(cli.PACKAGE_DATA_FILE)["providers"])

    def test_merge_keeps_curated_price(self):
        base = {"models": [{"id": "a", "input": 2.0, "output": 3.0, "desc": "内置"}]}
        over = {"models": [{"id": "a", "input": None, "output": None},
                           {"id": "b", "input": None, "output": None}]}
        merged = cli.merge_provider(base, over)
        by_id = {m["id"]: m for m in merged["models"]}
        self.assertEqual(by_id["a"]["input"], 2.0)   # 保留内置价格
        self.assertEqual(by_id["a"]["desc"], "内置")
        self.assertIn("b", by_id)

    def test_user_layer_overrides_base_url(self):
        cli.write_user_providers({"gateway": {"base_url": "http://my-gw/v1", "models": []}})
        providers = cli.load_providers()
        self.assertEqual(providers["gateway"]["base_url"], "http://my-gw/v1")

    def test_backup_created(self):
        cli.write_user_providers({"gateway": {"models": []}})
        cli.write_user_providers({"gateway": {"models": [{"id": "z"}]}})
        self.assertTrue((Path(str(cli.USER_DATA_FILE) + ".bak")).exists())


class TestExtractModelIds(unittest.TestCase):
    def test_openai_format(self):
        parsed = {"data": [{"id": "a"}, {"id": "b"}]}
        self.assertEqual(cli._extract_model_ids(parsed), ["a", "b"])

    def test_gateway_formats(self):
        self.assertEqual(cli._extract_model_ids({"models": ["x", "y"]}), ["x", "y"])
        self.assertEqual(cli._extract_model_ids([{"name": "n1"}]), ["n1"])
        self.assertEqual(cli._extract_model_ids({"result": [{"model": "m"}]}), ["m"])

    def test_dedup(self):
        self.assertEqual(cli._extract_model_ids({"data": [{"id": "a"}, {"id": "a"}]}), ["a"])


class TestDoctor(unittest.TestCase):
    def test_doctor_runs(self):
        ns = type("NS", (), {"provider": None})()
        out = cli.cmd_doctor(ns)
        self.assertIn("Python", out)
        self.assertIn("供应商", out)


class TestWeb(unittest.TestCase):
    """web 面板基础可用性:页面与 /api/list。"""

    def test_parser_has_web(self):
        ns = cli.build_parser().parse_args(["web", "--port", "0", "--no-open"])
        self.assertEqual(ns.command, "web")
        self.assertEqual(ns.port, 0)

    def test_parser_has_doctor(self):
        ns = cli.build_parser().parse_args(["doctor"])
        self.assertEqual(ns.command, "doctor")

    def test_page_and_list_api(self):
        import threading
        import urllib.request
        from llmbench import webui
        webui.Handler.port = 0
        srv = webui.ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5) as r:
                self.assertEqual(r.status, 200)
                self.assertIn("llmbench", r.read().decode("utf-8"))
            req = urllib.request.Request("http://127.0.0.1:%d/api/list" % port,
                                         data=b"{}", method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
                self.assertIn("providers", data["data"])
                self.assertIn("tasks", data["data"])
        finally:
            srv.shutdown()
            srv.server_close()


class TestCategoryData(unittest.TestCase):
    def test_category_field(self):
        data = cli.load_json(cli.DATA_FILE)
        for pid, p in data["providers"].items():
            self.assertIn("category", p, "provider %s 缺少 category" % pid)


class TestWebProviders(unittest.TestCase):
    def test_providers_api_has_category(self):
        from llmbench import webui
        res = webui.api_providers()
        self.assertTrue(res["ok"])
        for p in res["providers"].values():
            self.assertIn("category", p)


if __name__ == "__main__":
    unittest.main()
