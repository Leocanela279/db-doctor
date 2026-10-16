# Report template, scoring and writing rules

## Writing rules
- **Language:** write the report in the language the user writes in (Spanish user → Spanish report).
  Keep SQL, code, identifiers, rule IDs and file paths untranslated. Translate headings too
  (Spanish: Resumen ejecutivo · Informe de rendimiento · Hallazgos · Plan de optimización · Evidencias ·
  SQL propuesta · Riesgos · Validación · Rollback · Limitaciones).
- **Evidence over adjectives.** Every finding cites where (file:line, query, table) and how it was
  established. Never invent timings or row counts: if not measured, say so and give the mechanism and
  the arithmetic ("50 posts per page → 1 + 50 + 50 = 101 queries").
- **Symptom → root cause.** Connect what the user sees ("/posts takes 4 s") to what the database does.
  This is the point of the report: often the "code bug" is a data-access problem.
- **Fixes in the project's own idiom.** A Laravel project gets `->with()` and a migration with
  `$withinTransaction = false`; a Spring project gets `@EntityGraph` and a Flyway file. Raw SQL alone is
  not enough when an ORM owns the schema.
- **Every index recommendation states its good and bad consequences** (references/index-advisor.md §4).
- Don't include real row data (personal data); plans and statistics suffice.

## Evidence levels
| Mark | Level | Examples |
|---|---|---|
| ✅ | Measured | EXPLAIN ANALYZE, query log/count, pg_stat_statements, APM trace, benchmark |
| 🔎 | Verified in code | traced the code path: collection loaded at X without eager load, loop at Y touches relation Z |
| ⚠️ | Suspected | heuristic hit, or impact depends on unknown data volume/frequency |

## Scoring
**Impact** — High: dominates request time / causes timeouts / scales with data (N+1 on list pages,
full scans of growing tables, unindexed FK on hot joins). Medium: noticeable but bounded. Low: hygiene.

**Exposure** — Hot (every request of a main page/API, auth, middleware) · Periodic (cron, jobs, reports)
· Rare (admin, one-off).

| Priority | Rule of thumb |
|---|---|
| **P0** | High impact on a hot path, measured or verified — likely causing the reported symptom. Fix now. |
| **P1** | High impact suspected, or medium impact on a hot path. |
| **P2** | Medium/low impact or rare paths. |
| **P3** | Hygiene: redundant/duplicate indexes, style, future-proofing. |

Inside a priority, order by impact ÷ effort (quick wins first). Security findings (SQL built from
strings) are reported regardless of performance priority and flagged as security.

**Effort** — S (minutes, local change) · M (hours, migration or several call sites) · L (days, schema
or architecture change).

---

## Template (full audit)

~~~markdown
# DB Doctor — <project> · <date>

**Scope:** <what was analysed: repo/paths/endpoints>  ·  **Engine:** <PostgreSQL 16 / MySQL 8 / unknown>
**Data access:** <ORMs, raw SQL>  ·  **Evidence level:** <static only | static + EXPLAIN | measured>
**Not analysed:** <e.g. no DB access, no production stats, Kotlin entities>

## 1. Executive summary
<2–4 sentences: what is wrong, where the time goes, what to do first.>

| # | Problem | Where | Impact | Effort | Priority |
|---|---|---|---|---|---|
| 1 | … | … | High | S | P0 |

**Expected result after P0–P1:** <e.g. "/posts: 1 + 3N → 4 queries per request (N = 20)"; measured numbers only if measured.>

## 2. Performance report — results and priorities

### 2.1 Findings
| ID | Area | Finding | Location | Impact | Exposure | Evidence | Priority |
|---|---|---|---|---|---|---|---|
| ORM-1 | ORM | N+1 on `author` in posts list | resources/views/posts/index.blade.php:8 | High | Hot | 🔎 | P0 |

### 2.2 Symptom → root cause
- **<symptom the user reported>** ← ORM-1 (+ IDX-2): <one-line causal chain>.

### 2.3 Details
#### ORM-1 · <title>
- **Symptom:** …
- **Root cause:** …
- **Evidence (🔎):** `path:line` — <snippet or EXPLAIN excerpt>; <query-count arithmetic>
- **Impact:** …

### 2.4 Index health
| Action | Index | Why | Good | Bad / cost |
|---|---|---|---|---|
| Create | comments(post_id) | FK without index, used by `$post->comments` | seek instead of scan per post; cheap cascades | +1 index on every write to comments |
| Drop | users_email_index | duplicate of users_email_unique | less write cost & storage | none (verify no index hints) |
| Keep | posts_status_published_at_index | serves the list query | | |

## 3. Optimization plan

### Phase 1 — Quick wins (low risk, high return)
#### Action 1 — <title> · fixes ORM-1, ORM-3 · effort S
- **Change:** <code diff in the project's idiom>
- **Proposed SQL:**
  ```sql
  …
  ```
- **Expected effect:** …
- **Risks & trade-offs:** …
- **Validation:** <before/after EXPLAIN, query-count test, metric to watch, success threshold>
- **Rollback:** …

### Phase 2 — Planned changes (migrations on big tables, query rewrites, pagination)
### Phase 3 — Structural (only if phases 1–2 aren't enough: denormalization, caching, read replicas, partitioning, search engine)
### Execution order
<numbered order with dependencies, e.g. "create index before deploying the query that relies on it", "drop redundant indexes last, after a week of stats">

## 4. Validation plan
- **Baseline first:** <what to capture before changing anything: p95 of endpoints, query count per request, pg_stat_statements top 20 snapshot, EXPLAIN of target queries>
- **After each phase:** <same measurements; success criteria>
- **Regression guards:** <query-count tests, preventLazyLoading/strict mode in dev & CI, slow-query logging, alert>

## 5. Risks summary
| Change | Risk | Mitigation |
|---|---|---|

## 6. Limitations and next data to collect
- <what static analysis couldn't establish and exactly what to send/run to confirm it: row counts of X, EXPLAIN (ANALYZE, BUFFERS) of query Y, pg_stat_statements top 20, idx_scan of index Z on all replicas>

## Appendix
- Script summaries (counts per rule), index inventory, queries used for evidence.
~~~

## Short report (focused audit)
Use sections 1, 2.1, 3 (only the relevant actions) and 6. Skip empty sections rather than writing "none".

## Quick check (single query/snippet, in chat)
1. Verdict in one line. 2. Why (mechanism). 3. Improved version (code/SQL). 4. Index needed? with good/bad
consequences. 5. How to verify (EXPLAIN / query count). No file.
