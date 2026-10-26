<div align="center">

# 🔬 llmbench-cn

**国产大模型自测选型 CLI**
*Benchmark Chinese LLMs with your own keys — speed, cost & pass rate.*

[![Stars](https://img.shields.io/github/stars/datouy/llmbench-cn?style=social)](https://github.com/datouy/llmbench-cn)
[![License](https://img.shields.io/badge/license-MIT-orange)](https://github.com/datouy/llmbench-cn/blob/main/LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/datouy/llmbench-cn/ci.yml?branch=main)](https://github.com/datouy/llmbench-cn/actions)
![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![Version](https://img.shields.io/badge/version-0.1.0-brightgreen)
![Dependencies](https://img.shields.io/badge/dependencies-0-success)
[![Sister](https://img.shields.io/badge/sister_project-aicn-blueviolet)](https://github.com/datouy/aicn)

</div>

---

## 🔗 姊妹项目 · Sister Project

> **先选型,再接入。** Pick first, then plug in.

用 llmbench-cn 实测出你要的模型之后,再用 **[aicn](https://github.com/datouy/aicn)** 一键把模型接入 Codex / Claude Code / Cursor / Cline —— 查价格、测协议、写配置,零依赖同样一步到位。

Once you've picked a winner with llmbench-cn, use **[aicn](https://github.com/datouy/aicn)** to wire it into Codex / Claude Code / Cursor / Cline in one command.

---

## 📌 这是什么 · What is llmbench-cn

别人公布的排行榜测的是**别人的网络、别人的任务**;选模型这件事,自己跑 1 分钟比看 100 篇评测都准。

Published leaderboards measure *someone else's network and tasks*. For picking a model, one minute of self-testing beats a hundred reviews.

**llmbench-cn** 用你自己的密钥、自己的网络,实测 DeepSeek / Kimi / GLM / Qwen / 豆包 / SiliconFlow 以及任意 OpenAI 兼容网关的**首 token 延迟、总时延、token 量、成本与客观任务通过率**。零依赖、单文件包。

**llmbench-cn** benchmarks Chinese LLMs — or any OpenAI-compatible gateway — with *your* keys and *your* network: first-token latency, total latency, token usage, cost and objective task pass-rate. Zero dependencies.

## 🚀 快速开始 · Quick Start

```bash
# 方式一:pip 安装 / Install
git clone https://github.com/datouy/llmbench-cn.git && cd llmbench-cn
pip install .          # 之后直接用 llmbench 命令

# 方式二:免安装 / No install (Python >= 3.9)
python -m llmbench list                          # 看供应商与任务 / providers & tasks
python -m llmbench list --tasks

# 单模型实测(密钥走环境变量或内联) / single model
python -m llmbench run -p deepseek -m deepseek-chat
python -m llmbench run -p deepseek:sk-xxx -m deepseek-chat -t code-fib -n 3

# 多模型对比(按顺序配对 -p 与 -m) / multi-model showdown
python -m llmbench run -p deepseek -m deepseek-chat -p qwen -m qwen-plus -t code-fib,math

# 任意 OpenAI 兼容网关(one-api / new-api / WorkBuddy2API-Hub…)
python -m llmbench run -p gateway:网关Key -m 模型名 --base-url http://你的网关/v1
python -m llmbench refresh --base-url http://你的网关/v1 -k 网关Key   # 一键导入网关全部模型

# 导出报告 / export report
python -m llmbench run -p qwen -m qwen-plus --json --save report.json
```

## 📊 输出示例 · Sample Output

```
供应商      模型             任务       次数  首token(ms)  总时延(s)  输入/输出   成本      通过
----------  ---------------  ---------  ----  ------------  ---------  ----------  --------  ----
deepseek    deepseek-chat    code-fib   3/3   412          2.3        89/128      ¥0.0007   3/3
qwen        qwen-plus        code-fib   3/3   356          1.9        91/130      ¥0.0003   3/3
```

### 真实网关实测 · Real-world run

2026-10-26 对 (19 个模型)实测,7 模型 × 3 任务全部通过客观校验(节选):

| 模型 | 首token(ms) | 总时延(s) | 通过率 |
|---|---|---|---|
| glm-5.3-flash | 1440–1955 | 1.4–2.0 | 3/3 |
| minimax-m3 | 1306–2345 | 1.3–2.3 | 3/3 |
| deepseek-v4.1-flash | 1583–2171 | 1.6–2.2 | 3/3 |
| glm-5.3 | 1638–3846 | 1.6–3.8 | 3/3 |
| deepseek-v4-pro | 2468–4491 | 2.5–4.5 | 3/3 |
| step-5-preview | 3357–4915 | 3.4–5.4 | 3/3 |
| kimi-k3-1 | 3988–14857 | 4.0–14.9 | 3/3 |

- **首 token(ms)** 流式响应第一个字出现的延迟 / time to first token;
- **总时延(s)** 完整回答耗时 / total latency;
- **输入/输出** 中位数 token 量,`≈` 表示供应商未返回 usage、按字符估算 / median tokens (`≈` = estimated);
- **成本** 按官方价格折算(¥/$ 每百万 token) / cost by official pricing;
- **通过** `x/y` = 客观校验通过/可判定次数 / objective pass rate.

## 🧪 内置任务 · Built-in Tasks

| 任务 / Task | 校验 / Check | 说明 / Notes |
|---|---|---|
| `code-fib` | ✓ 执行验证 | 生成 Python 代码,在临时目录真实运行断言 / code actually runs |
| `json-extract` | ✓ 结构校验 | 中文文本结构化抽取,校验键与类型 / JSON structure check |
| `math` | ✓ 答案校验 | 基础数学推理 / math reasoning |
| `sql` | - | SQL 编写(仅测指标) |
| `zh-summary` | - | 中文摘要(检验中文输出成本) |

任务定义在 `llmbench/data/tasks.json`,改 JSON 即可扩展,欢迎 PR。Tasks live in `llmbench/data/tasks.json` — extend without touching code.

## 🧰 命令一览 · Commands

| 命令 / Command | 说明 / What it does |
|---|---|
| `llmbench list [--tasks] [--json]` | 查看供应商/模型/任务 / providers & tasks |
| `llmbench run -p 供应商[:密钥] -m 模型 [-t 任务] [-n 次数] [--base-url 网关] [--json] [--save 文件] [-v]` | 运行实测 / run benchmarks |
| `llmbench refresh [-p gateway] [--base-url 网关] [-k 密钥]` | 从网关 `/models` 导入模型列表,写入数据文件 / import gateway models |

密钥读取顺序:内联 `-p deepseek:sk-xxx` → 环境变量 `DEEPSEEK_API_KEY`。Key resolution: inline first, then env var.

## 🔒 安全说明 · Security

- `code-fib` 只在**临时目录**运行模型生成的代码(仅斐波那契断言 harness),绝不执行任意命令;Generated code runs in a temp dir under a strict harness only;
- 密钥只存在于命令行/环境变量,不上传、不落盘,报告不含密钥;Keys never leave your machine;
- 请求只发往你指定的供应商端点。Requests only go to the endpoints you choose.

## 📑 数据说明 · Data

- 供应商数据与姊妹项目 [aicn](https://github.com/datouy/aicn) 同源,价格更新于 2026-10,以官方定价页为准;
- 数据超过 3 个月未更新时,CI 工作流会自动开 Issue 提醒(每周一检查);
- 单次采样有波动,建议 `-n 3` 取中位数。Run `-n 3` for stable medians.

## 🗺️ 路线图 · Roadmap

- [ ] 更多客观任务:Bug 修复、代码审查、正则生成、长文摘要
- [ ] 并发对比与 token/s 吞吐指标
- [ ] 本地历史趋势存储(SQLite)
- [ ] 质量互评模式(模型 A 判模型 B 的输出)
- [ ] 导出 Markdown 对比报告
- [ ] PyPI 发布

## 📁 项目结构 · Structure

```
llmbench-cn/
├── llmbench/
│   ├── cli.py           # CLI 主程序(零依赖)
│   └── data/            # providers.json / tasks.json
├── tests/               # 单元测试 + mock 网关端到端测试
├── scripts/             # 数据新鲜度检查(CI 用)
├── pyproject.toml       # pip 安装入口
└── docs/蓝海验证.md      # 立项依据:GitHub 竞品搜索证据
```

## 📄 License

[MIT](LICENSE)
