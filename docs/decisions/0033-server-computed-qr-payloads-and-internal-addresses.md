<!--
SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# 0033 — Printed QR payloads are computed by the server; internal addresses stay with admins

- **Status:** Proposed
- **Date:** 2026-11-22
- **Deciders:** maintainer

## Context

Every integration (Snipe-IT, Grocy, Spoolman, Paperless-ngx) has one base address setting,
`integrations.<name>.url`. The hub uses it to call the app's API, and the plugins build the QR
payload of a looked-up item from it (`<base>/hardware/42`, `<base>/product/7`,
`<base>/spool/show/3`). Until 1.7.x the lookup route answered that payload as `url`, the web UI
showed it as the "Open in …" link and sent it back as `data.qr_payload` to
`POST /api/v1/render/preview`.

That base address is often an internal one: a container name, a LAN host or a Tailscale address
that the hub can reach and a phone cannot (#247). Three problems follow:

- the link in the UI and the printed code point at an address a person scanning the label cannot
  open;
- every key that may read, including a read-only key handed to a script or a display, learns the
  internal address of every connected app from lookup answers and from the job history, where
  `GET /api/v1/jobs` and `GET /api/v1/jobs/{id}` return the stored `label_data` with its
  `qr_payload` (also nested: sheet cells, the sheet ID cell, the rows of a listing label);
- the preview draws whatever payload the client sends, so the preview and the print can show two
  different codes.

## Decision

The server computes the QR payload of an integration item from the app and item id with one
function, a separate public address decides what people see and scan, and internal base
addresses are only answered to admin callers.

- **A public web address per integration.** The setting `integrations.<name>.web_url` (kind
  `url`, empty by default, locked by `LABELYARD_<NAME>_WEB_URL` like the other integration
  settings) is validated like every URL setting: http or https, no user info, at most 2048
  characters.
- **One rebase rule.** `rebase(url, internal_base, web_url)` moves a URL from the internal base
  onto the web address. The internal base matches only when it is followed by `/`, `?`, `#` or the
  end of the URL (`http://h:80` is not a prefix of `http://h:8080/x`); one trailing slash is
  ignored on both bases; path, query and fragment are kept; anything outside the internal base is
  not rewritten.
- **Links follow the web address only.** Every `url` a lookup route answers (the item lookup and
  the Grocy battery and chore reads) is the rebased link (`web_link_for`); without `web_url` it is
  `null` and the UI shows no link. A guard derives the routes whose answer has a `url` field from
  the running app, so a new one cannot skip this.
- **Printed codes follow the web address once it is set.** `qr_payload_for(app, item_id, label,
  settings)` is the payload every print path uses for a looked-up item: tape print, batch, sheet
  print, the webhooks and the render check. Without `web_url` the payload stays what it is today,
  so nothing printed changes until the operator sets the address.
- **The preview uses the same function.** `POST /api/v1/render/preview` accepts an optional
  `item: {app, id}`. With it the server runs the lookup (the same path, errors and rate-limit
  accounting as `GET /api/v1/lookup/{app}/{id}`) and draws `qr_payload_for`; a `data.qr_payload`
  sent along is ignored, not refused. Without `item` (a manual label) the client's payload is
  drawn as before.
- **Internal addresses stay with admins.** For a caller whose scope is not `admin`, every
  `qr_payload` in a job payload (tape `label_data`, `detail.cells[].label_data`,
  `detail.id_cell.label_data` and each `label_data.items[]` row) is answered in its web form when
  `web_url` is set and as `null` when it is not (`redact_for`, `redact_job_payload`). This holds
  for the job list, the job detail, a printer's queue and every other route that answers a job.
  A label of a looked-up integration is trusted only under that integration's **current**
  addresses: a payload stored under an earlier base, or of an integration that was cleared since,
  is `null`. A payload of any other label (`manual`, `hub`) that carries no internal base of any
  integration, and a Grocy code, is answered unchanged. An internal host is recognised in every
  spelling (case, trailing dot, user info, port, percent-encoding, IDNA), and the longer of two
  matching prefixes decides whether a payload is public or internal. The stored row is never
  changed; an admin sees it as stored.
- **Two rules, on purpose.** Job answers are redacted for every scope below `admin`, print keys
  and SSO users included, while the previews show the printed code to every caller that may
  print: a job answer is history anyone with read access can list, a preview is the picture of
  paper the caller could print anyway. Both are safe; the difference is by design.

- **The preview image is a response too.** `POST /api/v1/render/preview` and
  `POST /api/v1/print/{printer}/sheet/preview` are open to read scope (`Depends(require_read)`).
  Both draw an item's code through one function, `preview_qr_for`. A caller that may print (`print` or `admin` scope, an SSO user, the
  Basic bypass unless it is limited to `read`) sees the code that prints: it could print the item
  and read the paper anyway. Any other caller gets the code through `redact_for`: the web form
  when there is one; while there is none the image carries a stand-in code of the same byte length
  (the same QR version, so size and fit stay those of the print), flagged in the answer: the
  header `X-Labelyard-QR: withheld` on the tape preview, `qr_withheld` on the sheet preview's cell.

Printed paper is out of scope: it is not a response.

## Options considered

### Option A — server-computed payload, web address, redaction for non-admins (picked)

- Pros: preview and print draw the same code from the same function; the internal address no
  longer reaches read and print keys through lookup answers or job history; nothing printed
  changes until the operator opts in with `web_url`.
- Cons: the lookup `url` is `null` until `web_url` is set, so the "Open in …" link disappears on
  upgrade; the web UI has to send `item` instead of the payload; every route that answers a job
  has to go through the redaction.

### Option B — rewrite the payload in the plugins

- Pros: one place per integration.
- Cons: the plugin needs the internal URL for its API calls and the lookup link, the printed
  payload and the redaction each want a different form; it would mix three decisions into the
  plugin code that each integration repeats.

### Option C — keep the client's payload and only hide the lookup `url`

- Pros: smallest change.
- Cons: the client can no longer send the right payload once `url` is hidden, and a payload the
  client chooses can differ from what prints; the job history still carries the internal address.

## Consequences

- Upgrade note: the lookup `url` is `null` until `web_url` is set; printed codes follow `web_url`
  once it is set; non-admin keys no longer see internal addresses in job answers.
- A read-only caller's preview of an item without `web_url` shows a stand-in code, not the one that
  prints; setting `web_url` makes the preview and the print agree for every caller. (Found by the
  security review of the first draft, which drew the printed code for every caller.)
- `redact_job_payload` is the single walk over a job payload; a new nested place for label data
  is added there (the cell-label read joins it in a later task of 1.8.0).
- Code that follows this decision cites it as "ADR 0033" until it is accepted with the milestone.

## References

- Issue: #247
- Spec: milestone 1.8.0 design, sections 3.2 and 9.4 (S9, S16)
- Related ADRs: [0021](0021-api-versioning.md) (compatibility within `/api/v1`),
  [0017](0017-settings-in-database-env-locks-encrypted-secrets.md) (environment locks),
  [0032](0032-integration-plugins-and-layout-engine.md) (integration plugins)
