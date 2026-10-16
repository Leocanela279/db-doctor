---
name: db-doctor
description: Database performance auditor. Analyzes raw SQL and ORM data-access code (Eloquent, JPA/Hibernate, Prisma, TypeORM, Sequelize, Supabase and others) to find badly written queries, N+1 patterns, and missing, redundant or unused indexes, then writes a prioritized performance report and an optimization plan with evidence, proposed SQL, risks and validation. Use it whenever the user mentions slow pages, endpoints or queries, timeouts, high database load, N+1, eager/lazy loading, EXPLAIN plans, indexes, or asks to review, audit or optimize SQL, migrations or a data layer — even when they think the bug is in application code and never mention the database.
---

# DB Doctor

Many "slow code" bugs are database problems in disguise: a loop that silently fires 500 queries, a
`WHERE` the engine can't serve with any index, an index every write pays for that no read uses.
This skill examines the data layer like a doctor: gather symptoms, run the tests, diagnose with
evidence, prescribe a treatment plan the team can apply safely, and say how to check it worked.

Five modules, in this order for a full audit (each feeds the next):

| # | Module | Question | Tools (paths relative to this file) |
|---|---|---|---|
| 1 | SQL Analyzer | Can the engine execute each query efficiently and safely? | `scripts/scan_sql.py`, `references/sql-antipatterns.md` |
| 2 | ORM Inspector | Does the code issue more/heavier queries than it looks (N+1, writes in loops, over-fetching)? | `scripts/scan_orm.py`, `references/orm-patterns.md` |
| 3 | Index Advisor & Inspector | Are the right indexes there — and only those? | `scripts/analyze_indexes.py`, `references/index-advisor.md` |
| 4 | Performance Reporter | What actually hurts, how much, in which order to fix it? | `references/report-template.md` |
| 5 | Optimization Plan | What exactly changes, what can go wrong, how do we prove it worked? | `references/report-template.md` |

The scripts are Python 3 standard library only — no install. If the `scripts/` or `references/`
folders aren't available (e.g. the skill was saved as a single file), apply the same checks by
reading the code with search tools; the checklists below are enough to work from.

## Choose the depth

- **Quick check** — the user pastes one query, one ORM snippet or one EXPLAIN plan. Answer in the
  conversation: verdict, why, improved version, whether an index helps (with its good and bad
  consequences), how to verify. No scripts, no report file.
