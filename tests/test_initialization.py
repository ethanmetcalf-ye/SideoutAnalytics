from sideout_analytics import main


def test_package_imports_and_exposes_entry_point() -> None:
    assert callable(main)
