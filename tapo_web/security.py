"""Refuse camera addresses that would make the server reach its own network (SSRF).

The user types the camera's address and the server connects to it. In multi-account mode that must
only ever be a public address: the name is resolved, every answer is checked, and the checked IP is
what the caller connects to, so the name cannot change its mind between the check and the connection.
"""
import ipaddress
import re
import socket

HOSTNAME = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


class UnsafeHost(ValueError):
    pass


def _literal(host):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_public(ip):
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # is_global is False for private, loopback, link-local, shared (100.64/10), multicast and reserved ranges
    return ip.is_global and not ip.is_multicast


def assert_public_host(host, allow_private=False, resolver=socket.getaddrinfo):
    """Return the IPs `host` resolves to; raise UnsafeHost unless all of them are public."""
    if not isinstance(host, str) or not (_literal(host) or HOSTNAME.match(host)):
        raise UnsafeHost("not a valid host name or IP address")
    try:
        answers = resolver(host, None)
    except OSError as err:
        raise UnsafeHost(f"cannot resolve {host}") from err
    ips = []
    for answer in answers:
        text = answer[4][0].split("%", 1)[0]  # drop an IPv6 scope id
        ip = ipaddress.ip_address(text)
        if not allow_private and not _is_public(ip):
            raise UnsafeHost(f"{host} resolves to a non-public address, which is not allowed")
        if text not in ips:
            ips.append(text)
    if not ips:
        raise UnsafeHost(f"{host} has no address")
    return ips


def checked_host(host, allow_private=False, resolver=socket.getaddrinfo):
    """One validated IP to connect to. Call it again before every (re)connection."""
    return assert_public_host(host, allow_private, resolver)[0]
