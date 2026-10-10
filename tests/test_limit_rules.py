"""Stage 027′：涨跌停规则测试（market.rules.limits）。

只覆盖**关键边界**，不重复已有测试：
    - 板块比例：主板 / 创业板 / 科创板 / 北交所 / ST / 科创板无 ST 制度 / 未知板块
    - 价格与四舍五入（含 half-up 边界、与实测涨停案例交叉验证）
    - 新股无涨跌幅限制阶段（不得当作 ±10% 或 0）
    - 除权除息日 / 前复权价不得当涨跌停基准
    - 前收盘缺失、非法、跨缺失交易日的处置（一律 unknown，不推断停牌/未涨停）
    - K 线与涨跌停价的事实性对照（封住 / 触及未封 / 跌停 / 区间内 / 缺值）
    - 临时数据库集成：真实表结构下跑一遍，并证明**未触碰正式数据库**
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import KlineRepository, SymbolRepository, initialize_database  # noqa: E402
from market.rules import (  # noqa: E402
    BSE,
    BOARD_LIMIT_RATIOS,
    LISTING_STAGE_MAIN_FIRST_DAY,
    LISTING_STAGE_NO_LIMIT,
    LIMIT_STAGES,
    LISTING_STAGE_TRADING,
    SSE_MAIN,
    SSE_STAR,
    STATUS_NO_LIMIT,
    STATUS_OK,
    STATUS_UNKNOWN,
    SZSE_GEM,
    SZSE_MAIN,
    classify_bar_against_limits,
    EVENT_REGULAR,
    SOURCE_AGREE,
    ExRightsFields,
    compute_limit_prices,
    resolve_limit_ratio,
    resolve_listing_stage,
    resolve_prev_close,
    round_to_tick,
    usable_reference_prev_close,
    verify_ex_rights_reference,
)

FORMAL_DB = PROJECT_ROOT / "data" / "strategy_engine.db"


class BoardCodeConsistencyTest(unittest.TestCase):
    """规则层的板块代码必须与数据层（Stage 24 交易板块）完全一致。"""

    def test_board_codes_match_data_layer(self) -> None:
        from market.data.boards import BOARD_CODES

        self.assertEqual(set(BOARD_CODES), {SSE_MAIN, SZSE_MAIN, SZSE_GEM, SSE_STAR, BSE})
        self.assertEqual(set(BOARD_CODES), set(BOARD_LIMIT_RATIOS))


class LimitRatioTest(unittest.TestCase):
    def test_board_ratios(self) -> None:
        for board, expected in ((SSE_MAIN, "0.10"), (SZSE_MAIN, "0.10"), (SZSE_GEM, "0.20"),
                                (SSE_STAR, "0.20"), (BSE, "0.30")):
            ratio, basis = resolve_limit_ratio(board)
            self.assertEqual(ratio, Decimal(expected), board)
            self.assertIn(board, basis)

    def test_st_ratios(self) -> None:
        self.assertEqual(resolve_limit_ratio(SSE_MAIN, is_st=True, as_of="2026-01-01")[0], Decimal("0.05"))
        self.assertEqual(resolve_limit_ratio(SZSE_MAIN, is_st=True, as_of="2026-01-01")[0], Decimal("0.05"))
        # 创业板 ST 仍是 20%（制度）
        self.assertEqual(resolve_limit_ratio(SZSE_GEM, is_st=True)[0], Decimal("0.20"))

    def test_star_market_st_keeps_20_percent(self) -> None:
        # 制度 + 实证：科创板 *ST 涨跌幅仍为 20%（实测 13 只科创板 ST 最大日内涨幅 20.45%）
        ratio, basis = resolve_limit_ratio(SSE_STAR, is_st=True)
        self.assertEqual(ratio, Decimal("0.20"))
        self.assertEqual(compute_limit_prices("10.00", board=SSE_STAR, is_st=True, prev_close_is_exchange_reference=True).upper, Decimal("12.00"))

    def test_every_board_has_normal_and_st_ratio(self) -> None:
        # 5 个板块都必须能给出常规与 ST 两套比例，不得出现“板块存在但 ST 无比例”的隐式缺口
        for board in BOARD_LIMIT_RATIOS:
            self.assertIsNotNone(resolve_limit_ratio(board)[0], board)
            self.assertIsNotNone(resolve_limit_ratio(board, is_st=True, as_of="2026-01-01")[0],
                                 f"{board} ST")

    def test_main_board_st_ratio_differs_from_star(self) -> None:
        # 主板 ST 5% vs 科创板 ST 20%，不得混用同一张表（历史上曾把科创板 ST 误判为 unknown）
        self.assertEqual(resolve_limit_ratio(SZSE_MAIN, is_st=True, as_of="2026-01-01")[0], Decimal("0.05"))
        self.assertEqual(resolve_limit_ratio(SSE_STAR, is_st=True)[0], Decimal("0.20"))

    def test_unknown_board_is_not_guessed(self) -> None:
        for board in (None, "", "NASDAQ"):
            ratio, basis = resolve_limit_ratio(board)
            self.assertIsNone(ratio)
            self.assertTrue(basis)


class LimitPriceTest(unittest.TestCase):
    def test_main_board_prices(self) -> None:
        result = compute_limit_prices("1258.62", board=SSE_MAIN, prev_close_is_exchange_reference=True)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.upper, Decimal("1384.48"))
        self.assertEqual(result.lower, Decimal("1132.76"))

    def test_rounding_is_half_up(self) -> None:
        # 10.05 × 1.1 = 11.055 → half-up 11.06（银行家舍入会得 11.06 之外的结果）
        self.assertEqual(round_to_tick(Decimal("11.055")), Decimal("11.06"))
        upper = compute_limit_prices("10.05", board=SSE_MAIN, prev_close_is_exchange_reference=True).upper
        self.assertEqual(upper, Decimal("11.06"))
        self.assertEqual(compute_limit_prices("10.05", board=SSE_MAIN, prev_close_is_exchange_reference=True).lower, Decimal("9.05"))

    def test_matches_real_observed_limit_up_examples(self) -> None:
        # 来自实测：600010 前收 1.90 → 涨停 2.09；300001 前收 24.11 → 28.93
        self.assertEqual(compute_limit_prices("1.90", board=SSE_MAIN, prev_close_is_exchange_reference=True).upper, Decimal("2.09"))
        self.assertEqual(compute_limit_prices("24.11", board=SZSE_GEM, prev_close_is_exchange_reference=True).upper, Decimal("28.93"))

    def test_st_main_board_5_percent(self) -> None:
        result = compute_limit_prices("10.00", board=SSE_MAIN, is_st=True, as_of="2026-01-01",
                                      prev_close_is_exchange_reference=True)
        self.assertEqual(result.upper, Decimal("10.50"))
        self.assertEqual(result.lower, Decimal("9.50"))

    def test_bse_30_percent(self) -> None:
        result = compute_limit_prices("20.00", board=BSE, prev_close_is_exchange_reference=True)
        self.assertEqual(result.upper, Decimal("26.00"))
        self.assertEqual(result.lower, Decimal("14.00"))

    def test_custom_tick(self) -> None:
        result = compute_limit_prices("10.005", board=SSE_MAIN, prev_close_is_exchange_reference=True,
                                      tick=Decimal("0.001"))
        self.assertEqual(result.upper, Decimal("11.006"))

    def test_new_listing_stage_has_no_limit(self) -> None:
        result = compute_limit_prices("10.00", board=SZSE_GEM, listing_stage=LISTING_STAGE_NO_LIMIT)
        self.assertEqual(result.status, STATUS_NO_LIMIT)
        self.assertIsNone(result.upper)
        self.assertIsNone(result.lower)
        self.assertIn("无涨跌幅限制", result.basis)

    def test_unknown_listing_stage_is_unknown(self) -> None:
        result = compute_limit_prices("10.00", board=SSE_MAIN, listing_stage="ipo")
        self.assertEqual(result.status, STATUS_UNKNOWN)

    def test_ex_dividend_day_must_use_exchange_reference(self) -> None:
        result = compute_limit_prices("10.00", board=SSE_MAIN, prev_close_is_exchange_reference=False)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIsNone(result.upper)
        self.assertIn("交易所参考前收盘价", result.basis)

    def test_invalid_prev_close_is_unknown(self) -> None:
        for value in (None, 0, -1, "abc", "", float("nan")):
            result = compute_limit_prices(value, board=SSE_MAIN, prev_close_is_exchange_reference=True)
            self.assertEqual(result.status, STATUS_UNKNOWN, f"value={value!r}")
            self.assertIsNone(result.upper)

    def test_unknown_board_yields_unknown_status(self) -> None:
        result = compute_limit_prices("10.00", board=None, prev_close_is_exchange_reference=True)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIsNone(result.upper)

    def test_main_board_first_day_is_asymmetric(self) -> None:
        result = compute_limit_prices("10.00", board=SSE_MAIN, listing_stage=LISTING_STAGE_MAIN_FIRST_DAY)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.upper, Decimal("14.40"))  # +44%
        self.assertEqual(result.lower, Decimal("6.40"))   # −36%
        self.assertIsNone(result.ratio)                    # 非对称：无单一比例
        self.assertEqual(result.upper_ratio, Decimal("0.44"))
        self.assertEqual(result.lower_ratio, Decimal("0.36"))
        # 深市主板同规则（发行价 4.39 → 上限 6.32）
        self.assertEqual(
            compute_limit_prices("4.39", board=SZSE_MAIN, listing_stage=LISTING_STAGE_MAIN_FIRST_DAY).upper,
            Decimal("6.32"),
        )

    def test_main_board_first_day_rejects_non_main_board(self) -> None:
        for board in (SZSE_GEM, SSE_STAR, BSE):
            result = compute_limit_prices("10.00", board=board, listing_stage=LISTING_STAGE_MAIN_FIRST_DAY)
            self.assertEqual(result.status, STATUS_UNKNOWN, board)
            self.assertIsNone(result.upper)

    def test_main_board_first_day_invalid_issue_price(self) -> None:
        for value in (None, 0, -1, "abc"):
            result = compute_limit_prices(value, board=SSE_MAIN, listing_stage=LISTING_STAGE_MAIN_FIRST_DAY)
            self.assertEqual(result.status, STATUS_UNKNOWN, f"value={value!r}")
            self.assertIsNone(result.upper)


class ListingStageTest(unittest.TestCase):
    """阶段判定：信息不足一律 None（unknown），不得凭经验补齐。"""

    def test_gem_and_star_first_five_sessions(self) -> None:
        for board in (SZSE_GEM, SSE_STAR):
            for k in range(5):
                self.assertEqual(resolve_listing_stage(board, sessions_since_listing=k)[0],
                                 LISTING_STAGE_NO_LIMIT, f"{board} k={k}")
            self.assertEqual(resolve_listing_stage(board, sessions_since_listing=5)[0], LISTING_STAGE_TRADING)

    def test_bse_first_day_only(self) -> None:
        self.assertEqual(resolve_listing_stage(BSE, sessions_since_listing=0)[0], LISTING_STAGE_NO_LIMIT)
        self.assertEqual(resolve_listing_stage(BSE, sessions_since_listing=1)[0], LISTING_STAGE_TRADING)

    def test_main_board_requires_listing_date(self) -> None:
        stage, basis = resolve_listing_stage(SSE_MAIN, sessions_since_listing=0)
        self.assertIsNone(stage)
        self.assertIn("上市日期", basis)

    def test_main_board_pre_2023_04_10_first_day_asymmetric(self) -> None:
        stage, _ = resolve_listing_stage(SSE_MAIN, sessions_since_listing=0, listing_date="2022-06-01")
        self.assertEqual(stage, LISTING_STAGE_MAIN_FIRST_DAY)
        self.assertEqual(
            resolve_listing_stage(SSE_MAIN, sessions_since_listing=1, listing_date="2022-06-01")[0],
            LISTING_STAGE_TRADING,
        )

    def test_main_board_post_2023_04_10_first_five_no_limit(self) -> None:
        for k in range(5):
            self.assertEqual(
                resolve_listing_stage(SZSE_MAIN, sessions_since_listing=k, listing_date="2025-01-15")[0],
                LISTING_STAGE_NO_LIMIT, f"k={k}",
            )
        self.assertEqual(
            resolve_listing_stage(SZSE_MAIN, sessions_since_listing=5, listing_date="2025-01-15")[0],
            LISTING_STAGE_TRADING,
        )
        # 边界：恰在 2023-04-10 上市 → 按注册制（前 5 日无限制）
        self.assertEqual(
            resolve_listing_stage(SSE_MAIN, sessions_since_listing=0, listing_date="2023-04-10")[0],
            LISTING_STAGE_NO_LIMIT,
        )

    def test_unknown_inputs_return_none(self) -> None:
        self.assertIsNone(resolve_listing_stage(None, sessions_since_listing=0)[0])
        self.assertIsNone(resolve_listing_stage("NASDAQ", sessions_since_listing=0)[0])
        self.assertIsNone(resolve_listing_stage(SSE_MAIN, sessions_since_listing=None)[0])
        self.assertIsNone(resolve_listing_stage(SSE_MAIN, sessions_since_listing=-1)[0])

    def test_empirical_consistency_2026_main_board_ipo(self) -> None:
        # 正式库实测：601091（2026-09-17 上市）首日至第 5 日无限制、第 6 日起 ±10%
        self.assertEqual(
            resolve_listing_stage(SSE_MAIN, sessions_since_listing=0, listing_date="2026-09-17")[0],
            LISTING_STAGE_NO_LIMIT,
        )


class PrevCloseTest(unittest.TestCase):
    def test_ok_from_previous_bar(self) -> None:
        result = resolve_prev_close("10.00", sessions_since_prev_bar=1)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.prev_close, Decimal("10.00"))

    def test_missing_bar_is_not_interpreted_as_suspension(self) -> None:
        result = resolve_prev_close(None, bar_present=False)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIn("不推断为停牌", result.reason)
        self.assertIn("未涨停", result.reason)

    def test_session_gap_requires_calendar_confirmation(self) -> None:
        result = resolve_prev_close("10.00", sessions_since_prev_bar=3)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIn("缺 2 个交易日", result.reason)

    def test_corporate_action_requires_exchange_reference(self) -> None:
        result = resolve_prev_close("10.00", corporate_action=True)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIn("除权除息", result.reason)

    def test_invalid_previous_close(self) -> None:
        for value in (None, "0", "abc"):
            self.assertEqual(resolve_prev_close(value).status, STATUS_UNKNOWN, f"value={value!r}")


class BarAgainstLimitsTest(unittest.TestCase):
    def test_states(self) -> None:
        upper, lower = Decimal("11.00"), Decimal("9.00")
        self.assertEqual(classify_bar_against_limits(high="11.00", low="10.50", close="11.00",
                                                     upper=upper, lower=lower), "at_upper")
        self.assertEqual(classify_bar_against_limits(high="11.00", low="10.20", close="10.80",
                                                     upper=upper, lower=lower), "touched_upper")
        self.assertEqual(classify_bar_against_limits(high="9.90", low="9.00", close="9.00",
                                                     upper=upper, lower=lower), "at_lower")
        self.assertEqual(classify_bar_against_limits(high="10.90", low="10.10", close="10.50",
                                                     upper=upper, lower=lower), "inside")

    def test_unknown_when_values_or_bounds_missing(self) -> None:
        self.assertEqual(classify_bar_against_limits(high=None, low="9.9", close="10",
                                                     upper=Decimal("11"), lower=Decimal("9")), STATUS_UNKNOWN)
        self.assertEqual(classify_bar_against_limits(high="10.5", low="10.1", close="10.2",
                                                     upper=None, lower=None), STATUS_UNKNOWN)


class TempDatabaseIntegrationTest(unittest.TestCase):
    """在临时数据库（真实表结构）上跑一遍，并证明不触碰正式数据库。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "foundation.db"
        initialize_database(self.db_path)
        self.symbols = SymbolRepository(self.db_path)
        self.klines = KlineRepository(self.db_path)
        self.formal_before = (FORMAL_DB.stat().st_size, FORMAL_DB.stat().st_mtime) if FORMAL_DB.exists() else None

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed(self, code: str, bars: list[tuple[str, float, float, float, float]]) -> int:
        symbol_id = self.symbols.create_symbol(code, f"测试{code}", "CN", "STOCK")
        rows = [
            {"symbol_id": symbol_id, "timeframe": "1d", "timestamp": f"{day} 00:00:00",
             "open": open_, "high": high, "low": low, "close": close, "volume": 1_000_000, "amount": None}
            for day, open_, high, low, close in bars
        ]
        self.klines.bulk_upsert_klines(rows)
        return symbol_id

    def test_limit_up_and_failed_limit_up_from_real_tables(self) -> None:
        # 600000：09-28 收 10.00 → 09-29 涨停价 11.00，收盘封住
        self._seed("600000", [("2026-09-28", 10.0, 10.1, 9.9, 10.0), ("2026-09-29", 10.5, 11.0, 10.4, 11.0)])
        # 601398：09-29 盘中触及 11.00 但收盘 10.80（炸板）
        self._seed("601398", [("2026-09-28", 10.0, 10.1, 9.9, 10.0), ("2026-09-29", 10.5, 11.0, 10.4, 10.8)])

        for code, expected in (("600000", "at_upper"), ("601398", "touched_upper")):
            symbol_id = self.symbols.get_symbol("CN", code)["id"]
            bars = self.klines.list_klines(symbol_id, "1d")
            prev = resolve_prev_close(bars[-2]["close"], sessions_since_prev_bar=1)
            limit = compute_limit_prices(prev.prev_close, board=SSE_MAIN, prev_close_is_exchange_reference=True)
            self.assertEqual(limit.status, STATUS_OK)
            state = classify_bar_against_limits(
                high=bars[-1]["high"], low=bars[-1]["low"], close=bars[-1]["close"],
                upper=limit.upper, lower=limit.lower,
            )
            self.assertEqual(state, expected, code)

    def test_missing_bar_yields_unknown_not_a_guess(self) -> None:
        self._seed("000001", [("2026-09-28", 10.0, 10.1, 9.9, 10.0)])
        # 假设 09-29 该标的没有 K 线：只能得到 unknown
        result = resolve_prev_close(None, bar_present=False, sessions_since_prev_bar=None)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        limit = compute_limit_prices(result.prev_close, board=SZSE_MAIN, prev_close_is_exchange_reference=True)
        self.assertEqual(limit.status, STATUS_UNKNOWN)

    def test_formal_database_untouched(self) -> None:
        if self.formal_before is None:
            self.skipTest("正式库不存在")
        self._seed("600519", [("2026-09-28", 10.0, 10.1, 9.9, 10.0)])
        after = (FORMAL_DB.stat().st_size, FORMAL_DB.stat().st_mtime)
        self.assertEqual(self.formal_before, after)
        self.assertNotEqual(os.fspath(self.db_path), os.fspath(FORMAL_DB))



