# Integration tests

Empty by design until Phase 2.

These tests run the API against a real PostgreSQL instance through Testcontainers, which needs a
working Docker daemon. The development machine does not have one yet — Docker Desktop on Windows
Home requires the WSL2 backend, and WSL is not installed — so writing tests that cannot be
executed would mean shipping unverified assertions.

The packages are already referenced, so the first test to be written needs no project changes.

**Planned coverage**

- `POST /api/auth/login` issues a token the protected endpoints accept, and rejects bad credentials.
- `POST /api/incidents` assigns sequential `INC-XXXXX` codes from the database default, including
  under concurrent inserts.
- `DELETE /api/services/{id}` returns 409 rather than a foreign-key error when incidents exist.
- Role policies: a `viewer` token cannot create an incident; an `engineer` token can.
