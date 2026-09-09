"""Benchmarks: retrieval now, the router in Phase 8 and the agent in Phase 11.

Each one is a script that can be run on its own and a set of functions the backend's
``POST /api/evaluations/run`` will call in Phase 11. The metrics live apart from the runners so
that "what recall@5 means here" is stated once and tested without a database.
"""
