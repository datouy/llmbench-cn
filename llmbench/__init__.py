# -*- coding: utf-8 -*-
"""llmbench-cn —— 国产大模型自测选型 CLI(包入口)"""

from .cli import *  # noqa: F401,F403
from .cli import (  # noqa: F401
    VERSION,
    capture_output,
    calc_cost,
    check_contains,
    check_json_keys,
    check_py_fib,
    cmd_doctor,
    cmd_list,
    cmd_refresh,
    cmd_run,
    estimate_tokens,
    evaluate,
    extract_code_block,
    load_json,
    load_providers,
    main,
    parse_pairs,
    parse_sse_lines,
    render_markdown,
    render_table,
)

__version__ = VERSION