- **Focused audit** — one module or one symptom ("check my indexes", "is there N+1 in this
  controller?", "why is /orders slow?"). Run what that needs, then a short report.
- **Full audit** — "audit the database layer", "the app is slow", a vague performance complaint over a
  codebase. All five modules and the full report.

Start from the user's symptom and widen if the evidence points elsewhere: a slow endpoint often traces
back to a shared model, serializer, scope or a missing index used everywhere.

## Step 0 — Recon (always)

A query that is fine on 1,000 rows is a disaster on 50 million, and index advice depends on the engine,
so establish context before judging anything:

1. **Engine & version** — `.env` (`DB_CONNECTION`), `application.properties` (JDBC URL), `schema.prisma`
   provider, docker-compose images, `package.json` drivers. The scripts auto-detect it; pass `--engine`
   if they can't. PostgreSQL vs MySQL changes FK indexing, online DDL, partial and functional indexes.
2. **Data access** — which ORM(s), raw SQL, query builders, repositories, serializers, templates.
3. **Schema source** — migrations (Laravel, Flyway/Liquibase, Prisma, TypeORM, Sequelize, Supabase),
   entity/model annotations, or a dump (`pg_dump --schema-only`, `mysqldump --no-data`). A dump of the real
   database beats migrations — ask for one when migrations look incomplete or the stack isn't parsed.
4. **Hot paths** — the endpoints/jobs the user complains about; otherwise list pages, dashboards,
   reports, exports, cron jobs, and anything that runs on every request (middleware, auth, policies).
5. **Evidence available** — ask once, briefly, only for what would change conclusions: row counts of the
   main tables, slow query log / `pg_stat_statements` / performance_schema digests / APM traces, EXPLAIN
   output. If the user isn't around or doesn't have them, proceed statically and mark confidence.

If a live database is reachable (a database connector, a client with credentials the user gave you,
Supabase tools), read `references/db-introspection.md` and use its read-only queries. With Supabase,
also run the performance advisor (`get_advisors`, type `performance`).

## Step 1 — SQL Analyzer

```bash
python3 scripts/scan_sql.py <project-root> [--engine postgres|mysql] [--min-severity low] [--json]
```
Covers `.sql` files, SQL strings in code (including SQL assembled by concatenation/interpolation) and
query-builder calls that compile to bad SQL (`whereDate`, `…IgnoreCase`, `…Containing`, Prisma
`contains`/`mode: 'insensitive'`, `inRandomOrder`, OFFSET pagination, COUNT as existence test).

For each query on a hot path, check (details and fixes: `references/sql-antipatterns.md`):
- **Sargability** — functions or casts on columns, leading-wildcard `LIKE`, implicit type/collation
  conversions, `OR` across columns.
- **Reading too much** — `SELECT *`, no `LIMIT`, `COUNT(*)` to test existence, filtering in app code.
- **Pagination** — deep `OFFSET` → keyset.
- **Set traps** — `NOT IN` with NULLs, `DISTINCT` hiding a fan-out join, `UNION` vs `UNION ALL`.
- **Per-row work in SQL** — correlated subqueries in the SELECT list, RLS policies calling functions per row.
- **Writes** — unbounded UPDATE/DELETE, huge single transactions, long-held `FOR UPDATE`.
- **Security in passing** — SQL built from strings is an injection risk *and* defeats plan caching;
  always report it, flagged as security.

## Step 2 — ORM Inspector

```bash
python3 scripts/scan_orm.py <project-root> [--min-severity low] [--json]
```
Finds queries, counts and writes inside loops/callbacks/templates, lazy relation access on loop
variables (Eloquent, JPA getters, TypeORM lazy, Sequelize accessors, Django/SQLAlchemy), `Promise.all`
fan-out, serializers touching relations, EAGER to-many mappings, `open-in-view`, missing lazy-loading
guards, whole-table loads and in-memory filtering. For Blade views it checks whether the controller
action rendering the view eager-loads the relation.

The N+1 shape: one query loads N rows, then code touches a relation or calls the database once per row
→ 1 + N queries (N × M nested). **Verify every candidate before reporting it** — this is where false
positives come from. Trace the collection back to the query that produced it and check whether the
relation is already eager-loaded (`with()`, `JOIN FETCH`, `@EntityGraph`, `include`, `relations`,
embedded select), batch-fetched (`@BatchSize`, `default_batch_fetch_size`) or batched by the ORM (Prisma
batches `findUnique` calls in the same tick). Then count: how big can N be (page size? whole table?),
how many queries does the path really issue? That arithmetic is the evidence.

Also look for writes in loops (→ bulk/upsert), loading whole tables, counting/filtering in memory, and
the opposite problem — eager loading that's too broad (huge relations nobody uses, JOIN-fetching two
collections → cartesian explosion). Per-ORM signals, fixes, fix pitfalls and how to *prove* query
counts (query logs, `preventLazyLoading`, Hibernate statistics, Prisma query events):
`references/orm-patterns.md` — read section 0 and only the sections for the ORMs in the project.

## Step 3 — Index Advisor & Inspector

```bash
python3 scripts/analyze_indexes.py <project-root or schema.sql> [--engine postgres|mysql] [--inventory] [--json]
```
Builds the index inventory from migrations/DDL/`schema.prisma`/JPA/TypeORM/Sequelize (merging the same
index declared in two places) and reports duplicates, left-prefix redundancies, foreign keys without an
index, low-selectivity indexes, wide indexes, and a **hot columns** table: columns the code filters,
joins or sorts on, and whether any index leads with them — the basis for "this table/column is queried
a lot, it needs an index". If it lists tables "referenced in code but not found in the schema", the
schema source is incomplete — say so instead of trusting the missing-index list.

Inspect (rules and reasoning: `references/index-advisor.md`):
- **Redundant** — exact duplicates; `(a)` next to `(a, b)`; a plain index on the columns of a UNIQUE/PK.
- **Unused** — needs runtime stats over a representative period on the primary *and every replica*.
  Never conclude "unused" from code alone; ask for `pg_stat_user_indexes` / `sys.schema_unused_indexes`.
- **Missing** — FK columns on PostgreSQL (never auto-indexed), hot filter/join columns no index leads
  with, sorts that could be served by extending an index. Design composites Equality → Sort → Range.
- **Wrong kind** — `LIKE '%x%'` needs trigram/full-text, `lower(col)` needs an expression index (or
  citext/normalization), JSONB containment needs GIN, skewed status flags want partial indexes.

