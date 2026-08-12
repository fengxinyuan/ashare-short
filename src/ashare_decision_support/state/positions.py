"""你手上真正持有的那几只。

这里从前是两份清单：自选股（人维护、永不自动删除）与候选池（每早生成、隔日
失效），所有接受 `--symbols` 的命令省略参数时都读它们。两份在 2026-08-11 一起
删掉了，判据是**使用方式变了**：标的由你在提问时直接给出，`advise` / `ask` /
`chat` 不再替你猜该看哪几只。一份没人维护却仍被当默认输入的清单，比没有清单更
糟——它会让每条命令都多算十几只你今天根本没在看的票，而结论看起来一样正常。

留下的只有持仓，因为它回答的是另一个问题。持仓不是「想看什么」，是**已经承担
了风险的那几只**：止损、目标、结构破坏、时间止损、浮盈回吐这五类判断只对它们
成立，而且必须在盘中被盯着——那不能靠你每次手输代码来触发。

系统只出建议，不下单、不撤单、不接管账户，所以成本价、股数、计划止损与目标
全部由人手工维护（`ashare-ds position open`），这一层只读写这一个文件。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import read_json, write_json
from ashare_decision_support.symbols import normalize_symbol

POSITIONS_FILE = "positions.json"


def _today() -> date:
    """今天（北京时区）。

    延迟导入：`schedule/__init__` 会把 `daemon` 拉进来，而 `daemon` 又 import
    `state`——放在模块顶端就成了一个环，且环成不成立取决于**谁先被 import**。
    这一层只在真正需要日期时才要那个函数，所以放在这里最省事，也最不容易在
    某次无关的导入顺序改动后突然炸掉。
    """
    from ashare_decision_support.schedule.timezone import beijing_today

    return beijing_today()


@dataclass
class Position:
    """一笔持仓。字段全部由人手工填写维护。

    没有 `cost_price` 就给不出「你已经浮盈 4%，跌破 X 该走」这类建议，只能报
    绝对价位——所以 CLI 那一侧把成本价定为必填，这里仍然容忍它缺失：读到一份
    手工改过的文件时，少一个数应该降级成「只报价位」，而不是让盘中监控起不来。
    """

    symbol: str
    name: str | None = None
    cost_price: float | None = None
    shares: int | None = None
    opened_on: str | None = None      # 建仓日，时间止损从这天算起
    plan_stop: float | None = None    # 你自己定的止损，优先于系统算出来的
    plan_target: float | None = None
    note: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> Position:
        return cls(
            symbol=normalize_symbol(payload.get("symbol", "")),
            name=payload.get("name"),
            cost_price=payload.get("cost_price"),
            shares=payload.get("shares"),
            opened_on=payload.get("opened_on"),
            plan_stop=payload.get("plan_stop"),
            plan_target=payload.get("plan_target"),
            note=payload.get("note"),
        )

    @property
    def held_days(self) -> int | None:
        """持仓自然日数。时间止损用它，不精确到交易日——差一两天不影响判断。"""
        if not self.opened_on:
            return None
        try:
            return (_today() - date.fromisoformat(str(self.opened_on))).days
        except (TypeError, ValueError):
            return None

    def unrealised_pct(self, price: float | None) -> float | None:
        """浮动盈亏。没有成本价就返回 None，不猜。"""
        if not self.cost_price or not price:
            return None
        return round(float(price) / float(self.cost_price) - 1.0, 6)


class Positions:
    """持仓表。人维护、持久，只有你自己会增删。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else state_path(POSITIONS_FILE)
        rows = read_json(self.path, default=[]) or []
        self.entries: list[Position] = [
            Position.from_dict(row) for row in rows if isinstance(row, dict)
        ]

    def symbols(self) -> list[str]:
        return [entry.symbol for entry in self.entries]

    def get(self, symbol: str) -> Position | None:
        target = normalize_symbol(symbol)
        return next((entry for entry in self.entries if entry.symbol == target), None)

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, symbol: object) -> bool:
        return self.get(str(symbol)) is not None

    def open(
        self,
        symbol: str,
        cost_price: float,
        shares: int | None = None,
        opened_on: date | None = None,
        plan_stop: float | None = None,
        plan_target: float | None = None,
        name: str | None = None,
        note: str | None = None,
        today: date | None = None,
    ) -> Position:
        """建仓或改仓。已存在就就地更新，不会重复插一条。"""
        code = normalize_symbol(symbol)
        if not code:
            raise ValueError(f"无法识别的标的代码：{symbol!r}")

        entry = self.get(code)
        if entry is None:
            entry = Position(symbol=code)
            self.entries.append(entry)
        entry.cost_price = float(cost_price)
        entry.shares = shares if shares is None else int(shares)
        entry.opened_on = str(opened_on or today or _today())
        # 补填不覆盖：一次不带 `--stop` 的改仓不该把你上次定的止损抹掉。
        for attribute, value in (
            ("plan_stop", None if plan_stop is None else float(plan_stop)),
            ("plan_target", None if plan_target is None else float(plan_target)),
            ("name", name),
            ("note", note),
        ):
            if value is not None:
                setattr(entry, attribute, value)
        self.save()
        return entry

    def close(self, symbol: str) -> bool:
        """平仓即删除这一条。

        不留一条「曾经持有」的空壳：这个文件唯一的消费者是盘中卖点监控，而卖点
        对一只已经卖掉的票没有任何意义。历史留在台账里（`journal/`），那才是
        回答「当时为什么买、后来怎么样了」的地方。
        """
        entry = self.get(symbol)
        if entry is None:
            return False
        self.entries.remove(entry)
        self.save()
        return True

    def clear(self) -> int:
        count = len(self.entries)
        self.entries = []
        self.save()
        return count

    def save(self) -> None:
        write_json(self.path, [entry.to_dict() for entry in self.entries])
