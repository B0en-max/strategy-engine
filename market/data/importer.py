"""A 股数据导入编排（Stage 21 Step 1）。

分层职责（与本项目既有 data_source/importer.py 一致的分工）：

    Adapter（market/data/sina.py）        只负责取数与字段映射
    Repository（SymbolRepository / …）    只负责写库（唯一约束、外键、OHLC 校验）
    本模块                                只做编排 + 统计 + dry-run 策略

两条必须成立的规则：

    1. **幂等**：同一份数据重复导入不产生重复记录
       - 股票：(market, symbol) 已存在 → 只在名称变化时 update，否则跳过
       - K 线：(symbol_id, timeframe, timestamp) 已存在 → upsert 覆盖（允许数据源修正历史）
    2. **不破坏已有数据**：单只标的失败只记一条错误并继续，
       不删除任何记录、不改 is_active、不回滚已成功的其它标的

dry-run 与正式导入**共用同一套取数 / 校验 / 裁切规则**（prepare_kline_rows），
区别只有一处：dry-run 不调用任何写库方法。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from data_source.base import KlineDataSource
from data_source.importer import prepare_kline_rows
from database import KlineRepository, SymbolRepository
from market.data.base import StockSymbol, SymbolListSource
from market.data.codes import AStockCode, normalize_a_share_code

#: 进度回调（CLI 用来打印进度；默认不输出）
ProgressHook = Callable[[str], None]


def _noop(_: str) -> None:
    """默认进度回调：什么都不做。"""


@dataclass
class SymbolSyncReport:
    """股票基础数据同步结果（统计口径与 CLI 输出一一对应）。"""

    source: str
    dry_run: bool
    mode: str = "list"  # 'list'（全量列表）或 'codes'（指定代码）
    pages: int = 0
    #: 从数据源读到的行数（含被跳过的）
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    #: 已存在且无需改动（跳过写入）
    unchanged: int = 0
    #: 数据源里有但无法使用（代码非法等）
    skipped: int = 0
    #: 写库失败
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def written(self) -> int:
        """本次实际改动数据库的条数（dry-run 时为 0）。"""
        return 0 if self.dry_run else self.inserted + self.updated

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "mode": self.mode,
            "dry_run": self.dry_run,
            "pages": self.pages,
            "fetched": self.fetched,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "failed": self.failed,
            "written": self.written,
            "errors": list(self.errors),
        }


def _sync_symbol_row(
    symbols: SymbolRepository,
    item: StockSymbol,
    *,
    dry_run: bool,
    report: SymbolSyncReport,
) -> None:
    """同步单只股票：不存在 → 新建；已存在 → 名称变化才更新；绝不删除 / 停用。"""
    existing = symbols.get_symbol(item.market, item.symbol)
    if existing is None:
        if not dry_run:
            symbols.create_symbol(item.symbol, item.name, item.market, item.asset_type, is_active=item.is_active)
        report.inserted += 1
        return

    # 已存在：只关心名称变化；is_active / asset_type 不在这里改（避免误伤人工设置）
    if str(existing["name"]) != item.name:
        if not dry_run:
            symbols.update_symbol(item.market, item.symbol, name=item.name)
        report.updated += 1
        return
    report.unchanged += 1


def sync_a_share_symbols(
    source: SymbolListSource,
    symbols: SymbolRepository,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    page_size: int = 100,
    max_pages: int = 500,
    progress: ProgressHook | None = None,
) -> SymbolSyncReport:
    """把数据源的 A 股列表同步进 symbols 表（默认只预演，不写库）。

    参数：
        limit      最多处理多少只（None = 全部）
        page_size  每页条数（数据源单页上限 100）
        max_pages  安全阀：最多翻多少页，避免接口异常时无限翻页
    """
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError(f"limit 必须是正整数或 None，实际为 {limit!r}")
    notify = progress or _noop
    report = SymbolSyncReport(source=source.name, dry_run=dry_run, mode="list")
    total = source.count()
    processed = 0  # 实际处理过的股票数（不含数据源里的坏行）

    for page in range(1, max_pages + 1):
        page_result = source.fetch_page(page=page, page_size=page_size)
        if page_result.is_empty:
            break
        report.pages += 1
        report.fetched += len(page_result.items)
        report.skipped += len(page_result.skipped)
        report.errors.extend(page_result.skipped)

        reached_limit = False
        for item in page_result.items:
            if limit is not None and processed >= limit:
                reached_limit = True
                break
            processed += 1
            try:
                _sync_symbol_row(symbols, item, dry_run=dry_run, report=report)
            except Exception as exc:  # noqa: BLE001 - 单只失败不影响整体
                report.failed += 1
                report.errors.append(f"{item.symbol} {item.name}: {type(exc).__name__}: {exc}")

        notify(
            f"第 {page} 页：累计读到 {report.fetched} 只，新增 {report.inserted}、"
            f"更新 {report.updated}、未变 {report.unchanged}、失败 {report.failed}"
        )
        if reached_limit:
            break
        if total is not None and report.pages * page_size >= total:
            break

    return report


def ensure_a_share_symbols(
    source: SymbolListSource,
    symbols: SymbolRepository,
    codes: Sequence[str],
    *,
    dry_run: bool = True,
    progress: ProgressHook | None = None,
) -> SymbolSyncReport:
    """确保指定代码的股票存在于 symbols 表（缺失时按数据源查名称）。

    用于 `--symbols 600519,000001` 这种小规模场景：不必先全量同步列表。
    """
    notify = progress or _noop
    report = SymbolSyncReport(source=source.name, dry_run=dry_run, mode="codes")
    report.fetched = len(codes)

    for raw in codes:
        try:
            code = normalize_a_share_code(raw)
        except (TypeError, ValueError) as exc:
            report.skipped += 1
            report.errors.append(f"{raw!r} 无法标准化: {exc}")
            continue

        existing = symbols.get_symbol(code.market, code.symbol)
        if existing is not None:
            report.unchanged += 1
            continue

        name = source.lookup_name(code.symbol)
        if not name:
            name = code.symbol
            report.errors.append(f"{code.display_code}: 数据源未返回名称，暂用代码作为名称")
        item = StockSymbol(
            symbol=code.symbol,
            name=name,
            market=code.market,
            exchange=code.exchange,
            source=source.name,
        )
        try:
            _sync_symbol_row(symbols, item, dry_run=dry_run, report=report)
        except Exception as exc:  # noqa: BLE001 - 单只失败不影响整体
            report.failed += 1
            report.errors.append(f"{code.display_code}: {type(exc).__name__}: {exc}")
        notify(f"{code.display_code} 处理完成（新增 {report.inserted}、失败 {report.failed}）")

    return report


@dataclass(frozen=True)
class KlineTarget:
    """一只待导入 K 线的标的（symbol_id 已存在，数据源代码由 AStockCode 提供）。"""

    symbol_id: int
    code: AStockCode
    name: str = ""

    @property
    def api_symbol(self) -> str:
        """向数据源请求时使用的代码（新浪：sh600519）。"""
        return self.code.source_symbol


@dataclass
class DailyKlineReport:
    """日 K 导入结果。"""

    source: str
    timeframe: str
    dry_run: bool
    symbols: int = 0
    symbols_with_data: int = 0
    symbols_empty: int = 0
    symbols_failed: int = 0
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    failed_rows: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def written(self) -> int:
        return 0 if self.dry_run else self.inserted + self.updated

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "timeframe": self.timeframe,
            "dry_run": self.dry_run,
            "symbols": self.symbols,
            "symbols_with_data": self.symbols_with_data,
            "symbols_empty": self.symbols_empty,
            "symbols_failed": self.symbols_failed,
            "fetched": self.fetched,
            "inserted": self.inserted,
            "updated": self.updated,
            "failed_rows": self.failed_rows,
            "written": self.written,
            "errors": list(self.errors),
        }


def import_daily_klines(
    source: KlineDataSource,
    klines: KlineRepository,
    targets: Iterable[KlineTarget],
    *,
    source_id: str = "sina",
    timeframe: str = "1d",
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
    dry_run: bool = True,
    progress: ProgressHook | None = None,
) -> DailyKlineReport:
    """把日 K 导入 klines 表（幂等；已存在的 (symbol, timeframe, timestamp) 用 upsert 覆盖）。

    「新增 / 更新」的判定：写库前先读该标的已有时间戳集合，
    待写入行里时间戳不在集合内 → 新增；已在集合内 → 更新（数据源修正历史数据的情形）。
    """
    notify = progress or _noop
    report = DailyKlineReport(source=source_id, timeframe=timeframe, dry_run=dry_run)

    for target in targets:
        report.symbols += 1
        label = target.name or target.code.display_code
        try:
            existing = {
                str(row["timestamp"])
                for row in klines.list_klines(target.symbol_id, timeframe)
            }
            fetched, rows = prepare_kline_rows(
                source,
                symbol=target.api_symbol,
                timeframe=timeframe,
                symbol_id=target.symbol_id,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
        except Exception as exc:  # noqa: BLE001 - 单只失败不影响其它标的
            report.symbols_failed += 1
            report.errors.append(f"{label}({target.api_symbol}): {type(exc).__name__}: {exc}")
            continue

        report.fetched += fetched
        if not rows:
            report.symbols_empty += 1
            notify(f"{label}: 无数据（0 根）")
            continue

        new_rows = [row for row in rows if str(row["timestamp"]) not in existing]
        updated_rows = len(rows) - len(new_rows)

        if not dry_run:
            try:
                klines.bulk_upsert_klines(rows)
            except Exception as exc:  # noqa: BLE001 - 整批回滚，已有数据不受影响
                report.symbols_failed += 1
                report.failed_rows += len(rows)
                report.errors.append(f"{label}({target.api_symbol}): 写入失败 {type(exc).__name__}: {exc}")
                continue

        report.symbols_with_data += 1
        report.inserted += len(new_rows)
        report.updated += updated_rows
        notify(f"{label}: 抓取 {fetched} 根，新增 {len(new_rows)}、更新 {updated_rows}")

    return report
