"""Shared parsing primitives.

Everything in here is deterministic, side-effect free and independent of the stage it runs in,
so triage (Stage 1), accumulation (Stage 2) and extraction (Stage 3) all agree on what a PO
number, a spec code or a quantity *is*. Before this package each stage had its own regex in
config/settings.py and they disagreed — see docs/CODE-ANALYSIS-FINDINGS.md C4.

Every rule here was written against the real June corpus (Documents/Premier/5,8 june), not
against invented samples; the corpus grammar is documented in
Documents/Premier_Delivery_Email_Corpus_Analysis.md.
"""
