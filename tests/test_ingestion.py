import pytest
from google.api_core.exceptions import AlreadyExists

from app.integrations import ingestion
from app.services import hospital_status_getter

# Same shape as the MSSS file: a stray tab/space after a header name, the typographic
# apostrophe in the extraction-time column, and a regional total row without a permit number.
HEADER = (
    "RSS,Region,Nom_installation,No_permis_installation\t ,Nombre_de_civieres_occupees,"
    "Heure_de_l'extraction_(image),Mise_a_jour\n"
)
EXTRACTED = "21:00:00"
PUBLISHED = "2026-11-25T21:45"
ROWS = [
    f"06,Montréal,HÔPITAL {i},5123{i:04d},{i},{EXTRACTED},{PUBLISHED}\n" for i in range(8)
]
TOTAL_ROW = f"06,Montréal,Total régional,,28,{EXTRACTED},{PUBLISHED}\n"
CSV_TEXT = HEADER + "".join(ROWS) + TOTAL_ROW
PQ_DATA = {
    "updated_at": "2026-11-25T22:46:00.000Z",
    "stale": False,
    "by_id": {"51218097": {"occupancy_rate": 0.73}, 51248219: {"occupancy_rate": 1.42}},
}


class FakeUrlResponse:
    def __init__(self, raw_bytes):
        self._raw_bytes = raw_bytes

    def read(self):
        return self._raw_bytes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def patch_urlopen(monkeypatch, raw_bytes, calls=None):
    def urlopen(url, timeout):
        if calls is not None:
            calls.append({"url": url, "timeout": timeout})
        return FakeUrlResponse(raw_bytes)

    monkeypatch.setattr(ingestion.urllib.request, "urlopen", urlopen)


class FakeFirestore:
    """Offline stand-in for the Firestore client: create() fails if the document exists."""

    def __init__(self, fail=False):
        self.docs = {}
        self.fail = fail

    def collection(self, name):
        self._collection = name
        return self

    def document(self, doc_id):
        self._key = (self._collection, doc_id)
        return self

    def create(self, data):
        if self.fail:
            raise RuntimeError("firestore down")
        if self._key in self.docs:
            raise AlreadyExists("document exists")
        self.docs[self._key] = data


# --- download_csv_text ---------------------------------------------------------------

def test_decodes_windows_1252(monkeypatch):
    # How MSSS publishes the file.
    patch_urlopen(monkeypatch, "Hôpital Sacré-Cœur".encode("cp1252"))

    assert ingestion.download_csv_text("https://example.test") == "Hôpital Sacré-Cœur"


def test_decodes_utf8_and_strips_bom(monkeypatch):
    patch_urlopen(monkeypatch, "\ufeffHôpital".encode("utf-8"))

    assert ingestion.download_csv_text("https://example.test") == "Hôpital"


def test_download_uses_timeout(monkeypatch):
    calls = []
    patch_urlopen(monkeypatch, b"x", calls)

    ingestion.download_csv_text("https://example.test/file.csv")

    assert calls == [{
        "url": "https://example.test/file.csv",
        "timeout": ingestion.REQUEST_TIMEOUT_SECONDS,
    }]


# --- save_snapshot and print_preview -------------------------------------------------

def test_save_snapshot_creates_folder_and_writes_utf8(tmp_path):
    path = tmp_path / "new" / "folder" / "snapshot.csv"

    ingestion.save_snapshot("a,b\r\nHôpital,1\r\n", path)

    # newline="" keeps the source's line endings unchanged.
    assert path.read_bytes() == "a,b\r\nHôpital,1\r\n".encode("utf-8")


def test_print_preview_strips_header_whitespace_and_limits_rows(capsys):
    ingestion.print_preview(CSV_TEXT)

    out = capsys.readouterr().out
    assert "  No_permis_installation\n" in out  # stray tab/space removed
    assert "HÔPITAL 4" in out
    assert "HÔPITAL 5" not in out  # only PREVIEW_ROW_COUNT (5) rows
    assert ingestion.PREVIEW_ROW_COUNT == 5


# --- clean_value and parse_snapshots -------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    (" 12 ", 12),
    ("1,5", 1.5),
    ("2.25", 2.25),
    ("", None),
    ("  pas d'information disponible ", None),
    ("Pas d\u2019information disponible", None),  # typographic apostrophe
    ("4:30", "4:30"),  # hours:minutes stays text
])
def test_clean_value(text, expected):
    assert ingestion.clean_value(text) == expected


def test_parse_groups_by_publication_and_skips_total_rows():
    snapshots = ingestion.parse_snapshots(CSV_TEXT)

    assert list(snapshots) == [PUBLISHED]
    hospitals = snapshots[PUBLISHED]
    assert len(hospitals) == 8  # the regional total row has no permit number
    assert all(isinstance(hospital_id, str) for hospital_id in hospitals)
    assert hospitals["51230003"]["Nombre_de_civieres_occupees"] == 3
    assert hospitals["51230003"]["Heure_de_l'extraction_(image)"] == EXTRACTED


