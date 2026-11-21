# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""PTouchBackend — wraps the `ptouch` Python library for Brother PT-Series.

Status queries go through query_status_over_socket; print calls run the synchronous library in
asyncio.to_thread and rewrap its exceptions as our PrinterError subtypes (#395: status reads in
``ptouch_status``, raw send in ``ptouch_raw``). The names tests patch (``_ptouch_print``,
``_ptouch_print_multi``, ``query_preflight``) are imported here and every call site stays here.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from PIL import Image

if TYPE_CHECKING:
    import httpx

from app.models.tape import TapeSpec
from app.printer_backends.exceptions import PrinterError, TapeMismatchError, refused_before_sending
from app.printer_backends.ptouch_raw import (  # noqa: F401 — patched by tests, shared with callers
    _PTOUCH_PRINTER_CLASSES,
    _PTOUCH_TAPE_CLASSES,
    _ptouch_errors,
    _ptouch_print,
    _ptouch_print_multi,
    _ptouch_render,
    _require_tape_class,
)
from app.printer_backends.ptouch_status import (
    preflight_check_impl,
    query_status_impl,
    read_status_impl,
)
from app.printer_backends.snmp_helper import PreflightStatus, query_preflight, roll_record
from app.printer_backends.status_query import query_status_over_socket
from app.printer_backends.tape_confirmation import (
    TapeIppTransport,
    TapeSendState,
    check_label_lengths,
    label_length_mm,
)
from app.services.status_block import StatusBlock


