from __future__ import annotations

from mapi_platform import identity


def test_distribution_defaults_follow_platform(monkeypatch) -> None:
    monkeypatch.delenv("MAPI_DISTRIBUTION_NAME", raising=False)
    monkeypatch.setattr(identity, "current_platform", lambda: "windows")
    assert identity.default_distribution_name() == "Aurora"
    assert identity.distribution_name() == "Aurora"
    monkeypatch.setattr(identity, "current_platform", lambda: "linux")
    assert identity.default_distribution_name() == "Polaris"
    assert identity.distribution_name() == "Polaris"


def test_distribution_name_can_be_explicitly_overridden(monkeypatch) -> None:
    monkeypatch.setenv("MAPI_DISTRIBUTION_NAME", "MAPI Lab")
    monkeypatch.setattr(identity, "current_platform", lambda: "windows")
    assert identity.distribution_name() == "MAPI Lab"
    assert identity.distribution_slug() == "mapi-lab"
