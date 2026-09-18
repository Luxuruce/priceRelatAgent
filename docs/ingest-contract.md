# 采集数据契约

本项目**不写爬虫**。采集交给成熟 RPA 工具，项目只定义「RPA 产出什么格式的数据」。
只要 RPA 侧按本契约产出，换任何工具下游都不用改。

---

## 一、字段定义

### 必填

| 标准字段 | 含义 | 可接受的中文表头 |
|---|---|---|
| `sku_id` | 平台内商品唯一标识 | 商品ID / 商品编码 / 货号 / sku |
| `title` | 商品标题 | 商品标题 / 商品名称 / 品名 / 名称 |

缺任一必填字段的记录会被丢弃。

### 强烈建议提供

| 标准字段 | 为什么重要 | 可接受的中文表头 |
|---|---|---|
| `price` | 没有价格就无法比价 | 售价 / 价格 / 现价 / 成交价 |
| `barcode` | **有条码就能 100% 准确匹配**，性价比最高的一个字段 | 条码 / 国际条码 / 条形码 / ean |
| `brand` | 提升 L1 规则命中率 | 品牌 / 品牌名 |
| `spec_text` | 平台标注的规格比从标题猜准 | 规格 / 包装规格 / 净含量 |

### 可选

`origin_price`（原价/划线价）、`category`（品类）、`city`（城市）、`store`（门店）、
`url`（商品链接）、`image_url`（商品图片）、`collected_at`（采集时间）。

未识别的列会原样保留在 `extra` 里，不会丢失。

> **价格字段容错**：`¥12.90`、`12.9元`、`12.90` 都能正确解析。
> **空值容错**：空单元格、`nan`、`-`、`N/A` 一律归一为空串。

---

## 二、四种对接方式

### A. 文件落盘（推荐起步）

RPA 跑完导出 CSV/Excel 到 `data/inbox/`，程序扫描导入。

**最稳的一种** —— 不依赖网络连通性，RPA 挂了也不会丢数据，出问题能直接翻原始文件。

```
文件命名：<平台key>_<任意后缀>.csv
```

| 平台 | 文件名示例 |
|---|---|
| 我方（麦德龙） | `self_商品池.csv` |
| 山姆 | `sams_20260916.csv` |
| 大润发 | `rtmart_周比价.xlsx` |
| 盒马 | `freshhema_上海.csv` |

平台 key 在 `config/config.yaml` 的 `competitors` 下定义。
导入后原始文件自动归档到 `data/raw/`，保留追溯能力。

```bash
python run.py collect    # 只导入并统计
python run.py compare    # 导入 + 匹配 + 比价 + 生成报告
```

### B. HTTP 回调（实时）

```bash
python run.py serve      # 默认监听 127.0.0.1:8770
```

RPA 流程的**最后一步**加一个「发送 HTTP 请求」指令：

```
POST http://<host>:8770/api/ingest?platform=sams
Content-Type: application/json
X-Auth-Token: <config.yaml 里配的 token，未配置则不校验>

{"items": [
  {"sku_id": "S001", "title": "农夫山泉 550ml*24瓶", "price": "39.90", "barcode": "6921168509256"},
  {"sku_id": "S002", "title": "怡宝 纯净水 555ml*24瓶", "price": "35.80"}
]}
```

也接受直接传数组 `[{...}, {...}]`。

收到的数据会落盘到 `data/inbox/<platform>_<时间戳>.json`，
再运行 `python run.py compare` 走后续流程。

> 落盘而非直接入内存，是为了 RPA 和比价流程能各自独立重跑。

### C. 主动触发（全自动编排）

```bash
python run.py trigger --robot <机器人UUID>
```

调影刀 OpenAPI 触发机器人并轮询到任务结束。

> ⚠️ **影刀 OpenAPI 没有主动回调** —— 任务跑完不会推给你，只能轮询查状态。
> 所以本命令只负责「触发 + 等待完成」，**数据传输仍需走 A 或 B**。

凭证从环境变量读，避免写进配置文件被提交：

```bash
export YINGDAO_ACCESS_KEY_ID=xxx
export YINGDAO_ACCESS_KEY_SECRET=xxx
```

### D. 云采集 API（免安装）

面向本身就跑在云上的工具：八爪鱼云采集、Apify、Firecrawl，以及数据服务商接口。
我方只调 API 把结果拉回来，落盘到 `data/inbox/` 后走与方式 A 相同的导入流程。