class PTouchBackend:
    """PrinterBackend backed by the ptouch library."""

    backend_id = "ptouch"
    half_cut_supported: bool = True

    def __init__(
        self,
        host: str,
        *,
        port: int = 9100,
        model_id: str = "PT-P750W",
        community: str = "public",
        ipp_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not host:
            raise ValueError("PTouchBackend requires a non-empty host")
        if model_id not in _PTOUCH_PRINTER_CLASSES:
            raise ValueError(
                f"Unknown printer_model {model_id!r}; known: {sorted(_PTOUCH_PRINTER_CLASSES)}"
            )
        self.host = host
        self._port = port
        self._model_id = model_id
        # The stored SNMP community (#296): the preflight asks with it, like discovery does.
        self._community = community
        # #389: the status read right before the last send (``None``: none was read for it).
        self.last_send_status: PreflightStatus | None = None
        # #393: IPP or TCP 9100, the labels sent and the IPP jobs to confirm after sending.
        self.send_state = TapeSendState(host, TapeIppTransport(host, transport=ipp_transport))

    def check_label_lengths(
        self, images: list[Image.Image], tape_spec: TapeSpec, **_options: Any
    ) -> list[float]:
        """The tape length of each label; ``TapeLabelTooLongError`` (``nothing_sent``) for a
        label longer than the tape allows (R26-2). Called at submit and again by the send."""
        lengths = [label_length_mm(image, self.dpi) for image in images]
        check_label_lengths(lengths, tape_spec.max_length_mm)
        return lengths

    @property
    def dpi(self) -> int:
        """The model's raster resolution along the tape (``RESOLUTION_DPI``: 180 on the PT-P750W,
        360 on the PT-P900 family); high resolution doubles the dots, not the length (R26-2)."""
        return int(getattr(_PTOUCH_PRINTER_CLASSES[self._model_id], "RESOLUTION_DPI", 180))

    @classmethod
    def from_settings(cls, settings: Any) -> PTouchBackend:
        host = getattr(settings, "pt750w_host", "") or ""
        if not host:
            raise ValueError(
                "Empty pt750w_host with printer_backend=ptouch — "
                "set LABELYARD_PT750W_HOST to the printer's IP/hostname."
            )
        return cls(
            host=host,
            port=int(getattr(settings, "pt750w_port", 9100)),
            model_id=str(getattr(settings, "printer_model", "PT-P750W")),
        )

    async def query_status(self) -> StatusBlock:
        return await query_status_impl(self.host, self._port, query=query_status_over_socket)

    async def read_status(
        self,
        *,
        community: str | None = None,
        timeout_s: float = 3.0,
    ) -> PreflightStatus:
        """One SNMP read (``ptouch_status.read_status_impl``); never refuses on a bit."""
        return await read_status_impl(
            self.host,
            self._community if community is None else community,
            timeout_s,
            self.send_state,
            query_preflight=query_preflight,
        )

    async def preflight_check(
        self,
        *,
        community: str | None = None,
        timeout_s: float = 3.0,
    ) -> PreflightStatus:
        """SNMP preflight: ``read_status`` plus a refusal on every blocking error bit.

        Use this BEFORE submitting a print job. Raises ``PrinterOfflineError`` (SNMP failed),
        ``TapeEmptyError``, ``PrinterCoverOpenError`` or ``PrinterReportedError`` (any other bit
        of ``post_send.HARD_ERROR_FLAGS``, #389). It does NOT raise ``TapeMismatchError``: the
        caller compares ``loaded_tape_mm`` with the request, so this stays agnostic of what is
        printed.
        """
        return await preflight_check_impl(
            lambda: self.read_status(community=community, timeout_s=timeout_s)
        )

    async def _check_before_send(self, tape_spec: TapeSpec) -> None:
        """#389: the status read right before sending, kept for the check after sending and the
        job record; refuses on a blocking bit or a different tape width."""
        status = await self.preflight_check()
        self.last_send_status = status
        if status.loaded_tape_mm != tape_spec.width_mm:
            raise TapeMismatchError(
                expected_mm=tape_spec.width_mm,
                loaded_mm=status.loaded_tape_mm,
            )

    @property
    def flags_before_send(self) -> frozenset[str]:
        """The error bits of the read before the last send (``post_send.check_after_send``)."""
        status = self.last_send_status
        return frozenset(status.error_flags) if status is not None else frozenset()

    def roll_at_send(self) -> dict[str, object] | None:
        """The tape the last send was checked against (#389), for the job record."""
        return roll_record(self.last_send_status)

    async def print_image(
        self,
        image: Image.Image,
        tape_spec: TapeSpec,
        *,
        auto_cut: bool = True,
        high_resolution: bool = False,
        half_cut: bool = False,
        last_page: bool = True,
        detect_confirmation: bool = False,
    ) -> None:
        """SNMP preflight, then one label (#393: IPP Print-Job when the printer takes it, else TCP
        9100; ``detect_confirmation`` (the test print) tries IPP and the check after sending
        stores what works). ``TapeMismatchError`` is raised here after comparing the loaded
        tape with *tape_spec*. ``last_page=False`` is ptouch-py ``feed=False`` (~5mm feed, not
        the ~22.5mm pre-roll between batch items).
        """
        self.last_send_status = None
        (length,) = self.check_label_lengths([image], tape_spec)  # R26-2, before any SNMP
        if detect_confirmation:
            self.send_state.reset_counter_gate()  # R3-2: the read below asks for the counter
        try:
            await self._check_before_send(tape_spec)
        except PrinterError as exc:
            refused_before_sending(exc)  # #277: the preflight refused, nothing was sent
            raise

        self.send_state.begin(detect=detect_confirmation)

        async def raw() -> None:
            with _ptouch_errors():
                await asyncio.to_thread(
                    _ptouch_print,
                    self.host,
                    self._port,
                    image,
                    tape_spec.width_mm,
                    model_id=self._model_id,
                    auto_cut=auto_cut,
                    high_resolution=high_resolution,
                    half_cut=half_cut,
                    last_page=last_page,
                )

        data = b""
        if await self.send_state.use_ipp():
            data = await self._render(
                [image],
                tape_spec.width_mm,
                auto_cut=auto_cut,
                high_resolution=high_resolution,
                half_cut=half_cut,
                last_page=last_page,
                multi=False,
            )
        await self.send_state.send(data, 1, raw, length_mm=length)

    async def _render(
        self,
        images: list[Image.Image],
        tape_mm: int,
        *,
        auto_cut: bool,
        high_resolution: bool,
        half_cut: bool,
        last_page: bool = True,
        multi: bool,
    ) -> bytes:
        """#393: the raster bytes of the IPP path, built by ``ptouch`` without sending."""
        try:
            return await asyncio.to_thread(
                _ptouch_render,
                images,
                tape_mm,
                model_id=self._model_id,
                auto_cut=auto_cut,
                high_resolution=high_resolution,
                half_cut=half_cut,
                last_page=last_page,
                multi=multi,
            )
        except PrinterError as exc:
            refused_before_sending(exc)  # nothing was sent
            raise

    async def print_images(
        self,
        images: list[Image.Image],
        tape_spec: TapeSpec,
        *,
        auto_cut: bool = True,
        high_resolution: bool = False,
        half_cut: bool = True,
    ) -> None:
        """Batch-print via ptouch ``print_multi`` — atomic: one SNMP preflight at the start, a
        tape mismatch raises before anything is sent, 5mm half-cut between labels."""
        if not images:
            raise ValueError("print_images requires at least one image")
        self.last_send_status = None
        lengths = self.check_label_lengths(images, tape_spec)  # R26-2: the whole batch or none
        try:
            _require_tape_class(tape_spec.width_mm)
            await self._check_before_send(tape_spec)
        except PrinterError as exc:
            refused_before_sending(exc)  # #277: no tape class or the preflight said no
            raise

        self.send_state.begin()

        async def raw() -> None:
            with _ptouch_errors():
                await asyncio.to_thread(
                    _ptouch_print_multi,
                    self.host,
                    self._port,
                    images,
                    tape_spec.width_mm,
                    model_id=self._model_id,
                    auto_cut=auto_cut,
                    high_resolution=high_resolution,
                    half_cut=half_cut,
                )

        data = b""
        if await self.send_state.use_ipp():
            data = await self._render(
                images,
                tape_spec.width_mm,
                auto_cut=auto_cut,
                high_resolution=high_resolution,
                half_cut=half_cut,
                multi=True,
            )
        await self.send_state.send(data, len(images), raw, length_mm=sum(lengths))
