"""Backtest metrics, ledgers, and reproducible tear-sheet artifacts."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

import matplotlib.pyplot as plt
import pandas as pd

from .execution import BacktestResult


def result_frames(result: BacktestResult) -> dict[str, pd.DataFrame]:
    trades = []
    for trade in result.trades:
        trades.append(
            {
                "instrument": trade.instrument,
                "contract": trade.contract,
                "session": trade.session,
                "direction": trade.direction.value,
                "entry_time": trade.entry.timestamp,
                "entry_price": trade.entry.price,
                "exit_time": trade.exits[-1].timestamp,
                "exit_price": trade.exits[-1].price,
                "quantity": trade.entry.quantity,
                "stop_price": trade.stop_price,
                "target_price": trade.target_price,
                "gross_pnl": trade.gross_pnl,
                "fees": trade.fees,
                "net_pnl": trade.net_pnl,
                "risk_points": trade.risk_points,
                "r_multiple": trade.r_multiple,
                "exit_reason": trade.exit_reason.value,
                "duration_minutes": int(
                    (trade.exits[-1].timestamp - trade.entry.timestamp)
                    / pd.Timedelta(minutes=1)
                ),
            }
        )
    equity = [asdict(point) for point in result.equity]
    fills = [
        {
            **asdict(fill),
            "side": fill.side.value,
            "reason": fill.reason.value,
        }
        for fill in result.fills
    ]
    violations = [asdict(item) for item in result.violations]
    cancellations = [
        {"timestamp": timestamp, "instrument": symbol, "reason": reason}
        for timestamp, symbol, reason in result.cancellations
    ]
    return {
        "trades": pd.DataFrame(
            trades,
            columns=[
                "instrument",
                "contract",
                "session",
                "direction",
                "entry_time",
                "entry_price",
                "exit_time",
                "exit_price",
                "quantity",
                "stop_price",
                "target_price",
                "gross_pnl",
                "fees",
                "net_pnl",
                "risk_points",
                "r_multiple",
                "exit_reason",
                "duration_minutes",
            ],
        ),
        "equity": pd.DataFrame(equity, columns=["timestamp", "balance", "equity"]),
        "fills": pd.DataFrame(
            fills,
            columns=[
                "timestamp",
                "instrument",
                "side",
                "quantity",
                "nominal_price",
                "price",
                "fee",
                "reason",
                "contract",
            ],
        ),
        "violations": pd.DataFrame(
            violations, columns=["timestamp", "rule", "drawdown", "limit"]
        ),
        "cancellations": pd.DataFrame(
            cancellations, columns=["timestamp", "instrument", "reason"]
        ),
    }


def calculate_metrics(result: BacktestResult) -> dict[str, Any]:
    frames = result_frames(result)
    trades, equity = frames["trades"], frames["equity"]
    net = trades.get("net_pnl", pd.Series(dtype=float))
    gross_profit = float(net[net > 0].sum()) if not net.empty else 0.0
    gross_loss = float(-net[net < 0].sum()) if not net.empty else 0.0
    if equity.empty:
        max_drawdown = daily_max_drawdown = 0.0
    else:
        values = equity["equity"].astype(float)
        max_drawdown = float((values.cummax() - values).max())
        reset = result.config.risk_limits.reset_time
        dates = pd.to_datetime(equity["timestamp"]).map(
            lambda stamp: (
                stamp.date()
                if stamp.time() < reset
                else (stamp + pd.Timedelta(days=1)).date()
            )
        )
        daily_max_drawdown = float(
            equity.assign(_date=dates)
            .groupby("_date")["equity"]
            .apply(lambda series: (series.cummax() - series).max())
            .max()
        )
    metrics: dict[str, Any] = {
        "starting_balance": result.config.starting_balance,
        "ending_balance": result.ending_balance,
        "total_net_profit": result.ending_balance - result.config.starting_balance,
        "trade_count": int(len(trades)),
        "win_rate": float((net > 0).mean()) if not net.empty else 0.0,
        "average_win": float(net[net > 0].mean()) if (net > 0).any() else 0.0,
        "average_loss": float(net[net < 0].mean()) if (net < 0).any() else 0.0,
        "expectancy": float(net.mean()) if not net.empty else 0.0,
        "average_r_multiple": float(trades["r_multiple"].mean())
        if not trades.empty
        else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "maximum_drawdown": max_drawdown,
        "daily_maximum_drawdown": daily_max_drawdown,
        "fees": float(trades["fees"].sum()) if not trades.empty else 0.0,
        "risk_violation_count": len(result.violations),
    }
    if not trades.empty:
        metrics["by_session"] = (
            trades.groupby("session")["net_pnl"]
            .agg(["count", "sum", "mean"])
            .to_dict("index")
        )
        metrics["by_instrument"] = (
            trades.groupby("instrument")["net_pnl"]
            .agg(["count", "sum", "mean"])
            .to_dict("index")
        )
        enriched = trades.assign(
            weekday=pd.to_datetime(trades["entry_time"]).dt.day_name(),
            month=pd.to_datetime(trades["entry_time"]).dt.strftime("%Y-%m"),
        )
        for field in ("direction", "exit_reason", "weekday", "month"):
            metrics[f"by_{field}"] = (
                enriched.groupby(field)["net_pnl"]
                .agg(["count", "sum", "mean"])
                .to_dict("index")
            )
    else:
        metrics["by_session"] = {}
        metrics["by_instrument"] = {}
        for field in ("direction", "exit_reason", "weekday", "month"):
            metrics[f"by_{field}"] = {}
    return metrics


class TearSheetBuilder:
    def build(
        self,
        result: BacktestResult,
        output_dir: str | Path,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Path]:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        frames = result_frames(result)
        paths: dict[str, Path] = {}
        for name, frame in frames.items():
            path = output / f"{name}.csv"
            frame.to_csv(path, index=False)
            paths[name] = path
        metrics = calculate_metrics(result)
        payload = {"metrics": metrics, "metadata": dict(metadata or {})}
        metrics_path = output / "metrics.json"
        metrics_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
        paths["metrics"] = metrics_path

        equity = frames["equity"]
        figure, axes = plt.subplots(2, 2, figsize=(14, 9))
        equity_axis, drawdown_axis, daily_axis, session_axis = axes.flat
        if equity.empty:
            for axis in (equity_axis, drawdown_axis):
                axis.text(0.5, 0.5, "No equity observations", ha="center", va="center")
        else:
            timestamps = pd.to_datetime(equity["timestamp"])
            values = equity["equity"].astype(float)
            equity_axis.plot(timestamps, values, color="#1677c8")
            equity_axis.set_title("Equity Curve")
            equity_axis.set_ylabel("Account value")
            drawdown = values.cummax() - values
            drawdown_axis.fill_between(
                timestamps, drawdown, color="#c83b3b", alpha=0.65
            )
            drawdown_axis.set_title("Drawdown")
            drawdown_axis.set_ylabel("Dollars")
        trades = frames["trades"]
        if trades.empty:
            daily_axis.text(0.5, 0.5, "No daily P&L", ha="center", va="center")
            session_axis.text(0.5, 0.5, "No session results", ha="center", va="center")
        else:
            dates = pd.to_datetime(trades["exit_time"]).dt.date
            daily = trades.assign(_date=dates).groupby("_date")["net_pnl"].sum()
            daily_axis.bar(daily.index.astype(str), daily.values, color="#1677c8")
            daily_axis.tick_params(axis="x", rotation=45)
            daily_axis.set_title("Daily Net P&L")
            session = trades.groupby("session")["net_pnl"].sum().sort_values()
            session_axis.barh(session.index, session.values, color="#4e9f63")
            session_axis.set_title("Net P&L by Session")
        for axis in axes.flat:
            axis.grid(alpha=0.2)
        figure.tight_layout()
        chart_path = output / "tear-sheet.png"
        figure.savefig(chart_path, dpi=150)
        pdf_path = output / "tear-sheet.pdf"
        figure.savefig(pdf_path)
        plt.close(figure)
        paths["chart"] = chart_path
        paths["report"] = pdf_path
        return paths
