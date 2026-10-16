# Live database introspection (read-only) and EXPLAIN reading

Use only when a database is reachable (DB connector/MCP, a client with credentials the user provided,
Supabase tools). Every statement here is read-only; still run them on a replica or staging when possible.
Never paste real row data into the report — plans, counts and statistics are enough evidence.

## Contents
1. Session safety
2. PostgreSQL catalog queries
3. MySQL 8 catalog queries
4. Supabase specifics
5. Reading EXPLAIN — PostgreSQL
6. Reading EXPLAIN — MySQL
7. Capturing slow queries over time

---

## 1. Session safety

```sql
-- PostgreSQL
SET statement_timeout = '30s';
SET lock_timeout = '2s';
SET default_transaction_read_only = on;     -- this session only

-- MySQL
SET SESSION MAX_EXECUTION_TIME = 30000;     -- SELECT statements, ms
SET SESSION TRANSACTION READ ONLY;
```
`EXPLAIN ANALYZE` **executes** the statement. For INSERT/UPDATE/DELETE:
`BEGIN; EXPLAIN (ANALYZE, BUFFERS) UPDATE …; ROLLBACK;` — and avoid heavy statements on a busy primary.

---

## 2. PostgreSQL

**Version and key settings**
```sql
SELECT version();
SELECT name, setting, unit FROM pg_settings
WHERE name IN ('shared_buffers','work_mem','effective_cache_size','random_page_cost','max_connections','default_statistics_target');
```

**Biggest tables (rows are estimates)**
```sql
SELECT relname AS table, n_live_tup AS rows_est,
       pg_size_pretty(pg_total_relation_size(relid)) AS total,
       pg_size_pretty(pg_relation_size(relid))       AS heap,
       pg_size_pretty(pg_indexes_size(relid))        AS indexes
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 30;
```

**Top statements** (extension `pg_stat_statements`; PG13+ column names, ≤12 use `total_time`/`mean_time`)
```sql
SELECT queryid, calls,
       round(total_exec_time::numeric, 1) AS total_ms,
       round(mean_exec_time::numeric, 2)  AS mean_ms,
       rows,
       round((100 * total_exec_time / sum(total_exec_time) OVER ())::numeric, 1) AS pct_of_total,
       shared_blks_hit, shared_blks_read,
       left(query, 200) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
-- N+1 fingerprint: ORDER BY calls DESC → a cheap query with calls far above request volume.
```

**Tables read by sequential scans**
```sql
SELECT relname, seq_scan, seq_tup_read, idx_scan, n_live_tup,
       round(seq_tup_read::numeric / NULLIF(seq_scan, 0)) AS avg_rows_per_seq_scan
FROM pg_stat_user_tables
WHERE seq_scan > 0
ORDER BY seq_tup_read DESC
LIMIT 20;
```

**Indexes never scanned** (since `stats_reset`; check every replica too)
```sql
SELECT s.relname AS table, s.indexrelname AS index, s.idx_scan,
       pg_size_pretty(pg_relation_size(s.indexrelid)) AS size
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.idx_scan = 0 AND NOT i.indisunique AND NOT i.indisprimary
ORDER BY pg_relation_size(s.indexrelid) DESC;

SELECT stats_reset FROM pg_stat_database WHERE datname = current_database();
```

**Duplicate indexes** (same table, columns, opclasses, expressions and predicate)
```sql
SELECT pg_size_pretty(sum(pg_relation_size(idx))::bigint) AS size,
       (array_agg(idx))[1] AS idx1, (array_agg(idx))[2] AS idx2,
       (array_agg(idx))[3] AS idx3, (array_agg(idx))[4] AS idx4
FROM (SELECT indexrelid::regclass AS idx,
             (indrelid::text || E'\n' || indclass::text || E'\n' || indkey::text || E'\n' ||
              coalesce(indexprs::text, '') || E'\n' || coalesce(indpred::text, '')) AS key
      FROM pg_index) sub
GROUP BY key HAVING count(*) > 1
ORDER BY sum(pg_relation_size(idx)) DESC;
```

