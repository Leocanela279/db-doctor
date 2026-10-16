# Index advisor & inspector

How to decide which indexes a schema needs, which ones it should lose, and how to change them safely.
Rule IDs match `scripts/analyze_indexes.py`.

## Contents
1. Mental model — what a B-tree index can and can't do
2. Designing composite, covering, partial and expression indexes
3. Index types beyond B-tree
4. Trade-offs: the good and the bad (state them in every recommendation)
5. Duplicate, redundant and unused indexes
6. Foreign keys
7. Safe rollout in production (Postgres, MySQL, migration tools)
8. Validating that an index works
9. Rule ID reference

---

## 1. Mental model

A B-tree index is a sorted copy of the key columns, left to right — like a phone book sorted by
(last name, first name) — with pointers to the rows.

It can serve:
- equality and range predicates on the **leading** column(s): `(a, b)` serves `a = ?`, `a = ? AND b > ?`,
  `a IN (…)`, but **not** `b = ?` alone (MySQL 8.0.13+ and PostgreSQL 18+ can *skip-scan* when the
  leading column has very few distinct values — don't design around it);
- `ORDER BY` that matches the index order after the equality columns → no sort, early stop at `LIMIT`;
- `MIN/MAX`, `GROUP BY` on the prefix;
- **index-only / covering scans** when every column the query needs is in the index (Postgres also needs
  the visibility map to be current → VACUUM; MySQL shows `Using index`).

The planner uses it only when it expects it to be cheaper than reading the table: if a predicate
matches a large fraction of rows (typically more than ~5–20 %, depends on row width and cache), a
sequential scan wins. That's why an index on a boolean is rarely used. Plans depend on statistics —
stale stats (`ANALYZE` not run after big loads) produce bad plans.

---

## 2. Designing indexes

**Composite order — Equality → Sort → Range (ESR)**
1. Columns compared with `=` / `IN` first (any order among them; put first the one other queries also use alone).
2. Then the `ORDER BY` columns, in the same order/direction.
3. Then at most one range column (`>`, `<`, `BETWEEN`, `LIKE 'x%'`) — columns after a range can't be used for seeking.

Example: `WHERE customer_id = ? AND status = ? ORDER BY created_at DESC LIMIT 20`
→ `CREATE INDEX … ON orders (customer_id, status, created_at DESC);`

**One composite beats several single-column indexes.** Two separate indexes `(a)` and `(b)` force a
bitmap AND / index merge, usually slower than one `(a, b)`.

**Covering:** Postgres 11+ `INCLUDE (col, …)` adds payload columns without changing the key order:
`CREATE INDEX … ON orders (customer_id, created_at) INCLUDE (total);`. MySQL: add the column to the
key (InnoDB secondary indexes already contain the primary key).

**Partial indexes** (Postgres, SQLite): index only the rows queries care about.
```sql
CREATE INDEX CONCURRENTLY posts_live_created_idx ON posts (created_at) WHERE deleted_at IS NULL;
CREATE INDEX CONCURRENTLY jobs_pending_idx ON jobs (run_at) WHERE status = 'pending';
```
Smaller and cheaper to maintain. The query's WHERE must *imply* the index predicate — write the
predicate value as a literal; a parameter (`status = $1`) may not match under generic plans.
MySQL has no partial indexes (use a composite or a generated column).

**Expression indexes:** `(lower(email))`, `((payload->>'type'))`, `(date_trunc('day', created_at))`.
The query must use the *identical* expression. MySQL 8.0.13+: functional key parts `((lower(email)))`.

**Descending / mixed order:** `ORDER BY created_at DESC, id DESC` is served by `(created_at, id)`
scanned backwards; mixed directions (`ORDER BY a ASC, b DESC`) need `(a ASC, b DESC)`.

**Primary keys:** InnoDB clusters the table by PK and copies it into every secondary index — keep it
small and sequential. Random UUIDv4 keys cause page splits and poor cache locality (in Postgres too);
prefer bigint identities or time-ordered UUIDv7/ULID.

**Don't index everything.** Index the *access paths of important queries*, verified with EXPLAIN.

---

## 3. Index types beyond B-tree

| Type | Use for | Notes |
|---|---|---|
| B-tree (default) | `=`, ranges, sorting, prefix `LIKE 'x%'` | Postgres non-C collation needs `text_pattern_ops` for `LIKE 'x%'` |
| Hash (Postgres) | equality only | rarely better than B-tree |
| GIN (Postgres) | `jsonb` containment `@>`, arrays, full-text `tsvector`, trigram (`pg_trgm`) | big, slower writes (pending list) |
| GiST (Postgres) | geometry, ranges, exclusion constraints, trigram | faster updates than GIN, slower lookups |
| BRIN (Postgres) | huge append-only tables where the value follows physical order (event/log timestamps) | tiny, lossy; useless if rows are updated/shuffled |
| FULLTEXT (MySQL) | word search with `MATCH … AGAINST` | tokenization, stop words, `innodb_ft_min_token_size` |
| Prefix index (MySQL) | long strings: `INDEX (url(40))` | can't cover, can't serve ORDER BY |
| SPATIAL | geometry | |

---

## 4. Trade-offs: the good and the bad

An index is a trade, never a free upgrade. Every recommendation in the report states both sides,
concretely for that table (size, read/write mix) when known.

**Good**
- Turns scans of millions of rows into a few page reads for selective predicates.
- Speeds up JOINs (index on the inner side's join column), `ORDER BY … LIMIT` (no sort), `GROUP BY`, `MIN/MAX`.
- Enables index-only / covering scans (no table access).
- Enforces integrity (`UNIQUE`) and makes FK checks cheap (deleting a parent doesn't scan the child).
- Reduces locking: InnoDB locks every row an `UPDATE`/`DELETE` *examines*, so an index on the `WHERE`
  columns shrinks the locked set; in Postgres it shortens the statement and therefore lock time.

**Bad**
- **Write amplification:** every INSERT and DELETE updates every index; UPDATEs update indexes on changed
  columns — and in Postgres changing any indexed column disables HOT updates, so *all* indexes of the row
  get new entries. Write-heavy tables feel each extra index.
- **Storage and memory:** indexes compete with table data for `shared_buffers` / `innodb_buffer_pool`;
  when the working set stops fitting in RAM everything slows down. Rough size: rows × (key bytes + ~16–20
  bytes overhead); measure on staging (`pg_relation_size`, `index_length`).
- **Build cost and locking:** plain `CREATE INDEX` blocks writes in Postgres for the whole build; online
  variants take longer and can fail half-way (see §7).
- **Maintenance:** bloat and VACUUM work (Postgres), bigger WAL/binlog, replica lag during builds, bigger
  backups.
- **Planner side effects:** a new index can change plans of *other* queries (occasionally for the worse);
  low-selectivity indexes are rarely chosen but always maintained.
- **UNIQUE changes behavior:** inserts that used to succeed will fail; creating it fails if duplicates exist.

Template sentence for the report: "Speeds up X (seek instead of scanning ~N rows) at the cost of one
more index maintained on every write to `table` (~W writes/s if known) and ~S MB of storage/cache."

---

## 5. Duplicate, redundant and unused indexes

**Duplicate** (`IDX-DUPLICATE`): same table, same columns in the same order, same predicate, same type.
Keep the one backing a constraint (PK/UNIQUE); drop the plain one. A non-unique index on exactly the
columns of a UNIQUE/PK index is redundant.

**Left-prefix redundant** (`IDX-REDUNDANT-PREFIX`): `(a)` is redundant with `(a, b)` for B-trees with the
same predicate. Exceptions — keep `(a)` when:
- it is UNIQUE/PK (it enforces a constraint the wider one doesn't);
- the wider index is much wider (many/large columns) and `(a)` serves very hot scans — measure;
- it uses a different operator class/collation (e.g. `text_pattern_ops`) or is partial vs full;
- MySQL: dropping an index used by a FK is only allowed if another index can serve the FK (a composite
  led by the FK column can).

MySQL/InnoDB: `(a, id)` where `id` is the PK is equivalent to `(a)` (the PK is implicitly appended).

**Unused** — never conclude from code alone. Requires runtime statistics:
- Postgres: `pg_stat_user_indexes.idx_scan = 0` since `pg_stat_database.stats_reset` (PG16+ also
  `last_idx_scan`). Statistics are per instance → check the primary **and every replica** (read traffic
  often hits replicas only). Exclude indexes that back UNIQUE/PK/exclusion constraints (they work even
  with 0 scans). Observe a full business cycle (month-end jobs, yearly reports).
- MySQL: `sys.schema_unused_indexes` (since server restart), `sys.schema_redundant_indexes`.
- Supabase: Performance Advisor `unused_index` / `duplicate_index`.

**Safe removal path**
- MySQL 8.0+: `ALTER TABLE t ALTER INDEX i INVISIBLE;` → watch latency/plans for days → `DROP INDEX`.
  Revert instantly with `VISIBLE`. MariaDB 10.6+: `IGNORED`.
- Postgres has no invisible indexes: HypoPG 1.4+ can hide an index from `EXPLAIN` in your session
  (`hypopg_hide_index`) to preview plans; keep the exact `CREATE INDEX CONCURRENTLY` statement as the
  rollback; drop with `DROP INDEX CONCURRENTLY`.

---

## 6. Foreign keys

- **Postgres does not index FK columns automatically** (only the referenced PK/UNIQUE side is indexed).
  Neither do Laravel `constrained()`, Prisma, Hibernate, TypeORM or Sequelize on Postgres.
- **MySQL/InnoDB creates an index** for a FK if no usable one exists (and may silently drop it later when
  another index that can serve the FK is created).

Why an unindexed FK hurts (`IDX-FK-NO-INDEX`):
- loading children of a parent (`WHERE post_id = ?`, ORM relation loading `WHERE post_id IN (…)`) scans;
- JOINs parent → child scan the child per parent or hash the whole table;
- every `DELETE`/key `UPDATE` on the parent checks the child table (`ON DELETE CASCADE/RESTRICT`) — deleting
  one user can scan `comments` once per user and hold locks meanwhile.

The supporting index must **lead** with the FK column(s). Skipping it is defensible only for tiny child
tables where the parent is never deleted and the relation is never navigated from the parent side.

---

## 7. Safe rollout in production

**Postgres**
```sql
SET lock_timeout = '5s';     -- don't sit in the lock queue blocking everyone
CREATE INDEX CONCURRENTLY IF NOT EXISTS comments_post_id_idx ON comments (post_id);
-- CONCURRENTLY: no write lock, but two table passes, waits for running transactions,
-- cannot run inside a transaction block. If it fails it leaves an INVALID index:
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;   -- then DROP INDEX CONCURRENTLY … and retry
DROP INDEX CONCURRENTLY IF EXISTS comments_user_idx;
REINDEX INDEX CONCURRENTLY posts_title_idx;   -- PG12+, rebuild a bloated index
```
`CREATE UNIQUE INDEX CONCURRENTLY` fails if duplicates exist — find them first.

**MySQL 8 (InnoDB online DDL)**
```sql
ALTER TABLE comments ADD INDEX comments_post_id_idx (post_id), ALGORITHM=INPLACE, LOCK=NONE;
-- fails immediately if it can't be done online, instead of silently locking the table
ALTER TABLE comments ALTER INDEX comments_user_idx INVISIBLE;
ALTER TABLE comments DROP INDEX comments_user_idx, ALGORITHM=INPLACE, LOCK=NONE;
```
Online DDL still takes a brief metadata lock at start and end: a long-running transaction blocks it, and
then it blocks everyone behind it → check `information_schema.innodb_trx`, set `lock_wait_timeout`.
Very large/busy tables: gh-ost or pt-online-schema-change. Replicas apply the DDL afterwards (lag).

**Migration tools**
- **Laravel + Postgres:** migrations run inside a transaction; for CONCURRENTLY set
  `public $withinTransaction = false;` on the migration and use
  `DB::statement('CREATE INDEX CONCURRENTLY …')`. On MySQL `$table->index(...)` is an online `ALTER` in
  most cases; use `DB::statement` with `ALGORITHM=INPLACE, LOCK=NONE` to make it explicit.
- **Flyway + Postgres:** put the `CONCURRENTLY` statement alone in its own migration file (it can't run in
  a transaction; mixing it with transactional statements needs Flyway's `mixed` setting).
  Liquibase: `runInTransaction="false"` on the changeSet.
- **Prisma Migrate:** `prisma migrate dev --create-only`, edit `migration.sql`. If your pipeline can't run
  CONCURRENTLY, create the index manually with the exact name Prisma expects, then add the migration and
  mark it applied with `prisma migrate resolve --applied <migration>`; keep `@@index` in `schema.prisma`.
- **TypeORM:** run migrations with `--transaction none`/`each` or disable the transaction for that
  migration; **Sequelize:** `queryInterface.addIndex(table, cols, { concurrently: true })` (Postgres).
- **Django:** `AddIndexConcurrently` with `atomic = False`; **Rails:** `algorithm: :concurrently` +
  `disable_ddl_transaction!`.

Run index changes off-peak, one table at a time, and watch replication lag and lock waits.

---

## 8. Validating that an index works

1. **Before:** capture `EXPLAIN (ANALYZE, BUFFERS)` (MySQL: `EXPLAIN ANALYZE` / `FORMAT=TREE`) of the target
   query with realistic parameters on production-like data volume, plus its mean time from
   `pg_stat_statements`/digests or APM p95.
2. **Preview (Postgres):** HypoPG — `SELECT * FROM hypopg_create_index('CREATE INDEX ON comments (post_id)');`
   then plain `EXPLAIN` (not ANALYZE) shows whether the planner would use it. Supabase: `index_advisor`.
3. **After:** same EXPLAIN shows the index (`Index Scan`/`Index Only Scan`, MySQL `key = …`, rows examined
   down), latency improved, `pg_stat_user_indexes.idx_scan` increasing, write latency of the table not
   degraded, index size as expected.
4. **Small dev databases lie:** with a few hundred rows the planner prefers sequential scans; validate on
   realistic volumes (or at least use `SET enable_seqscan = off` in a session *only* to check usability).

---

## 9. Rule ID reference

| Rule | Meaning | Typical action |
|---|---|---|
| IDX-DUPLICATE | same columns as another index / a UNIQUE/PK | drop the redundant one |
| IDX-REDUNDANT-PREFIX | left prefix of a wider index | drop after usage check |
| IDX-FK-NO-INDEX | FK without supporting index (Postgres/SQLite) | create index led by the FK |
| IDX-FK-AUTO | MySQL auto-indexes FKs (info) | none |
| IDX-LOW-SELECTIVITY | single-column index on boolean/enum/status | partial or composite instead |
| IDX-MISSING-FOR-QUERY | code filters/joins on columns no index leads with | create composite (ESR), validate with EXPLAIN |
| IDX-SORT-NOT-COVERED | filter indexed but sort not → sort every call | extend index with the sort column |
| IDX-PARTIAL-CANDIDATE | `IS NULL` filter (soft deletes) + sort | partial index |
| IDX-WIDE | > 5 key columns | trim / use INCLUDE |
| IDX-MANY-INDEXES | ≥ 7 secondary indexes on a table | review usage, consolidate |
