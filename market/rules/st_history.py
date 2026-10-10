"""历史风险警示（ST/*ST）事件解析（Stage 027′-C1）。

目标
------------------------------------------------------------------
把**巨潮资讯网公告**（交易所指定信息披露平台）还原成结构化的风险警示事件，
为后续"历史涨跌停判定"提供可信的 ST 生效区间。

本模块只做**解析**（纯函数：文本/字节进，结构出）：不联网、不连数据库、不改 schema。
公告的检索与下载由调用方完成（见 README/verify 中的查询流程）。

铁律（与项目"不猜数据"约定一致）
------------------------------------------------------------------
    - **公告日期 ≠ 停牌日期 ≠ 实施/撤销生效日期**，四者可相差数日，绝不互相替代；
    - 生效日期**只取自公告正文**的明确表述；正文无法确认时字段为 None（unknown），
      **绝不返回"看似有效"的默认日期**；
    - **不得**用 K 线涨跌幅反推 ST 日期；也**不得**把当前证券简称当作历史 ST 状态；
    - 解析失败（无正文 / 结构无法识别）显式返回 None 或抛 AnnouncementParseError。

已验证样本（正文均含结构化"起始日"字段；来源：巨潮资讯网）
------------------------------------------------------------------
    600053 沪市主板 实施退市风险警示  停牌 2026-04-29 / 实施 2026-04-30
    600238 沪市主板 撤销退市风险警示  停牌 2026-07-31 / 撤销 2026-08-03（并继续其他）
    000820 深市主板 撤销退市风险警示  停牌 2026-07-29 / 撤销 2026-07-30
    300211 创业板   撤销退市风险警示  停牌 2026-09-28 / 撤销 2026-09-29
    300527 创业板   撤销其他风险警示  停牌 2026-09-29 / 撤销 2026-09-30

已知局限
------------------------------------------------------------------
    - 只处理**文本型 PDF**（含 ToUnicode 映射）；扫描/图片型 PDF 一律抛错，不猜；
    - 公告措辞存在版本差异，规则基于已验证样本，覆盖不到时返回 None；
    - 事件**完整性**无法保证（历史/更早公告可能未入库或不可解析）→ 区间构造会显式标注不完整。
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
    )


# --------------------------------------------------------------------------- 区间
@dataclass(frozen=True)
class StInterval:
    """一段风险警示区间（实施 → 撤销）。

    end_date 为 None 表示"截至已知事件仍未撤销"；complete=False 表示事件链不完整（不猜）。
    """

    scope: str
    start_date: str | None
    end_date: str | None
    complete: bool
    basis: str


def build_st_intervals(events: Iterable[RiskWarningEvent]) -> tuple[StInterval, ...]:
    """把同一范围（退市/其他）的实施-撤销事件配成区间。

    只在事件链完整（实施→撤销 交替、且以实施开头）时给 complete=True。
    出现"只有撤销没有实施"、"连续两次实施"等不完整情形时，仍产出区间并标 complete=False，
    **不推测缺失日期**。
    """
    by_scope: dict[str, list[RiskWarningEvent]] = {}
    for ev in events:
        if ev.effective_date is None:
            continue
        by_scope.setdefault(ev.scope, []).append(ev)

    intervals: list[StInterval] = []
    for scope, evs in by_scope.items():
        evs = sorted(evs, key=lambda e: e.effective_date or "")
        open_start: str | None = None
        open_note: str | None = None
        for ev in evs:
            if ev.kind == EVENT_IMPLEMENT:
                if open_start is not None:
                    intervals.append(StInterval(scope, open_start, None, False,
                                                f"两次实施之间缺少撤销公告（{open_start} → {ev.effective_date}）"))
                open_start = ev.effective_date
                open_note = None
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


__all__ = [
    "EVENT_IMPLEMENT",
    "EVENT_REVOKE",
    "SCOPE_DELISTING",
    "SCOPE_OTHER",
    "SCOPE_UNKNOWN",
    "AnnouncementParseError",
    "RiskWarningEvent",
    "StInterval",
    "build_st_intervals",
    "extract_text_from_pdf",
    "parse_risk_warning_event",
]
