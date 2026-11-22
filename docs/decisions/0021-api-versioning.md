<!--
SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# 0021 — Every API route lives under `/api/v1`

- **Status:** Accepted
- **Date:** 2026-11-10
- **Deciders:** maintainer
- **Decision labels:** D31
- **Amended:** 2026-11-22 (#204) — compatibility rules within `/api/v1`: additive fields, bounded lists with a `Link` header (`GET /api/v1/sheets` at most 500 rows), tolerated and tightened request changes with upgrade notes (D31), see "Amendment — 2026-11-22" at the end
- **Related:** [0011](0011-openapi-as-api-contract.md) (OpenAPI is the contract); the paths quoted in older ADRs are the original ones (#226)

## Context

Until 1.4 the hub's HTTP API grew without a prefix: some routes sat under `/api/v1`, others under `/api`, a few at the root. Clients (integrations, the web UI, scripts) could not tell which paths were part of the contract, and there was no room to introduce a `v2` later without a mixed layout. With the web UI served by the same process (ADR 0015), the single-page-app catch-all also needs a clear, small set of paths it must never swallow.

## Decision

**We serve every API route under `/api/v1/`, except a fixed, short list of unversioned paths. The route guard test is the enforcement.**

The unversioned allow-list is exactly the set encoded in `ALLOWED_UNVERSIONED` of `backend/tests/integration/api/test_route_versioning.py`:

| Path | Why it stays unversioned |
|---|---|
| `/healthz`, `/readiness` | Probes used by orchestrators and the container health check |
| `/openapi.json` | The generated contract |
| `/docs`, `/docs/scalar.js` | The interactive API documentation and its bundle |
| `/loc/{entity_id}`, `/asset/{entity_id}`, `/spool/{entity_id}`, `/product/{entity_id}` | Scan targets printed into QR codes; they must stay valid for labels that are already stuck on things |
| `/{full_path:path}` | The single-page-app catch-all, present only when a built UI exists |

**No aliases on moves.** When a route moved under `/api/v1`, the old path was not kept as an alias, redirect or duplicate. A client on an old path gets a plain 404 (or the SPA page for a browser path) rather than a second contract to maintain.

The guard test walks the real route table, recursing into included routers (a naive walk of the top-level routes would see almost nothing and let the test pass vacuously, so a companion test pins the walk). It fails when a route outside `/api/v1/` is not on the list, and also when a list entry is no route any more, so the list cannot rot. The end-to-end control routes used by browser tests are served by a separate ASGI wrapper that never exists in production, covered by their own test.

## Options considered

### Option A — everything under `/api/v1`, fixed allow-list, no aliases (chosen)
- Pros: one rule a client can rely on; room for `/api/v2`; the allow-list is small, reviewed and tested; no legacy surface to maintain.
- Cons: a one-time breaking move for clients on old paths (accepted for a 1.x minor with a changelog entry).

### Option B — keep old paths as aliases
- Pros: no client breaks.
- Cons: two contracts for every route, aliases that never get removed, and the guard test could no longer say "this is the whole surface".

### Option C — leave the layout mixed and document it
- Pros: no change.
- Cons: the problem stays; every new route re-opens the question.

## Consequences

- A new route outside `/api/v1` fails the guard test until it is either moved or deliberately added to the allow-list in a reviewed change (and then this ADR is amended).
- QR links on printed labels keep working because the scan-target paths are on the list.
- The generated frontend schema and all documentation use the `/api/v1` paths; older ADRs carry an "Amended" header line noting that.
- A future `v2` can live beside `v1` without moving anything.

## References

- Issue: #226
- Guard test: `backend/tests/integration/api/test_route_versioning.py`
- Related ADRs: [0011](0011-openapi-as-api-contract.md), [0015](0015-single-process-deployment.md)

## Amendment — 2026-11-22 (#204): compatibility within `/api/v1` (D31)

The decision above fixes where the contract lives; it said nothing about how a route may change while it stays at `v1`. 1.8.0 needs that answer: the sheet list must be bounded (#204), and several requests are tightened. The rules for every change within `/api/v1` (spec 3.2 of the 1.8.0 design):

- **Responses only grow.** No response field is removed or renamed. A new field is additive and has a default, so a client that ignores it keeps working. Where a field's meaning would change, a new field is added and the old one keeps its meaning, with a description that points to the new one.
- **A list is bounded opt-in or with a `Link` header, never by changing the body.** A list that has to be bounded keeps its body a plain JSON array and names the next page in a `Link: <…>; rel="next"` header (RFC 8288, a relative reference with the other query parameters kept, `backend/app/api/pagination.py`); `limit` and `offset` are query parameters. `GET /api/v1/sheets` is the first: at most 500 rows per request (default and largest `limit`), with the order and every filter applied in SQL before the page is cut. A bounded default instead of opt-in because the bound protects the hub for every caller (#204: 20,000 sheets); 500 lies above the default per-type cap of active sheets (200), so an ordinary installation never sees a second page.
- **A loosening, a tolerated request change and a tightening are allowed in a minor release, each with an upgrade note** (`website/src/content/docs/admin/upgrades/`, English and German). A loosening accepts what was refused (deleting a seeded type) or bounds an answer (the sheet list). A tolerated change ignores a part of a request in favour of the server's own value (the preview's `data.qr_payload` when `item` is sent). A tightening refuses a value that was accepted but wrong or unsafe (user info in URLs, invisible format characters, an oversized corner radius); it is released as a `fix`.
- **Anything else is a new route or a new version.** A response field removed or renamed, a changed body shape or a changed meaning of an existing field does not ship under `/api/v1`.

The generated client (`frontend/src/api/schema.gen.ts`) and the OpenAPI document (ADR 0011) describe the headers of a bounded list; the web UI follows `Link` where it needs every row (`fetchAllPages` in `frontend/src/api/client.ts`).
