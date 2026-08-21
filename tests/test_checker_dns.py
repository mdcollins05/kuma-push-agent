"""Tests for the DNS check path in checker — unit-level _check_dns covering
answer matching, expected-value comparison, and the dnspython error cases."""
import dns.resolver

from app.checker import _check_dns


# ── Helpers ──────────────────────────────────────────────────────────────────

class _Rec:
    """Minimal stand-in for a dnspython answer record."""
    def __init__(self, value: str):
        self._value = value

    def to_text(self) -> str:
        return self._value


def _stub_resolver(monkeypatch, *, answers=None, exc=None):
    """Patch dns.resolver.Resolver so resolve() returns `answers` or raises `exc`."""
    class StubResolver:
        def __init__(self, configure=True):
            self.nameservers = []
            self.timeout = None
            self.lifetime = None

        def resolve(self, query, record_type):
            if exc is not None:
                raise exc
            return answers

    monkeypatch.setattr(dns.resolver, "Resolver", StubResolver)


def _cfg(**kw) -> dict:
    base = {
        "type": "dns",
        "dns_query": "example.com",
        "dns_record_type": "A",
        "dns_resolver": None,
        "expected_value": None,
        "max_response_ms": None,
    }
    base.update(kw)
    return base


# ── _check_dns() unit ────────────────────────────────────────────────────────

def test_resolves_up(monkeypatch):
    _stub_resolver(monkeypatch, answers=[_Rec("203.0.113.5")])
    status, msg, _ = _check_dns(_cfg())
    assert status == "up"
    assert "203.0.113.5" in msg


def test_expected_value_match_up(monkeypatch):
    _stub_resolver(monkeypatch, answers=[_Rec("203.0.113.5"), _Rec("203.0.113.6")])
    status, _, _ = _check_dns(_cfg(expected_value="203.0.113.6"))
    assert status == "up"


def test_expected_value_mismatch_down(monkeypatch):
    _stub_resolver(monkeypatch, answers=[_Rec("203.0.113.5")])
    status, msg, _ = _check_dns(_cfg(expected_value="198.51.100.1"))
    assert status == "down"
    assert "Expected 198.51.100.1" in msg
    assert "203.0.113.5" in msg


def test_nxdomain_down(monkeypatch):
    _stub_resolver(monkeypatch, exc=dns.resolver.NXDOMAIN())
    status, msg, _ = _check_dns(_cfg(dns_query="nope.invalid"))
    assert status == "down"
    assert "NXDOMAIN" in msg


def test_no_answer_down(monkeypatch):
    _stub_resolver(monkeypatch, exc=dns.resolver.NoAnswer())
    status, msg, _ = _check_dns(_cfg(dns_record_type="AAAA"))
    assert status == "down"
    assert "No AAAA records" in msg


def test_custom_resolver_applied(monkeypatch):
    seen = {}

    class StubResolver:
        def __init__(self, configure=True):
            seen["configure"] = configure
            self.nameservers = []
            self.timeout = None
            self.lifetime = None

        def resolve(self, query, record_type):
            seen["nameservers"] = self.nameservers
            return [_Rec("203.0.113.5")]

    monkeypatch.setattr(dns.resolver, "Resolver", StubResolver)
    status, _, _ = _check_dns(_cfg(dns_resolver="1.1.1.1"))
    assert status == "up"
    assert seen["nameservers"] == ["1.1.1.1"]
    # custom resolver must skip system resolv.conf read
    assert seen["configure"] is False


def test_no_resolver_configuration_down(monkeypatch):
    """A broken/absent system resolv.conf raises at construction — must be
    caught and reported DOWN, not propagated out of _check_dns."""
    class BrokenResolver:
        def __init__(self, configure=True):
            raise dns.resolver.NoResolverConfiguration("no nameservers")

    monkeypatch.setattr(dns.resolver, "Resolver", BrokenResolver)
    status, msg, _ = _check_dns(_cfg())
    assert status == "down"
    assert msg
