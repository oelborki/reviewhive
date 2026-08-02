"""Postgres persistence.

Nothing outside this package imports SQLAlchemy. Callers depend on the
`ReviewStore` protocol in `reviewhive.persistence`, which is why the default test
run never needs the database extra installed.

This module deliberately re-exports nothing: importing `reviewhive.db` must not
drag in a driver.
"""
