"""AI Sanitization CI Gate — run as part of CI to enforce sanitization contract.

Three checks:

  1. No raw orchestrator.generate_chat_output() calls outside sanctioned files
  2. No function that calls an orchestrator AI method without also calling sanitizer
  3. Middleware path list covers all AI-returning routes

Exit codes: 0 = pass, 1 = fail
"""

import ast
import os
import re
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP_DIR = os.path.join(PROJECT_ROOT, "app")
MIDDLEWARE_FILE = os.path.join(APP_DIR, "middleware", "sanitize.py")
MAIN_FILE = os.path.join(APP_DIR, "main.py")

SANCTIONED_FILES = {
    os.path.join(PROJECT_ROOT, "app", "ai", "gateway.py"),
    os.path.join(PROJECT_ROOT, "app", "estimates", "engine.py"),
}
SANCTIONED_RELPATHS = {"app/ai/gateway.py", "app/estimates/engine.py"}
ORCHESTRATOR_IMPORT_SOURCES = {"app.ai.orchestrator", "app.ai.gateway"}
SANITIZE_FUNCTIONS = {"_sanitize_output_dict", "_sanitize_output_text", "sanitize_llm_output_text"}
ORCHESTRATOR_METHODS = {
    "generate_chat_output",
    "generate_structured_output",
    "transcribe_audio",
    "analyze_images",
}


def _relpath(abspath: str) -> str:
    try:
        return os.path.relpath(abspath, PROJECT_ROOT).replace("\\", "/")
    except ValueError:
        return abspath


def _get_python_files(root: str) -> list[str]:
    matches = []
    for dirpath, _dirnames, filenames in os.walk(root):
        matches.extend(os.path.join(dirpath, f) for f in filenames if f.endswith(".py"))
    return sorted(matches)


def _is_sanctioned(filepath: str) -> bool:
    return filepath in SANCTIONED_FILES


# ── Check 1: No raw orchestrator calls outside sanctioned files ────────────


class OrchestratorCallVisitor(ast.NodeVisitor):
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.errors: list[str] = []
        self._orchestrator_alias = None

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            if alias.name in ORCHESTRATOR_IMPORT_SOURCES:
                self._orchestrator_alias = alias.asname or alias.name.split(".")[-1]
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module in ORCHESTRATOR_IMPORT_SOURCES:
            for alias in node.names:
                if alias.name == "orchestrator":
                    self._orchestrator_alias = alias.asname or alias.name
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Attribute):
            chain = self._resolve_attr_chain(node.func)
            if chain:
                full = ".".join(chain)
                alias = self._orchestrator_alias or "orchestrator"
                for meth in ORCHESTRATOR_METHODS:
                    if full == f"{alias}.{meth}" or full.endswith(f".{meth}"):
                        if full == f"{alias}.{meth}" or (len(chain) >= 2 and chain[0] == alias):
                            self.errors.append(
                                f"{_relpath(self.filepath)}:{node.lineno}: "
                                f"'{full}' — raw orchestrator call outside sanctioned file. "
                                f"Must go through _sanitize_output_dict() wrapper. "
                                f"Sanctioned: app/ai/gateway.py, app/estimates/engine.py"
                            )
        self.generic_visit(node)

    def _resolve_attr_chain(self, node: ast.AST) -> list[str] | None:
        parts = []
        current = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        else:
            return None
        return list(reversed(parts))


def _check_raw_orchestrator_calls(files: list[str]) -> list[str]:
    errors = []
    for fpath in files:
        if _is_sanctioned(fpath):
            continue
        if "test_" in os.path.basename(fpath):
            continue
        try:
            with open(fpath, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=fpath)
        except (SyntaxError, UnicodeDecodeError) as e:
            errors.append(f"Skipping {_relpath(fpath)}: parse error {e}")
            continue
        visitor = OrchestratorCallVisitor(fpath)
        visitor.visit(tree)
        errors.extend(visitor.errors)
    return errors


# ── Check 2: Unsanitized return paths ──────────────────────────────────────


