# SQL anti-patterns catalog

Rule IDs match `scripts/scan_sql.py`. For each pattern: why it hurts → how to confirm → fix → caveats.
A pattern is only a problem when the table is big enough or the query hot enough — always tie a
finding to data volume or frequency before ranking it.

## Contents
1. Sargability — functions, casts and conversions on columns
2. Pattern matching — `LIKE '%…%'`, case-insensitive search
3. Reading more than needed — `SELECT *`, unbounded reads, COUNT for existence
4. Pagination — deep OFFSET
5. Set semantics — NOT IN, DISTINCT over joins, UNION, OR across columns, huge IN lists
6. Per-row work inside the database — correlated subqueries, RLS functions, triggers
7. Sorting — `ORDER BY RAND()`, sorts that spill
8. Writes, transactions and locking
9. Safety — SQL built from strings
10. Misc — implicit joins, CTEs, deprecated features

---

## 1. Sargability

A predicate is *sargable* when it has the shape `column <op> value` so an index range scan can seek
into it. Anything that transforms the column per row forces the engine to compute the expression for
every candidate row.

| Non-sargable | Sargable rewrite |
|---|---|
| `WHERE YEAR(created_at) = 2025` / `EXTRACT(YEAR FROM created_at) = 2025` | `WHERE created_at >= '2025-01-01' AND created_at < '2026-01-01'` |
| `WHERE DATE(created_at) = '2025-03-01'` (Laravel `whereDate`) | `WHERE created_at >= '2025-03-01' AND created_at < '2025-03-02'` |
| `WHERE created_at::date = $1` (Postgres cast) | `WHERE created_at >= $1 AND created_at < $1 + interval '1 day'` |
| `WHERE price * 1.21 > 100` | `WHERE price > 100 / 1.21` |
| `WHERE COALESCE(deleted, false) = false` | `WHERE deleted = false OR deleted IS NULL` (or make the column NOT NULL) |
| `WHERE lower(email) = lower(?)` | expression index on `lower(email)`, `citext`, or store the normalized value |

**Rule IDs:** `SQL-FUNC-ON-COLUMN`, `QB-WHERE-DATE`, `QB-CASE-INSENSITIVE`.

**Case-insensitive lookups**
- Postgres: `CREATE INDEX CONCURRENTLY users_lower_email_idx ON users (lower(email));` — the query must use
  exactly `lower(email) = …`. Alternatives: the `citext` type, or normalize on write and compare plainly.
- MySQL: the default `_ci` collations already compare case-insensitively → `lower()` is pointless and
  kills the index; drop it. If needed, MySQL 8.0.13+ supports functional key parts:
  `CREATE INDEX idx ON users ((lower(email)));` (note the double parentheses).
- Spring Data `…IgnoreCase` wraps both sides in `upper()`/`lower()`; Prisma `mode: 'insensitive'` emits
  `ILIKE` on Postgres. Both need the expression/trigram approach or a normalized column.

**Implicit conversions (silent full scans)**
- MySQL comparing a string column to a number (`WHERE phone = 600123123`) converts the *column* for every
  row → no index. Bind the value as a string.
- JOINs between columns with different charset/collation (e.g. `utf8mb3` vs `utf8mb4`, or two different
  collations) can't use the index on one side. Align column definitions.
- Parameter type ≠ column type (e.g. numeric parameter vs integer column, varchar parameter vs a
  different type) can force a cast on the column. Check `EXPLAIN` for a `Filter` with casts.

**Confirm:** `EXPLAIN` shows `Seq Scan`/`type: ALL` with a `Filter:` containing the function, and
`Rows Removed by Filter` (Postgres) or `rows` examined ≫ rows returned.

---

## 2. Pattern matching

- `LIKE 'abc%'` (anchored prefix) can use a B-tree. MySQL: yes. Postgres: only with a `C` collation
  or an index built with `text_pattern_ops` / `varchar_pattern_ops` when the database collation is not `C`.
