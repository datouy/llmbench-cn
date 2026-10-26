#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llmbench-cn —— 国产大模型自测选型 CLI（零依赖）

用你自己的网络、自己的密钥，实测各家模型的：
  - 首 token 延迟 / 总时延
  - 输入输出 token 量与成本（按官方价格估算）
  - 客观任务通过率（代码执行、JSON 抽取、数学、SQL）

用法示例：
    python llmbench.py list                      # 看供应商与任务
    python llmbench.py run -p deepseek -m deepseek-chat
    python llmbench.py run -p deepseek -p qwen -m qwen-plus --task code-fib --repeat 3
    python llmbench.py run -p deepseek:sk-xxx -m deepseek-chat --json --save report.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.1.0"
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "providers.json"
TASKS_FILE = BASE_DIR / "data" / "tasks.json"

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    if not path.exists():
        sys.stderr.write("错误：找不到数据文件 %s\n" % path)
        sys.exit(2)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


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


# --------------------------------------------------------------------------
# SSE 流式解析
# --------------------------------------------------------------------------

def parse_sse_lines(lines, started: float):
    """从 SSE 行流中解析内容、首 token 延迟与 usage。"""
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
        if piece:
            if first_token_ms is None:
                first_token_ms = int((time.time() - started) * 1000)
            content_parts.append(piece)
    content = "".join(content_parts)
    if first_token_ms is None and content:
        first_token_ms = int((time.time() - started) * 1000)
    return content, first_token_ms, usage


def request_chat(provider: dict, model: str, prompt: str, key: str,
                 max_tokens: int, base_override: str | None, timeout: int, stream: bool):
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
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            if stream:
                content, first_ms, usage = parse_sse_lines(resp, started)
            else:
                raw = resp.read().decode("utf-8", "replace")
                parsed = json.loads(raw)
                usage = parsed.get("usage")
                choices = parsed.get("choices") or []
                content = ""
                if choices and choices[0].get("message"):
                    content = choices[0]["message"].get("content") or ""
                first_ms = int((time.time() - started) * 1000)
            total_ms = int((time.time() - started) * 1000)
            return status, content, first_ms, total_ms, usage, None
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "replace")
        return err.code, "", None, int((time.time() - started) * 1000), None, raw[:300]
    except Exception as err:
        return None, "", None, int((time.time() - started) * 1000), None, str(err)[:300]


# --------------------------------------------------------------------------
# 客观校验
# --------------------------------------------------------------------------

def extract_code_block(content: str) -> str:
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", content, re.S)
    if m:
        return m.group(1)
    return content.strip()


def check_py_fib(content: str) -> bool:
    code = extract_code_block(content)
    if not code or "def" not in code:
        return False
    harness = code + "\n\nassert fib(0) == 0\nassert fib(1) == 1\nassert fib(10) == 55\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "check_fib.py"
        path.write_text(harness, encoding="utf-8")
        try:
            proc = subprocess.run([sys.executable, str(path)], capture_output=True, timeout=30)
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False


def check_json_keys(content: str, params: dict) -> bool:
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return False
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return False
    for key, expected in params.items():
        if key not in obj:
            return False
        if expected == "int" and not isinstance(obj[key], int):
            return False
        if expected == "str" and not isinstance(obj[key], str):
            return False
    return True


def check_contains(content: str, params: dict) -> bool:
    return all(kw in content for kw in params.get("keywords", []))


def evaluate(task: dict, content: str) -> bool | None:
    check = task.get("check")
    if not check or not content:
        return None
    params = task.get("check_params") or {}
    if check == "py_fib":
        return check_py_fib(content)
    if check == "json_keys":
        return check_json_keys(content, params)
    if check == "contains":
        return check_contains(content, params)
    return None


# --------------------------------------------------------------------------
# 成本计算
# --------------------------------------------------------------------------

def calc_cost(provider: dict, model_id: str, usage: dict | None, estimated: bool):
    """返回 (成本字符串, 数值或 None)。usage 含 prompt_tokens/completion_tokens。"""
    model_info = None
    for m in provider.get("models", []):
        if m["id"] == model_id:
            model_info = m
            break
    if usage is None or model_info is None:
        return "—", None
    price_in = model_info.get("input")
    price_out = model_info.get("output")
    if price_in is None or price_out is None:
        return "见官网", None
    currency = provider.get("currency", "CNY")
    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    cost = (prompt_tokens / 1e6) * price_in + (completion_tokens / 1e6) * price_out
    symbol = "$" if currency == "USD" else "¥"
    suffix = "≈" if estimated else ""
    return "%s%.4f%s" % (symbol, cost, suffix), cost


