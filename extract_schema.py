"""
extract_schema.py -- Extract firm schema from Revit model dump data.

Reads dump data JSONs from Documents/Graftd/dumps/, produces schema JSONs
in Documents/Graftd/schemas/{model_name}.schema.json.

Usage:
    python extract_schema.py                        # all models (latest dump each)
    python extract_schema.py "Mount Joy Borough"    # single model
"""

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

DUMPS_DIR = Path.home() / "Documents" / "Graftd" / "dumps"
SCHEMAS_DIR = Path.home() / "Documents" / "Graftd" / "schemas"

# Patterns that suggest a field is a detail reference
DETAIL_REF_PATTERNS = [
    re.compile(r"\bhead\b.*\bdetail\b", re.IGNORECASE),
    re.compile(r"\bjamb\b.*\bdetail\b", re.IGNORECASE),
    re.compile(r"\bsill\b.*\bdetail\b", re.IGNORECASE),
    re.compile(r"\bdetail\b.*\bref\b", re.IGNORECASE),
    re.compile(r"\bhead\b.*\bref\b", re.IGNORECASE),
    re.compile(r"\bjamb\b.*\bref\b", re.IGNORECASE),
    re.compile(r"\bsill\b.*\bref\b", re.IGNORECASE),
    re.compile(r"\bspecial\b.*\bdetail\b", re.IGNORECASE),
    re.compile(r"\bdetail\b.*\(", re.IGNORECASE),
]

# Detail ref role detection: which role does a field name map to?
ROLE_PATTERNS = {
    "head": re.compile(r"\bhead\b", re.IGNORECASE),
    "jamb": re.compile(r"\bjamb\b", re.IGNORECASE),
    "sill": re.compile(r"\bsill\b", re.IGNORECASE),
    "special": re.compile(r"\bspecial\b", re.IGNORECASE),
}

# Universal door parameters (Revit built-in, present in most models)
UNIVERSAL_PARAMS = {
    "Area", "Category", "Head Height", "Level", "Mark", "Width", "Height",
    "Volume", "Phase Created", "Phase Demolished", "Host Id", "Sill Height",
    "Family", "Family and Type", "Type", "Type Name", "Family Name",
    "Export to IFC", "Export Type to IFC", "IfcGUID", "Type IfcGUID",
    "Design Option", "Image",
}

UNIVERSAL_TYPE_PARAMS = {
    "Category", "Family Name", "Type Name", "Width", "Height",
    "Export Type to IFC", "Type IfcGUID", "Design Option",
    "Code Name", "Define Thermal Properties by", "Analytic Construction",
    "Classification Title", "Classification Number", "Assembly Description",
    "Assembly Code", "Type Mark", "Keynote", "Model", "Manufacturer",
    "Type Comments", "URL", "Description", "Cost", "OmniClass Title",
    "OmniClass Number",
}


def find_latest_dumps(model_filter=None):
    """Find the latest _data.json for each unique model name."""
    dumps = {}
    for f in DUMPS_DIR.glob("*_data.json"):
        # Format: {ModelName}_{timestamp}_data.json
        # Timestamp is YYYYMMDD_HHMMSS (14 chars + underscore = 15)
        name = f.name
        # Strip _data.json suffix
        stem = name[:-len("_data.json")]
        # Extract timestamp: last 15 chars are YYYYMMDD_HHMMSS
        match = re.match(r"^(.+)_(\d{8}_\d{6})$", stem)
        if not match:
            continue
        model_name = match.group(1)
        timestamp = match.group(2)

        if model_filter and model_filter.lower() != model_name.lower():
            continue

        if model_name not in dumps or timestamp > dumps[model_name][1]:
            dumps[model_name] = (f, timestamp)

    return {name: path for name, (path, _) in dumps.items()}


def is_detail_ref_field(field_name):
    """Check if a field name looks like a detail reference."""
    return any(p.search(field_name) for p in DETAIL_REF_PATTERNS)


