"""Unit tests for the loudness-envelope averaging used in bleed rejection (capture._mean_db)."""

from __future__ import annotations

from transcribbler.capture import _mean_db


def test_empty_env_is_silence():
    assert _mean_db([], 0.0, 1.0) == -120.0


def test_power_average_of_equal_frames():
    assert abs(_mean_db([-20.0, -20.0], 0.0, 0.1) - (-20.0)) < 1e-6


def test_windows_select_by_time():
    # frame_s default 0.05: frame 0 = [0,0.05), frame 2 = [0.10,0.15) ...
    env = [-10.0, -60.0, -60.0, -60.0]  # loud only in the first 50 ms
    assert -12.0 < _mean_db(env, 0.0, 0.04) < -9.0  # first frame dominates
    assert _mean_db(env, 0.10, 0.19) < -55.0  # later frames are quiet


def test_power_average_favors_the_loud_frame():
    # -10 dB frame carries ~1000x the power of a -40 dB frame, so the mean sits near -13
    m = _mean_db([-10.0, -40.0], 0.0, 0.1)
    assert -14.0 < m < -12.0
