# HELIX Pine Script (TradingView)

`helix_ib_breakout.pine` is a Pine Script v5 port of the live Python agent —
the same Initial Balance breakout strategy the trading agent runs against
OANDA, ready to backtest inside TradingView on any FX pair.

## How to use

1. Open TradingView, choose a chart such as **EURUSD**, **GBPUSD** or **USDJPY**.
2. Switch the timeframe to **30 minutes**.
3. Open the **Pine Editor** (bottom panel).
4. Copy the entire contents of `helix_ib_breakout.pine` into the editor.
5. Click **Save** → then **Add to chart**.
6. Open **Strategy Tester** at the bottom to see trades, equity curve,
   and stats.

## Notes on parity with the live agent

* Strategy logic (IB, EMA-100 filter, ATR-buffered SL, min 1 : 1.5 R:R,
  session times, max 3 trades / day, cancel un-filled orders at 18:00
  London) is identical to `helix/agent.py` / `helix/strategy/*`.
* Position sizing is risk-based on strategy equity. For USD-quoted pairs
  (EUR/USD, GBP/USD, AUD/USD…) sizing matches the live agent exactly.
  For USD-base pairs (USD/JPY, USD/CAD…) the Pine version applies the
  proper JPY→USD conversion — the live agent has a known pre-existing
  bug that oversizes JPY trades, so Pine results for USD/JPY will look
  different (and are more realistic).
* TradingView commission and slippage defaults are set inside the script:
  * `commission_value = 0.00007` (~0.7 pips round-trip)
  * `slippage = 1` tick
  Adjust in the `strategy(...)` header if your broker differs.
* TradingView uses the *chart's* candle high/low for stop-fill detection,
  which is the same conservative model the Python backtest uses.

## Timezone

The script reads all times in `Europe/London`, regardless of your
TradingView chart clock — see the `Session Timezone` input if you need
to change it (e.g. to `America/New_York`).
