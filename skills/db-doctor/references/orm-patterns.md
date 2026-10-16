# ORM patterns: N+1, over-fetching and how to prove it

Read section 0, then only the sections for the ORMs present in the project.

## Contents
0. The N+1 shape and how to prove it (any stack)
1. Laravel Eloquent
2. Spring Data JPA / Hibernate
3. Prisma
4. TypeORM
5. Sequelize
6. Supabase (supabase-js / PostgREST)
7. Other ORMs in brief (Django, SQLAlchemy, GORM, Rails, Drizzle, EF Core)
8. Pitfalls of the fixes

---

## 0. The N+1 shape and how to prove it

One query loads N parent rows; then something touches a relation, or calls the database, once per
parent → 1 + N queries (N × M with nested loops). It hides in:
- loops and collection callbacks (`foreach`, `for…of`, `.map`, `.forEach`, `Promise.all(items.map(…))`)
- templates (Blade `@foreach`, Thymeleaf `th:each`, JSX `.map`) and **serializers** (Laravel API
  Resources, Jackson serializing lazy associations, DTO mappers, GraphQL field resolvers)
- accessors / computed attributes / `$appends`, policies and authorization checks per item
- per-row counts (`$post->comments->count()`, `order.getItems().size()`)
- React Server Components: a child async component that queries, rendered inside a list

**Proof beats suspicion.** Before reporting, try to count queries for the code path:
1. Reason it out from code: what loads the collection, how big can N be (page size? whole table?),
   which relations are touched per item, is anything eager-loaded/batched upstream.
2. If the project can run: enable query logging (per-ORM switches below) and hit the endpoint with
   1 row and with 20 rows. N+1 = the query count grows with the row count.
3. Lock it in with a test that asserts the number of queries stays constant as data grows.
4. At database level, an N+1 shows up as a cheap statement with an enormous `calls` count in
   `pg_stat_statements` (or `count_star` in MySQL digests).

---

## 1. Laravel Eloquent

**Shapes**
- `$post->author->name` / `$post->tags` inside `foreach` or Blade `@foreach` without `with()`.
- `$post->comments->count()` → loads *all* comments per post to count them.
- `$user->posts()->count()` / `->exists()` inside a loop → one aggregate query per row.
- API Resources reading `$this->relation` unconditionally; `Resource::collection($items)` triggers it per item.
- `protected $with = [...]` on a model: the opposite problem — always eager-loads, even when unused.
- `->load('x')` called per item instead of once on the collection.

**Detect / prove**
```php
// AppServiceProvider::boot()
Model::preventLazyLoading(! app()->isProduction());           // throws in dev/tests
Model::handleLazyLoadingViolationUsing(fn ($model, $relation) =>
    logger()->warning("Lazy loaded {$relation} on ".$model::class)); // log instead of throw
// ad-hoc counting
DB::enableQueryLog(); /* … */ count(DB::getQueryLog());
DB::listen(fn ($q) => logger()->debug($q->sql, ['ms' => $q->time]));
```
Tools: Laravel Debugbar, Telescope, Pulse (slow queries). Test: seed 1 vs 20 posts, assert the query
count is equal.

**Fixes**
- `Post::with(['author', 'tags'])->…`, nested `with('comments.user')`, constrained eager loading
  `with(['comments' => fn ($q) => $q->latest()->limit(3)])` (per-parent limit is native since Laravel 11).
- Counts/aggregates: `withCount('comments')`, `withSum('items', 'total')`, `withExists('likes')` → read
  `$post->comments_count`.
- Collections already loaded: `$posts->loadMissing('author')`.
- Resources: `'author' => new UserResource($this->whenLoaded('author'))`, `whenCounted('comments')`.
- Batch jobs: `chunkById()`, `lazyById()`; never `all()` on large tables.
- Writes: `Model::whereIn('id', $ids)->update([...])`, `upsert($rows, ['uniqueBy'], ['cols'])`,
  `insert($rows)` in chunks, inside `DB::transaction()`. Note `createMany()`/`saveMany()` still run one
  INSERT per model.
