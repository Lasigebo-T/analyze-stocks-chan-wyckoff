#!/usr/bin/env python3
"""Calculate economic-cost changes from one or more T trades (standard library only)."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Sequence


MONEY = Decimal("0.01")
PER_SHARE = Decimal("0.0001")


@dataclass(frozen=True)
class Trade:
    quantity: Decimal
    sell_price: Decimal
    buy_price: Decimal
    fees: Decimal


def parse_decimal(value: str, label: str) -> Decimal:
    try:
        number = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise argparse.ArgumentTypeError(f"{label} must be a decimal number: {value!r}")
    if not number.is_finite():
        raise argparse.ArgumentTypeError(f"{label} must be finite: {value!r}")
    return number


def positive(value: str) -> Decimal:
    number = parse_decimal(value, "value")
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return number


def nonnegative(value: str) -> Decimal:
    number = parse_decimal(value, "fees")
    if number < 0:
        raise argparse.ArgumentTypeError("fees cannot be negative")
    return number


def parse_trade(value: str) -> Trade:
    fields = [item.strip() for item in value.split(",")]
    if len(fields) != 4 or any(not item for item in fields):
        raise argparse.ArgumentTypeError(
            "--trade must be quantity,sell_price,buy_price,fees; e.g. 1000,88,82,16"
        )
    return Trade(positive(fields[0]), positive(fields[1]), positive(fields[2]), nonnegative(fields[3]))


def decimal_string(value: Decimal) -> str:
    """Avoid a lossy conversion from Decimal to a JSON float."""
    return format(value, "f")


def money(value: Decimal) -> str:
    return format(value.quantize(MONEY, rounding=ROUND_HALF_UP), ".2f")


def per_share(value: Decimal) -> str:
    return format(value.quantize(PER_SHARE, rounding=ROUND_HALF_UP), ".4f")


def parser_for_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calculate the impact of T trades on overall economic cost.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Example:\n"
            "  python t_cost.py --shares 3000 --cost 88 --trade 1000,88,82,16 --trade 1000,87,83,16\n\n"
            "Formula: net profit = quantity * (sell price - buy price) - fees\n"
            "         cost reduction/share = net profit / original shares\n"
            "         final cost = initial cost - total net profit / original shares"
        ),
    )
    parser.add_argument("--shares", required=True, type=positive,
                        help="Original total holding Q; must be greater than zero.")
    parser.add_argument("--cost", required=True, type=positive,
                        help="Initial economic cost C per share; must be greater than zero.")
    parser.add_argument("--trade", required=True, action="append", type=parse_trade,
                        metavar="Q,SELL,BUY,FEES",
                        help="One T trade; repeat for multiple trades. Fees may be zero.")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Emit JSON. Decimal values are exact strings, not lossy floats.")
    return parser


def validate_trades(parser: argparse.ArgumentParser, shares: Decimal, trades: Sequence[Trade]) -> None:
    for index, trade in enumerate(trades, 1):
        if trade.quantity > shares:
            parser.error(
                f"trade {index} quantity {decimal_string(trade.quantity)} cannot exceed "
                f"original shares {decimal_string(shares)}"
            )


def calculate(shares: Decimal, initial_cost: Decimal, trades: Sequence[Trade]) -> dict:
    total_profit = Decimal("0")
    rows = []
    for index, trade in enumerate(trades, 1):
        net_profit = trade.quantity * (trade.sell_price - trade.buy_price) - trade.fees
        reduction = net_profit / shares
        total_profit += net_profit
        rows.append({
            "round": index,
            "quantity": trade.quantity,
            "sell_price": trade.sell_price,
            "buy_price": trade.buy_price,
            "fees": trade.fees,
            "net_profit": net_profit,
            "per_share_reduction": reduction,
        })
    total_reduction = total_profit / shares
    return {
        "shares": shares,
        "initial_cost": initial_cost,
        "rounds": rows,
        "total_net_profit": total_profit,
        "total_per_share_reduction": total_reduction,
        "final_economic_cost": initial_cost - total_reduction,
    }


def render_json(result: dict) -> str:
    rounds = []
    for row in result["rounds"]:
        rounds.append({key: value if key == "round" else decimal_string(value)
                       for key, value in row.items()})
    return json.dumps({
        "input": {
            "shares": decimal_string(result["shares"]),
            "initial_economic_cost": decimal_string(result["initial_cost"]),
        },
        "rounds": rounds,
        "summary": {
            "total_net_profit": decimal_string(result["total_net_profit"]),
            "total_per_share_reduction": decimal_string(result["total_per_share_reduction"]),
            "final_economic_cost": decimal_string(result["final_economic_cost"]),
        },
    }, ensure_ascii=False, indent=2)


def render_human(result: dict) -> str:
    lines = [
        f"Original shares: {decimal_string(result['shares'])}",
        f"Initial economic cost: {per_share(result['initial_cost'])} per share",
        "",
        "Round  Quantity  Sell price  Buy price  Fees       Net profit  Cost reduction/share",
        "-----  --------  ----------  ---------  ---------  ----------  --------------------",
    ]
    for row in result["rounds"]:
        lines.append(
            f"{row['round']:>5}  {decimal_string(row['quantity']):>8}  "
            f"{money(row['sell_price']):>10}  {money(row['buy_price']):>9}  "
            f"{money(row['fees']):>9}  {money(row['net_profit']):>10}  "
            f"{per_share(row['per_share_reduction']):>20}"
        )
    lines.extend((
        "",
        f"Total net profit: {money(result['total_net_profit'])}",
        f"Total cost reduction/share: {per_share(result['total_per_share_reduction'])}",
        f"Final economic cost: {per_share(result['final_economic_cost'])} per share",
    ))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = parser_for_cli()
    args = parser.parse_args(argv)
    validate_trades(parser, args.shares, args.trade)
    result = calculate(args.shares, args.cost, args.trade)
    print(render_json(result) if args.as_json else render_human(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
