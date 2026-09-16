# 自动化比价系统

商超比价的核心链路：**商品匹配 → 价格对比 → 报告输出**。

参考麦德龙商超比价方案简化而来，聚焦最有业务价值的两段，去掉了周期调度、
多项目管理、飞书多维表格协作等重型环节。

---

## 设计要点

**采集层使用已有 RPA。** 项目不写爬虫 —— 反爬、登录态、页面改版是持续消耗，
交给影刀 / UiPath / 八爪鱼这类成熟工具更划算。项目只定义数据契约，
换任何采集工具下游都不用改。详见 [采集数据契约](docs/ingest-contract.md)。

**比价必须落在单位价上。** `500ml×6` 和 `1.5L` 的标价没有可比性。
系统从标题解析净含量与件数，统一折算成「元/100g」「元/100ml」「元/件」再对比。
解析不出来的降级为标价对比，并在报告中显式标注。

**匹配分三层，成本递增。** 准确的用零成本方法解决，只有真正模糊的才调模型：

```
L1 硬规则   条码命中 / 品牌+品类+规格一致        零成本，准确率≈100%
    ↓ 未命中
L2 模糊召回  文本相似度 + 规格差异扣分            零成本，Top-K 候选
    ↓ 落在灰区（55~88分）
L3 AI 判定   Claude 结构化输出裁决                仅灰区调用，成本可控
    ↓ 低置信
人工复核     导出 CSV 清单                        对应原方案的「人工2次确认」
```

**错配比漏配代价大。** 错配会直接改错价，漏配只是回到人工。所以：
- 口味冲突（红烧 vs 香辣）→ **硬排除**
- 产品线不一致（蒙牛纯牛奶 vs 特仑苏）→ **降级到灰区**，不硬排除也不直接接受
- 低置信匹配一律标记待复核，不静默当成匹配

---

## 快速开始

```bash
# 1. 装依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 用样例数据跑通全流程
.venv/bin/python run.py demo
```

跑完会生成：
- `data/output/比价报告.html` —— 可视化报告，带筛选和搜索
- `data/output/待人工复核.csv` —— 低置信匹配清单

### 用自己的数据

把 CSV 按 `<平台key>_<后缀>.csv` 命名放进 `data/inbox/`：

```
data/inbox/
├── self_麦德龙商品池.csv     ← 我方商品（平台 key 固定为 self）
├── sams_山姆.csv
├── rtmart_大润发.csv
└── freshhema_盒马.csv
```

必填列只有两个：`商品标题`、`商品ID`。建议再加 `售价`、`条码`、`品牌`。
完整字段说明见 [采集数据契约](docs/ingest-contract.md)。

```bash
.venv/bin/python run.py compare
```

### 启用 AI 兜底匹配

```bash
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python run.py compare
```

未配置密钥时 L3 自动跳过，灰区候选全部转人工复核，主流程不受影响。

---

## 命令

| 命令 | 作用 |
|---|---|
| `run.py demo` | 用样例数据跑通全流程，验证安装 |
| `run.py collect` | 只导入 inbox 数据并统计，不比价 |
| `run.py compare` | 导入 + 匹配 + 比价 + 生成报告（最常用） |
| `run.py serve` | 启动 HTTP 服务接收 RPA 推送 |
| `run.py trigger --robot <UUID>` | 触发影刀机器人任务 |

加 `-v` 看详细日志，加 `-c <路径>` 指定配置文件。

---

## 配置

全部在 [`config/config.yaml`](config/config.yaml)，几个关键项：

```yaml
matching:
  fuzzy:
    auto_accept: 88      # ≥该分直接接受，不调 AI
    reject_below: 55     # <该分直接丢弃
    # 两者之间为灰区，交 L3 判定 —— 调窄可省钱，调宽可提高召回

compare:
  benchmark: "min"       # 跟价基准：min=最低价竞品 | avg=竞品均价 | 指定平台key
  thresholds:
    critical_high: 0.15  # 我方贵出15%以上 → 建议降价(高优)
    warn_low: -0.10      # 便宜10%以上 → 有提价空间
```

---

## 项目结构

```
src/pricerelat/
├── models.py              Product / MatchPair / CompareRow 三个核心模型
├── ingest/                采集层 —— RPA 对接
│   ├── base.py              数据契约、字段映射、标准化
│   ├── file_collector.py    方式A：文件落盘（CSV/Excel/JSON）
│   ├── http_server.py       方式B：HTTP 回调接收
│   └── yingdao.py           方式C：影刀 OpenAPI 触发
├── normalize/             标准化层
│   ├── spec_parser.py       规格解析与单位折算
│   └── text.py              标题清洗、品牌/品类/口味/产品线识别
├── matching/              匹配层
│   ├── rules.py             L1 硬规则
│   ├── fuzzy.py             L2 模糊召回
│   ├── llm.py               L3 AI 判定
│   └── pipeline.py          分层编排
├── compare/engine.py      比价计算与跟价建议
├── report/                HTML 报告
└── cli.py                 命令行入口
```

---

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

测试重点覆盖两类曾导致**静默错误结论**的问题：
- pandas 把空单元格读成 `NaN`，`str(NaN)` 得到 `"nan"`，导致所有空条码互相匹配
- `token_set_ratio` 对子集返回满分，导致「蒙牛纯牛奶」被判为等同「蒙牛特仑苏纯牛奶」

---