**Every index recommendation states its consequences in both directions** — the good and the bad —
because an index is a trade, never a free upgrade:
- 👍 seeks instead of scans, faster joins/sorts/`LIMIT`, index-only scans, uniqueness, cheap FK checks,
  less row locking for UPDATE/DELETE.
- 👎 every write maintains it (on PostgreSQL an update to an indexed column also loses HOT updates),
  storage and cache memory, build time and locks on big tables, bloat/vacuum/replication volume, possible
  plan changes elsewhere, and low-selectivity indexes that are paid for but rarely used.
Quantify when you can (table size, write rate). Prefer one well-designed composite over several
single-column indexes.

## Step 4 — Performance Reporter

Turn leads into findings. Each finding gets: an ID (`SQL-1`, `ORM-2`, `IDX-3`…), location
(file:line / query / table), **symptom** and **root cause** with the link made explicit ("the 4 s on
/posts isn't the controller logic: it's 1 + 3 × 20 = 61 queries from lazy-loading `author`,
`comments` and `tags` for 20 posts"), **evidence** with its level — ✅ measured · 🔎 verified in code · ⚠️ suspected —,
impact, exposure (hot / periodic / rare), effort, and a priority:

| Priority | Rule of thumb |
|---|---|
| P0 | High impact on a hot path, measured or verified — likely the reported symptom. |
| P1 | High impact suspected, or medium impact on a hot path. |
| P2 | Medium/low impact or rare paths. |
| P3 | Hygiene — redundant indexes, style, future-proofing. |

Within a priority, order by impact ÷ effort. **Never invent numbers**: if timings or row counts weren't
measured, say so and give the mechanism and the arithmetic instead. Query counts derived from code are
usually possible and are the most convincing evidence a static audit can offer.

## Step 5 — Optimization Plan

For each action (one action may close several findings):
- **Change** — in the project's own idiom (a Laravel app gets `->with('author')` and a migration; a Spring
  app gets `@EntityGraph` and a Flyway file), plus the **proposed SQL**: production-safe DDL
  (`CREATE INDEX CONCURRENTLY` / `ALGORITHM=INPLACE, LOCK=NONE`) and how to wire it into the migration
  tool, or the rewritten query.
- **Expected effect** — in query counts, rows examined, plan shape; measured numbers only if measured.
- **Risks & trade-offs** — locks and build time, write overhead, behavior changes (`NOT IN` → `NOT EXISTS`
  changes NULL semantics — usually fixing a latent bug, but say it), memory of eager loading big relations,
  plan changes for other queries.
- **Validation** — before/after `EXPLAIN (ANALYZE, BUFFERS)` with the same parameters on realistic data,
  query-count assertions in tests, metrics (p95, rows examined, `idx_scan` growing).
- **Rollback** — how to undo (drop concurrently, revert migration, MySQL invisible index first).

Group actions into phases — **Quick wins** (eager loading, missing FK index, dropping obvious duplicates),
**Planned changes** (composite indexes on big tables, query rewrites, keyset pagination), **Structural**
(denormalization, caching, read replicas, partitioning, a search engine) — and give the execution order.

## Writing the report

Follow `references/report-template.md`.
- Write it in the user's language (a Spanish-speaking user gets a Spanish report); keep SQL, code,
  identifiers, rule IDs and paths as they are.
- In a repository, save it as `db-doctor-report.md` at the project root (or where the user asks), and
  reply with a short summary: the top 3 problems and the first action to take. Quick checks stay in chat.
- Don't paste real data rows (they may contain personal data); plans, counts and statistics are enough.
- Mention what wasn't analysed and exactly which data would confirm the ⚠️ findings.

## Safety rules

The audit is read-only. The team applies changes, not the audit.
- Against a user's database run only read-only statements (catalog queries, `EXPLAIN`). Don't run DDL
  or DML unless the user explicitly asks you to apply a specific change.
- `EXPLAIN ANALYZE` executes the statement: wrap writes in `BEGIN; … ROLLBACK;`, avoid heavy statements
  on a production primary (prefer a replica/staging with realistic data), set `statement_timeout` /
  `MAX_EXECUTION_TIME`.
- Never recommend dropping an index on static analysis alone — require usage statistics, and on MySQL 8
  make it INVISIBLE first.
- Propose DDL with its locking behavior spelled out; flag anything that blocks writes on a big table.
