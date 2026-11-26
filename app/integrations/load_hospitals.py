"""One-off: load data/hospitals.csv into the Firestore 'hospitals' collection.

Run from the repo root:  python app/integrations/load_hospitals.py
Safe to re-run: each hospital's document is overwritten, never duplicated.
"""

import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOSPITALS_CSV = REPO_ROOT / "data" / "hospitals.csv"
COLLECTION = "hospitals"
FLOAT_COLUMNS = {"lat", "lon"}


def read_hospitals(path=HOSPITALS_CSV):
    """Return {id: fields}. The id stays a string (8-digit MSSS code)."""
    hospitals = {}
    with Path(path).open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            fields = {name.strip(): (value or "").strip() for name, value in row.items()}
            for name in FLOAT_COLUMNS & fields.keys():
                fields[name] = float(fields[name]) if fields[name] else None
            hospitals[fields["id"]] = fields
    return hospitals


def load(client, hospitals):
    for hospital_id, fields in hospitals.items():
        client.collection(COLLECTION).document(hospital_id).set(fields)
    return len(hospitals)


def main():
    from google.cloud import firestore

    count = load(firestore.Client(), read_hospitals())
    print(f"Wrote {count} documents to {COLLECTION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())