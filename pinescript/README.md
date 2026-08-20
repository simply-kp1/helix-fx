# HELIX Pine Script (TradingView)

`helix_ib_breakout.pine` is a Pine Script v5 port of the live Python agent —
the same Initial Balance breakout strategy runs against OANDA, ready to
backtest inside TradingView on any instrument.

## Defaults

The script now defaults to the **DAX / Xetra opening range** setup
(IB 08:00-09:00 London, session end 17:00 London) to match the current
`config.yaml`. Change the three "IB Start Hour", "IB End Hour" and
"Session End Hour" inputs to `6 / 7 / 18` to run the original FX
London-open configuration.

## How to use

1. Open TradingView. Pick a chart:
   * **DAX:** `OANDA:DE30EUR` or `XETR:DAX` on the **30-minute** timeframe.
   * **FX:** `OANDA:EURUSD`, `OANDA:GBPUSD`, `OANDA:USDJPY` at M30.
2. Open the **Pine Editor** (bottom panel).
3. Copy the entire contents of `helix_ib_breakout.pine` into the editor.
4. Click **Save** → then **Add to chart**.
5. Open **Strategy Tester** at the bottom to see trades, equity curve,
   and stats.

## Notes on parity with the live agent

* Strategy logic (IB, EMA-100 filter, ATR-buffered SL, min 1 : 1.5 R:R,
  session times, max 3 trades / day, cancel un-filled orders at session
  end) is identical to `helix/agent.py` / `helix/strategy/*`.
* Position sizing is risk-based on strategy equity. For USD-quoted FX
  pairs (EUR/USD, GBP/USD, AUD/USD…) sizing matches the live agent
  exactly. For USD-base pairs (USD/JPY, USD/CAD…) the Pine version
  applies the proper JPY→USD conversion — the live agent has a known
  pre-existing bug that oversizes JPY trades, so Pine results for
  USD/JPY will look more realistic.
* For DAX and other index CFDs the strategy quantity is treated as
  "1 unit per point of movement" which matches how TradingView's
  broker-emulated fills score the PnL.
* Commission and slippage defaults are set inside the script header
  (`commission_type = strategy.commission.percent`, `commission_value = 0.005`,
  `slippage = 1` tick). Adjust for your broker.
* TradingView uses the chart's candle high/low for stop-fill detection,
  which is the same conservative model the Python backtest uses.

## Timezone

The script reads all times in `Europe/London`, regardless of your
TradingView chart clock — see the `Session Timezone` input if you need
to change it (e.g. to `Europe/Berlin` for DAX or `America/New_York`
for US indices).