- `LIKE '%abc%'` / `'%abc'` cannot use a B-tree (rule `SQL-LEADING-WILDCARD`, `QB-CONTAINS`):
  Spring Data `Containing`/`EndingWith`, Prisma `contains`/`endsWith`, TypeORM `Like('%…')`,
  Sequelize `Op.substring`, Supabase `.ilike('col', '%…%')`, Eloquent `where('col', 'like', "%$q%")`.

**Fixes**
- Postgres substring search: trigram index
  ```sql
  CREATE EXTENSION IF NOT EXISTS pg_trgm;
  CREATE INDEX CONCURRENTLY posts_title_trgm_idx ON posts USING gin (title gin_trgm_ops);
  -- serves LIKE / ILIKE '%term%' (useful for terms of 3+ characters)
  ```
- Word search: full-text (`to_tsvector(...) @@ websearch_to_tsquery(...)` + GIN index on the tsvector;
  MySQL `FULLTEXT` + `MATCH … AGAINST`). Semantics differ from LIKE (tokens, stemming, stop words,
  MySQL `innodb_ft_min_token_size`), so confirm with the product owner.
- Dedicated search (Meilisearch, Typesense, Elasticsearch/OpenSearch, Laravel Scout) when search is a
  product feature with ranking/typos.
- Or change the UX to prefix search when acceptable.

**Trade-offs:** trigram GIN indexes are large (often bigger than the column data) and slow writes;
full-text needs an extra column or expression index and changes matching semantics.

---

## 3. Reading more than needed

**`SELECT *` (`SQL-SELECT-STAR`)** — transfers and hydrates every column (including big `text`/`jsonb`
that Postgres would otherwise leave in TOAST), rules out index-only/covering scans, and changes silently
when columns are added. Select only what the caller uses: Eloquent `select([...])`, JPA DTO/interface
projections, Prisma/TypeORM `select`, Supabase `.select('id,title')`, Sequelize `attributes`.
Severity is usually low unless rows are wide or the result is large.

**Unbounded reads** (`ORM-UNBOUNDED-FETCH`) — `Model::all()`, `findAll()`, `findMany()` without `take`.
Fine for lookup tables; dangerous on tables that grow. Paginate, stream (`chunkById`, `lazyById`,
`cursor`, `Stream<T>` + `@QueryHints(fetch size)`), or aggregate in SQL.

**COUNT used as existence** (`SQL-COUNT-FOR-EXISTENCE`, `QB-COUNT-EXISTS`) — counts every match to
answer yes/no. Use `EXISTS (SELECT 1 …)`; Eloquent `exists()`, Spring `existsBy…`, Prisma
`findFirst({ select: { id: true } })`, Django `.exists()`.

**Exact totals on big tables** — Postgres `COUNT(*)` scans the table/index (MVCC has no cached count).
For "about 1.2M results" use `pg_class.reltuples`/`EXPLAIN` estimates, Supabase `count: 'estimated'`,
or skip totals (`simplePaginate`, `Slice<T>`).

**Filtering in application code** (`ORM-IN-MEMORY`) — `->get()->count()`, `findAll().stream().filter`,
`(await findMany()).length`: the database ships every row so the app can discard most of them.

---

## 4. Pagination

`OFFSET n` makes the engine produce and discard `n` rows (`SQL-DEEP-OFFSET`, `QB-OFFSET-PAGINATION`).
Page 1 is fast, page 5,000 is slow, and concurrent inserts make pages shift.

**Keyset (cursor) pagination**
```sql
-- first page
SELECT id, title, created_at FROM posts
WHERE status = 'published'
ORDER BY created_at DESC, id DESC
LIMIT 20;

-- next page: pass the last row's (created_at, id)
SELECT id, title, created_at FROM posts
WHERE status = 'published'
  AND (created_at, id) < (:last_created_at, :last_id)
ORDER BY created_at DESC, id DESC
LIMIT 20;

-- supporting index (equality column first, then the sort keys)
CREATE INDEX CONCURRENTLY posts_status_created_id_idx ON posts (status, created_at DESC, id DESC);
```
MySQL: row-value comparison may not be optimized into a range scan in every version; the expanded form
is safer: `AND (created_at < :c OR (created_at = :c AND id < :id))`.

