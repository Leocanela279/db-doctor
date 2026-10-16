#!/usr/bin/env python3
"""db-doctor · SQL analyzer.

Looks for SQL that the engine cannot execute efficiently (or safely):
  * .sql files (migrations, reports, views, functions, RLS policies)
  * SQL string literals inside code (raw queries, @Query, DB::select, $queryRaw...),
    including SQL assembled by concatenation/interpolation (injection + no plan reuse)
  * ORM / query-builder calls that compile to non-sargable or expensive SQL
    (whereDate, ...IgnoreCase, ...Containing, Prisma contains/insensitive, inRandomOrder,
    OFFSET pagination, COUNT used as an existence test)

Usage:
  python3 scan_sql.py <project-root> [--engine postgres|mysql] [--json] [--min-severity low]

Heuristic: each hit is a lead. Confirm with the real query (query log) and EXPLAIN.
"""
from __future__ import annotations

import os
import re
import sys

sys.dont_write_bytecode = True  # don't litter the skill folder with __pycache__
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    Finding, LineIndex, base_parser, dedupe, detect_stacks, filter_severity, iter_files, read_text, rel,
    render_markdown, resolve_engine, scan_source, snippet_of, sort_findings, to_json,
)

SQL_START = re.compile(r"^\s*\(?\s*(SELECT|WITH|UPDATE|DELETE|INSERT|MERGE|REPLACE)\b", re.I)
SQL_SHAPE = re.compile(r"\bSELECT\b[\s\S]+?\bFROM\b|\bUPDATE\s+[\w.\"`]+\s+SET\b|\bDELETE\s+FROM\b|\bINSERT\s+INTO\b", re.I)
RAW_CONTEXT = re.compile(
    r"(?:whereRaw|orWhereRaw|havingRaw|orHavingRaw|orderByRaw|selectRaw|groupByRaw|DB::raw|DB::select|"
    r"DB::selectOne|DB::statement|DB::unprepared|DB::insert|DB::update|DB::delete|->raw|\.raw|"
    r"queryRawUnsafe|executeRawUnsafe|createNativeQuery|createQuery|createSQLQuery|nativeQuery|\.query|"
    r"\.execute|\.exec|executeQuery|executeUpdate|prepareStatement|\.prepare|literal|sequelize\.query|"
    r"\.Raw|\.Exec|\.Query|QueryRow\w*|QueryContext|ExecContext|cursor\.execute|text|@Query)\s*\(\s*"
    r"(?:value\s*=\s*|sql\s*=\s*)?$")

FUNCS = (r"LOWER|UPPER|DATE|YEAR|MONTH|DAY|TRIM|LTRIM|RTRIM|SUBSTRING|SUBSTR|LEFT|RIGHT|CAST|CONVERT|"
         r"COALESCE|IFNULL|ISNULL|NVL|DATE_FORMAT|TO_CHAR|TO_DATE|EXTRACT|CONCAT|ABS|ROUND|FLOOR|CEIL|"
         r"LENGTH|CHAR_LENGTH|UNIX_TIMESTAMP|DATE_TRUNC|DATEDIFF|TIMESTAMPDIFF|JSON_EXTRACT|JSON_UNQUOTE|"
         r"MD5|STR_TO_DATE")
FUNC_ON_COL = re.compile(
    r"(?:\bWHERE|\bAND|\bOR|\bON|\bNOT)\s+\(?\s*(" + FUNCS + r")\s*\(((?:[^()]|\([^()]*\))*)\)\s*"
    r"(=|<>|!=|<=|>=|<|>|\bIN\b|\bLIKE\b|\bILIKE\b|\bBETWEEN\b|\bIS\b)", re.I)
PG_CAST_ON_COL = re.compile(r"(?:\bWHERE|\bAND|\bOR|\bON)\s+\(?\s*([A-Za-z_][\w.\"]*)::(\w+)\s*(=|<>|!=|<=|>=|<|>|\bIN\b|\bBETWEEN\b)", re.I)


def norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def select_list(s: str):
    m = re.search(r"\bSELECT\b", s, re.I)
    if not m:
        return ""
    depth = 0
    i = m.end()
    while i < len(s):
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and re.match(r"\bFROM\b", s[i:i + 5], re.I) and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] == "_")):
            return s[m.end():i]
        i += 1
    return ""


def sql_rules(sql: str, engine: str, built: bool = False):
    """Return list of (rule, severity, message, hint, confidence) for one SQL statement."""
    out = []
    s = norm(sql)

    def add(rule, sev, msg, hint, conf="high"):
        out.append((rule, sev, msg, hint, conf))

    for m in re.finditer(r"\bSELECT\s+(?:DISTINCT\s+|ALL\s+|TOP\s+\d+\s+)?((?:[\w\"`]+\.)?\*)\s*(?:,|\bFROM\b)", s, re.I):
        if re.search(r"\bEXISTS\s*\(\s*$", s[:m.start()], re.I):
            continue
        add("SQL-SELECT-STAR", "low",
            f"`SELECT {m.group(1)}` fetches every column: more I/O, memory and network, rules out index-only/covering scans, and silently changes when columns are added.",
            "Select only the columns the caller uses (or a DTO/projection).")
        break

    if re.search(r"\b(?:I?LIKE)\s+(?:N?'%|CONCAT\s*\(\s*'%'|'%'\s*\|\|)", s, re.I):
        add("SQL-LEADING-WILDCARD", "medium",
            "`LIKE '%…'` with a leading wildcard can't use a B-tree index: every candidate row is scanned and compared.",
            "Postgres: `pg_trgm` GIN index (`CREATE INDEX ... USING gin (col gin_trgm_ops)`) or full-text search; MySQL: FULLTEXT index + MATCH…AGAINST; or anchor the pattern (`'abc%'`).")

    for m in FUNC_ON_COL.finditer(s):
        arg = re.sub(r"'[^']*'", "", m.group(2))
        if not re.search(r"[A-Za-z_][\w.]*", re.sub(r"(?i)\b(YEAR|MONTH|DAY|HOUR|MINUTE|EPOCH|DOW|WEEK|QUARTER|FROM|AS|\w+_ops|text|date|int\w*|varchar|char|unsigned|signed)\b", "", arg)):
            continue
        fn = m.group(1).upper()
        hint = ("Rewrite as a range on the raw column (e.g. `created_at >= '2025-01-01' AND created_at < '2026-01-01'`), "
                "normalize the value when writing, or create an expression index on exactly that expression "
                "(Postgres `CREATE INDEX ... ON t ((lower(email)))`; MySQL 8.0.13+ functional key part).")
        if fn in ("LOWER", "UPPER") and engine == "mysql":
            hint = "MySQL `_ci` collations already compare case-insensitively: drop the `lower()`/`upper()` and the index on the raw column works. " + hint
        add("SQL-FUNC-ON-COLUMN", "medium",
            f"`{fn}(…)` applied to a column inside a predicate (`{norm(m.group(0))[:90]}`): a plain index on that column can't be used (non-sargable).",
            hint)
        break
    else:
        m = PG_CAST_ON_COL.search(s)
        if m:
            add("SQL-FUNC-ON-COLUMN", "medium",
                f"Cast `{m.group(1)}::{m.group(2)}` on a column inside a predicate: the column's index can't be used.",
                "Compare against a range of the column's own type instead (e.g. `ts >= :day AND ts < :day + interval '1 day'`), or index the expression.")

    m = re.search(r"\bOFFSET\s+(\d+)", s, re.I) or re.search(r"\bLIMIT\s+(\d+)\s*,\s*\d+", s, re.I)
    if m and int(m.group(1)) >= 1000:
        add("SQL-DEEP-OFFSET", "medium",
            f"OFFSET {m.group(1)}: the engine produces and throws away {m.group(1)} rows before returning the page; cost grows with every page.",
            "Keyset pagination: `WHERE (created_at, id) < (:last_created_at, :last_id) ORDER BY created_at DESC, id DESC LIMIT 50` with an index on (created_at, id). MySQL: expand to `created_at < ? OR (created_at = ? AND id < ?)`.")
    elif re.search(r"\bOFFSET\s+(\?|:\w+|\$\d+|%s|@\w+)|\bLIMIT\s+(\?|:\w+|\$\d+)\s*,", s, re.I):
        add("SQL-DEEP-OFFSET", "info", "Variable OFFSET: deep pages get slower linearly.",
            "Fine for the first pages; for deep pagination / infinite scroll use keyset (cursor) pagination.", "medium")

    if re.search(r"\bORDER\s+BY\s+(?:RAND|RANDOM|NEWID|DBMS_RANDOM\.VALUE)\s*\(", s, re.I):
        add("SQL-ORDER-RANDOM", "medium",
            "`ORDER BY RAND()/RANDOM()` computes a random value for every row and sorts the whole set to return a few rows.",
            "Pick random keys in the application, `TABLESAMPLE SYSTEM (n)` in Postgres, or precompute a random column/sample.")

    if re.search(r"\bNOT\s+IN\s*\(\s*SELECT\b", s, re.I):
        add("SQL-NOT-IN-SUBQUERY", "medium",
            "`NOT IN (SELECT …)`: if the subquery returns any NULL the predicate matches nothing (silent wrong results), and the planner can't turn it into an efficient anti-join.",
            "Use `NOT EXISTS (SELECT 1 FROM … WHERE … = outer.col)` (or LEFT JOIN … IS NULL) — note it changes NULL semantics, usually fixing a latent bug.")

    head = s.lstrip("( ").upper()
    if (head.startswith("UPDATE ") or head.startswith("DELETE ")) and not re.search(r"\bWHERE\b", s, re.I) and "CURRENT OF" not in head:
        add("SQL-UNBOUNDED-WRITE", "high",
            "UPDATE/DELETE without WHERE touches every row: full-table lock pressure, a huge transaction/WAL/binlog, replica lag.",
            "If intended, batch it (`WHERE id BETWEEN … LIMIT n` loops) off-peak; otherwise add the missing WHERE.",
            "low" if built else "high")

    if re.search(r"\bSELECT\s+DISTINCT\b", s, re.I) and re.search(r"\bJOIN\b", s, re.I):
        add("SQL-DISTINCT-JOIN", "low",
            "`DISTINCT` over a JOIN often hides a fan-out (1→N join duplicating parent rows): the full product is built and then de-duplicated.",
            "If you only need parents that have a match, use `WHERE EXISTS (SELECT 1 FROM child …)` instead of JOIN + DISTINCT.", "medium")

    sl = select_list(s)
    if re.search(r"\(\s*SELECT\b", sl, re.I):
        add("SQL-CORRELATED-SUBQUERY", "medium",
            "Subquery in the SELECT list: unless the planner decorrelates it, it runs once per returned row (an N+1 inside the database).",
            "Rewrite as a JOIN to a pre-aggregated derived table (`LEFT JOIN (SELECT customer_id, COUNT(*) … GROUP BY customer_id) x ON …`) or a LATERAL join; make sure the correlated column is indexed.",
            "medium")

    w = re.search(r"\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bHAVING\b|$)", s, re.I)
    if w:
        cond = w.group(1)
        om = re.search(r"([A-Za-z_][\w.\"`]*)\s*(?:=|<>|!=|>=|<=|>|<|\bLIKE\b|\bIN\b)[^()]*?\bOR\s+(?:NOT\s+)?([A-Za-z_][\w.\"`]*)\s*(?:=|<>|!=|>=|<=|>|<|\bLIKE\b|\bIN\b)", cond, re.I)
        if om and om.group(1).split(".")[-1].lower() != om.group(2).split(".")[-1].lower():
            add("SQL-OR-DIFFERENT-COLUMNS", "low",
                f"`{om.group(1)} … OR {om.group(2)} …` across different columns: a single index can't serve both branches; the planner falls back to a scan or a costly bitmap/index-merge.",
                "Split into `UNION ALL` of two index-friendly queries (dedupe if needed), or make sure each column has an index and check EXPLAIN.", "medium")

    if re.search(r"\bUNION\b(?!\s+ALL\b)", s, re.I):
        add("SQL-UNION-DISTINCT", "low",
            "`UNION` (without ALL) de-duplicates the combined result with a sort/hash.",
            "Use `UNION ALL` when duplicates are impossible or acceptable.", "medium")

    if re.search(r"\bFROM\s+[\w.\"`]+(?:\s+(?:AS\s+)?(?!WHERE\b|JOIN\b|LEFT\b|RIGHT\b|INNER\b|ORDER\b|GROUP\b|LIMIT\b)\w+)?\s*,\s*[A-Za-z_\"`]", s, re.I):
        add("SQL-IMPLICIT-JOIN", "info",
            "Comma-style join (`FROM a, b`): a missing join predicate silently becomes a cartesian product.",
            "Use explicit `JOIN … ON …`.", "medium")

    is_policy = bool(re.match(r"(?i)\s*(CREATE|ALTER)\s+POLICY\b", s))
    if not is_policy and re.search(r"\bFOR\s+(?:UPDATE|NO\s+KEY\s+UPDATE|SHARE)\b", s, re.I) and not re.search(r"SKIP\s+LOCKED|NOWAIT", s, re.I):
        add("SQL-LOCKING-READ", "info",
            "Locking read (`FOR UPDATE`/`FOR SHARE`): rows stay locked until the transaction ends; long transactions here cause lock waits and deadlocks.",
            "Keep the transaction short (no HTTP calls inside); for job queues use `FOR UPDATE SKIP LOCKED`.", "medium")

    if "SQL_CALC_FOUND_ROWS" in s.upper():
        add("SQL-CALC-FOUND-ROWS", "low", "`SQL_CALC_FOUND_ROWS` is deprecated (MySQL 8.0.17) and forces the full result to be computed.",
            "Run a separate `SELECT COUNT(*)` (or avoid the total count).")

    if re.search(r"\bCOUNT\s*\(\s*(?:\*|1|[\w.]+)\s*\)\s*(?:>|>=)\s*[01]\b", s, re.I):
        add("SQL-COUNT-FOR-EXISTENCE", "low", "COUNT(*) compared to 0/1: counts every match when one row is enough.",
            "Use `EXISTS (SELECT 1 …)`.")

    if re.search(r"\bCREATE\s+POLICY\b", s, re.I):
        for m in re.finditer(r"\b(auth\.(?:uid|jwt|role|email)|current_setting)\s*\(", s, re.I):
            before = s[max(0, m.start() - 12):m.start()]
            if re.search(r"select\s*$", before, re.I):
                continue
            add("SUPA-RLS-PER-ROW", "medium",
                f"RLS policy calls `{m.group(1)}()` directly: Postgres may evaluate it for every row scanned.",
                "Wrap it so it becomes an initPlan evaluated once: `using ((select auth.uid()) = user_id)`, and index the column it is compared with (`user_id`).")
            break
    return out


