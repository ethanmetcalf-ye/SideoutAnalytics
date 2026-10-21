def test_package_imports_and_exposes_entry_point() -> None:
    from sideout_analytics import main

    assert callable(main)
