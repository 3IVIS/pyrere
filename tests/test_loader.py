"""
Tests for pyrere/ingestion/loader.py — load_python_files, _should_skip.
"""

import os
import pytest

from pyrere.ingestion.loader import _should_skip, load_python_files


# ─────────────────────────────────────────────────────────────────────────────
# _should_skip
# ─────────────────────────────────────────────────────────────────────────────


class TestShouldSkip:
    @pytest.mark.parametrize(
        "dirname",
        [
            "__pycache__",
            ".git",
            ".hg",
            ".tox",
            ".venv",
            "venv",
            "build",
            "dist",
            "node_modules",
            ".mypy_cache",
            ".pytest_cache",
        ],
    )
    def test_skips_known_non_source_dirs(self, dirname):
        assert _should_skip(dirname) is True

    def test_skips_egg_info_suffix(self):
        assert _should_skip("mypkg.egg-info") is True
        assert _should_skip("pyrere.egg-info") is True

    def test_does_not_skip_source_dirs(self):
        for name in ["src", "lib", "mypkg", "tests", "docs"]:
            assert _should_skip(name) is False


# ─────────────────────────────────────────────────────────────────────────────
# load_python_files
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadPythonFiles:
    def test_finds_py_files(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1")
        (tmp_path / "b.py").write_text("y = 2")
        found = set(load_python_files(str(tmp_path)))
        assert str(tmp_path / "a.py") in found
        assert str(tmp_path / "b.py") in found

    def test_ignores_non_py_files(self, tmp_path):
        (tmp_path / "readme.md").write_text("# hi")
        (tmp_path / "data.json").write_text("{}")
        (tmp_path / "script.py").write_text("pass")
        found = list(load_python_files(str(tmp_path)))
        assert all(f.endswith(".py") for f in found)
        assert len(found) == 1

    def test_recurses_into_subdirectories(self, simple_repo):
        found = list(load_python_files(str(simple_repo)))
        names = {os.path.basename(f) for f in found}
        assert "__init__.py" in names
        assert "utils.py" in names
        assert "helper.py" in names

    def test_skips_venv_directory(self, tmp_path):
        venv = tmp_path / ".venv"
        venv.mkdir()
        (venv / "site_pkg.py").write_text("pass")
        (tmp_path / "real.py").write_text("pass")
        found = list(load_python_files(str(tmp_path)))
        assert len(found) == 1
        assert found[0].endswith("real.py")

    def test_skips_pycache(self, tmp_path):
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "module.cpython-312.pyc").write_text("")
        (cache / "cached.py").write_text("pass")  # shouldn't happen, but test robustness
        (tmp_path / "real.py").write_text("pass")
        found = list(load_python_files(str(tmp_path)))
        paths = [os.path.normpath(f) for f in found]
        assert not any("__pycache__" in p for p in paths)

    def test_skips_egg_info(self, tmp_path):
        egg = tmp_path / "mypkg.egg-info"
        egg.mkdir()
        (egg / "SOURCES.py").write_text("pass")
        (tmp_path / "real.py").write_text("pass")
        found = list(load_python_files(str(tmp_path)))
        assert len(found) == 1

    def test_empty_directory_returns_nothing(self, tmp_path):
        assert list(load_python_files(str(tmp_path))) == []

    def test_returns_absolute_paths(self, tmp_path):
        (tmp_path / "mod.py").write_text("pass")
        found = list(load_python_files(str(tmp_path)))
        assert all(os.path.isabs(f) for f in found)