# ------------------------------------------------------------- query-builder rules

def _qb(rule, sev, langs, pattern, message, hint, conf="high", flags=0):
    return dict(rule=rule, sev=sev, langs=langs, rx=re.compile(pattern, flags), msg=message, hint=hint, conf=conf)


QB_RULES = [
    _qb("QB-WHERE-DATE", "medium", {"php"},
        r"(?:->|::)\s*(?:or)?[wW]here(Date|Year|Month|Day|Time)\s*\(\s*['\"]([\w.]+)['\"]",
        lambda m: f"`where{m.group(1)}('{m.group(2)}', …)` compiles to `{m.group(1).lower()}({m.group(2)}) = ?`: the index on `{m.group(2)}` can't be used.",
        lambda m: f"Use a half-open range on the raw column: `->where('{m.group(2)}', '>=', $start)->where('{m.group(2)}', '<', $end)`."),
    _qb("QB-RANDOM-ORDER", "medium", {"php", "js", "python", "java"},
        r"->\s*inRandomOrder\s*\(|orderByRaw\s*\(\s*['\"]\s*(?:RAND|RANDOM)\s*\(|(?:sequelize|Sequelize)\s*\.\s*(?:random\s*\(|fn\s*\(\s*['\"](?:RAND|RANDOM)['\"])|orderBy\s*\(\s*['\"](?:RAND|RANDOM)\(\)|order_by\s*\(\s*['\"]\?['\"]\s*\)",
        lambda m: "Random ordering: computes a random key for every row and sorts the whole set to return a few rows.",
        lambda m: "Pick random ids in the app, use `TABLESAMPLE` (Postgres), or precompute/cache a random sample."),
    _qb("QB-CONTAINS", "medium", {"java", "kotlin"},
        r"\b((?:find|read|get|query|search|stream|count|exists|delete)\w*By\w*?(?:Containing|Contains|IsContaining|EndingWith|EndsWith|IsEndingWith)\w*)\s*\(",
        lambda m: f"Spring Data `{m.group(1)}` generates `LIKE '%…%'`/`LIKE '%…'`: leading wildcard, no B-tree index can serve it.",
        lambda m: "Prefer `StartingWith` (+ `text_pattern_ops` index on Postgres non-C collations) or full-text/trigram search for real substring search."),
    _qb("QB-CONTAINS", "medium", {"js"},
        r"\b(contains|endsWith)\s*:\s*(?!\{)|\bI?Like\s*\(\s*[`'\"]%|\[\s*Op\.(?:substring|endsWith|iLike|like|notLike)\s*\]\s*:\s*[`'\"]%|\[\s*Op\.(?:substring|endsWith)\s*\]|\.(?:i?like|whereI?Like|orWhereI?Like)\s*\(\s*['\"][\w.]+['\"]\s*,\s*[`'\"]%",
        lambda m: f"`{m.group(0).strip().replace('`', chr(39))[:40]}…` produces a leading-wildcard `LIKE/ILIKE '%…'`: no B-tree index can serve it.",
        lambda m: "Postgres: trigram GIN index (`pg_trgm`) or full-text search; MySQL: FULLTEXT; or switch to prefix search (`startsWith`)."),
    _qb("QB-CONTAINS", "medium", {"php"},
        r"(?:->|::)\s*(?:or)?[wW]here\s*\(\s*['\"]([\w.]+)['\"]\s*,\s*['\"](?:i?like|I?LIKE|not like)['\"]\s*,\s*(?:[\"'`]%|'%'\s*\.)",
        lambda m: f"`LIKE '%…'` on `{m.group(1)}` (leading wildcard): no B-tree index can serve it.",
        lambda m: "Postgres: trigram GIN index or full-text; MySQL: FULLTEXT index (or Laravel Scout) for search; prefix search (`'term%'`) can use an index."),
    _qb("QB-CONTAINS", "medium", {"python"},
        r"__(i?contains|i?endswith)\s*=",
        lambda m: f"`__{m.group(1)}` produces a leading-wildcard LIKE: no B-tree index can serve it.",
        lambda m: "Trigram index (`GinIndex(opclasses=['gin_trgm_ops'])`) or full-text search."),
    _qb("QB-CASE-INSENSITIVE", "medium", {"java", "kotlin"},
        r"\b((?:find|read|get|query|search|stream|count|exists|delete)\w*By\w*?(?:IgnoreCase|IgnoringCase)\w*)\s*\(",
        lambda m: f"Spring Data `{m.group(1)}` wraps the column in `upper()`/`lower()`: a plain index on it isn't used.",
        "ci"),
    _qb("QB-CASE-INSENSITIVE", "medium", {"js"},
        r"mode\s*:\s*['\"]insensitive['\"]|\bILike\s*\(|Op\.iLike|\$iLike|\.ilike\s*\(",
        lambda m: f"`{m.group(0).strip()}` compiles to `ILIKE`/case folding on the column: a plain B-tree index isn't used.",
        "ci"),
    _qb("QB-CASE-INSENSITIVE", "medium", {"php"},
        r"(?:where|orWhere)Raw\s*\(\s*['\"]\s*(?:lower|upper)\s*\(|['\"]ilike['\"]",
        lambda m: "Case folding / ILIKE on the column: a plain index on it isn't used.",
        "ci"),
    _qb("QB-CASE-INSENSITIVE", "medium", {"python"},
        r"__i(exact|startswith)\s*=",
        lambda m: f"`__i{m.group(1)}` wraps the column in UPPER()/uses ILIKE: a plain index isn't used.",
        "ci"),
    _qb("QB-OFFSET-PAGINATION", "info", {"php"},
        r"->\s*(?:skip|offset)\s*\(|->\s*paginate\s*\(",
        lambda m: "OFFSET pagination (`paginate()` also runs a `COUNT(*)` per request): deep pages read and discard every skipped row.",
        lambda m: "For big tables / infinite scroll: `cursorPaginate()` (keyset) or `simplePaginate()` (no COUNT).", "medium"),
    _qb("QB-OFFSET-PAGINATION", "info", {"java", "kotlin"},
        r"\bPageRequest\s*\.\s*of\s*\(",
        lambda m: "OFFSET pagination (`Page<T>` also runs a COUNT query): deep pages get linearly slower.",
        lambda m: "Return `Slice<T>` to skip the COUNT, or keyset scrolling (`Window<T>` + `ScrollPosition.keyset()`, Spring Data 3.1+).", "medium"),
    _qb("QB-OFFSET-PAGINATION", "info", {"js"},
        r"\bskip\s*:\s*(?!0\b)|\boffset\s*:\s*(?!0\b)|\.(?:skip|offset)\s*\(\s*(?!0\b)|\.range\s*\(",
        lambda m: "OFFSET pagination: deep pages read and discard every skipped row.",
        lambda m: "Use cursor/keyset pagination (Prisma `cursor` + `take`, or `WHERE (created_at, id) < (…)`).", "medium"),
    _qb("QB-COUNT-EXISTS", "low", {"php"},
        r"->\s*count\s*\(\s*\)\s*(?:>|>=|!=|!==|==|===)\s*[01]\b",
        lambda m: "COUNT used as an existence test: counts every match when one row is enough.",
        lambda m: "Use `->exists()` / `->doesntExist()`."),
    _qb("QB-COUNT-EXISTS", "low", {"java", "kotlin"},
        r"\b(count\w*)\s*\([^;{}()]*\)\s*(?:>|>=|!=)\s*0\b",
        lambda m: f"`{m.group(1)}(…) > 0` counts every match to answer a yes/no question.",
        lambda m: "Declare an `existsBy…` derived query (stops at the first row)."),
    _qb("QB-COUNT-EXISTS", "low", {"js"},
        r"\.count\s*\((?:[^;()]|\([^()]*\))*\)\s*\)?\s*(?:>|>=|!==|!=)\s*0\b",
        lambda m: "COUNT used as an existence test.",
        lambda m: "Use `findFirst({ select: { id: true } })` / `exists` (TypeORM) and test for null."),
    _qb("QB-COUNT-EXISTS", "low", {"python"},
        r"\.count\(\)\s*(?:>|>=|!=)\s*0\b",
        lambda m: "COUNT used as an existence test.",
        lambda m: "Use `.exists()`."),
    _qb("QB-SELECT-ALL-COLUMNS", "info", {"js"},
        r"\.select\s*\(\s*['\"]\*['\"]\s*\)",
        lambda m: "`select('*')` returns every column of the table.",
        lambda m: "List the columns (and embedded relations) the UI actually uses.", "medium"),
]


