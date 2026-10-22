"""Download the MSSS hourly emergency-room CSV and save a local copy under data/snapshots/.

Run from any folder:  python app/integrations/ingestion.py
"""

import csv
import io
import itertools
import sys
import urllib.request
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


def main():
    try:
        text = download_csv_text(ER_CSV_URL)
    except OSError as error:  # covers HTTP errors, DNS failures and timeouts
        print(f"Download failed: {error}", file=sys.stderr)
        return 1

    header_line = text.partition("\n")[0]
    if "No_permis_installation" not in header_line:
        print("Downloaded file is not the expected ER CSV; nothing saved.", file=sys.stderr)
        return 1

    save_snapshot(text, SNAPSHOT_PATH)
    print(f"Saved {SNAPSHOT_PATH}\n")
    print_preview(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
