"""Observation-only research modules (docs/LAB_PROGRAMME.md).

Nothing in this package changes a P2 signal, threshold, exit, ticker, alert byte or the frozen P2 rule. Every function
is either a causal feature (uses only data up to and including the entry minute), a flag that is logged next to the real
alert and never applied, or a harness that reads archived data.
"""
