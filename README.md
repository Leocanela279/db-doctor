# db-doctor

**A Claude skill that audits the database layer of an application** — SQL queries, ORM code that turns
into queries, and indexes — and hands back a prioritized performance report plus an optimization plan
with evidence, proposed SQL, risks and validation.

> 🇪🇸 Skill para auditar consultas SQL y código ORM (N+1), analizar índices (redundantes, duplicados,
> que faltan) y generar un informe de rendimiento con plan de optimización: evidencias, SQL propuesta,
> riesgos y validación. El informe se escribe en el idioma del usuario.

Many "the code is slow" bugs are really data-access problems: a loop that silently fires 500 queries, a
`WHERE` no index can serve, an index every write pays for and no read uses. db-doctor makes the
database side visible and proves it with evidence.

## What it does

| Module | Finds |
|---|---|
| **SQL Analyzer** | non-sargable predicates (functions/casts on columns, `whereDate`, `IgnoreCase`), leading-wildcard `LIKE`, deep `OFFSET`, `NOT IN` + NULL, correlated subqueries, `ORDER BY RAND()`, unbounded `UPDATE/DELETE`, `SELECT *`, COUNT-as-existence, SQL built by string concatenation (injection), Supabase RLS policies evaluated per row |
| **ORM Inspector** | N+1 (queries, counts and lazy relations inside loops, templates and serializers), `Promise.all` fan-out, writes in loops, EAGER to-many mappings, `open-in-view`, missing lazy-loading guards, whole-table loads, in-memory filtering |
| **Index Advisor & Inspector** | index inventory from your schema, duplicate and left-prefix-redundant indexes, foreign keys without an index, low-selectivity indexes, hot columns the code filters/sorts on without an index — with the good *and* bad consequences of every change |
| **Performance Reporter** | findings ranked P0–P3 by impact, exposure and evidence level, linking each symptom to its root cause |
| **Optimization Plan** | phased actions with code in your ORM's idiom, production-safe DDL, risks, validation steps and rollback |

Stacks covered in depth: **PostgreSQL, MySQL/MariaDB, Laravel Eloquent, Spring Data JPA/Hibernate,
Prisma, TypeORM, Sequelize, Supabase**. Django, SQLAlchemy, GORM, Rails, Drizzle and EF Core get generic
coverage.

## Install

The skill is the folder [`skills/db-doctor`](skills/db-doctor):

```
skills/db-doctor/
├── SKILL.md                     # workflow: recon → 5 modules → report
├── scripts/                     # Python 3 stdlib only, no install
│   ├── scan_sql.py
│   ├── scan_orm.py
│   ├── analyze_indexes.py
│   └── _common.py
└── references/                  # loaded on demand
    ├── sql-antipatterns.md
    ├── orm-patterns.md
    ├── index-advisor.md
    ├── db-introspection.md      # read-only catalog queries + how to read EXPLAIN
    └── report-template.md
```

- **Claude Code:** copy it to `~/.claude/skills/db-doctor` (all projects) or `<project>/.claude/skills/db-doctor` (one project).
  ```bash
  git clone https://github.com/Leocanela279/db-doctor.git
  cp -r db-doctor/skills/db-doctor ~/.claude/skills/
  ```
- **Claude apps:** zip the `skills/db-doctor` folder and add it as a custom skill.

## Use

Just describe the problem; the skill triggers on performance and database topics:

- *"La página /posts tarda 4 segundos, creo que es un bug del controlador."*
- *"Audit the data access layer of this Spring Boot service, the orders endpoint times out."*
- *"¿Sobra o falta algún índice en este schema.prisma? Explícame lo bueno y lo malo de cada cambio."*
- *"Is this query OK?"* + a pasted query or EXPLAIN plan → quick answer in the chat.

It picks the depth (quick check, focused audit, full audit), runs the scripts, verifies every lead in
the code, and writes `db-doctor-report.md` for audits. See a full sample report (in Spanish):
[`examples/laravel-blog/db-doctor-report.md`](examples/laravel-blog/db-doctor-report.md).

## Run the scripts on their own

They work as a standalone CLI too:

```bash
python3 skills/db-doctor/scripts/scan_sql.py        path/to/project [--engine postgres|mysql]
python3 skills/db-doctor/scripts/scan_orm.py        path/to/project
python3 skills/db-doctor/scripts/analyze_indexes.py path/to/project --inventory
python3 skills/db-doctor/scripts/analyze_indexes.py schema.sql --engine postgres   # pg_dump/mysqldump file
```

Common flags: `--json`, `--min-severity high|medium|low|info`, `--limit N`, `--engine`.

Schema sources understood by `analyze_indexes.py`: SQL DDL and dumps (`pg_dump --schema-only`,
`mysqldump --no-data`, Flyway, Supabase and Prisma migration SQL), Laravel migrations, `schema.prisma`,
JPA entities (Java), TypeORM entities, Sequelize migrations. For anything else, point it at a schema dump.

## Limitations

- Static analysis gives **leads, not verdicts**: the skill verifies each one in context and labels
  evidence as measured ✅, verified in code 🔎 or suspected ⚠️.
- Without database access it can't know table sizes or real index usage — it never claims an index is
  "unused" from code alone and tells you which statistics to collect.
- The audit is read-only: it proposes DDL and code; your team applies it.

## Repository layout

```
skills/db-doctor/   the skill
examples/           sample apps with planted problems (Laravel, Spring, Next+Prisma+Supabase, TypeORM+Sequelize)
evals/evals.json    test prompts and assertions for iterating on the skill
```
