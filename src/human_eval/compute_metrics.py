#!/usr/bin/env python3
"""
Computes precision and recall for the DCR-relation-extraction human eval.

Precision (per relation type, and overall):
    TP = rows in ANNOTATION_MANIFEST with verdict == 'correct'
    FP = rows in ANNOTATION_MANIFEST with verdict == 'incorrect'
    precision = TP / (TP + FP)
    ('unsure' rows are excluded from the denominator but reported separately.)

Recall (per relation type, and overall):
    TP = same as above (correct rows the model DID extract)
    FN = rows in MISSING_RELATIONS with missing_kind == 'relation'
         (real relations the model FAILED to extract)
    recall = TP / (TP + FN)

Event recall (informational only, not part of precision/recall of relations):
    counts rows in MISSING_RELATIONS with missing_kind == 'event'

Usage:
    python compute_metrics.py HumanEval.xlsx
    python compute_metrics.py HumanEval.xlsx --out metrics_summary.csv
"""

import argparse
import sys
from pathlib import Path

import pandas as pd


def load_sheets(xlsx_path: str):
    manifest = pd.read_excel(xlsx_path, sheet_name="ANNOTATION_MANIFEST")
    missing = pd.read_excel(xlsx_path, sheet_name="MISSING_RELATIONS")

    # Normalize column names (MISSING_RELATIONS sometimes has a header row
    # that got pushed down under generic "Column1..N" names with the real
    # header in row 2 — handle both cases).
    missing.columns = [str(c).strip() for c in missing.columns]
    if "missing_kind" not in missing.columns:
        # Try to find the real header among the first couple of rows
        for i in range(min(3, len(missing))):
            row_vals = [str(v).strip() for v in missing.iloc[i].tolist()]
            if "missing_kind (event|relation)" in row_vals or "missing_kind" in row_vals:
                missing.columns = row_vals
                missing = missing.iloc[i + 1:].reset_index(drop=True)
                break

    # Normalize the long-form column name to a short one
    rename_map = {}
    for c in missing.columns:
        cl = c.lower()
        if cl.startswith("missing_kind"):
            rename_map[c] = "missing_kind"
        elif cl.startswith("type"):
            rename_map[c] = "type"
    missing = missing.rename(columns=rename_map)

    manifest.columns = [str(c).strip() for c in manifest.columns]

    return manifest, missing


def clean_str_col(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.lower().replace({"nan": None, "none": None})


def compute_metrics(manifest: pd.DataFrame, missing: pd.DataFrame) -> pd.DataFrame:
    manifest = manifest.copy()
    manifest["verdict"] = clean_str_col(manifest["verdict"])
    manifest["type"] = clean_str_col(manifest["type"])

    missing = missing.copy()
    missing["missing_kind"] = clean_str_col(missing["missing_kind"])
    if "type" in missing.columns:
        missing["type"] = clean_str_col(missing["type"])
    else:
        missing["type"] = None

    missing_relations = missing[missing["missing_kind"] == "relation"]
    missing_events_count = int((missing["missing_kind"] == "event").sum())

    relation_types = ["condition", "exclusion", "inclusion", "response"]
    rows = []

    def row_for(label: str, sub_manifest: pd.DataFrame, sub_missing: pd.DataFrame):
        tp = int((sub_manifest["verdict"] == "correct").sum())
        fp = int((sub_manifest["verdict"] == "incorrect").sum())
        unsure = int((sub_manifest["verdict"] == "unsure").sum())
        fn = len(sub_missing)

        precision = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
        recall = tp / (tp + fn) if (tp + fn) > 0 else float("nan")
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0 and not (pd.isna(precision) or pd.isna(recall))
            else float("nan")
        )

        rows.append(
            {
                "type": label,
                "correct (TP)": tp,
                "incorrect (FP)": fp,
                "unsure (excluded)": unsure,
                "missing (FN)": fn,
                "precision": round(precision, 4) if not pd.isna(precision) else None,
                "recall": round(recall, 4) if not pd.isna(recall) else None,
                "f1": round(f1, 4) if not pd.isna(f1) else None,
            }
        )

    # Overall
    row_for("OVERALL", manifest, missing_relations)

    # Per relation type
    for t in relation_types:
        sub_manifest = manifest[manifest["type"] == t]
        sub_missing = missing_relations[missing_relations["type"] == t]
        row_for(t, sub_manifest, sub_missing)

    summary = pd.DataFrame(rows)

    print(summary.to_string(index=False))
    print()
    print(f"Missing EVENTS logged (informational, not counted in relation recall): {missing_events_count}")
    print(f"Total relations judged: {len(manifest)}")
    print(f"Total relations missed (FN): {len(missing_relations)}")

    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx_path", help="Path to the annotated HumanEval.xlsx")
    parser.add_argument("--out", default=None, help="Optional path to write the summary as CSV")
    args = parser.parse_args()

    if not Path(args.xlsx_path).exists():
        print(f"File not found: {args.xlsx_path}", file=sys.stderr)
        sys.exit(1)

    manifest, missing = load_sheets(args.xlsx_path)
    summary = compute_metrics(manifest, missing)

    if args.out:
        summary.to_csv(args.out, index=False)
        print(f"\nSaved summary to {args.out}")


if __name__ == "__main__":
    main()