def ci_hint(engine):
    if engine == "mysql":
        return "MySQL `_ci` collations already compare case-insensitively: a plain equality uses the index — drop the case folding."
    return ("Postgres: expression index on `lower(col)` queried with `lower(col) = lower(:v)`, the `citext` type, or store a normalized column; "
            "for substring ILIKE use a `pg_trgm` GIN index.")


# ------------------------------------------------------------ literals in code

def literal_groups(code, strings, lang):
    """Merge string literals joined by concatenation into logical strings."""
    op = r"\." if lang == "php" else r"\+"
    pure = re.compile(r"^\s*" + op + r"\s*$" if lang != "python" else r"^\s*\+?\s*$")
    with_expr = re.compile(r"^\s*" + op + r"\s*[^;{}\n]{1,80}?\s*" + op + r"\s*$")
    groups = []
    cur = None
    for sp in strings:
        if cur is not None:
            gap = code[cur["end"]:sp.start]
            if pure.match(gap):
                cur["spans"].append(sp)
                cur["end"] = sp.end
                continue
            if with_expr.match(gap):
                cur["spans"].append(sp)
                cur["end"] = sp.end
                cur["built"] = "concatenation"
                cur["joins"].append(len(cur["spans"]) - 1)
                continue
            groups.append(cur)
        cur = {"spans": [sp], "end": sp.end, "built": "", "joins": []}
    if cur:
        groups.append(cur)
    return groups


