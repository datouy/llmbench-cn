# llmbench-cn

> 国产大模型**自测选型** CLI：用你自己的网络、自己的密钥，实测 DeepSeek / Kimi / GLM / Qwen / 豆包 / SiliconFlow 等模型的速度、成本与客观任务通过率。零依赖、单文件 Python。

别人公布的排行榜测的是**别人的网络、别人的任务**；选模型这件事，自己跑 1 分钟比看 100 篇评测都准。

## 为什么做这个

- **排行榜 ≠ 你的情况**：`chinese-llm-benchmark`(6.5k) 是别人发布的结果数据仓库；`llmBench`(49) 只测本地 Ollama 运行时；没有一个工具让你用**自己的供应商密钥 + 自己的网络**做买前实测；
- **客观指标缺失**：首 token 延迟、总时延、按官方价格折算的成本——这三个数直接决定你的 Agent 体验和账单；
- **任务可验证**：内置任务带客观校验（代码真的会跑、JSON 真的会解析），不是"感觉不错"。

## 快速开始

```bash
git clone <你的仓库地址>
cd llmbench-cn

# 1. 看供应商与内置任务
python llmbench.py list
python llmbench.py list --tasks

# 2. 单模型实测（密钥走环境变量或内联）
python llmbench.py run -p deepseek -m deepseek-chat
python llmbench.py run -p deepseek:sk-xxx -m deepseek-chat -t code-fib -n 3

# 3. 多模型对比（按顺序配对 -p 与 -m）
python llmbench.py run -p deepseek -m deepseek-chat -p qwen -m qwen-plus -t code-fib,math

# 4. 导出报告
python llmbench.py run -p qwen -m qwen-plus --json --save report.json
```

## 输出示例

```
供应商      模型             任务       次数  首token(ms)  总时延(s)  输入/输出   成本      通过
----------  ---------------  ---------  ----  ------------  ---------  ----------  --------  ----
deepseek    deepseek-chat    code-fib   3/3   412          2.3        89/128      ¥0.0007   3/3
qwen        qwen-plus        code-fib   3/3   356          1.9        91/130      ¥0.0003   3/3
```

- **首 token(ms)**：流式响应中第一个字出现的延迟（响应速度体验）；
- **总时延(s)**：完整回答耗时；
- **输入/输出**：中位数 token 量；`≈` 表示供应商未返回 usage、按字符估算；
- **成本**：按官方价格（¥/$ 每百万 token）折算；供应商未收录价格时显示"见官网"；
- **通过**：`x/y` = 客观校验通过/可判定次数（`-` 表示该任务无客观校验）。

## 内置任务

| 任务 | 校验 | 说明 |
|---|---|---|
| `code-fib` | ✓ 执行验证 | 生成 Python 代码，在临时目录真实运行断言 |
| `json-extract` | ✓ 结构校验 | 中文文本结构化抽取，校验键与类型 |
| `math` | ✓ 答案校验 | 基础数学推理 |
| `sql` | - | SQL 编写（仅测指标） |
| `zh-summary` | - | 中文摘要（仅测指标，检验中文输出成本） |

任务定义在 `data/tasks.json`，欢迎 PR 添加更多任务（改 JSON 即可，无需改代码）。

## 命令一览

| 命令 | 说明 |
|---|---|
| `llmbench list [--tasks] [--json]` | 查看供应商/模型/任务 |
| `llmbench run -p 供应商[:密钥] -m 模型 [-t 任务] [-n 次数] [--json] [--save 文件] [-v]` | 运行实测 |

密钥读取顺序：`-p deepseek:sk-xxx` 内联 → 环境变量 `DEEPSEEK_API_KEY`（各供应商变量名见 `list` 与 `data/providers.json`）。

## 安全说明

- `code-fib` 会在**临时目录**中用本机 Python 运行模型生成的代码（仅运行斐波那契断言 harness）。本工具不会执行模型生成的任意命令；
- 密钥只存在于你的命令行/环境变量中，不上传、不落盘（`--save` 的报告不含密钥）；
- 请求只发往你指定的供应商端点。

## 数据说明

- 供应商数据（端点/环境变量/价格）与 `aicn` 项目同源，价格更新于 2026-10，以官方定价页为准；
- 单次采样有波动，建议 `-n 3` 取中位数。

## 路线图

- [ ] 更多客观任务：Bug 修复、代码审查、正则生成、长文摘要
- [ ] 并发对比与 token/s 吞吐指标
- [ ] 本地历史趋势存储（SQLite），追踪模型长期表现
- [ ] 质量互评模式（模型 A 判模型 B 的输出）
- [ ] 导出 Markdown 对比报告

## 项目结构

```
llmbench-cn/
├── llmbench.py           # 单文件 CLI（零依赖）
├── data/providers.json   # 供应商/价格数据
├── data/tasks.json       # 内置任务（可扩展）
├── tests/test_llmbench.py
└── docs/蓝海验证.md       # 立项依据：GitHub 竞品搜索证据
```

## License

[MIT](LICENSE)
