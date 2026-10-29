#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llmbench-cn —— 国产大模型自测选型 CLI（零依赖）

用你自己的网络、自己的密钥，实测各家模型的：
  - 首 token 延迟 / 总时延 / 输出吞吐 (tok/s)
  - 输入输出 token 量与成本（按官方价格估算）
  - 客观任务通过率（代码执行、JSON 抽取、数学、SQL）

用法示例：
    python -m llmbench list                          # 看供应商与任务
    python -m llmbench list --provider deepseek      # 看某供应商全部模型与价格
    python -m llmbench run -p deepseek -m deepseek-chat
    python -m llmbench run -p deepseek -p qwen -m qwen-plus -t code-fib -n 3
    python -m llmbench run -p deepseek:sk-xxx -m deepseek-chat --json --save report.json
    python -m llmbench doctor                        # 环境自检
"""

from __future__ import annotations

import argparse
import contextvars
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.2.0"
BASE_DIR = Path(__file__).resolve().parent

# 内置数据（随包分发，只读）
PACKAGE_DATA_FILE = BASE_DIR / "data" / "providers.json"
TASKS_FILE = BASE_DIR / "data" / "tasks.json"

# 用户数据层：refresh 只写这里，绝不写进已安装的包目录（site-packages 常常只读）
USER_DATA_DIR = Path(os.path.expanduser("~")) / ".llmbench"
USER_DATA_FILE = USER_DATA_DIR / "providers.json"

# 向后兼容：旧的 DATA_FILE 仍指向内置数据文件
DATA_FILE = PACKAGE_DATA_FILE

# Windows 控制台默认不是 UTF-8，中文会乱码
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# --------------------------------------------------------------------------
# 输出捕获（线程安全，替代 contextlib.redirect_stdout）
# --------------------------------------------------------------------------

_OUT_SINK = contextvars.ContextVar("llmbench_out_sink", default=None)
_ERR_SINK = contextvars.ContextVar("llmbench_err_sink", default=None)


def emit(msg: str = "") -> None:
    """向 stdout 输出；被 capture_output 捕获时写入 sink 而非全局 stdout。"""
    sink = _OUT_SINK.get()
    if sink is None:
        print(msg)
    else:
        sink.append(msg)


def emit_err(msg: str = "") -> None:
    """向 stderr 输出。"""
    sink = _ERR_SINK.get()
    if sink is None:
        print(msg, file=sys.stderr)
    else:
        sink.append(msg)


class capture_output:
    """捕获本上下文内的 emit/emit_err 输出。

    背景：contextlib.redirect_stdout 改的是全局 sys.stdout，Web 面板并发请求
    时会互相串台（一个请求的输出跑到另一个请求里）。contextvars 天然按线程
    隔离，因此这里用它实现等价的、线程安全的捕获。
    """

    def __init__(self) -> None:
        self.out: list = []
        self.err: list = []
        self._t_out = None
        self._t_err = None

    def __enter__(self) -> "capture_output":
        self._t_out = _OUT_SINK.set(self.out)
        self._t_err = _ERR_SINK.set(self.err)
        return self

    def __exit__(self, *exc) -> bool:
        _OUT_SINK.reset(self._t_out)
        _ERR_SINK.reset(self._t_err)
        return False

    @property
    def text(self) -> str:
        return "\n".join(self.out + self.err)

    @property
    def stdout(self) -> str:
        return "\n".join(self.out)


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    if not path.exists():
        emit_err("错误：找不到数据文件 %s" % path)
        sys.exit(2)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as exc:
        emit_err("错误：数据文件 %s 不是合法 JSON（%s）" % (path, exc))
        sys.exit(2)


def display_width(text: str) -> int:
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(1, width - display_width(text))


def median(values: list) -> float | None:
    if not values:
        return None
    return statistics.median(values)


def estimate_tokens(text: str) -> int:
    """无 usage 时的粗略估算：中文约 1 字符/1 token，英文约 4 字符/1 token。"""
    if not text:
        return 0
    import unicodedata
    cjk = sum(1 for ch in text if unicodedata.east_asian_width(ch) in ("W", "F"))
    other = len(text) - cjk
    return max(1, int(cjk + other / 4))


def normalize_usage(usage: dict | None) -> dict | None:
    """把各协议的 usage 字段统一成 prompt_tokens / completion_tokens。"""
    if not usage:
        return None
    prompt = usage.get("prompt_tokens")
    if prompt is None:
        prompt = usage.get("input_tokens")
    completion = usage.get("completion_tokens")
    if completion is None:
        completion = usage.get("output_tokens")
    out = {}
    if prompt is not None:
        out["prompt_tokens"] = int(prompt)
    if completion is not None:
        out["completion_tokens"] = int(completion)
    return out or None


def fmt_cost(provider: dict, model_id: str, prompt_tokens: int, completion_tokens: int,
             estimated: bool) -> tuple[str, float | None]:
    """按官方价格折算成本，返回 (显示串, 数值)。"""
    model_info = None
    for m in provider.get("models", []):
        if m.get("id") == model_id:
            model_info = m
            break
    if model_info is None:
        return "—", None
    price_in = model_info.get("input")
    price_out = model_info.get("output")
    if price_in is None or price_out is None:
        return "见官网", None
    currency = provider.get("currency", "CNY")
    cost = (prompt_tokens / 1e6) * price_in + (completion_tokens / 1e6) * price_out
    symbol = "$" if currency == "USD" else "¥"
    suffix = "≈" if estimated else ""
    return "%s%.4f%s" % (symbol, cost, suffix), cost


def calc_cost(provider: dict, model_id: str, usage: dict | None, estimated: bool):
    """兼容旧接口：返回 (成本字符串, 数值或 None)。usage 含 prompt_tokens/completion_tokens。"""
    usage = normalize_usage(usage) or {}
    return fmt_cost(provider, model_id,
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                    estimated)


# --------------------------------------------------------------------------
# 数据层：内置数据 + 用户数据层
# --------------------------------------------------------------------------

def _merge_model(base: dict, over: dict) -> dict:
    """合并单个模型：内置（人工整理的价格）优先，用户层只补空缺。"""
    merged = dict(over)
    for key, val in (base or {}).items():
        if val is not None:
            merged[key] = val
    return merged


def merge_provider(base: dict | None, over: dict) -> dict:
    """合并供应商：内置的展示字段优先，base_url 以用户层为准，models 按 id 合并。"""
    if not base:
        return dict(over)
    merged = dict(over)
    for key in ("name", "cn", "site", "category", "note", "env_key", "currency"):
        if base.get(key) is not None:
            merged[key] = base[key]
    base_models = {m.get("id"): m for m in base.get("models", []) if m.get("id")}
    models: list = []
    seen: set = set()
    for m in over.get("models", []):
        mid = m.get("id")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        models.append(_merge_model(base_models.get(mid, {}), m))
    for mid, m in base_models.items():
        if mid not in seen:
            models.append(m)
    merged["models"] = models
    return merged


def load_providers() -> dict:
    """返回合并后的供应商表（内置数据 + ~/.llmbench 用户层）。"""
    base = load_json(PACKAGE_DATA_FILE)
    providers = dict(base.get("providers", {}))
    if USER_DATA_FILE.exists():
        try:
            user = json.loads(USER_DATA_FILE.read_text(encoding="utf-8"))
        except ValueError:
            user = {}
        for pid, over in (user.get("providers") or {}).items():
            providers[pid] = merge_provider(providers.get(pid), over)
    return providers


def load_provider_doc() -> dict:
    """返回合并后的完整供应商文档（保留顶层元信息）。"""
    doc = load_json(PACKAGE_DATA_FILE)
    doc = dict(doc)
    doc["providers"] = load_providers()
    if USER_DATA_FILE.exists():
        try:
            user = json.loads(USER_DATA_FILE.read_text(encoding="utf-8"))
        except ValueError:
            user = {}
        if user.get("price_updated"):
            doc["price_updated"] = user["price_updated"]
    return doc


def write_user_providers(providers: dict, price_updated: str | None = None) -> Path:
    """写入用户数据层（~/.llmbench/providers.json），写入前自动备份 .bak。"""
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if USER_DATA_FILE.exists():
        shutil.copy2(str(USER_DATA_FILE), str(USER_DATA_FILE) + ".bak")
    doc = {"schema_version": 1, "providers": providers}
    if price_updated:
        doc["price_updated"] = price_updated
    USER_DATA_FILE.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return USER_DATA_FILE


# --------------------------------------------------------------------------
# SSE 流式解析
# --------------------------------------------------------------------------

def parse_sse_lines(lines, started: float):
    """从 SSE 行流中解析内容、首 token 延迟与 usage。

    started 必须是 time.perf_counter() 的时间点（单调时钟，不受系统时间调整影响）。
    """
    content_parts: list = []
    first_token_ms = None
    usage = None
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        if obj.get("usage"):
            usage = obj["usage"]
        choices = obj.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        piece = delta.get("content")
        if piece is None:
            # 部分网关把内容放在 message.content（部分非标准流式实现）
            piece = (choices[0].get("message") or {}).get("content")
        if piece:
            if first_token_ms is None:
                first_token_ms = int((time.perf_counter() - started) * 1000)
            content_parts.append(piece)
    content = "".join(content_parts)
    if first_token_ms is None and content:
        first_token_ms = int((time.perf_counter() - started) * 1000)
    return content, first_token_ms, normalize_usage(usage)


def _http_error_message(raw: str, status) -> str:
    """从错误响应里提取可读信息，避免整段 HTML 噪声。"""
    try:
        obj = json.loads(raw)
        err = obj.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or raw[:200])
        if isinstance(err, str):
            return err[:200]
    except ValueError:
        pass
    text = re.sub(r"<[^>]+>", " ", raw).strip()
    text = re.sub(r"\s+", " ", text)
    return (text or ("HTTP %s" % status))[:200]


def request_chat(provider: dict, model: str, prompt: str, key: str,
                 max_tokens: int, base_override: str | None, timeout: int,
                 stream: bool, retries: int = 0, retry_wait: float = 2.0):
    """发起一次 chat/completions 请求。

    返回 (status, content, first_ms, total_ms, usage, error)。
    status 为 None 表示网络层异常；HTTP 错误时 content 为空、error 为可读信息。
    """
    base = (base_override or provider["base_url"]).rstrip("/")
    url = base + "/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": stream,
    }
    if stream:
        body["stream_options"] = {"include_usage": True}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    started = time.perf_counter()
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                if stream:
                    content, first_ms, usage = parse_sse_lines(resp, started)
                else:
                    raw = resp.read().decode("utf-8", "replace")
                    parsed = json.loads(raw)
                    usage = normalize_usage(parsed.get("usage"))
                    choices = parsed.get("choices") or []
                    content = ""
                    if choices and choices[0].get("message"):
                        content = choices[0]["message"].get("content") or ""
                    first_ms = int((time.perf_counter() - started) * 1000)
                total_ms = int((time.perf_counter() - started) * 1000)
                return status, content, first_ms, total_ms, usage, None
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", "replace")
            # 429/5xx 属于可重试；其余（如 401/404/400）重试没有意义
            if attempt < retries and (err.code == 429 or 500 <= err.code < 600):
                attempt += 1
                wait = retry_wait * (2 ** (attempt - 1))
                emit_err("提示：HTTP %s，%s 秒后重试（第 %d 次）" % (err.code, wait, attempt))
                time.sleep(wait)
                continue
            total_ms = int((time.perf_counter() - started) * 1000)
            return err.code, "", None, total_ms, None, _http_error_message(raw, err.code)
        except Exception as err:
            if attempt < retries:
                attempt += 1
                wait = retry_wait * (2 ** (attempt - 1))
                emit_err("提示：%s，%s 秒后重试（第 %d 次）" % (err, wait, attempt))
                time.sleep(wait)
                continue
            total_ms = int((time.perf_counter() - started) * 1000)
            return None, "", None, total_ms, None, str(err)[:300]


# --------------------------------------------------------------------------
# 客观校验
# --------------------------------------------------------------------------

CODE_BLOCK_RE = re.compile(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", re.S)


def extract_code_block(content: str) -> str:
    """提取模型回复中的首个代码块；没有代码块则返回原文（去掉围栏残留）。"""
    m = CODE_BLOCK_RE.search(content)
    if m:
        return m.group(1)
    return content.strip()


def _strip_dangerous_imports(code: str) -> str:
    """移除会触碰文件系统的危险 import（os / shutil / pathlib / socket ...）。"""
    dangerous = ("os", "shutil", "pathlib", "socket", "subprocess", "sys", "ctypes",
                 "multiprocessing", "urllib", "http", "requests", "tempfile", "pickle",
                 "importlib", "resource", "signal", "pty", "glob")
    out = []
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", stripped)
            hit = False
            for n in names[1:] if stripped.startswith("from ") else names:
                root = n.split(".")[0]
                if root in dangerous:
                    hit = True
                    break
            if hit:
                out.append("raise ImportError('import blocked by llmbench sandbox')")
                continue
        out.append(line)
    return "\n".join(out)


def check_py_fib(content: str, timeout: int = 30) -> bool:
    """在临时目录中真实运行模型生成的代码，验证 fib 是否正确。

    安全边界：
      - 只在临时目录里跑，用完即删；
      - 阻断 os/shutil/subprocess/socket 等危险 import；
      - 子进程无 stdin，限制输出大小与运行时长；
      - 只断言 fib 的行为，不执行任何外部命令。

    注意：这**不是**真正的安全沙箱（同一台机器上的 Python 进程仍有理论逃逸可能），
    详见 README「安全说明」。
    """
    code = extract_code_block(content)
    if not code or "fib" not in code:
        return False
    code = _strip_dangerous_imports(code)
    harness = (code + "\n\n"
               "assert fib(0) == 0\n"
               "assert fib(1) == 1\n"
               "assert fib(2) == 1\n"
               "assert fib(10) == 55\n"
               "assert fib(20) == 6765\n")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "check_fib.py"
        path.write_text(harness, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-S", str(path)],
                capture_output=True, timeout=timeout, cwd=tmp,
                stdin=subprocess.DEVNULL,
                env={"PATH": "", "PYTHONIOENCODING": "utf-8",
                     "PYTHONDONTWRITEBYTECODE": "1"},
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False


def _extract_json_object(content: str):
    """从回复中稳健地提取第一个 JSON 对象。

    原实现用贪婪的 `\\{.*\\}`，遇到「JSON 后面还有一段解释文字」或多个对象时
    会截取到错误的范围导致解析失败。这里改为括号配对扫描。
    """
    # 优先尝试 ```json 代码块
    for m in re.finditer(r"```(?:json)?[ \t]*\r?\n(.*?)```", content, re.S):
        obj = _loads_object(m.group(1))
        if obj is not None:
            return obj
    start = content.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(content)):
            ch = content[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    obj = _loads_object(content[start:i + 1])
                    if obj is not None:
                        return obj
                    break
        start = content.find("{", start + 1)
    return None


def _loads_object(text: str):
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _type_matches(value, expected: str) -> bool:
    """严格的类型判定。

    注意 bool 是 int 的子类，Python 里 isinstance(True, int) 为 True，
    因此「期望 int」时必须显式排除 bool，否则 count: true 会被误判为通过。
    """
    if expected == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "float":
        return isinstance(value, float) or (isinstance(value, int) and not isinstance(value, bool))
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "str":
        return isinstance(value, str)
    if expected == "bool":
        return isinstance(value, bool)
    if expected == "list":
        return isinstance(value, list)
    if expected == "dict":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return True


def check_json_keys(content: str, params: dict) -> bool:
    obj = _extract_json_object(content)
    if obj is None:
        return False
    for key, expected in (params or {}).items():
        if key not in obj:
            return False
        if not _type_matches(obj[key], expected):
            return False
    return True


def check_contains(content: str, params: dict) -> bool:
    keywords = params.get("keywords") or []
    if not keywords:
        return True
    text = content.strip()
    return all(kw in text for kw in keywords)


def evaluate(task: dict, content: str) -> bool | None:
    """返回 True(通过) / False(未通过) / None(该任务无客观校验,不可判定)。

    注意：任务**有**校验但模型返回空内容时，应判为**未通过**（False），
    而不是「不可判定」(None)——否则空回复会被算进分母之外，抬高通过率。
    """
    check = task.get("check")
    if not check:
        return None
    if not content:
        return False
    params = task.get("check_params") or {}
    if check == "py_fib":
        return check_py_fib(content)
    if check == "json_keys":
        return check_json_keys(content, params)
    if check == "contains":
        return check_contains(content, params)
    return None


# --------------------------------------------------------------------------
# 单次运行
# --------------------------------------------------------------------------

def resolve_max_tokens(task: dict, args) -> int:
    """任务自带 max_tokens，--max-tokens 可覆盖（原实现解析了参数却从未使用）。"""
    override = getattr(args, "max_tokens", 0) or 0
    if override > 0:
        return override
    return int(task.get("max_tokens") or 1024)


def run_one(provider: dict, model: str, key: str, task: dict, args) -> dict:
    """跑一次任务，按「流式 → 非流式」回退，返回本次运行的原始指标。"""
    max_tokens = resolve_max_tokens(task, args)
    retries = int(getattr(args, "retries", 0) or 0)
    timeout = int(getattr(args, "timeout", 120) or 120)

    status, content, first_ms, total_ms, usage, error = request_chat(
        provider, model, task["prompt"], key, max_tokens,
        args.base_url, timeout, stream=True, retries=retries)

    # 回退阶梯：
    #   1) 流式成功但内容为空（如网关不支持 stream_options/include_usage）
    #   2) 流式被拒（400/404/422 等，说明该端点不支持流式）
    #   3) 网络异常（status is None）
    need_fallback = (
        status is None
        or (status == 200 and not content)
        or (status is not None and 400 <= status < 500 and status not in (401, 403, 429))
    )
    if need_fallback:
        status2, content2, first_ms2, total_ms2, usage2, error2 = request_chat(
            provider, model, task["prompt"], key, max_tokens,
            args.base_url, timeout, stream=False, retries=retries)
        if status2 == 200:
            status, content, first_ms, total_ms, usage, error = \
                status2, content2, first_ms2, total_ms2, usage2, error2

    estimated = False
    if status == 200 and content and usage is None:
        usage = {"prompt_tokens": estimate_tokens(task["prompt"]),
                 "completion_tokens": estimate_tokens(content)}
        estimated = True
    passed = evaluate(task, content) if status == 200 else None
    # 成本按本次实际 usage 现算，保证与表格里的 token 数同源
    prompt_tokens = int((usage or {}).get("prompt_tokens") or 0)
    completion_tokens = int((usage or {}).get("completion_tokens") or 0)
    cost_str, _ = fmt_cost(provider, model, prompt_tokens, completion_tokens, estimated)
    # 输出吞吐 tok/s：只统计生成阶段（总时延 - 首 token 延迟）
    throughput = None
    if total_ms and first_ms is not None and completion_tokens > 0:
        gen_ms = max(1, total_ms - first_ms)
        throughput = round(completion_tokens / (gen_ms / 1000.0), 1)
    return {
        "task": task["id"], "status": status, "content": content,
        "first_token_ms": first_ms, "total_ms": total_ms,
        "usage": usage, "estimated_usage": estimated,
        "passed": passed, "cost_str": cost_str, "error": error,
        "throughput_tps": throughput,
    }


def summarize_runs(runs: list, provider: dict, model: str, task_id: str) -> dict:
    """把多次运行聚合成一行结果（全部指标取中位数）。"""
    ok = [r for r in runs if r["status"] == 200]
    if not ok:
        first_err = next((r for r in runs if r.get("error")), None)
        detail = (first_err["error"] if first_err else (runs[0]["status"] if runs else "无运行"))
        return {"pid": None, "model": model, "task": task_id,
                "runs": len(runs), "ok_runs": 0,
                "first_ms": None, "total_ms": None,
                "usage_in": None, "usage_out": None, "tps": None,
                "cost_str": "—", "passed": "失败(%s)" % detail,
                "estimated": False}

    first_ms = median([r["first_token_ms"] for r in ok if r["first_token_ms"] is not None])
    total_ms = median([r["total_ms"] for r in ok if r["total_ms"] is not None])
    usage_in = median([(r["usage"] or {}).get("prompt_tokens") or 0 for r in ok])
    usage_out = median([(r["usage"] or {}).get("completion_tokens") or 0 for r in ok])
    tps = median([r["throughput_tps"] for r in ok if r.get("throughput_tps") is not None])
    passed_count = sum(1 for r in ok if r["passed"] is True)
    pass_known = sum(1 for r in ok if r["passed"] is not None)
    passed_str = "%d/%d" % (passed_count, pass_known) if pass_known else "-"
    estimated = any(r["estimated_usage"] for r in ok)
    # 成本基于中位数 token 重算，避免「成本取第一次、token 取中位数」的不一致
    cost_str, _ = fmt_cost(provider, model, int(usage_in), int(usage_out), estimated)
    return {"pid": None, "model": model, "task": task_id,
            "runs": len(runs), "ok_runs": len(ok),
            "first_ms": first_ms, "total_ms": total_ms,
            "usage_in": int(usage_in), "usage_out": int(usage_out), "tps": tps,
            "cost_str": cost_str, "passed": passed_str,
            "estimated": estimated}


# --------------------------------------------------------------------------
# 命令：list
# --------------------------------------------------------------------------

def _protocols(p: dict) -> str:
    protos = []
    if p.get("base_url"):
        protos.append("chat")
    if p.get("anthropic_base_url"):
        protos.append("anthropic")
    if p.get("responses_api"):
        protos.append("responses")
    return "/".join(protos) or "—"


def _price_text(p: dict, m: dict) -> str:
    if m.get("input") is None or m.get("output") is None:
        return "—"
    symbol = "$" if p.get("currency", "CNY") == "USD" else "¥"
    return "%s%.2f / %s%.2f" % (symbol, m["input"], symbol, m["output"])


def cmd_list(args) -> str:
    providers = load_providers()
    tasks = load_json(TASKS_FILE)["tasks"]

    # list --provider <id>：展示该供应商全部模型与价格
    if getattr(args, "provider", None):
        pid = args.provider
        p = providers.get(pid)
        if not p:
            emit_err("未知供应商 %r，可用：%s" % (pid, ", ".join(sorted(providers))))
            sys.exit(2)
        lines = ["%s (%s)  %s" % (p.get("cn") or p.get("name") or pid, pid,
                                  p.get("site") or ""),
                 "base_url: %s   协议: %s   密钥环境变量: %s" % (
                     p.get("base_url") or "—", _protocols(p), p.get("env_key") or "—"),
                 ""]
        if p.get("note"):
            lines.append(p["note"])
            lines.append("")
        lines.append("%-34s %-22s %-12s %-8s %s" % ("模型", "输入/输出 (1M)", "上下文", "价格", "说明"))
        lines.append("%-34s %-22s %-12s %-8s %s" % ("-" * 34, "-" * 22, "-" * 12, "-" * 8, "-" * 40))
        for m in p.get("models", []):
            lines.append("%-34s %-22s %-12s %-8s %s" % (
                m.get("id", ""), _price_text(p, m), m.get("context") or "—",
                "有" if m.get("input") is not None else "无", (m.get("desc") or "")[:40]))
        if not p.get("models"):
            lines.append("(无内置模型) 运行 refresh 导入网关 /models 或用 -m 指定模型名")
        lines.append("")
        lines.append("密钥：环境变量 %s 或 -p %s:sk-xxx" % (p.get("env_key"), pid))
        if getattr(args, "json", False):
            return json.dumps({"providers": {pid: p}, "tasks": tasks},
                              ensure_ascii=False, indent=2)
        return "\n".join(lines)

    lines = []
    if not args.tasks:
        header = ["供应商", "默认模型", "输入/输出 (1M)", "协议", "备注"]
        widths = [14, 34, 24, 22, 40]
        lines.append("  ".join(pad(h, w) for h, w in zip(header, widths)))
        lines.append("  ".join("-" * w for w in widths))
        for pid, p in providers.items():
            if not p.get("models"):
                lines.append("  ".join(pad(c, w) for c, w in zip(
                    [pid, "(无内置模型)", "—", _protocols(p),
                     "运行 refresh 导入网关 /models 或 -m 指定模型"], widths)))
                continue
            first = p["models"][0]
            lines.append("  ".join(pad(c, w) for c, w in zip(
                [pid, first["id"], _price_text(p, first), _protocols(p),
                 (p.get("note") or "")[:40]], widths)))
        lines.append("")
        lines.append("查看单个供应商的全部模型与价格：llmbench list --provider deepseek")
        lines.append("密钥：从环境变量读取（如 DEEPSEEK_API_KEY），或 -p deepseek:sk-xxx 内联指定。")
        lines.append("密钥获取与完整价格以官方平台为准。")
    else:
        lines.append("内置任务（客观校验：✓ 自动判定 / - 仅测指标）：")
        for t in tasks:
            check = {"py_fib": "✓执行验证", "json_keys": "✓结构校验",
                     "contains": "✓答案校验"}.get(t.get("check") or "", "-")
            lines.append("  %-16s %s" % (t["id"], t["name"]))
            lines.append("  %-16s 校验: %s" % ("", check))
    if args.json:
        return json.dumps({"providers": providers, "tasks": tasks},
                          ensure_ascii=False, indent=2)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 命令：run
# --------------------------------------------------------------------------

def parse_pairs(args) -> list:
    """把 -p a[:key] 与 -m model 组合成 (provider_id, model, key_override) 列表。"""
    providers_data = load_providers()
    pairs = []
    pids = list(args.provider)
    models = list(args.model) if args.model else []
    for i, token in enumerate(pids):
        if ":" in token:
            pid, key_override = token.split(":", 1)
        else:
            pid, key_override = token, None
        if pid not in providers_data:
            emit_err("未知供应商 %r，可用：%s" % (pid, ", ".join(sorted(providers_data))))
            sys.exit(2)
        if i < len(models) and models[i]:
            model = models[i]
        elif providers_data[pid].get("models"):
            model = providers_data[pid]["models"][0]["id"]
        else:
            emit_err("供应商 %s 没有内置模型,请用 -m 指定模型名。" % pid)
            sys.exit(2)
        pairs.append((pid, model, key_override))
    return pairs


def _select_tasks(args, tasks_data: list) -> list:
    if not getattr(args, "task", None):
        return tasks_data
    task_ids = [x.strip() for chunk in args.task for x in chunk.split(",") if x.strip()]
    tasks = [t for t in tasks_data if t["id"] in task_ids]
    missing = set(task_ids) - {t["id"] for t in tasks_data}
    if missing:
        emit_err("未知任务：%s" % ", ".join(sorted(missing)))
        sys.exit(2)
    return tasks


def cmd_run(args) -> str:
    providers_data = load_providers()
    tasks_data = load_json(TASKS_FILE)["tasks"]
    repeat = int(getattr(args, "repeat", 1) if getattr(args, "repeat", 1) is not None else 1)
    if repeat < 1:
        emit_err("重复次数必须 >= 1（当前 %d）" % repeat)
        sys.exit(2)
    tasks = _select_tasks(args, tasks_data)
    pairs = parse_pairs(args)

    rows = []
    for pid, model, key_override in pairs:
        provider = providers_data[pid]
        key = key_override or os.environ.get(provider.get("env_key") or "") or ""
        if not key:
            emit_err("提示：%s 未提供密钥（环境变量 %s 或 -p %s:sk-xxx），预计会 401。" % (
                pid, provider.get("env_key"), pid))
        for task in tasks:
            runs = [run_one(provider, model, key, task, args) for _ in range(repeat)]
            row = summarize_runs(runs, provider, model, task["id"])
            row["pid"] = pid
            rows.append(row)
            if args.verbose:
                # 明细走 stderr：--json 时 stdout 必须是纯 JSON，否则无法解析
                for r in runs:
                    emit_err("[%s/%s %s] HTTP %s 首token %sms 总 %sms %s tok/s 通过 %s" % (
                        pid, model, task["id"], r["status"], r["first_token_ms"],
                        r["total_ms"], r.get("throughput_tps"), r["passed"]))

    report = {"version": VERSION, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "repeat": repeat, "rows": rows}
    if getattr(args, "save", None):
        save_path = Path(args.save)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    md_path = getattr(args, "markdown", None)
    if md_path:
        md_file = Path(md_path)
        md_file.parent.mkdir(parents=True, exist_ok=True)
        md_file.write_text(render_markdown(report), encoding="utf-8")

    if args.json:
        return json.dumps(report, ensure_ascii=False, indent=2)

    lines = render_table(rows).split("\n")
    if getattr(args, "save", None):
        lines.append("报告已保存到 %s" % args.save)
    if md_path:
        lines.append("Markdown 报告已保存到 %s" % md_path)
    return "\n".join(lines)


def render_table(rows: list) -> str:
    header = ["供应商", "模型", "任务", "次数", "首token(ms)", "总时延(s)",
              "tok/s", "输入/输出", "成本", "通过"]
    widths = [12, 26, 14, 6, 12, 10, 8, 12, 12, 8]
    lines = ["  ".join(pad(h, w) for h, w in zip(header, widths)),
             "  ".join("-" * w for w in widths)]
    for r in rows:
        first = "%d" % r["first_ms"] if r["first_ms"] is not None else "—"
        total = "%.1f" % (r["total_ms"] / 1000) if r["total_ms"] is not None else "—"
        tokens = "%d/%d" % (r["usage_in"], r["usage_out"]) if r["usage_in"] is not None else "—"
        tps = "%g" % r["tps"] if r.get("tps") is not None else "—"
        cells = [r["pid"] or "—", r["model"], r["task"], "%d/%d" % (r["ok_runs"], r["runs"]),
                 first, total, tps, tokens, r["cost_str"], str(r["passed"])]
        lines.append("  ".join(pad(c, w) for c, w in zip(cells, widths)))
    lines.append("")
    lines.append("通过列：x/y = 客观校验通过数/可判定次数（- 表示该任务无客观校验）。")
    lines.append("tok/s = 生成阶段输出吞吐（中位数）；成本按官方价格估算。")
    lines.append("usage 为 ≈ 时表示供应商未返回用量、按字符数估算。")
    return "\n".join(lines)


def render_markdown(report: dict) -> str:
    """把 JSON 报告渲染成 Markdown 对比表（便于贴到 PR / 文档）。"""
    rows = report.get("rows", [])
    out = ["# llmbench-cn 实测报告", ""]
    out.append("- 生成时间：%s" % report.get("generated_at", "-"))
    out.append("- 重复次数：%s" % report.get("repeat", "-"))
    out.append("- llmbench 版本：%s" % report.get("version", "-"))
    out.append("")
    out.append("| 供应商 | 模型 | 任务 | 次数 | 首token(ms) | 总时延(s) | tok/s | 输入/输出 | 成本 | 通过 |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        first = r["first_ms"] if r["first_ms"] is not None else "—"
        total = "%.1f" % (r["total_ms"] / 1000) if r["total_ms"] is not None else "—"
        tps = "%g" % r["tps"] if r.get("tps") is not None else "—"
        tokens = ("%d/%d" % (r["usage_in"], r["usage_out"])
                  if r["usage_in"] is not None else "—")
        out.append("| %s | %s | %s | %s/%s | %s | %s | %s | %s | %s | %s |" % (
            r.get("pid") or "—", r["model"], r["task"], r["ok_runs"], r["runs"],
            first, total, tps, tokens, r["cost_str"], r["passed"]))
    out.append("")
    out.append("> 通过列 x/y = 客观校验通过数 / 可判定次数；成本按官方价格估算。")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# 命令：refresh
# --------------------------------------------------------------------------

def _extract_model_ids(parsed) -> list:
    """从 /models 响应里提取模型 id，兼容多种网关返回格式。"""
    items = []
    if isinstance(parsed, dict):
        for field in ("data", "models", "result", "items"):
            value = parsed.get(field)
            if isinstance(value, list):
                items = value
                break
        else:
            # {"object":"list","data":[...]} 之外的 {"id": ...} 单体格式
            if parsed.get("id"):
                items = [parsed]
    elif isinstance(parsed, list):
        items = parsed
    ids = []
    for item in items:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            mid = item.get("id") or item.get("name") or item.get("model")
            if mid:
                ids.append(str(mid))
    # 去重并保持顺序
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def cmd_refresh(args) -> str:
    """从 OpenAI 兼容网关的 /models 导入模型列表。

    写入 ~/.llmbench/providers.json（用户数据层），**不写包目录**——
    pip 安装后 site-packages 通常只写，写进去会失败也会污染安装。
    """
    providers = load_providers()
    pid = args.provider or "gateway"
    provider = providers.get(pid) or {"base_url": "", "env_key": ""}
    base = (args.base_url or provider.get("base_url") or "http://127.0.0.1:8788/v1").rstrip("/")
    key = args.key or os.environ.get(provider.get("env_key") or "") or ""
    url = base + "/models"
    headers = {"User-Agent": "llmbench-refresh"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, headers=headers, method="GET")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        status = err.code
        raw = err.read().decode("utf-8", "replace")
    except Exception as err:
        return "刷新失败:无法连接 %s (%s)" % (url, err)
    ms = int((time.perf_counter() - started) * 1000)
    try:
        parsed = json.loads(raw)
    except ValueError:
        return "刷新失败:响应不是合法 JSON\n%s" % raw[:300]
    ids = _extract_model_ids(parsed)
    if not ids:
        return "刷新失败:未在响应中找到模型列表 (HTTP %s, %sms)" % (status, ms)

    existing = {m.get("id"): m for m in provider.get("models", []) if m.get("id")}
    added = [i for i in ids if i not in existing]
    removed = [i for i in existing if i not in ids] if not getattr(args, "no_prune", False) else []

    # 保留已有模型的价格等人工整理信息
    models = []
    for i in ids:
        old = existing.get(i)
        if old:
            models.append(dict(old))
        else:
            models.append({"id": i, "input": None, "output": None,
                           "cache_hit_input": None, "context": None,
                           "desc": "网关导入"})

    new_provider = dict(provider)
    new_provider.update({
        "name": provider.get("name") or pid,
        "cn": provider.get("cn") or "",
        "site": provider.get("site") or "",
        "base_url": base,
        "anthropic_base_url": provider.get("anthropic_base_url"),
        "responses_api": provider.get("responses_api"),
        "env_key": provider.get("env_key") or (pid.upper() + "_API_KEY"),
        "currency": provider.get("currency") or "CNY",
        "note": provider.get("note") or "由 llmbench refresh 从网关导入。",
        "category": provider.get("category") or "自建网关",
        "models": models,
        "models_refreshed_at": time.strftime("%Y-%m-%d %H:%M"),
    })
    providers[pid] = new_provider

    if getattr(args, "dry_run", False):
        lines = ["预演模式（--dry-run），未写入任何文件。",
                 "%s HTTP %s %sms" % (base, status, ms),
                 "新增 %d 个模型,移除 %d 个,当前共 %d 个。" % (len(added), len(removed), len(ids))]
        if added:
            lines.append("新增:" + ", ".join(added))
        if removed:
            lines.append("移除:" + ", ".join(removed))
        return "\n".join(lines)

    # 只把「网关来源」的供应商写进用户层，内置供应商（如 deepseek）保持内置数据不动
    user_doc = {"schema_version": 1, "providers": {}}
    if USER_DATA_FILE.exists():
        try:
            user_doc = json.loads(USER_DATA_FILE.read_text(encoding="utf-8"))
        except ValueError:
            user_doc = {"schema_version": 1, "providers": {}}
    user_doc.setdefault("providers", {})
    user_doc["providers"][pid] = new_provider
    path = write_user_providers(user_doc["providers"], price_updated=None)

    lines = ["刷新完成:%s (%s) HTTP %s %sms" % (pid, base, status, ms),
             "新增 %d 个模型,移除 %d 个,当前共 %d 个。" % (len(added), len(removed), len(ids))]
    if added:
        lines.append("新增:" + ", ".join(added))
    if removed:
        lines.append("移除:" + ", ".join(removed))
    lines.append("已写入 %s（原文件已备份为 %s.bak）" % (path, path))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 命令：doctor
# --------------------------------------------------------------------------

def cmd_doctor(args) -> str:
    """环境自检：Python 版本、数据文件、网络可达性、密钥是否配置。"""
    lines = ["llmbench-cn 环境自检", ""]
    ok = True

    v = sys.version_info
    py_ok = v >= (3, 9)
    lines.append("%s Python %d.%d.%d（需要 >= 3.9）" % ("✓" if py_ok else "✗",
                 v.major, v.minor, v.micro))
    ok = ok and py_ok

    for label, path in (("内置供应商数据", PACKAGE_DATA_FILE), ("内置任务数据", TASKS_FILE)):
        exists = path.exists()
        lines.append("%s %s：%s" % ("✓" if exists else "✗", label, path))
        ok = ok and exists

    if USER_DATA_FILE.exists():
        lines.append("✓ 用户数据层：%s" % USER_DATA_FILE)
    else:
        lines.append("- 用户数据层：%s（尚未创建，refresh 会自动创建）" % USER_DATA_FILE)

    providers = load_providers()
    lines.append("✓ 供应商 %d 个 / 模型 %d 个" % (
        len(providers), sum(len(p.get("models", [])) for p in providers.values())))

    # 密钥：只报告「是否配置」，绝不打印密钥内容
    if getattr(args, "provider", None):
        targets = [args.provider]
    else:
        targets = list(providers.keys())
    configured = []
    for pid in targets:
        p = providers.get(pid)
        if not p:
            continue
        env_key = p.get("env_key")
        if env_key and os.environ.get(env_key):
            configured.append(pid)
    if configured:
        lines.append("✓ 已配置密钥的环境变量：%s" % ", ".join(configured))
    else:
        lines.append("- 尚未检测到任何 API Key 环境变量（-p provider:sk-xxx 也可内联传入）")

    lines.append("")
    lines.append("提示：运行 `llmbench list` 查看供应商，`llmbench run -p <id> -m <model>` 开始实测。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmbench",
        description="国产大模型自测选型 CLI：速度 / 成本 / 客观任务通过率（零依赖）")
    parser.add_argument("--version", action="version", version="llmbench " + VERSION)
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="查看供应商/模型/任务")
    p_list.add_argument("--tasks", action="store_true", help="只看内置任务")
    p_list.add_argument("--provider", "-p", help="只看某个供应商（列出全部模型与价格）")
    p_list.add_argument("--json", action="store_true")

    p_run = sub.add_parser("run", help="运行基准测试")
    p_run.add_argument("--provider", "-p", action="append", required=True,
                       help="供应商 id，可多个；可内联密钥：-p deepseek:sk-xxx")
    p_run.add_argument("--model", "-m", action="append",
                       help="与 -p 按顺序配对；缺省用该供应商第一个模型")
    p_run.add_argument("--task", "-t", action="append", help="任务 id（缺省全部）")
    p_run.add_argument("--repeat", "-n", type=int, default=1, help="每个任务重复次数（默认 1）")
    p_run.add_argument("--max-tokens", type=int, default=0, help="覆盖任务的 max_tokens（0 = 用任务自带值）")
    p_run.add_argument("--base-url", help="覆盖 base URL（如指向本地网关）")
    p_run.add_argument("--timeout", type=int, default=120, help="单次请求超时秒数（默认 120）")
    p_run.add_argument("--retries", type=int, default=0,
                       help="429/5xx/网络错误的重试次数（默认 0，指数退避）")
    p_run.add_argument("--json", action="store_true", help="输出 JSON 报告")
    p_run.add_argument("--save", help="保存 JSON 报告到文件")
    p_run.add_argument("--markdown", "--md", dest="markdown",
                       help="保存 Markdown 对比报告到文件")
    p_run.add_argument("--verbose", "-v", action="store_true", help="打印每次运行的明细")

    p_refresh = sub.add_parser("refresh", help="从网关 /models 导入模型列表（写入用户数据层）")
    p_refresh.add_argument("--provider", "-p", default="gateway", help="供应商 id（缺省 gateway）")
    p_refresh.add_argument("--base-url", help="网关地址（缺省用该供应商的 base_url）")
    p_refresh.add_argument("--key", "-k", help="API Key（缺省读环境变量）")
    p_refresh.add_argument("--dry-run", action="store_true", help="只预演不写文件")
    p_refresh.add_argument("--no-prune", action="store_true",
                           help="保留网关已下线的模型，不做删除")

    p_doctor = sub.add_parser("doctor", help="环境自检（Python 版本 / 数据 / 密钥）")
    p_doctor.add_argument("--provider", "-p", help="只检查某个供应商的密钥")

    p_web = sub.add_parser("web", help="启动本地 Web 面板（浏览器操作）")
    p_web.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    p_web.add_argument("--port", type=int, default=8789, help="端口（默认 8789）")
    p_web.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    return parser


def main(argv: list | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        if args.command == "list":
            emit(cmd_list(args))
        elif args.command == "run":
            emit(cmd_run(args))
        elif args.command == "refresh":
            emit(cmd_refresh(args))
        elif args.command == "doctor":
            emit(cmd_doctor(args))
        elif args.command == "web":
            from . import webui
            return webui.cmd_web(args)
        return 0
    except KeyboardInterrupt:
        emit("\n已取消")
        return 130


if __name__ == "__main__":
    sys.exit(main())



