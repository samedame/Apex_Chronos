"""APEX-CHRONOS 1M alert bot.

Alert-only (never places orders). One shared engine drives both the historical
replay used for verification and the live minute-by-minute loop, so the live
bot cannot drift from what was backtested without the verification failing.
"""
__version__ = "1.0.0"
