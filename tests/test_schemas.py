from mapi_core.schemas import normalize_area_code


def test_normalize_area_code_accepts_sandman() -> None:
    assert normalize_area_code("sandman") == "sandman"
    assert normalize_area_code("Sandman") == "sandman"