def test_parse_keeps_each_publication_separate():
    later = ROWS[0].replace(PUBLISHED, "2026-11-25T22:45")
    snapshots = ingestion.parse_snapshots(HEADER + ROWS[0] + later)

    assert sorted(snapshots) == [PUBLISHED, "2026-11-25T22:45"]


def test_snapshot_id_has_no_slash():
    assert ingestion.snapshot_id("25/11/2026 19:45") == "25-11-2026 19:45"


# --- write_snapshots -----------------------------------------------------------------

def test_write_creates_one_document_per_publication():
    fake = FakeFirestore()

    written = ingestion.write_snapshots(fake, ingestion.parse_snapshots(CSV_TEXT))

    assert written == 1
    document = fake.docs[("er_snapshots", PUBLISHED)]
    assert document["published_at"] == PUBLISHED
    assert len(document["hospitals"]) == 8
    assert document["ingested_at"] is not None


def test_write_skips_publications_already_stored():
    fake = FakeFirestore()
    snapshots = ingestion.parse_snapshots(CSV_TEXT)

    assert ingestion.write_snapshots(fake, snapshots) == 1
    assert ingestion.write_snapshots(fake, snapshots) == 0  # next hourly run, same publication
    assert len(fake.docs) == 1


# --- main ----------------------------------------------------------------------------

@pytest.fixture
def snapshot_path(tmp_path, monkeypatch):
    path = tmp_path / "snapshots" / "er.csv"
    monkeypatch.setattr(ingestion, "SNAPSHOT_PATH", path)
    return path


def test_main_local_saves_snapshot(monkeypatch, snapshot_path, capsys):
    monkeypatch.setattr(ingestion, "download_csv_text", lambda url: CSV_TEXT)

    assert ingestion.main(["--local"]) == 0
    assert snapshot_path.read_text(encoding="utf-8") == CSV_TEXT
    assert f"Saved {snapshot_path}" in capsys.readouterr().out


def test_main_writes_to_firestore_and_skips_existing(monkeypatch, snapshot_path, capsys):
    monkeypatch.setattr(ingestion, "download_csv_text", lambda url: CSV_TEXT)
    fake = FakeFirestore()

    assert ingestion.main(["--msss"], client=fake) == 0
    assert ingestion.main(["--msss"], client=fake) == 0

    assert list(fake.docs) == [("er_snapshots", PUBLISHED)]
    assert not snapshot_path.exists()  # no local file without --local
    out = capsys.readouterr().out
    assert "1 new snapshot(s) from msss; 0 already stored" in out
    assert "0 new snapshot(s) from msss; 1 already stored" in out


def test_main_firestore_failure(monkeypatch, capsys):
    monkeypatch.setattr(ingestion, "download_csv_text", lambda url: CSV_TEXT)

    assert ingestion.main(["--msss"], client=FakeFirestore(fail=True)) == 1
    assert "Firestore write failed" in capsys.readouterr().err


def test_main_no_hospital_rows(monkeypatch, capsys):
    monkeypatch.setattr(ingestion, "download_csv_text", lambda url: HEADER + TOTAL_ROW)

    assert ingestion.main(["--msss"], client=FakeFirestore()) == 1
    assert "nothing stored" in capsys.readouterr().err


def test_main_download_failure(monkeypatch, snapshot_path, capsys):
    def failing(url):
        raise OSError("timed out")

    monkeypatch.setattr(ingestion, "download_csv_text", failing)

    assert ingestion.main(["--msss"]) == 1
    assert not snapshot_path.exists()
    assert "Download failed: timed out" in capsys.readouterr().err


def test_main_rejects_unexpected_file(monkeypatch, snapshot_path, capsys):
    # For example an HTML error page served with status 200.
    monkeypatch.setattr(ingestion, "download_csv_text", lambda url: "<html>Maintenance</html>")

    assert ingestion.main(["--msss"]) == 1
    assert not snapshot_path.exists()
    assert "not the expected ER CSV" in capsys.readouterr().err

def test_main_default_uses_patient_quebec(monkeypatch, capsys):
    monkeypatch.setattr(hospital_status_getter, "get_er_data", lambda: PQ_DATA)
    fake = FakeFirestore()

    assert ingestion.main([], client=fake) == 0
    assert ingestion.main([], client=fake) == 0  # same publication an hour later: skipped

    document = fake.docs[("er_snapshots", "2026-11-25T22:46:00.000Z")]
    assert document["source"] == "patient.quebec"
    assert set(document["hospitals"]) == {"51218097", "51248219"}  # ids become strings
    assert "0 new snapshot(s) from patient.quebec; 1 already stored" in capsys.readouterr().out


def test_main_patient_quebec_failure(monkeypatch, capsys):
    def failing():
        raise RuntimeError("ER status request failed: timed out")

    monkeypatch.setattr(hospital_status_getter, "get_er_data", failing)

    assert ingestion.main([], client=FakeFirestore()) == 1
    assert "patient.quebec request failed" in capsys.readouterr().err