def detect_detail_ref_role(field_name):
    """Detect which role (head/jamb/sill/special) a detail ref field has."""
    for role, pattern in ROLE_PATTERNS.items():
        if pattern.search(field_name):
            return role
    return "unknown"


def detect_family_prefix(family_names):
    """Detect common naming convention prefix from family names."""
    if not family_names:
        return None

    # Look for common suffixes/patterns like _CRA_v23, _v14
    suffix_pattern = re.compile(r"[_]([A-Z]{2,}[_]v\d+)\b")
    prefix_pattern = re.compile(r"^([A-Z]{2,}[_])")
    suffixes = Counter()
    prefixes = Counter()

    for name in family_names:
        for m in suffix_pattern.finditer(name):
            suffixes[m.group(1)] += 1
        m = prefix_pattern.match(name)
        if m:
            prefixes[m.group(1)] += 1

    if suffixes:
        return suffixes.most_common(1)[0][0]
    if prefixes:
        return prefixes.most_common(1)[0][0]
    return None


def detect_sheet_prefix(data):
    """Detect the most common sheet number prefix from title blocks."""
    tb = data.get("categories", {}).get("TitleBlocks", {})
    families = tb.get("families", {})
    sheet_numbers = []

    for fam_data in families.values():
        for type_data in fam_data.values():
            if not isinstance(type_data, dict):
                continue
            for inst in type_data.get("instances", []):
                sn = inst.get("parameters", {}).get("Sheet Number", "")
                if sn and sn != "00":
                    sheet_numbers.append(sn)

    if not sheet_numbers:
        return None

    # Extract prefix (letters + optional digits before the dot/dash)
    prefix_counter = Counter()
    for sn in sheet_numbers:
        m = re.match(r"^([A-Za-z]+\d*)", sn)
        if m:
            prefix_counter[m.group(1)] += 1

    if prefix_counter:
        return prefix_counter.most_common(1)[0][0]
    return None


def extract_door_info(data):
    """Extract door families, types, instances, and parameters."""
    doors_cat = data.get("categories", {}).get("Doors", {})
    if not doors_cat:
        return {
            "totalCount": 0,
            "families": {},
            "familyPrefix": None,
            "instanceParams": {"universal": [], "firmSpecific": [], "graftd": []},
            "typeParams": {"universal": [], "firmSpecific": []},
            "instanceParamSamples": {},
            "typeParamSamples": {},
        }

    total_count = doors_cat.get("instanceCount", 0)
    families_raw = doors_cat.get("families", {})

    families = {}
    all_instance_params = defaultdict(list)  # param_name -> [sample_values]
    all_type_params = defaultdict(list)
    family_names = list(families_raw.keys())

    for fam_name, fam_data in families_raw.items():
        if not isinstance(fam_data, dict):
            continue

        type_count = 0
        instance_count = 0

        for type_name, type_data in fam_data.items():
            if not isinstance(type_data, dict):
                continue
            type_count += 1
            count = type_data.get("count", 0)
            instance_count += count

            # Collect type parameters
            for k, v in type_data.get("typeParameters", {}).items():
                if v and str(v).strip() and str(v) != "-1":
                    all_type_params[k].append(str(v))

            # Collect instance parameters
            for inst in type_data.get("instances", []):
                for k, v in inst.get("parameters", {}).items():
                    if v and str(v).strip() and str(v) != "-1":
                        all_instance_params[k].append(str(v))

        families[fam_name] = {"types": type_count, "instances": instance_count}

    # Classify parameters
    inst_universal = sorted(k for k in all_instance_params if k in UNIVERSAL_PARAMS)
    inst_graftd = sorted(k for k in all_instance_params if k.startswith("g_"))
    inst_firm = sorted(
        k for k in all_instance_params
        if k not in UNIVERSAL_PARAMS and not k.startswith("g_")
    )

    type_universal = sorted(k for k in all_type_params if k in UNIVERSAL_TYPE_PARAMS)
    type_firm = sorted(
        k for k in all_type_params if k not in UNIVERSAL_TYPE_PARAMS
    )

    # Build sample values (first non-empty, up to 3 unique)
    inst_samples = {}
    for k, vals in all_instance_params.items():
        unique = list(dict.fromkeys(v for v in vals if v))[:3]
        if unique:
            inst_samples[k] = unique

    type_samples = {}
    for k, vals in all_type_params.items():
        unique = list(dict.fromkeys(v for v in vals if v))[:3]
        if unique:
            type_samples[k] = unique

    return {
        "totalCount": total_count,
        "families": families,
        "familyPrefix": detect_family_prefix(family_names),
        "instanceParams": {
            "universal": inst_universal,
            "firmSpecific": inst_firm,
            "graftd": inst_graftd,
        },
        "typeParams": {
            "universal": type_universal,
            "firmSpecific": type_firm,
        },
        "instanceParamSamples": inst_samples,
        "typeParamSamples": type_samples,
    }


