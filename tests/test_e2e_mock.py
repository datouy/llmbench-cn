# -*- coding: utf-8 -*-
"""端到端集成测试:本地 mock OpenAI 兼容网关,验证完整链路
(HTTP → SSE 流式解析 → 首 token → usage → 成本 → 客观校验)。"""

import contextlib
import http.server
import io
import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llmbench import cli  # noqa: E402

FIB_CODE = "def fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n"


class MockHandler(http.server.BaseHTTPRequestHandler):
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
        if "斐波那契" in prompt or "fib" in prompt.lower():
            content = FIB_CODE
        elif "一半" in prompt:
            content = "14"
        else:
            content = "ok"
        usage = {"prompt_tokens": 30, "completion_tokens": 25}
        if body.get("stream"):
            self._sse(content, usage)
        else:
            self._json(200, {"model": body.get("model"),
                             "choices": [{"message": {"role": "assistant", "content": content}}],
                             "usage": usage})


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

    def _run_cli(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def test_run_full_pipeline(self):
        base = "http://127.0.0.1:%d/v1" % self.port
        rc, out = self._run_cli(["run", "-p", "deepseek:sk-test", "-m", "deepseek-chat",
                                 "--base-url", base, "-t", "code-fib,math", "--json"])
        self.assertEqual(rc, 0, out)
        report = json.loads(out)
        by_task = {r["task"]: r for r in report["rows"]}
        self.assertEqual(by_task["code-fib"]["passed"], "1/1")
        self.assertEqual(by_task["math"]["passed"], "1/1")
        self.assertIsNotNone(by_task["code-fib"]["first_ms"])
        self.assertIn("¥", by_task["code-fib"]["cost_str"])
        self.assertEqual(by_task["code-fib"]["usage_out"], 25)

    def test_refresh_against_mock(self):
        base = "http://127.0.0.1:%d/v1" % self.port
        rc, out = self._run_cli(["refresh", "-p", "gateway", "--base-url", base, "-k", "sk-test"])
        self.assertEqual(rc, 0, out)
        self.assertIn("mock-a", out)
        self.assertIn("mock-b", out)
        # 清理:避免污染共享数据文件
        import json as _json
        path = cli.DATA_FILE
        data = _json.loads(path.read_text(encoding="utf-8"))
        data["providers"]["gateway"]["models"] = []
        path.write_text(_json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
