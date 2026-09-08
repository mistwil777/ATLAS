"""Smoke test for fake-agent — used as fixture by agent-tester."""


def test_fake_agent_tools_importable():
    from fake_agent.tools import get_manifest, check_dependencies  # noqa: F401


def test_fake_agent_manifest_not_empty():
    from fake_agent.tools import get_manifest
    manifest = get_manifest()
    assert len(manifest) >= 1


def test_fake_agent_check_dependencies_returns_dict():
    from fake_agent.tools import check_dependencies
    result = check_dependencies()
    assert isinstance(result, dict)
    assert "missing" in result