class FailClosedTest(unittest.TestCase):
    """D4：涨跌停计算不得默认信任参考前收（fail-closed）。"""

    def test_missing_flag_is_unknown(self) -> None:
        result = compute_limit_prices("10.00", board=SSE_MAIN)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIsNone(result.upper)
        self.assertIsNone(result.lower)

    def test_explicit_none_flag_is_unknown(self) -> None:
        result = compute_limit_prices("10.00", board=SSE_MAIN, prev_close_is_exchange_reference=None)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIsNone(result.upper)

    def test_false_flag_is_unknown(self) -> None:
        result = compute_limit_prices("10.00", board=SSE_MAIN, prev_close_is_exchange_reference=False)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIsNone(result.upper)

    def test_non_bool_flag_raises(self) -> None:
        with self.assertRaises(ValueError):
            compute_limit_prices("10.00", board=SSE_MAIN, prev_close_is_exchange_reference=1)

    def test_true_flag_computes(self) -> None:
        result = compute_limit_prices("10.00", board=SSE_MAIN, prev_close_is_exchange_reference=True)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.upper, Decimal("11.00"))

    def test_no_limit_stage_needs_no_flag(self) -> None:
        result = compute_limit_prices("10.00", board=SZSE_GEM, listing_stage=LISTING_STAGE_NO_LIMIT)
        self.assertEqual(result.status, STATUS_NO_LIMIT)