**Foreign keys without a supporting index**
```sql
SELECT c.conrelid::regclass AS table, c.conname AS fk, pg_get_constraintdef(c.oid) AS definition
FROM pg_constraint c
WHERE c.contype = 'f'
  AND NOT EXISTS (
    SELECT 1 FROM pg_index i
    WHERE i.indrelid = c.conrelid
      AND (i.indkey::int2[])[0:array_length(c.conkey, 1) - 1] @> c.conkey
  )
ORDER BY 1;
```

**Index definitions of a table**: `SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'comments';`

**Column statistics (selectivity)**
```sql
SELECT attname, n_distinct, most_common_vals, most_common_freqs, correlation
FROM pg_stats WHERE tablename = 'orders' AND attname IN ('status', 'customer_id', 'created_at');
```
`n_distinct` negative = fraction of rows (−1 = unique). `correlation` near ±1 = physical order follows the
column (BRIN candidate).

**Maintenance health**
```sql
SELECT relname, n_dead_tup, n_live_tup, last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 20;

SELECT sum(heap_blks_hit)::float / NULLIF(sum(heap_blks_hit) + sum(heap_blks_read), 0) AS table_cache_hit_ratio
FROM pg_statio_user_tables;   -- OLTP usually > 0.99
```

**What is running / blocked now**
```sql
SELECT pid, now() - query_start AS duration, state, wait_event_type, wait_event, left(query, 150) AS query
FROM pg_stat_activity WHERE state <> 'idle' AND pid <> pg_backend_pid()
ORDER BY duration DESC;

SELECT pid, pg_blocking_pids(pid) AS blocked_by, left(query, 120) AS query
FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;
```

---

## 3. MySQL 8

**Version and buffer pool**
```sql
SELECT VERSION();
SHOW VARIABLES LIKE 'innodb_buffer_pool_size';
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_read%';   -- reads (disk) vs read_requests (logical)
```

**Biggest tables**
```sql
SELECT table_name, table_rows AS rows_est,
       ROUND(data_length / 1024 / 1024, 1)  AS data_mb,
       ROUND(index_length / 1024 / 1024, 1) AS index_mb
FROM information_schema.tables
WHERE table_schema = DATABASE()
ORDER BY data_length + index_length DESC
LIMIT 30;
```

**Top statements** (performance_schema; timers are picoseconds)
```sql
SELECT digest_text, count_star AS calls,
       ROUND(sum_timer_wait / 1e12, 2) AS total_s,
       ROUND(avg_timer_wait / 1e9, 2)  AS avg_ms,
       sum_rows_examined, sum_rows_sent, sum_no_index_used, sum_created_tmp_disk_tables
FROM performance_schema.events_statements_summary_by_digest
WHERE schema_name = DATABASE()
ORDER BY sum_timer_wait DESC
LIMIT 20;
```
`sys` shortcuts: `sys.statement_analysis`, `sys.statements_with_full_table_scans`,
`sys.statements_with_sorting`, `sys.statements_with_temp_tables`.
Red flag: `sum_rows_examined / sum_rows_sent` ≫ 100.

**Index health**
```sql
SELECT * FROM sys.schema_unused_indexes    WHERE object_schema = DATABASE();  -- since restart
SELECT * FROM sys.schema_redundant_indexes WHERE table_schema = DATABASE();   -- includes sql_drop_index
SELECT * FROM sys.schema_index_statistics  WHERE table_schema = DATABASE() ORDER BY rows_selected DESC;
SHOW INDEX FROM orders;    -- Cardinality = estimated distinct values
```

**Locks and long transactions**
```sql
SELECT * FROM sys.innodb_lock_waits;
SELECT trx_id, trx_started, trx_rows_locked, trx_query FROM information_schema.innodb_trx ORDER BY trx_started;
SHOW FULL PROCESSLIST;
```

---

## 4. Supabase

- Performance Advisor / connector `get_advisors` (type `performance`): unindexed foreign keys,
  duplicate and unused indexes, `auth_rls_initplan` (RLS calling `auth.uid()` per row), multiple
  permissive policies. Also run type `security` if the user cares — but keep it separate from performance.
