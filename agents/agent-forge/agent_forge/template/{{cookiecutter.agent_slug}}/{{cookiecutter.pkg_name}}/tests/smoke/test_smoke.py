"""Smoke tests for {{cookiecutter.agent_slug}}.

Add capability-specific smoke tests here.
Each test should verify that a core dependency works end-to-end.
"""

from __future__ import annotations


def test_import() -> None:
    """Verify the package imports cleanly."""
    import {{cookiecutter.pkg_name}}
    assert {{cookiecutter.pkg_name}}.__version__


def test_check_dependencies_runs() -> None:
    """Verify check_dependencies() runs without raising."""
    from {{cookiecutter.pkg_name}}.tools import check_dependencies
    result = check_dependencies()
    assert isinstance(result, dict)
    assert "missing" in result
