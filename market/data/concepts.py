"""A 股**概念板块**数据（Stage 026）——来源：新浪财经公开接口。

与前面两类完全不同：

    Stage 024 交易板块（交易所层级）：一只股票**只属于一个**（沪市主板 / 创业板 / …）
    Stage 025 行业板块（证监会行业）：一只股票**只属于一个**
    Stage 026 概念板块（本模块）    ：**多对多** —— 一只股票属于多个概念，
                                     一个概念包含多只股票（实测最多 15 个概念/股）

数据源（本机实测，2026-10-07，共 217 次请求 / 33.4s / 0 失败）：
    概念清单  http://vip.stock.finance.sina.com.cn/q/view/newFLJK.php?param=class
              → JS 变量 {"gn_hwqc":"gn_hwqc,华为汽车,97,…",…}，共 175 个概念
    概念成员  http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php
              /Market_Center.getHQNodeData?page=1&num=100&sort=symbol&asc=1&node=gn_hwqc
              → 每页 100 条；字段含 code（裸代码）
    覆盖      数据源覆盖 3265 只，其中在库 3031 只（本库 A 股 5571）
              未覆盖的股票如实计入 unclassified，**不做任何推断/编造**

写入策略（与 Stage 025 一致，复用其批量方法）：
    - 复用 SymbolGroupRepository.bulk_add_symbols_to_group()（单事务 + executemany + INSERT OR IGNORE）
    - 按概念**流式**处理：每个概念「分页取成员 → 批量写」，不在内存堆积全市场关系
    - 只增不删；不覆盖已有 industry / trading_board / Universe 数据
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from data_source.base import DataSourceError
from data_source.http_source import default_opener
from database import SymbolGroupRepository, SymbolRepository
from market.data.sina import decode_text

#: 概念集合在 symbol_groups.group_type 里的取值
CONCEPT_GROUP_TYPE = "concept"
#: description 里标识「这是概念集合」的前缀
CONCEPT_TAG = "concept"

SINA_CONCEPT_LIST_URL = "http://vip.stock.finance.sina.com.cn/q/view/newFLJK.php?param=class"
SINA_CONCEPT_MEMBERS_URL = (
    "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
    "/Market_Center.getHQNodeData"
)
SINA_CONCEPT_PAGE_SIZE = 100
#: 防御性上限：单个概念最多翻多少页（数据源异常时不会无限翻页）
SINA_CONCEPT_MAX_PAGES = 100
#: 数据源名称（写进 description，便于后续按来源区分）
SINA_CONCEPT_SOURCE_LABEL = "新浪财经概念板块"
SINA_CONCEPT_SOURCE_ID = "sina_concept"

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
class Concept:
    """一个概念板块。"""

    code: str
    name: str
    #: 数据源声明的成员数（实测该字段常被截断为 100，仅作参考）
    declared_count: int | None = None

    @property
    def group_description(self) -> str:
        return f"{CONCEPT_TAG}={self.code} · {SINA_CONCEPT_SOURCE_LABEL}（Stage 026）"


@dataclass
class ConceptSyncReport:
    """概念同步结果（字段与 CLI 输出一一对应）。"""

    dry_run: bool
    source: str
    #: 数据源侧
    concept_count: int = 0
    request_count: int = 0
    failed_requests: int = 0
    elapsed_seconds: float = 0.0
    #: 概念 × 股票 关系
    membership_count: int = 0
    membership_added: int = 0
    membership_existing: int = 0
    duplicate_memberships: int = 0
    #: 覆盖情况（库内口径）
    symbols_total: int = 0
    symbols_classified: int = 0
    symbols_unclassified: int = 0
    unclassified_codes: list[str] = field(default_factory=list)
    source_symbols_not_in_db: int = 0
    source_invalid_codes: int = 0
    #: 集合与清洗
    groups_created: int = 0
    groups_reused: int = 0
    empty_concepts: list[str] = field(default_factory=list)
    skipped_concepts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def written(self) -> int:
        return 0 if self.dry_run else self.membership_added

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "dry_run": self.dry_run,
            "concept_count": self.concept_count,
            "request_count": self.request_count,
            "failed_requests": self.failed_requests,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "membership_count": self.membership_count,
            "membership_added": self.membership_added,
            "membership_existing": self.membership_existing,
            "duplicate_memberships": self.duplicate_memberships,
            "symbols_total": self.symbols_total,
            "symbols_classified": self.symbols_classified,
            "symbols_unclassified": self.symbols_unclassified,
            "unclassified_codes": list(self.unclassified_codes),
            "source_symbols_not_in_db": self.source_symbols_not_in_db,
            "source_invalid_codes": self.source_invalid_codes,
            "groups_created": self.groups_created,
            "groups_reused": self.groups_reused,
            "empty_concepts": list(self.empty_concepts),
            "skipped_concepts": list(self.skipped_concepts),
            "written": self.written,
            "errors": list(self.errors),
        }


class SinaConceptSource:
    """新浪财经「概念板块」数据源（概念清单 + 概念成员）。"""

    name = SINA_CONCEPT_SOURCE_ID

    def __init__(
        self,
        *,
        opener: Callable[[str, Mapping[str, str], float], bytes] | None = None,
        timeout: float = 15.0,
        list_url: str = SINA_CONCEPT_LIST_URL,
        members_url: str = SINA_CONCEPT_MEMBERS_URL,
        page_size: int = SINA_CONCEPT_PAGE_SIZE,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._opener = opener or default_opener
        self._timeout = float(timeout)
        self._list_url = list_url
        self._members_url = members_url
        self._page_size = int(page_size)
        self._headers = dict(_SINA_HEADERS if headers is None else headers)
        #: 请求计数（供报告使用）
        self.request_count = 0
        self.failed_requests = 0

    # ------------------------------------------------------------------ 网络
    def _get(self, url: str) -> bytes:
        self.request_count += 1
        try:
            return self._opener(url, self._headers, self._timeout)
        except DataSourceError:
            self.failed_requests += 1
            raise
        except HTTPError as exc:
            self.failed_requests += 1
            raise DataSourceError(f"新浪返回 HTTP {exc.code} {exc.reason}: {url}") from exc
        except URLError as exc:
            self.failed_requests += 1
            raise DataSourceError(f"无法连接新浪: {exc.reason} -> {url}") from exc
        except OSError as exc:
            self.failed_requests += 1
            raise DataSourceError(f"请求新浪失败: {exc} -> {url}") from exc

    # ------------------------------------------------------------------ 概念清单
    def fetch_concepts(self) -> list[Concept]:
        """取概念清单（一次请求）。解析 `var S_Finance_bankuai_class = {...};`。"""
        text = decode_text(self._get(self._list_url), encodings=("gbk", "utf-8"))
        match = _JS_VAR_RE.search(text.strip())
        if match is None:
            raise DataSourceError(f"新浪概念清单格式异常（未找到 JS 变量）: {text[:120]!r}")
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise DataSourceError(f"新浪概念清单不是合法 JSON 对象: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise DataSourceError(f"新浪概念清单应为对象，实际为 {type(payload).__name__}")

        concepts: list[Concept] = []
        for value in payload.values():
            parts = str(value).split(",")
            if len(parts) < 2:
                continue
            code, name = parts[0].strip(), parts[1].strip()
            if not code or not name:
                continue  # 空概念名称 → 丢弃（调用方无法计数，这里不伪造）
            try:
                count: int | None = int(parts[2]) if len(parts) >= 3 else None
            except ValueError:
                count = None
            concepts.append(Concept(code=code, name=name, declared_count=count))
        if not concepts:
            raise DataSourceError("新浪概念清单解析后为空")
        return concepts

    # ------------------------------------------------------------------ 概念成员
    def iter_member_pages(self, code: str) -> Iterator[tuple[list[str], int]]:
        """逐页取某概念的成员。

        每页产出 (6 位裸代码列表, 该页被丢弃的无效代码条数)：
        —— 非 6 位代码（数据源里的非 A 股标的 / 噪声）在这里被过滤并计数，
        不进入数据库，也不静默丢失。
        """
        page = 1
        while page <= SINA_CONCEPT_MAX_PAGES:
            url = (
                f"{self._members_url}?page={page}&num={self._page_size}"
                f"&sort=symbol&asc=1&node={code}"
            )
            text = decode_text(self._get(url), encodings=("utf-8", "gbk"))
            try:
                rows = json.loads(text)
            except json.JSONDecodeError as exc:
                raise DataSourceError(f"概念 {code} 第 {page} 页不是合法 JSON: {exc}") from exc
            if not isinstance(rows, list):
                raise DataSourceError(f"概念 {code} 第 {page} 页应为数组，实际为 {type(rows).__name__}")
            if not rows:
                return
            codes: list[str] = []
            invalid = 0
            for row in rows:
                raw = row.get("code") if isinstance(row, Mapping) else None
                if raw is None and isinstance(row, Mapping):
                    raw = row.get("symbol")
                text_code = str(raw or "").strip()
                if _CODE_RE.match(text_code):
                    codes.append(text_code)
                else:
                    invalid += 1
            yield codes, invalid
            if len(rows) < self._page_size:
                return
            page += 1
        raise DataSourceError(f"概念 {code} 分页超过 {SINA_CONCEPT_MAX_PAGES} 页仍未取完，疑似数据源异常")

    def fetch_members(self, code: str) -> list[str]:
        """取某概念的全部成员代码（按出现顺序去重）。"""
        ordered: dict[str, None] = {}
        for page, _invalid in self.iter_member_pages(code):
            for item in page:
                ordered.setdefault(item, None)
        return list(ordered)


def _ensure_concept_group(
    groups: SymbolGroupRepository, concept: Concept, *, dry_run: bool, report: ConceptSyncReport
) -> int | None:
    """确保概念集合存在（幂等复用）。

    安全性：同名集合若**不是概念集合**（例如与行业/交易板块同名），
    一律跳过并记录，绝不修改它（不删、不改、不塞成员）。
    """
    existing = groups.get_group_by_name(concept.name)
    if existing is not None:
        if str(existing["group_type"]) != CONCEPT_GROUP_TYPE:
            report.skipped_concepts.append(
                f"{concept.name}: 已存在同名集合（group_type={existing['group_type']}），已跳过"
            )
            return None
        report.groups_reused += 1
        return int(existing["id"])

    report.groups_created += 1
    if dry_run:
        return None
    return groups.create_group(
        concept.name,
        CONCEPT_GROUP_TYPE,
        market="CN",
        description=concept.group_description,
    )


def sync_concepts(
    groups: SymbolGroupRepository,
    symbols: SymbolRepository,
    source: SinaConceptSource,
    *,
    dry_run: bool = True,
    progress: ProgressHook | None = None,
) -> ConceptSyncReport:
    """把概念板块的成员关系同步进数据库（默认只预演）。

    - 多对多：不做「一股一概念」假设；同一概念内重复代码会被去重并计数
    - 幂等：概念集合已存在则复用；成员已存在则跳过（INSERT OR IGNORE）
    - 只增不删：不删除任何集合 / 成员 / 股票 / K 线
    - 数据源里的非 6 位代码被过滤并计数；库中不存在的股票代码单独统计，**不自动创建**
    - 未分类：数据源没有覆盖的股票如实计入 unclassified（不推断、不编造）
    """
    notify = progress or _noop
    report = ConceptSyncReport(dry_run=dry_run, source=source.name)
    started = time.perf_counter()

    db_rows = list(symbols.list_symbols(market="CN"))
    code_to_id: dict[str, int] = {str(row["symbol"]): int(row["id"]) for row in db_rows}
    report.symbols_total = len(code_to_id)

    concepts = source.fetch_concepts()
    report.concept_count = len(concepts)
    notify(f"概念清单: {len(concepts)} 个概念（来源 {source.name}）")

    classified: set[str] = set()
    not_in_db_codes: set[str] = set()
    for index, concept in enumerate(concepts, 1):
        pages: list[list[str]] = []
        invalid = 0
        try:
            for page, page_invalid in source.iter_member_pages(concept.code):
                pages.append(page)
                invalid += page_invalid
        except Exception as exc:  # noqa: BLE001 - 单个概念失败不影响其它概念
            report.errors.append(f"{concept.name}({concept.code}): {type(exc).__name__}: {exc}")
            continue

        raw_total = sum(len(page) for page in pages)
        codes = list(dict.fromkeys(code for page in pages for code in page))
        report.source_invalid_codes += invalid
        # 同一概念内同一股票出现多次（页内或跨页）→ 去重并如实计数
        report.duplicate_memberships += raw_total - len(codes)

        # 先确保集合存在（空概念也建立集合，如实保留 0 成员，不伪造成员）
        group_id = _ensure_concept_group(groups, concept, dry_run=dry_run, report=report)
        if not codes:
            report.empty_concepts.append(concept.name)
            notify(f"[{index}/{len(concepts)}] {concept.name}: 无成员（如实留空）")
            continue

        known = [code for code in codes if code in code_to_id]
        not_in_db_codes.update(code for code in codes if code not in code_to_id)
        report.source_symbols_not_in_db = len(not_in_db_codes)
        classified.update(known)

        symbol_ids = [code_to_id[code] for code in known]
        report.membership_count += len(symbol_ids)

        if group_id is None or not symbol_ids:
            notify(f"[{index}/{len(concepts)}] {concept.name}: 计划 {len(symbol_ids)} 只")
            continue

        try:
            added = groups.bulk_add_symbols_to_group(group_id, symbol_ids)
        except Exception as exc:  # noqa: BLE001 - 单个概念失败不影响其它概念
            report.errors.append(f"{concept.name}({concept.code}) 批量写入失败: {type(exc).__name__}: {exc}")
            continue
        report.membership_added += added
        report.membership_existing += len(symbol_ids) - added
        notify(f"[{index}/{len(concepts)}] {concept.name}: {len(symbol_ids)} 只（新增 {added}）")

    report.symbols_classified = len(classified)
    report.symbols_unclassified = report.symbols_total - report.symbols_classified
    report.source_symbols_not_in_db = len(not_in_db_codes)
    report.unclassified_codes = sorted(set(code_to_id) - classified)[:50]
    report.request_count = source.request_count
    report.failed_requests = source.failed_requests
    report.elapsed_seconds = time.perf_counter() - started
    return report
