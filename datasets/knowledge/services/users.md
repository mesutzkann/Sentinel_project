---
type: service_doc
title: users service
service: users
id: SVC-users
---

# users service

Port 8081, schema `svc_users`. The simplest service in the estate: user records, and no
downstream dependency beyond its own database.

## Endpoints

`GET /users`, `GET /users/{id}`, `POST /users`.

## Dependencies

PostgreSQL through EF Core and Npgsql. It also calls orders in one path, and that call is the
only operationally interesting thing about it: the retry policy on that client is configuration,
and a bad value there is scenario 10.

## How it fails

> **Downstream load rises while inbound load does not.** Request rate into orders jumps roughly
> tenfold with no change in traffic arriving at users, and orders' logs fill with repeated
> identical requests. That is amplification: a retry count set high with no backoff. The service
> generating the load is healthy by every metric it exports, which is why looking at the loudest
> service first is the wrong move here.

## Chaos scenarios owned

- `RETRY_STORM` (10) — retry policy set to 10 attempts with no backoff. The victim is orders.