def extract_schedules(data):
    """Extract schedule inventory with door schedule and key schedule focus."""
    schedules = data.get("schedules", [])
    door_schedules = []
    key_schedules = []
    all_schedules = []

    for s in schedules:
        name = s.get("name", "")
        category = s.get("category", "")
        fields = [f.get("name", "") for f in s.get("fields", [])]
        row_count = s.get("rowCount", 0)
        is_key = s.get("isKeySchedule", False)

        entry = {
            "name": name,
            "category": category,
            "fields": fields,
            "rowCount": row_count,
        }

        if is_key:
            # Detect purpose from fields
            has_door_refs = any(is_detail_ref_field(f) for f in fields)
            purpose = "Door type to detail reference lookup" if has_door_refs else "Key lookup"
            key_entry = {**entry, "purpose": purpose}
            key_schedules.append(key_entry)

        if category == "Doors" or "door" in name.lower():
            # Check for detail ref fields
            detail_ref_fields = {}
            for f in fields:
                if is_detail_ref_field(f):
                    role = detect_detail_ref_role(f)
                    detail_ref_fields[role] = f

            door_entry = {
                **entry,
                "hasDetailRefs": bool(detail_ref_fields),
                "detailRefFields": detail_ref_fields if detail_ref_fields else None,
            }
            door_schedules.append(door_entry)

        all_schedules.append(entry)

    return {
        "doorSchedules": door_schedules,
        "keySchedules": key_schedules,
        "allSchedules": all_schedules,
    }


def extract_categories(data):
    """Extract category breakdown."""
    cats = data.get("categories", {})
    breakdown = {}
    for cat_name, cat_data in cats.items():
        if isinstance(cat_data, dict):
            count = cat_data.get("instanceCount", 0)
            fam_count = len(cat_data.get("families", {}))
            breakdown[cat_name] = {"instances": count, "families": fam_count}
    return breakdown


def build_wire_profile_draft(door_info, schedule_info, sheet_prefix):
    """Generate a draft wire profile suggestion."""
    # Collect all detail ref fields from door schedules
    all_refs = {}
    for ds in schedule_info["doorSchedules"]:
        if ds.get("detailRefFields"):
            all_refs.update(ds["detailRefFields"])

    # Also check key schedules
    for ks in schedule_info["keySchedules"]:
        for f in ks["fields"]:
            if is_detail_ref_field(f):
                role = detect_detail_ref_role(f)
                if role not in all_refs:
                    all_refs[role] = f

    head_param = all_refs.get("head")
    jamb_param = all_refs.get("jamb")

    if not head_param and not jamb_param:
        return {
            "headParam": None,
            "jambParam": None,
            "sheetPrefix": sheet_prefix,
            "confidence": "none",
            "notes": "No detail reference fields detected in any schedule",
        }

    confidence = "high" if head_param and jamb_param else "medium"
    notes_parts = []
    if door_info["familyPrefix"]:
        notes_parts.append(f"{door_info['familyPrefix']} families")
    if schedule_info["keySchedules"]:
        notes_parts.append("key schedule drives detail refs")
    if not notes_parts:
        notes_parts.append("detail ref fields found in door schedules")

    return {
        "headParam": head_param,
        "jambParam": jamb_param,
        "sheetPrefix": sheet_prefix,
        "confidence": confidence,
        "notes": ", ".join(notes_parts),
    }


