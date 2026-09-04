from pathlib import Path

from firmware.src.registration import make_slug


def test_make_slug_from_document_id_and_collision():
    assert make_slug("ГОСТ 839—80") == "GOST_839_80"
    assert make_slug("ГОСТ 839—80", ("GOST_839_80",)) == "GOST_839_80_2"


def test_extract_first_page_returns_none_for_invalid_doc(tmp_path: Path):
    from firmware.src.registration import extract_first_page
    assert extract_first_page(tmp_path / "missing.pdf") is None
