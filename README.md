# xin-core

全新独立的 AI 运行机制实验代码。设计与契约见 [kuangjiasheji/new-system](https://github.com/gaotianci449-source/kuangjiasheji/tree/main/new-system)。

当前阶段：E1 纯文本 B 层基线实验。只用 Python 标准库，无需安装依赖。

| 文件 | 作用 |
|---|---|
| `fixtures/e1_cases.json` | 固定测试事件集（6 类用例，含预期） |
| `e1_params.json` | E1 固定的 API 参数 |
| `xin/db.py` | SQLite 表结构、连接、Backup API 快照 |
| `xin/engine.py` | 目录组装、模型输出校验、重试、短事务落盘 |
| `xin/models.py` | Mock 模型（第一层）与 DeepSeek 客户端 |
| `run_layer1.py` | 第一层：程序机制验收（Mock） |
| `e0.py` | E0 环境预检；`--api` 另测结构化输出 |
| `tests/` | 单元测试：`python3 -m unittest -v` |

## 与契约的差异

- `states` 表多了 `content` 列：契约第 1 节的字段清单没有状态内容，但预期与人工语义评估都需要它。
- 事件处理日志的 `result` 多一个 `seed`：只用于写入用例的初始数据，不会由模型产生。

## 试运行记录

- `docs/B层状态判定试运行收口-2026-11-27.md`：追认主题粒度定义与 content 列；重复确认判 keep，语义变化才 write（提示词 b-layer-v3）。
