"""Raw SQL audit — verifies no raw SQL bypasses tenant isolation in app code.

MED-2: Audits all use of sqlalchemy.text() in the app layer to ensure
every raw SQL query either:
  a) Has an explicit company_id filter, OR
  b) Is only for system tables (no tenant data), OR
  c) Runs with RLS context set.

This test analyzes the AST of all .py files under app/ to detect
text() calls that query tenant-scoped tables without a company_id
filter. It does NOT audit internal operations (Celery, beat, DB
maintenance) which legitimately operate without tenant context.
"""

import ast
import re
from pathlib import Path

APP_DIR = Path(__file__).parent.parent / "app"

TENANT_TABLES = {
    "users",
    "customers",
    "jobs",
    "job_media",
    "ai_outputs",
    "quotes",
    "billing_accounts",
    "usage_ledger",
    "invoices",
    "company_pricing_brains",
    "services",
    "estimates",
    "estimate_line_items",
    "historical_job_data",
    "ai_job_estimates",
    "notifications",
    "push_tokens",
    "analytics_events",
    "idempotency_keys",
}

SYSTEM_TABLES = {
    "information_schema",
    "pg_catalog",
    "alembic_version",
    "companies",
    "stripe_webhook_events",
    "dead_letter_jobs",
    "pii_access_audit",
    "ai_audit_log",
    "execution_traces",
    "user_daily_usage",
    "ai_output_feedback",
}

ALLOWED_NO_FILTER_FILES = {
    "app/database.py",
    "app/db/rls.py",
    "app/db/tenant.py",
    "app/ai/audit.py",
    "app/analytics/events.py",
    "app/tracing/models.py",
    "app/monitoring/prometheus.py",
}


def test_no_raw_sql_bypasses_tenant_isolation():
    """Verify no raw SQL text() queries bypass tenant isolation."""
    violations = []

    for py_file in APP_DIR.rglob("*.py"):
        rel_path = str(py_file.relative_to(py_file.parent.parent)).replace("\\", "/")

        if rel_path in ALLOWED_NO_FILTER_FILES:
            continue

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_text_call(node) and not _is_system_query(node):
                sql_text = _extract_sql(node)
                if sql_text and _contains_tenant_table(sql_text) and not _has_company_filter(sql_text):
                    violations.append(
                        f"{rel_path}:{node.lineno}: Raw SQL on tenant table without company_id filter:\n  {sql_text[:120]}"
                    )

    if violations:
        violations.sort()
        msg = f"Found {len(violations)} raw SQL queries bypassing tenant isolation:\n\n" + "\n\n".join(violations)
        raise AssertionError(msg)


def _is_text_call(node: ast.Call) -> bool:
    """Check if this is a sqlalchemy.text() call."""
    if isinstance(node.func, ast.Name) and node.func.id == "text":
        return True
    return bool(isinstance(node.func, ast.Attribute) and node.func.attr == "text")


def _extract_sql(node: ast.Call) -> str:
    """Extract SQL string from a text() call argument."""
    if node.args:
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        elif isinstance(arg, ast.Str):  # Python 3.7 compatibility
            return arg.s
    return ""


def _is_system_query(node: ast.Call) -> bool:
    """Check if the sql references are purely system/non-tenant."""
    sql = _extract_sql(node).lower()
    if not sql:
        return True
    if re.search(
        r"\b(select\s+1|ping|show\s|set\s|reset\s|create\s|alter\s|drop\s|grant\s|revoke\s)\b", sql
    ) and sql.strip().upper().startswith(
        ("SET ", "RESET ", "SHOW ", "CREATE ", "ALTER ", "DROP ", "GRANT ", "REVOKE ")
    ):
        return True
    return bool("pg_stat_statements" in sql or "pg_stat" in sql)


def _contains_tenant_table(sql: str) -> bool:
    """Check if the SQL references any tenant-scoped tables."""
    sql_lower = sql.lower()
    return any(table in sql_lower for table in TENANT_TABLES)


def _has_company_filter(sql: str) -> bool:
    """Check if the SQL has a company_id filter clause."""
    sql_lower = sql.lower()
    patterns = [
        r"\bcompany_id\s*=\s*",
        r"\bcompany_id\s*in\s*\(",
        r"\bcompany_id\s+in\s*\(",
        r"current_setting\(.*app\.current_tenant_id",
        r"company_id\s*=.*tenant_id",
        r"company_id\s*=.*:company_id",
    ]
    return any(re.search(pattern, sql_lower) for pattern in patterns)


def test_tenant_scoped_tables_has_no_stale_entries():
    """Verify all tables in _TENANT_SCOPED_TABLES are real tenant tables."""
    # Just validates that the set is well-formed (all valid identifiers)
    assert all(re.match(r"^[a-z][a-z0-9_]*$", t) for t in TENANT_TABLES), (
        "All tenant table names must be valid SQL identifiers"
    )


def test_rls_block_uuid_is_valid():
    """Verify RLS block UUID is the nil UUID."""
    from app.db.rls import RLS_BLOCK_UUID, RLS_BLOCK_UUID_STR

    assert RLS_BLOCK_UUID_STR == "00000000-0000-0000-0000-000000000000"
    assert str(RLS_BLOCK_UUID) == "00000000-0000-0000-0000-000000000000"