def group_text(text, g):
    parts = []
    for i, sp in enumerate(g["spans"]):
        if i in g["joins"]:
            parts.append("?")
        parts.append(sp.content(text))
    return "".join(parts)


def interpolation(text, code, g, lang):
    first, last = g["spans"][0], g["spans"][-1]
    before = code[max(0, first.start - 40):first.start]
    after = code[last.end:last.end + 40]
    op = r"\." if lang == "php" else r"\+"
    if g["built"]:
        return g["built"]
    if re.match(r"\s*" + op + r"\s*[\w$(\[]", after) and lang not in ("sql",):
        return "concatenation"
    if re.search(r"[\w)\]]\s*" + op + r"\s*$", before) and lang not in ("sql",):
        return "concatenation"
    for sp in g["spans"]:
        content = sp.content(text)
        if lang == "php" and sp.quote == '"' and re.search(r"\$\w|\{\$", content):
            return "interpolation"
        if lang == "js" and sp.quote == "`" and "${" in content:
            prev = code[max(0, sp.start - 1):sp.start]
            if re.match(r"[\w$)\].]", prev):
                return ""  # tagged template (sql`…`, Prisma.sql`…`, $queryRaw`…`) -> parameterized
            return "interpolation"
        if lang == "python":
            prefix = code[max(0, sp.start - 2):sp.start].lower()
            if "f" in prefix and "{" in content:
                return "f-string"
        if lang == "kotlin" and re.search(r"\$\w|\$\{", content):
            return "interpolation"
        if lang == "ruby" and "#{" in content:
            return "interpolation"
        if lang == "csharp" and code[max(0, sp.start - 2):sp.start].endswith(("$", "$@", "@$")) and "{" in content:
            return "interpolation"
    if lang == "python" and re.match(r"\s*(%\s*[\w(]|\.format\s*\()", after):
        return "string formatting"
    if lang == "go" and re.search(r"Sprintf\s*\(\s*$", before) and re.search(r"%[sdvq]", "".join(sp.content(text) for sp in g["spans"])):
        return "fmt.Sprintf"
    if lang == "java" and (re.search(r"String\s*\.\s*format\s*\(\s*$", before) or re.match(r"\s*\.\s*formatted\s*\(", after)):
        return "String.format"
    return ""


