# Query Plans & Performance Analysis

This document records the execution plans and indexing verification for the Trade History keyset pagination endpoint (`GET /api/trades`), fulfilling the Day 11 verification requirements.

---

## 1. Benchmarking Setup
- **Dataset Size**: 10,000 executed trade records for a single user.
- **Engine**: PostgreSQL 16.
- **Query Type**: Keyset pagination using `(executed_at DESC, id DESC)`.
- **Target Index**: `ix_trades_user_executed_at` on `(user_id, executed_at DESC)`.

---

## 2. Keyset Query with Cursor (Mid-Dataset Page)

### SQL Query
```sql
EXPLAIN (ANALYZE, BUFFERS, COSTS, VERBOSE)
SELECT id, symbol, trade_type, quantity, price, total_value, executed_at
FROM trades
WHERE user_id = :uid
  AND (executed_at < :cursor_ts OR (executed_at = :cursor_ts AND id < :cursor_id))
ORDER BY executed_at DESC, id DESC
LIMIT 21;
```

### Execution Plan Output
```text
Limit  (cost=8.31..8.36 rows=2 width=54) (actual time=1.675..1.680 rows=21 loops=1)
  Output: id, symbol, trade_type, quantity, price, total_value, executed_at
  Buffers: shared hit=115
  ->  Incremental Sort  (cost=8.31..8.36 rows=2 width=54) (actual time=1.672..1.674 rows=21 loops=1)
        Output: id, symbol, trade_type, quantity, price, total_value, executed_at
        Sort Key: trades.executed_at DESC, trades.id DESC
        Presorted Key: trades.executed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 27kB  Peak Memory: 27kB
        Buffers: shared hit=115
        ->  Index Scan using ix_trades_user_executed_at on public.trades  (cost=0.28..8.30 rows=1 width=54) (actual time=1.546..1.559 rows=22 loops=1)
              Output: id, symbol, trade_type, quantity, price, total_value, executed_at
              Index Cond: (trades.user_id = 'dad9a7d2-8ca8-4be3-91aa-474a89885260'::uuid)
              Filter: ((trades.executed_at < '2026-01-04 11:20:00+00'::timestamp with time zone) OR ((trades.executed_at = '2026-01-04 11:20:00+00'::timestamp with time zone) AND (trades.id < '23f2df71-27eb-4d9f-90d6-b4b7ba79dcee'::uuid)))
              Rows Removed by Filter: 5000
              Buffers: shared hit=109
Planning Time: 0.786 ms
Execution Time: 1.755 ms
```

---

## 3. Keyset Query for First Page (No Cursor)

### SQL Query
```sql
EXPLAIN (ANALYZE, BUFFERS, COSTS, VERBOSE)
SELECT id, symbol, trade_type, quantity, price, total_value, executed_at
FROM trades
WHERE user_id = :uid
ORDER BY executed_at DESC, id DESC
LIMIT 21;
```

### Execution Plan Output
```text
Limit  (cost=8.30..8.35 rows=2 width=54) (actual time=0.128..0.131 rows=21 loops=1)
  Output: id, symbol, trade_type, quantity, price, total_value, executed_at
  Buffers: shared hit=3
  ->  Incremental Sort  (cost=8.30..8.35 rows=2 width=54) (actual time=0.127..0.129 rows=21 loops=1)
        Output: id, symbol, trade_type, quantity, price, total_value, executed_at
        Sort Key: trades.executed_at DESC, trades.id DESC
        Presorted Key: trades.executed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 27kB  Peak Memory: 27kB
        Buffers: shared hit=3
        ->  Index Scan using ix_trades_user_executed_at on public.trades  (cost=0.28..8.29 rows=1 width=54) (actual time=0.097..0.102 rows=22 loops=1)
              Output: id, symbol, trade_type, quantity, price, total_value, executed_at
              Index Cond: (trades.user_id = 'dad9a7d2-8ca8-4be3-91aa-474a89885260'::uuid)
              Buffers: shared hit=3
Planning Time: 0.095 ms
Execution Time: 0.179 ms
```

---

## 4. Key Findings & Performance Observations

1. **Index Scan Confirmed**:
   The existing B-tree index `ix_trades_user_executed_at` on `(user_id, executed_at DESC)` is successfully chosen by the PostgreSQL query planner (`Index Scan using ix_trades_user_executed_at`).
2. **Incremental Sort Utilization**:
   Because the index already guarantees that rows arrive presorted by `executed_at DESC`, PostgreSQL performs an ultra-light `Incremental Sort` on the 21-row batch using `id DESC` as a secondary tiebreak. Peak memory consumption is negligible (27 kB), and the entire plan finishes in **0.18 ms** for initial pages and **1.75 ms** for deep pagination across 10,000 records.
3. **No Migration Needed**:
   Since the existing composite index `ix_trades_user_executed_at` satisfies the index scan requirement with sub-2 millisecond latency, no additional migration is required.

