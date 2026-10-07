"""A site's TLS certificate: valid for its name, and when it expires.

One handshake, no HTTP request: it reads the certificate the server presents
and lets the platform's trust store verify it.
"""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class Certificate:
    #: Verified for this host name by the system trust store.
    valid: bool
    expires_at: datetime | None = None
    #: Why it isn't valid ("certificate has expired", "Hostname mismatch", ...),
    #: or why there was no handshake at all ("connection refused").
    error: str = ""
    #: The handshake happened (False: nothing listens on 443, or it timed out).
    reachable: bool = True
    issuer: str = ""

    def days_left(self, now: datetime | None = None) -> float | None:
        if self.expires_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (self.expires_at - now).total_seconds() / 86400


def certificate(host: str, port: int = 443, *, timeout: float = 10.0) -> Certificate:
    """The certificate `host` serves on `port`; never raises."""
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
    except ssl.SSLCertVerificationError as exc:
        return Certificate(valid=False, error=str(exc.verify_message or exc.reason or exc))
    except ssl.SSLError as exc:
        return Certificate(valid=False, error=f"TLS error: {exc.reason or exc}")
    except OSError as exc:
        return Certificate(valid=False, reachable=False, error=str(exc) or type(exc).__name__)
    expires = None
    if cert.get("notAfter"):
        try:
            expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]),
                                             tz=timezone.utc)
        except (ValueError, OverflowError):
            expires = None
    issuer = ""
    for part in cert.get("issuer") or ():
        for key, value in part:
            if key == "organizationName":
                issuer = value
    return Certificate(valid=True, expires_at=expires, issuer=issuer)
