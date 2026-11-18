# Selecting evidence for the subscription verifier

The 10-section, 36,000-byte pack delivered all labelled quotes for 498 of 522
quoted development questions, the most of the four registered pack sizes.

A small addition delivered all quotes for 504 of 522 questions at the same byte
limit. When a query term appears in at most three corpus chunks and is missing
from the selected pack, the CLI can move up to three already retrieved passages
containing those terms to the front. The candidates come from the existing
hybrid, vector and text ranks. The raw Recall@5 calculation uses the original ranks.

Use this pack for the verifier and the subsequent answer. The study freezes
the verifier, pack and model before it collects predictions on the new cohort.
