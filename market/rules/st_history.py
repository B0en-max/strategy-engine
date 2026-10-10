"""历史风险警示（ST/*ST）事件解析与状态查询（Stage 027′-C1/C2）。

目标
------------------------------------------------------------------
把**巨潮资讯网公告**（交易所指定信息披露平台）还原成结构化的风险警示事件，
并据此在**不猜**的前提下回答"某交易日某类风险警示是否生效"。

本模块只做**解析与判定**（纯函数：文本/字节或事件进，结构出）：不联网、不连数据库、不改 schema。
公告的检索与下载由调用方完成（见 verify 中的查询流程）。

铁律（与项目"不猜数据"约定一致）
------------------------------------------------------------------
    - **公告日期 ≠ 停牌日期 ≠ 实施/撤销生效日期**，四者可相差数日，绝不互相替代；
    - 生效日期**只取自公告正文**的明确表述；正文无法确认时字段为 None（unknown），
      **绝不返回"看似有效"的默认日期**；
    - **不得**用 K 线涨跌幅反推 ST 日期；也**不得**把当前证券简称当作历史 ST 状态；
    - 事件链不完整或冲突时，状态查询返回 `unknown`（**绝不默认"非 ST"**）；
    - 解析失败（无正文 / 结构无法识别）显式返回 None 或抛 AnnouncementParseError。

两类风险警示必须独立表达
------------------------------------------------------------------
    退市风险警示（*ST，SCOPE_DELISTING）与其他风险警示（ST，SCOPE_OTHER）是**两条独立链**，
    各自有 实施/撤销。**撤销一种不得影响另一种**：公告"撤销退市风险警示并继续被实施其他
    风险警示"要同时表达"退市链撤销"与"其他链继续生效"（用 RiskWarningEvent.continues 记录）。

已验证样本（正文均含结构化"起始日"字段；来源：巨潮资讯网）
------------------------------------------------------------------
    600053 沪市主板 实施退市风险警示  停牌 2026-04-29 / 实施 2026-04-30
    688033 科创板   实施退市风险警示  停牌 2026-04-30 / 实施 2026-05-06
    600238 沪市主板 撤销退市风险警示  停牌 2026-07-31 / 撤销 2026-08-03（并继续其他风险警示）
    000820 深市主板 撤销退市风险警示  停牌 2026-07-29 / 撤销 2026-07-30
    300211 创业板   撤销退市风险警示  停牌 2026-09-28 / 撤销 2026-09-29
    300527 创业板   撤销其他风险警示  停牌 2026-09-29 / 撤销 2026-09-30

已知局限
------------------------------------------------------------------
    - 只处理**文本型 PDF**（含 ToUnicode 映射）；扫描/图片型 PDF 一律抛错，不猜；
    - 公告措辞存在版本差异，规则基于已验证样本，覆盖不到时返回 None；
    - 事件**完整性**无法保证（历史/更早公告可能未入库或不可解析）→ 状态查询会显式标注
      `complete=False` 或返回 `unknown`，区间构造同样标注不完整。
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from datetime import date
from typing import Final, Iterable, Sequence

# --------------------------------------------------------------------------- 常量
EVENT_IMPLEMENT: Final[str] = "implement"   # 被实施风险警示
EVENT_REVOKE: Final[str] = "revoke"         # 撤销风险警示

SCOPE_DELISTING: Final[str] = "delisting_risk"  # 退市风险警示（*ST）
SCOPE_OTHER: Final[str] = "other_risk"          # 其他风险警示（ST）
SCOPE_UNKNOWN: Final[str] = "unknown"

#: 状态查询结果
STATUS_ACTIVE: Final[str] = "active"        # 该日该类风险警示生效
STATUS_INACTIVE: Final[str] = "inactive"    # 该日该类风险警示已撤销
STATUS_UNKNOWN: Final[str] = "unknown"      # 无法判定（链不完整/冲突/无记录）

#: 内部动作（状态机用）：某类风险警示"继续生效"的声明
_ACTION_CONTINUE: Final[str] = "continue"

_DATE_CN = r"(\d{4})\s*年\s*(\d{1,2})\s*月\s{0,3}(\d{1,2})\s*日"
_DATE_ANY = r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s{0,3}(\d{1,2})\s*日?"


class AnnouncementParseError(ValueError):
    """公告解析失败：缺正文 / 非文本型 PDF / 正文无法还原。**不返回默认值。**"""


# --------------------------------------------------------------------------- 日期
def _iso(y: str, m: str, d: str) -> str | None:
    try:
        return date(int(y), int(m), int(d)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _first_date(text: str, patterns: Sequence[str]) -> str | None:
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            iso = _iso(m.group(1), m.group(2), m.group(3))
            if iso is not None:
                return iso
    return None


# --------------------------------------------------------------------------- PDF 正文
def extract_text_from_pdf(raw: bytes) -> str:
    """从**文本型** PDF 提取正文（解析 ToUnicode CMap + 内容流 CID）。

    失败一律抛 AnnouncementParseError（扫描件、无映射、空正文），不返回空串冒充成功。
    """
    if not isinstance(raw, (bytes, bytearray)) or not bytes(raw).lstrip().startswith(b"%PDF"):
        raise AnnouncementParseError("不是 PDF 内容（缺少 %PDF 头）")
    raw = bytes(raw)

    streams: list[bytes] = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.DOTALL):
        try:
            streams.append(zlib.decompress(m.group(1)))
        except Exception:  # noqa: BLE001 - 非 Flate 流跳过
            continue

    cmap: dict[str, str] = {}
    for s in streams:
        for blk in re.findall(rb"beginbfchar(.*?)endbfchar", s, re.DOTALL):
            for a, b in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
                try:
                    cmap[a.decode().upper()] = bytes.fromhex(b.decode()).decode("utf-16-be", "ignore")
                except Exception:  # noqa: BLE001
                    continue
        for blk in re.findall(rb"beginbfrange(.*?)endbfrange", s, re.DOTALL):
            for a, b, c in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
                lo, hi, dst = int(a, 16), int(b, 16), int(c, 16)
                for i in range(lo, hi + 1):
                    cmap[f"{i:04X}"] = chr(dst + (i - lo))

    if not cmap:
        raise AnnouncementParseError("未找到 ToUnicode 映射：可能为扫描/图片型 PDF，无法可靠提取")

    chunks: list[str] = []
    for s in streams:
        if b"BT" not in s:
            continue
        for m in re.finditer(rb"<([0-9A-Fa-f\s]{4,})>", s):
            hx = re.sub(rb"\s", b"", m.group(1)).decode()
            if len(hx) % 4:
                continue
            chunks.append("".join(cmap.get(hx[i:i + 4], "") for i in range(0, len(hx), 4)))

    text = "".join(chunks)
    if not any("\u4e00" <= ch <= "\u9fff" for ch in text):
        raise AnnouncementParseError("未从 PDF 还原出中文正文（映射或编码不匹配）")
    return text


# --------------------------------------------------------------------------- 事件
@dataclass(frozen=True)
class RiskWarningEvent:
    """一条风险警示事件（实施或撤销）。

    所有日期均为 'YYYY-MM-DD' 或 None（None = 正文未确认，**不是**默认值）。
    """

    kind: str                       # EVENT_IMPLEMENT / EVENT_REVOKE
    scope: str                      # SCOPE_DELISTING / SCOPE_OTHER / SCOPE_UNKNOWN
    effective_date: str | None      # 实施/撤销的生效起始日
    suspension_date: str | None     # 停牌日
    resume_date: str | None         # 复牌日
    title: str
    basis: str                      # 判定依据（哪句话支撑）
    code: str | None = None
    #: 本公告生效同时"继续实施"的**其它**范围（起点未知，仅表示自 effective_date 起仍在实施）。
    #: 例：撤销退市风险警示并继续被实施其他风险警示 → (SCOPE_OTHER,)
    continues: tuple[str, ...] = ()


def _classify_title(title: str) -> tuple[str, str] | None:
    """由公告标题判定事件类型与范围；不是"实施/撤销风险警示的正式公告"时返回 None。"""
    t = re.sub(r"<[^>]+>", "", title or "")  # 检索结果标题可能带 <em> 高亮标签
    if "风险警示" not in t:
        return None
    if "撤销" in t:
        # "申请撤销"型公告只是董事会/公司申请，不含生效日 → 不是事件公告
        if "申请" in t and "暨" not in t:
            return None
        kind = EVENT_REVOKE
        if "撤销退市风险警示" in t:
            scope = SCOPE_DELISTING
        elif "撤销其他风险警示" in t:
            scope = SCOPE_OTHER
        else:
            scope = SCOPE_UNKNOWN
        return kind, scope
    # 实施类
    if "实施" not in t:
        return None
    if "可能" in t or "拟" in t:
        # "可能/拟被实施…"是提示性公告，不是实际实施事件 → 不当作事件
        return None
    if "退市风险警示" in t:
        return EVENT_IMPLEMENT, SCOPE_DELISTING
    if "其他风险警示" in t:
        return EVENT_IMPLEMENT, SCOPE_OTHER
    return EVENT_IMPLEMENT, SCOPE_UNKNOWN


def _detect_continues(text: str) -> tuple[str, ...]:
    """从正文识别"撤销某类警示的同时继续实施另一类警示"的范围。"""
    found: list[str] = []
    if re.search(r"继续(?:被)?实施其他风险警示", text):
        found.append(SCOPE_OTHER)
    if re.search(r"继续(?:被)?实施退市风险警示", text):
        found.append(SCOPE_DELISTING)
    return tuple(found)


_SUSPENSION_PATTERNS = (
    rf"(?:于|自)\s*{_DATE_CN}[^。；\n]{{0,12}}?停牌",
    rf"停牌(?:日期为|时间)?[^。；\n]{{0,8}}?{_DATE_CN}",
)
_RESUME_PATTERNS = (
    # 复牌日必须与日期紧邻（允许中间只有"（星期X）"与"开市起/起"），否则会误取前一个停牌日
    rf"{_DATE_CN}\s*(?:（[^）]{{0,10}}）)?\s*(?:开市起|起)?复牌",
    rf"复牌(?:日|日期)[^。；\n]{{0,8}}?{_DATE_CN}",
)


def parse_risk_warning_event(*, title: str, text: str) -> RiskWarningEvent | None:
    """解析一条公告，返回风险警示事件；无法可靠识别时返回 None。

    生效日期**只从正文明确的"起始日/起始日期"或"自…（开市起）撤销/实施"表述取**；
    取不到时字段为 None（显式 unknown），绝不用公告日期、停牌日期或 K 线反推填充。
    """
    clean_title = re.sub(r"<[^>]+>", "", title or "").strip()
    classified = _classify_title(clean_title)
    if classified is None:
        return None
    kind, scope = classified

    if kind == EVENT_REVOKE:
        effective_patterns = (
            rf"撤销[^。；\n]{{0,40}}?起始日[期]?\s*(?:[：:]|为)\s*(?:自)?\s*{_DATE_CN}",
            rf"自{_DATE_CN}[^。；\n]{{0,12}}?(?:开市起)?撤销",
            rf"于{_DATE_CN}[^。；\n]{{0,20}}?撤销",
        )
    else:
        effective_patterns = (
            rf"实施[^。；\n]{{0,30}}?起始日[期]?\s*(?:[：:]|为)\s*{_DATE_CN}",
            rf"(?:被)?实施[^。；\n]{{0,24}}?起始日\s*为\s*{_DATE_CN}",
            rf"自{_DATE_CN}[^。；\n]{{0,12}}?起(?:被)?实施",
            rf"于{_DATE_CN}[^。；\n]{{0,20}}?起?(?:被)?实施",
        )

    effective = _first_date(text, effective_patterns)
    suspension = _first_date(text, _SUSPENSION_PATTERNS)
    resume = _first_date(text, _RESUME_PATTERNS)

    code_m = re.search(r"证券代码[：:]\s*(\d{6})", text)
    code = code_m.group(1) if code_m else None

    label = "撤销" if kind == EVENT_REVOKE else "实施"
    if effective is None:
        basis = f"{label}公告：正文未找到明确的生效起始日（不猜）"
    else:
        basis = f"{label}公告：正文明确 {effective} 为起始日"
    return RiskWarningEvent(
        kind=kind, scope=scope, effective_date=effective,
        suspension_date=suspension, resume_date=resume,
        title=clean_title, basis=basis, code=code,
        continues=_detect_continues(text),
    )


# --------------------------------------------------------------------------- 区间
@dataclass(frozen=True)
class StInterval:
    """一段风险警示区间（实施 → 撤销）。

    end_date 为 None 表示"截至已知事件仍未撤销"；complete=False 表示事件链不完整（不猜）。
    **区间只描述单一范围**；"撤销退市但继续其他"这类跨范围信息见 `continues` 与状态查询。
    """

    scope: str
    start_date: str | None
    end_date: str | None
    complete: bool
    basis: str


def build_st_intervals(events: Iterable[RiskWarningEvent]) -> tuple[StInterval, ...]:
    """把同一范围（退市/其他）的实施-撤销事件配成区间。

    处理规则：
        - **去重**：同 (kind, effective_date) 视为重复公告，只保留一条；
        - **同日冲突**（同范围同一天既有实施又有撤销）→ 只产出一条 complete=False 的冲突标记，
          不硬配成"实施 d → 撤销 d"这种看似合法的区间；
        - 只有撤销没有实施、连续两次实施 等不完整情形 → complete=False，**不推测缺失日期**。
    只在事件链完整（实施→撤销 交替、且以实施开头）时给 complete=True。
    """
    by_scope: dict[str, list[RiskWarningEvent]] = {}
    for ev in events:
        if ev.effective_date is None:
            continue
        by_scope.setdefault(ev.scope, []).append(ev)

    intervals: list[StInterval] = []
    for scope, evs in by_scope.items():
        unique: dict[tuple[str, str], RiskWarningEvent] = {}
        for ev in evs:
            unique.setdefault((ev.kind, ev.effective_date or ""), ev)
        ordered = sorted(unique.values(), key=lambda e: (e.effective_date or "", e.kind))

        kinds_by_date: dict[str, set[str]] = {}
        for ev in ordered:
            kinds_by_date.setdefault(ev.effective_date or "", set()).add(ev.kind)
        conflicts = sorted(d for d, ks in kinds_by_date.items() if len(ks) > 1)
        if conflicts:
            intervals.append(StInterval(scope, None, None, False,
                                        f"同日同时存在实施与撤销（冲突）：{'、'.join(conflicts)}，无法配成区间"))
            continue

        open_start: str | None = None
        for ev in ordered:
            if ev.kind == EVENT_IMPLEMENT:
                if open_start is not None:
                    intervals.append(StInterval(scope, open_start, None, False,
                                                f"两次实施之间缺少撤销公告（{open_start} → {ev.effective_date}）"))
                open_start = ev.effective_date
            else:  # revoke
                if open_start is None:
                    intervals.append(StInterval(scope, None, ev.effective_date, False,
                                                f"缺少对应的实施公告（撤销于 {ev.effective_date}）"))
                else:
                    intervals.append(StInterval(scope, open_start, ev.effective_date, True,
                                                f"实施 {open_start} → 撤销 {ev.effective_date}"))
                    open_start = None
        if open_start is not None:
            intervals.append(StInterval(scope, open_start, None, False,
                                        f"截至已知事件未见撤销（自 {open_start} 起）"))
    intervals.sort(key=lambda i: (i.scope, i.start_date or i.end_date or ""))
    return tuple(intervals)


# --------------------------------------------------------------------------- 状态查询
@dataclass(frozen=True)
class ScopeStatus:
    """某范围（退市/其他）在某交易日的状态。

    status: STATUS_ACTIVE（已确认实施）/ STATUS_INACTIVE（已确认撤销）/ STATUS_UNKNOWN（无法判定）。
    complete: 该范围已知事件链是否完整（以实施开头、实施/撤销交替、无未知日期事件、无冲突）。
    """

    scope: str
    date: str
    status: str
    complete: bool
    basis: str


def _scope_chain(events: Sequence[RiskWarningEvent], scope: str) -> tuple[list[tuple[str, str]], bool]:
    """取某范围的有日期变更（含"继续实施"声明）；返回 (changes, 是否存在无日期事件)。"""
    changes: list[tuple[str, str]] = []
    has_unknown_date = False
    for ev in events:
        if ev.scope == scope and ev.kind in (EVENT_IMPLEMENT, EVENT_REVOKE):
            if ev.effective_date is None:
                has_unknown_date = True
            else:
                changes.append((ev.effective_date, ev.kind))
        if scope in getattr(ev, "continues", ()) and ev.effective_date:
            changes.append((ev.effective_date, _ACTION_CONTINUE))
    changes = sorted(set(changes))  # 去重：重复公告不改变事件链
    return changes, has_unknown_date


def _chain_complete(changes: Sequence[tuple[str, str]], has_unknown_date: bool) -> bool:
    """事件链是否完整：以实施开头、同范围实施/撤销交替、无未知日期事件、无同日冲突。"""
    if has_unknown_date or not changes:
        return False
    if changes[0][1] != EVENT_IMPLEMENT:
        return False
    for (d1, a1), (d2, a2) in zip(changes, changes[1:]):
        if a1 == a2 or d1 == d2:
            return False
    return True


def query_risk_warning_status(
    events: Iterable[RiskWarningEvent], *, date: str
) -> tuple[ScopeStatus, ...]:
    """查询某交易日的两类风险警示状态（退市风险警示、其他风险警示各自一条）。

    契约（**不猜**）：
        - 无任何事件记录、早于首个已知变更、存在同日冲突 → STATUS_UNKNOWN；
        - "撤销"只有在事件链完整时才判为 STATUS_INACTIVE（否则 UNKNOWN，**不默认非 ST**）；
        - "实施"或公告明确的"继续实施" → STATUS_ACTIVE；
        - 当前简称不作为任何输入，绝不用它推断历史状态。
    """
    materialized = list(events)
    return tuple(_scope_status(materialized, scope, date) for scope in (SCOPE_DELISTING, SCOPE_OTHER))


def _scope_status(events: Sequence[RiskWarningEvent], scope: str, date: str) -> ScopeStatus:
    changes, has_unknown_date = _scope_chain(events, scope)
    if not changes:
        return ScopeStatus(scope, date, STATUS_UNKNOWN, False,
                           "该范围无任何事件记录：不得默认认定为非 ST（不猜）")

    kinds_by_date: dict[str, set[str]] = {}
    for d, action in changes:
        kinds_by_date.setdefault(d, set()).add(action)
    conflicts = sorted(d for d, ks in kinds_by_date.items() if len(ks) > 1 and d <= date)
    if conflicts:
        return ScopeStatus(scope, date, STATUS_UNKNOWN, False,
                           f"存在同日冲突：{'、'.join(conflicts)}，无法判定")

    if date < changes[0][0]:
        return ScopeStatus(scope, date, STATUS_UNKNOWN, False,
                           f"早于已知首个变更（{changes[0][0]}）：此前历史未知（不猜）")

    complete = _chain_complete(changes, has_unknown_date)
    latest_date, latest_action = [c for c in changes if c[0] <= date][-1]

    if latest_action == _ACTION_CONTINUE:
        return ScopeStatus(scope, date, STATUS_ACTIVE, complete,
                           f"{latest_date} 起明确继续实施（该范围起点未知）")
    if latest_action == EVENT_IMPLEMENT:
        note = "事件链完整" if complete else "事件链不完整（缺撤销或非交替），但该日实施状态由公告直接支撑"
        return ScopeStatus(scope, date, STATUS_ACTIVE, complete, f"最近一次变更为实施（{latest_date}）；{note}")
    # revoke：只有链完整才敢判 INACTIVE
    if complete:
        return ScopeStatus(scope, date, STATUS_INACTIVE, True,
                           f"已撤销（{latest_date}），事件链完整")
    return ScopeStatus(scope, date, STATUS_UNKNOWN, False,
                       f"最近一次变更为撤销（{latest_date}），但事件链不完整（缺少对应的实施事件）：不判定为非 ST")


__all__ = [
    "EVENT_IMPLEMENT",
    "EVENT_REVOKE",
    "SCOPE_DELISTING",
    "SCOPE_OTHER",
    "SCOPE_UNKNOWN",
    "STATUS_ACTIVE",
    "STATUS_INACTIVE",
    "STATUS_UNKNOWN",
    "AnnouncementParseError",
    "RiskWarningEvent",
    "ScopeStatus",
    "StInterval",
    "build_st_intervals",
    "extract_text_from_pdf",
    "parse_risk_warning_event",
    "query_risk_warning_status",
]
