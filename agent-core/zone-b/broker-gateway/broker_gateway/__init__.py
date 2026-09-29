"""AFE broker gateway (Zone B, ADR-004 Option C).

The only component that holds broker credentials. It forwards a submit to the broker only
with a valid, unexpired, unused Aegis attestation whose signed fields equal the order, and
otherwise allows only cancels and read-only queries. See ``authorize`` and README.md.
"""
