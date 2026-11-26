"""Download the MSSS hourly emergency-room CSV and store each publication in Firestore.

Run from any folder:
  python app/integrations/ingestion.py           write new publications to er_snapshots
  python app/integrations/ingestion.py --local   only save a local copy and print a preview
"""

import csv
import io
import itertools
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Resource "Fichier horaire de la situation à l'urgence avec le nombre de personnes présentes" from
# https://www.donneesquebec.ca/recherche/dataset/fichier-horaire-des-donnees-de-la-situation-a-l-urgence
ER_CSV_URL = (
    "https://www.msss.gouv.qc.ca/professionnels/statistiques/documents/urgences/"
    "Releve_horaire_urgences_7jours_nbpers.csv"
)
REQUEST_TIMEOUT_SECONDS = 30
REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "data" / "snapshots" / "Releve_horaire_urgences_7jours_nbpers.csv"
PREVIEW_ROW_COUNT = 5

COLLECTION = "er_snapshots"
ID_COLUMN = "No_permis_installation"  # empty on region and whole-Québec total rows
PUBLICATION_TIME_COLUMN = "Mise_a_jour"  # MSSS: date and time the data was published
# Compared after lowercasing and replacing the typographic apostrophe with a plain one.
MISSING_VALUES = {"", "pas d'information disponible"}


def download_csv_text(url):
    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        raw_bytes = response.read()
    # MSSS publishes this file in Windows-1252, not UTF-8. Accept UTF-8 too in case that changes.
    try:
        return raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw_bytes.decode("cp1252")


def save_snapshot(text, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def print_preview(text):
    rows = csv.reader(io.StringIO(text, newline=""))
    # Some header names in the source contain stray tabs and spaces.
    column_names = [name.strip() for name in next(rows)]
    print("Columns:")
    for name in column_names:
        print(f"  {name}")
    print(f"\nFirst {PREVIEW_ROW_COUNT} rows:")
    for row in itertools.islice(rows, PREVIEW_ROW_COUNT):
        print("  " + " | ".join(value.strip() for value in row))


def clean_value(text):
    """MSSS values are text. Missing ones become None, numbers become int or float."""
    value = (text or "").strip()
    if value.lower().replace("\u2019", "'") in MISSING_VALUES:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value.replace(",", "."))  # French decimal comma
    except ValueError:
        return value  # names, times like "4:30", anything else stays text


def parse_snapshots(text):
    """Return {publication time: {hospital id: {column: value}}}.

    Rows without a hospital id (region and whole-Québec totals) or without a publication
    time are skipped. Hospital ids stay strings.
    """
    rows = csv.reader(io.StringIO(text, newline=""))
    header = [name.strip() for name in next(rows)]  # some names have stray tabs and spaces
    snapshots = {}
    for row in rows:
        record = dict(zip(header, row))
        published = (record.get(PUBLICATION_TIME_COLUMN) or "").strip()
        hospital_id = (record.get(ID_COLUMN) or "").strip()
        if not published or not hospital_id:
            continue
        values = {
            name: clean_value(value)
            for name, value in record.items()
            if name not in (ID_COLUMN, PUBLICATION_TIME_COLUMN)
        }
        snapshots.setdefault(published, {})[hospital_id] = values
    return snapshots


def snapshot_id(published):
    """Firestore document ids cannot contain "/"."""
    return published.replace("/", "-")

def fetch_patient_quebec_snapshots():
    """The current MSSS publication as republished by patient.quebec (reachable from Google Cloud,
    unlike the MSSS site). Same shape as parse_snapshots: {publication time: {hospital id: fields}}.
    Raises RuntimeError if patient.quebec fails.
    """
    from app.services.hospital_status_getter import get_er_data

    data = get_er_data()
    published = str(data.get("updated_at") or "").strip()
    hospitals = {str(hospital_id): fields for hospital_id, fields in data["by_id"].items()}
    if not published or not hospitals:
        return {}
    return {published: hospitals}

def write_snapshots(client, snapshots, source="msss"):
    """Create one document per publication, skipping ones already 
    stored. Returns the count written."""
    from google.api_core.exceptions import AlreadyExists

    written = 0
    for published, hospitals in sorted(snapshots.items()):
        document = client.collection(COLLECTION).document(snapshot_id(published))
        try:
            document.create({
                "published_at": published,
                "ingested_at": datetime.now(timezone.utc),
                "source": source,
                "hospitals": hospitals,
            })
            written += 1
        except AlreadyExists:
            pass  # this publication is already stored
    return written


def main(argv=None, client=None):
    """argv: command-line arguments (tests pass a list). 
    client: Firestore client (tests pass a fake).

    Default: read the current publication from patient.quebec (works on Google Cloud).
    --msss:  read the MSSS CSV directly (MSSS blocks Google Cloud, so only from elsewhere).
    --local: save the MSSS CSV locally and print a preview; nothing is stored.
    """
    argv = sys.argv[1:] if argv is None else argv

    if "--local" in argv or "--msss" in argv:
        try:
            text = download_csv_text(ER_CSV_URL)
        except OSError as error:  # covers HTTP errors, DNS failures and timeouts
            print(f"Download failed: {error}", file=sys.stderr)
            return 1

        if ID_COLUMN not in text.partition("\n")[0]:
            print("Downloaded file is not the expected ER CSV; nothing saved.", file=sys.stderr)
            return 1

        if "--local" in argv:
            save_snapshot(text, SNAPSHOT_PATH)
            print(f"Saved {SNAPSHOT_PATH}\n")
            print_preview(text)
            return 0

        snapshots, source = parse_snapshots(text), "msss"
    else:
        try:
            snapshots, source = fetch_patient_quebec_snapshots(), "patient.quebec"
        except RuntimeError as error:
            print(f"patient.quebec request failed: {error}", file=sys.stderr)
            return 1

    if not snapshots:
        print("No hospital rows with a publication time; nothing stored.", file=sys.stderr)
        return 1

    if client is None:
        from google.cloud import firestore

        client = firestore.Client()

    try:
        written = write_snapshots(client, snapshots, source)
    except Exception as error:  # Firestore unreachable, permission denied, etc.
        print(f"Firestore write failed: {error}", file=sys.stderr)
        return 1

    print(f"{written} new snapshot(s) from {source}; {len(snapshots) - written} already stored")
    return 0


if __name__ == "__main__":
    sys.exit(main())