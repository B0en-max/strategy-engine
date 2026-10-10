"""Stage 027′-C1：历史风险警示事件解析测试（market.rules.st_history）。

夹具为**筛选后的**公告正文（tests/fixtures/st_notices/*.txt），来源：巨潮资讯网，
经公告 PDF 文本提取得到；测试只依赖文本，不联网、不连数据库。
"""

from __future__ import annotations

import sys
import unittest
import zlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from market.rules.st_history import (  # noqa: E402
    EVENT_IMPLEMENT,
    EVENT_REVOKE,
    SCOPE_DELISTING,
    SCOPE_OTHER,
    STATUS_ACTIVE,
    STATUS_INACTIVE,
    STATUS_UNKNOWN,
    AnnouncementParseError,
    RiskWarningEvent,
    build_st_intervals,
    extract_text_from_pdf,
    parse_risk_warning_event,
    query_risk_warning_status,
)

FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "st_notices"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_text_pdf(text: str) -> bytes:
    """构造一个最小"文本型 PDF"（独立 ToUnicode CMap 流 + 内容流），用于测试提取路径。"""
    chars = list(dict.fromkeys(text))
    ids = {ch: f"{i + 1:04X}" for i, ch in enumerate(chars)}
    bf = "".join(f"<{ids[ch]}> <{ch.encode('utf-16-be').hex().upper()}>" for ch in chars)
    cmap_stream = f"beginbfchar {bf} endbfchar".encode()
    content = ("<" + "".join(ids[ch] for ch in text) + "> Tj").encode()
    return (
        b"%PDF-1.4\n"
        b"stream\n" + zlib.compress(cmap_stream) + b"\nendstream\n"
        b"stream\n" + zlib.compress(b"BT " + content + b" ET") + b"\nendstream\n%%EOF"
    )


class ParseFixtureTest(unittest.TestCase):
    def test_600053_implement_delisting(self) -> None:
        ev = parse_risk_warning_event(
            title="关于公司股票被实施退市风险警示暨停牌的公告",
            text=load_fixture("600053_implement_delisting.txt"),
        )
        assert ev is not None
        self.assertEqual(ev.kind, EVENT_IMPLEMENT)
        self.assertEqual(ev.scope, SCOPE_DELISTING)
        self.assertEqual(ev.effective_date, "2026-04-30")
        self.assertEqual(ev.suspension_date, "2026-04-29")
        self.assertEqual(ev.code, "600053")

    def test_600238_revoke_delisting_and_continue_other(self) -> None:
        ev = parse_risk_warning_event(
            title="关于撤销退市风险警示及继续被实施其他风险警示暨停牌的公告",
            text=load_fixture("600238_revoke_delisting_and_other.txt"),
        )
        assert ev is not None
        self.assertEqual(ev.kind, EVENT_REVOKE)
        self.assertEqual(ev.scope, SCOPE_DELISTING)  # 撤销对象是"退市风险警示"
        self.assertEqual(ev.effective_date, "2026-08-03")
        self.assertEqual(ev.suspension_date, "2026-07-31")
        self.assertEqual(ev.resume_date, "2026-08-03")

    def test_000820_revoke_does_not_take_historical_implement_date(self) -> None:
        # 正文含"自2025年4月30日起被实施退市风险警示"（历史实施日）→ 不得当作撤销生效日
        ev = parse_risk_warning_event(
            title="关于公司股票交易撤销退市风险警示暨停复牌的公告",
            text=load_fixture("000820_revoke_delisting.txt"),
        )
        assert ev is not None
        self.assertEqual(ev.effective_date, "2026-07-30")
        self.assertNotEqual(ev.effective_date, "2025-04-30")
        self.assertEqual(ev.suspension_date, "2026-07-29")

    def test_300211_revoke_delisting(self) -> None:
        ev = parse_risk_warning_event(
            title="关于公司股票交易撤销退市风险警示暨停牌的公告",
            text=load_fixture("300211_revoke_delisting.txt"),
        )
        assert ev is not None
        self.assertEqual(ev.kind, EVENT_REVOKE)
        self.assertEqual(ev.effective_date, "2026-09-29")
        self.assertEqual(ev.suspension_date, "2026-09-28")

    def test_300527_revoke_other_risk(self) -> None:
        ev = parse_risk_warning_event(
            title="关于撤销其他风险警示暨股票停复牌的公告",
            text=load_fixture("300527_revoke_other.txt"),
        )
        assert ev is not None
        self.assertEqual(ev.kind, EVENT_REVOKE)
        self.assertEqual(ev.scope, SCOPE_OTHER)
        self.assertEqual(ev.effective_date, "2026-09-30")
        self.assertEqual(ev.suspension_date, "2026-09-29")


    def test_688033_implement_delisting_star(self) -> None:
        # 科创板（688）实施样本
        ev = parse_risk_warning_event(
            title="关于公司股票被实施退市风险警示及叠加其他风险警示暨停牌的公告",
            text=load_fixture("688033_implement_delisting.txt"),
        )
        assert ev is not None
        self.assertEqual(ev.kind, EVENT_IMPLEMENT)
        self.assertEqual(ev.scope, SCOPE_DELISTING)
        self.assertEqual(ev.effective_date, "2026-05-06")
        self.assertEqual(ev.suspension_date, "2026-04-30")
        self.assertEqual(ev.resume_date, "2026-05-06")  # 跨五一假期，复牌日≠停牌日
        self.assertEqual(ev.code, "688033")

    def test_title_with_html_highlight_tags(self) -> None:
        # 巨潮检索结果标题带 <em> 高亮标签 → 必须先剥离，否则会静默漏判
        ev = parse_risk_warning_event(
            title="关于公司股票被<em>实施</em><em>退市</em><em>风险</em><em>警示</em>暨停牌的公告",
            text="实施起始日为2026年4月30日。",
        )
        assert ev is not None
        self.assertEqual(ev.kind, EVENT_IMPLEMENT)
        self.assertEqual(ev.scope, SCOPE_DELISTING)
        self.assertEqual(ev.effective_date, "2026-04-30")
        self.assertNotIn("<em>", ev.title)


