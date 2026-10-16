#!/usr/bin/env python3
"""db-doctor · ORM inspector.

Finds code that issues more (or heavier) queries than it looks like it does:
  * queries / writes / counts executed inside loops, callbacks and templates (N+1)
  * lazy relation access on loop variables (Eloquent, JPA/Hibernate, TypeORM lazy,
    Sequelize accessors, Django, SQLAlchemy)
  * Promise.all fan-out of per-item queries
  * risky mappings/config (EAGER to-many, open-session-in-view, no lazy-loading guard)
  * whole-table loads and in-memory filtering/counting of query results

Usage:
  python3 scan_orm.py <project-root> [--json] [--min-severity low]

Heuristic: every hit is a lead to verify by reading the code path (is the relation
already eager-loaded upstream? is the loop bounded to a handful of rows?).
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

sys.dont_write_bytecode = True  # don't litter the skill folder with __pycache__
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    CODE_LANGS, Finding, LineIndex, _glob_configs, _read_if, base_parser, dedupe, detect_stacks,
    filter_severity, iter_files, match_close, read_text, rel, render_markdown, resolve_engine,
    scan_source, snippet_of, sort_findings, statement_end, to_json,
)

# ============================================================== relation discovery

ELOQUENT_MODEL_RE = re.compile(r"class\s+(\w+)\s+extends\s+[\\\w]*(?:Model|Authenticatable|Pivot|User)\b")
ELOQUENT_REL_RE = re.compile(
    r"function\s+(\w+)\s*\([^)]*\)\s*(?::\s*[?\\\w]+\s*)?\{[^{}]*?\$this\s*->\s*"
    r"(hasMany|hasOne|belongsTo|belongsToMany|morphMany|morphOne|morphTo|morphToMany|morphedByMany|"
    r"hasManyThrough|hasOneThrough)\s*\(\s*(?:([\\\w]+)::class)?", re.S)
ELOQUENT_TO_MANY = {"hasMany", "belongsToMany", "morphMany", "morphToMany", "morphedByMany", "hasManyThrough"}

JPA_REL_RE = re.compile(r"@(OneToMany|ManyToMany|ManyToOne|OneToOne)\b")
JAVA_FIELD_RE = re.compile(r"(?:private|protected|public)\s+(?:final\s+|transient\s+)*([\w.<>, ?\[\]]+?)\s+(\w+)\s*(?:=[^;]*)?;")

TYPEORM_REL_RE = re.compile(r"@(OneToMany|ManyToMany|ManyToOne|OneToOne)\s*\(")
TS_PROP_AFTER_RE = re.compile(
    r"(?:\s*@\w+\s*\((?:[^()]|\([^()]*\))*\))*\s*(?:(?:public|private|protected|readonly|declare)\s+)*"
    r"(\w+)\s*[!?]?\s*:\s*(Promise\s*<)?")

DJANGO_REL_RE = re.compile(r"(\w+)\s*=\s*models\.(ForeignKey|OneToOneField|ManyToManyField)\s*\(([^)]*)\)")
SQLA_REL_RE = re.compile(r"(\w+)\s*(?::\s*Mapped\[[^\]]*\])?\s*=\s*(?:db\.)?relationship\s*\(([^)]*)\)")


@dataclass
class Relation:
    name: str
    many: bool
    owner: str = ""
    accessor: str = ""       # how code touches it (getter name for JPA)
    eager: bool = False      # mapped as eager / default-eager
    batch: bool = False      # @BatchSize or similar mitigation
    source: str = ""


@dataclass
class ProjectContext:
    root: str
    stacks: list
    eloquent: dict = field(default_factory=dict)      # relation name -> Relation
    eloquent_models: set = field(default_factory=set)
    jpa: dict = field(default_factory=dict)           # getter name -> Relation
    typeorm_lazy: dict = field(default_factory=dict)  # property -> Relation
    django: dict = field(default_factory=dict)
    sqlalchemy: dict = field(default_factory=dict)
    eager_mentions: dict = field(default_factory=dict)  # relation name -> ["file:line"]
    global_batch_fetch: bool = False
    views: dict = field(default_factory=dict)  # blade view name -> [(where, function, body)]


def _add_mention(ctx, name, where):
    lst = ctx.eager_mentions.setdefault(name, [])
    if where not in lst and len(lst) < 5:
        lst.append(where)


def _quoted_names(s):
    out = []
    for q in re.findall(r"['\"]([^'\"]+)['\"]", s):
        head = re.split(r"[.:]", q.strip())[0].strip()
        if re.fullmatch(r"\w+", head):
            out.append(head)
    return out


def discover(ctx: ProjectContext, files):
    for path, lang in files:
        try:
            text = read_text(path)
        except OSError:
            continue
        code, struct, _ = scan_source(text, lang)
        lines = LineIndex(text)
        where = lambda off: f"{rel(path, ctx.root)}:{lines.line(off)}"  # noqa: E731

        if lang == "php":
            if ELOQUENT_MODEL_RE.search(code):
                for m in ELOQUENT_MODEL_RE.finditer(code):
                    ctx.eloquent_models.add(m.group(1))
                owner = ELOQUENT_MODEL_RE.search(code).group(1)
                for m in ELOQUENT_REL_RE.finditer(code):
                    ctx.eloquent[m.group(1)] = Relation(m.group(1), m.group(2) in ELOQUENT_TO_MANY, owner,
                                                        m.group(1), source=where(m.start()))
                wm = re.search(r"protected\s+\$with\s*=\s*\[([^\]]*)\]", code)
                if wm:
                    for name in _quoted_names(wm.group(1)):
                        _add_mention(ctx, name, where(wm.start()) + " ($with)")
            for m in re.finditer(r"\b(?:with|load|loadMissing|withCount|loadCount|withWhereHas)\s*\(\s*(\[[^\]]*\]|['\"][^'\"]+['\"](?:\s*,\s*['\"][^'\"]+['\"])*)", code):
                for name in _quoted_names(m.group(1)):
                    _add_mention(ctx, name, where(m.start()))
            for m in re.finditer(r"\b(?:view|View::make)\s*\(\s*['\"]([\w.\-/:]+)['\"]", code):
                fn = None
                for fm in re.finditer(r"function\s+(\w+)\s*\([^)]*\)[^{;]*\{", struct[:m.start()]):
                    fn = fm
                if fn:
                    brace = fn.end() - 1
                    body = code[brace:match_close(struct, brace) + 1]
                    ctx.views.setdefault(m.group(1).replace("/", "."), []).append((where(fn.start()), fn.group(1), body))

        elif lang == "java":
            if "@Entity" in code or "@MappedSuperclass" in code:
                cm = re.search(r"class\s+(\w+)", code)
                owner = cm.group(1) if cm else ""
                for m in JPA_REL_RE.finditer(code):
                    args = ""
                    k = m.end()
                    while k < len(struct) and struct[k] in " \t":
                        k += 1
                    if k < len(struct) and struct[k] == "(":
                        close = match_close(struct, k)
                        args = code[k:close + 1]
                        k = close + 1
                    fm = JAVA_FIELD_RE.search(code, k)
                    if not fm:
                        continue
                    fname = fm.group(2)
                    block = code[m.start():fm.start()]
                    kind = m.group(1)
                    many = kind in ("OneToMany", "ManyToMany")
                    eager = "EAGER" in args or (not many and "LAZY" not in args)
                    getter = "get" + fname[0].upper() + fname[1:]
                    ctx.jpa[getter] = Relation(fname, many, owner, getter, eager=eager,
                                               batch="@BatchSize" in block, source=where(m.start()))
            for m in re.finditer(r"(?i)join\s+fetch\s+\w+\.(\w+)", code):
                _add_mention(ctx, m.group(1), where(m.start()))
            for m in re.finditer(r"attributePaths\s*=\s*(\{[^}]*\}|\"[^\"]*\")", code):
                for name in _quoted_names(m.group(1)):
                    _add_mention(ctx, name, where(m.start()))

        elif lang == "js":
            if "@Entity" in code:
                for m in TYPEORM_REL_RE.finditer(code):
                    close = match_close(struct, m.end() - 1)
                    pm = TS_PROP_AFTER_RE.match(code, close + 1)
                    if pm and pm.group(2):
                        ctx.typeorm_lazy[pm.group(1)] = Relation(pm.group(1), m.group(1) in ("OneToMany", "ManyToMany"),
                                                                 accessor=pm.group(1), source=where(m.start()))
            for m in re.finditer(r"\binclude\s*:\s*\{", struct):
                close = match_close(struct, m.end() - 1)
                body = code[m.end():close]
                for km in re.finditer(r"(?:^|[,{\s])(\w+)\s*:\s*(?:true|\{)", body):
                    _add_mention(ctx, km.group(1), where(m.start()))
            for m in re.finditer(r"\brelations\s*:\s*(\[[^\]]*\]|\{[^}]*\})", code):
                names = _quoted_names(m.group(1)) or re.findall(r"(\w+)\s*:\s*true", m.group(1))
                for name in names:
                    _add_mention(ctx, name, where(m.start()))
            for m in re.finditer(r"\b(?:left|inner)JoinAndSelect\s*\(\s*['\"]\w+\.(\w+)['\"]", code):
                _add_mention(ctx, m.group(1), where(m.start()))
            for m in re.finditer(r"\.select\s*\(\s*(['\"`])([^'\"`]*)\1", code):
                for em in re.finditer(r"(\w+)(?::\w+)?\s*\(", m.group(2)):
                    _add_mention(ctx, em.group(1), where(m.start()))

        elif lang == "python":
            for m in DJANGO_REL_RE.finditer(code):
                ctx.django[m.group(1)] = Relation(m.group(1), m.group(2) == "ManyToManyField", source=where(m.start()))
                rn = re.search(r"related_name\s*=\s*['\"](\w+)['\"]", m.group(3))
                if rn:
                    ctx.django[rn.group(1)] = Relation(rn.group(1), True, source=where(m.start()))
            for m in SQLA_REL_RE.finditer(code):
                eager = bool(re.search(r"lazy\s*=\s*['\"](joined|selectin|subquery|immediate)['\"]", m.group(2)))
                ctx.sqlalchemy[m.group(1)] = Relation(m.group(1), True, eager=eager, source=where(m.start()))
            for m in re.finditer(r"\b(?:select_related|prefetch_related)\s*\(([^)]*)\)", code):
                for name in _quoted_names(m.group(1)):
                    _add_mention(ctx, name.split("__")[0], where(m.start()))
            for m in re.finditer(r"\b(?:joinedload|selectinload|subqueryload)\s*\(\s*\w+\.(\w+)", code):
                _add_mention(ctx, m.group(1), where(m.start()))

    for path in _glob_configs(ctx.root, r"application[\w-]*\.(properties|ya?ml)|persistence\.xml"):
        if re.search(r"default_batch_fetch_size", _read_if(path)):
            ctx.global_batch_fetch = True


# ======================================================================= loops

@dataclass
class Loop:
    start: int
    body_start: int
    body_end: int
    var: str | None
    line: int
    label: str
    fanout: bool = False


def _skip_ws(s, k):
    while k < len(s) and s[k] in " \t\r\n":
        k += 1
    return k


def _header_then_body(lang, struct, open_idx):
    close = match_close(struct, open_idx)
    k = _skip_ws(struct, close + 1)
    if k < len(struct) and struct[k] == "{":
        return close, k + 1, match_close(struct, k)
    if lang == "php" and k < len(struct) and struct[k] == ":":
        m = re.compile(r"\bend(?:foreach|for|while)\b").search(struct, k)
        return close, k + 1, (m.start() if m else len(struct))
    return close, k, statement_end(struct, k)


PHP_LOOP = re.compile(r"\b(foreach|for|while)\s*\(")
PHP_CB = re.compile(r"->\s*(each|map|transform|flatMap|mapWithKeys|filter|reject|every|sortBy|sortByDesc|sum|groupBy|keyBy|contains|mapToGroups|mapSpread|eachSpread|partition)\s*\(|\barray_(?:map|filter|walk|reduce)\s*\(")
C_LOOP = re.compile(r"\b(for|while)\s*(?:await\s*)?\(")
JS_CB = re.compile(r"\.\s*(forEach|map|flatMap|filter|reduce|some|every|find|findIndex)\s*\(")
JAVA_CB = re.compile(r"\.\s*(forEach|map|flatMap|filter|peek|anyMatch|allMatch|noneMatch|mapToObj|mapToInt|mapToLong|mapToDouble|collect|reduce|sorted|distinct)\s*\(")
KT_CB = re.compile(r"\.\s*(forEach|map|flatMap|filter|mapNotNull|associate\w*|sumOf|onEach|forEachIndexed|mapIndexed|groupBy|any|all|none|count)\s*(\{|\()")
GO_LOOP = re.compile(r"\bfor\b")
PY_LOOP = re.compile(r"^([ \t]*)(?:async[ \t]+)?(for|while)\b.*:[ \t]*(?:#.*)?$", re.M)
PY_COMP = re.compile(r"\bfor\s+\(?\s*(\w+)(?:\s*,\s*(\w+))?\s*\)?\s+in\b")
BLADE_TOK = re.compile(r"@(foreach|forelse|for|while)\s*\(|@(endforeach|endforelse|endfor|endwhile)\b")


def _cb_var(lang, code, start):
    head = code[start:start + 160]
    if lang == "php":
        m = re.match(r"\s*(?:static\s+)?(?:function|fn)\s*\(\s*(?:[?\\\w]+\s+)?&?(\$\w+)", head)
    elif lang == "java":
        m = re.match(r"\s*\(?\s*(?:final\s+)?(?:[\w.<>\[\]]+\s+)?(\w+)\s*(?:,\s*\w+\s*)?\)?\s*->", head)
    elif lang == "kotlin":
        m = re.match(r"\s*\{?\s*(\w+)\s*->", head)
        return m.group(1) if m else "it"
    else:  # js
        m = re.match(r"\s*(?:async\s+)?(?:function\s*\w*\s*)?\(?\s*(\w+)", head)
        if m and m.group(1) in ("async", "function"):
            m = None
    return m.group(1) if m else None


def find_loops(lang, code, struct, lines):
    loops = []

    def add(start, bs, be, var, fanout=False):
        label = lines.line_text(lines.line(start)).strip()
        loops.append(Loop(start, bs, be, var, lines.line(start), label[:120], fanout))

    if lang == "blade":
        stack = []
        for m in BLADE_TOK.finditer(code):
            if m.group(1):
                open_idx = m.end() - 1
                close = match_close(code, open_idx)
                header = code[m.start():close + 1]
                vm = re.search(r"\bas\s+(?:\$\w+\s*=>\s*)?(\$\w+)", header)
                stack.append((m.start(), close + 1, vm.group(1) if vm else None))
            elif stack:
                start, bs, var = stack.pop()
                add(start, bs, m.start(), var)
        return loops

    if lang == "python":
        text_lines = code.split("\n")
        offsets = [0]
        for ln in text_lines:
            offsets.append(offsets[-1] + len(ln) + 1)
        for m in PY_LOOP.finditer(code):
            indent = len(m.group(1).expandtabs(4))
            li = code.count("\n", 0, m.start())
            end_li = li + 1
            while end_li < len(text_lines):
                ln = text_lines[end_li]
                if ln.strip() and len(ln) - len(ln.lstrip()) <= indent:
                    break
                end_li += 1
            vm = re.search(r"for\s+\(?\s*(\w+)(?:\s*,\s*(\w+))?", m.group(0))
            var = (vm.group(2) or vm.group(1)) if vm else None
            add(m.start(), offsets[li + 1] if li + 1 < len(offsets) else len(code), offsets[min(end_li, len(offsets) - 1)], var)
        for m in PY_COMP.finditer(code):
            line_start = code.rfind("\n", 0, m.start()) + 1
            if not code[line_start:m.start()].strip():
                continue
            depth, k = 0, m.start() - 1
            while k >= 0:
                ch = struct[k]
                if ch in ")]}":
                    depth += 1
                elif ch in "([{":
                    if depth == 0:
                        break
                    depth -= 1
                k -= 1
            if k >= 0:
                add(k, k + 1, match_close(struct, k), m.group(2) or m.group(1))
        return loops

    if lang == "go":
        for m in GO_LOOP.finditer(struct):
            brace = struct.find("{", m.end())
            if brace < 0:
                continue
            header = code[m.start():brace]
            vm = re.search(r"for\s+(?:(\w+)\s*,\s*)?(\w+)\s*:?=\s*range", header)
            add(m.start(), brace + 1, match_close(struct, brace), vm.group(2) if vm else None)
        return loops

    # brace languages: php, java, kotlin, js, csharp, ruby (partial)
    loop_re = PHP_LOOP if lang == "php" else C_LOOP
    for m in loop_re.finditer(struct):
        open_idx = m.end() - 1
        close, bs, be = _header_then_body(lang, struct, open_idx)
        header = code[m.start():close + 1]
        var = None
        if lang == "php":
            vm = re.search(r"\bas\s+(?:\$\w+\s*=>\s*)?&?(\$\w+)", header)
        elif lang == "java" or lang == "csharp":
            vm = re.search(r"\(\s*(?:final\s+)?(?:@\w+\s+)?[\w.<>\[\], ?]+?\s+(\w+)\s*(?::|\bin\b)", header)
        elif lang == "kotlin":
            vm = re.search(r"\(\s*(\w+)\s+in\b", header)
        else:
            vm = re.search(r"\(\s*(?:const|let|var)\s+(\w+)\s+(?:of|in)\b", header)
        if vm:
            var = vm.group(1)
        add(m.start(), bs, be, var)

    cb_re = {"php": PHP_CB, "java": JAVA_CB, "kotlin": KT_CB, "js": JS_CB}.get(lang)
    if cb_re:
        for m in cb_re.finditer(struct):
            open_idx = m.end() - 1
            if lang == "java":
                stmt_start = max(struct.rfind(";", 0, m.start()), struct.rfind("{", 0, m.start()),
                                 struct.rfind("}", 0, m.start()))
                prefix = code[stmt_start + 1:m.start()]
                if m.group(1) != "forEach" and not re.search(r"stream\s*\(\s*\)|Stream\s*\.|\.parallelStream", prefix):
                    continue
            close = match_close(struct, open_idx)
            var = _cb_var(lang, code, open_idx + 1)
            fanout = False
            if lang == "js":
                fanout = bool(re.search(r"Promise\s*\.\s*all(?:Settled)?\s*\(\s*[\w.$\s]*$", code[max(0, m.start() - 80):m.start()]))
            add(m.start(), open_idx + 1, close, var, fanout)
    return loops


# ================================================================ call patterns

PHP_FACADES = ("DB|Str|Arr|Carbon|CarbonImmutable|Cache|Log|Auth|Http|Route|View|Validator|Gate|Config|Session|"
               "Storage|Mail|Event|Queue|Hash|Crypt|Request|Response|App|Lang|URL|Redirect|Schema|Artisan|Bus|"
               "Notification|Password|RateLimiter|Blade|File|Cookie|Collection|Number|Date|Process|Context|"
               "Concurrency|Pipeline|Vite|Js|Inertia|Livewire|Sentry|Http|Broadcast|Pdf|Excel|Image|"
               "self|static|parent")
PHP_STATIC = re.compile(
    r"(?<![\w\\$>])(?!(?:" + PHP_FACADES + r")::)([A-Z]\w*)::(find\w*|where\w*|first\w*|create|forceCreate|"
    r"updateOrCreate|firstOrCreate|firstOrNew|insert\w*|upsert|query|select|with|withCount|count|exists|"
    r"doesntExist|pluck|value|sum|max|min|avg|all|get|latest|oldest|orderBy\w*|has|whereHas|destroy|"
    r"increment|decrement|paginate|simplePaginate|cursorPaginate)\s*\(")
PHP_DB = re.compile(r"\bDB::(table|select|selectOne|insert|update|delete|statement|scalar|unprepared|affectingStatement)\s*\(")
PHP_REL_QUERY = re.compile(
    r"(\$\w+)\s*->\s*(\w+)\s*\(\s*\)\s*->\s*(get|first|firstOrFail|count|exists|doesntExist|sum|avg|max|min|pluck|"
    r"value|paginate|where\w*|latest|oldest|orderBy\w*|find\w*|create\w*|attach|detach|sync\w*|toggle|"
    r"updateExistingPivot|save\w*|delete|update|increment|decrement|limit|take|skip|select|with)\s*\(")
PHP_WRITE = re.compile(
    r"(\$\w+)\s*->\s*(?:(save|saveQuietly)\s*\(\s*(?:\)|\[)|(update|updateQuietly)\s*\(\s*\[|"
    r"(delete|forceDelete|restore|touch|deleteQuietly|refresh)\s*\(\s*\)|(increment|decrement)\s*\(\s*['\"])")
PHP_LOAD = re.compile(r"(\$\w+)\s*->\s*(load|loadMissing|loadCount|fresh)\s*\(")
PHP_PROP = re.compile(r"(\$\w+)\s*->\s*(\w+)\b(?!\s*\()(?!\s*=[^=>])")
PHP_COUNT_AFTER = re.compile(r"\s*->\s*(count|isEmpty|isNotEmpty|sum|avg|max|min)\s*\(")

JAVA_REPO_READ = re.compile(r"\b(\w*(?:[Rr]epository|[Rr]epo|[Dd]ao|DAO))\s*\.\s*((?:find|get|read|query|search|stream|count|exists|load|fetch)\w*)\s*\(")
JAVA_REPO_WRITE = re.compile(r"\b(\w*(?:[Rr]epository|[Rr]epo|[Dd]ao|DAO))\s*\.\s*(save\w*|delete\w*|insert\w*|update\w*|persist|merge|remove\w*)\s*\(")
JAVA_MYBATIS = re.compile(r"\b(\w*Mapper)\s*\.\s*(select\w*|insert\w*|update\w*|delete\w*)\s*\(")
JAVA_EM = re.compile(r"\b(em|entityManager|session|getEntityManager\(\)|getSession\(\)|currentSession\(\))\s*\.\s*(find|getReference|createQuery|createNativeQuery|createNamedQuery|get|load|persist|merge|remove|refresh)\s*\(")
JAVA_JDBC = re.compile(r"\b(\w*(?:[Jj]dbc\w*|[Tt]emplate))\s*\.\s*(query\w*|update|batchUpdate|execute)\s*\(")
JAVA_GETTER = re.compile(r"\b(\w+)\s*\.\s*(get[A-Z]\w*)\s*\(\s*\)")

JS_PRISMA = re.compile(r"\b((?:this\.)?(?:prisma|tx|db|client|ctx\.prisma|ctx\.db|database))\s*\.\s*(\w+)\s*\.\s*(findUnique|findUniqueOrThrow|findFirst|findFirstOrThrow|findMany|count|aggregate|groupBy|create|createMany|createManyAndReturn|update|updateMany|upsert|delete|deleteMany)\s*\(")
JS_PRISMA_RAW = re.compile(r"\b(?:this\.)?(?:prisma|tx|db)\s*\.\s*\$(queryRaw|queryRawUnsafe|executeRaw|executeRawUnsafe)\b")
JS_TYPEORM = re.compile(r"\b(\w*(?:[Rr]epository|[Rr]epo|[Mm]anager|dataSource|queryRunner))\s*\.\s*(findOne\w*|find|findBy|findAndCount\w*|count\w*|exist\w*|query|createQueryBuilder|sum|average|minimum|maximum|save|insert|update|upsert|delete|remove|softDelete|softRemove|restore|increment|decrement)\s*\(")
JS_GETREPO = re.compile(r"\bgetRepository\s*\([^)]*\)\s*\.\s*(\w+)\s*\(")
JS_STATIC = re.compile(
    r"(?<![\w.$])(?!(?:Math|Object|Array|JSON|Promise|Number|Date|String|Reflect|Intl|Buffer|URL|Response|"
    r"Request|Headers|Map|Set|Symbol|BigInt|Error|Boolean|RegExp|Function|React|NextResponse|z|Z)\b)"
    r"([A-Z]\w*)\s*\.\s*(findOne|findByPk|findById|findAll|findAndCountAll|findOrCreate|count|countDocuments|"
    r"aggregate|create|bulkCreate|update|updateOne|updateMany|destroy|upsert|deleteOne|deleteMany|insertMany|find)\s*\(")
JS_SUPABASE = re.compile(r"\b(supabase\w*|sb)\s*\.\s*(from|rpc)\s*\(|\.from\s*\(\s*['\"`]\w+['\"`]\s*\)\s*\.\s*(?:select|insert|update|upsert|delete)\s*\(")
JS_DRIZZLE = re.compile(r"\bdb\s*\.\s*(select|insert|update|delete)\s*\(|\bdb\s*\.\s*query\s*\.\s*\w+\s*\.\s*(findMany|findFirst)\s*\(")
JS_SQLCLIENT = re.compile(r"\b(pool|client|db|conn|connection|knex|pgClient|mysql|pg)\s*\.\s*(query|execute|one|oneOrNone|many|manyOrNone|any|none|raw)\s*\(|\bsql\s*`")
JS_ACCESSOR = re.compile(r"\bawait\s+(\w+)\s*\.\s*(get[A-Z]\w*|\$get|\$count)\s*\(")
JS_LAZY_PROP = re.compile(r"\bawait\s+(\w+)\s*\.\s*(\w+)\b(?!\s*\()")
JS_INSTANCE_WRITE = re.compile(r"\b(\w+)\s*\.\s*(?:(save|destroy|reload)\s*\(\s*\)|(update|increment|decrement)\s*\(\s*[{'\"])")

PY_DJANGO = re.compile(r"\b([A-Z]\w*)\.objects\.(\w+)\s*\(")
PY_SQLA = re.compile(r"\b(?:self\.)?(?:session|db\.session|db|Session\(\))\s*\.\s*(query|execute|get|scalars|scalar|add|add_all|merge|delete|flush|commit|refresh)\s*\(")
PY_CURSOR = re.compile(r"\b(cursor|cur|conn|connection)\s*\.\s*(execute|executemany)\s*\(")
PY_RELMGR = re.compile(r"\b(\w+)\.(\w+)\.(all|filter|exclude|count|exists|first|last|aggregate|values|values_list|order_by|get)\s*\(")
PY_ATTR = re.compile(r"\b(\w+)\.(\w+)\b(?!\s*\()")
PY_SAVE = re.compile(r"\b(\w+)\.(save|delete)\s*\(")

GO_GORM = re.compile(r"\.\s*(First|Find|Take|Last|Count|Pluck|Scan|Create|CreateInBatches|Save|Update|Updates|UpdateColumn|UpdateColumns|Delete|FirstOrCreate|FirstOrInit|Exec|Raw)\s*\(")
GO_SQL = re.compile(r"\b(\w*(?:db|DB|Db|tx|Tx|conn|Conn|stmt|pool|Pool))\s*\.\s*(Query|QueryRow|QueryContext|QueryRowContext|Exec|ExecContext|Get|GetContext|Select|SelectContext|NamedExec\w*)\s*\(")

WRITE_WORDS = re.compile(r"^(create|forceCreate|updateOrCreate|firstOrCreate|insert\w*|upsert|destroy|increment|decrement|"
                         r"attach|detach|sync\w*|toggle|updateExistingPivot|save\w*|delete\w*|update\w*|persist|merge|"
                         r"remove\w*|bulkCreate|createMany\w*|deleteMany|insertMany|softDelete|softRemove|restore|"
                         r"add|add_all|flush|commit|statement|unprepared|affectingStatement|Create\w*|Save|Updates?|"
                         r"UpdateColumns?|Delete|Exec\w*|executeRaw\w*|executemany|batchUpdate|NamedExec\w*)$")
COUNT_WORDS = re.compile(r"^(count\w*|exists|exist\w*|doesntExist|sum|avg|average|max|min|minimum|maximum|aggregate|"
                         r"countDocuments|Count|scalar)$")


def _kind(method):
    if WRITE_WORDS.match(method):
        return "write"
    if COUNT_WORDS.match(method):
        return "count"
    return "query"


@dataclass
class Call:
    off: int
    kind: str          # query | write | count | lazy | lazy_count
    desc: str
    var: str | None = None
    relation: Relation | None = None
    stack: str = ""


def detect_calls(lang, code, ctx: ProjectContext):
    calls = []
    if lang in ("php", "blade"):
        for m in PHP_STATIC.finditer(code):
            if ctx.eloquent_models and m.group(1) not in ctx.eloquent_models and lang == "php":
                continue
            calls.append(Call(m.start(), _kind(m.group(2)), f"{m.group(1)}::{m.group(2)}()", stack="eloquent"))
        for m in PHP_DB.finditer(code):
            calls.append(Call(m.start(), _kind(m.group(1)) if m.group(1) != "table" else "query",
                              f"DB::{m.group(1)}()", stack="eloquent"))
        rel_query_spans = []
        for m in PHP_REL_QUERY.finditer(code):
            name = m.group(2)
            if ctx.eloquent and name not in ctx.eloquent:
                continue
            calls.append(Call(m.start(), _kind(m.group(3)), f"{m.group(1)}->{name}()->{m.group(3)}()",
                              m.group(1), ctx.eloquent.get(name), stack="eloquent"))
            rel_query_spans.append((m.start(), m.end()))
        for m in PHP_WRITE.finditer(code):
            method = next(g for g in m.groups()[1:] if g)
            calls.append(Call(m.start(), "write", f"{m.group(1)}->{method}()", m.group(1), stack="eloquent"))
        for m in PHP_LOAD.finditer(code):
            calls.append(Call(m.start(), "query", f"{m.group(1)}->{m.group(2)}()", m.group(1), stack="eloquent-load"))
        for m in PHP_PROP.finditer(code):
            name = m.group(2)
            if any(a <= m.start() < b for a, b in rel_query_spans):
                continue
            relation = ctx.eloquent.get(name)
            if relation is None:
                if ctx.eloquent:
                    continue
                if not re.match(r"\s*->\s*\w+", code[m.end():m.end() + 40]) or name in ("this",):
                    continue
                relation = Relation(name, False, source="(unknown: no Eloquent models parsed)")
            after = PHP_COUNT_AFTER.match(code, m.end())
            kind = "lazy_count" if (after and relation.many) else "lazy"
            calls.append(Call(m.start(), kind, f"{m.group(1)}->{name}", m.group(1), relation, stack="eloquent"))

    elif lang in ("java", "kotlin"):
        for rx in (JAVA_REPO_READ, JAVA_REPO_WRITE, JAVA_MYBATIS, JAVA_EM, JAVA_JDBC):
            for m in rx.finditer(code):
                method = m.group(2)
                if method in ("getClass", "getReferenceById", "getOne"):
                    continue
                calls.append(Call(m.start(), _kind(method), f"{m.group(1)}.{method}()", stack="jpa"))
        for m in JAVA_GETTER.finditer(code):
            relation = ctx.jpa.get(m.group(2))
            if not relation:
                continue
            tail = code[m.end():m.end() + 30]
            if not relation.many and re.match(r"\s*\.\s*getId\s*\(\s*\)", tail):
                continue  # FK id is available on the proxy without a query
            kind = "lazy_count" if relation.many and re.match(r"\s*\.\s*(size|isEmpty)\s*\(", tail) else "lazy"
            calls.append(Call(m.start(), kind, f"{m.group(1)}.{m.group(2)}()", m.group(1), relation, stack="jpa"))

    elif lang == "js":
        for m in JS_PRISMA.finditer(code):
            calls.append(Call(m.start(), _kind(m.group(3)), f"{m.group(1)}.{m.group(2)}.{m.group(3)}()", stack="prisma"))
        for m in JS_PRISMA_RAW.finditer(code):
            calls.append(Call(m.start(), "write" if "execute" in m.group(1) else "query", f"${m.group(1)}", stack="prisma"))
        for m in JS_TYPEORM.finditer(code):
            if m.group(1) in ("prisma", "tx"):
                continue
            calls.append(Call(m.start(), _kind(m.group(2)), f"{m.group(1)}.{m.group(2)}()", stack="typeorm"))
        for m in JS_GETREPO.finditer(code):
            calls.append(Call(m.start(), _kind(m.group(1)), f"getRepository(...).{m.group(1)}()", stack="typeorm"))
        for m in JS_STATIC.finditer(code):
            calls.append(Call(m.start(), _kind(m.group(2)), f"{m.group(1)}.{m.group(2)}()", stack="sequelize"))
        for m in JS_SUPABASE.finditer(code):
            stmt = code[m.start():m.start() + 400].split(";")[0]
            kind = "write" if re.search(r"\.\s*(insert|update|upsert|delete)\s*\(", stmt) else "query"
            calls.append(Call(m.start(), kind, "supabase.from(...)" if "from" in m.group(0) else "supabase.rpc()", stack="supabase"))
        for m in JS_DRIZZLE.finditer(code):
            calls.append(Call(m.start(), _kind(m.group(1) or m.group(2) or "query"), m.group(0).strip("( "), stack="drizzle"))
        for m in JS_SQLCLIENT.finditer(code):
            calls.append(Call(m.start(), "query", m.group(0).strip("( `"), stack="sql"))
        for m in JS_ACCESSOR.finditer(code):
            rname = m.group(2)[3:] if m.group(2).startswith("get") else m.group(2)
            rname = rname[:1].lower() + rname[1:]
            calls.append(Call(m.start(), "lazy", f"{m.group(1)}.{m.group(2)}()", m.group(1),
                              Relation(rname, rname.endswith("s")), stack="sequelize"))
        for m in JS_LAZY_PROP.finditer(code):
            relation = ctx.typeorm_lazy.get(m.group(2))
            if relation:
                calls.append(Call(m.start(), "lazy", f"await {m.group(1)}.{m.group(2)}", m.group(1), relation, stack="typeorm"))
        for m in JS_INSTANCE_WRITE.finditer(code):
            if m.group(1) in ("prisma", "tx", "db", "supabase") or re.match(r"[A-Z]", m.group(1)):
                continue
            calls.append(Call(m.start(), "write", f"{m.group(1)}.{m.group(2) or m.group(3)}()", m.group(1), stack="sequelize"))

    elif lang == "python":
        for m in PY_DJANGO.finditer(code):
            calls.append(Call(m.start(), _kind(m.group(2)), f"{m.group(1)}.objects.{m.group(2)}()", stack="django"))
        for m in PY_SQLA.finditer(code):
            calls.append(Call(m.start(), _kind(m.group(1)), f"session.{m.group(1)}()", stack="sqlalchemy"))
        for m in PY_CURSOR.finditer(code):
            calls.append(Call(m.start(), "query", f"{m.group(1)}.{m.group(2)}()", stack="sql"))
        relmgr_spans = []
        for m in PY_RELMGR.finditer(code):
            if m.group(2) == "objects":
                continue
            kind = "lazy_count" if m.group(3) in ("count", "exists") else "lazy"
            calls.append(Call(m.start(), kind, f"{m.group(1)}.{m.group(2)}.{m.group(3)}()", m.group(1),
                              Relation(m.group(2), True), stack="django"))
            relmgr_spans.append(m.start())
        known = {**ctx.django, **ctx.sqlalchemy}
        for m in PY_ATTR.finditer(code):
            relation = known.get(m.group(2))
            if relation and m.start() not in relmgr_spans and not relation.eager:
                calls.append(Call(m.start(), "lazy", f"{m.group(1)}.{m.group(2)}", m.group(1), relation, stack="python-orm"))
        for m in PY_SAVE.finditer(code):
            calls.append(Call(m.start(), "write", f"{m.group(1)}.{m.group(2)}()", m.group(1), stack="python-orm"))

    elif lang == "go":
        for m in GO_GORM.finditer(code):
            line_start = code.rfind("\n", 0, m.start()) + 1
            if re.search(r"\b\w*(?:db|DB|Db|tx|Tx|gorm|orm)\b\s*\.", code[line_start:m.start()]):
                calls.append(Call(m.start(), _kind(m.group(1)), f".{m.group(1)}()", stack="gorm"))
        for m in GO_SQL.finditer(code):
            calls.append(Call(m.start(), _kind(m.group(2)), f"{m.group(1)}.{m.group(2)}()", stack="database/sql"))
    return calls


# ======================================================================= hints

FIX_QUERY = {
    "eloquent": "Fetch before the loop: eager load (`->with('rel')`, `->withCount('rel')`, `->withSum(...)`) or one `whereIn(...)` query + `->keyBy()`/`->groupBy()` in memory.",
    "eloquent-load": "Call `->load()`/`->loadMissing()` once on the whole collection, not per model.",
    "jpa": "Load in one round-trip: `JOIN FETCH` / `@EntityGraph`, a repository method taking a collection (`findAllById`, `findByXIn`), or batch fetching (`hibernate.default_batch_fetch_size`).",
    "prisma": "Use `include`/`select` on the parent query, `_count` for counts, or one `findMany({ where: { id: { in: ids } } })` and map in memory.",
    "typeorm": "Use `relations` / `leftJoinAndSelect`, or one `findBy({ id: In(ids) })` and map in memory.",
    "sequelize": "Use `include` on the parent query, or one `findAll({ where: { id: ids } })` and map in memory.",
    "supabase": "Embed the relation in the parent select (`select('*, comments(*)')`) or query once with `.in('post_id', ids)`; every call here is also an HTTP round-trip.",
    "drizzle": "Use relational queries with `with: {...}` or one `inArray(...)` query.",
    "django": "Use `select_related` (FK/1-1) / `prefetch_related` (reverse/M2M) / `annotate(Count(...))`.",
    "sqlalchemy": "Use `selectinload`/`joinedload` options or one `IN` query.",
    "python-orm": "Use `select_related`/`prefetch_related` (Django) or `selectinload`/`joinedload` (SQLAlchemy).",
    "gorm": "Use `Preload`/`Joins`, or one `Where(\"id IN ?\", ids)` query.",
    "database/sql": "Replace the per-item query by one set-based query (`IN`, `ANY($1)`, JOIN).",
    "sql": "Replace the per-item query by one set-based query (`IN`, `ANY($1)`, JOIN).",
}
FIX_WRITE = {
    "eloquent": "Use one set-based statement (`Model::whereIn('id', $ids)->update([...])`), `upsert()`/`insert()` in chunks, inside a transaction.",
    "jpa": "Use a bulk JPQL `UPDATE ... WHERE id IN :ids` (@Modifying) or `saveAll` with `hibernate.jdbc.batch_size` + `order_inserts/order_updates`.",
    "prisma": "Use `updateMany`/`createMany`/`deleteMany`, or group the writes in `$transaction([...])`.",
    "typeorm": "Use `save(entities[])` with `chunk`, or a single `update()`/`insert()` with a QueryBuilder.",
    "sequelize": "Use `bulkCreate` (with `updateOnDuplicate`) or `Model.update(values, { where: { id: ids } })`.",
    "supabase": "Send one `.upsert([...])`/`.insert([...])` with all rows, or move the logic to an RPC (SQL function).",
    "django": "Use `bulk_create` / `bulk_update` / `QuerySet.update()`.",
    "sqlalchemy": "Use `session.execute(update(...).where(...))` or bulk operations; commit once.",
}


def _fix(stack, kind):
    table = FIX_WRITE if kind == "write" else FIX_QUERY
    return table.get(stack) or table.get(stack.split("-")[0]) or FIX_QUERY.get("sql")


# ===================================================================== scanning

def scan_file(path, lang, ctx: ProjectContext, findings):
    text = read_text(path)
    code, struct, _ = scan_source(text, lang)
    lines = LineIndex(text)
    rpath = rel(path, ctx.root)
    loops = find_loops(lang, code, struct, lines)
    calls = detect_calls(lang, code, ctx)
    blade_view = ""
    if lang == "blade":
        norm = path.replace(os.sep, "/")
        if "/views/" in norm:
            blade_view = norm.split("/views/", 1)[1][: -len(".blade.php")].replace("/", ".")

    def mk(rule, severity, off, message, confidence="high", hint="", **extra):
        ln = lines.line(off)
        findings.append(Finding(rule, severity, rpath, ln, message, confidence, snippet_of(lines, ln), hint,
                                dict(lang=lang, **extra)))

    for call in calls:
        containing = [L for L in loops if L.body_start <= call.off < L.body_end]
        if not containing:
            continue
        if call.kind in ("lazy", "lazy_count"):
            if not call.var:
                continue
            matching = [L for L in containing if L.var == call.var]
            if not matching:
                continue
            loop = max(matching, key=lambda L: L.body_start)
        else:
            loop = max(containing, key=lambda L: L.body_start)
        depth = len(containing)
        mult = "1 + N" if depth == 1 else "N × M (nested loops, depth %d)" % depth
        where = f"the loop at line {loop.line} `{loop.label}`"
        rname = call.relation.name if call.relation else ""

        if call.kind in ("lazy", "lazy_count"):
            mentions = ctx.eager_mentions.get(rname, [])
            conf = "low" if mentions else "medium"
            note = (f" Relation `{rname}` is eager-loaded at {', '.join(mentions)} — verify that load feeds this loop."
                    if mentions else " No eager loading of it was found in the project.")
            if lang == "blade" and blade_view in ctx.views:
                eager_rx = re.compile(r"\b(?:with|load|loadMissing|withCount|loadCount)\s*\([^;]*['\"]" + re.escape(rname) + r"\b")
                srcs = ctx.views[blade_view]
                loading = [f"{w} {fn}()" for w, fn, body in srcs if eager_rx.search(body)]
                if loading:
                    conf, note = "low", f" The action rendering this view eager-loads it ({', '.join(loading)})."
                else:
                    conf = "high"
                    note = " The action(s) rendering this view ({}) do not eager-load `{}`.".format(
                        ", ".join(f"{w} {fn}()" for w, fn, _ in srcs), rname)
            if call.relation and call.relation.batch:
                conf, note = "low", note + " Field has @BatchSize (queries are batched)."
            if lang in ("java", "kotlin") and ctx.global_batch_fetch:
                conf, note = "low", note + " `default_batch_fetch_size` is configured (lazy loads are batched)."
            if call.kind == "lazy_count":
                mk("ORM-LAZY-COUNT-IN-LOOP", "high", call.off,
                   f"`{call.desc}` loads the whole `{rname}` collection per row only to count/test it, in {where} → {mult} queries, plus every child row hydrated." + note,
                   conf, {"eloquent": "Use `withCount('%s')` and read `$model->%s_count`." % (rname, snake_name(rname)),
                          "jpa": "Use a COUNT query grouped by parent (or `@Formula`/DTO projection) instead of `.size()` on a lazy collection.",
                          "django": "Use `annotate(n=Count('%s'))`." % rname}.get(call.stack, "Aggregate in SQL (COUNT ... GROUP BY parent) in the parent query."))
            else:
                tail = ""
                if call.relation and lang in ("java", "kotlin") and not call.relation.many and call.relation.eager:
                    tail = " (@ManyToOne/@OneToOne is EAGER by default: Hibernate already fires one select per distinct parent when the list is loaded without JOIN FETCH.)"
                mk("ORM-LAZY-RELATION-IN-LOOP", "high", call.off,
                   f"Relation access `{call.desc}` on the loop variable of {where} → if `{rname}` isn't eager-loaded this is {mult} queries.{tail}" + note,
                   conf, _fix(call.stack, "query"))
            continue

        if loop.fanout:
            sev, conf = "high", "high"
            msg = f"`{call.desc}` inside `Promise.all(...map())` at {where}: N concurrent queries — still N round-trips and it can exhaust the connection pool."
            if call.stack == "prisma" and re.search(r"findUnique", call.desc):
                sev, conf = "low", "medium"
                msg += " Prisma batches `findUnique` calls issued in the same tick, so impact may be small — confirm with query logging."
            mk("ORM-QUERY-FANOUT", sev, call.off, msg, conf, _fix(call.stack, call.kind))
            continue

        if call.kind == "write":
            mk("ORM-WRITE-IN-LOOP", "medium" if depth == 1 else "high", call.off,
               f"Write `{call.desc}` inside {where}: one statement + round-trip per item ({mult}).",
               "high", _fix(call.stack, "write"))
        elif call.kind == "count":
            mk("ORM-COUNT-IN-LOOP", "high", call.off,
               f"Aggregate `{call.desc}` inside {where}: one COUNT/aggregate query per item ({mult}).",
               "high", {"eloquent": "Use `withCount()`/`withSum()` on the parent query, or one grouped query (`selectRaw('x_id, count(*)')->groupBy('x_id')`).",
                        "prisma": "Use `include: { _count: { select: { rel: true } } }` or one `groupBy` query.",
                        "jpa": "Use one grouped query (`SELECT p.id, COUNT(c) ... GROUP BY p.id`) into a map/DTO."}.get(call.stack, _fix(call.stack, "query")))
        else:
            mk("ORM-QUERY-IN-LOOP", "high", call.off,
               f"Query `{call.desc}` inside {where}: runs once per iteration → {mult} queries.",
               "high", _fix(call.stack, "query"))

    file_level_rules(lang, path, rpath, code, struct, lines, ctx, mk)


def snake_name(name):
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def file_level_rules(lang, path, rpath, code, struct, lines, ctx, mk):
    if lang == "php":
        if re.search(r"extends\s+(?:JsonResource|ResourceCollection)\b", code) and ctx.eloquent:
            for m in re.finditer(r"\$this\s*->\s*(\w+)\b(?!\s*\()", code):
                if m.group(1) in ctx.eloquent:
                    line_start = code.rfind("\n", 0, m.start()) + 1
                    line_end = code.find("\n", m.start())
                    if re.search(r"when(Loaded|Counted|Aggregated)", code[line_start:line_end]):
                        continue
                    mk("ORM-SERIALIZER-LAZY", "high", m.start(),
                       f"API Resource reads relation `{m.group(1)}` unconditionally: `Resource::collection($items)` triggers one query per item unless every caller eager-loads it.",
                       "medium", f"Use `$this->whenLoaded('{m.group(1)}')` (or `whenCounted`) and eager load in the controller.")
        for m in re.finditer(r"->\s*get\s*\(\s*\)\s*->\s*(count|first|isEmpty|isNotEmpty|sum|avg|max|min|where|filter|pluck|contains|last|take|sortBy\w*|unique)\s*\(", code):
            mk("ORM-IN-MEMORY", "medium", m.start(),
               f"`->get()->{m.group(1)}()` loads every matching row into PHP and then does the work in memory.",
               "high", "Let the database do it: `->count()`, `->exists()`, `->first()`, `->sum('col')`, `->where(...)` before `get()`.")
        for m in re.finditer(r"(?<![\w\\$>])(?!(?:" + PHP_FACADES + r")::)([A-Z]\w*)::all\s*\(\s*\)", code):
            if ctx.eloquent_models and m.group(1) not in ctx.eloquent_models:
                continue
            after = code[m.end():m.end() + 60]
            if re.match(r"\s*->\s*(count|first|isEmpty|isNotEmpty|sum|avg|max|min|where|filter|pluck|contains)\s*\(", after):
                mk("ORM-IN-MEMORY", "medium", m.start(), f"`{m.group(1)}::all()->...` loads the whole table to compute something the database can.", "high",
                   "Use a query (`Model::where(...)->count()`, `->exists()`, `->pluck()`, ...).")
            else:
                mk("ORM-UNBOUNDED-FETCH", "low", m.start(), f"`{m.group(1)}::all()` loads the whole table (no WHERE/LIMIT).", "medium",
                   "Fine for small lookup tables; otherwise paginate, `chunkById()`/`lazyById()` for batch jobs, or select only needed columns.")

    if lang in ("java", "kotlin"):
        if "@Entity" in code:
            for m in re.finditer(r"@(OneToMany|ManyToMany)\s*\(", struct):
                close = match_close(struct, m.end() - 1)
                if "EAGER" in code[m.end():close]:
                    mk("ORM-EAGER-TO-MANY", "high", m.start(),
                       f"`@{m.group(1)}(fetch = FetchType.EAGER)`: the collection is loaded every time the parent is, even when unused; with lists of parents it becomes N+1 or a huge join.",
                       "high", "Map it LAZY and fetch explicitly where needed (`JOIN FETCH`, `@EntityGraph`, batch fetching).")
            for m in re.finditer(r"@(ManyToOne|OneToOne)\b", struct):
                k = _skip_ws(struct, m.end())
                args = ""
                if k < len(struct) and struct[k] == "(":
                    args = code[k:match_close(struct, k) + 1]
                if "LAZY" not in args and "mappedBy" not in args:
                    mk("ORM-TO-ONE-EAGER-DEFAULT", "low", m.start(),
                       f"`@{m.group(1)}` without `fetch = FetchType.LAZY` is EAGER by default (JPA spec): loading a list of these entities fires extra selects for the association unless the query JOIN FETCHes it.",
                       "high", "Declare `fetch = FetchType.LAZY` and fetch explicitly in the queries that need it.")
        for m in re.finditer(r"\b\w*[Rr]epository\s*\.\s*findAll\s*\(\s*\)", code):
            after = code[m.end():m.end() + 80]
            if re.match(r"\s*\.\s*(stream\s*\(\s*\)\s*\.\s*(filter|count|anyMatch|noneMatch|allMatch|max|min|mapToInt|mapToLong|mapToDouble|sorted|limit|findFirst|findAny)|size\s*\(|isEmpty\s*\()", after):
                mk("ORM-IN-MEMORY", "medium", m.start(), "`findAll()` followed by in-memory filtering/counting: the whole table is loaded and hydrated as entities.",
                   "high", "Write a derived/`@Query` method that filters, counts or `exists` in SQL.")
            else:
                mk("ORM-UNBOUNDED-FETCH", "low", m.start(), "`findAll()` without paging loads and hydrates the whole table.", "medium",
                   "Fine for tiny tables; otherwise use `findAll(Pageable)`, a filtered query, or a projection.")

    if lang == "js":
        for m in re.finditer(r"\.\s*(findMany|findAll)\s*\(", struct):
            open_idx = m.end() - 1
            close = match_close(struct, open_idx)
            args = code[open_idx + 1:close]
            after = code[close + 1:close + 40]
            if re.match(r"\s*\)?\s*\.\s*(length|filter|find|some|every|reduce|slice)\b", after):
                mk("ORM-IN-MEMORY", "medium", m.start(), f"Result of `{m.group(1)}()` is filtered/counted in JavaScript: every row is fetched and deserialized first.",
                   "high", "Push it into the query: `count()`, `where`, `take`, `_count`, `aggregate`.")
            elif m.group(1) == "findMany" and not re.search(r"\b(take|first|last)\s*:", args):
                mk("ORM-UNBOUNDED-FETCH", "low", m.start(), "`findMany()` without `take`: returns every matching row.", "medium",
                   "Add `take` + cursor pagination, or confirm the result set is small by design.")
            elif m.group(1) == "findAll" and not args.strip():
                mk("ORM-UNBOUNDED-FETCH", "low", m.start(), "`findAll()` without `where`/`limit` loads the whole table.", "medium",
                   "Add `limit`/`offset` or keyset pagination, or confirm the table is small.")
        if "@Entity" in code:
            for m in TYPEORM_REL_RE.finditer(struct):
                close = match_close(struct, m.end() - 1)
                if re.search(r"eager\s*:\s*true", code[m.end():close]):
                    many = m.group(1) in ("OneToMany", "ManyToMany")
                    mk("ORM-EAGER-TO-MANY" if many else "ORM-EAGER-RELATION", "medium" if many else "low", m.start(),
                       f"`@{m.group(1)}(..., {{ eager: true }})` joins this relation into every `find*()` of the entity (ignored by QueryBuilder).",
                       "high", "Prefer explicit `relations` per query; eager to-many relations multiply result rows.")


def project_rules(ctx: ProjectContext, findings):
    root = ctx.root
    if "spring-jpa" in ctx.stacks:
        configs = list(_glob_configs(root, r"application[\w-]*\.(properties|ya?ml)"))
        joined = "\n".join(_read_if(p) for p in configs)
        target = rel(configs[0], root) if configs else "(no application.properties found)"
        if not re.search(r"open-in-view\s*[:=]\s*false", joined):
            findings.append(Finding(
                "ORM-OPEN-IN-VIEW", "medium", target, 1,
                "`spring.jpa.open-in-view` is not disabled (Spring Boot default: true). Lazy relations can then load from controllers/serializers (Jackson) — N+1 hidden outside the service layer — and a DB connection is held for the whole request.",
                "high", hint="Set `spring.jpa.open-in-view=false` and fetch what each endpoint needs explicitly (expect LazyInitializationException in places that relied on it)."))
        if not ctx.global_batch_fetch:
            findings.append(Finding(
                "ORM-NO-BATCH-FETCH", "info", target, 1,
                "No `hibernate.default_batch_fetch_size` configured: every uninitialized lazy association/collection is loaded with its own query.",
                "high", hint="`spring.jpa.properties.hibernate.default_batch_fetch_size=50` turns N lazy loads into ~N/50 IN-queries (a safety net, not a replacement for proper fetch plans)."))
    if "laravel" in ctx.stacks:
        guarded = False
        provider = None
        for path, lang in iter_files(root, {"php"}):
            if "/app/" not in path.replace(os.sep, "/") and "/bootstrap/" not in path.replace(os.sep, "/"):
                continue
            txt = _read_if(path)
            if re.search(r"preventLazyLoading|shouldBeStrict|automaticallyEagerLoadRelationships", txt):
                guarded = True
                break
            if path.endswith("AppServiceProvider.php"):
                provider = path
        if not guarded:
            findings.append(Finding(
                "ORM-LAZY-GUARD-MISSING", "info", rel(provider, root) if provider else "app/Providers/AppServiceProvider.php", 1,
                "Lazy loading is not guarded: N+1 queries fail silently.",
                "high", hint="In `AppServiceProvider::boot()`: `Model::preventLazyLoading(! app()->isProduction());` (or `Model::shouldBeStrict()`), so N+1 throws in dev/tests."))


def main():
    p = base_parser("db-doctor ORM inspector: N+1 and risky ORM usage (heuristic).")
    args = p.parse_args()
    root = os.path.abspath(args.root)
    if not os.path.exists(root):
        p.error(f"path not found: {root}")
    args.root = root
    files = list(iter_files(root, CODE_LANGS))
    ctx = ProjectContext(root=root, stacks=detect_stacks(root))
    discover(ctx, files)
    findings = []
    for path, lang in files:
        try:
            scan_file(path, lang, ctx, findings)
        except Exception as exc:  # keep going on unparseable files
            findings.append(Finding("SCAN-ERROR", "info", rel(path, root), 1, f"Could not analyse file: {exc}", "low"))
    project_rules(ctx, findings)
    findings = filter_severity(sort_findings(dedupe(findings)), args.min_severity)

    engine, why = resolve_engine(args)
    rels = []
    if ctx.eloquent:
        rels.append("Eloquent: " + ", ".join(f"{r.owner}.{n}({'many' if r.many else 'one'})" for n, r in sorted(ctx.eloquent.items())))
    if ctx.jpa:
        rels.append("JPA: " + ", ".join(f"{r.owner}.{r.name}({'many' if r.many else 'one'}{', EAGER' if r.eager else ''})" for r in ctx.jpa.values()))
    if ctx.typeorm_lazy:
        rels.append("TypeORM lazy: " + ", ".join(sorted(ctx.typeorm_lazy)))
    meta = [
        ("Root", root),
        ("Stacks detected", ", ".join(ctx.stacks) or "none"),
        ("Engine", f"{engine} ({why})"),
        ("Files scanned", str(len(files))),
        ("Relations mapped", " · ".join(rels) or "none"),
        ("Eager-load sites found", ", ".join(f"{k}→{len(v)}" for k, v in sorted(ctx.eager_mentions.items())) or "none"),
    ]
    if args.json:
        print(to_json(meta, findings, eager_mentions=ctx.eager_mentions))
    else:
        print(render_markdown("db-doctor · ORM inspector", meta, findings, args.limit))


if __name__ == "__main__":
    main()
