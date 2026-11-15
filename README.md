# Dhan Quant Intraday Options — standalone Streamlit app

**Separate GitHub repository.** This is a prototype and has NOT been live-tested against Dhan or validated for profitability.

## What it does
- Downloads the current NSE F&O universe from Dhan's public instrument master; selected instruments use Dhan's intraday candle endpoint.
- Scores completed 5-minute candles with CPR, breakout/retest, prior-day range, EMA 9/21, VWAP, RSI, Supertrend 10,3, and volume >=1.5x.
- The ninth historical Telegram condition (NIFTY tide) is displayed as **not verified**, and is excluded from the 8-point score until integrated. The PDH/PDL check is not a full Camarilla calculation. This is **not an exact reproduction** of your earlier Telegram bot.
- Auto-opens simulated CE/PE contracts using Dhan option-chain ask prices when all mandatory filters, lot affordability, risk and exposure checks pass.
- Automatically checks open trades during regular weekday market hours while the dashboard is connected. **REFRESH NOW** requests the same cycle immediately. It checks all open trades using Dhan bid prices; exits on stop, target, ST(7,2) flip, opposing eligible signal, or 15:20 IST. No unattended monitoring after the dashboard disconnects. Stops and targets can be missed between observations.
- Logs simulated trades in CSV; optional private GitHub Gist for persistence.

## Create a new repository and deploy
1. Create a **new** GitHub repository, e.g. `dhan-intraday-options`. Do not overwrite the swing repository.
2. Upload `app.py`, `intraday.py`, `swing_core.py`, `requirements.txt`, `.streamlit/config.toml` preserving folders.
3. On Streamlit Community Cloud select this repository and main file `app.py`.
4. Add secrets in **Streamlit app settings**, not in GitHub code:

```toml
DHAN_CLIENT_ID = "your_dhan_client_id"
DHAN_ACCESS_TOKEN = "your_dhan_access_token"
# Optional but needed for durable paper trades:
GITHUB_TOKEN = "your_github_token_with_gist_access"
INTRADAY_GIST_ID = "existing_private_gist_id"
```

5. Open the app during market hours. The scan and paper cycle start automatically, and **REFRESH NOW** remains available. Confirm candles are recent and quotes are available. When the first private Gist is created, copy the ID displayed in the app into `INTRADAY_GIST_ID` and redeploy. A GitHub token must be kept secret.

## Important safeguards / known limitations
- **No real orders**; all trading is simulated. Dhan access token may expire.
- Option-chain calls may be rate-limited; failed quotes leave positions open. Quotes are snapshots, not fill confirmations.
- Gist is shared across app sessions: multiple simultaneous users could race; use one user only.
- With the dashboard connected, it starts a refresh roughly every 60 seconds during regular NSE market hours (IST). Slow scans do not overlap. The previous scan stays on screen while Dhan requests run. For monitoring with the app closed, a separate always-on worker and persistent trade state would be required.
- Simulated P&L uses a **rough cost approximation**. It is not an exact Zerodha/Dhan statutory charge model.
- No validated backtest, no claim of guaranteed profit. The signal checks and Dhan endpoint payloads require live testing.
- Default 1% risk cap can block many options: this is intentional.
- Scanner only fetches selected symbols per refresh; selecting the entire universe may be slow and rate-limited.
- Never paste access tokens or GitHub tokens in screenshots or messages.

## Updating V1
Replace `app.py`, `swing_core.py` and `requirements.txt` in the intraday repository. Keep your existing Streamlit Secrets and use a single session to avoid concurrent journal writes. The swing app is unchanged.
