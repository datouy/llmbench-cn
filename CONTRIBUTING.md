# 贡献指南 · Contributing

欢迎贡献!本项目是「零依赖 + 纯 JSON 数据」的设计:大部分贡献不需要会写 Python。

## 5 分钟贡献速查

| 想做什么 | 改哪里 | 是否需要写代码 |
|---|---|---|
| 更新模型价格 | `llmbench/data/providers.json` | ❌ |
| 新增供应商 | `llmbench/data/providers.json` | ❌ |
| 新增基准任务 | `llmbench/data/tasks.json` | ❌ |
| 修 bug / 加命令 | `llmbench/cli.py` | ✅ |
| 测试 | `tests/` | ✅ |

## 数据贡献(最常见)

1. **更新价格**:找到对应供应商的 `models` 列表,修改 `input` / `output`(单位:¥ 或 $ / 1M tokens),同时更新顶层 `price_updated` 为当前年月;
2. **新增供应商**:复制一个结构相似的条目,填 `name`、`cn`、`site`、`base_url`、`env_key`、`currency`、`note`,以及至少一个 `models` 条目;`responses_api` 填 `true` / `false` / `null`(null = 未确认);
3. **新增任务**:在 `tasks.json` 里加一个对象:`id`(kebab-case)、`name`、`prompt`、`max_tokens`,可选 `check`(`py_fib` / `json_keys` / `contains` / `null`)与 `check_params`。

## 代码贡献

```bash
git clone https://github.com/datouy/llmbench-cn.git
cd llmbench-cn
python -m unittest discover -s tests -v   # 全部通过再提交
```

- 保持**零第三方依赖**(标准库 only),除非有非常充分的理由;
- 命令行为不要破坏向后兼容(改动 CLI 参数需在 PR 描述中说明);
- 提交信息用中文或英文皆可,推荐 Conventional Commits 风格(`feat:` / `fix:` / `docs:`)。

## PR 流程

1. Fork → 新分支 → 改动 → 跑测试 → 提交;
2. 发起 PR,描述「改了什么 + 为什么 + 怎么验证」;
3. CI 必须全绿;数据类 PR 一般 1 天内合并。

## 需要帮助?

开一个 Issue 即可,附上你遇到的场景(供应商、模型、命令输出)会更快得到回复。