class NotAnEventTest(unittest.TestCase):
    def test_application_announcement_is_not_an_event(self) -> None:
        ev = parse_risk_warning_event(
            title="关于向深圳证券交易所申请撤销退市风险警示的公告",
            text="公司向交易所提交了撤销退市风险警示的申请。截止本公告日，尚未实施。",
        )
        self.assertIsNone(ev)

    def test_prospective_implement_notice_is_not_an_event(self) -> None:
        for title in (
            "关于定期报告披露进展暨可能被实施退市风险警示的风险提示公告",
            "关于公司股票拟被实施其他风险警示的提示性公告",
        ):
            self.assertIsNone(parse_risk_warning_event(title=title, text="……"), title)

    def test_unrelated_title_returns_none(self) -> None:
        self.assertIsNone(parse_risk_warning_event(title="关于股票交易异常波动的公告", text="无"))
        self.assertIsNone(parse_risk_warning_event(title="2025年年度报告", text="……"))

    def test_missing_effective_date_is_explicit_none(self) -> None:
        # 是撤销公告，但正文没有明确起始日 → 生效日为 None（显式 unknown），不得填充
        ev = parse_risk_warning_event(
            title="关于公司股票撤销退市风险警示暨停牌的公告",
            text="公司股票将停牌一天后复牌，简称变更，具体日期以公告为准。",
        )
        assert ev is not None
        self.assertEqual(ev.kind, EVENT_REVOKE)
        self.assertIsNone(ev.effective_date)
        self.assertIn("未找到", ev.basis)

    def test_format_variants(self) -> None:
        variants = [
            "撤销退市风险警示的起始日：2026年8月3日。",
            "撤销退市风险警示起始日为2026年8月3日。",
            "撤销退市风险警示的起始日期：自2026年8月3日开市起撤销。",
            "公司股票将于2026年8月3日起复牌并撤销退市风险警示。",
        ]
        for text in variants:
            ev = parse_risk_warning_event(title="关于撤销退市风险警示的公告", text=text)
            assert ev is not None
            self.assertEqual(ev.effective_date, "2026-08-03", text)

    def test_invalid_date_is_not_returned(self) -> None:
        ev = parse_risk_warning_event(
            title="关于撤销退市风险警示的公告",
            text="撤销退市风险警示的起始日：2026年13月40日。",
        )
        assert ev is not None
        self.assertIsNone(ev.effective_date)  # 非法日期 → None，不返回假日期


