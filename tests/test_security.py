import socket

import pytest

from tapo_web.security import UnsafeHost, assert_public_host, checked_host


def fake_resolver(mapping):
    """A resolver that answers from a dict (name -> list of IPs); IP literals resolve to themselves."""
    def resolve(host, port=None, *args, **kwargs):
        ips = mapping.get(host, [host])
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]
    return resolve


@pytest.mark.parametrize("host", ["192.168.1.5", "10.0.0.1", "172.16.5.4", "127.0.0.1", "::1", "169.254.169.254",
                                  "100.64.0.1", "0.0.0.0", "::ffff:127.0.0.1", "fd00::1", "224.0.0.1"])
def test_rejects_private_and_loopback(host):
    with pytest.raises(UnsafeHost):
        assert_public_host(host, resolver=fake_resolver({}))


def test_accepts_public():
    assert assert_public_host("93.184.216.34", resolver=fake_resolver({})) == ["93.184.216.34"]


def test_rejects_name_resolving_privately():
    resolver = fake_resolver({"cam.example.com": ["10.0.0.5"]})
    with pytest.raises(UnsafeHost):
        assert_public_host("cam.example.com", resolver=resolver)


def test_rejects_mixed_results():
    resolver = fake_resolver({"cam.example.com": ["93.184.216.34", "192.168.0.9"]})
    with pytest.raises(UnsafeHost):
        assert_public_host("cam.example.com", resolver=resolver)


def test_rebinding_second_lookup_is_checked():
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])

    def resolver(host, port=None, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers)[0], 0))]

    assert checked_host("cam.example.com", resolver=resolver) == "93.184.216.34"
    with pytest.raises(UnsafeHost):
        checked_host("cam.example.com", resolver=resolver)


def test_allow_private_flag():
    assert assert_public_host("192.168.1.5", allow_private=True, resolver=fake_resolver({})) == ["192.168.1.5"]
    assert checked_host("10.0.0.1", allow_private=True, resolver=fake_resolver({})) == "10.0.0.1"


@pytest.mark.parametrize("host", ["a b", "http://x", "", "cam:554", "cam/evil", "-bad.example.com", "x" * 300])
def test_rejects_non_hostname_inputs(host):
    with pytest.raises(UnsafeHost):
        assert_public_host(host, allow_private=True, resolver=fake_resolver({}))


def test_unresolvable_name_is_unsafe():
    def resolver(*args, **kwargs):
        raise socket.gaierror("no such host")

    with pytest.raises(UnsafeHost):
        assert_public_host("nope.example.com", resolver=resolver)
