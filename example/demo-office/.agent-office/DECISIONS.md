# Decisions

## D001: Use recall@10 as the main metric

Latency is reported alongside it; nDCG is a secondary check.

## D002: Fix the query set

All runs use the same 2,000 queries and the same index snapshot.

## D003: Rerun dense with a smaller batch

dense-v1 ran out of memory; dense-v2 uses batch size 64.
