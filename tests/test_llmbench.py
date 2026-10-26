# -*- coding: utf-8 -*-
"""llmbench-cn 单元测试：python -m unittest discover -s tests -v"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llmbench  # noqa: E402

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


class TestSSE(unittest.TestCase):
    def test_parse_stream(self):
        lines = [
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"!"}}]}\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}\n',
            b'data: [DONE]\n',
        ]
        content, first_ms, usage = llmbench.parse_sse_lines(iter(lines), 0.0)
        self.assertEqual(content, "ok!")
        self.assertEqual(usage["completion_tokens"], 2)
        self.assertIsNotNone(first_ms)

    def test_parse_ignore_junk(self):
        lines = [b'event: ping\n', b'data: not-json\n',
                 b'data: {"choices":[{"delta":{"content":"a"}}]}\n']
        content, _, _ = llmbench.parse_sse_lines(iter(lines), 0.0)
        self.assertEqual(content, "a")


class TestChecks(unittest.TestCase):
    def test_fib_ok(self):
        self.assertTrue(llmbench.check_py_fib(FIB_OK))

    def test_fib_bad(self):
        self.assertFalse(llmbench.check_py_fib(FIB_BAD))

    def test_fib_no_code(self):
        self.assertFalse(llmbench.check_py_fib("没有代码"))

    def test_json_keys(self):
        ok = '{"name": "张三", "city": "北京", "count": 5}'
        self.assertTrue(llmbench.check_json_keys(ok, {"name": "str", "city": "str", "count": "int"}))
        bad_type = '{"name": "张三", "city": "北京", "count": "五"}'
        self.assertFalse(llmbench.check_json_keys(bad_type, {"count": "int"}))

    def test_contains(self):
        self.assertTrue(llmbench.check_contains("答案是 14", {"keywords": ["14"]}))
        self.assertFalse(llmbench.check_contains("答案是 15", {"keywords": ["14"]}))

    def test_extract_code_block(self):
        self.assertIn("def fib", llmbench.extract_code_block(FIB_OK))
        self.assertEqual(llmbench.extract_code_block("print(1)"), "print(1)")


class TestCostAndTokens(unittest.TestCase):
    def test_estimate_tokens(self):
        self.assertGreater(llmbench.estimate_tokens("你好世界"), 3)
        self.assertGreater(llmbench.estimate_tokens("hello world"), 1)

    def test_cost(self):
        provider = {"currency": "CNY", "models": [{"id": "m", "input": 2.0, "output": 3.0}]}
        usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
        text, _ = llmbench.calc_cost(provider, "m", usage, estimated=False)
        self.assertEqual(text, "¥0.0050")
        text_est, _ = llmbench.calc_cost(provider, "m", usage, estimated=True)
        self.assertTrue(text_est.endswith("≈"))

    def test_cost_no_price(self):
        provider = {"currency": "CNY", "models": [{"id": "m", "input": None, "output": None}]}
        text, _ = llmbench.calc_cost(provider, "m", {"prompt_tokens": 1, "completion_tokens": 1}, False)
        self.assertEqual(text, "见官网")


class TestPairs(unittest.TestCase):
    def _ns(self, provider, model=None):
        return type("NS", (), {"provider": provider, "model": model})()

    def test_pair_with_inline_key(self):
        pairs = llmbench.parse_pairs(self._ns(["deepseek:sk-abc"], ["deepseek-chat"]))
        self.assertEqual(pairs, [("deepseek", "deepseek-chat", "sk-abc")])

    def test_pair_default_model(self):
        pairs = llmbench.parse_pairs(self._ns(["qwen"]))
        self.assertEqual(pairs[0][0], "qwen")
        self.assertEqual(pairs[0][1], "qwen-plus")
        self.assertIsNone(pairs[0][2])


class TestCommands(unittest.TestCase):
    def test_list_tasks(self):
        out = llmbench.cmd_list(type("NS", (), {"tasks": True, "json": False})())
        self.assertIn("code-fib", out)
        self.assertIn("py_fib" in out and "执行验证" or "code-fib", out)

    def test_list_json(self):
        out = llmbench.cmd_list(type("NS", (), {"tasks": False, "json": True})())
        data = json.loads(out)
        self.assertIn("providers", data)
        self.assertIn("deepseek", data["providers"])

    def test_tasks_data(self):
        tasks = llmbench.load_json(llmbench.TASKS_FILE)["tasks"]
        ids = [t["id"] for t in tasks]
        self.assertEqual(len(ids), len(set(ids)))
        for t in tasks:
            self.assertIn("prompt", t)


if __name__ == "__main__":
    unittest.main()
