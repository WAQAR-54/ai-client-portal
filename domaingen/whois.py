"""Real, credential-free domain availability checks via raw WHOIS (port 43)
- no registrar API key needed, but slower (~1-3s/domain) and occasionally
inconclusive since response formats aren't standardized across registries.
Only the three TLDs the Domain Generator UI actually offers (.com/.io/.ai)
are supported; anything else returns None (unknown) rather than guessing."""

import socket

WHOIS_SERVERS = {
    "com": "whois.verisign-grs.com",
    "io": "whois.nic.io",
    "ai": "whois.nic.ai",
}

# Checked in this order: a "Domain Name:" field is the strongest possible
# signal the domain is actually registered, so it's tested first and wins
# over any coincidental match below (registered-domain WHOIS records
# sometimes carry unrelated boilerplate that happens to contain a phrase
# like "not found" elsewhere in the terms-of-use text).
_TAKEN_INDICATOR = "domain name:"
_AVAILABLE_INDICATORS = (
    "no match for",
    "domain not found",
    "not found",
    "no data found",
    "no entries found",
    "status: free",
)


def _raw_whois_query(server, domain, timeout):
    with socket.create_connection((server, 43), timeout=timeout) as sock:
        sock.sendall((domain + "\r\n").encode())
        chunks = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks).decode(errors="replace")


def check_domain_available(name, tld, timeout=5):
    """True (available) / False (taken) / None (unsupported TLD, timeout,
    or a response this heuristic can't confidently read). `name` is the
    label only (e.g. "google", no TLD) - querying WHOIS for a bare label
    instead of the full "name.tld" returns a registry-dependent "no
    match" response for almost anything, since no such unqualified record
    exists, which used to read as a false "available"."""
    server = WHOIS_SERVERS.get(tld)
    if not server:
        return None
    try:
        raw = _raw_whois_query(server, f"{name}.{tld}", timeout)
    except (OSError, socket.timeout):
        return None

    lower = raw.lower()
    if _TAKEN_INDICATOR in lower:
        return False
    if any(indicator in lower for indicator in _AVAILABLE_INDICATORS):
        return True
    return None