def scan_code_file(path, lang, root, engine, findings):
    text = read_text(path)
    code, struct, strings = scan_source(text, lang)
    lines = LineIndex(text)
    rpath = rel(path, root)

    def emit(rule, sev, off, msg, hint, conf, fence="sql"):
        ln = lines.line(off)
        findings.append(Finding(rule, sev, rpath, ln, msg, conf, snippet_of(lines, ln), hint, {"lang": fence}))

    for g in literal_groups(code, strings, lang):
        sql = group_text(text, g)
        first = g["spans"][0]
        before = code[max(0, first.start - 60):first.start]
        in_raw_ctx = bool(RAW_CONTEXT.search(before))
        is_sql = bool(SQL_START.match(sql) or SQL_SHAPE.search(sql))
        if not (is_sql or in_raw_ctx):
            continue
        if not is_sql and not re.search(r"[A-Za-z_]\w*\)?\s*(=|<|>|\blike\b|\bin\b|\bis\b)|\b(and|or|order by|select|from|desc|asc)\b", sql, re.I):
            continue
        built = interpolation(text, code, g, lang)
        if built:
            emit("SQL-STRING-BUILT", "high", first.start,
                 f"SQL assembled with {built}: SQL-injection risk, and each distinct value is a new statement for the plan cache / prepared-statement pool.",
                 "Use bound parameters (`?`, `:name`, `$1`, `where('col', $v)`, Prisma `$queryRaw` tagged template, `createNativeQuery(sql).setParameter(...)`).",
                 "high", lang)
        if is_sql:
            for rule, sev, msg, hint, conf in sql_rules(sql, engine, built=bool(built)):
                emit(rule, sev, first.start, msg, hint, conf, lang)

    for r in QB_RULES:
        if lang not in r["langs"]:
            continue
        for m in r["rx"].finditer(code):
            if any(sp.content_start <= m.start() < sp.content_end for sp in strings) and r["rule"] not in ("QB-CONTAINS",):
                # match inside a string literal (e.g. JPQL text) is handled by the SQL rules
                if lang != "php":
                    continue
            hint = ci_hint(engine) if r["hint"] == "ci" else r["hint"](m)
            emit(r["rule"], r["sev"], m.start(), r["msg"](m), hint, r["conf"], lang)

    # merge CONTAINS + CASE-INSENSITIVE on the same line into one finding
    by_line = {}
    for f in findings:
        if f.file == rpath and f.rule in ("QB-CONTAINS", "QB-CASE-INSENSITIVE"):
            by_line.setdefault(f.line, []).append(f)
    for ln, fs in by_line.items():
        rules = {f.rule for f in fs}
        if rules == {"QB-CONTAINS", "QB-CASE-INSENSITIVE"}:
            for f in fs:
                if f.rule == "QB-CASE-INSENSITIVE":
                    findings.remove(f)
                else:
                    f.message += " It is also case-insensitive (ILIKE/lower()), so even a prefix index wouldn't help without a matching expression/trigram index."


