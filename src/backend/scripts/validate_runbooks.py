"""Runbook Auto-Validation Script

Parses all runbooks across the repository and checks for required sections:
- Trigger (what activates the runbook)
- Diagnosis (how to identify the problem)
- Mitigation (steps to resolve)
- Rollback (how to undo changes)

Usage:
    python scripts/validate_runbooks.py [--dir docs/runbooks] [--dir ops/runbooks]
"""

import argparse
import sys
from pathlib import Path


REQUIRED_SECTIONS = [
    ("trigger", ["trigger", "when to use", "activation", "alert"]),
    ("diagnosis", ["diagnosis", "symptoms", "identification", "detection", "investigation"]),
    ("mitigation", ["mitigation", "resolution", "steps", "procedure", "remediation", "fix"]),
    ("rollback", ["rollback", "revert", "undo", "recovery", "backout"]),
]


def _has_section(content: str, keywords: list[str]) -> bool:
    content_lower = content.lower()
    for kw in keywords:
        if ("# " + kw) in content_lower or ("## " + kw) in content_lower or ("##" + kw) in content_lower:
            return True
    return False


def validate_runbook(filepath: Path) -> dict:
    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception:
        content = filepath.read_text()

    missing = []
    for section_name, keywords in REQUIRED_SECTIONS:
        if not _has_section(content, keywords):
            missing.append(section_name)

    return {
        "file": str(filepath),
        "missing": missing,
        "valid": len(missing) == 0,
        "size": len(content),
    }


def validate_directory(dirpath: str) -> tuple[int, int, list[dict]]:
    valid = 0
    invalid = 0
    results = []

    for md_file in Path(dirpath).rglob("*.md"):
        result = validate_runbook(md_file)
        results.append(result)
        if result["valid"]:
            valid += 1
        else:
            invalid += 1

    return valid, invalid, results


def main():
    parser = argparse.ArgumentParser(description="Validate runbook completeness")
    parser.add_argument("--dir", action="append", dest="dirs", default=[], help="Directory to scan")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--fail-on-missing", action="store_true", help="Exit non-zero if any runbook missing sections")
    args = parser.parse_args()

    dirs = args.dirs or ["docs/runbooks", "ops/runbooks", "src/backend/docs/runbooks"]

    total_valid = 0
    total_invalid = 0
    all_results = []

    for d in dirs:
        p = Path(d)
        if not p.exists():
            print(f"WARNING: Directory not found: {d}", file=sys.stderr)
            continue
        v, i, results = validate_directory(str(p))
        total_valid += v
        total_invalid += i
        all_results.extend(results)

    if args.json:
        import json
        print(json.dumps({"valid": total_valid, "invalid": total_invalid, "results": all_results}, indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"  RUNBOOK VALIDATION REPORT")
        print(f"{'='*60}")
        print(f"  Valid: {total_valid}")
        print(f"  Invalid: {total_invalid}")
        print(f"  Total: {total_valid + total_invalid}")
        print(f"{'='*60}\n")

        for r in all_results:
            if not r["valid"]:
                print(f"  ✗ {r['file']}")
                print(f"    Missing sections: {', '.join(r['missing'])}")

        if total_invalid == 0:
            print(f"  ✓ ALL RUNBOOKS HAVE REQUIRED SECTIONS\n")
        else:
            print(f"  ⚠ {total_invalid} runbook(s) need attention\n")

    if args.fail_on_missing and total_invalid > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