class ReturnPathVisitor(ast.NodeVisitor):
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.errors: list[str] = []
        self._imported_orchestrator = False
        self._imported_sanitizer = False

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            if "orchestrator" in alias.name:
                self._imported_orchestrator = True
            if "_sanitize" in alias.name or "sanitize" in alias.name.lower():
                self._imported_sanitizer = True
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = node.module or ""
        for alias in node.names:
            name = alias.name
            if "orchestrator" in name or "orchestrator" in module:
                self._imported_orchestrator = True
            if "_sanitize" in name or "sanitize" in name.lower() or "sanitize" in module.lower():
                self._imported_sanitizer = True
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._check_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._check_function(node)

    def _check_function(self, node):
        called_orch = False
        called_san = False
        has_return = False
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                s = ast.unparse(child) if hasattr(ast, "unparse") else ""
                for meth in ORCHESTRATOR_METHODS:
                    if meth in s and ("orchestrator" in s or "gateway" in s):
                        called_orch = True
                for sf in SANITIZE_FUNCTIONS:
                    if sf in s:
                        called_san = True
            elif isinstance(child, (ast.Return, ast.Yield)):
                has_return = True
        if called_orch and not called_san and has_return:
            self.errors.append(
                f"{_relpath(self.filepath)}: function '{node.name}' calls orchestrator "
                f"but no sanitizer call found in same function"
            )


def _check_sanitizer_usage(files: list[str]) -> list[str]:
    errors = []
    for fpath in files:
        if _is_sanctioned(fpath):
            continue
        if "test_" in os.path.basename(fpath):
            continue
        try:
            with open(fpath, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=fpath)
        except (SyntaxError, UnicodeDecodeError):
            continue
        visitor = ReturnPathVisitor(fpath)
        visitor.visit(tree)
        errors.extend(visitor.errors)
    return errors


# ── Check 3: Middleware coverage of AI routes ───────────────────────────────


def _read_middleware_paths() -> set[str]:
    if not os.path.exists(MIDDLEWARE_FILE):
        return set()
    with open(MIDDLEWARE_FILE, encoding="utf-8") as f:
        source = f.read()
    paths = set()
    for line in source.splitlines():
        s = line.strip()
        for marker in ("/ai/", "/estimates/", "/quotes/"):
            if marker in s and ("'" in s or '"' in s):
                m = re.search(r"[\"']([^\"']*" + re.escape(marker) + r"[^\"']*)[\"']", s)
                if m:
                    paths.add(m.group(1).replace("{prefix}", "").rstrip("/"))
    return paths


def _get_route_prefixes() -> set[str]:
    if not os.path.exists(MAIN_FILE):
        return set()
    with open(MAIN_FILE, encoding="utf-8") as f:
        source = f.read()
    prefixes = set()
    for m in re.finditer(r'prefix\s*=\s*"([^"]+)"', source):
        prefixes.add(m.group(1).rstrip("/"))
    return prefixes


def _check_middleware_coverage() -> list[str]:
    errors = []
    middleware_paths = _read_middleware_paths()
    known_ai_prefixes = {"/ai", "/estimates", "/quotes"}
    route_prefixes = _get_route_prefixes()
    for p in route_prefixes:
        base = p.split("/")[1] if p.startswith("/") else p
        if (
            any(base == k.split("/")[1] for k in known_ai_prefixes)
            and p not in middleware_paths
            and not any(mp.startswith(p) for mp in middleware_paths)
        ):
            errors.append(
                f"Route prefix '{p}' returns AI content but is not covered by "
                f"AIResponseSanitizationMiddleware path list in middleware/sanitize.py"
            )
    return errors


# ── Main ────────────────────────────────────────────────────────────────────

_CHECKS = []


def check(fn):
    _CHECKS.append(fn)
    return fn


@check
def check_raw_orchestrator_calls() -> list[str]:
    files = _get_python_files(APP_DIR)
    return _check_raw_orchestrator_calls(files)


@check
def check_sanitizer_usage() -> list[str]:
    files = _get_python_files(APP_DIR)
    return _check_sanitizer_usage(files)


@check
def check_middleware_coverage() -> list[str]:
    return _check_middleware_coverage()


def main() -> int:
    all_errors = []
    for fn in _CHECKS:
        errors = fn()
        all_errors.extend(errors)

    if all_errors:
        print("AI SANITIZATION CI GATE — FAILED")
        print("=" * 60)
        for err in all_errors:
            print(f"  FAIL  {err}")
        print()
        print(f"Found {len(all_errors)} issue(s).")
        print("Contract: All AI outputs must pass _sanitize_output_dict() before leaving trust boundary.")
        return 1
    print("AI sanitization CI gate — PASSED")
    print("  - Raw orchestrator calls: clean")
    print("  - Sanitizer usage: clean")
    print("  - Middleware coverage: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
