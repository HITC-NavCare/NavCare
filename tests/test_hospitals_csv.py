"""Checks on data/hospitals.csv, so an edit that breaks the list fails CI."""

import csv
from pathlib import Path

import pytest

from app.recommender_v1.rules_based_recommender import SPECIALISED_ER_IDS

HOSPITALS_CSV = Path(__file__).resolve().parents[1] / "data" / "hospitals.csv"


@pytest.fixture(scope="module")
def rows():
    with HOSPITALS_CSV.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def test_columns(rows):
    assert list(rows[0]) == ["id", "name", "address", "lat", "lon", "region", "pediatric"]


def test_has_all_120_facilities(rows):
    assert len(rows) == 120


def test_ids_are_unique_8_digit_codes(rows):
    ids = [row["id"] for row in rows]
    assert all(len(hospital_id) == 8 and hospital_id.isdigit() for hospital_id in ids)
    assert len(set(ids)) == len(ids)


def test_required_fields_are_filled(rows):
    for row in rows:
        assert row["name"].strip(), row
        assert row["address"].strip(), row
        assert row["region"].strip(), row


def test_coordinates_are_in_quebec(rows):
    for row in rows:
        assert 44.9 <= float(row["lat"]) <= 63.0, row
        assert -80.0 <= float(row["lon"]) <= -57.0, row


def test_pediatric_is_blank_until_an_official_source_is_found(rows):
    # See README: blank means "unknown", not "no".
    assert {row["pediatric"] for row in rows} == {""}


def test_french_accents_are_intact(rows):
    names = " ".join(row["name"] for row in rows)
    assert "HÔPITAL" in names
    assert "Ã" not in names  # sign of UTF-8 read as Windows-1252


def test_every_specialised_id_is_in_the_list(rows):
    ids = {row["id"] for row in rows}
    assert SPECIALISED_ER_IDS <= ids