class PdfExtractTest(unittest.TestCase):
    def test_round_trip_synthetic_text_pdf(self) -> None:
        text = "撤销退市风险警示起始日：2026年8月3日"
        self.assertEqual(extract_text_from_pdf(make_text_pdf(text)), text)

    def test_non_pdf_raises(self) -> None:
        for raw in (b"", b"<html>not pdf</html>", b"%PD"):
            with self.assertRaises(AnnouncementParseError):
                extract_text_from_pdf(raw)

    def test_pdf_without_tounicode_mapping_raises(self) -> None:
        raw = b"%PDF-1.4\nstream\n" + zlib.compress(b"BT <0041> Tj ET") + b"\nendstream\n%%EOF"
        with self.assertRaises(AnnouncementParseError):
            extract_text_from_pdf(raw)

    def test_pdf_without_cjk_raises(self) -> None:
        with self.assertRaises(AnnouncementParseError):
            extract_text_from_pdf(make_text_pdf("ABC123"))


class BuildIntervalsTest(unittest.TestCase):
    def _ev(self, kind: str, date: str, scope: str = SCOPE_DELISTING) -> RiskWarningEvent:
        return RiskWarningEvent(kind=kind, scope=scope, effective_date=date, suspension_date=None,
                                resume_date=None, title="t", basis="b")

    def test_complete_pair(self) -> None:
        intervals = build_st_intervals([
            self._ev(EVENT_IMPLEMENT, "2025-04-30"),
            self._ev(EVENT_REVOKE, "2026-07-30"),
        ])
        self.assertEqual(len(intervals), 1)
        self.assertTrue(intervals[0].complete)
        self.assertEqual(intervals[0].start_date, "2025-04-30")
        self.assertEqual(intervals[0].end_date, "2026-07-30")

    def test_revoke_without_implement_is_incomplete(self) -> None:
        intervals = build_st_intervals([self._ev(EVENT_REVOKE, "2026-07-30")])
        self.assertEqual(len(intervals), 1)
        self.assertFalse(intervals[0].complete)
        self.assertIsNone(intervals[0].start_date)
        self.assertEqual(intervals[0].end_date, "2026-07-30")

    def test_open_interval_without_revoke(self) -> None:
        intervals = build_st_intervals([self._ev(EVENT_IMPLEMENT, "2025-04-30")])
        self.assertEqual(len(intervals), 1)
        self.assertFalse(intervals[0].complete)
        self.assertIsNone(intervals[0].end_date)

    def test_scope_is_separated(self) -> None:
        intervals = build_st_intervals([
            self._ev(EVENT_IMPLEMENT, "2025-04-30", SCOPE_DELISTING),
            self._ev(EVENT_IMPLEMENT, "2025-05-06", SCOPE_OTHER),
        ])
        self.assertEqual(len(intervals), 2)
        self.assertEqual({i.scope for i in intervals}, {SCOPE_DELISTING, SCOPE_OTHER})

    def test_events_without_date_are_ignored(self) -> None:
        ev = RiskWarningEvent(kind=EVENT_IMPLEMENT, scope=SCOPE_DELISTING, effective_date=None,
                              suspension_date=None, resume_date=None, title="t", basis="b")
        self.assertEqual(build_st_intervals([ev]), ())


