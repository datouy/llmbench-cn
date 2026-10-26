# -*- coding: utf-8 -*-
"""数据新鲜度检查:price_updated 超过 3 个月时自动开 Issue 提醒。"""
import json
import subprocess
import sys
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "llmbench" / "data" / "providers.json"


def main() -> int:
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    updated = data.get("price_updated", "")
    title = "提醒:模型价格数据超过 3 个月未更新"
    try:
        year, month = (int(x) for x in updated.split("-")[:2])
        # GitHub Actions 的 UTC 时间足够用于月度粒度判断
        import datetime
        now = datetime.date.today()
        months = (now.year - year) * 12 + (now.month - month)
    except Exception:
        print("无法解析 price_updated,跳过。")
        return 0
    if months <= 3:
        print("数据新鲜,无需提醒。(%s, 距今 %d 个月)" % (updated, months))
        return 0
    # 已有同类 open issue 则不重复创建
    try:
        listed = subprocess.run(
            ["gh", "issue", "list", "--state", "open", "--search", title, "--json", "number"],
            capture_output=True, text=True, timeout=60)
        import json as _json
        if listed.returncode == 0 and _json.loads(listed.stdout):
            print("已有同类 Issue,跳过。")
            return 0
    except Exception:
        pass
    body = (
        "数据文件 `llmbench/data/providers.json` 的 `price_updated` 为 `%s`，距今超过 3 个月。\n\n"
        "请检查各家官方定价页并更新价格，或运行 `llmbench refresh` 更新网关模型列表。\n\n"
        "> 本 Issue 由 data-freshness 工作流自动创建。" % updated
    )
    proc = subprocess.run(
        ["gh", "issue", "create", "--title", title, "--body", body],
        capture_output=True, text=True, timeout=60)
    print(proc.stdout or proc.stderr)
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
