"""Shared helpers for the db-doctor analysis scripts (Python 3.8+, standard library only).

Everything here is heuristic. The scripts produce *leads* for a human/LLM to verify,
never final verdicts. Keep outputs explicit about that.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
from dataclasses import asdict, dataclass, field

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

SKIP_DIRS = {
    "node_modules", "vendor", "dist", "build", "out", "target", "coverage", "storage",
    "__pycache__", "venv", "env", "bower_components", "public", "tmp", "logs",
}

EXT_LANG = {
    ".php": "php", ".java": "java", ".kt": "kotlin",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".ts": "js", ".tsx": "js", ".mts": "js", ".cts": "js",
    ".py": "python", ".go": "go", ".rb": "ruby", ".cs": "csharp",
    ".sql": "sql", ".prisma": "prisma",
}

CODE_LANGS = {"php", "blade", "java", "kotlin", "js", "python", "go", "ruby", "csharp"}


# --------------------------------------------------------------------------- files

def lang_for(path: str):
    name = os.path.basename(path).lower()
    if name.endswith(".blade.php"):
        return "blade"
    if name.endswith((".min.js", ".bundle.js", ".d.ts", ".map")):
        return None
    return EXT_LANG.get(os.path.splitext(name)[1])


def iter_files(root: str, langs=None, max_bytes: int = 1_500_000):
    """Yield (path, lang) for source files under root, skipping dependency/build dirs."""
    if os.path.isfile(root):
        lang = lang_for(root)
        if lang and (not langs or lang in langs):
            yield root, lang
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fname in sorted(filenames):
            path = os.path.join(dirpath, fname)
            lang = lang_for(path)
            if lang is None or (langs and lang not in langs):
                continue
            try:
                if os.path.getsize(path) > max_bytes:
                    continue
            except OSError:
                continue
            yield path, lang


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def rel(path: str, root: str) -> str:
    base = root if os.path.isdir(root) else os.path.dirname(root)
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


class LineIndex:
    """Offset -> 1-based line number."""

    def __init__(self, text: str):
        self.text = text
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)

    def line_text(self, lineno: int) -> str:
        start = self.starts[lineno - 1]
        end = self.starts[lineno] - 1 if lineno < len(self.starts) else len(self.text)
        return self.text[start:end]


# ------------------------------------------------------------------ source masking

LINE_COMMENTS = {
    "php": ("//", "#"), "java": ("//",), "kotlin": ("//",), "js": ("//",), "go": ("//",),
    "csharp": ("//",), "python": ("#",), "ruby": ("#",), "sql": ("--",), "prisma": ("//",),
}
BLOCK_COMMENT_LANGS = {"php", "java", "kotlin", "js", "go", "csharp", "sql"}
QUOTES = {
    "php": "'\"", "java": "'\"", "kotlin": "'\"", "js": "'\"`", "go": "'\"`", "csharp": "'\"",
    "python": "'\"", "ruby": "'\"", "sql": "'", "prisma": "\"",
}
TRIPLE_QUOTES = {"python": ('"""', "'''"), "java": ('"""',), "kotlin": ('"""',)}
MULTILINE_STRING_LANGS = {"php", "sql", "ruby"}
_DOLLAR_QUOTE = re.compile(r"\$([A-Za-z_]\w*)?\$")
_HEREDOC = re.compile(r"<<<[ \t]*(['\"]?)([A-Za-z_]\w*)\1\r?\n")


@dataclass
class StringSpan:
    start: int          # index of the opening quote
    end: int            # index just after the closing quote
    quote: str
    content_start: int
    content_end: int

    def content(self, text: str) -> str:
        return text[self.content_start:self.content_end]


def _blank(buf: list, a: int, b: int) -> None:
    for k in range(a, min(b, len(buf))):
        if buf[k] != "\n":
            buf[k] = " "


def scan_source(text: str, lang: str):
    """Return (code, struct, strings).

    code    -- text with comments blanked (strings kept)
    struct  -- text with comments AND string contents blanked (for bracket matching)
    strings -- list[StringSpan]
    All three keep the original length so offsets and line numbers line up.
    """
    if lang == "blade":
        code = list(text)
        for m in re.finditer(r"\{\{--.*?--\}\}|<!--.*?-->", text, re.S):
            _blank(code, m.start(), m.end())
        s = "".join(code)
        return s, s, []

    n = len(text)
    code = list(text)
    struct = list(text)
    strings = []
    line_tokens = LINE_COMMENTS.get(lang, ())
    block = lang in BLOCK_COMMENT_LANGS
    quotes = QUOTES.get(lang, "'\"")
    triples = TRIPLE_QUOTES.get(lang, ())
    i = 0
    while i < n:
        c = text[i]
        # block comment
        if block and c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            _blank(code, i, j)
            _blank(struct, i, j)
            i = j
            continue
        # line comment
        tok = None
        for t in line_tokens:
            if text.startswith(t, i):
                if t == "#" and lang == "php" and text.startswith("#[", i):
                    continue
                tok = t
                break
        if tok:
            j = text.find("\n", i)
            j = n if j < 0 else j
            _blank(code, i, j)
            _blank(struct, i, j)
            i = j
            continue
        # SQL dollar quoting ($$ ... $$ / $tag$ ... $tag$)
        if lang == "sql" and c == "$":
            m = _DOLLAR_QUOTE.match(text, i)
            if m:
                tag = m.group(0)
                j = text.find(tag, m.end())
                end = n if j < 0 else j + len(tag)
                _blank(struct, m.end(), end - len(tag) if j >= 0 else n)
                i = end
                continue
        # PHP heredoc / nowdoc
        if lang == "php" and c == "<" and text.startswith("<<<", i):
            m = _HEREDOC.match(text, i)
            if m:
                endm = re.compile(r"^[ \t]*" + re.escape(m.group(2)) + r"\b", re.M).search(text, m.end())
                cend = endm.start() if endm else n
                end = endm.end() if endm else n
                strings.append(StringSpan(i, end, '"' if m.group(1) != "'" else "'", m.end(), cend))
                _blank(struct, m.end(), cend)
                i = end
                continue
        # triple-quoted strings
        tq = next((t for t in triples if text.startswith(t, i)), None)
        if tq:
            j = text.find(tq, i + 3)
            cend = n if j < 0 else j
            end = n if j < 0 else j + 3
            strings.append(StringSpan(i, end, tq[0], i + 3, cend))
            _blank(struct, i + 3, cend)
            i = end
            continue
        # regular strings
        if c in quotes:
            raw = lang == "go" and c == "`"
            multiline = c == "`" or lang in MULTILINE_STRING_LANGS
            j = i + 1
            while j < n:
                ch = text[j]
                if ch == "\\" and not raw:
                    j += 2
                    continue
                if ch == c:
                    break
                if ch == "\n" and not multiline:
                    break
                j += 1
            cend = min(j, n)
            closed = j < n and text[j] == c
            strings.append(StringSpan(i, cend + 1 if closed else cend, c, i + 1, cend))
            _blank(struct, i + 1, cend)
            i = cend + 1 if closed else cend
            continue
        i += 1
    return "".join(code), "".join(struct), strings


PAIRS = {"(": ")", "[": "]", "{": "}"}


def match_close(s: str, i: int) -> int:
    """Index of the bracket closing the one at s[i] (s should be a `struct` text)."""
    o = s[i]
    c = PAIRS[o]
    depth = 0
    for j in range(i, len(s)):
        ch = s[j]
        if ch == o:
            depth += 1
        elif ch == c:
            depth -= 1
            if depth == 0:
                return j
    return len(s) - 1


def statement_end(struct: str, i: int, stop: str = ";") -> int:
    """End of the statement starting at i: first `stop` at bracket depth 0."""
    depth = 0
    for j in range(i, len(struct)):
        ch = struct[j]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return j
        elif ch == stop and depth == 0:
            return j
    return len(struct)


def split_top_level(s: str, sep: str = ","):
    """Split on `sep` at bracket depth 0 (ignores quotes: pass masked text if needed)."""
    parts, depth, cur = [], 0, []
    quote = None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


# ------------------------------------------------------------------- naming helpers

def snake(name: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return s.replace("-", "_").lower()


def pluralize(word: str) -> str:
    irregular = {"person": "people", "child": "children", "man": "men", "woman": "women"}
    head, _, last = word.rpartition("_")
    if last in irregular:
        plural = irregular[last]
    elif last.endswith("y") and len(last) > 1 and last[-2] not in "aeiou":
        plural = last[:-1] + "ies"
    elif last.endswith(("s", "x", "z", "ch", "sh")):
        plural = last + "es"
    else:
        plural = last + "s"
    return f"{head}_{plural}" if head else plural


def singularize(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def norm_ident(name: str) -> str:
    """Normalize a SQL identifier: strip quotes/schema, lowercase."""
    name = name.strip().strip(";")
    parts = [p.strip().strip('`"[]') for p in re.split(r"\.(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", name)]
    return parts[-1].lower() if parts else name.lower()


# ------------------------------------------------------------- project detection

def _read_if(path: str) -> str:
    try:
        if os.path.isfile(path) and os.path.getsize(path) < 2_000_000:
            return read_text(path)
    except OSError:
        pass
    return ""


def _glob_configs(root: str, names_re: str, max_depth: int = 6):
    root = root if os.path.isdir(root) else os.path.dirname(root)
    rx = re.compile(names_re)
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if rx.fullmatch(f):
                yield os.path.join(dirpath, f)


def detect_engine(root: str):
    """Best-effort database engine detection. Returns (engine, evidence)."""
    checks = []
    for path in _glob_configs(root, r"\.env(\.\w+)?|application[\w-]*\.(properties|ya?ml)|schema\.prisma|"
                                   r"docker-compose[\w.-]*\.ya?ml|compose\.ya?ml|package\.json|config\.toml|"
                                   r"database\.(php|js|ts|json)|ormconfig\.\w+|data-source\.\w+|settings\.py"):
        checks.append((path, _read_if(path)))
    patterns = [
        (r"DB_CONNECTION\s*=\s*(pgsql|postgres\w*)", "postgres"),
        (r"DB_CONNECTION\s*=\s*(mysql|mariadb)", "mysql"),
        (r"DB_CONNECTION\s*=\s*sqlite", "sqlite"),
        (r"jdbc:(postgresql)", "postgres"),
        (r"jdbc:(mysql|mariadb)", "mysql"),
        (r"provider\s*=\s*\"(postgresql|postgres|cockroachdb)\"", "postgres"),
        (r"provider\s*=\s*\"mysql\"", "mysql"),
        (r"provider\s*=\s*\"sqlite\"", "sqlite"),
        (r"(?:DATABASE_URL|DB_URL)\s*=\s*[\"']?postgres(?:ql)?://", "postgres"),
        (r"(?:DATABASE_URL|DB_URL)\s*=\s*[\"']?mysql://", "mysql"),
        (r"image:\s*[\"']?(?:postgres|postgis|supabase/postgres)", "postgres"),
        (r"image:\s*[\"']?(?:mysql|mariadb|percona)", "mysql"),
        (r"\"@supabase/supabase-js\"", "postgres"),
        (r"type\s*:\s*['\"](postgres)['\"]", "postgres"),
        (r"type\s*:\s*['\"](mysql|mariadb)['\"]", "mysql"),
        (r"dialect\s*:\s*['\"](postgres)['\"]", "postgres"),
        (r"dialect\s*:\s*['\"](mysql|mariadb)['\"]", "mysql"),
        (r"django\.db\.backends\.(postgresql)", "postgres"),
        (r"django\.db\.backends\.(mysql)", "mysql"),
        (r"\"(pg|postgres)\"\s*:", "postgres"),
        (r"\"(mysql2?)\"\s*:", "mysql"),
    ]
    for rx, engine in patterns:
        for path, txt in checks:
            if re.search(rx, txt, re.I):
                return engine, f"{rel(path, root)} matches /{rx}/"
    base = root if os.path.isdir(root) else os.path.dirname(root)
    if os.path.isdir(os.path.join(base, "supabase")):
        return "postgres", "supabase/ directory"
    return "unknown", "no engine hint found (pass --engine)"


def detect_stacks(root: str):
    stacks = set()
    for path in _glob_configs(root, r"composer\.json|package\.json|pom\.xml|build\.gradle(\.kts)?|"
                                   r"requirements[\w.-]*\.txt|pyproject\.toml|go\.mod|Gemfile|schema\.prisma"):
        txt = _read_if(path)
        name = os.path.basename(path)
        if name == "composer.json" and "laravel/framework" in txt:
            stacks.add("laravel")
        if name == "schema.prisma" or "@prisma/client" in txt:
            stacks.add("prisma")
        if '"typeorm"' in txt:
            stacks.add("typeorm")
        if '"sequelize"' in txt or '"sequelize-typescript"' in txt:
            stacks.add("sequelize")
        if "@supabase/supabase-js" in txt:
            stacks.add("supabase")
        if '"drizzle-orm"' in txt:
            stacks.add("drizzle")
        if '"mongoose"' in txt:
            stacks.add("mongoose")
        if name.startswith(("pom.xml", "build.gradle")) and re.search(r"data-jpa|hibernate", txt):
            stacks.add("spring-jpa")
        if name.startswith(("requirements", "pyproject")) and re.search(r"(?i)\bdjango\b", txt):
            stacks.add("django")
        if name.startswith(("requirements", "pyproject")) and re.search(r"(?i)sqlalchemy", txt):
            stacks.add("sqlalchemy")
        if name == "go.mod" and "gorm.io" in txt:
            stacks.add("gorm")
        if name == "Gemfile" and re.search(r"\brails\b|activerecord", txt):
            stacks.add("rails")
    return sorted(stacks)


# ------------------------------------------------------------------------ findings

@dataclass
class Finding:
    rule: str
    severity: str            # high | medium | low | info
    file: str
    line: int
    message: str
    confidence: str = "medium"   # high | medium | low
    snippet: str = ""
    hint: str = ""
    extra: dict = field(default_factory=dict)


def sort_findings(findings):
    return sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.rule, f.file, f.line))


def dedupe(findings):
    seen, out = set(), []
    for f in findings:
        key = (f.rule, f.file, f.line)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def filter_severity(findings, min_severity: str):
    limit = SEVERITY_ORDER.get(min_severity, 3)
    return [f for f in findings if SEVERITY_ORDER.get(f.severity, 9) <= limit]


def snippet_of(lines: LineIndex, lineno: int, width: int = 180) -> str:
    s = lines.line_text(lineno).strip()
    return s if len(s) <= width else s[: width - 1] + "…"


FENCE_LANG = {"php": "php", "blade": "blade", "java": "java", "kotlin": "kotlin", "js": "ts",
              "python": "python", "go": "go", "ruby": "ruby", "sql": "sql", "prisma": "prisma"}


def render_markdown(title: str, meta, findings, limit: int = 300, extra_sections=None) -> str:
    out = [f"# {title}", ""]
    for k, v in meta:
        out.append(f"- **{k}:** {v}")
    out.append("")
    counts = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    by_rule = {}
    for f in findings:
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
    out.append("## Summary")
    out.append("")
    out.append("| Severity | Count |")
    out.append("|---|---|")
    for sev in ("high", "medium", "low", "info"):
        if counts.get(sev):
            out.append(f"| {sev} | {counts[sev]} |")
    out.append("")
    if by_rule:
        out.append("| Rule | Hits |")
        out.append("|---|---|")
        for rule, n in sorted(by_rule.items(), key=lambda kv: (-kv[1], kv[0])):
            out.append(f"| {rule} | {n} |")
        out.append("")
    out.append("> Heuristic leads, not verdicts: verify each one in context before reporting it.")
    out.append("")
    shown = 0
    for sev in ("high", "medium", "low", "info"):
        group = [f for f in findings if f.severity == sev]
        if not group:
            continue
        out.append(f"## {sev.upper()} ({len(group)})")
        out.append("")
        for f in group:
            if shown >= limit:
                break
            shown += 1
            out.append(f"{shown}. **{f.rule}** · `{f.file}:{f.line}` · confidence: {f.confidence}")
            out.append(f"   {f.message}")
            if f.snippet:
                lang = FENCE_LANG.get(f.extra.get("lang", ""), "")
                out.append(f"   ```{lang}")
                out.append(f"   {f.snippet}")
                out.append("   ```")
            if f.hint:
                out.append(f"   → {f.hint}")
            out.append("")
    if shown < len(findings):
        out.append(f"_… {len(findings) - shown} more findings not shown (use --limit or --json)._")
        out.append("")
    for section in extra_sections or []:
        out.append(section)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def to_json(meta, findings, **extra) -> str:
    payload = {"meta": dict(meta), "findings": [asdict(f) for f in findings]}
    payload.update(extra)
    return json.dumps(payload, indent=2, ensure_ascii=False)


def base_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("root", nargs="?", default=".", help="project directory or single file (default: .)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--min-severity", default="info", choices=list(SEVERITY_ORDER),
                   help="hide findings below this severity (default: info = show all)")
    p.add_argument("--limit", type=int, default=300, help="max findings printed in markdown mode")
    p.add_argument("--engine", default="auto", choices=["auto", "postgres", "mysql", "sqlite", "unknown"],
                   help="database engine (default: auto-detect from config files)")
    return p


def resolve_engine(args):
    if args.engine != "auto":
        return args.engine, "--engine flag"
    return detect_engine(args.root)