def scan_sql_file(path, root, engine, findings):
    text = read_text(path)
    code, struct, _ = scan_source(text, "sql")
    lines = LineIndex(text)
    rpath = rel(path, root)
    start = 0
    for i, ch in enumerate(struct + ";"):
        if ch != ";":
            continue
        stmt = code[start:i]
        lead = len(stmt) - len(stmt.lstrip())
        off = start + lead
        start = i + 1
        body = stmt.strip()
        if not body:
            continue
        if re.match(r"(?i)(CREATE\s+(UNIQUE\s+)?INDEX|CREATE\s+(TEMP\w*\s+|UNLOGGED\s+)?TABLE|ALTER\s+TABLE|DROP\s|COPY\s|SET\s|GRANT\s|REVOKE\s|COMMENT\s|BEGIN|COMMIT|ROLLBACK|CREATE\s+(EXTENSION|SCHEMA|TYPE|SEQUENCE|DOMAIN|TRIGGER))", body):
            continue
        if re.match(r"(?i)INSERT\s+INTO\s+\S+\s*(\([^)]*\))?\s*VALUES", body):
            continue
        ln = lines.line(off)
        for rule, sev, msg, hint, conf in sql_rules(body, engine):
            findings.append(Finding(rule, sev, rpath, ln, msg, conf, norm(body)[:180], hint, {"lang": "sql"}))


def main():
    p = base_parser("db-doctor SQL analyzer: anti-patterns in SQL files, SQL strings and query builders (heuristic).")
    args = p.parse_args()
    root = os.path.abspath(args.root)
    if not os.path.exists(root):
        p.error(f"path not found: {root}")
    args.root = root
    engine, why = resolve_engine(args)
    findings = []
    n = 0
    for path, lang in iter_files(root):
        if lang == "prisma":
            continue
        n += 1
        try:
            if lang == "sql":
                scan_sql_file(path, root, engine, findings)
            else:
                scan_code_file(path, lang, root, engine, findings)
        except Exception as exc:
            findings.append(Finding("SCAN-ERROR", "info", rel(path, root), 1, f"Could not analyse file: {exc}", "low"))
    findings = filter_severity(sort_findings(dedupe(findings)), args.min_severity)
    meta = [("Root", root), ("Engine", f"{engine} ({why})"), ("Stacks detected", ", ".join(detect_stacks(root)) or "none"),
            ("Files scanned", str(n))]
    if args.json:
        print(to_json(meta, findings))
    else:
        print(render_markdown("db-doctor · SQL analyzer", meta, findings, args.limit))


if __name__ == "__main__":
    main()
