import random

import pytest

from tapo_web.cameras import NoFreePorts, assign_ports
from tapo_web.guide import build_guide
from tests.conftest import make_settings


def test_assign_ports_unique_in_range_not_adjacent():
    rng = random.Random(7)
    for _ in range(200):
        taken = {40001, 40002}
        rtsp, onvif = assign_ports(taken, 40000, 40100, rng)
        assert rtsp != onvif and abs(rtsp - onvif) > 1
        assert 40000 <= rtsp <= 40100 and 40000 <= onvif <= 40100
        assert rtsp not in taken and onvif not in taken


def test_assign_ports_is_not_sequential():
    rng = random.Random()
    firsts = {assign_ports(set(), 30000, 39999, rng)[0] for _ in range(20)}
    assert len(firsts) > 10


def test_assign_ports_exhausted_raises():
    with pytest.raises(NoFreePorts):
        assign_ports(set(range(40000, 40010)), 40000, 40009, random.Random(1))
    with pytest.raises(NoFreePorts):
        assign_ports(set(), 40000, 40001, random.Random(1))  # two ports but they would be adjacent


def test_ai_prompt_has_server_ip_and_ports_but_no_secrets(tmp_path):
    settings = make_settings(tmp_path, server_public_ip="203.0.113.7")
    guide = build_guide(settings, "my-cam.example.com", 38554, 38020)
    prompt = guide.ai_prompt
    for needed in ("203.0.113.7", "38554", "38020", "554", "2020", "my-cam.example.com"):
        assert needed in prompt
    assert "[your router model]" in prompt
    for secret in ("camuser", "S3cretPW", "password:"):
        assert secret not in prompt


def test_manual_steps_say_allow_only_server_ip(tmp_path):
    settings = make_settings(tmp_path, server_public_ip="203.0.113.7")
    steps = " ".join(build_guide(settings, "my-cam.example.com", 38554, 38020).manual_steps)
    assert "203.0.113.7" in steps and "only" in steps.lower()
    assert "38554" in steps and "38020" in steps


def test_guide_without_server_ip_points_to_the_administrator(tmp_path):
    settings = make_settings(tmp_path, server_public_ip="")
    guide = build_guide(settings, "my-cam.example.com", 38554, 38020)
    assert "ask your administrator" in guide.ai_prompt.lower()
    assert "ask your administrator" in " ".join(guide.manual_steps).lower()