class ExRightsToLimitsBoundaryTest(unittest.TestCase):
    """D4：ex_rights 未通过门禁的结果，不得在 limits 侧变成可用涨跌停价。"""

    def test_usable_reference_can_be_used(self) -> None:
        fields = ExRightsFields(code="001225", transfer_per_10="4", event_type=EVENT_REGULAR)
        verified = verify_ex_rights_reference(prev_close="50.92", fields=fields,
                                              ex_bar_high="37.8", ex_bar_low="36.37",
                                              limit_ratio=Decimal("0.10"), source_agreement=SOURCE_AGREE)
        ref = usable_reference_prev_close(verified)
        self.assertIsNotNone(ref)
        result = compute_limit_prices(ref, board=SZSE_MAIN, prev_close_is_exchange_reference=True)
        self.assertEqual(result.status, STATUS_OK)

    def test_conflict_reference_stays_unknown(self) -> None:
        fields = ExRightsFields(code="000908", transfer_per_10="10", event_type=EVENT_REGULAR)
        conflict = verify_ex_rights_reference(prev_close="6.37", fields=fields,
                                              ex_bar_high="4.58", ex_bar_low="4.58",
                                              limit_ratio=Decimal("0.10"), source_agreement=SOURCE_AGREE)
        self.assertIsNone(usable_reference_prev_close(conflict))
        result = compute_limit_prices(None, board=SZSE_MAIN, prev_close_is_exchange_reference=True)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIsNone(result.upper)