- `pg_stat_statements` is enabled (dashboard "Query Performance"); the queries in §2 work through the SQL
  editor or a read-only `execute_sql`.
- `index_advisor` extension: `select * from index_advisor('select * from posts where author_id = 1');`
- CLI: `supabase inspect db outliers | calls | seq-scans | unused-indexes | index-usage | bloat | blocking | long-running-queries | table-sizes`.
- PostgREST requests appear as many short statements: N+1 from supabase-js looks like a high `calls`
  count of the same `SELECT … WHERE post_id = $1`.

---

## 5. Reading EXPLAIN — PostgreSQL

```sql
EXPLAIN (ANALYZE, BUFFERS, VERBOSE) SELECT …;   -- executes the query
EXPLAIN SELECT …;                               -- plan only, safe for anything
```

Read bottom-up/innermost-first; for each node compare **estimated rows** vs **actual rows × loops**.

| Signal | Meaning | Usual fix |
|---|---|---|
| `Seq Scan` on a big table, `Rows Removed by Filter` ≫ returned rows | no usable index for the filter | index (check sargability first) |
| Estimated vs actual rows off by 10×+ | bad statistics / correlated columns | `ANALYZE t`; raise statistics target; `CREATE STATISTICS` (dependencies/ndistinct) |
| `Nested Loop` with a large `loops=` and an inner Seq Scan | missing index on the join column | index the inner join column (often an FK) |
| `Sort Method: external merge  Disk: …` | sort spilled to disk | index matching ORDER BY, or more `work_mem` for that query |
| `Hash … Batches: 2+` | hash join spilled | `work_mem`, or reduce rows earlier |
| `Index Only Scan` with high `Heap Fetches` | visibility map stale | VACUUM (autovacuum tuning) |
| `Bitmap Heap Scan` with `lossy` blocks / big `Recheck` | bitmap exceeded `work_mem` | `work_mem` or more selective index |
| `Buffers: shared read` high vs `hit` | cold cache / data bigger than RAM | fewer pages: better index, covering, partial |
| Large `Planning Time` | huge IN lists, many partitions/indexes | arrays instead of IN, prune |
| `SubPlan` executed per row | correlated subquery / NOT IN | rewrite as JOIN / NOT EXISTS |
| JIT time dominates a short query | JIT not worth it | `SET jit = off` for that workload |

Visualizers (explain.dalibo.com, explain.depesz.com) help; don't upload plans with sensitive literals.

---

## 6. Reading EXPLAIN — MySQL

```sql
EXPLAIN FORMAT=TREE SELECT …;   -- 8.0.16+, readable tree
EXPLAIN ANALYZE SELECT …;       -- 8.0.18+, executes the query, actual times/rows
EXPLAIN SELECT …; SHOW WARNINGS;  -- classic table + rewritten query
```

`type` from best to worst: `system` / `const` → `eq_ref` → `ref` → `range` → `index` (full index scan) →
`ALL` (full table scan). `key` = chosen index (`NULL` = none). `rows` × `filtered` ≈ rows passed on.

`Extra` flags: `Using index` (covering, good) · `Using index condition` (ICP, ok) · `Using where` ·
`Using filesort` (sort not served by an index) · `Using temporary` (temp table, often GROUP BY/DISTINCT) ·
`Using join buffer (hash join)` (no index on the join column).

---

## 7. Capturing slow queries over time

- Postgres: `log_min_duration_statement = 500` (ms) for slow statements; `auto_explain`
  (`auto_explain.log_min_duration`, `log_analyze` adds overhead) to log plans of slow queries;
  `pg_stat_statements` for aggregates; pgBadger to summarize logs.
- MySQL: `slow_query_log = ON`, `long_query_time = 0.5`; summarize with `pt-query-digest`.
- Application: APM traces (Sentry, Datadog, New Relic, OpenTelemetry) show query counts per request — the
  most direct evidence of N+1.
