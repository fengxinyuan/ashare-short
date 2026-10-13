"""手工维护的持仓表，为监控提供成本、股数、建仓日及止损/目标。

查询使用实例快照；每次修改在文件锁内重读最新持仓，成功落盘后才更新实例。
标的输入与持仓职责见 docs/ARCHITECTURE.md。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path

from ashare_decision_support.numeric import finite, positive
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import file_lock, read_json, write_json
from ashare_decision_support.symbols import VALID_SUFFIXES, normalize_symbol

POSITIONS_FILE = "positions.json"


def unrealised_pct(cost_price: float | None, price: float | None) -> float | None:
    """持仓和监控视图共用的浮动盈亏；任一价格无效时返回 None。"""
    cost, last = positive(cost_price), positive(price)
    if cost is None or last is None:
        return None
    return finite(round(last / cost - 1.0, 6))


def _position_symbol(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    code, _, suffix = normalized.partition(".")
    if len(code) != 6 or not code.isascii() or not code.isdigit() or suffix not in VALID_SUFFIXES:
        raise ValueError(f"无法识别的标的代码：{symbol!r}")
    return normalized


def _share_count(value: object) -> int | None:
    number = positive(value)
    return int(number) if number is not None and number.is_integer() else None


def _required_price(value: object, label: str) -> float:
    number = positive(value)
    if number is None:
        raise ValueError(f"{label}必须是大于 0 的有限数字")
    return number


def _today() -> date:
    """北京日期；延迟导入以避开 schedule -> daemon -> state 的初始化环。"""
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
    opened_on: str | None = None  # 建仓日，时间止损从这天算起
    plan_stop: float | None = None  # 你自己定的止损，优先于系统算出来的
    plan_target: float | None = None
    note: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> Position:
        return cls(
            symbol=_position_symbol(payload.get("symbol", "")),
            name=payload.get("name"),
            cost_price=positive(payload.get("cost_price")),
            shares=_share_count(payload.get("shares")),
            opened_on=payload.get("opened_on"),
            plan_stop=positive(payload.get("plan_stop")),
            plan_target=positive(payload.get("plan_target")),
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
        return unrealised_pct(self.cost_price, price)


class Positions:
    """持仓表。人维护、持久，只有你自己会增删。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else state_path(POSITIONS_FILE)
        self.entries = self._load()

    def _load(self) -> list[Position]:
        rows = read_json(self.path, default=[]) or []
        entries: dict[str, Position] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                entry = Position.from_dict(row)
            except ValueError:
                continue
            entries[entry.symbol] = entry
        return list(entries.values())

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
        """建仓或改仓。省略的字段保留，校验或写入失败时不改变持仓。"""
        code = _position_symbol(symbol)
        cost = _required_price(cost_price, "成本价")
        count = _share_count(shares)
        if shares is not None and count is None:
            raise ValueError("持仓股数必须是正整数")
        if opened_on is not None and type(opened_on) is not date:
            raise ValueError("建仓日必须是有效日期")

        changes = {
            "cost_price": cost,
            "shares": count,
            "plan_stop": None if plan_stop is None else _required_price(plan_stop, "止损价"),
            "plan_target": None if plan_target is None else _required_price(plan_target, "目标价"),
            "name": name,
            "note": note,
        }
        changes = {key: value for key, value in changes.items() if value is not None}
        with file_lock(self.path):
            entries = self._load()
            existing = next((item for item in entries if item.symbol == code), None)
            entry = replace(existing or Position(symbol=code), **changes)
            if opened_on is not None or not entry.opened_on:
                entry.opened_on = str(opened_on or today or _today())
            updated = [entry if item.symbol == code else item for item in entries]
            if existing is None:
                updated.append(entry)
            self._save(updated)
        return entry

    def close(self, symbol: str) -> bool:
        """平仓即删除这一条。

        不留一条「曾经持有」的空壳：这个文件唯一的消费者是盘中卖点监控，而卖点
        对一只已经卖掉的票没有任何意义。历史留在台账里（`journal/`），那才是
        回答「当时为什么买、后来怎么样了」的地方。
        """
        code = normalize_symbol(symbol)
        with file_lock(self.path):
            entries = self._load()
            updated = [item for item in entries if item.symbol != code]
            if len(updated) == len(entries):
                self.entries = entries
                return False
            self._save(updated)
        return True

    def clear(self) -> int:
        with file_lock(self.path):
            count = len(self._load())
            self._save([])
        return count

    def _save(self, entries: list[Position]) -> None:
        write_json(self.path, [entry.to_dict() for entry in entries])
        self.entries = entries