class RuleBasisAuditTest(unittest.TestCase):
    """D6：把规则映射审计结论固化为回归（不改变任何规则行为）。

    规则依据（**历史证据**：2021-05-15 版《深圳证券交易所交易规则》；现行版本条款原文未取得）：
      3.3.15 股票涨跌幅限制比例 10%；
      3.3.16 涨跌幅限制价格 = 前收盘价×(1±比例)，四舍五入至价格最小变动单位；
      4.5.5 主板风险警示股票 5%、退市整理股票 10%、创业板风险警示/退市整理股票 20%；
      4.5.6 退市整理期首个交易日不实行价格涨跌幅限制。
    """

    def test_st_ratio_mapping_matches_documented_clause(self) -> None:
        # 主板风险警示（2026-07-06 之前）5%、创业板风险警示 20%（与条款逐项对齐）
        self.assertEqual(resolve_limit_ratio(SSE_MAIN, is_st=True, as_of="2026-01-01")[0], Decimal("0.05"))
        self.assertEqual(resolve_limit_ratio(SZSE_MAIN, is_st=True, as_of="2026-01-01")[0], Decimal("0.05"))
        self.assertEqual(resolve_limit_ratio(SZSE_GEM, is_st=True)[0], Decimal("0.20"))

    def test_resume_never_auto_gains_no_limit(self) -> None:
        # 未确认的复牌情形不得自动获得“无涨跌幅限制”：只有显式 stage 才返回 no_limit
        normal = compute_limit_prices("10.00", board=SSE_MAIN, prev_close_is_exchange_reference=True)
        self.assertEqual(normal.status, STATUS_OK)
        explicit = compute_limit_prices("10.00", board=SSE_MAIN, listing_stage=LISTING_STAGE_NO_LIMIT)
        self.assertEqual(explicit.status, STATUS_NO_LIMIT)

    def test_delisting_consolidation_is_not_expressible(self) -> None:
        # 接口缺口：现有输入无法表达“退市整理期股票”（主板 10% / 首日不设限）→ 不得猜测
        for bogus in ("delisting_consolidation", "退市整理期"):
            result = compute_limit_prices("10.00", board=SSE_MAIN, listing_stage=bogus,
                                          prev_close_is_exchange_reference=True)
            self.assertEqual(result.status, STATUS_UNKNOWN, bogus)
            self.assertIsNone(result.upper)
        self.assertEqual(LIMIT_STAGES, (LISTING_STAGE_TRADING, LISTING_STAGE_NO_LIMIT,
                                        LISTING_STAGE_MAIN_FIRST_DAY))


