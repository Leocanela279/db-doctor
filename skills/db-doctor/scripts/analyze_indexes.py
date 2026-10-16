#!/usr/bin/env python3
"""db-doctor · Index advisor & inspector.

1. Builds an index inventory from the schema sources it finds:
     SQL DDL / schema dumps / Flyway / Supabase / Prisma-migration .sql files,
     Laravel migrations, schema.prisma, JPA entities, TypeORM entities, Sequelize migrations.
2. Reports: exact duplicates, left-prefix redundant indexes, foreign keys without a supporting
   index, low-selectivity single-column indexes, wide indexes, tables with many indexes.
3. Scans the code for the columns queries filter / join / sort on (Eloquent, Spring Data derived
   queries, Prisma, TypeORM, Sequelize, Supabase, raw SQL) and suggests indexes for access paths
   that no index serves ("hot columns").

Usage:
  python3 analyze_indexes.py <project-root> [--engine postgres|mysql] [--json] [--inventory]
  python3 analyze_indexes.py schema.sql --engine postgres     # a pg_dump/mysqldump --no-data file

Static analysis cannot know table sizes or real usage: "unused" is never claimed here (needs
pg_stat_user_indexes / sys.schema_unused_indexes) and every suggestion must be checked with EXPLAIN.
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field

sys.dont_write_bytecode = True  # don't litter the skill folder with __pycache__
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    Finding, LineIndex, base_parser, filter_severity, iter_files, match_close, natural_key, norm_ident,
    pluralize, read_text, rel, render_markdown, resolve_engine, scan_source, snake, sort_findings,
    split_top_level, statement_end, to_json,
)

# =========================================================================== model

@dataclass
class Index:
    table: str
    columns: list
    name: str = ""
    unique: bool = False
    primary: bool = False
    method: str = "btree"
    where: str = ""
    include: list = field(default_factory=list)
    family: str = ""
    source: str = ""
    sources: list = field(default_factory=list)

    def full_key(self):
        return (self.table, tuple(self.columns), self.unique or self.primary, self.primary,
                re.sub(r"\s+", "", self.where.lower()), self.method, tuple(self.include))

    def shape_key(self):
        return (self.table, tuple(self.columns), re.sub(r"\s+", "", self.where.lower()), self.method, tuple(self.include))

    def kind(self):
        return "PRIMARY KEY" if self.primary else ("UNIQUE" if self.unique else ("INDEX" if self.method == "btree" else self.method.upper()))

    def label(self):
        cols = ", ".join(c[5:] if c.startswith("expr:") else c for c in self.columns)
        extra = f" WHERE {self.where}" if self.where else ""
        inc = f" INCLUDE ({', '.join(self.include)})" if self.include else ""
        nm = f" `{self.name}`" if self.name else ""
        return f"{self.kind()}{nm} {self.table}({cols}){inc}{extra}"


@dataclass
class ForeignKey:
    table: str
    columns: list
    ref_table: str
    ref_columns: list
    family: str
    source: str
    name: str = ""


class Schema:
    def __init__(self):
        self.tables = defaultdict(dict)      # table -> {column: type}
        self.indexes = []
        self.fks = []
        self.files = defaultdict(list)       # family -> [files]
        self.entity_table = {}               # entity/model class -> table
        self.field_column = {}               # (entity, field) -> column
        self.relation_fields = {}            # (entity, field) -> target entity
        self.relation_used = set()           # (table, column) loaded by ORM relations
        self.pending_fks = []                # (table, col, target_entity, family, source, unique)
        self.prisma_lists = []               # (parent_model, child_model)
        self.notes = []
        self.relation_mode_prisma = False

    def col(self, table, column, typ=""):
        t = self.tables[table]
        if column and (column not in t or (typ and not t[column])):
            t[column] = typ

    def add_index(self, table, columns, family, source, **kw):
        if not columns:
            return
        self.tables.setdefault(table, {})
        self.indexes.append(Index(table=table, columns=[c.lower() for c in columns], family=family, source=source, **kw))

    def add_fk(self, table, columns, ref_table, ref_columns, family, source, name=""):
        self.fks.append(ForeignKey(table, [c.lower() for c in columns], ref_table, [c.lower() for c in ref_columns or ["id"]], family, source, name))

    def drop_index(self, name, table=None):
        name = norm_ident(name)
        self.indexes = [i for i in self.indexes if not (i.name.lower() == name and (table is None or i.table == table))]
        self.fks = [f for f in self.fks if not (f.name.lower() == name and (table is None or f.table == table))]

    def drop_pk(self, table):
        self.indexes = [i for i in self.indexes if not (i.table == table and i.primary)]

    def drop_table(self, table):
        self.indexes = [i for i in self.indexes if i.table != table]
        self.fks = [f for f in self.fks if f.table != table]
        self.tables.pop(table, None)

    def drop_column(self, table, column):
        column = column.lower()
        self.indexes = [i for i in self.indexes if not (i.table == table and column in i.columns)]
        self.fks = [f for f in self.fks if not (f.table == table and column in f.columns)]
        self.tables.get(table, {}).pop(column, None)


def parse_cols(inner: str):
    cols = []
    for part in split_top_level(inner):
        p = part.strip()
        if not p:
            continue
        m = re.match(r'^[`"\[]?([A-Za-z_]\w*)[`"\]]?\s*(?:\(\s*\d+\s*\))?(?:\s+(?:ASC|DESC|NULLS\s+(?:FIRST|LAST)|COLLATE\s+\S+|[\w.]+_ops))*\s*$', p, re.I)
        cols.append(m.group(1).lower() if m else "expr:" + re.sub(r"\s+", "", p.lower()))
    return cols


def ident_list(s: str):
    return [norm_ident(x) for x in s.split(",") if x.strip()]


# ===================================================================== SQL DDL

CT_RE = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:GLOBAL|LOCAL)\s+)?(?:TEMP(?:ORARY)?\s+|UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"`\[\]]+)\s*\(", re.I)
CI_RE = re.compile(r"CREATE\s+(UNIQUE\s+)?(?:(FULLTEXT|SPATIAL)\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(?:(?!ON\s)([\w.\"`\[\]]+)\s+)?(?:USING\s+\w+\s+)?ON\s+(?:ONLY\s+)?([\w.\"`\[\]]+)\s*(?:USING\s+(\w+)\s*)?\(", re.I)
AT_RE = re.compile(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?([\w.\"`\[\]]+)\s+(.*)$", re.I | re.S)
DI_RE = re.compile(r"DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?([\w.\"`\[\]]+(?:\s*,\s*[\w.\"`\[\]]+)*)(?:\s+ON\s+([\w.\"`\[\]]+))?", re.I)
DT_RE = re.compile(r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.\"`\[\]]+(?:\s*,\s*[\w.\"`\[\]]+)*)", re.I)
REF_RE = re.compile(r"\bREFERENCES\s+([\w.\"`\[\]]+)\s*(?:\(([^)]*)\))?", re.I)


def _column_def(schema, table, cdef, family, src):
    m = re.match(r'^([`"\[]?[A-Za-z_]\w*[`"\]]?)\s+(.*)$', cdef, re.S)
    if not m:
        return
    col = norm_ident(m.group(1))
    rest = m.group(2)
    tm = re.match(r"([A-Za-z]+(?:\s+(?:VARYING|PRECISION|UNSIGNED|WITH(?:OUT)?\s+TIME\s+ZONE))?(?:\s*\([^)]*\))?)", rest)
    typ = tm.group(1).lower() if tm else ""
    if re.match(r"tinyint\s*\(\s*1\s*\)|bool", typ):
        typ = "boolean"
    elif typ.startswith("enum"):
        typ = "enum"
    schema.col(table, col, typ)
    if re.search(r"\bPRIMARY\s+KEY\b", rest, re.I):
        schema.add_index(table, [col], family, src, primary=True, unique=True, name=f"{table}_pkey")
    elif re.search(r"\bUNIQUE\b", rest, re.I):
        schema.add_index(table, [col], family, src, unique=True, name=f"{table}_{col}_key")
    r = REF_RE.search(rest)
    if r:
        schema.add_fk(table, [col], norm_ident(r.group(1)), ident_list(r.group(2) or "id"), family, src, f"{table}_{col}_fkey")


def _table_element(schema, table, el, family, src):
    e = el.strip()
    if not e or re.match(r"(?i)(CHECK|EXCLUDE|LIKE|PERIOD)\b|CONSTRAINT\s+\S+\s+(CHECK|EXCLUDE)\b", e):
        return
    m = re.match(r'(?is)^(?:CONSTRAINT\s+([\w"`]+)\s+)?PRIMARY\s+KEY\s*(?:USING\s+\w+\s*)?\((.*)\)', e)
    if m:
        schema.add_index(table, parse_cols(m.group(2)), family, src, primary=True, unique=True, name=norm_ident(m.group(1) or f"{table}_pkey"))
        return
    m = re.match(r'(?is)^(?:CONSTRAINT\s+([\w"`]+)\s+)?UNIQUE(?:\s+(?:KEY|INDEX))?(?:\s+(?!\()([\w"`]+))?\s*(?:USING\s+\w+\s*)?\((.*?)\)', e)
    if m:
        schema.add_index(table, parse_cols(m.group(3)), family, src, unique=True, name=norm_ident(m.group(1) or m.group(2) or ""))
        return
    m = re.match(r'(?is)^(?:CONSTRAINT\s+([\w"`]+)\s+)?FOREIGN\s+KEY\s*(?:[\w"`]+\s*)?\(([^)]*)\)\s*REFERENCES\s+([\w."`\[\]]+)\s*(?:\(([^)]*)\))?', e)
    if m:
        schema.add_fk(table, ident_list(m.group(2)), norm_ident(m.group(3)), ident_list(m.group(4) or "id"), family, src, norm_ident(m.group(1) or ""))
        return
    m = re.match(r'(?is)^(FULLTEXT|SPATIAL)\s+(?:KEY|INDEX)?\s*([\w"`]+)?\s*\((.*)\)', e)
    if m:
        schema.add_index(table, parse_cols(m.group(3)), family, src, method=m.group(1).lower(), name=norm_ident(m.group(2) or ""))
        return
    m = re.match(r'(?is)^(?:KEY|INDEX)\s+(?:([\w"`]+)\s*)?(?:USING\s+\w+\s*)?\((.*)\)', e)
    if m:
        schema.add_index(table, parse_cols(m.group(2)), family, src, name=norm_ident(m.group(1) or ""))
        return
    _column_def(schema, table, e, family, src)


def parse_sql(text, family, rpath, schema, line_offset=0):
    code, struct, _ = scan_source(text, "sql")
    lines = LineIndex(text)
    start = 0
    for i, ch in enumerate(struct + ";"):
        if ch != ";":
            continue
        stmt, sstruct = code[start:i], struct[start:i]
        lead = len(stmt) - len(stmt.lstrip())
        src = f"{rpath}:{lines.line(start + lead) + line_offset}"
        start = i + 1
        s = stmt.strip()
        ss = sstruct.strip()
        if not s:
            continue
        m = CT_RE.match(s)
        if m:
            table = norm_ident(m.group(1))
            open_idx = m.end() - 1
            close = match_close(ss, open_idx)
            schema.tables.setdefault(table, {})
            for el in split_top_level(s[open_idx + 1:close]):
                _table_element(schema, table, el, family, src)
            continue
        m = CI_RE.match(s)
        if m:
            table = norm_ident(m.group(4))
            open_idx = m.end() - 1
            close = match_close(ss, open_idx)
            tail = s[close + 1:]
            inc = re.search(r"\bINCLUDE\s*\(([^)]*)\)", tail, re.I)
            whr = re.search(r"\bWHERE\s+(.+)$", tail, re.I | re.S)
            method = (m.group(5) or m.group(2) or "btree").lower()
            schema.add_index(table, parse_cols(s[open_idx + 1:close]), family, src, unique=bool(m.group(1)),
                             method=method, name=norm_ident(m.group(3) or ""),
                             where=(whr.group(1).strip() if whr else ""), include=ident_list(inc.group(1)) if inc else [])
            continue
        m = AT_RE.match(s)
        if m:
            table = norm_ident(m.group(1))
            for act in split_top_level(m.group(2)):
                a = act.strip()
                am = re.match(r"(?is)^ADD\s+(.*)$", a)
                if am:
                    body = am.group(1).strip()
                    if re.match(r"(?i)(CONSTRAINT|PRIMARY|UNIQUE|FOREIGN|INDEX|KEY|FULLTEXT|SPATIAL)\b", body):
                        _table_element(schema, table, body, family, src)
                    else:
                        _column_def(schema, table, re.sub(r"(?i)^COLUMN\s+(IF\s+NOT\s+EXISTS\s+)?", "", body), family, src)
                    continue
                dm = re.match(r'(?is)^DROP\s+(?:INDEX|KEY)\s+([\w"`]+)', a)
                if dm:
                    schema.drop_index(dm.group(1), table)
                    continue
                dm = re.match(r'(?is)^DROP\s+(?:CONSTRAINT|FOREIGN\s+KEY)\s+(?:IF\s+EXISTS\s+)?([\w"`]+)', a)
                if dm:
                    schema.drop_index(dm.group(1), table)
                    continue
                if re.match(r"(?i)^DROP\s+PRIMARY\s+KEY", a):
                    schema.drop_pk(table)
                    continue
                dm = re.match(r'(?is)^DROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?([\w"`]+)', a)
                if dm:
                    schema.drop_column(table, norm_ident(dm.group(1)))
            continue
        m = DI_RE.match(s)
        if m:
            for name in m.group(1).split(","):
                schema.drop_index(name, norm_ident(m.group(2)) if m.group(2) else None)
            continue
        m = DT_RE.match(s)
        if m:
            for t in m.group(1).split(","):
                schema.drop_table(norm_ident(t))


# ================================================================= Laravel

def _str_args(args):
    return re.findall(r"['\"]([^'\"]+)['\"]", args)


def _cols_arg(arg):
    a = arg.strip()
    if a.startswith("["):
        return _str_args(a)
    s = _str_args(a)
    return s[:1]


def _laravel_name(table, cols, suffix):
    return re.sub(r"[-.]", "_", f"{table}_{'_'.join(cols)}_{suffix}").lower()


CALL_RE = re.compile(r"->\s*(\w+)\s*\(((?:[^()]|\((?:[^()]|\([^()]*\))*\))*)\)")


def laravel_stmt(schema, table, stmt, src):
    calls = CALL_RE.findall(stmt)
    if not calls:
        return
    method, args = calls[0]
    mods = calls[1:]
    parts = split_top_level(args)
    fam = "laravel"
    if method in ("index", "unique", "primary", "fullText", "fulltext", "spatialIndex"):
        cols = _cols_arg(parts[0]) if parts else []
        name = _str_args(parts[1])[0] if len(parts) > 1 and _str_args(parts[1]) else ""
        suffix = {"index": "index", "unique": "unique", "primary": "primary"}.get(method, method.lower())
        schema.add_index(table, cols, fam, src, unique=method in ("unique", "primary"), primary=method == "primary",
                         method="btree" if method in ("index", "unique", "primary") else ("fulltext" if method.lower() == "fulltext" else "spatial"),
                         name=name or _laravel_name(table, cols, suffix))
        return
    if method == "rawIndex":
        s = _str_args(args)
        if s:
            schema.add_index(table, ["expr:" + re.sub(r"\s+", "", s[0].lower())], fam, src, name=s[1] if len(s) > 1 else "")
        return
    if method == "foreign":
        cols = _cols_arg(parts[0]) if parts else []
        ref_cols, ref_table = ["id"], ""
        for mm, ma in mods:
            if mm == "references":
                ref_cols = _cols_arg(ma) or ["id"]
            elif mm == "on":
                ref_table = (_str_args(ma) or [""])[0]
        if cols and ref_table:
            schema.add_fk(table, cols, ref_table.lower(), ref_cols, fam, src, _laravel_name(table, cols, "foreign"))
        return
    if method in ("dropIndex", "dropUnique", "dropPrimary", "dropFullText", "dropSpatialIndex"):
        if method == "dropPrimary":
            schema.drop_pk(table)
            return
        a = parts[0].strip() if parts else ""
        if a.startswith("["):
            suffix = {"dropIndex": "index", "dropUnique": "unique"}.get(method, "fulltext")
            schema.drop_index(_laravel_name(table, _str_args(a), suffix), table)
        elif _str_args(a):
            schema.drop_index(_str_args(a)[0], table)
        return
    if method == "dropForeign":
        a = parts[0].strip() if parts else ""
        cols = _str_args(a)
        if a.startswith("["):
            schema.fks = [f for f in schema.fks if not (f.table == table and f.columns == [c.lower() for c in cols])]
        elif cols:
            schema.drop_index(cols[0], table)
        return
    if method in ("dropColumn", "dropColumns"):
        for c in _str_args(args):
            schema.drop_column(table, c)
        return
    if method == "dropMorphs":
        n = (_str_args(args) or [""])[0]
        schema.drop_column(table, f"{n}_type")
        schema.drop_column(table, f"{n}_id")
        return
    if method == "dropConstrainedForeignId":
        for c in _str_args(args)[:1]:
            schema.drop_column(table, c)
        return
    if method in ("dropTimestamps", "dropSoftDeletes", "renameColumn", "renameIndex", "dropRememberToken"):
        return

    # column definitions
    s_args = _str_args(args)
    if method in ("morphs", "nullableMorphs", "uuidMorphs", "nullableUuidMorphs", "ulidMorphs", "nullableUlidMorphs"):
        n = s_args[0] if s_args else ""
        schema.col(table, f"{n}_type", "string")
        schema.col(table, f"{n}_id", "")
        schema.add_index(table, [f"{n}_type", f"{n}_id"], fam, src, name=s_args[1] if len(s_args) > 1 else _laravel_name(table, [f"{n}_type", f"{n}_id"], "index"))
        return
    if method == "timestamps" or method == "timestampsTz" or method == "nullableTimestamps":
        schema.col(table, "created_at", "timestamp")
        schema.col(table, "updated_at", "timestamp")
        return
    if method in ("softDeletes", "softDeletesTz"):
        schema.col(table, s_args[0] if s_args else "deleted_at", "timestamp")
        return
    if method == "rememberToken":
        schema.col(table, "remember_token", "string")
        return
    if method == "foreignIdFor":
        mm = re.search(r"([A-Z]\w*)::class", args)
        col = s_args[0] if s_args else (snake(mm.group(1)) + "_id" if mm else "")
    elif method == "id" or method.endswith("Increments") or method == "increments":
        col = s_args[0] if s_args else "id"
    else:
        col = s_args[0] if s_args else ""
    if not col:
        return
    typ = {"boolean": "boolean", "enum": "enum"}.get(method, method.lower())
    schema.col(table, col, typ)
    if method == "id" or method == "increments" or method.endswith("Increments"):
        schema.add_index(table, [col], fam, src, primary=True, unique=True, name=f"{table}_pkey")
    is_change = any(mm == "change" for mm, _ in mods)
    for mm, ma in mods:
        mname = (_str_args(ma) or [""])[0]
        if mm == "primary":
            schema.add_index(table, [col], fam, src, primary=True, unique=True, name=f"{table}_pkey")
        elif mm == "unique":
            schema.add_index(table, [col], fam, src, unique=True, name=mname or _laravel_name(table, [col], "unique"))
        elif mm == "index":
            schema.add_index(table, [col], fam, src, name=mname or _laravel_name(table, [col], "index"))
        elif mm in ("fullText", "fulltext"):
            schema.add_index(table, [col], fam, src, method="fulltext", name=mname)
        elif mm == "constrained" and not is_change:
            pa = split_top_level(ma)
            ref_table = (_str_args(pa[0])[0] if pa and _str_args(pa[0]) else "")
            if not ref_table:
                if method == "foreignIdFor":
                    mm2 = re.search(r"([A-Z]\w*)::class", args)
                    ref_table = pluralize(snake(mm2.group(1))) if mm2 else ""
                else:
                    ref_table = pluralize(re.sub(r"_id$", "", col))
            ref_col = _str_args(pa[1])[0] if len(pa) > 1 and _str_args(pa[1]) else "id"
            schema.add_fk(table, [col], ref_table.lower(), [ref_col], fam, src, _laravel_name(table, [col], "foreign"))
        elif mm == "references":
            ref_col = (_str_args(ma) or ["id"])[0]
            on = next(((_str_args(a2) or [""])[0] for m2, a2 in mods if m2 == "on"), "")
            if on:
                schema.add_fk(table, [col], on.lower(), [ref_col], fam, src, _laravel_name(table, [col], "foreign"))


def parse_laravel(text, rpath, schema):
    code, struct, strings = scan_source(text, "php")
    lines = LineIndex(text)
    um = re.search(r"function\s+up\s*\([^)]*\)\s*(?::\s*\w+\s*)?\{", struct)
    if not um:
        return False
    ub = um.end() - 1
    ue = match_close(struct, ub)
    events = []
    for m in re.finditer(r"Schema::(?:connection\([^)]*\)\s*->\s*)?(create|table)\s*\(\s*['\"](\w+)['\"]\s*,\s*(?:static\s+)?function\s*\(\s*(?:Blueprint\s+)?\$(\w+)\s*\)\s*(?:use\s*\([^)]*\)\s*)?\{", code[:ue]):
        if m.start() < ub:
            continue
        events.append((m.start(), "block", m))
    for m in re.finditer(r"Schema::(?:drop|dropIfExists)\s*\(\s*['\"](\w+)['\"]", code[:ue]):
        if m.start() > ub:
            events.append((m.start(), "drop", m))
    for sp in strings:
        if ub < sp.start < ue and re.match(r"(?is)\s*(CREATE|ALTER|DROP)\s", sp.content(text)):
            events.append((sp.start, "sql", sp))
    for off, kind, m in sorted(events, key=lambda e: e[0]):
        if kind == "drop":
            schema.drop_table(m.group(1).lower())
        elif kind == "sql":
            parse_sql(m.content(text), "laravel", rpath, schema, line_offset=lines.line(m.start) - 1)
        else:
            table = m.group(2).lower()
            schema.tables.setdefault(table, {})
            var = m.group(3)
            bb = m.end() - 1
            be = match_close(struct, bb)
            pos = bb + 1
            while pos < be:
                end = statement_end(struct, pos)
                end = min(end, be)
                stmt = code[pos:end].strip()
                if stmt.startswith("$" + var):
                    lead = len(code[pos:end]) - len(code[pos:end].lstrip())
                    laravel_stmt(schema, table, stmt, f"{rpath}:{lines.line(pos + lead)}")
                pos = end + 1
    return True


# =================================================================== Prisma

def parse_prisma(text, rpath, schema):
    code, struct, _ = scan_source(text, "prisma")
    lines = LineIndex(text)
    ds = re.search(r"datasource\s+\w+\s*\{([^}]*)\}", code)
    if ds and re.search(r'relationMode\s*=\s*"prisma"', ds.group(1)):
        schema.relation_mode_prisma = True
    enums = set(re.findall(r"^\s*enum\s+(\w+)\s*\{", code, re.M))
    models = []
    for m in re.finditer(r"^\s*(?:model|view)\s+(\w+)\s*\{", code, re.M):
        brace = m.end() - 1
        close = match_close(struct, brace)
        models.append((m.group(1), code[brace + 1:close], brace + 1))
    names = {n for n, _, _ in models}
    info = {}
    for name, body, off in models:
        tm = re.search(r'@@map\(\s*"([^"]+)"', body)
        table = (tm.group(1) if tm else name).lower()
        fields = {}
        for fm in re.finditer(r"^[ \t]*(\w+)[ \t]+(\w+)(\[\])?(\?)?[ \t]*(.*)$", body, re.M):
            cm = re.search(r'@map\(\s*"([^"]+)"', fm.group(5))
            fields[fm.group(1)] = dict(col=(cm.group(1) if cm else fm.group(1)).lower(), type=fm.group(2),
                                       list=bool(fm.group(3)), attrs=fm.group(5), off=off + fm.start())
        info[name] = (table, fields)
        schema.entity_table[name] = table
    for name, body, off in models:
        table, fields = info[name]
        schema.tables.setdefault(table, {})
        for fname, f in fields.items():
            src = f"{rpath}:{lines.line(f['off'])}"
            if f["type"] in names:
                schema.relation_fields[(name, fname)] = f["type"]
                rm = re.search(r"@relation\(([^)]*)\)", f["attrs"])
                if rm and re.search(r"fields\s*:", rm.group(1)):
                    fl = re.search(r"fields\s*:\s*\[([^\]]*)\]", rm.group(1))
                    rl = re.search(r"references\s*:\s*\[([^\]]*)\]", rm.group(1))
                    cols = [fields[x.strip()]["col"] for x in fl.group(1).split(",") if x.strip() in fields]
                    tgt_table, tgt_fields = info[f["type"]]
                    refs = [tgt_fields[x.strip()]["col"] for x in (rl.group(1).split(",") if rl else ["id"]) if x.strip() in tgt_fields]
                    schema.add_fk(table, cols, tgt_table, refs or ["id"], "prisma", src, f"{table}_{'_'.join(cols)}_fkey")
                    if re.search(r"@unique\b", " ".join(fields[x.strip()]["attrs"] for x in fl.group(1).split(",") if x.strip() in fields)):
                        pass
                elif f["list"]:
                    schema.prisma_lists.append((name, f["type"]))
                continue
            typ = "boolean" if f["type"] == "Boolean" else ("enum" if f["type"] in enums else f["type"].lower())
            schema.col(table, f["col"], typ)
            schema.field_column[(name, fname)] = f["col"]
            if re.search(r"@id\b", f["attrs"]):
                schema.add_index(table, [f["col"]], "prisma", src, primary=True, unique=True, name=f"{table}_pkey")
            if re.search(r"@unique\b", f["attrs"]):
                schema.add_index(table, [f["col"]], "prisma", src, unique=True, name=f"{table}_{f['col']}_key")
        for bm in re.finditer(r"@@(id|unique|index|fulltext)\s*\(\s*(?:fields\s*:\s*)?\[([^\]]*)\]([^\n]*)", body):
            cols = []
            for raw in split_top_level(bm.group(2)):
                fn = re.match(r"\s*(\w+)", raw)
                if fn:
                    cols.append(fields.get(fn.group(1), {}).get("col", fn.group(1).lower()))
            rest = bm.group(3)
            nm = re.search(r'(?:map|name)\s*:\s*"([^"]+)"', rest)
            tp = re.search(r"type\s*:\s*(\w+)", rest)
            kind = bm.group(1)
            src = f"{rpath}:{lines.line(off + bm.start())}"
            default = f"{table}_{'_'.join(cols)}_" + {"id": "pkey", "unique": "key", "index": "idx", "fulltext": "idx"}[kind]
            schema.add_index(table, cols, "prisma", src, unique=kind in ("id", "unique"), primary=kind == "id",
                             method="fulltext" if kind == "fulltext" else (tp.group(1).lower() if tp else "btree"),
                             name=nm.group(1) if nm else default)


# ===================================================================== JPA

JAVA_FIELD = re.compile(r"(?:private|protected|public)\s+((?:static\s+|final\s+|transient\s+)*)([\w.<>, ?\[\]]+?)\s+(\w+)\s*(?:=[^;]*)?;")


def _prev_boundary(struct, i):
    depth = 0
    k = i - 1
    while k >= 0:
        ch = struct[k]
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
        elif depth == 0 and ch in ";{}":
            return k
        k -= 1
    return -1


def parse_jpa(text, rpath, schema):
    code, struct, _ = scan_source(text, "java")
    if "@Entity" not in code:
        return False
    lines = LineIndex(text)
    cm = re.search(r"\bclass\s+(\w+)", code)
    if not cm:
        return False
    cls = cm.group(1)
    table = snake(cls)
    tm = re.search(r"@Table\s*\(", struct)
    targs = ""
    if tm and tm.start() < cm.start():
        targs = code[tm.end() - 1:match_close(struct, tm.end() - 1) + 1]
        top = re.sub(r"@\w+\s*\((?:[^()]|\([^()]*\))*\)", "", targs)
        nm = re.search(r'\bname\s*=\s*"([^"]+)"', top)
        if nm:
            table = nm.group(1)
    table = table.lower()
    schema.entity_table[cls] = table
    schema.tables.setdefault(table, {})
    tsrc = f"{rpath}:{lines.line(tm.start() if tm else cm.start())}"
    fields_cols = {}
    for fm in JAVA_FIELD.finditer(code, cm.end()):
        if "static" in fm.group(1):
            continue
        ftype, fname = fm.group(2).strip(), fm.group(3)
        ann = code[_prev_boundary(struct, fm.start()) + 1:fm.start()]
        if "@Transient" in ann:
            continue
        src = f"{rpath}:{lines.line(fm.start())}"
        relm = re.search(r"@(ManyToOne|OneToOne|OneToMany|ManyToMany)\b", ann)
        cn = re.search(r'@Column\s*\([^)]*\bname\s*=\s*"([^"]+)"', ann)
        if relm:
            kind = relm.group(1)
            gm = re.search(r"<\s*(\w+)\s*>", ftype)
            target = gm.group(1) if gm else ftype.split(".")[-1]
            schema.relation_fields[(cls, fname)] = target
            if kind in ("ManyToOne", "OneToOne") and "mappedBy" not in ann:
                jc = re.search(r'@JoinColumn\s*\([^)]*\bname\s*=\s*"([^"]+)"', ann)
                col = (jc.group(1) if jc else snake(fname) + "_id").lower()
                schema.col(table, col, "")
                schema.field_column[(cls, fname)] = col
                fields_cols[fname] = col
                schema.pending_fks.append((table, col, target, "jpa", src, kind == "OneToOne"))
            elif kind == "OneToMany":
                mb = re.search(r'mappedBy\s*=\s*"(\w+)"', ann)
                if mb:
                    schema.pending_fks.append(("__used__", mb.group(1), target, "jpa", src, False))
            elif kind == "ManyToMany":
                jt = re.search(r'@JoinTable\s*\((.*)\)', ann, re.S)
                if jt:
                    jn = re.search(r'\bname\s*=\s*"([^"]+)"', re.sub(r"@JoinColumn\s*\([^)]*\)", "", jt.group(1)))
                    jcs = re.findall(r'joinColumns\s*=\s*\{?\s*@JoinColumn\s*\([^)]*name\s*=\s*"([^"]+)"', jt.group(1))
                    ijcs = re.findall(r'inverseJoinColumns\s*=\s*\{?\s*@JoinColumn\s*\([^)]*name\s*=\s*"([^"]+)"', jt.group(1))
                    if jn and jcs and ijcs:
                        jtab = jn.group(1).lower()
                        schema.tables.setdefault(jtab, {})
                        schema.add_fk(jtab, [jcs[0]], table, ["id"], "jpa", src)
                        schema.pending_fks.append((jtab, ijcs[0].lower(), target, "jpa", src, False))
                        if re.match(r"\s*(java\.util\.)?Set\b", ftype):
                            schema.add_index(jtab, [jcs[0], ijcs[0]], "jpa", src, primary=True, unique=True, name=f"{jtab}_pkey")
            continue
        col = (cn.group(1) if cn else snake(fname)).lower()
        fields_cols[fname] = col
        typ = "boolean" if ftype in ("boolean", "Boolean") else ("enum" if "@Enumerated" in ann else ftype.lower())
        schema.col(table, col, typ)
        schema.field_column[(cls, fname)] = col
        if re.search(r"@Id\b", ann):
            schema.add_index(table, [col], "jpa", src, primary=True, unique=True, name=f"{table}_pkey")
        if re.search(r"@Column\s*\([^)]*unique\s*=\s*true", ann):
            schema.add_index(table, [col], "jpa", src, unique=True)
    for im in re.finditer(r"@Index\s*\(((?:[^()]|\([^()]*\))*)\)", targs):
        a = im.group(1)
        cl = re.search(r'columnList\s*=\s*"([^"]*)"', a)
        if not cl:
            continue
        cols = [snake(re.sub(r"(?i)\s+(ASC|DESC)$", "", c.strip())) for c in cl.group(1).split(",") if c.strip()]
        nm = re.search(r'name\s*=\s*"([^"]*)"', a)
        schema.add_index(table, cols, "jpa", tsrc, unique=bool(re.search(r"unique\s*=\s*true", a)), name=nm.group(1) if nm else "")
    for um in re.finditer(r"@UniqueConstraint\s*\(((?:[^()]|\([^()]*\))*)\)", targs):
        cols = re.findall(r'"([^"]+)"', re.search(r"columnNames\s*=\s*(\{[^}]*\}|\"[^\"]*\")", um.group(1)).group(1)) if re.search(r"columnNames", um.group(1)) else []
        nm = re.search(r'name\s*=\s*"([^"]*)"', um.group(1))
        schema.add_index(table, [snake(c) for c in cols], "jpa", tsrc, unique=True, name=nm.group(1) if nm else "")
    return True


# ================================================================= TypeORM

TS_PROP = re.compile(r"((?:@\w+\s*\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)\s*)+)(?:(?:public|private|protected|readonly|declare)\s+)*(\w+)\s*[!?]?\s*:\s*([^;=\n]+)")


def parse_typeorm(text, rpath, schema):
    code, struct, _ = scan_source(text, "js")
    em = re.search(r"@Entity\s*\(([^)]*)\)", code)
    if not em or "typeorm" not in code:
        return False
    lines = LineIndex(text)
    cm = re.search(r"\bclass\s+(\w+)", code[em.end():])
    if not cm:
        return False
    cls = cm.group(1)
    cls_off = em.end() + cm.start()
    tn = re.search(r"['\"](\w+)['\"]", em.group(1))
    table = (tn.group(1) if tn else snake(cls)).lower()
    schema.entity_table[cls] = table
    schema.tables.setdefault(table, {})
    brace = struct.find("{", cls_off)
    body_end = match_close(struct, brace)
    props = {}
    for pm in TS_PROP.finditer(code, brace + 1, body_end):
        decos, prop, ptype = pm.group(1), pm.group(2), pm.group(3).strip()
        src = f"{rpath}:{lines.line(pm.start(2))}"
        relm = re.search(r"@(ManyToOne|OneToOne|OneToMany|ManyToMany)\s*\(\s*(?:\(\s*\)\s*=>\s*|['\"])(\w+)", decos)
        if relm:
            schema.relation_fields[(cls, prop)] = relm.group(2)
            jc = re.search(r"@JoinColumn\s*\(\s*\{[^}]*name\s*:\s*['\"](\w+)['\"]", decos)
            if relm.group(1) == "ManyToOne" or (relm.group(1) == "OneToOne" and "@JoinColumn" in decos):
                col = (jc.group(1) if jc else prop + "Id").lower()
                schema.col(table, col, "")
                props[prop] = col
                schema.field_column[(cls, prop)] = col
                schema.pending_fks.append((table, col, relm.group(2), "typeorm", src, relm.group(1) == "OneToOne"))
            continue
        nm = re.search(r"name\s*:\s*['\"](\w+)['\"]", decos)
        col = (nm.group(1) if nm else prop).lower()
        props[prop] = col
        schema.field_column[(cls, prop)] = col
        typ = "boolean" if re.match(r"boolean\b", ptype) or re.search(r"type\s*:\s*['\"](boolean|bool)['\"]", decos) else ptype.lower()
        if "enum" in decos.lower():
            typ = "enum"
        schema.col(table, col, typ)
        if re.search(r"@Primary(Generated)?Column\b", decos):
            schema.add_index(table, [col], "typeorm", src, primary=True, unique=True, name=f"{table}_pkey")
        if re.search(r"@Column\s*\([^)]*unique\s*:\s*true", decos):
            schema.add_index(table, [col], "typeorm", src, unique=True)
        im = re.search(r"@Index\s*\(([^)]*)\)", decos)
        if im:
            schema.add_index(table, [col], "typeorm", src, unique=bool(re.search(r"unique\s*:\s*true", im.group(1))))
    head = code[max(0, em.start() - 600):cls_off]
    for im in re.finditer(r"@(Index|Unique)\s*\(\s*(?:['\"](\w+)['\"]\s*,\s*)?\[([^\]]*)\]\s*(?:,\s*\{([^}]*)\})?\s*\)", head):
        cols = [props.get(p, p.lower()) for p in re.findall(r"['\"](\w+)['\"]", im.group(3))]
        schema.add_index(table, cols, "typeorm", f"{rpath}:{lines.line(max(0, em.start() - 600) + im.start())}",
                         unique=im.group(1) == "Unique" or bool(re.search(r"unique\s*:\s*true", im.group(4) or "")),
                         name=im.group(2) or "")
    return True


# =============================================================== Sequelize

def _obj_keys(body):
    keys = {}
    depth = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        elif depth == 0:
            m = re.match(r"['\"]?(\w+)['\"]?\s*:\s*", body[i:])
            if m and (i == 0 or body[i - 1] in " \t\n,{"):
                vstart = i + m.end()
                vend = vstart
                d2 = 0
                while vend < len(body):
                    c2 = body[vend]
                    if c2 in "{[(":
                        d2 += 1
                    elif c2 in "}])":
                        if d2 == 0:
                            break
                        d2 -= 1
                    elif c2 == "," and d2 == 0:
                        break
                    vend += 1
                keys[m.group(1)] = body[vstart:vend].strip()
                i = vend
                continue
        i += 1
    return keys


def parse_sequelize(text, rpath, schema):
    code, struct, strings = scan_source(text, "js")
    if "queryInterface" not in code and "QueryInterface" not in code:
        return False
    lines = LineIndex(text)
    um = re.search(r"\bup\s*(?::\s*)?(?:async\s*)?(?:function\s*)?\(([^)]*)\)\s*(?:=>\s*)?\{", struct)
    ub, ue = (um.end() - 1, match_close(struct, um.end() - 1)) if um else (0, len(code))
    body = code[ub:ue]
    sbody = struct[ub:ue]
    fam = "sequelize"

    def src(off):
        return f"{rpath}:{lines.line(ub + off)}"

    for m in re.finditer(r"\.createTable\s*\(\s*['\"](\w+)['\"]\s*,\s*\{", body):
        table = m.group(1).lower()
        schema.tables.setdefault(table, {})
        ob = m.end() - 1
        oe = match_close(sbody, ob)
        for col, val in _obj_keys(body[ob + 1:oe]).items():
            c = col.lower()
            typ = "boolean" if re.search(r"BOOLEAN", val) else ("enum" if "ENUM" in val else "")
            schema.col(table, c, typ)
            if re.search(r"primaryKey\s*:\s*true", val):
                schema.add_index(table, [c], fam, src(m.start()), primary=True, unique=True, name=f"{table}_pkey")
            if re.search(r"unique\s*:\s*true", val):
                schema.add_index(table, [c], fam, src(m.start()), unique=True)
            rm = re.search(r"references\s*:\s*\{[^}]*model\s*:\s*(?:\{[^}]*tableName\s*:\s*)?['\"](\w+)['\"][^}]*?(?:key\s*:\s*['\"](\w+)['\"])?", val)
            if rm:
                schema.add_fk(table, [c], rm.group(1).lower(), [rm.group(2) or "id"], fam, src(m.start()))
    for m in re.finditer(r"\.addColumn\s*\(\s*['\"](\w+)['\"]\s*,\s*['\"](\w+)['\"]\s*,\s*\{", body):
        table, c = m.group(1).lower(), m.group(2).lower()
        ob = m.end() - 1
        val = body[ob:match_close(sbody, ob) + 1]
        schema.col(table, c, "boolean" if "BOOLEAN" in val else "")
        rm = re.search(r"references\s*:\s*\{[^}]*model\s*:\s*['\"](\w+)['\"]", val)
        if rm:
            schema.add_fk(table, [c], rm.group(1).lower(), ["id"], fam, src(m.start()))
        if re.search(r"unique\s*:\s*true", val):
            schema.add_index(table, [c], fam, src(m.start()), unique=True)
    for m in re.finditer(r"\.addIndex\s*\(\s*['\"](\w+)['\"]\s*,\s*", body):
        table = m.group(1).lower()
        rest = body[m.end():]
        srest = sbody[m.end():]
        cols, opts = [], ""
        if rest.startswith("["):
            close = match_close(srest, 0)
            cols = re.findall(r"['\"](\w+)['\"]", rest[:close])
            om = re.match(r"\s*,\s*\{", rest[close + 1:])
            if om:
                o = close + 1 + om.end() - 1
                opts = rest[o:match_close(srest, o) + 1]
        elif rest.startswith("{"):
            opts = rest[:match_close(srest, 0) + 1]
            fm = re.search(r"fields\s*:\s*\[([^\]]*)\]", opts)
            cols = re.findall(r"['\"](\w+)['\"]", fm.group(1)) if fm else []
        nm = re.search(r"name\s*:\s*['\"](\w+)['\"]", opts)
        schema.add_index(table, cols, fam, src(m.start()), unique=bool(re.search(r"unique\s*:\s*true", opts)),
                         name=nm.group(1) if nm else "", where="partial" if re.search(r"\bwhere\s*:", opts) else "")
    for m in re.finditer(r"\.addConstraint\s*\(\s*['\"](\w+)['\"]\s*,\s*\{", body):
        table = m.group(1).lower()
        ob = m.end() - 1
        o = body[ob:match_close(sbody, ob) + 1]
        fm = re.search(r"fields\s*:\s*\[([^\]]*)\]", o)
        cols = re.findall(r"['\"](\w+)['\"]", fm.group(1)) if fm else []
        tp = re.search(r"type\s*:\s*['\"]([\w ]+)['\"]", o)
        nm = re.search(r"name\s*:\s*['\"](\w+)['\"]", o)
        t = (tp.group(1).lower() if tp else "")
        if t == "foreign key":
            rt = re.search(r"table\s*:\s*['\"](\w+)['\"]", o)
            rf = re.search(r"field\s*:\s*['\"](\w+)['\"]", o)
            if rt:
                schema.add_fk(table, cols, rt.group(1).lower(), [rf.group(1) if rf else "id"], fam, src(m.start()), nm.group(1) if nm else "")
        elif t in ("unique", "primary key"):
            schema.add_index(table, cols, fam, src(m.start()), unique=True, primary=t == "primary key", name=nm.group(1) if nm else "")
    for m in re.finditer(r"\.removeIndex\s*\(\s*['\"](\w+)['\"]\s*,\s*(['\"](\w+)['\"]|\[[^\]]*\])", body):
        table = m.group(1).lower()
        if m.group(3):
            schema.drop_index(m.group(3), table)
        else:
            cols = [c.lower() for c in re.findall(r"['\"](\w+)['\"]", m.group(2))]
            schema.indexes = [i for i in schema.indexes if not (i.table == table and i.columns == cols)]
    for m in re.finditer(r"\.removeColumn\s*\(\s*['\"](\w+)['\"]\s*,\s*['\"](\w+)['\"]", body):
        schema.drop_column(m.group(1).lower(), m.group(2))
    for m in re.finditer(r"\.dropTable\s*\(\s*['\"](\w+)['\"]", body):
        schema.drop_table(m.group(1).lower())
    for sp in strings:
        if ub < sp.start < ue and re.match(r"(?is)\s*(CREATE|ALTER|DROP)\s", sp.content(text)):
            parse_sql(sp.content(text), fam, rpath, schema, line_offset=lines.line(sp.start) - 1)
    return True


def parse_js_migration_sql(text, rpath, schema, family):
    """TypeORM/Knex/node-pg-migrate migrations that run raw SQL in up()."""
    code, struct, strings = scan_source(text, "js")
    um = re.search(r"\bup\s*\(([^)]*)\)\s*(?::\s*[\w<>]+\s*)?\{", struct)
    if not um:
        return False
    ub = um.end() - 1
    ue = match_close(struct, ub)
    lines = LineIndex(text)
    found = False
    for sp in strings:
        if ub < sp.start < ue and re.match(r"(?is)\s*(CREATE|ALTER|DROP)\s", sp.content(text)):
            parse_sql(sp.content(text), family, rpath, schema, line_offset=lines.line(sp.start) - 1)
            found = True
    return found


# ============================================================ schema loading

def load_schema(root):
    schema = Schema()
    files = list(iter_files(root, {"sql", "php", "prisma", "java", "js"}, max_bytes=25_000_000))
    files.sort(key=lambda f: natural_key(f[0]))
    for path, lang in files:
        rpath = rel(path, root)
        norm = path.replace(os.sep, "/")
        try:
            if lang == "sql":
                text = read_text(path)
                if re.search(r"(?i)\b(CREATE|ALTER|DROP)\s+(UNIQUE\s+)?(TABLE|INDEX)\b", text):
                    parse_sql(text, "sql", rpath, schema)
                    schema.files["sql"].append(rpath)
            elif lang == "prisma":
                parse_prisma(read_text(path), rpath, schema)
                schema.files["prisma"].append(rpath)
            elif lang == "php" and "/migrations/" in norm:
                text = read_text(path)
                if "Schema::" in text and parse_laravel(text, rpath, schema):
                    schema.files["laravel"].append(rpath)
            elif lang == "java":
                text = read_text(path)
                if "@Entity" in text and parse_jpa(text, rpath, schema):
                    schema.files["jpa"].append(rpath)
            elif lang == "js":
                text = read_text(path)
                if "@Entity" in text and parse_typeorm(text, rpath, schema):
                    schema.files["typeorm"].append(rpath)
                elif "/migrations/" in norm or "/migration/" in norm:
                    if parse_sequelize(text, rpath, schema):
                        schema.files["sequelize"].append(rpath)
                    elif parse_js_migration_sql(text, rpath, schema, "js-migration"):
                        schema.files["js-migration"].append(rpath)
        except Exception as exc:  # keep going
            schema.notes.append(f"could not parse {rpath}: {exc}")
    for table, col, target, fam, src, unique in schema.pending_fks:
        ref = schema.entity_table.get(target, snake(target))
        if table == "__used__":
            child = schema.entity_table.get(target)
            jc = schema.field_column.get((target, col))
            if child and jc:
                schema.relation_used.add((child, jc))
            continue
        schema.add_fk(table, [col], ref, ["id"], fam, src)
        if unique:
            schema.add_index(table, [col], fam, src, unique=True)
    for parent, child in schema.prisma_lists:
        ptable, ctable = schema.entity_table.get(parent), schema.entity_table.get(child)
        for fk in schema.fks:
            if fk.table == ctable and fk.ref_table == ptable:
                schema.relation_used.add((ctable, fk.columns[0]))
    return schema


def physical_indexes(schema):
    """Merge the same index declared by several sources (e.g. JPA @Index + Flyway SQL)."""
    phys = []
    for idx in schema.indexes:
        twin = next((p for p in phys if p.full_key() == idx.full_key() and idx.family not in {s[0] for s in p.sources}), None)
        if twin:
            twin.sources.append((idx.family, idx.source))
            if not twin.name and idx.name:
                twin.name = idx.name
            continue
        idx.sources = [(idx.family, idx.source)]
        phys.append(idx)
    return phys


# =========================================================== code access paths

@dataclass
class Access:
    table: str
    eq: list
    rng: list
    sort: list
    join: list
    like: list
    func: list
    where: str
    via: str
    weight: int = 1
    nulls: list = field(default_factory=list)


def _uniq(seq):
    out = []
    for x in seq:
        if x and x not in out:
            out.append(x)
    return out


def eloquent_models(root):
    models = {}
    rels = {}
    for path, lang in iter_files(root, {"php"}):
        text = read_text(path)
        m = re.search(r"class\s+(\w+)\s+extends\s+[\\\w]*(?:Model|Authenticatable|Pivot|User)\b", text)
        if not m:
            continue
        cls = m.group(1)
        tm = re.search(r"protected\s+\$table\s*=\s*['\"](\w+)['\"]", text)
        models[cls] = (tm.group(1) if tm else pluralize(snake(cls))).lower()
        for rm in re.finditer(r"function\s+(\w+)\s*\([^)]*\)[^{]*\{[^{}]*?\$this\s*->\s*(hasMany|hasOne|belongsTo|belongsToMany|morphMany|morphOne)\s*\(\s*\\?([\w\\]+)::class\s*(?:,\s*['\"](\w+)['\"])?\s*(?:,\s*['\"](\w+)['\"])?", text, re.S):
            rels.setdefault(rm.group(1), []).append((cls, rm.group(2), rm.group(3).split("\\")[-1], rm.group(4), rm.group(5)))
    return models, rels


def parse_eloquent_chain(stmt):
    eq, rng, sort, like, func = [], [], [], [], []
    for m in re.finditer(r"(?:->|::)\s*(or)?[wW]here(Not)?(In|Null|Between|Date|Year|Month|Day|Time|Like|Column)?\s*\(\s*['\"]([\w.]+)['\"]\s*(?:,\s*['\"](<=|>=|<>|!=|<|>|=|like|ilike|not like)['\"])?", stmt):
        col = m.group(4).split(".")[-1].lower()
        variant, op = m.group(3), (m.group(5) or "").lower()
        if variant in ("Date", "Year", "Month", "Day", "Time"):
            func.append(col)
        elif variant == "Between" or op in ("<", ">", "<=", ">="):
            rng.append(col)
        elif variant == "Like" or "like" in op:
            like.append(col)
        elif m.group(1) or m.group(2) or op in ("<>", "!="):
            continue
        else:
            eq.append(col)
    for m in re.finditer(r"(?:->|::)\s*where\s*\(\s*\[([^\]]*)\]", stmt):
        eq += [k.lower() for k in re.findall(r"['\"](\w+)['\"]\s*=>", m.group(1))]
    for m in re.finditer(r"(?:->|::)\s*firstWhere\s*\(\s*['\"](\w+)['\"]", stmt):
        eq.append(m.group(1).lower())
    for m in re.finditer(r"(?:->|::)\s*orderBy(?:Desc)?\s*\(\s*['\"]([\w.]+)['\"]", stmt):
        sort.append(m.group(1).split(".")[-1].lower())
    for m in re.finditer(r"(?:->|::)\s*(latest|oldest)\s*\(\s*(?:['\"]([\w.]+)['\"])?\s*\)", stmt):
        sort.append((m.group(2) or "created_at").split(".")[-1].lower())
    return eq, rng, sort, like, func


SD_PREFIX = re.compile(r"^(?:find|read|get|query|search|stream|count|exists|delete|remove)(?:First|Top)?\d*(?:Distinct)?(?:All)?By(\w+)$")
SD_OPS = [("IsNotNull", "eq"), ("NotNull", "eq"), ("IsNull", "eq"), ("Null", "eq"), ("LessThanEqual", "rng"),
          ("LessThan", "rng"), ("GreaterThanEqual", "rng"), ("GreaterThan", "rng"), ("Before", "rng"), ("After", "rng"),
          ("Between", "rng"), ("IsBetween", "rng"), ("NotLike", "like"), ("Like", "like"), ("StartingWith", "like"),
          ("EndingWith", "like"), ("Containing", "like"), ("NotContaining", "like"), ("NotIn", "eq"), ("In", "eq"),
          ("IsTrue", "eq"), ("True", "eq"), ("IsFalse", "eq"), ("False", "eq"), ("Not", "skip"), ("Is", "eq"),
          ("Equals", "eq")]


def parse_derived(method):
    m = SD_PREFIX.match(method)
    if not m:
        return None
    crit = m.group(1)
    crit, _, order = crit.partition("OrderBy")
    preds = []
    for part in re.split(r"(?<=[a-z0-9])(?:And|Or)(?=[A-Z])", crit):
        p = re.sub(r"(AllIgnoreCase|IgnoreCase|IgnoringCase)$", "", part)
        kind = "eq"
        changed = True
        while changed:
            changed = False
            for suf, k in SD_OPS:
                if p.endswith(suf) and len(p) > len(suf):
                    p = p[: -len(suf)]
                    kind = k if kind == "eq" else kind
                    changed = True
                    break
        preds.append((p, kind, part != re.sub(r"IgnoreCase$", "", part)))
    sorts = [re.sub(r"(Asc|Desc)$", "", s) for s in re.findall(r"[A-Z][a-z0-9]*(?:[A-Z][a-z0-9]*)*?(?:Asc|Desc)", order)] if order else []
    return preds, sorts


def resolve_prop(schema, entity, prop):
    """'CustomerEmail' on Order -> [(table, column, is_join_fk)]"""
    camel = prop[0].lower() + prop[1:]
    table = schema.entity_table.get(entity, snake(entity))
    if (entity, camel) in schema.field_column and (entity, camel) not in schema.relation_fields:
        return [(table, schema.field_column[(entity, camel)])]
    parts = re.findall(r"[A-Z][a-z0-9]*", prop)
    for i in range(len(parts) - 1, 0, -1):
        head = "".join(parts[:i])
        head = head[0].lower() + head[1:]
        target = schema.relation_fields.get((entity, head))
        if target:
            rest = "".join(parts[i:])
            fk = schema.field_column.get((entity, head), snake(head) + "_id")
            if rest.lower() == "id":
                return [(table, fk)]
            return [(table, fk)] + resolve_prop(schema, target, rest)
    return [(table, schema.field_column.get((entity, camel), snake(camel)))]


def collect_access(root, schema):
    paths = []
    models, rels = eloquent_models(root)
    for name, defs in rels.items():
        for owner, kind, related, a4, a5 in defs:
            if kind in ("hasMany", "hasOne", "morphMany", "morphOne"):
                schema.relation_used.add((models.get(related, pluralize(snake(related))), (a4 or snake(owner) + "_id").lower()))
            elif kind == "belongsToMany":
                pivot = (a4 or "_".join(sorted([snake(owner), snake(related)]))).lower()
                schema.relation_used.add((pivot, (a5 or snake(owner) + "_id").lower()))
    prisma_models = {n[0].lower() + n[1:]: n for n in schema.entity_table}
    java_calls = defaultdict(int)
    java_files = []
    for path, lang in iter_files(root):
        rpath = rel(path, root)
        try:
            text = read_text(path)
        except OSError:
            continue
        if lang == "sql":
            continue
        code, struct, strings = scan_source(text, lang)
        lines = LineIndex(text)
        loc = lambda off: f"{rpath}:{lines.line(off)}"  # noqa: E731

        if lang == "php" and models:
            for m in re.finditer(r"(?<![\w\\$>])([A-Z]\w*)::(where\w*|firstWhere|orderBy\w*|latest|oldest|query|with|select|whereHas)\s*\(", code):
                table = models.get(m.group(1))
                if not table:
                    continue
                stmt = code[m.start():expr_end(struct, m.start())]
                eq, rng, sort, like, func = parse_eloquent_chain(stmt)
                if eq or rng or sort or func:
                    paths.append(Access(table, eq, rng, sort, [], like, func, loc(m.start()), f"{m.group(1)}::{m.group(2)}(…)"))
            for m in re.finditer(r"\$(\w+)\s*->\s*(\w+)\s*\(\s*\)\s*->", code):
                cands = rels.get(m.group(2))
                if cands and len(cands) > 1:
                    guess = m.group(1)[0].upper() + m.group(1)[1:]
                    cands = [c for c in cands if c[0] in (guess, guess.rstrip("s"))]
                if not cands or len(cands) != 1:
                    continue
                owner, kind, related, fk, _ = cands[0]
                table = models.get(related, pluralize(snake(related)))
                stmt = code[m.start():expr_end(struct, m.start())]
                eq, rng, sort, like, func = parse_eloquent_chain(stmt)
                if kind in ("hasMany", "hasOne", "morphMany", "morphOne"):
                    eq = [(fk or snake(owner) + "_id").lower()] + eq
                if eq or rng or sort:
                    paths.append(Access(table, eq, rng, sort, [], like, func, loc(m.start()), f"${m.group(1)}->{m.group(2)}()->…"))

        if lang == "java":
            java_files.append((path, rpath, code, lines))
            for m in re.finditer(r"\.\s*(\w+By\w+)\s*\(", code):
                java_calls[m.group(1)] += 1

        if lang == "js":
            for m in re.finditer(r"\b(?:this\.)?(?:prisma|tx|db|client|ctx\.prisma)\s*\.\s*(\w+)\s*\.\s*(findMany|findFirst|findFirstOrThrow|findUnique|findUniqueOrThrow|count|aggregate|groupBy|updateMany|deleteMany|update|delete|upsert)\s*\(", code):
                model = prisma_models.get(m.group(1))
                if not model:
                    continue
                table = schema.entity_table[model]
                open_idx = m.end() - 1
                args = code[open_idx + 1:match_close(struct, open_idx)]
                eq, rng, like, sort = [], [], [], []
                wm = re.search(r"\bwhere\s*:\s*\{", args)
                if wm:
                    sargs = struct[open_idx + 1:open_idx + 1 + len(args)]
                    wb = wm.end() - 1
                    wbody = args[wb + 1:match_close(sargs, wb)]
                    for k, v in _obj_keys(wbody).items():
                        if k in ("AND", "OR", "NOT") or (model, k) in schema.relation_fields:
                            continue
                        col = schema.field_column.get((model, k), k.lower())
                        if re.search(r"\b(gt|gte|lt|lte)\s*:", v):
                            rng.append(col)
                        elif re.search(r"\b(contains|startsWith|endsWith|search)\s*:", v):
                            like.append(col)
                        elif v.startswith("{") and not re.search(r"\b(equals|in)\s*:", v):
                            continue
                        else:
                            eq.append(col)
                om = re.search(r"\borderBy\s*:\s*\[?\s*\{\s*(\w+)\s*:", args)
                if om:
                    sort.append(schema.field_column.get((model, om.group(1)), om.group(1).lower()))
                if eq or rng or sort:
                    paths.append(Access(table, eq, rng, sort, [], like, [], loc(m.start()), f"prisma.{m.group(1)}.{m.group(2)}()"))
            for m in re.finditer(r"\.from\s*\(\s*['\"](\w+)['\"]\s*\)", code):
                end = statement_end(struct, m.start())
                stmt = code[m.start():end]
                eq, rng, like, sort = [], [], [], []
                for f in re.finditer(r"\.(eq|neq|gt|gte|lt|lte|like|ilike|is|in|match)\s*\(\s*['\"](\w+)['\"]", stmt):
                    op, col = f.group(1), f.group(2).lower()
                    (rng if op in ("gt", "gte", "lt", "lte") else like if op in ("like", "ilike") else eq if op != "neq" else []).append(col)
                for f in re.finditer(r"\.order\s*\(\s*['\"](\w+)['\"]", stmt):
                    sort.append(f.group(1).lower())
                if eq or rng or sort:
                    paths.append(Access(m.group(1).lower(), eq, rng, sort, [], like, [], loc(m.start()), "supabase.from(…)"))
            for m in re.finditer(r"(?<![\w.$])([A-Z]\w*)\s*\.\s*(findAll|findOne|count|findAndCountAll|findOrCreate|update|destroy)\s*\(", code):
                table = schema.entity_table.get(m.group(1)) or (pluralize(snake(m.group(1))) if pluralize(snake(m.group(1))) in schema.tables else None)
                if not table:
                    continue
                open_idx = m.end() - 1
                args = code[open_idx + 1:match_close(struct, open_idx)]
                wm = re.search(r"\bwhere\s*:\s*\{([^{}]*)\}", args)
                eq = [k.lower() for k in _obj_keys(wm.group(1))] if wm else []
                sort = [s.lower() for s in re.findall(r"order\s*:\s*\[\s*\[\s*['\"](\w+)['\"]", args)]
                if eq or sort:
                    paths.append(Access(table, eq, [], sort, [], [], [], loc(m.start()), f"{m.group(1)}.{m.group(2)}()"))
            for m in re.finditer(r"\b(\w+?)(?:Repository|Repo)\s*\.\s*(find|findOne|findBy|findOneBy|count|countBy|findAndCount|exist|existsBy)\s*\(\s*\{", code):
                entity = m.group(1)[0].upper() + m.group(1)[1:]
                table = schema.entity_table.get(entity)
                if not table:
                    continue
                open_idx = code.index("(", m.end() - 2)
                args = code[open_idx + 1:match_close(struct, open_idx)]
                if m.group(2).endswith("By"):
                    keys = _obj_keys(args.strip()[1:-1])
                else:
                    wm = re.search(r"\bwhere\s*:\s*\{([^{}]*)\}", args)
                    keys = _obj_keys(wm.group(1)) if wm else {}
                eq, rng, like = [], [], []
                for k, v in keys.items():
                    col = schema.field_column.get((entity, k), k.lower())
                    (rng if re.match(r"(MoreThan|LessThan|Between)", v) else like if re.match(r"I?Like", v) else eq).append(col)
                om = re.search(r"\border\s*:\s*\{\s*(\w+)\s*:", args)
                sort = [schema.field_column.get((entity, om.group(1)), om.group(1).lower())] if om else []
                if eq or rng or sort:
                    paths.append(Access(table, eq, rng, sort, [], like, [], loc(m.start()), f"{m.group(1)}Repository.{m.group(2)}()"))

        for sp in strings:
            sql = sp.content(text)
            if re.match(r"(?is)\s*\(?\s*(SELECT|WITH|UPDATE|DELETE)\b", sql) and re.search(r"(?i)\b(FROM|UPDATE)\b", sql):
                for a in sql_access(sql, schema):
                    a.where, a.via = loc(sp.start), "SQL string"
                    paths.append(a)

    for path, rpath, code, lines in java_files:
        rm = re.search(r"interface\s+(\w+)\s+extends\s+\w*Repository\s*<\s*(\w+)\s*,", code)
        if not rm:
            continue
        entity = rm.group(2)
        for m in re.finditer(r"\b((?:find|read|get|query|search|stream|count|exists|delete|remove)\w*By\w+)\s*\(", code):
            decl_start = max(code.rfind(";", 0, m.start()), code.rfind("{", 0, m.start()), code.rfind("}", 0, m.start()))
            if "@Query" in code[decl_start + 1:m.start()]:
                continue  # explicit query: not a derived query (its SQL/JPQL is analysed as a string)
            parsed = parse_derived(m.group(1))
            if not parsed:
                continue
            preds, sorts = parsed
            groups = defaultdict(lambda: dict(eq=[], rng=[], like=[], sort=[], func=[]))
            for prop, kind, ci in preds:
                resolved = resolve_prop(schema, entity, prop)
                *joins, (t, c) = resolved
                for jt, jc in joins:
                    groups[jt]["eq"].append(jc)
                if kind == "skip":
                    continue
                if kind == "like":
                    groups[t]["like"].append(c)
                elif ci:
                    groups[t]["func"].append(c)
                else:
                    groups[t][kind].append(c)
            for s in sorts:
                *_, (t, c) = resolve_prop(schema, entity, s)
                groups[t]["sort"].append(c)
            weight = max(1, java_calls.get(m.group(1), 0))
            for t, g in groups.items():
                paths.append(Access(t, _uniq(g["eq"]), _uniq(g["rng"]), _uniq(g["sort"]), [], g["like"], g["func"],
                                    f"{rpath}:{lines.line(m.start())}", f"{rm.group(1)}.{m.group(1)}()", weight))

    for path, lang in iter_files(root, {"sql"}):
        text = read_text(path)
        code, struct, _ = scan_source(text, "sql")
        lines = LineIndex(text)
        start = 0
        for i, ch in enumerate(struct + ";"):
            if ch != ";":
                continue
            stmt = code[start:i]
            off = start + len(stmt) - len(stmt.lstrip())
            start = i + 1
            if re.match(r"(?is)\s*\(?\s*(SELECT|WITH|UPDATE|DELETE)\b", stmt):
                for a in sql_access(stmt, schema):
                    a.where, a.via = f"{rel(path, root)}:{lines.line(off)}", "SQL file"
                    paths.append(a)
    return paths


def expr_end(struct, i):
    depth = 0
    for j in range(i, len(struct)):
        ch = struct[j]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return j
        elif ch in ";," and depth == 0:
            return j
    return len(struct)


SQL_KW = {"WHERE", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "CROSS", "FULL", "ON", "GROUP", "ORDER", "LIMIT",
          "OFFSET", "USING", "SET", "NATURAL", "UNION", "HAVING", "WINDOW", "FOR", "AND", "OR", "NOT", "AS", "LATERAL"}


def sql_access(sql, schema):
    s = re.sub(r"\s+", " ", sql)
    s = re.sub(r"'(?:[^']|'')*'", "'?'", s)
    aliases = {}
    for m in re.finditer(r"\b(?:FROM|JOIN|UPDATE)\s+([\w.\"`]+)(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?", s, re.I):
        t = norm_ident(m.group(1))
        if t.upper() in SQL_KW or t == "(":
            continue
        aliases[t] = t
        if m.group(2) and m.group(2).upper() not in SQL_KW:
            aliases[m.group(2).lower()] = t
    tables = set(aliases.values())
    out = defaultdict(lambda: dict(eq=[], rng=[], join=[], sort=[], like=[], func=[], nulls=[]))

    def resolve(alias):
        if alias:
            return aliases.get(alias.lower())
        return next(iter(tables)) if len(tables) == 1 else None

    for m in re.finditer(r"\b(?:WHERE|ON|AND|OR)\s+(?:NOT\s+)?\(?\s*(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)\s*(=|<>|!=|<=|>=|<|>|\bIN\b|\bLIKE\b|\bILIKE\b|\bBETWEEN\b|\bIS\b)\s*(?:([A-Za-z_]\w*)\.([A-Za-z_]\w*))?", s, re.I):
        alias, col, op = m.group(1), m.group(2).lower(), m.group(3).upper()
        if col.upper() in SQL_KW or col in ("null", "true", "false"):
            continue
        t = resolve(alias)
        if not t:
            continue
        if m.group(4) and m.group(5):
            out[t]["join"].append(col)
            t2 = resolve(m.group(4))
            if t2:
                out[t2]["join"].append(m.group(5).lower())
        elif op in ("<", ">", "<=", ">=", "BETWEEN"):
            out[t]["rng"].append(col)
        elif op in ("LIKE", "ILIKE"):
            out[t]["like"].append(col)
        elif op in ("<>", "!="):
            continue
        elif op == "IS":
            if re.match(r"\s*NULL\b", s[m.end():], re.I):
                out[t]["nulls"].append(col)
        else:
            out[t]["eq"].append(col)
    ob = re.search(r"\bORDER\s+BY\s+(.+?)(?:\bLIMIT\b|\bOFFSET\b|\bFOR\b|\)|$)", s, re.I)
    if ob:
        for part in ob.group(1).split(","):
            pm = re.match(r"\s*(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)", part)
            if pm and pm.group(2).upper() not in ("RANDOM", "RAND", "NEWID"):
                t = resolve(pm.group(1))
                if t:
                    out[t]["sort"].append(pm.group(2).lower())
    return [Access(t, _uniq(g["eq"]), _uniq(g["rng"]), _uniq(g["sort"]), _uniq(g["join"]), g["like"], g["func"], "", "",
                   nulls=_uniq(g["nulls"]))
            for t, g in out.items() if g["eq"] or g["rng"] or g["sort"] or g["join"]]


# ================================================================== analysis

LOW_CARD_NAME = re.compile(r"^(is|has|can|should|was)_\w+$|(^|_)(active|enabled|disabled|deleted|visible|published|archived|status|state|type|kind|gender|flag|role|locale|lang|currency|country)$")


def ddl_create(engine, table, cols, unique=False):
    name = f"{'ux' if unique else 'idx'}_{table}_{'_'.join(c.replace('expr:', 'expr') for c in cols)}"[:63]
    collist = ", ".join(cols)
    if engine == "mysql":
        return f"ALTER TABLE {table} ADD {'UNIQUE ' if unique else ''}INDEX {name} ({collist}), ALGORITHM=INPLACE, LOCK=NONE;"
    return f"CREATE {'UNIQUE ' if unique else ''}INDEX CONCURRENTLY {name} ON {table} ({collist});"


def ddl_drop(engine, idx):
    name = idx.name or "<index_name>"
    if engine == "mysql":
        return f"ALTER TABLE {idx.table} ALTER INDEX {name} INVISIBLE;  -- observe, then: DROP INDEX {name} ON {idx.table};"
    return f"DROP INDEX CONCURRENTLY {name};"


def analyze(schema, phys, paths, engine, root):
    findings = []
    by_table = defaultdict(list)
    for i in phys:
        by_table[i.table].append(i)

    def add(rule, sev, src, msg, hint, conf="high", **extra):
        f, _, ln = src.rpartition(":")
        findings.append(Finding(rule, sev, f or src, int(ln) if ln.isdigit() else 1, msg, conf, "", hint, extra))

    # duplicates
    for table, idxs in by_table.items():
        groups = defaultdict(list)
        for i in idxs:
            groups[i.shape_key()].append(i)
        for key, g in groups.items():
            if len(g) < 2:
                continue
            keeper = next((i for i in g if i.primary), None) or next((i for i in g if i.unique), None) or g[0]
            for i in g:
                if i is keeper or (i.unique and not keeper.primary and i.unique == keeper.unique and False):
                    continue
                if i.unique and keeper.unique and not keeper.primary and not i.primary:
                    why = "two UNIQUE indexes on the same columns"
                elif i.unique and keeper.primary:
                    why = "the primary key already enforces uniqueness on these columns"
                elif keeper.unique or keeper.primary:
                    why = f"{keeper.kind()} `{keeper.name or '(unnamed)'}` on the same columns already serves every lookup this one can"
                else:
                    why = f"identical to `{keeper.name or '(unnamed)'}` ({keeper.sources[0][1]})"
                add("IDX-DUPLICATE", "medium", i.source,
                    f"{i.label()} is redundant: {why}. Every INSERT/UPDATE/DELETE maintains both, for zero read benefit.",
                    f"Drop it (after checking nothing references it by name, e.g. index hints): `{ddl_drop(engine, i)}`",
                    duplicate_of=keeper.label())

    # left-prefix redundancy
    for table, idxs in by_table.items():
        for a in idxs:
            if a.unique or a.primary or a.method != "btree" or any(c.startswith("expr:") for c in a.columns):
                continue
            for b in idxs:
                if b is a or b.method != "btree" or b.where.lower() != a.where.lower():
                    continue
                if len(b.columns) > len(a.columns) and b.columns[:len(a.columns)] == a.columns:
                    wide = len(b.columns) - len(a.columns) >= 3
                    add("IDX-REDUNDANT-PREFIX", "low" if wide else "medium", a.source,
                        f"{a.label()} is a left prefix of {b.label()} ({b.sources[0][1]}): any query that can use it can use the wider index.",
                        f"Usually safe to drop (`{ddl_drop(engine, a)}`). Keep it only if measurements show the narrower index matters (much wider second index, very hot range scans). Confirm with usage stats first.",
                        "high" if not wide else "medium")
                    break

    # FK without supporting index
    fk_cols_used = schema.relation_used | {(p.table, c) for p in paths for c in p.eq + p.join}
    if engine == "mysql":
        if schema.fks:
            add("IDX-FK-AUTO", "info", schema.fks[0].source,
                "MySQL/InnoDB creates an index for every foreign key that lacks one, so FK coverage isn't reported for this engine.",
                "Those implicit indexes still cost writes; they are visible in `SHOW INDEX` / `sys.schema_redundant_indexes`.")
    else:
        seen = set()
        for fk in schema.fks:
            key = (fk.table, tuple(fk.columns))
            if key in seen or not fk.columns:
                continue
            seen.add(key)
            n = len(fk.columns)
            covered = any(set(i.columns[:n]) == set(fk.columns) for i in by_table.get(fk.table, []) if i.method == "btree" and not i.where)
            if covered:
                continue
            used = any((fk.table, c) in fk_cols_used for c in fk.columns)
            add("IDX-FK-NO-INDEX", "high" if used else "medium", fk.source,
                f"Foreign key {fk.table}({', '.join(fk.columns)}) → {fk.ref_table}({', '.join(fk.ref_columns)}) has no index starting with its column(s). "
                f"Loading `{fk.table}` rows of a parent (relations, joins) scans the table, and every DELETE/UPDATE of a `{fk.ref_table}` key scans `{fk.table}` to check the constraint."
                + (" The code filters/joins on it." if used else ""),
                f"`{ddl_create(engine, fk.table, fk.columns)}`" + (" (Prisma `relationMode = \"prisma\"`: no FK constraints exist, so add `@@index` on relation scalars.)" if schema.relation_mode_prisma else ""),
                "high", table=fk.table, column=fk.columns[0])

    # low selectivity
    for table, idxs in by_table.items():
        for i in idxs:
            if len(i.columns) != 1 or i.unique or i.primary or i.where or i.method != "btree":
                continue
            col = i.columns[0]
            typ = schema.tables.get(table, {}).get(col, "")
            if typ in ("boolean", "enum", "bool") or LOW_CARD_NAME.search(col):
                add("IDX-LOW-SELECTIVITY", "low", i.source,
                    f"{i.label()} indexes a low-cardinality column ({typ or 'name suggests few distinct values'}): the planner rarely uses it unless you query a rare value, yet every write pays for it.",
                    f"Replace with a partial index on the rare value (`CREATE INDEX … ON {table} (created_at) WHERE {col} = …`) or a composite led by a selective column (`({col}, created_at)` only if always filtered together). Check `pg_stats.n_distinct`/`most_common_freqs` or `SELECT {col}, count(*) … GROUP BY {col}`.",
                    "medium")

    # wide / many
    for table, idxs in by_table.items():
        for i in idxs:
            if len(i.columns) > 5:
                add("IDX-WIDE", "info", i.source, f"{i.label()} has {len(i.columns)} columns: large, expensive to maintain, and only its left prefix is usable by most queries.",
                    "Check which prefixes queries actually use; consider INCLUDE columns (Postgres 11+) for covering instead of key columns.", "medium")
        non_pk = [i for i in idxs if not i.primary]
        if len(non_pk) >= 7:
            add("IDX-MANY-INDEXES", "info", non_pk[0].source, f"Table `{table}` has {len(non_pk)} secondary indexes: each write updates all of them.",
                "Review usage stats and drop the unused/redundant ones; consolidate into composites.", "medium")

    # code access paths -> missing indexes
    fk_reported = {(f.extra.get("table"), f.extra.get("column")) for f in findings if f.rule == "IDX-FK-NO-INDEX"}
    suggestions = {}
    hot = defaultdict(lambda: dict(uses=0, where=[]))
    unknown_tables = defaultdict(int)
    for p in paths:
        if p.table not in schema.tables and p.table not in by_table:
            unknown_tables[p.table] += p.weight
            continue
        idxs = by_table.get(p.table, [])
        leading = {i.columns[0] for i in idxs if i.columns and i.method == "btree"}
        pk_cols = {i.columns[0] for i in idxs if i.primary and len(i.columns) == 1}
        filt = _uniq(p.eq + p.join + p.rng)
        for c in _uniq(filt + p.sort + p.func):
            h = hot[(p.table, c)]
            h["uses"] += p.weight
            if len(h["where"]) < 3 and p.where not in h["where"]:
                h["where"].append(p.where)
        if any(c in pk_cols for c in p.eq):
            continue
        if not filt and p.nulls and p.sort and p.sort[0] not in leading:
            key = (p.table, tuple(p.sort[:2]), "partial")
            s = suggestions.setdefault(key, dict(uses=0, where=[], via=[], func=[], partial=p.nulls[0]))
            s["uses"] += p.weight
            if p.where not in s["where"] and len(s["where"]) < 4:
                s["where"].append(p.where)
            continue
        if filt and not (set(filt) & leading):
            cols = _uniq(p.eq + p.join)
            cols += [c for c in p.sort if c not in cols]
            cols += [c for c in p.rng if c not in cols][:1]
            cols = cols[:4]
            key = (p.table, tuple(cols), "")
            s = suggestions.setdefault(key, dict(uses=0, where=[], via=[], func=[]))
            s["uses"] += p.weight
            if p.where not in s["where"] and len(s["where"]) < 4:
                s["where"].append(p.where)
            if p.via not in s["via"] and len(s["via"]) < 3:
                s["via"].append(p.via)
        elif p.eq and p.sort:
            prefixes = [i.columns for i in idxs if i.method == "btree"]
            want = _uniq(p.eq) + [c for c in p.sort if c not in p.eq]
            if not any(cols[:len(want)] == want for cols in prefixes):
                key = (p.table, tuple(want[:4]), "sort")
                s = suggestions.setdefault(key, dict(uses=0, where=[], via=[], func=[], sort_only=True))
                s["uses"] += p.weight
                if p.where not in s["where"] and len(s["where"]) < 4:
                    s["where"].append(p.where)
        if p.func and not (set(p.func) & leading):
            for c in p.func:
                key = (p.table, (c,), "")
                s = suggestions.setdefault(key, dict(uses=0, where=[], via=[], func=[]))
                s["uses"] += p.weight
                s["func"].append(c)
                if p.where not in s["where"] and len(s["where"]) < 4:
                    s["where"].append(p.where)
    for (table, cols, _tag), s in sorted(suggestions.items(), key=lambda kv: -kv[1]["uses"]):
        locs = ", ".join(s["where"])
        if (table, cols[0]) in fk_reported:
            continue
        if s.get("partial"):
            ddl = ddl_create(engine, table, list(cols)).rstrip(";")
            add("IDX-PARTIAL-CANDIDATE", "low", s["where"][0],
                f"Queries on `{table}` keep only rows where `{s['partial']} IS NULL` (soft deletes?) and sort by `{cols[0]}`; no index leads with `{cols[0]}` ({s['uses']} code site(s): {locs}).",
                (f"If the query uses LIMIT/pagination on a big table: `{ddl} WHERE {s['partial']} IS NULL;` (partial index: smaller, only live rows)."
                 if engine != "mysql" else f"MySQL has no partial indexes: `{ddl_create(engine, table, [s['partial']] + list(cols))}`."),
                "low")
            continue
        if s.get("sort_only"):
            add("IDX-SORT-NOT-COVERED", "low", s["where"][0],
                f"Queries on `{table}` filter by ({', '.join(cols[:-1])}) and sort by `{cols[-1]}`; an index exists for the filter but not in that order, so matching rows are sorted on every call ({s['uses']} code site(s): {locs}).",
                f"If the filtered set can be large, a composite lets the engine read rows already ordered and stop at LIMIT: `{ddl_create(engine, table, list(cols))}` (it can replace the narrower index).",
                "low")
            continue
        low_card = [c for c in cols if schema.tables.get(table, {}).get(c, "") in ("boolean", "enum") or LOW_CARD_NAME.search(c)]
        lc_note = ""
        if low_card and len(low_card) == len(cols):
            lc_note = (f" Note: ({', '.join(low_card)}) look low-cardinality — a plain index only pays off when the filtered value is rare"
                       + (f"; prefer a partial index (`… ON {table} (id) WHERE {low_card[0]} = …`) or lead with a selective column." if engine != "mysql" else "; lead with a more selective column if the query has one."))
        func_note = (f" Some sites wrap `{', '.join(_uniq(s['func']))}` in a function (whereDate/IgnoreCase): fix the query to a plain range/normalized value first or index the expression."
                     if s["func"] else "")
        add("IDX-MISSING-FOR-QUERY", "medium" if s["uses"] >= 2 else "low", s["where"][0],
            f"No index leads with any filter column of queries on `{table}` by ({', '.join(cols)}) — {s['uses']} code site(s): {locs}. On a large table each call scans it." + func_note + lc_note,
            f"Candidate (equality columns first, then sort, then one range column): `{ddl_create(engine, table, list(cols))}`. Worth it only if the table is large or the query hot — confirm with EXPLAIN before/after.",
            "medium" if s["uses"] >= 2 else "low")

    hot_rows = []
    for (table, col), h in sorted(hot.items(), key=lambda kv: (-kv[1]["uses"], kv[0])):
        idxs = by_table.get(table, [])
        lead = any(i.columns and i.columns[0] == col for i in idxs)
        inside = any(col in i.columns[1:] for i in idxs)
        state = "leading column of an index" if lead else ("only non-leading in an index" if inside else "NO INDEX")
        hot_rows.append((table, col, h["uses"], state, ", ".join(h["where"])))
    return findings, hot_rows, unknown_tables


def inventory_md(phys):
    out = ["## Index inventory", "", "| Table | Index | Declared in |", "|---|---|---|"]
    for i in sorted(phys, key=lambda x: (x.table, not x.primary, x.columns)):
        srcs = "; ".join(f"{fam}: {s}" for fam, s in i.sources)
        cols = ", ".join(c[5:] if c.startswith("expr:") else c for c in i.columns)
        extra = (f" INCLUDE ({', '.join(i.include)})" if i.include else "") + (f" WHERE {i.where}" if i.where else "")
        out.append(f"| {i.table} | {i.kind()} `{i.name or '(unnamed)'}` ({cols}){extra} | {srcs} |")
    return "\n".join(out)


def main():
    p = base_parser("db-doctor index advisor: inventory, redundant/duplicate/missing indexes, FK coverage, hot columns.")
    p.add_argument("--inventory", action="store_true", help="also print the full index inventory")
    p.add_argument("--no-usage", action="store_true", help="skip scanning code for filtered/sorted columns")
    p.add_argument("--hot", type=int, default=20, help="rows in the hot-columns table (default 20)")
    args = p.parse_args()
    root = os.path.abspath(args.root)
    if not os.path.exists(root):
        p.error(f"path not found: {root}")
    args.root = root
    engine, why = resolve_engine(args)
    schema = load_schema(root)
    phys = physical_indexes(schema)
    paths = [] if args.no_usage else collect_access(root, schema)
    findings, hot_rows, unknown = analyze(schema, phys, paths, engine, root)
    findings = filter_severity(sort_findings(findings), args.min_severity)

    sources = "; ".join(f"{fam}: {len(fs)} file(s)" for fam, fs in schema.files.items()) or "none found"
    meta = [("Root", root), ("Engine", f"{engine} ({why})"), ("Schema sources", sources),
            ("Tables / indexes / FKs", f"{len(schema.tables)} / {len(phys)} / {len(schema.fks)}"),
            ("Code access paths analysed", str(len(paths)))]
    if schema.relation_mode_prisma:
        meta.append(("Prisma relationMode", "prisma (no FK constraints in the database)"))
    if not schema.files:
        meta.append(("Note", "No schema source found. Dump the schema (`pg_dump --schema-only`, `mysqldump --no-data`) and pass the file."))
    for n in schema.notes:
        meta.append(("Parse note", n))

    sections = []
    if hot_rows:
        rows = ["## Hot columns (filtered / joined / sorted in code)", "",
                "Code-site counts are a proxy for query frequency, not a measurement.", "",
                "| Table | Column | Code sites | Index status | Examples |", "|---|---|---|---|---|"]
        for t, c, n, state, wh in hot_rows[:args.hot]:
            rows.append(f"| {t} | {c} | {n} | {'**' + state + '**' if state == 'NO INDEX' else state} | {wh} |")
        sections.append("\n".join(rows))
    if unknown:
        sections.append("## Tables referenced in code but not found in the parsed schema\n\n" +
                        ", ".join(f"`{t}` ({n})" for t, n in sorted(unknown.items(), key=lambda kv: -kv[1])) +
                        "\n\nEither the schema source wasn't found/parsed or the name mapping guessed wrong — check before trusting the missing-index list.")
    if args.inventory:
        sections.append(inventory_md(phys))

    if args.json:
        from dataclasses import asdict
        print(to_json(meta, findings,
                      inventory=[dict(asdict(i), label=i.label()) for i in phys],
                      foreign_keys=[asdict(f) for f in schema.fks],
                      hot_columns=[dict(table=t, column=c, code_sites=n, status=s, examples=w) for t, c, n, s, w in hot_rows],
                      unknown_tables=dict(unknown)))
    else:
        print(render_markdown("db-doctor · Index advisor", meta, findings, args.limit, sections))


if __name__ == "__main__":
    main()
