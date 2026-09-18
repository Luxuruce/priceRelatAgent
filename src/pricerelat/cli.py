"""命令行入口。

    python run.py collect   # 从 inbox 导入 RPA 采集结果
    python run.py compare   # 匹配 + 比价 + 生成报告（最常用）
    python run.py serve     # 启动 HTTP 回调服务，接收 RPA 推送
    python run.py trigger   # 调影刀 OpenAPI 触发机器人任务
    python run.py fetch     # 从云采集 API（八爪鱼 / Apify / Firecrawl 等）拉取数据
    python run.py demo      # 用样例数据跑通全流程
"""

from __future__ import annotations

import argparse
import csv
import logging
import shutil
import sys
from pathlib import Path

import yaml

from .compare.engine import build_rows
from .ingest.file_collector import FileCollector
from .matching.pipeline import match_platform
from .models import Product
from .report import html as report_html

logger = logging.getLogger("pricerelat")

ROOT = Path(__file__).resolve().parents[2]
SELF_PLATFORM = "self"


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def load_config(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        raise SystemExit(f"配置文件不存在：{p}")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def collect_all(cfg: dict) -> tuple[list[Product], dict[str, list[Product]]]:
    """从 inbox 导入我方与各竞品平台的商品数据。"""
    i_cfg = cfg.get("ingest", {})
    collector = FileCollector(
        inbox_dir=_resolve(i_cfg.get("inbox_dir", "data/inbox")),
        archive_dir=_resolve(i_cfg.get("archive_dir", "data/raw")),
        encoding=i_cfg.get("encoding", "utf-8-sig"),
    )

    self_name = cfg.get("project", {}).get("self_name", "我方")
    self_products = collector.collect(SELF_PLATFORM, self_name)

    rivals: dict[str, list[Product]] = {}
    for c in cfg.get("competitors", []):
        rivals[c["key"]] = collector.collect(c["key"], c["name"])

    return self_products, rivals


def _export_review(rows, cfg: dict) -> Path | None:
    """导出待人工复核清单，对应原方案的「人工2次确认」环节。"""
    path = _resolve(
        cfg.get("matching", {}).get("review", {}).get(
            "export_path", "data/output/待人工复核.csv"
        )
    )
    records = []
    for row in rows:
        for platform, pair in row.rivals.items():
            if not pair.need_review:
                continue
            records.append(
                {
                    "我方SKU": row.self_product.sku_id,
                    "我方商品": row.self_product.title,
                    "我方规格": row.self_product.spec.display(),
                    "竞品平台": platform,
                    "竞品商品": pair.rival_product.title if pair.matched else "（判定为不匹配）",
                    "竞品规格": pair.rival_product.spec.display() if pair.matched else "",
                    "匹配层级": pair.level.value,
                    "相似度": round(pair.score, 1),
                    "置信度": round(pair.confidence, 2),
                    "判定依据": pair.reason,
                    "人工确认": "",  # 留空供人工填写：确认 / 否决 / 改配SKU
                }
            )

    if not records:
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    return path


def cmd_collect(args, cfg: dict) -> int:
    self_products, rivals = collect_all(cfg)
    print(f"\n我方商品：{len(self_products)} 个")
    for key, items in rivals.items():
        print(f"  {key}: {len(items)} 个")
    if not self_products:
        print("\n提示：未找到我方商品数据。请把文件命名为 self_*.csv 放入 data/inbox/")
    return 0


def cmd_compare(args, cfg: dict) -> int:
    if getattr(args, "fetch", False) and cmd_fetch(args, cfg) != 0:
        # 部分数据源失败不阻断比价：失败平台会在匹配阶段显示「无商品数据，跳过」
        logger.warning("部分云采集数据源拉取失败，继续使用 inbox 中已有的数据比价")

    self_products, rivals = collect_all(cfg)

    if not self_products:
        print("错误：没有我方商品数据。把 self_*.csv 放进 data/inbox/ 后重试。", file=sys.stderr)
        return 1

    parsed = sum(1 for p in self_products if p.spec.parsed)
    logger.info(
        "我方商品 %d 个，规格解析成功 %d 个（%.0f%%）",
        len(self_products), parsed, parsed / len(self_products) * 100,
    )

    # ---- 逐平台匹配 ----
    matches = {}
    for c in cfg.get("competitors", []):
        key, name = c["key"], c["name"]
        items = rivals.get(key, [])
        if not items:
            logger.warning("[%s] 无商品数据，跳过", name)
            continue
        pairs, stats = match_platform(self_products, items, cfg)
        matches[key] = pairs
        logger.info("[%s] %s", name, stats.summary())

    if not matches:
        print("错误：没有任何竞品数据。", file=sys.stderr)
        return 1

    # ---- 比价 ----
    rows = build_rows(self_products, matches, cfg)

    # ---- 输出 ----
    report_path = report_html.render(rows, cfg)
    review_path = _export_review(rows, cfg)

    priced = [r for r in rows if r.diff_rate is not None]
    higher = [r for r in priced if r.diff_rate > 0]
    print("\n" + "=" * 56)
    print(f"比价完成：{len(rows)} 个商品，{len(priced)} 个产出有效价差")
    if priced:
        avg = sum(r.diff_rate for r in priced) / len(priced)
        print(f"我方偏高 {len(higher)} 个，平均价差率 {avg * 100:+.1f}%")
    print(f"\n报告：{report_path}")
    if review_path:
        print(f"待复核清单：{review_path}")
    print("=" * 56)
    return 0


def cmd_serve(args, cfg: dict) -> int:
    from .ingest.http_server import serve

    i_cfg = cfg.get("ingest", {})
    h_cfg = i_cfg.get("http", {})
    serve(
        inbox_dir=_resolve(i_cfg.get("inbox_dir", "data/inbox")),
        host=args.host or h_cfg.get("host", "127.0.0.1"),
        port=args.port or h_cfg.get("port", 8770),
        token=h_cfg.get("token", ""),
    )
    return 0


def cmd_trigger(args, cfg: dict) -> int:
    from .ingest.yingdao import YingdaoClient, YingdaoError

    y_cfg = cfg.get("ingest", {}).get("yingdao", {})
    robot_uuid = args.robot or y_cfg.get("robot_uuid", "")
    if not robot_uuid:
        print("错误：未指定机器人，用 --robot 或在 config.yaml 填 ingest.yingdao.robot_uuid", file=sys.stderr)
        return 1

    try:
        client = YingdaoClient(
            access_key_id=y_cfg.get("access_key_id", ""),
            access_key_secret=y_cfg.get("access_key_secret", ""),
            base_url=y_cfg.get("base_url", "https://api.yingdao.com"),
        )
        info = client.run(
            robot_uuid=robot_uuid,
            schedule_uuid=y_cfg.get("schedule_uuid", ""),
            poll_interval=y_cfg.get("poll_interval_sec", 10),
            timeout=y_cfg.get("poll_timeout_sec", 1800),
        )
    except YingdaoError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1

    print(f"任务完成：{info}")
    print("提示：影刀不回传数据本身。请确认机器人已把结果落盘到 data/inbox/ "
          "或 POST 到本项目的 /api/ingest，然后运行 `python run.py compare`。")
    return 0


def cmd_fetch(args, cfg: dict) -> int:
    from .ingest.cloud_api import CloudSource, CloudSourceError

    i_cfg = cfg.get("ingest", {})
    sources = i_cfg.get("cloud") or []
    wanted = getattr(args, "source", None)

    if wanted:
        # 显式点名的数据源即使 enabled: false 也执行，方便单独调试
        selected = [s for s in sources if s.get("name") == wanted]
        if not selected:
            names = ", ".join(str(s.get("name")) for s in sources) or "（无）"
            print(f"错误：未找到数据源 {wanted!r}，已配置：{names}", file=sys.stderr)
            return 1
    else:
        selected = [s for s in sources if s.get("enabled")]
        if not selected:
            print("没有启用的云采集数据源。在 config.yaml 的 ingest.cloud 下设置 enabled: true，"
                  "或用 --source <名称> 单独执行。")
            return 0

    known_platforms = {SELF_PLATFORM} | {c["key"] for c in cfg.get("competitors", [])}
    inbox = _resolve(i_cfg.get("inbox_dir", "data/inbox"))
    failed = 0

    for source_cfg in selected:
        name = source_cfg.get("name", "?")
        if source_cfg.get("platform") not in known_platforms:
            logger.warning(
                "[%s] platform=%r 不在 competitors 中，拉回的数据不会参与比价",
                name, source_cfg.get("platform"),
            )
        try:
            path, count = CloudSource(source_cfg, inbox).fetch_to_inbox()
        except CloudSourceError as e:
            failed += 1
            logger.error("%s", e)
            continue
        print(f"  {name}: {count} 条" + (f" → {path.name}" if path else ""))

    if failed:
        print(f"\n{failed}/{len(selected)} 个数据源拉取失败，详见上方日志", file=sys.stderr)
        return 1
    return 0


def cmd_demo(args, cfg: dict) -> int:
    """把样例数据复制进 inbox 后跑完整流程，用于验证安装。"""
    samples = _resolve("data/samples")
    inbox = _resolve(cfg.get("ingest", {}).get("inbox_dir", "data/inbox"))
    inbox.mkdir(parents=True, exist_ok=True)

    files = sorted(samples.glob("*.csv"))
    if not files:
        print(f"错误：样例数据目录为空：{samples}", file=sys.stderr)
        return 1

    for f in files:
        shutil.copy(f, inbox / f.name)
    print(f"已载入 {len(files)} 份样例数据\n")

    return cmd_compare(args, cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pricerelat",
        description="自动化比价：商品匹配 → 价格对比 → HTML 报告",
    )
    parser.add_argument("-c", "--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="从 inbox 导入 RPA 采集结果并统计")
    p_compare = sub.add_parser("compare", help="匹配 + 比价 + 生成报告")
    p_compare.add_argument("--fetch", action="store_true", help="比价前先从云采集 API 拉取数据")
    sub.add_parser("demo", help="用样例数据跑通全流程")

    p_fetch = sub.add_parser("fetch", help="从云采集 API 拉取数据到 inbox")
    p_fetch.add_argument("--source", help="只执行指定名称的数据源（忽略 enabled）")

    p_serve = sub.add_parser("serve", help="启动 HTTP 回调服务接收 RPA 推送")
    p_serve.add_argument("--host", help="监听地址")
    p_serve.add_argument("--port", type=int, help="监听端口")

    p_trigger = sub.add_parser("trigger", help="触发影刀 RPA 机器人任务")
    p_trigger.add_argument("--robot", help="机器人 UUID")

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config(args.config)

    handlers = {
        "collect": cmd_collect,
        "compare": cmd_compare,
        "serve": cmd_serve,
        "trigger": cmd_trigger,
        "fetch": cmd_fetch,
        "demo": cmd_demo,
    }
    return handlers[args.command](args, cfg)
