"""Everything that speaks GitHub's protocol.

Nothing here imports FastAPI. That is what keeps signature checking, anchor
translation and the HTTP client testable on a bare `pip install -e ".[dev]"`,
with only the endpoint itself needing the `service` extra.
"""
