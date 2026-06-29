"""Tests for profile discovery/resolution (the "just run it" CLI ergonomics)."""

from __future__ import annotations

import pytest

from transcribbler import profiles


@pytest.fixture
def profile_dirs(tmp_path, monkeypatch):
    """Isolated config + bundled dirs so tests don't depend on the real host."""
    config = tmp_path / "config"
    bundled = tmp_path / "bundled"
    config_profiles = config / "transcribbler" / "profiles"
    config_profiles.mkdir(parents=True)
    bundled.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(profiles, "BUNDLED_DIR", bundled)
    monkeypatch.delenv("TRANSCRIBBLER_PROFILE", raising=False)
    return config_profiles, bundled


def _write(path, name, backend="vulkan"):
    path.write_text(f'name = "{name}"\n[asr]\nengine = "whisper.cpp"\nbackend = "{backend}"\n')


def test_resolve_explicit_path(tmp_path, profile_dirs):
    p = tmp_path / "custom.toml"
    _write(p, "custom")
    assert profiles.resolve(str(p)) == p


def test_resolve_missing_path_raises(tmp_path, profile_dirs):
    with pytest.raises(profiles.ProfileError, match="not found"):
        profiles.resolve(str(tmp_path / "ghost.toml"))


def test_resolve_bare_name_from_bundled(profile_dirs):
    _, bundled = profile_dirs
    _write(bundled / "desktop-vulkan.toml", "desktop-vulkan")
    assert profiles.resolve("desktop-vulkan").stem == "desktop-vulkan"


def test_config_profiles_shadow_bundled(profile_dirs):
    config, bundled = profile_dirs
    _write(bundled / "desktop-vulkan.toml", "bundled")
    _write(config / "desktop-vulkan.toml", "local-override")
    assert profiles.load(profiles.resolve("desktop-vulkan")).name == "local-override"


def test_unknown_name_lists_available(profile_dirs):
    _, bundled = profile_dirs
    _write(bundled / "desktop-vulkan.toml", "desktop-vulkan")
    with pytest.raises(profiles.ProfileError, match="desktop-vulkan"):
        profiles.resolve("nope")


def test_env_var_used_when_no_arg(profile_dirs, monkeypatch):
    _, bundled = profile_dirs
    _write(bundled / "cube-cuda.toml", "cube-cuda")
    monkeypatch.setenv("TRANSCRIBBLER_PROFILE", "cube-cuda")
    assert profiles.resolve(None).stem == "cube-cuda"


def test_auto_select_matches_backend(profile_dirs, monkeypatch):
    _, bundled = profile_dirs
    _write(bundled / "a-cuda.toml", "a", backend="cuda")
    _write(bundled / "b-rocm.toml", "b", backend="rocm")

    from transcribbler import probe

    monkeypatch.setattr(
        probe, "detect", lambda: probe.Capabilities(cuda=None, rocm="gfx", vulkan=None)
    )
    assert profiles.resolve(None).stem == "b-rocm"


def test_auto_select_no_match_raises(profile_dirs, monkeypatch):
    _, bundled = profile_dirs
    _write(bundled / "a-cuda.toml", "a", backend="cuda")
    from transcribbler import probe

    monkeypatch.setattr(
        probe, "detect", lambda: probe.Capabilities(cuda=None, rocm=None, vulkan="dev")
    )
    with pytest.raises(profiles.ProfileError, match="no profile matches"):
        profiles.resolve(None)