- Recent Laravel 12 releases can auto eager-load relations
  (`Model::automaticallyEagerLoadRelationships()`); treat it as a safety net, not a substitute for
  explicit loading.

**Query-builder traps:** `whereDate/whereYear/whereMonth` (non-sargable), `inRandomOrder()`,
`paginate()` (OFFSET + COUNT per request → `cursorPaginate()`/`simplePaginate()`),
`where('col', 'like', "%$q%")`, `->get()->count()`.

**Schema notes:** `foreignId()->constrained()` adds the FK constraint only — on Postgres there is no
index unless you add `->index()`. Models with `SoftDeletes` add `deleted_at IS NULL` to every query:
on Postgres consider partial indexes `… WHERE deleted_at IS NULL` for hot queries.

---

## 2. Spring Data JPA / Hibernate

**Shapes**
- Lazy associations/collections touched in loops, MapStruct/DTO mappers, or by Jackson while
  serializing (works only because `spring.jpa.open-in-view=true`, the Boot default, keeps the session
  open → N+1 in the web layer and a connection held per request).
- `@ManyToOne`/`@OneToOne` are **EAGER by default**: loading a list via JPQL/derived query without
  `JOIN FETCH` makes Hibernate issue an extra select per distinct referenced entity.
- `fetch = FetchType.EAGER` on collections: always loaded; with lists of parents → N+1 or huge joins.
- `repository.findById(id)` / `save(entity)` inside loops.
- `save()` on entities with an assigned id (e.g. UUID set in the constructor) is treated as "not new"
  → `merge` → an extra SELECT before each INSERT. Implement `Persistable<ID>` or use `@Version`.

**Detect / prove**
```properties
spring.jpa.properties.hibernate.generate_statistics=true
logging.level.org.hibernate.stat=DEBUG          # per-session: "N JDBC statements executed"
logging.level.org.hibernate.SQL=DEBUG           # each statement
logging.level.org.hibernate.orm.jdbc.bind=TRACE # bind values (Hibernate 6)
```
In tests, read `sessionFactory.getStatistics().getPrepareStatementCount()` (statistics enabled), or wrap
the DataSource with datasource-proxy/p6spy and assert the count.

**Fixes**
- Fetch plans per use case:
  ```java
  @Query("select o from Order o join fetch o.customer where o.status = :status")
  List<Order> findWithCustomerByStatus(String status);

  @EntityGraph(attributePaths = {"customer", "items", "items.product"})
  List<Order> findByStatus(String status);
  ```
- DTO projections for read models: `select new com.acme.OrderRow(o.id, c.name, sum(i.price * i.quantity)) from Order o join o.customer c join o.items i group by o.id, c.name`.
- Safety net: `spring.jpa.properties.hibernate.default_batch_fetch_size=50` (or `@BatchSize` on the
  association) turns N lazy loads into ~N/50 `IN` queries.
- Map every `@ManyToOne`/`@OneToOne` with `fetch = FetchType.LAZY`; collections stay LAZY.
- `spring.jpa.open-in-view=false` (then fix the `LazyInitializationException`s by fetching explicitly).
- Writes: `hibernate.jdbc.batch_size=50`, `hibernate.order_inserts=true`, `hibernate.order_updates=true`;
  note `GenerationType.IDENTITY` disables JDBC insert batching — prefer `SEQUENCE` (pooled) on Postgres.
  Bulk changes: `@Modifying @Query("update Order o set o.status = :s where o.id in :ids")` (bypasses the
  persistence context; use `clearAutomatically = true`).
- Existence: `existsByCustomerId` instead of `countBy… > 0`. Paging: `Slice<T>` avoids the COUNT;
  keyset with `Window<T>`/`ScrollPosition` (Spring Data 3.1+).

**Fix pitfalls**
- `MultipleBagFetchException`: you can't `JOIN FETCH` two `List` collections at once → use `Set` or
  fetch them in two queries.
- `JOIN FETCH` of a collection + pagination: Hibernate paginates **in memory** (warning HHH90003004 in
  Hibernate 6, HHH000104 in 5). Page ids first, then fetch entities by ids; or set
  `hibernate.query.fail_on_pagination_over_collection_fetch=true` to fail fast.