```bash
python run.py fetch                   # 拉取 config.yaml 里所有 enabled 的数据源
python run.py fetch --source <名称>    # 单独执行某个数据源（忽略 enabled，调试用）
python run.py compare --fetch         # 先拉取再比价
```

配置在 `config.yaml` 的 `ingest.cloud` 下，每个数据源一项。常见工具有预设，只需填 ID 和映射：

| 预设 | 模式 | 需要的环境变量 | 需要填的配置 | 接口核实情况 |
|---|---|---|---|---|
| `apify` | 拉取 Dataset | `APIFY_TOKEN` | `vars.dataset_id`、`field_map` | 已核实 |
| `firecrawl` | 逐 URL 抽取 | `FIRECRAWL_API_KEY` | `urls` | 已核实 |
| `bazhuayu` | 拉取任务数据 | `BAZHUAYU_USERNAME` / `BAZHUAYU_PASSWORD` | `vars.task_id`、`field_map` | token 接口已核实；**导出路径与数据列表字段需对照官方文档核对** |

不用预设时，URL、请求头、参数、分页、字段映射全部可配，能对接绝大多数返回 JSON 的接口：

```yaml
- name: "freshhema-vendor"
  enabled: true
  platform: "freshhema"
  url: "https://api.example.com/v1/products"
  headers:
    Authorization: "Bearer ${VENDOR_API_KEY}"   # ${...} 先查 vars，再查环境变量
  items_path: "data.items"                       # 数据列表位置；响应本身是数组则留空
  pagination:
    type: "page"                                 # none | offset | page | cursor
    page_param: "page"
    limit_param: "page_size"
    page_size: 100
  field_map:                                     # 标准字段 <- 源字段，支持点路径
    sku_id: "goodsNo"
    title: "goodsName"
    price: "priceInfo.sale"
```

适配器内置的防护：
- 429 / 5xx / 网络错误指数退避重试，遵守 `Retry-After`；4xx 不重试
- 八爪鱼 token 过期（401）自动重取一次
- `max_pages` 分页上限，服务端 offset 不推进时立即停止，避免死循环
- 日志和报错里的密钥自动替换为 `***`
- 数据拉回来但一条都导入不了（字段映射配错）时直接报错，并列出源数据字段名

> ⚠️ 云采集工具抓的是**网页**。山姆、盒马这类价格主要在 APP / 小程序里的平台，
> 更适合影刀跑在云服务器上（方式 C），或直接对接数据服务商（通用模式）。

---

## 三、各 RPA 工具选型

| 工具 | 抓真实电商站 | 对接方式 | 适合度 |
|---|---|---|---|
| **影刀 RPA** | 强，国内电商场景成熟，应用市场有现成采集机器人 | `token/v2/token/create` → `dispatch/v2/task/start` → 轮询 | ⭐ 首选 |
| **UiPath** | 强，但偏企业流程，国内电商适配要自己做 | Orchestrator API + Queue 机制 | 重，适合已有 UiPath 的企业 |
| **Coze** | 弱。LLM Agent 编排平台，没有稳定登录态/反爬能力 | Workflow + HTTP 插件 | 只适合轻量补数 |
| **八爪鱼云采集** | 中，网页类站点；有网页版，免安装 | 方式 D 预设 `bazhuayu`，或导出 CSV → 方式 A | 快速起步可选 |
| **Apify** | 中，网页类站点；有现成爬虫市场 | 方式 D 预设 `apify` | 海外团队常用 |
| **Firecrawl** | 中，网页类站点；按 schema 直接抽结构化数据 | 方式 D 预设 `firecrawl` | 页面结构多变时省配置 |
| **数据服务商** | 取决于服务商，通常覆盖 APP 类平台 | 方式 D 通用模式 | 买数据比自建便宜时首选 |

---

## 四、给 RPA 开发同学的检查清单

- [ ] 每个平台一个独立任务，输出文件名带平台 key
- [ ] **尽量抓到条码** —— 有条码的商品匹配准确率接近 100%，是投入产出比最高的字段
- [ ] 标题抓**完整原文**，不要截断，规格信息通常在标题尾部
- [ ] 价格抓**实际售价**（到手价），不是划线价；划线价单独放 `origin_price`
- [ ] 同一商品多城市/多门店时，每行带上 `city` / `store`
- [ ] 任务失败要有重试，且失败记录要能回溯到具体商品
- [ ] 采集时间统一用 `YYYY-MM-DD HH:MM:SS`