ORM support: Laravel `cursorPaginate()`; Spring Data `Window<T>` + `ScrollPosition.keyset()` (Spring
Data 3.1+), `Slice<T>` to at least drop the COUNT; Prisma `cursor` + `skip: 1` + `take`;
TypeORM/Sequelize: write the keyset `WHERE` yourself.

**Trade-offs:** no "jump to page N"; the sort must include a unique tie-breaker; cursors must be
encoded/validated. Offset is fine for admin screens and the first few pages.

---

## 5. Set semantics

**`NOT IN (SELECT …)`** (`SQL-NOT-IN-SUBQUERY`) — if the subquery yields any `NULL`, `x NOT IN (…)` is
never true → zero rows (a correctness bug). Postgres also can't plan it as an anti-join (hashed subplan
at best, per-row subplan at worst). Use:
```sql
SELECT o.* FROM orders o
WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id);
```
State the semantic change in the report (it usually fixes a latent bug).

**`DISTINCT` + `JOIN`** (`SQL-DISTINCT-JOIN`) — often hides a 1→N fan-out: the full product is built
and then de-duplicated. If you need parents with at least one matching child, use a semi-join:
`WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.status = 'PAID')`.

**`UNION` vs `UNION ALL`** (`SQL-UNION-DISTINCT`) — `UNION` de-duplicates with a sort/hash. Use
`UNION ALL` when duplicates are impossible or acceptable.

**`OR` across different columns** (`SQL-OR-DIFFERENT-COLUMNS`) — one index can't serve both branches.
Postgres may combine two indexes (BitmapOr), MySQL may use `index_merge`; both are often slower than
two indexed queries combined with `UNION ALL`. Check EXPLAIN before rewriting.

**Huge `IN (…)` lists** — produced by ORM eager loading of large parent sets. Planning cost grows; in
Postgres each element is a bind parameter and the protocol caps a statement at 65,535 parameters
(`= ANY($1::bigint[])` passes one array instead). MySQL switches to statistics-based estimates past
`eq_range_index_dive_limit` (default 200). Chunk the parent set.

---

## 6. Per-row work inside the database

**Correlated subquery in the SELECT list** (`SQL-CORRELATED-SUBQUERY`) — the "N+1 inside SQL":
```sql
-- before: one COUNT per customer row
SELECT c.id, (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS orders_count FROM customers c;

-- after: aggregate once, join
SELECT c.id, COALESCE(x.orders_count, 0) AS orders_count
FROM customers c
LEFT JOIN (SELECT customer_id, COUNT(*) AS orders_count FROM orders GROUP BY customer_id) x
       ON x.customer_id = c.id;
```
For "top-N per parent" use `LATERAL` (Postgres) / `LATERAL` derived tables (MySQL 8.0.14+) or window
functions. Make sure the correlated column (`orders.customer_id`) is indexed either way.

**Row-level security functions** (`SUPA-RLS-PER-ROW`) — in Supabase/Postgres RLS, `auth.uid()` in a
policy can be evaluated for every row. Wrap it so Postgres evaluates it once as an initPlan:
```sql
create policy "own rows" on comments for select to authenticated
  using ((select auth.uid()) = user_id);
create index comments_user_id_idx on comments (user_id);  -- big live table: CONCURRENTLY, outside a transaction
```
Also: index every column a policy filters on, add the same filter explicitly in the client query
(`.eq('user_id', userId)`) so the planner can use it, avoid joins inside policies (use
`column IN (select …)` or a `security definer` function), and scope policies with `to authenticated`.

**Triggers and scalar functions** — row-level triggers multiply the cost of bulk writes; user functions
called in `WHERE`/`SELECT` run per row and block index use. Check `pg_trigger` / `SHOW TRIGGERS`.

---

## 7. Sorting

**`ORDER BY RAND()/RANDOM()`** (`SQL-ORDER-RANDOM`, `QB-RANDOM-ORDER`) — random key per row + full
sort. Alternatives: choose random ids in the app (accepting gaps), `TABLESAMPLE SYSTEM (p)` /
`BERNOULLI (p)` in Postgres, or a precomputed/cached random sample.

