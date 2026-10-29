# -*- coding: utf-8 -*-
"""端到端集成测试:本地 mock OpenAI 兼容网关,验证完整链路
(HTTP → SSE 流式解析 → 首 token → usage → 成本 → 客观校验)。

重要:测试把 cli.USER_DATA_FILE 指向临时目录，
refresh 只会写用户数据层，不会碰仓库里的 llmbench/data/providers.json。
"""
import contextlib
import http.server
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llmbench import cli  # noqa: E402

FIB_CODE = "def fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n"


class MockHandler(http.server.BaseHTTPRequestHandler):
    """可配置的 mock 网关。

    开关：
      - stream_reject: /chat/completions 对 stream=true 直接返回 400
      - no_usage:     不返回 usage（触发 token 估算路径）
      - flaky:        前 N 次返回 429（触发重试路径）
    """

    stream_reject = False
    no_usage = False
    flaky = 0

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode("utf-8"))

    def _sse(self, content, usage):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        mid = max(1, len(content) // 2)
        for chunk in (content[:mid], content[mid:]):
            time.sleep(0.05)
            obj = {"choices": [{"delta": {"content": chunk}}]}
            self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode("utf-8"))
        if usage:
            obj = {"choices": [{"delta": {}}], "usage": usage}
            self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")

    def do_GET(self):
        if self.path == "/v1/models":
            self._json(200, {"data": [{"id": "mock-a"}, {"id": "mock-b"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        prompt = (body.get("messages") or [{}])[-1].get("content", "")

        if MockHandler.flaky > 0:
            MockHandler.flaky -= 1
            self._json(429, {"error": {"message": "rate limited"}})
            return

        if body.get("stream") and MockHandler.stream_reject:
            # 部分网关不支持流式，应自动回退非流式
            self._json(400, {"error": {"message": "stream not supported"}})
            return

        if "斐波那契" in prompt or "fib" in prompt.lower():
            content = FIB_CODE
        elif "一半" in prompt:
            content = "14"
        else:
            content = "ok"
        usage = None if MockHandler.no_usage else {"prompt_tokens": 30, "completion_tokens": 25}
        if body.get("stream"):
            self._sse(content, usage)
        else:
            payload = {"model": body.get("model"),
                       "choices": [{"message": {"role": "assistant", "content": content}}]}
            if usage:
                payload["usage"] = usage
            self._json(200, payload)


class TestE2EMock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        # 把用户数据层重定向到临时目录：refresh 不得写仓库文件
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = cli.USER_DATA_DIR
        self._old_file = cli.USER_DATA_FILE
        cli.USER_DATA_DIR = Path(self._tmp.name)
        cli.USER_DATA_FILE = Path(self._tmp.name) / "providers.json"
        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)
        MockHandler.stream_reject = False
        MockHandler.no_usage = False
        MockHandler.flaky = 0

    def _restore(self):
        cli.USER_DATA_DIR = self._old_dir
        cli.USER_DATA_FILE = self._old_file

    def _run_cli(self, argv):
        """stdout / stderr 分开捕获。

        JSON 报告只保证在 stdout 上纯 JSON；重试提示之类的诊断信息走 stderr，
        混在一起会导致 JSON 解析失败。
        """
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc = cli.main(argv)
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def test_run_full_pipeline(self):
        base = "http://127.0.0.1:%d/v1" % self.port
        rc, out, _ = self._run_cli(["run", "-p", "deepseek:sk-test", "-m", "deepseek-chat",
                                    "--base-url", base, "-t", "code-fib,math", "--json"])
        self.assertEqual(rc, 0, out)
        report = json.loads(out)
        by_task = {r["task"]: r for r in report["rows"]}
        self.assertEqual(by_task["code-fib"]["passed"], "1/1")
        self.assertEqual(by_task["math"]["passed"], "1/1")
        self.assertIsNotNone(by_task["code-fib"]["first_ms"])
        self.assertIn("¥", by_task["code-fib"]["cost_str"])
        self.assertEqual(by_task["code-fib"]["usage_out"], 25)
        self.assertIsNotNone(by_task["code-fib"]["tps"])

    def test_run_falls_back_to_non_stream(self):
        """网关拒绝流式(400)时，应自动回退非流式并成功完成。"""
        MockHandler.stream_reject = True
        base = "http://127.0.0.1:%d/v1" % self.port
        rc, out, _ = self._run_cli(["run", "-p", "deepseek:sk-test", "-m", "deepseek-chat",
                                    "--base-url", base, "-t", "code-fib", "--json"])
        self.assertEqual(rc, 0, out)
        report = json.loads(out)
        self.assertEqual(report["rows"][0]["passed"], "1/1")
        self.assertEqual(report["rows"][0]["ok_runs"], 1)

    def test_run_estimates_tokens_when_usage_missing(self):
        MockHandler.no_usage = True
        base = "http://127.0.0.1:%d/v1" % self.port
        rc, out, _ = self._run_cli(["run", "-p", "deepseek:sk-test", "-m", "deepseek-chat",
                                    "--base-url", base, "-t", "math", "--json"])
        self.assertEqual(rc, 0, out)
        report = json.loads(out)
        row = report["rows"][0]
        self.assertTrue(row["estimated"])
        self.assertIn("≈", row["cost_str"])

    def test_run_retries_on_429(self):
        MockHandler.flaky = 1
        base = "http://127.0.0.1:%d/v1" % self.port
        rc, out, err = self._run_cli(["run", "-p", "deepseek:sk-test", "-m", "deepseek-chat",
                                      "--base-url", base, "-t", "math", "--retries", "2",
                                      "--json"])
        self.assertEqual(rc, 0, out)
        # 重试提示走 stderr，不污染 stdout 的 JSON
        report = json.loads(out)
        self.assertEqual(report["rows"][0]["passed"], "1/1")
        self.assertIn("重试", err)

    def test_refresh_against_mock(self):
        base = "http://127.0.0.1:%d/v1" % self.port
        rc, out, _ = self._run_cli(["refresh", "-p", "gateway", "--base-url", base,
                                    "-k", "sk-test"])
        self.assertEqual(rc, 0, out)
        self.assertIn("mock-a", out)
        self.assertIn("mock-b", out)
        # 写入的应是用户数据层，而不是仓库里的 providers.json
        self.assertTrue(cli.USER_DATA_FILE.exists())
        data = json.loads(cli.USER_DATA_FILE.read_text(encoding="utf-8"))
        ids = [m["id"] for m in data["providers"]["gateway"]["models"]]
        self.assertIn("mock-a", ids)
        # 仓库文件未被污染
        repo_data = json.loads(cli.PACKAGE_DATA_FILE.read_text(encoding="utf-8"))
        self.assertEqual(repo_data["providers"]["gateway"].get("models"), [])

    def test_refresh_dry_run_does_not_write(self):
        base = "http://127.0.0.1:%d/v1" % self.port
        rc, out, _ = self._run_cli(["refresh", "-p", "gateway", "--base-url", base,
                                    "-k", "sk-test", "--dry-run"])
        self.assertEqual(rc, 0, out)
        self.assertIn("预演", out)
        self.assertFalse(cli.USER_DATA_FILE.exists())

    def test_markdown_export(self):
        base = "http://127.0.0.1:%d/v1" % self.port
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "report.md"
            rc, _, _ = self._run_cli(["run", "-p", "deepseek:sk-test", "-m", "deepseek-chat",
                                       "--base-url", base, "-t", "code-fib",
                                       "--markdown", str(md)])
            self.assertEqual(rc, 0)
            self.assertTrue(md.exists())
            text = md.read_text(encoding="utf-8")
            self.assertIn("| 供应商 |", text)
            self.assertIn("deepseek-chat", text)

    def test_verbose_does_not_corrupt_json(self):
        """--verbose 的明细必须走 stderr，否则 stdout 的 JSON 无法解析。"""
        base = "http://127.0.0.1:%d/v1" % self.port
        rc, out, err = self._run_cli(["run", "-p", "deepseek:sk-test", "-m", "deepseek-chat",
                                      "--base-url", base, "-t", "code-fib",
                                      "--json", "--verbose"])
        self.assertEqual(rc, 0)
        json.loads(out)          # stdout 必须是纯 JSON
        self.assertIn("首token", err)  # 明细在 stderr


if __name__ == "__main__":
    unittest.main()