# --------------------------------------------------------------------------
# 单次运行
# --------------------------------------------------------------------------

def run_one(provider: dict, model: str, key: str, task: dict, args) -> dict:
    status, content, first_ms, total_ms, usage, error = request_chat(
        provider, model, task["prompt"], key,
        task.get("max_tokens", 1024), args.base_url, args.timeout, stream=True)
    # 流式失败/空内容时回退非流式
    if (status == 200 and not content and usage is None) or status is None:
        status, content, first_ms, total_ms, usage, error = request_chat(
            provider, model, task["prompt"], key,
            task.get("max_tokens", 1024), args.base_url, args.timeout, stream=False)
    estimated = False
    if status == 200 and content and usage is None:
        usage = {"prompt_tokens": estimate_tokens(task["prompt"]),
                 "completion_tokens": estimate_tokens(content)}
        estimated = True
    passed = evaluate(task, content) if status == 200 else None
    cost_str, _ = calc_cost(provider, model, usage, estimated)
    return {
        "task": task["id"], "status": status, "content": content,
        "first_token_ms": first_ms, "total_ms": total_ms,
        "usage": usage, "estimated_usage": estimated,
        "passed": passed, "cost_str": cost_str, "error": error,
    }


# --------------------------------------------------------------------------
# 命令：list
# --------------------------------------------------------------------------

def cmd_list(args) -> str:
    providers = load_json(DATA_FILE)["providers"]
    lines = []
    if not args.tasks:
        header = ["供应商", "默认模型", "输入/输出 (1M)", "协议", "备注"]
        widths = [14, 34, 24, 22, 40]
        lines.append("  ".join(pad(h, w) for h, w in zip(header, widths)))
        lines.append("  ".join("-" * w for w in widths))
        for pid, p in providers.items():
            currency = p.get("currency", "CNY")
            protocols = []
            if p.get("base_url"):
                protocols.append("chat")
            if p.get("anthropic_base_url"):
                protocols.append("anthropic")
            if p.get("responses_api"):
                protocols.append("responses")
            if not p.get("models"):
                lines.append("  ".join(pad(c, w) for c, w in zip(
                    [pid, "(无内置模型)", "—", "/".join(protocols),
                     "运行 refresh 导入网关 /models 或 -m 指定模型"], widths)))
                continue
            first = p["models"][0]
            price = "—"
            if first.get("input") is not None and first.get("output") is not None:
                symbol = "$" if currency == "USD" else "¥"
                price = "%s%.2f / %s%.2f" % (symbol, first["input"], symbol, first["output"])
            lines.append("  ".join(pad(c, w) for c, w in zip(
                [pid, first["id"], price, "/".join(protocols), p.get("note", "")[:40]], widths)))
        lines.append("")
        lines.append("密钥：从环境变量读取（如 DEEPSEEK_API_KEY），或 -p deepseek:sk-xxx 内联指定。")
        lines.append("密钥获取与完整价格以官方平台为准。")
    else:
        tasks = load_json(TASKS_FILE)["tasks"]
        lines.append("内置任务（客观校验：✓ 自动判定 / - 仅测指标）：")
        for t in tasks:
            check = {"py_fib": "✓执行验证", "json_keys": "✓结构校验",
                     "contains": "✓答案校验"}.get(t.get("check") or "", "-")
            lines.append("  %-16s %s" % (t["id"], t["name"]))
            lines.append("  %-16s 校验: %s" % ("", check))
    if args.json:
        return json.dumps({"providers": providers,
                           "tasks": load_json(TASKS_FILE)["tasks"]}, ensure_ascii=False, indent=2)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 命令：run
# --------------------------------------------------------------------------

def parse_pairs(args) -> list:
    """把 -p a[:key] 与 -m model 组合成 (provider_id, model, key_override) 列表。"""
    providers_data = load_json(DATA_FILE)["providers"]
    pairs = []
    pids = list(args.provider)
    models = list(args.model) if args.model else []
    for i, token in enumerate(pids):
        if ":" in token:
            pid, key_override = token.split(":", 1)
        else:
            pid, key_override = token, None
        if pid not in providers_data:
            sys.stderr.write("未知供应商 %r，可用：%s\n" % (pid, ", ".join(sorted(providers_data))))
            sys.exit(2)
        if i < len(models):
            model = models[i]
        elif providers_data[pid].get("models"):
            model = providers_data[pid]["models"][0]["id"]
        else:
            sys.stderr.write("供应商 %s 没有内置模型,请用 -m 指定模型名。\n" % pid)
            sys.exit(2)
        pairs.append((pid, model, key_override))
    return pairs


