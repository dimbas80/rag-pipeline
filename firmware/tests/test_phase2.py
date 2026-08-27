from pathlib import Path

from firmware.src.registration import make_slug


def test_make_slug_transliterates_domain_and_collision():
    assert make_slug("ГОСТ 839—80", "ГОСТ", "Электроснабжение", ()) == "GOST_839_elektrosnabzhenie"
    assert make_slug("ГОСТ 839—80", "ГОСТ", "Электроснабжение", ("GOST_839_elektrosnabzhenie",)) == "GOST_839_elektrosnabzhenie_2"


def test_extract_first_page_returns_none_for_invalid_doc(tmp_path: Path):
    from firmware.src.registration import extract_first_page
    assert extract_first_page(tmp_path / "missing.pdf") is None
