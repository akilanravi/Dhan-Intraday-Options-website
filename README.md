# Dhan Quant Intraday Options

Dhan only. Full NSE F&O universe from the Dhan instrument master, including the app's mapped NSE indices. **Paper simulation only. No broker orders and no Telegram.** The swing repository is not involved.

## Update your existing Streamlit app

1. Extract this ZIP and replace the files in the **intraday repository root** with its contents. Keep `.streamlit/config.toml` and all the Python modules. Leave the Streamlit entry point at `app.py`. The new requirements include the Streamlit version needed for lazy tabs.
2. Keep your working Streamlit secrets `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN`. Use your existing journal settings if you already have them. A Gist is optional in dashboard mode. Without it, the local journal may be lost if Streamlit Cloud restarts.
3. **If no separate worker is running, set `PAPER_WORKER_MODE = false`** in Streamlit Secrets, then reboot the app and keep one dashboard session open during NSE hours. You can leave any of the seven tabs selected. When a new scan completes, every live view uses the same snapshot. `Refresh Now` loads the current batch or a read-only historical preview outside the schedule.
4. Verify after an NSE candle closes. Look for **Published candle close**, **Scan progress**, and **Scanned symbols**. Open **Scan and paper-trading diagnostics** or **Paper trading** to see actual entries and the reason for a skipped or blocked entry. Dashboard mode shows partial results as Dhan responds. Choose up to 20 priority symbols in the sidebar to see them first without excluding the rest. Paper entries still wait for the ranked batch. A full scan takes time after the close. An empty eligible list alone is not an error.

### Optional unattended setup on your Windows laptop

Use this if scanning must continue with the dashboard closed. You do not need a paid server, but the laptop must stay awake and connected. Run just one worker for a given journal. You may use an existing intraday secret Gist, or create a new secret Gist with a placeholder file named `README.txt` (do not paste any tokens into Gist files).

1. Open PowerShell in the extracted project folder. Install Python 3.11 or 3.12 first if `py` is not available.
   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   Copy-Item worker.env.example worker.env
   ```
2. Edit `worker.env` **on the laptop only**. Set the Dhan credentials and the GitHub token with Gist access, plus `INTRADAY_GIST_ID` (the ID at the end of the Gist URL). Keep this file out of GitHub. If you already have a worker.env file, change INTRADAY_RISK_PCT to 5 there to use the new paper-risk setting.
3. In Streamlit Secrets, set `PAPER_WORKER_MODE = true` and use the **same** Gist ID and GitHub token. Keep the Dhan secrets there for on-demand backtests and quote preflight.
   ```toml
   PAPER_WORKER_MODE = true
   GITHUB_TOKEN = "your-token-with-gist-access"
   INTRADAY_GIST_ID = "your-intraday-gist-id"
   ```
4. Double-click `Run_Intraday_Worker.cmd`, or run `.\.venv\Scripts\python.exe paper_worker.py --env worker.env`. To test one cycle, add `--once` during the regular session after a five-minute boundary. The dashboard pulls results from the worker and does not run a second scanner or write the journal. The sidebar cannot change worker settings. Adjust them in `worker.env`. You can update the Dhan token in that file without restarting the running worker.

## How the live cycle works

- Starts shortly after each completed five-minute bar, from the **09:20 IST close** through the **15:30 IST close**. One batch per candle, no overlap. Regular weekdays only, excluding dates entered in `NSE_CLOSED_DATES` in the relevant configuration. Special or shifted exchange sessions are not scheduled.
- Uses persistent HTTP connections, the existing Dhan rate limiter, cached history plus incremental fetches, and a bounded retry for delayed candles. A warm scan normally requests just the newest completed bar and one overlapping bar per symbol. First run and gaps still need history. Dhan’s published five data requests per second means a 241-symbol REST scan takes on the order of 50 seconds minimum before retries and option-chain lookups. No WebSocket and no Yahoo fallback. The universe stays full when you filter the displayed table. Coverage and skipped candles are shown explicitly.
- Scores CE and PE separately. Existing 7/8 default and mandatory filters remain. NIFTY tide is unverified and not counted. Spot indices without volume use a mapped near-month index future as an explicitly labelled volume proxy when available. Without verified volume, those setups cannot pass the volume filter.
- New simulated fills use an observed Dhan ask for a liquid ATM or one-strike ITM option and a fresh candle between **09:45 and 15:10 IST** (end exclusive). Maximum two new trades per day and three open trades. At the default ₹100,000 capital, 5% risk and 20% premium stop, the effective one-lot premium cap is **₹25,000** before exposure even though the nominal limit is ₹35,000. The engine tests eligible strikes against that effective cap and reports why it skips. It does not automatically loosen the settings to force a trade.
- Existing positions are checked first at an observed Dhan bid. Exits use the configured premium stop/target, ST(7,2) reversal, opposing signal or EOD checks starting at **15:20 IST**. Spot scans/price snapshots can miss movement between checks. No valid bid means the position stays open. The 15:30 close is a scan only, no simulated execution after the market closes.
- Market Scanner, Eligible Signal Cards, Trade Analysis, Paper Trading, Conditions and Trade Decision each read the published snapshot. The Trade Decision quote preflight and the Call/Put Backtest are still on demand. The preflight can show additional advisory checks that are not automatic entry rules.

Dhan references for [Historical Data](https://dhanhq.co/docs/v2/historical-data/), [API rate limits](https://dhanhq.co/docs/v2/#rate-limit), and [instrument master](https://dhanhq.co/docs/v2/instruments/). Live timing and coverage depend on Dhan availability and were not verified with a live token in this package.

## Local verification

Run `python -m unittest discover -s tests -v` in the project folder. The tests use mocked broker data and do not submit orders.