def extract_schema(model_name, dump_path):
    """Extract full schema from a dump data file."""
    with open(dump_path) as f:
        data = json.load(f)

    door_info = extract_door_info(data)
    schedule_info = extract_schedules(data)
    categories = extract_categories(data)
    sheet_prefix = detect_sheet_prefix(data)
    wire_draft = build_wire_profile_draft(door_info, schedule_info, sheet_prefix)

    schema = {
        "firm": model_name,
        "extractedFrom": data.get("path", ""),
        "extractedAt": datetime.now(timezone.utc).isoformat(),
        "dumpedAt": data.get("dumpedAt", ""),
        "doors": {
            "totalCount": door_info["totalCount"],
            "families": door_info["families"],
            "familyPrefix": door_info["familyPrefix"],
        },
        "parameters": {
            "instance": door_info["instanceParams"],
            "type": door_info["typeParams"],
            "instanceSamples": door_info["instanceParamSamples"],
            "typeSamples": door_info["typeParamSamples"],
        },
        "schedules": {
            "doorSchedules": schedule_info["doorSchedules"],
            "keySchedules": schedule_info["keySchedules"],
        },
        "categories": categories,
        "wireProfileDraft": wire_draft,
    }

    return schema


def print_summary(results):
    """Print a summary table of all extracted schemas."""
    print()
    print("=" * 90)
    print(f"{'Model':<40} {'Doors':>6} {'Fams':>5} {'DoorSch':>8} {'KeySch':>7} {'DetRefs':>8}")
    print("-" * 90)

    for model_name, schema in sorted(results.items()):
        doors = schema["doors"]["totalCount"]
        fams = len(schema["doors"]["families"])
        door_sch = len(schema["schedules"]["doorSchedules"])
        key_sch = len(schema["schedules"]["keySchedules"])
        has_refs = any(
            ds.get("hasDetailRefs")
            for ds in schema["schedules"]["doorSchedules"]
        )
        ref_str = "yes" if has_refs else "no"

        print(f"{model_name:<40} {doors:>6} {fams:>5} {door_sch:>8} {key_sch:>7} {ref_str:>8}")

    print("-" * 90)
    print(f"{'Total models: ' + str(len(results)):<40}")
    print()

    # Wire profile readiness
    print("Wire profile readiness:")
    for model_name, schema in sorted(results.items()):
        draft = schema["wireProfileDraft"]
        conf = draft["confidence"]
        head = draft["headParam"] or "(none)"
        jamb = draft["jambParam"] or "(none)"
        prefix = draft["sheetPrefix"] or "(none)"
        print(f"  {model_name}: {conf} -- head={head}, jamb={jamb}, prefix={prefix}")
    print()


def main():
    model_filter = None
    if len(sys.argv) > 1:
        model_filter = sys.argv[1]

    if not DUMPS_DIR.exists():
        print(f"Dumps directory not found: {DUMPS_DIR}")
        sys.exit(1)

    SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)

    dumps = find_latest_dumps(model_filter)
    if not dumps:
        if model_filter:
            print(f"No dumps found for model: {model_filter}")
        else:
            print(f"No dump files found in: {DUMPS_DIR}")
        sys.exit(1)

    print(f"Found {len(dumps)} model(s) to process")
    results = {}

    for model_name, dump_path in sorted(dumps.items()):
        print(f"  Processing: {model_name} ({dump_path.name})")
        schema = extract_schema(model_name, dump_path)
        results[model_name] = schema

        out_path = SCHEMAS_DIR / f"{model_name}.schema.json"
        with open(out_path, "w") as f:
            json.dump(schema, f, indent=2)
        print(f"    -> {out_path}")

    print_summary(results)


if __name__ == "__main__":
    main()
