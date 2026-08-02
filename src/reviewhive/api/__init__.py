"""The HTTP surface.

The only package that imports FastAPI. Everything it orchestrates —
`github/`, `jobs.py`, the store — is reachable without it, which is what keeps
the bulk of the phase's tests in the default offline suite.
"""
