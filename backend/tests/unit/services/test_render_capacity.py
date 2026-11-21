# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""#405 fix round 1: the number of concurrent renders is a setting (``render.max_concurrent``).

With the default 2 slots every caller without a key (signed-in users and page jobs) shares one
slot; from 3 slots two signed-in users render side by side. The slots follow a change once no
render holds or waits for one.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from uuid import uuid4

import pydantic
import pytest
from app.config import Settings
from app.services import render_slots
from app.settings_store.registry import SETTINGS
from app.settings_store.store import InvalidSettingValueError, _validate

pytestmark = pytest.mark.anyio

SHORT_S = 0.05
WAIT_S = 5.0


@pytest.fixture
def capacity() -> Iterator[None]:
    previous = render_slots.bind_max_concurrent(None)
    yield
    render_slots.bind_max_concurrent(previous)


@pytest.fixture
def release() -> Iterator[threading.Event]:
    event = threading.Event()
    yield event
    event.set()


async def _until_idle() -> None:
    async with asyncio.timeout(WAIT_S):
        while render_slots.stuck_renders() or render_slots.free_slots() < render_slots.capacity():
            await asyncio.sleep(0.01)


async def test_the_default_is_two_slots(capacity: None) -> None:
    assert render_slots.capacity() == render_slots.DEFAULT_MAX_CONCURRENT_RENDERS == 2
    assert await render_slots.render_bounded(lambda: 1, label="t", owner=None) == 1
    assert render_slots.free_slots() == 2


async def test_from_three_slots_two_sessions_render_side_by_side(
    capacity: None, release: threading.Event
) -> None:
    render_slots.bind_max_concurrent(lambda: 3)
    assert render_slots.capacity() == 3
    mine, theirs = render_slots.owner_of(None, uuid4()), render_slots.owner_of(None, uuid4())
    with pytest.raises(render_slots.RenderTimeoutError):
        await render_slots.render_bounded(
            release.wait, WAIT_S, label="t", owner=mine, timeout_s=SHORT_S
        )
    assert render_slots.free_slots() == 2
    assert await render_slots.render_bounded(lambda: 2, label="t", owner=theirs) == 2
    release.set()
    await _until_idle()


async def test_with_two_slots_the_other_session_is_refused(
    capacity: None, release: threading.Event
) -> None:
    """The honest default: one slot for everyone without a key; the stuck session gets 429,
    the others 503, a key renders."""
    mine, theirs = render_slots.owner_of(None, uuid4()), render_slots.owner_of(None, uuid4())
    with pytest.raises(render_slots.RenderTimeoutError):
        await render_slots.render_bounded(
            release.wait, WAIT_S, label="t", owner=mine, timeout_s=SHORT_S
        )
    with pytest.raises(render_slots.OwnerRenderStuckError):
        await render_slots.render_bounded(lambda: 0, label="t", owner=mine)
    with pytest.raises(render_slots.RenderUnavailableError):
        await render_slots.render_bounded(lambda: 0, label="t", owner=theirs)
    assert await render_slots.render_bounded(lambda: 3, label="t", owner=uuid4()) == 3
    release.set()
    await _until_idle()


async def test_a_new_slot_count_applies_once_no_render_holds_a_slot(
    capacity: None, release: threading.Event
) -> None:
    busy = asyncio.create_task(
        render_slots.render_bounded(release.wait, WAIT_S, label="t", owner=uuid4())
    )
    async with asyncio.timeout(WAIT_S):
        while render_slots.free_slots() == 2:
            await asyncio.sleep(0.01)
    render_slots.bind_max_concurrent(lambda: 4)
    assert render_slots.free_slots() == 1  # the old slots stay while a render holds one
    release.set()
    await busy
    assert await render_slots.render_bounded(lambda: 5, label="t", owner=None) == 5
    assert render_slots.free_slots() == 4


def test_the_setting_is_in_the_registry() -> None:
    d = SETTINGS["render.max_concurrent"]
    assert (d.kind, d.default, d.env_field) == ("int", 2, "render_max_concurrent")


@pytest.mark.parametrize("value", [1, 17, 0, -2])
def test_the_store_refuses_a_slot_count_out_of_bounds(value: int) -> None:
    with pytest.raises(InvalidSettingValueError):
        _validate(SETTINGS["render.max_concurrent"], value)


@pytest.mark.parametrize("value", [2, 3, 16])
def test_the_store_accepts_a_sane_slot_count(value: int) -> None:
    assert _validate(SETTINGS["render.max_concurrent"], value) == value


@pytest.mark.parametrize("value", ["1", "17", "x"])
def test_the_env_slot_count_is_bounded(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LABELYARD_RENDER_MAX_CONCURRENT", value)
    with pytest.raises(pydantic.ValidationError, match="render_max_concurrent"):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["0", "-1", "301", "inf", "nan"])
def test_the_env_lookup_deadline_is_bounded(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix round 1, Q4: like ``render_timeout_s``, no way to switch the deadline off."""
    monkeypatch.setenv("LABELYARD_LOOKUP_BATCH_DEADLINE_S", value)
    with pytest.raises(pydantic.ValidationError, match="lookup_batch_deadline_s"):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["0.5", "30", "300"])
def test_the_env_lookup_deadline_accepts_sane_values(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LABELYARD_LOOKUP_BATCH_DEADLINE_S", value)
    assert Settings(_env_file=None).lookup_batch_deadline_s == float(value)  # type: ignore[call-arg]
