"""Address matching for tailnet-only listeners and ADB transports."""
from ipaddress import IPv4Address, IPv4Network

# RFC 6598 shared IPv4 space used by Tailscale. A range, not a deployment endpoint.
TAILNET_IPV4 = IPv4Network((int.from_bytes(bytes((100, 64, 0, 0)), "big"), 10))


def is_tailnet_ipv4(host: str) -> bool:
    try:
        return IPv4Address(host) in TAILNET_IPV4
    except ValueError:
        return False


def is_tailnet_transport(serial: str) -> bool:
    host, sep, port = serial.partition(":")
    return bool(sep and port.isdecimal() and is_tailnet_ipv4(host))