- Fetching several collections in one query multiplies rows (cartesian explosion).

**Derived-query traps:** `…IgnoreCase` (function on column), `…Containing`/`EndingWith` (leading
wildcard), `Page<T>` (extra COUNT). Hibernate does not create indexes for FK columns.

---

## 3. Prisma

**Shapes** — Prisma has no lazy loading, so N+1 is always explicit:
- `await prisma.x.findUnique/findFirst/count(...)` inside `for…of` / `.map` / `.forEach`.
- `Promise.all(items.map(i => prisma…))` — N concurrent queries: same round-trips, plus connection-pool
  pressure. Exception: `findUnique` calls in the same tick are batched by Prisma's built-in dataloader
  (also the fluent API `findUnique(...).posts()`), so verify with logs before reporting.
- GraphQL field resolvers and React Server Components that query per item.

**Detect / prove**
```ts
const prisma = new PrismaClient({ log: [{ emit: 'event', level: 'query' }] });
let n = 0; prisma.$on('query', () => n++);
```

**Fixes**
- `include` / nested `select` on the parent query (by default one extra query per relation *level*,
  using `IN (…)`, not per row). A JOIN-based strategy (`relationLoadStrategy: 'join'`) exists behind the
  `relationJoins` preview flag in recent versions — check the project's Prisma version before suggesting it.
- Counts: `include: { _count: { select: { comments: true } } }`; aggregates: `groupBy`.
- Manual batching: `findMany({ where: { id: { in: ids } } })` + `Map` lookup.
- Writes: `createMany`, `updateMany`, `deleteMany`, or `$transaction([...])` to group them.
- Pagination: `cursor` + `take` instead of `skip` for deep pages.

**Schema notes**
- On Postgres, relation scalars (`authorId`) get an FK constraint but **no index** → add
  `@@index([authorId])`. With `relationMode = "prisma"` there are no FK constraints at all, so every
  relation scalar needs an explicit `@@index`.
- `contains` + `mode: 'insensitive'` → `ILIKE '%…%'`: needs a trigram index. Create it in SQL
  (`prisma migrate dev --create-only`, then edit the generated migration.sql).
- `@unique` already creates an index; `@@index([email])` next to it is a duplicate.

---

## 4. TypeORM

**Shapes**
- Lazy relations (`product: Promise<Product>`): every `await order.product` is a query.
- `eager: true` relations are joined into every `find*()` (not QueryBuilder) — heavy and implicit,
  especially on to-many.
- Repository calls inside loops; `save()` per entity.

**Detect:** DataSource `logging: ['query']` (or `true`), `maxQueryExecutionTime: 500` to log slow
queries; count via a custom `Logger`.

**Fixes:** `find({ relations: { author: true } })`, QueryBuilder `leftJoinAndSelect('post.author', 'author')`,
`relationLoadStrategy: 'query'` (separate queries instead of a big join) where joins multiply rows,
`loadRelationCountAndMap` for counts, `findBy({ id: In(ids) })` for manual batching. With joins,
paginate with `skip/take` (TypeORM pages distinct parent ids) rather than `offset/limit`.
Writes: `save(entities, { chunk: 100 })`, `insert().values([...])`, `upsert()`.

**Schema notes:** `@Index()` on relation columns isn't automatic; `@Column({ unique: true })` plus
`@Index(['email'])` duplicates the index; on Postgres FK columns (`userId`) are not indexed by default.

---

## 5. Sequelize

**Shapes:** association accessors per item (`await invoice.getUser()`), `Model.findOne` inside loops,
`instance.update()/save()` per row.

**Detect:** `new Sequelize(url, { logging: (sql, ms) => console.log(ms, sql), benchmark: true })`.

**Fixes:** `include: [{ model: User, as: 'user', attributes: ['id', 'email'] }]`; for `hasMany` with
`limit`, `separate: true` (one extra query per include instead of a join); `findAndCountAll` with
`include` needs `distinct: true` or the count is inflated; `raw: true` for read-only lists;
`attributes` to avoid `SELECT *`. Writes: `bulkCreate(rows, { updateOnDuplicate: [...] })`,
`Model.update(values, { where: { id: ids } })`.