class MainBoardStRatioVersionTest(unittest.TestCase):
    """D7：主板风险警示比例自 2026-07-06 起由 5% 调整为 10%（上交所现行原文）；必须给日期。"""

    def test_boundary_around_2026_07_06_sse(self) -> None:
        self.assertEqual(resolve_limit_ratio(SSE_MAIN, is_st=True, as_of="2026-07-05")[0], Decimal("0.05"))
        self.assertEqual(resolve_limit_ratio(SSE_MAIN, is_st=True, as_of="2026-07-06")[0], Decimal("0.10"))
        self.assertEqual(resolve_limit_ratio(SSE_MAIN, is_st=True, as_of="2026-09-30")[0], Decimal("0.10"))

    def test_no_date_is_fail_closed_for_main_board_st(self) -> None:
        for board in (SSE_MAIN, SZSE_MAIN):
            ratio, basis = resolve_limit_ratio(board, is_st=True)
            self.assertIsNone(ratio, board)
            self.assertTrue(basis)
            result = compute_limit_prices("10.00", board=board, is_st=True,
                                          prev_close_is_exchange_reference=True)
            self.assertEqual(result.status, STATUS_UNKNOWN, board)
            self.assertIsNone(result.upper)

    def test_szse_after_change_is_10_percent(self) -> None:
        # 深交所《交易规则（2026年修订）》（深证上〔2026〕551号，自 2026-07-06 施行）：主板风险警示 5% → 10%
        self.assertEqual(resolve_limit_ratio(SZSE_MAIN, is_st=True, as_of="2026-07-06")[0], Decimal("0.10"))
        self.assertEqual(resolve_limit_ratio(SZSE_MAIN, is_st=True, as_of="2026-07-05")[0], Decimal("0.05"))
        self.assertEqual(resolve_limit_ratio(SZSE_MAIN, is_st=True, as_of="2026-09-30")[0], Decimal("0.10"))
        # 创业板风险警示现行仍为 20%（现行 3.3.13 创业板一般比例）
        self.assertEqual(resolve_limit_ratio(SZSE_GEM, is_st=True, as_of="2026-09-30")[0], Decimal("0.20"))

    def test_prices_after_change_sse(self) -> None:
        result = compute_limit_prices("10.00", board=SSE_MAIN, is_st=True, as_of="2026-07-06",
                                      prev_close_is_exchange_reference=True)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.upper, Decimal("11.00"))
        self.assertEqual(result.lower, Decimal("9.00"))

    def test_non_main_board_st_unaffected_by_as_of(self) -> None:
        self.assertEqual(resolve_limit_ratio(SZSE_GEM, is_st=True)[0], Decimal("0.20"))
        self.assertEqual(resolve_limit_ratio(SSE_STAR, is_st=True, as_of="2026-09-30")[0], Decimal("0.20"))


if __name__ == "__main__":
    unittest.main()