class ContinuesParseTest(unittest.TestCase):
    def test_600238_captures_continues_other(self) -> None:
        # "撤销退市风险警示并继续被实施其他风险警示" → 必须同时表达"其他链继续生效"
        ev = parse_risk_warning_event(
            title="关于撤销退市风险警示及继续被实施其他风险警示暨停牌的公告",
            text=load_fixture("600238_revoke_delisting_and_other.txt"),
        )
        assert ev is not None
        self.assertEqual(ev.kind, EVENT_REVOKE)
        self.assertEqual(ev.scope, SCOPE_DELISTING)
        self.assertEqual(ev.continues, (SCOPE_OTHER,))

    def test_plain_revoke_has_no_continues(self) -> None:
        ev = parse_risk_warning_event(
            title="关于公司股票交易撤销退市风险警示暨停牌的公告",
            text=load_fixture("300211_revoke_delisting.txt"),
        )
        assert ev is not None
        self.assertEqual(ev.continues, ())


def _ev(kind: str, scope: str, date: str, continues: tuple[str, ...] = ()) -> RiskWarningEvent:
    return RiskWarningEvent(kind=kind, scope=scope, effective_date=date, suspension_date=None,
                            resume_date=None, title="t", basis="b", continues=tuple(continues))


def _status(events, date: str) -> dict:
    return {s.scope: s for s in query_risk_warning_status(events, date=date)}


class StateQueryTest(unittest.TestCase):
    def test_both_scopes_always_returned(self) -> None:
        out = query_risk_warning_status([], date="2026-01-01")
        self.assertEqual({s.scope for s in out}, {SCOPE_DELISTING, SCOPE_OTHER})

    def test_revoke_delisting_and_continue_other(self) -> None:
        events = [
            _ev(EVENT_IMPLEMENT, SCOPE_DELISTING, "2025-04-30"),
            _ev(EVENT_REVOKE, SCOPE_DELISTING, "2026-08-03", continues=(SCOPE_OTHER,)),
        ]
        s = _status(events, "2026-07-30")
        self.assertEqual(s[SCOPE_DELISTING].status, STATUS_ACTIVE)
        self.assertTrue(s[SCOPE_DELISTING].complete)
        s = _status(events, "2026-08-03")
        self.assertEqual(s[SCOPE_DELISTING].status, STATUS_INACTIVE)
        self.assertTrue(s[SCOPE_DELISTING].complete)
        # 撤销退市之日"其他"仍在实施 → ACTIVE；绝不能因撤销退市而判 INACTIVE
        s = _status(events, "2026-08-04")
        self.assertEqual(s[SCOPE_OTHER].status, STATUS_ACTIVE)
        self.assertFalse(s[SCOPE_OTHER].complete)  # 起点未知
        # 继续声明之前：未知（不是非 ST）
        self.assertEqual(_status(events, "2026-07-01")[SCOPE_OTHER].status, STATUS_UNKNOWN)

    def test_implement_and_revoke_other_scope(self) -> None:
        events = [_ev(EVENT_IMPLEMENT, SCOPE_OTHER, "2025-05-06"),
                  _ev(EVENT_REVOKE, SCOPE_OTHER, "2026-09-30")]
        self.assertEqual(_status(events, "2026-09-29")[SCOPE_OTHER].status, STATUS_ACTIVE)
        s = _status(events, "2026-09-30")[SCOPE_OTHER]
        self.assertEqual(s.status, STATUS_INACTIVE)
        self.assertTrue(s.complete)
        # 另一类（退市）无记录 → 未知，不得默认非 ST
        self.assertEqual(_status(events, "2026-09-30")[SCOPE_DELISTING].status, STATUS_UNKNOWN)

    def test_boundaries_of_confirmed_chain(self) -> None:
        events = [_ev(EVENT_IMPLEMENT, SCOPE_DELISTING, "2025-04-30"),
                  _ev(EVENT_REVOKE, SCOPE_DELISTING, "2026-08-03")]
        self.assertEqual(_status(events, "2025-04-29")[SCOPE_DELISTING].status, STATUS_UNKNOWN)
        self.assertEqual(_status(events, "2025-04-30")[SCOPE_DELISTING].status, STATUS_ACTIVE)
        self.assertEqual(_status(events, "2026-08-02")[SCOPE_DELISTING].status, STATUS_ACTIVE)
        self.assertEqual(_status(events, "2026-08-03")[SCOPE_DELISTING].status, STATUS_INACTIVE)

    def test_missing_implement_is_unknown_not_inactive(self) -> None:
        events = [_ev(EVENT_REVOKE, SCOPE_DELISTING, "2026-08-03")]
        s = _status(events, "2026-08-04")[SCOPE_DELISTING]
        self.assertEqual(s.status, STATUS_UNKNOWN)
        self.assertFalse(s.complete)

    def test_missing_revoke_is_active(self) -> None:
        events = [_ev(EVENT_IMPLEMENT, SCOPE_DELISTING, "2025-04-30")]
        self.assertEqual(_status(events, "2026-01-01")[SCOPE_DELISTING].status, STATUS_ACTIVE)

    def test_same_day_conflict_is_unknown(self) -> None:
        events = [_ev(EVENT_IMPLEMENT, SCOPE_DELISTING, "2026-08-03"),
                  _ev(EVENT_REVOKE, SCOPE_DELISTING, "2026-08-03")]
        self.assertEqual(_status(events, "2026-08-03")[SCOPE_DELISTING].status, STATUS_UNKNOWN)

    def test_duplicate_announcement_deduped(self) -> None:
        events = [_ev(EVENT_IMPLEMENT, SCOPE_DELISTING, "2025-04-30"),
                  _ev(EVENT_IMPLEMENT, SCOPE_DELISTING, "2025-04-30"),
                  _ev(EVENT_REVOKE, SCOPE_DELISTING, "2026-08-03")]
        s = _status(events, "2025-05-01")[SCOPE_DELISTING]
        self.assertEqual(s.status, STATUS_ACTIVE)
        self.assertTrue(s.complete)

    def test_order_independent(self) -> None:
        a = [_ev(EVENT_IMPLEMENT, SCOPE_DELISTING, "2025-04-30"),
             _ev(EVENT_REVOKE, SCOPE_DELISTING, "2026-08-03")]
        b = list(reversed(a))
        for d in ("2025-04-30", "2026-08-02", "2026-08-03"):
            self.assertEqual(_status(a, d), _status(b, d), d)
        self.assertEqual(build_st_intervals(a), build_st_intervals(b))


