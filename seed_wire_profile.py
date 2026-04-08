"""
seed_wire_profile.py -- Generate a draft .wire.json from a schema JSON.

Reads a schema JSON (produced by extract_schema.py) and outputs a draft
wire profile to stdout. Redirect to a file to save.

Usage:
    python seed_wire_profile.py "Mount Joy Borough"
    python seed_wire_profile.py "Mount Joy Borough" > ~/Documents/Graftd/MountJoy.wire.json
    python seed_wire_profile.py path/to/schema.json
"""

import json
import sys
from pathlib import Path

SCHEMAS_DIR = Path.home() / "Documents" / "Graftd" / "schemas"


def find_schema(arg):
    """Resolve argument to a schema file path."""
    # Direct path
    p = Path(arg)
    if p.exists() and p.suffix == ".json":
        return p

    # Model name lookup in schemas dir
    candidate = SCHEMAS_DIR / f"{arg}.schema.json"
    if candidate.exists():
        return candidate

    return None


def seed_wire_profile(schema):
    """Build a wire profile from schema data."""
    draft = schema.get("wireProfileDraft", {})

    head_param = draft.get("headParam")
    jamb_param = draft.get("jambParam")
    sheet_prefix = draft.get("sheetPrefix")

    if not head_param and not jamb_param:
        # Try to find from schedule fields directly
        for ds in schema.get("schedules", {}).get("doorSchedules", []):
            refs = ds.get("detailRefFields") or {}
            if "head" in refs and not head_param:
                head_param = refs["head"]
            if "jamb" in refs and not jamb_param:
                jamb_param = refs["jamb"]

    wire = {}
    if head_param:
        wire["headParam"] = head_param
    if jamb_param:
        wire["jambParam"] = jamb_param
    if sheet_prefix:
        wire["sheetPrefix"] = sheet_prefix

    return wire


def main():
    if len(sys.argv) < 2:
        print("Usage: python seed_wire_profile.py <model_name_or_schema_path>", file=sys.stderr)
        print(file=sys.stderr)
        print("Examples:", file=sys.stderr)
        print('  python seed_wire_profile.py "Mount Joy Borough"', file=sys.stderr)
        print("  python seed_wire_profile.py path/to/schema.json", file=sys.stderr)
        sys.exit(1)

    arg = sys.argv[1]
    schema_path = find_schema(arg)
    if not schema_path:
        print(f"Schema not found: {arg}", file=sys.stderr)
        print(f"Looked in: {SCHEMAS_DIR}", file=sys.stderr)

        # List available schemas
        if SCHEMAS_DIR.exists():
            schemas = sorted(SCHEMAS_DIR.glob("*.schema.json"))
            if schemas:
                print(file=sys.stderr)
                print("Available schemas:", file=sys.stderr)
                for s in schemas:
                    name = s.name.replace(".schema.json", "")
                    print(f"  {name}", file=sys.stderr)
        sys.exit(1)

    with open(schema_path) as f:
        schema = json.load(f)

    wire = seed_wire_profile(schema)

    if not wire:
        print(f"No wire profile fields detected for: {schema.get('firm', arg)}", file=sys.stderr)
        print("This model may not have detail reference fields in its schedules.", file=sys.stderr)
        sys.exit(1)

    # Report to stderr, output JSON to stdout
    firm = schema.get("firm", arg)
    confidence = schema.get("wireProfileDraft", {}).get("confidence", "unknown")
    notes = schema.get("wireProfileDraft", {}).get("notes", "")
    print(f"Firm: {firm}", file=sys.stderr)
    print(f"Confidence: {confidence}", file=sys.stderr)
    if notes:
        print(f"Notes: {notes}", file=sys.stderr)
    print(file=sys.stderr)

    json.dump(wire, sys.stdout, indent=2)
    print()  # trailing newline


if __name__ == "__main__":
    main()
