import pytest

from app.integrations import ingestion

HEADER = "RSS,Region,Nom_installation,No_permis_installation\t ,Nombre_de_civieres_occupees\n"
ROWS = [f"06,Montréal,HÔPITAL {i},5123{i:04d},{i}\n" for i in range(8)]
CSV_TEXT = HEADER + "".join(ROWS)


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


# --- download_csv_text ---------------------------------------------------------------

def test_decodes_windows_1252(monkeypatch):
    # How MSSS publishes the file.
    patch_urlopen(monkeypatch, "Hôpital Sacré-Cœur".encode("cp1252"))

    assert ingestion.download_csv_text("https://example.test") == "Hôpital Sacré-Cœur"


def test_decodes_utf8_and_strips_bom(monkeypatch):
    patch_urlopen(monkeypatch, "﻿Hôpital".encode("utf-8"))

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


# --- main ----------------------------------------------------------------------------

@pytest.fixture
def snapshot_path(tmp_path, monkeypatch):
    path = tmp_path / "snapshots" / "er.csv"
    monkeypatch.setattr(ingestion, "SNAPSHOT_PATH", path)
    return path


def test_main_saves_snapshot(monkeypatch, snapshot_path, capsys):
    monkeypatch.setattr(ingestion, "download_csv_text", lambda url: CSV_TEXT)

    assert ingestion.main() == 0
    assert snapshot_path.read_text(encoding="utf-8") == CSV_TEXT
    assert f"Saved {snapshot_path}" in capsys.readouterr().out


def test_main_download_failure(monkeypatch, snapshot_path, capsys):
    def failing(url):
        raise OSError("timed out")

    monkeypatch.setattr(ingestion, "download_csv_text", failing)

    assert ingestion.main() == 1
    assert not snapshot_path.exists()
    assert "Download failed: timed out" in capsys.readouterr().err


def test_main_rejects_unexpected_file(monkeypatch, snapshot_path, capsys):
    # For example an HTML error page served with status 200.
    monkeypatch.setattr(ingestion, "download_csv_text", lambda url: "<html>Maintenance</html>")

    assert ingestion.main() == 1
    assert not snapshot_path.exists()
    assert "not the expected ER CSV" in capsys.readouterr().err
