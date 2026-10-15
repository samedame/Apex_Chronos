# Reference implementation (the oracle)

These four files are **unmodified copies** of the original research code from the
APEX-CHRONOS 1M workspace. They are the definition of "how the strategy was
backtested". `verification/reference_runner.py` runs them without changing a line of
logic: it only rewrites the hard-coded `/home/user/...` data/output paths (and,
for the extended cross-check, the `TICKERS` list) in a temporary copy.

`SHA256SUMS` pins the exact bytes. `python -m verification.verify_against_backtest`
re-checks the hashes before it trusts the oracle.