**Sorts without a supporting index** — Postgres `Sort Method: external merge Disk: …` or MySQL
`Using filesort` on large sets. An index matching `ORDER BY` *after the equality columns* lets the engine
read rows in order and stop at `LIMIT` (`IDX-SORT-NOT-COVERED`). Mixed directions
(`ORDER BY a, b DESC`) need an index with the same directions (or exactly reversed).

---

## 8. Writes, transactions and locking

**Unbounded `UPDATE`/`DELETE`** (`SQL-UNBOUNDED-WRITE`) — one huge transaction: locks, WAL/binlog
volume, replica lag, long rollback. Batch it:
```sql
-- Postgres: repeat until 0 rows
WITH batch AS (
  SELECT id FROM products WHERE active AND discontinued_at IS NOT NULL
  ORDER BY id LIMIT 5000 FOR UPDATE SKIP LOCKED
)
UPDATE products p SET active = false FROM batch WHERE p.id = batch.id;

-- MySQL: repeat until 0 rows affected
UPDATE products SET active = 0 WHERE active = 1 AND discontinued_at IS NOT NULL LIMIT 5000;
```
Laravel: `Model::where(...)->chunkById(1000, fn ($rows) => Model::whereIn('id', $rows->pluck('id'))->update([...]))`.

**Writes in loops** (`ORM-WRITE-IN-LOOP`) — one round-trip per row. Use multi-row `INSERT`, upserts
(`INSERT … ON CONFLICT … DO UPDATE` / `INSERT … ON DUPLICATE KEY UPDATE`), set-based `UPDATE … WHERE id
IN (…)`, ORM bulk APIs, and one transaction.

**InnoDB locks what it scans** — an `UPDATE`/`DELETE` whose `WHERE` can't use an index locks every row
it examines (next-key locks under REPEATABLE READ), effectively locking the table. An index on the
`WHERE` columns reduces locking as well as time.

**Long transactions** — hold row locks; in Postgres they also hold back VACUUM (bloat); in MySQL they
grow the undo/history list. Never keep a transaction open across HTTP calls or user think-time.

**Locking reads** (`SQL-LOCKING-READ`) — `FOR UPDATE` holds locks until commit; keep it short. Job
queues: `FOR UPDATE SKIP LOCKED`.

**DDL lock queues** — a migration waiting for an exclusive lock blocks every query queued behind it.
Use `SET lock_timeout = '5s'` (Postgres) / `lock_wait_timeout` (MySQL) in migrations and retry.

---

## 9. Safety — SQL built from strings

`SQL-STRING-BUILT`: concatenation/interpolation of values into SQL (`"… WHERE status = '" + status + "'"`,
`whereRaw("… $q …")`, `$queryRawUnsafe(\`… ${q}\`)`, f-strings, `fmt.Sprintf`).
- **Security:** SQL injection. Always report it, even in a performance audit (flag it as security).
- **Performance:** every distinct value is a new statement text → no plan reuse, prepared-statement cache
  churn, and `pg_stat_statements`/digests can't aggregate it.
- **Fix:** bound parameters everywhere (`?`, `:name`, `$1`, Eloquent bindings
  `whereRaw('lower(body) like ?', ["%{$q}%"])`, JPA `setParameter`, Prisma tagged `$queryRaw\`…${q}\``).
  Dynamic identifiers (sort column, direction) can't be bound: map them through an allow-list.

---

## 10. Misc

- **Comma joins** (`SQL-IMPLICIT-JOIN`): `FROM a, b` without a join predicate is a cartesian product.
  Use explicit `JOIN … ON`.
- **CTEs:** Postgres ≤ 11 always materialized CTEs (optimization fence); 12+ inlines them unless
  `MATERIALIZED` or referenced more than once. On old versions, move filters inside the CTE.
- **`HAVING` for non-aggregate filters:** filter in `WHERE` so rows are discarded before grouping.
- **`SQL_CALC_FOUND_ROWS` / `FOUND_ROWS()`** (`SQL-CALC-FOUND-ROWS`): deprecated since MySQL 8.0.17; run a
  separate `COUNT(*)` or drop the total.
- **N+1 seen from the database:** a cheap statement with a huge `calls` count in `pg_stat_statements`
  (or `count_star` in MySQL digests), far above the request count of the endpoint that issues it.
