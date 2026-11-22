# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Guard: no route answers an integration link with the internal base address (ADR 0033).

The routes are taken from the running app, not from a list: every route whose response model
carries a field named ``url`` (at any depth) is found. Each one needs a sample request in
``SAMPLES`` (a new route without one fails the first test), and each sample is called with a
read key, without and with web addresses: the answer never names an internal base, and every
``url`` is either ``null`` or under a web address. Harness: ``tests/helpers/web_url_hub.py``.
"""

from __future__ import annotations

import types
import typing
from typing import Any

import app.models  # noqa: F401
import pytest
from pydantic import BaseModel

from tests.helpers.routes import iter_api_routes
from tests.helpers.web_url_hub import WEB, Hub, assert_no_internal, h, hub

pytestmark = pytest.mark.asyncio
__all__ = ["hub"]

#: path template -> a request path the harness's upstreams answer.
SAMPLES = {
    "/api/v1/lookup/{app}/{entity_id}": [
        "/api/v1/lookup/snipeit/A-1",
        "/api/v1/lookup/grocy/7",
        "/api/v1/lookup/spoolman/3",
    ],
    "/api/v1/lookup/grocy/batteries/{battery_id}": ["/api/v1/lookup/grocy/batteries/4"],
    "/api/v1/lookup/grocy/chores/{chore_id}": ["/api/v1/lookup/grocy/chores/7"],
}


def _models(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    if isinstance(annotation, (typing._GenericAlias, types.GenericAlias, types.UnionType)):  # type: ignore[attr-defined]
        return [m for arg in typing.get_args(annotation) for m in _models(arg)]
    return []


def _has_url(model: type[BaseModel], seen: set[type[BaseModel]] | None = None) -> bool:
    seen = seen if seen is not None else set()
    if model in seen:
        return False
    seen.add(model)
    for name, field in model.model_fields.items():
        if name == "url":
            return True
        if any(_has_url(m, seen) for m in _models(field.annotation)):
            return True
    return False


def _routes_with_url() -> set[str]:
    from app.main import create_app

    inner = create_app()._app
    return {
        route.path
        for route in iter_api_routes(inner.routes)
        if route.response_model is not None
        and any(_has_url(m) for m in _models(route.response_model))
    }


async def test_every_route_with_a_url_field_has_a_sample() -> None:
    found = _routes_with_url()
    assert found, "the walk over the response models found nothing"
    assert found == set(SAMPLES), f"routes with a url field: {sorted(found)}"


def _urls(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return [v for k, v in node.items() if k == "url"] + [
            u for v in node.values() for u in _urls(v)
        ]
    if isinstance(node, list):
        return [u for v in node for u in _urls(v)]
    return []


@pytest.mark.parametrize("with_web", [False, True])
async def test_no_link_answers_an_internal_base_url(hub: Hub, with_web: bool) -> None:
    if with_web:
        hub.web("snipeit", "grocy", "spoolman")
    async with hub.running() as (client, keys):
        answers = [
            (path, await client.get(path, headers=h(keys.read)))
            for paths in SAMPLES.values()
            for path in paths
        ]
    for path, r in answers:
        assert r.status_code == 200, f"{path}: {r.text}"
        assert_no_internal(path, r.text)
        for url in _urls(r.json()):
            if with_web:
                assert any(url.startswith(w.rstrip("/")) for w in WEB.values()), path
            else:
                assert url is None, path
