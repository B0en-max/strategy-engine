"""A 股**行业板块**数据（Stage 025）——证监会行业分类（来源：新浪财经公开接口）。

与 Stage 024 的「交易板块」完全不同：
    交易板块（交易所层级）：沪市主板 / 深市主板 / 创业板 / 科创板 / 北交所
    行业板块（本模块）：酒、饮料和精制茶制造业 / 货币金融服务 / 软件和信息技术服务业 …

数据源（本机实测，2026-10-07，共 115 次请求 / 22.5s / 0 失败）：
    行业清单  http://vip.stock.finance.sina.com.cn/q/view/newFLJK.php?param=industry
              → JS 变量 {"hangye_ZA01":"hangye_ZA01,农业,16,…",…}，共 84 个行业
    行业成员  http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php
              /Market_Center.getHQNodeData?page=1&num=100&sort=symbol&asc=1&node=hangye_ZA01
              → 每页 100 条；字段含 code（裸代码）/ symbol（带交易所前缀）
    覆盖      5631 只（含沪市主板 / 深市主板 / 创业板 / 科创板 / 北交所），
              未覆盖的股票会被如实统计为「未分类」，**不硬塞进某个行业**

层级说明：该接口给的是**单层**行业名（证监会大类），没有额外的层级字段；
因此不为了层级修改 schema，层级信息如实缺省。

写入策略（吸取 Stage 024 逐条写入过慢的教训）：
    - 成员关系**批量**写入：SymbolGroupRepository.bulk_add_symbols_to_group()
      （单事务 + executemany + INSERT OR IGNORE，幂等）
    - 按行业**流式**处理：每个行业「分页取成员 → 批量写」，不在内存中堆全市场成员
    - 绝不逐条写、绝不每个 symbol 做多次查询（股票 id 由库内一次读取的字典直接查）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from data_source.base import DataSourceError
from data_source.http_source import default_opener
from database import SymbolGroupRepository, SymbolRepository
from market.data.sina import decode_text

#: 行业集合在 symbol_groups.group_type 里的取值（本阶段规定使用 industry）
INDUSTRY_GROUP_TYPE = "industry"
#: description 里标识「这是行业集合」的前缀（与 Stage 024 的 trading_board= 并列）
INDUSTRY_TAG = "industry"

SINA_INDUSTRY_LIST_URL = "http://vip.stock.finance.sina.com.cn/q/view/newFLJK.php?param=industry"
SINA_INDUSTRY_MEMBERS_URL = (
    "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
    "/Market_Center.getHQNodeData"
)
SINA_INDUSTRY_PAGE_SIZE = 100
#: 防御性上限：单行业最多翻多少页（避免数据源异常时无限翻页）
SINA_INDUSTRY_MAX_PAGES = 200
SINA_INDUSTRY_SOURCE_ID = "sina_csrc"

_SINA_HEADERS: dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "*/*",
    "Referer": "https://finance.sina.com.cn/",
}
_JS_VAR_RE = re.compile(r"=\s*(\{.*\})\s*;?\s*$", re.DOTALL)
_CODE_RE = re.compile(r"^\d{6}$")

ProgressHook = Callable[[str], None]


def _noop(_: str) -> None:
    """默认进度回调：什么都不做。"""


@dataclass(frozen=True)
class Industry:
    """一个行业（证监会行业分类）。"""

    code: str
    name: str
    #: 数据源声明的成员数（拿不到时为 None；以实际抓取为准）
    declared_count: int | None = None

    @property
    def group_description(self) -> str:
        return (
            f"{INDUSTRY_TAG}={self.code} · 证监会行业分类（Stage 025，数据来源：新浪财经）"
        )


class SinaIndustrySource:
    """新浪财经「证监会行业分类」数据源（行业清单 + 行业成员）。"""

    name = SINA_INDUSTRY_SOURCE_ID

    def __init__(
        self,
        *,
        opener: Callable[[str, Mapping[str, str], float], bytes] | None = None,
        timeout: float = 15.0,
        list_url: str = SINA_INDUSTRY_LIST_URL,
        members_url: str = SINA_INDUSTRY_MEMBERS_URL,
        page_size: int = SINA_INDUSTRY_PAGE_SIZE,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._opener = opener or default_opener
        self._timeout = float(timeout)
        self._list_url = list_url
        self._members_url = members_url
        self._page_size = int(page_size)
        self._headers = dict(_SINA_HEADERS if headers is None else headers)

    # ------------------------------------------------------------------ 网络
    def _get(self, url: str) -> bytes:
        try:
            return self._opener(url, self._headers, self._timeout)
        except DataSourceError:
            raise
        except HTTPError as exc:
            raise DataSourceError(f"新浪返回 HTTP {exc.code} {exc.reason}: {url}") from exc
        except URLError as exc:
            raise DataSourceError(f"无法连接新浪: {exc.reason} -> {url}") from exc
        except OSError as exc:
            raise DataSourceError(f"请求新浪失败: {exc} -> {url}") from exc

    # ------------------------------------------------------------------ 行业清单
    def fetch_industries(self) -> list[Industry]:
        """取行业清单（一次请求）。解析 `var S_Finance_bankuai_industry = {...};`。"""
        text = decode_text(self._get(self._list_url), encodings=("gbk", "utf-8"))
        match = _JS_VAR_RE.search(text.strip())
        if match is None:
            raise DataSourceError(f"新浪行业清单格式异常（未找到 JS 变量）: {text[:120]!r}")
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise DataSourceError(f"新浪行业清单不是合法 JSON 对象: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise DataSourceError(f"新浪行业清单应为对象，实际为 {type(payload).__name__}")

        industries: list[Industry] = []
        for value in payload.values():
            parts = str(value).split(",")
            if len(parts) < 3:
                continue
            code, name = parts[0].strip(), parts[1].strip()
            try:
                count: int | None = int(parts[2])
            except ValueError:
                count = None
            if code and name:
                industries.append(Industry(code=code, name=name, declared_count=count))
        if not industries:
            raise DataSourceError("新浪行业清单解析后为空")
        return industries

    # ------------------------------------------------------------------ 行业成员
    def iter_member_pages(self, code: str) -> Iterator[list[str]]:
        """逐页取某行业的成员代码（**流式**，不一次性堆全部）。

        返回的是库内约定的**裸代码**（6 位数字）；非 6 位代码（数据源偶发噪声）被跳过。
        """
        page = 1
        while page <= SINA_INDUSTRY_MAX_PAGES:
            url = (
                f"{self._members_url}?page={page}&num={self._page_size}"
                f"&sort=symbol&asc=1&node={code}"
            )
            text = decode_text(self._get(url), encodings=("utf-8", "gbk"))
            try:
                rows = json.loads(text)
            except json.JSONDecodeError as exc:
                raise DataSourceError(f"行业 {code} 第 {page} 页不是合法 JSON: {exc}") from exc
            if not isinstance(rows, list):
                raise DataSourceError(f"行业 {code} 第 {page} 页应为数组，实际为 {type(rows).__name__}")
            if not rows:
                return
            codes = []
            for row in rows:
                raw = row.get("code") if isinstance(row, Mapping) else None
                if raw is None and isinstance(row, Mapping):
                    raw = row.get("symbol")
                text_code = str(raw or "").strip()
                if _CODE_RE.match(text_code):
                    codes.append(text_code)
            yield codes
            if len(rows) < self._page_size:
                return
            page += 1
        raise DataSourceError(
            f"行业 {code} 分页超过 {SINA_INDUSTRY_MAX_PAGES} 页仍未取完，疑似数据源异常"
        )

    def fetch_members(self, code: str) -> list[str]:
        """取某行业的全部成员代码（去重、保持顺序）。"""
        seen: dict[str, None] = {}
        for page in self.iter_member_pages(code):
            for item in page:
                seen.setdefault(item, None)
        return list(seen)


@dataclass
class IndustrySyncReport:
    """行业同步结果（字段与 CLI 报告一一对应）。"""

    dry_run: bool
    source: str
    symbols_total: int = 0
    classified: int = 0
    unclassified: int = 0
    unclassified_codes: list[str] = field(default_factory=list)
    industries_total: int = 0
    industries_created: int = 0
    industries_reused: int = 0
    membership_planned: int = 0
    membership_added: int = 0
    membership_existing: int = 0
    duplicates: list[str] = field(default_factory=list)
    source_codes_not_in_db: int = 0
    skipped_industries: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def written(self) -> int:
        return 0 if self.dry_run else self.membership_added

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "source": self.source,
            "symbols_total": self.symbols_total,
            "classified": self.classified,
            "unclassified": self.unclassified,
            "unclassified_codes": list(self.unclassified_codes),
            "industries_total": self.industries_total,
            "industries_created": self.industries_created,
            "industries_reused": self.industries_reused,
            "membership_planned": self.membership_planned,
            "membership_added": self.membership_added,
            "membership_existing": self.membership_existing,
            "duplicates": list(self.duplicates),
            "source_codes_not_in_db": self.source_codes_not_in_db,
            "skipped_industries": list(self.skipped_industries),
            "written": self.written,
            "errors": list(self.errors),
        }


def _ensure_industry_group(
    groups: SymbolGroupRepository, industry: Industry, *, dry_run: bool, report: IndustrySyncReport
) -> int | None:
    """确保行业集合存在（幂等复用）。

    安全性：若同名集合已存在但**不是行业集合**（例如与交易板块重名），
    则跳过并记录，绝不改动它（不删、不改、不塞成员）。
    """
    existing = groups.get_group_by_name(industry.name)
    if existing is not None:
        if str(existing["group_type"]) != INDUSTRY_GROUP_TYPE:
            report.skipped_industries.append(
                f"{industry.name}: 已存在同名集合（group_type={existing['group_type']}），已跳过"
            )
            return None
        report.industries_reused += 1
        return int(existing["id"])

    report.industries_created += 1
    if dry_run:
        return None
    return groups.create_group(
        industry.name,
        INDUSTRY_GROUP_TYPE,
        market="CN",
        description=industry.group_description,
    )


def sync_industries(
    groups: SymbolGroupRepository,
    symbols: SymbolRepository,
    source: SinaIndustrySource,
    *,
    dry_run: bool = True,
    progress: ProgressHook | None = None,
) -> IndustrySyncReport:
    """把行业的股票成员同步进 symbol_groups / symbol_group_members（默认只预演）。

    - 只处理 market='CN' 的股票
    - 幂等：行业集合已存在则复用；成员已存在则跳过（批量 INSERT OR IGNORE）
    - 只增不删：不删除任何集合、成员、股票、K 线
    - 未分类：数据源没有给出行业的股票如实计入 unclassified（不硬塞）
    - 一只股票出现在多个行业时计入 duplicates（如实报告，不自动取舍）
    """
    notify = progress or _noop
    report = IndustrySyncReport(dry_run=dry_run, source=source.name)

    db_rows = list(symbols.list_symbols(market="CN"))
    code_to_id: dict[str, int] = {str(row["symbol"]): int(row["id"]) for row in db_rows}
    report.symbols_total = len(code_to_id)

    industries = source.fetch_industries()
    report.industries_total = len(industries)
    notify(f"行业清单: {len(industries)} 个行业（来源 {source.name}）")

    classified_codes: set[str] = set()
    first_industry: dict[str, str] = {}

    for index, industry in enumerate(industries, 1):
        try:
            codes = source.fetch_members(industry.code)
        except Exception as exc:  # noqa: BLE001 - 单个行业失败不影响其它行业
            report.errors.append(f"{industry.name}({industry.code}): {type(exc).__name__}: {exc}")
            continue

        known_codes = [code for code in codes if code in code_to_id]
        report.source_codes_not_in_db += len(codes) - len(known_codes)
        classified_codes.update(known_codes)

        for code in codes:
            previous = first_industry.get(code)
            if previous is None:
                first_industry[code] = industry.name
            elif previous != industry.name:
                report.duplicates.append(f"{code} 同时属于「{previous}」与「{industry.name}」")

        symbol_ids = [code_to_id[code] for code in known_codes]
        report.membership_planned += len(symbol_ids)

        group_id = _ensure_industry_group(groups, industry, dry_run=dry_run, report=report)
        if group_id is None or not symbol_ids:
            notify(f"[{index}/{len(industries)}] {industry.name}: 计划 {len(symbol_ids)} 只（{'预演' if dry_run else '无成员'}）")
            continue

        try:
            added = groups.bulk_add_symbols_to_group(group_id, symbol_ids)
        except Exception as exc:  # noqa: BLE001 - 单个行业失败不影响其它行业
            report.errors.append(f"{industry.name}({industry.code}) 批量写入失败: {type(exc).__name__}: {exc}")
            continue
        report.membership_added += added
        report.membership_existing += len(symbol_ids) - added
        notify(f"[{index}/{len(industries)}] {industry.name}: {len(symbol_ids)} 只（新增 {added}）")

    report.classified = len(classified_codes)
    report.unclassified = report.symbols_total - report.classified
    report.unclassified_codes = sorted(set(code_to_id) - classified_codes)[:50]
    return report
