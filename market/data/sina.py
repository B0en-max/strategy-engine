"""新浪财经 Adapter（Stage 21 Step 1）：A 股股票列表 + 历史日 K。

为什么选新浪（本阶段唯一数据源，实测依据见 README / 验收报告）：
    - 公开接口，**免费、无需 API Key**（不存在密钥泄漏问题）
    - A 股股票列表（含北交所）与长历史日 K 都能取
    - 日 K 实测单次可取 6000 根（回溯到 2001 年）；列表 100 条/页
    - Stage 12 已用同一家的 K 线接口，配置与解析能力可复用

本模块的边界：
    - **只做 Adapter**：请求 + 字段映射 + 容错 + 标准化（StockSymbol / KlineBar）
    - 不写 SQL、不决定写不写数据库（那是 importer 的职责）
    - 网络通过可注入的 opener 完成，测试一律用 mock，不打真实网络

第三方字段在这里被转换成标准结构，绝不直接进入数据库层。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Final, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import ENV_BASE_URL, ENV_TIMEOUT, ConfigError, DataSourceConfig
from data_source.base import DataSourceError
from data_source.http_source import default_opener  # 标准库 urllib 实现，复用同一套网络出口
from data_source.presets import SINA_FIELD_MAPPING, SINA_KLINE_URL, SINA_TIMEFRAME_MAPPING
from market.data.base import SINA_SOURCE_ID, StockSymbol, SymbolListSource, SymbolPage
from market.data.codes import normalize_a_share_code

#: 请求执行器：(url, headers, timeout) -> 响应字节
Opener = Callable[[str, Mapping[str, str], float], bytes]

#: A 股列表接口（node=hs_a 覆盖沪深 A 股与北交所）
SINA_A_SHARE_LIST_URL: Final[str] = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
    "/Market_Center.getHQNodeData"
)
#: A 股列表总数接口（返回形如 "5571" 的纯文本）
SINA_A_SHARE_COUNT_URL: Final[str] = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
    "/Market_Center.getHQNodeStockCount"
)
#: 列表节点：全部 A 股
SINA_A_SHARE_NODE: Final[str] = "hs_a"
#: 接口实测单页上限（num 传更大也只返回 100 条）
SINA_MAX_PAGE_SIZE: Final[int] = 100

#: 按代码查名称的轻量接口（返回形如 var suggestvalue="sh600519,11,600519,sh600519,贵州茅台,...";）
SINA_SUGGEST_URL: Final[str] = "https://suggest3.sinajs.cn/suggest/type=11"
#: suggest 接口里的 A 股类型码
SINA_SUGGEST_A_SHARE_TYPE: Final[str] = "11"

#: 日 K 单次可取根数上限（实测 datalen=6000 可用：返回 6000 根，回溯到 2001-09-17）
SINA_MAX_DAILY_BARS: Final[int] = 6000
#: 默认导入根数（约 1.3 年交易日；需要更长历史时显式传 --datalen）
SINA_DEFAULT_DAILY_BARS: Final[int] = 320

_DEFAULT_HEADERS: Final[dict[str, str]] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "*/*",
    "Referer": "https://finance.sina.com.cn/",
}

#: 新浪部分接口返回「键无引号」的伪 JSON，这里做一次最小修补后再解析
_BARE_KEY_RE: Final[re.Pattern[str]] = re.compile(r'([{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:')
_SUGGEST_RE: Final[re.Pattern[str]] = re.compile(r'var\s+suggestvalue\s*=\s*"(.*)"\s*;', re.DOTALL)


def decode_text(raw: bytes | str, *, encodings: tuple[str, ...] = ("utf-8", "gbk")) -> str:
    """解码响应体：新浪不同接口分别是 UTF-8 / GBK，逐个尝试，最后用 replace 兜底。"""
    if isinstance(raw, str):
        return raw
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode(encodings[-1], errors="replace")


def loads_tolerant(text: str) -> Any:
    """解析新浪返回的 JSON：先按严格 JSON，失败再修补「键无引号」的形态。

    仍然失败时抛 DataSourceError（错误信息里带响应片段，便于排查接口变更）。
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        repaired = _BARE_KEY_RE.sub(r'\1"\2":', text)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as exc:
            snippet = text.strip()[:200]
            raise DataSourceError(f"新浪响应不是合法 JSON: {exc}；片段={snippet!r}") from exc


