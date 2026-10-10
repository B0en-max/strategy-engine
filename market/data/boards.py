"""交易板块 / 市场层级分类（Stage 024）。

**本阶段只做「交易板块」**（交易所层级），不是行业板块、也不是概念板块 —— 那两类属于后续独立阶段。

五个交易板块（每只 A 股最多归属其中一个）：

    SSE_MAIN   沪市主板      600 / 601 / 603 / 605
    SZSE_MAIN  深市主板      000 / 001 / 002 / 003
    SZSE_GEM   创业板        300 / 301 / 302
    SSE_STAR   科创板        688 / 689
    BSE        北交所        430 / 830-839 / 870-879 / 920

另建一个**默认 Universe**（只建立成员关系，不复制股票数据）：

    沪深主板   成员 = 沪市主板 ∪ 深市主板

设计约束（与项目既有约定一致）：
    - 复用现有 symbol_groups / symbol_group_members，**不新增表、不改 schema**
      schema 的 group_type 只允许 industry / concept / watchlist / custom，
      因此交易板块统一记为 `custom`，并在 description 里写入 `trading_board=<CODE>`
      作为后续阶段区分「交易板块 / 行业 / 概念 / 自选」的依据（不改表结构）
    - **幂等**：重复执行只补齐缺失的成员，不重复插入、不改动已有成员
    - **不删除**：本模块没有任何删除动作（不删股票、不删 K 线、不删既有成员关系）
    - **不猜**：代码段无法可靠判断时返回 None，由调用方统计并报告；
      例如 88xxxx 段归属不确定，故**不**纳入规则（宁可报未分类也不误判）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from database import SymbolGroupRepository, SymbolRepository

#: 交易板块在 symbol_groups.group_type 里的取值（schema 只允许这四种，不新增类型）
TRADING_BOARD_GROUP_TYPE = "custom"
#: description 里用于标识「这是什么集合」的前缀（后续阶段据此区分，不改表结构）
TRADING_BOARD_TAG = "trading_board"
UNIVERSE_TAG = "universe"

#: 沪深主板 Universe 的标识
HS_MAIN_UNIVERSE_CODE = "HS_MAIN"
HS_MAIN_UNIVERSE_LABEL = "沪深主板"


@dataclass(frozen=True)
class TradingBoard:
    """一个交易板块（交易所层级）。"""

    code: str
    label: str
    #: 代码三位前缀（用于分类；来源为各交易所公布的代码段）
    prefixes: tuple[str, ...]
    description: str

    @property
    def group_description(self) -> str:
        return f"{TRADING_BOARD_TAG}={self.code} · {self.description}"


#: 无前缀的北交所段（830-839 / 870-879 用区间表示）
_BSE_PREFIX_RANGES: tuple[tuple[str, str], ...] = (("830", "839"), ("870", "879"))

#: 五个交易板块（顺序固定：沪、深、创业板、科创板、北交所）
TRADING_BOARDS: tuple[TradingBoard, ...] = (
    TradingBoard(
        code="SSE_MAIN",
        label="沪市主板",
        prefixes=("600", "601", "603", "605"),
        description="上海证券交易所主板（A 股）",
    ),
    TradingBoard(
        code="SZSE_MAIN",
        label="深市主板",
        prefixes=("000", "001", "002", "003"),
        description="深圳证券交易所主板（含原中小板 002）",
    ),
    TradingBoard(
        code="SZSE_GEM",
        label="创业板",
        prefixes=("300", "301", "302"),
        description="深圳证券交易所创业板",
    ),
    TradingBoard(
        code="SSE_STAR",
        label="科创板",
        prefixes=("688", "689"),
        description="上海证券交易所科创板（689 为科创板 CDR）",
    ),
    TradingBoard(
        code="BSE",
        label="北交所",
        prefixes=("430", "920"),
        description="北京证券交易所（430/830-839/870-879/920 段）",
    ),
)

BOARD_BY_CODE: dict[str, TradingBoard] = {board.code: board for board in TRADING_BOARDS}
BOARD_CODES: tuple[str, ...] = tuple(board.code for board in TRADING_BOARDS)

#: 沪深主板 Universe = 这些交易板块的并集
HS_MAIN_MEMBERS: tuple[str, ...] = ("SSE_MAIN", "SZSE_MAIN")

#: 进度回调
ProgressHook = Callable[[str], None]


def _noop(_: str) -> None:
    """默认进度回调：什么都不做。"""


def classify_board(symbol: str) -> str | None:
    """按代码段判断交易板块；无法可靠判断时返回 None（不猜）。

    只接受 6 位数字裸代码（与库内约定一致）；其它形态一律返回 None。
    """
    text = str(symbol or "").strip()
    if len(text) != 6 or not text.isdigit():
        return None
    prefix = text[:3]
    for board in TRADING_BOARDS:
        if prefix in board.prefixes:
            return board.code
    for low, high in _BSE_PREFIX_RANGES:
        if low <= prefix <= high:
            return "BSE"
    return None


@dataclass
class BoardSyncReport:
    """交易板块同步结果。"""

    dry_run: bool
    symbols_seen: int = 0
    classified: int = 0
    unclassified: int = 0
    unclassified_codes: list[str] = field(default_factory=list)
    groups_created: int = 0
    groups_existing: int = 0
    added: int = 0
    already_member: int = 0
    failed: int = 0
    per_board: dict[str, int] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def written(self) -> int:
        """本次真正写入的成员关系条数（dry-run 恒为 0）。"""
        return 0 if self.dry_run else self.added

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "symbols_seen": self.symbols_seen,
            "classified": self.classified,
            "unclassified": self.unclassified,
            "unclassified_codes": list(self.unclassified_codes),
            "groups_created": self.groups_created,
            "groups_existing": self.groups_existing,
            "added": self.added,
            "already_member": self.already_member,
            "failed": self.failed,
            "per_board": dict(self.per_board),
            "conflicts": list(self.conflicts),
            "written": self.written,
            "errors": list(self.errors),
        }


def plan_memberships(symbols: Iterable[Sequence[object]]) -> tuple[dict[str, list[int]], list[str]]:
    """把 symbols 行（需含 id / symbol）规划成 {板块代码: [symbol_id...]} 与未分类代码列表。

    纯函数：不写库，便于预演与单测。
    """
    plan: dict[str, list[int]] = {code: [] for code in BOARD_CODES}
    unclassified: list[str] = []
    for row in symbols:
        symbol_id = int(row["id"])
        code = str(row["symbol"])
        board = classify_board(code)
        if board is None:
            unclassified.append(code)
            continue
        plan[board].append(symbol_id)
    return plan, unclassified


def _ensure_group(
    groups: SymbolGroupRepository,
    *,
    name: str,
    description: str,
    dry_run: bool,
    report: BoardSyncReport,
) -> int | None:
    """确保集合存在（幂等）：已存在则原样复用，不修改名称与描述。

    dry-run 下**不建库**：集合不存在时只计数并返回 None（调用方按“空集合”预演）。
    """
    existing = groups.get_group_by_name(name)
    if existing is not None:
        report.groups_existing += 1
        return int(existing["id"])
    report.groups_created += 1
    if dry_run:
        return None
    return groups.create_group(name, TRADING_BOARD_GROUP_TYPE, market="CN", description=description)


def _sync_members(
    groups: SymbolGroupRepository,
    group_id: int | None,
    symbol_ids: Sequence[int],
    *,
    dry_run: bool,
    report: BoardSyncReport,
    label: str,
    progress: ProgressHook,
) -> None:
    """补齐成员（幂等、只增不减）。group_id 为 None 表示 dry-run 下集合尚不存在。"""
    existing_ids = set() if group_id is None else {int(row["id"]) for row in groups.list_group_symbols(group_id)}
    added_before = report.added
    for symbol_id in symbol_ids:
        if symbol_id in existing_ids:
            report.already_member += 1
            continue
        if not dry_run and group_id is not None:
            try:
                groups.add_symbol_to_group(group_id, symbol_id)
            except Exception as exc:  # noqa: BLE001 - 单只失败不影响其它
                report.failed += 1
                report.errors.append(f"{label} symbol_id={symbol_id}: {type(exc).__name__}: {exc}")
                continue
        report.added += 1
    progress(f"{label}: 目标 {len(symbol_ids)} 只，本次新增 {report.added - added_before}、已在集合 {len(existing_ids)}")


def sync_trading_boards(
    groups: SymbolGroupRepository,
    symbols: SymbolRepository,
    *,
    dry_run: bool = True,
    progress: ProgressHook | None = None,
) -> BoardSyncReport:
    """同步五个交易板块 + 沪深主板 Universe（默认只预演，不写库）。

    - 只处理 market='CN' 的股票；其它市场不动
    - 每只股票最多进入一个交易板块（分类由 classify_board 唯一决定）
    - 沪深主板 Universe 只建立成员关系（沪市主板 ∪ 深市主板），不复制股票数据
    - 不做任何删除：既有的集合与成员关系一律保留
    """
    notify = progress or _noop
    report = BoardSyncReport(dry_run=dry_run)

    rows = [row for row in symbols.list_symbols(market="CN")]
    report.symbols_seen = len(rows)
    plan, unclassified = plan_memberships(rows)
    report.classified = report.symbols_seen - len(unclassified)
    report.unclassified = len(unclassified)
    report.unclassified_codes = unclassified[:50]

    for board in TRADING_BOARDS:
        group_id = _ensure_group(
            groups, name=board.label, description=board.group_description, dry_run=dry_run, report=report
        )
        _sync_members(
            groups, group_id, plan[board.code], dry_run=dry_run, report=report, label=board.label, progress=notify
        )
        report.per_board[board.code] = len(plan[board.code])

    # 默认 Universe：只加成员，不复制数据
    hs_main_ids = [symbol_id for code in HS_MAIN_MEMBERS for symbol_id in plan[code]]
    universe_id = _ensure_group(
        groups,
        name=HS_MAIN_UNIVERSE_LABEL,
        description=f"{UNIVERSE_TAG}={HS_MAIN_UNIVERSE_CODE} · 默认 Universe（沪市主板 + 深市主板，仅成员关系）",
        dry_run=dry_run,
        report=report,
    )
    _sync_members(
        groups,
        universe_id,
        hs_main_ids,
        dry_run=dry_run,
        report=report,
        label=HS_MAIN_UNIVERSE_LABEL,
        progress=notify,
    )
    report.per_board[HS_MAIN_UNIVERSE_CODE] = len(hs_main_ids)

    # 冲突检查（只报告，不删除）：同一只股票出现在多个交易板块
    seen: dict[int, list[str]] = {}
    for board in TRADING_BOARDS:
        group = groups.get_group_by_name(board.label)
        if group is None:
            continue
        for row in groups.list_group_symbols(int(group["id"])):
            seen.setdefault(int(row["id"]), []).append(board.code)
    report.conflicts = [f"symbol_id={sid} 同时属于 {sorted(codes)}" for sid, codes in seen.items() if len(codes) > 1]

    return report