class BuildIntervalsEdgeTest(unittest.TestCase):
    def test_duplicate_implement_no_spurious_interval(self) -> None:
        events = [_ev(EVENT_IMPLEMENT, SCOPE_DELISTING, "2025-04-30"),
                  _ev(EVENT_IMPLEMENT, SCOPE_DELISTING, "2025-04-30"),
                  _ev(EVENT_REVOKE, SCOPE_DELISTING, "2026-08-03")]
        intervals = build_st_intervals(events)
        self.assertEqual(len(intervals), 1)
        self.assertTrue(intervals[0].complete)
        self.assertEqual((intervals[0].start_date, intervals[0].end_date), ("2025-04-30", "2026-08-03"))

    def test_same_day_conflict_flagged_not_paired(self) -> None:
        events = [_ev(EVENT_IMPLEMENT, SCOPE_DELISTING, "2026-08-03"),
                  _ev(EVENT_REVOKE, SCOPE_DELISTING, "2026-08-03")]
        intervals = build_st_intervals(events)
        self.assertEqual(len(intervals), 1)
        self.assertFalse(intervals[0].complete)
        self.assertIsNone(intervals[0].start_date)
        self.assertIsNone(intervals[0].end_date)

    def test_two_scopes_are_independent(self) -> None:
        events = [_ev(EVENT_IMPLEMENT, SCOPE_DELISTING, "2025-04-30"),
                  _ev(EVENT_REVOKE, SCOPE_DELISTING, "2026-08-03"),
                  _ev(EVENT_IMPLEMENT, SCOPE_OTHER, "2026-08-03")]
        intervals = build_st_intervals(events)
        self.assertEqual({i.scope for i in intervals}, {SCOPE_DELISTING, SCOPE_OTHER})


if __name__ == "__main__":
    unittest.main()