class SinaAStockListSource(SymbolListSource):
    """新浪 A 股股票列表数据源。"""

    name = SINA_SOURCE_ID

    def __init__(
        self,
        *,
        opener: Opener | None = None,
        timeout: float = 10.0,
        base_url: str = SINA_A_SHARE_LIST_URL,
        count_url: str = SINA_A_SHARE_COUNT_URL,
        suggest_url: str = SINA_SUGGEST_URL,
        node: str = SINA_A_SHARE_NODE,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._opener: Opener = opener or default_opener
        self._timeout = float(timeout)
        self._base_url = base_url
        self._count_url = count_url
        self._suggest_url = suggest_url
        self._node = node
        self._headers = dict(_DEFAULT_HEADERS if headers is None else headers)

    # ------------------------------------------------------------------ 请求
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

    def _build_list_url(self, page: int, page_size: int) -> str:
        return (
            f"{self._base_url}?page={page}&num={page_size}"
            f"&sort=symbol&asc=1&node={self._node}"
        )

    # ------------------------------------------------------------------ 接口
    def fetch_page(self, *, page: int, page_size: int = SINA_MAX_PAGE_SIZE) -> SymbolPage:
        """取一页 A 股列表。

        容错规则（都会如实体现在返回里，不静默丢弃）：
            - 代码非法（非 6 位数字）→ 计入 skipped
            - 名称为空 → 用代码代替（symbols.name 非空，属于明确兜底而不是猜测）
        """
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError(f"page 必须是从 1 开始的整数，实际为 {page!r}")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= SINA_MAX_PAGE_SIZE:
            raise ValueError(f"page_size 必须是 1~{SINA_MAX_PAGE_SIZE} 之间的整数，实际为 {page_size!r}")

        url = self._build_list_url(page, page_size)
        payload = loads_tolerant(decode_text(self._get(url)))
        if not isinstance(payload, list):
            raise DataSourceError(f"新浪列表响应应为 JSON 数组，实际为 {type(payload).__name__}")

        items: list[StockSymbol] = []
        skipped: list[str] = []
        for raw in payload:
            if not isinstance(raw, Mapping):
                skipped.append(f"非对象行: {raw!r}")
                continue
            raw_code = raw.get("symbol") or raw.get("code")
            try:
                code = normalize_a_share_code(raw_code)
            except (TypeError, ValueError) as exc:
                skipped.append(f"代码无法标准化({raw_code!r}): {exc}")
                continue
            name = str(raw.get("name") or "").strip() or code.symbol
            items.append(
                StockSymbol(
                    symbol=code.symbol,
                    name=name,
                    market=code.market,
                    exchange=code.exchange,
                    source=self.name,
                )
            )
        return SymbolPage(page=page, page_size=page_size, items=tuple(items), skipped=tuple(skipped))

    def count(self) -> int | None:
        """A 股总数（拿不到时返回 None，不猜测）。"""
        url = f"{self._count_url}?node={self._node}"
        try:
            text = decode_text(self._get(url)).strip()
        except DataSourceError:
            return None
        try:
            return int(loads_tolerant(text))
        except (DataSourceError, TypeError, ValueError):
            return None

    def lookup_name(self, code: str) -> str | None:
        """按 6 位代码查股票名称（用于 --symbols 指定代码时补名称）。"""
        bare = normalize_a_share_code(code).symbol
        text = decode_text(self._get(f"{self._suggest_url}&key={bare}"))
        match = _SUGGEST_RE.search(text)
        if match is None:
            return None
        payload = match.group(1).strip()
        if not payload:
            return None
        for row in payload.split(";"):
            fields = row.split(",")
            if len(fields) < 5:
                continue
            if fields[2].strip() == bare and fields[1].strip() == SINA_SUGGEST_A_SHARE_TYPE:
                name = fields[4].strip()
                if name:
                    return name
        return None


def sina_daily_kline_config(
    *,
    datalen: int = SINA_DEFAULT_DAILY_BARS,
    timeout: float | None = None,
    base_url: str | None = None,
    env: Mapping[str, str] | None = None,
) -> DataSourceConfig:
    """构造新浪**日 K**数据源配置（字段映射与 Stage 12 完全一致）。

    与 data_source.presets.sina_cn_kline_config 的区别只有一处：
    这里的 datalen 上限放宽到实测可用值（6000），用于导入更长历史；
    地址 / 周期映射 / 字段映射 / 时间语义全部复用同一份常量，不另起一套规则。
    """
    if isinstance(datalen, bool) or not isinstance(datalen, int) or not 1 <= datalen <= SINA_MAX_DAILY_BARS:
        raise ConfigError(f"datalen 必须是 1~{SINA_MAX_DAILY_BARS} 之间的整数，实际为 {datalen!r}")

    source = os.environ if env is None else env
    resolved_url = base_url or source.get(ENV_BASE_URL) or SINA_KLINE_URL
    if timeout is None:
        raw_timeout = source.get(ENV_TIMEOUT)
        try:
            timeout = float(raw_timeout) if raw_timeout else 10.0
        except ValueError as exc:
            raise ConfigError(f"{ENV_TIMEOUT} 必须是数字，实际为 {raw_timeout!r}") from exc

    return DataSourceConfig(
        base_url=resolved_url,
        api_key=None,
        timeout=timeout,
        extra_params={"ma": "no", "datalen": str(datalen)},
        symbol_param="symbol",
        timeframe_param="scale",
        start_param=None,  # 该接口不支持起止时间参数（由导入层本地裁切）
        end_param=None,
        timeframe_mapping=dict(SINA_TIMEFRAME_MAPPING),
        request_timestamp_format="datetime",
        response_timestamp_format="datetime",
        timestamp_semantics="end",  # 日内周期标签是 K 线结束时间（日线是日期，不回退）
        response_path=None,
        field_mapping=dict(SINA_FIELD_MAPPING),
    )


def make_a_share_list_source(
    *,
    timeout: float = 10.0,
    opener: Opener | None = None,
) -> SinaAStockListSource:
    """按默认参数创建新浪 A 股列表数据源（CLI 使用）。"""
    return SinaAStockListSource(opener=opener, timeout=timeout, node=SINA_A_SHARE_NODE)


__all__ = [
    "Opener",
    "SINA_A_SHARE_COUNT_URL",
    "SINA_A_SHARE_LIST_URL",
    "SINA_A_SHARE_NODE",
    "SINA_DEFAULT_DAILY_BARS",
    "SINA_MAX_DAILY_BARS",
    "SINA_MAX_PAGE_SIZE",
    "SINA_SUGGEST_URL",
    "SinaAStockListSource",
    "decode_text",
    "loads_tolerant",
    "make_a_share_list_source",
    "sina_daily_kline_config",
]