**Schema notes:** model `indexes: [{ fields: [...] }]` / migrations `addIndex`; FK columns created by
associations are not indexed automatically on Postgres. `addIndex(..., { concurrently: true })` for
Postgres online builds.

---

## 6. Supabase (supabase-js / PostgREST)

**Shapes:** `supabase.from(...)` inside a loop = N HTTP requests **and** N queries — worse than an ORM
N+1 because each one crosses the network. `count: 'exact'` on big tables runs a full `COUNT(*)`.

**Fixes**
- Resource embedding in one request: `.select('id, title, author:profiles(name), comments(count)')`.
- Batch by key: `.in('post_id', ids)`.
- Complex aggregation: a SQL function called with `supabase.rpc('fn', args)` (one round-trip).
- Counts: `count: 'planned'` or `'estimated'` for large tables.

**RLS performance** — policies run on every query (see `SUPA-RLS-PER-ROW` in sql-antipatterns.md):
wrap `auth.uid()` as `(select auth.uid())`, index policy columns, repeat the filter in the client query,
avoid joins in policies, add `to authenticated`.

**Tools:** Supabase Performance Advisor (lints such as unindexed foreign keys, duplicate/unused indexes,
`auth_rls_initplan`, multiple permissive policies); the Query Performance report (pg_stat_statements);
`index_advisor` extension (`select * from index_advisor('select …')`); `supabase inspect db …` CLI
commands (outliers, seq-scans, unused-indexes, index-usage, bloat, blocking, table-sizes). If a Supabase
connector is available, `get_advisors` (type `performance`) and read-only `execute_sql`.

---

## 7. Other ORMs in brief

| ORM | N+1 fix | Guard / detection | Bulk writes |
|---|---|---|---|
| Django | `select_related` (FK/1-1, JOIN), `prefetch_related` / `Prefetch(...)` (reverse FK, M2M), `annotate(Count(...))` | `assertNumQueries` in tests, django-debug-toolbar, nplusone | `bulk_create`, `bulk_update`, `QuerySet.update()` |
| SQLAlchemy | `selectinload`, `joinedload`, `subqueryload` | `raiseload('*')` to forbid lazy loads, `echo=True` | `session.execute(insert(...), rows)`, `update(...).where(...)` |
| GORM (Go) | `Preload("Orders")`, `Joins("Company")` | `db.Debug()`, logger level Info | `CreateInBatches(rows, 100)` |
| Rails ActiveRecord | `includes` / `preload` / `eager_load` | `strict_loading`, Bullet gem | `insert_all`, `upsert_all`, `update_all` |
| Drizzle | relational queries `with: { posts: true }` (single query) | logger | multi-row `insert().values([...])` |
| EF Core | `Include`/`ThenInclude`, `AsSplitQuery()` for multiple collections | logging, `AsNoTracking()` for reads | `ExecuteUpdate`/`ExecuteDelete` (EF 7+) |

---

## 8. Pitfalls of the fixes (always mention the relevant ones as risks)

- **Over-eager loading:** loading relations that aren't used, or 10,000 children for a list page that
  shows 3. Use constrained eager loading, counts instead of collections, or a separate endpoint.
- **Cartesian explosion:** JOIN-fetching two collections multiplies rows (parents × A × B). Prefer
  separate queries per collection (Prisma default, Hibernate batch fetching, EF `AsSplitQuery`).
- **Huge IN lists:** eager loading thousands of parents creates `IN (…)` lists with thousands of
  parameters (Postgres hard limit 65,535 bind parameters per statement). Chunk the parents.
- **Memory:** hydrating thousands of full entities is slow even with few queries — use projections,
  `select`, `raw: true`, streaming.
- **Pagination + collection fetch:** see Hibernate HHH90003004; similar issues with joins in other ORMs.
- **Caching as a band-aid:** caching an N+1 endpoint hides the problem and adds invalidation bugs.
  Fix the access pattern first; cache afterwards if still needed.