def cmd_run(args) -> str:
    providers_data = load_json(DATA_FILE)["providers"]
    tasks_data = load_json(TASKS_FILE)["tasks"]
    if args.task:
        task_ids = [x.strip() for chunk in args.task for x in chunk.split(",") if x.strip()]
        tasks = [t for t in tasks_data if t["id"] in task_ids]
        missing = set(task_ids) - {t["id"] for t in tasks_data}
        if missing:
            sys.stderr.write("未知任务：%s\n" % ", ".join(sorted(missing)))
            sys.exit(2)
    else:
        tasks = tasks_data
    pairs = parse_pairs(args)

    rows = []
    for pid, model, key_override in pairs:
        provider = providers_data[pid]
        key = key_override or os.environ.get(provider.get("env_key") or "") or ""
        if not key:
            print("提示：%s 未提供密钥（环境变量 %s 或 -p %s:sk-xxx），预计会 401。" % (
                pid, provider.get("env_key"), pid), file=sys.stderr)
        for task in tasks:
            runs = [run_one(provider, model, key, task, args) for _ in range(args.repeat)]
            ok = [r for r in runs if r["status"] == 200]
            if not ok:
                rows.append({"pid": pid, "model": model, "task": task["id"],
                             "runs": len(runs), "ok_runs": 0,
                             "first_ms": None, "total_ms": None,
                             "usage_in": None, "usage_out": None,
                             "cost_str": "—", "passed": "失败(%s)" % (runs[0]["error"] or runs[0]["status"]),
                             "estimated": False})
                continue
            first_ms = median([r["first_token_ms"] for r in ok if r["first_token_ms"] is not None])
            total_ms = median([r["total_ms"] for r in ok if r["total_ms"] is not None])
            usage_in = median([(r["usage"] or {}).get("prompt_tokens") or 0 for r in ok])
            usage_out = median([(r["usage"] or {}).get("completion_tokens") or 0 for r in ok])
            passed_count = sum(1 for r in ok if r["passed"] is True)
            pass_known = sum(1 for r in ok if r["passed"] is not None)
            passed_str = "%d/%d" % (passed_count, pass_known) if pass_known else "-"
            cost_str = ok[0]["cost_str"]
            estimated = any(r["estimated_usage"] for r in ok)
            rows.append({"pid": pid, "model": model, "task": task["id"],
                         "runs": len(runs), "ok_runs": len(ok),
                         "first_ms": first_ms, "total_ms": total_ms,
                         "usage_in": int(usage_in), "usage_out": int(usage_out),
                         "cost_str": cost_str, "passed": passed_str,
                         "estimated": estimated})
            if args.verbose:
                for r in runs:
                    print("[%s/%s %s] HTTP %s 首token %sms 总 %sms 通过 %s" % (
                        pid, model, task["id"], r["status"], r["first_token_ms"],
                        r["total_ms"], r["passed"]))

    if args.json or args.save:
        report = {"version": VERSION, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "repeat": args.repeat, "rows": rows}
        if args.save:
            save_path = Path(args.save)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        if args.json:
            return json.dumps(report, ensure_ascii=False, indent=2)

    header = ["供应商", "模型", "任务", "次数", "首token(ms)", "总时延(s)", "输入/输出", "成本", "通过"]
    widths = [12, 26, 14, 6, 12, 10, 12, 12, 8]
    lines = ["  ".join(pad(h, w) for h, w in zip(header, widths)),
             "  ".join("-" * w for w in widths)]
    for r in rows:
        first = "%d" % r["first_ms"] if r["first_ms"] is not None else "—"
        total = "%.1f" % (r["total_ms"] / 1000) if r["total_ms"] is not None else "—"
        tokens = "%d/%d" % (r["usage_in"], r["usage_out"]) if r["usage_in"] is not None else "—"
        cells = [r["pid"], r["model"], r["task"], "%d/%d" % (r["ok_runs"], r["runs"]),
                 first, total, tokens, r["cost_str"], str(r["passed"])]
        lines.append("  ".join(pad(c, w) for c, w in zip(cells, widths)))
    lines.append("")
    lines.append("通过列：x/y = 客观校验通过数/可判定次数（- 表示该任务无客观校验）。")
    lines.append("成本按官方价格估算；usage 为 ≈ 时表示供应商未返回用量、按字符数估算。")
    if args.save:
        lines.append("报告已保存到 %s" % args.save)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def cmd_refresh(args) -> str:
    """从 OpenAI 兼容网关的 /models 导入模型列表,写入数据文件。"""
    data = load_json(DATA_FILE)
    providers = data["providers"]
    pid = args.provider or "gateway"
    provider = providers.get(pid) or {"base_url": "", "env_key": ""}
    base = (args.base_url or provider.get("base_url") or "http://127.0.0.1:8788/v1").rstrip("/")
    key = args.key or os.environ.get(provider.get("env_key") or "") or ""
    url = base + "/models"
    headers = {"User-Agent": "llmbench-refresh"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, headers=headers, method="GET")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read().decode("utf-8", "replace")
    except Exception as err:
        return "刷新失败:无法连接 %s (%s)" % (url, err)
    ms = int((time.time() - started) * 1000)
    try:
        parsed = json.loads(raw)
    except ValueError:
        return "刷新失败:响应不是合法 JSON\n%s" % raw[:300]
    ids = [m.get("id") for m in parsed.get("data", []) if m.get("id")]
    if not ids:
        return "刷新失败:未在响应中找到模型列表 (HTTP %s, %sms)" % (status, ms)
    if pid not in providers:
        providers[pid] = {
            "name": pid, "cn": "", "site": "", "base_url": base,
            "anthropic_base_url": None, "responses_api": None,
            "env_key": (pid.upper() + "_API_KEY"), "currency": "CNY",
            "note": "由 llmbench refresh 从网关导入。", "models": [],
        }
    existing = {m["id"] for m in providers[pid].get("models", [])}
    added = [i for i in ids if i not in existing]
    removed = [i for i in existing if i not in ids]
    providers[pid]["models"] = [
        m for m in providers[pid]["models"] if m["id"] in ids
    ] + [{"id": i, "input": None, "output": None, "cache_hit_input": None,
          "context": None, "desc": "网关导入"} for i in added]
    providers[pid]["models_refreshed_at"] = time.strftime("%Y-%m-%d %H:%M")
    data["providers"] = providers
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["刷新完成:%s (%s) HTTP %s %sms" % (pid, base, status, ms),
             "新增 %d 个模型,移除 %d 个,当前共 %d 个。" % (len(added), len(removed), len(ids))]
    if added:
        lines.append("新增:" + ", ".join(added))
    if removed:
        lines.append("移除:" + ", ".join(removed))
    lines.append("已写入 %s" % DATA_FILE)
    return "\n".join(lines)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmbench",
        description="国产大模型自测选型 CLI：速度 / 成本 / 客观任务通过率（零依赖）")
    parser.add_argument("--version", action="version", version="llmbench " + VERSION)
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="查看供应商/模型/任务")
    p_list.add_argument("--tasks", action="store_true", help="只看内置任务")
    p_list.add_argument("--json", action="store_true")

    p_run = sub.add_parser("run", help="运行基准测试")
    p_run.add_argument("--provider", "-p", action="append", required=True,
                       help="供应商 id，可多个；可内联密钥：-p deepseek:sk-xxx")
    p_run.add_argument("--model", "-m", action="append",
                       help="与 -p 按顺序配对；缺省用该供应商第一个模型")
    p_run.add_argument("--task", "-t", action="append", help="任务 id（缺省全部）")
    p_run.add_argument("--repeat", "-n", type=int, default=1, help="每个任务重复次数（默认 1）")
    p_run.add_argument("--max-tokens", type=int, default=0, help="覆盖任务的 max_tokens")
    p_run.add_argument("--base-url", help="覆盖 base URL（如指向本地网关）")
    p_run.add_argument("--timeout", type=int, default=120, help="单次请求超时秒数（默认 120）")
    p_run.add_argument("--json", action="store_true", help="输出 JSON 报告")
    p_run.add_argument("--save", help="保存 JSON 报告到文件")
    p_run.add_argument("--verbose", "-v", action="store_true", help="打印每次运行的明细")

    p_refresh = sub.add_parser("refresh", help="从网关 /models 导入模型列表（更新数据文件）")
    p_refresh.add_argument("--provider", "-p", default="gateway", help="供应商 id（缺省 gateway）")
    p_refresh.add_argument("--base-url", help="网关地址（缺省用该供应商的 base_url）")
    p_refresh.add_argument("--key", "-k", help="API Key（缺省读环境变量）")
    return parser


def main(argv: list | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        if args.command == "list":
            print(cmd_list(args))
        elif args.command == "run":
            print(cmd_run(args))
        elif args.command == "refresh":
            print(cmd_refresh(args))
        return 0
    except KeyboardInterrupt:
        print("\n已取消")
        return 130


if __name__ == "__main__":
    sys.exit(main())







