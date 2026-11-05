"""Stimulus timing (eds.measure.common.source_settle_s / stimulus_changed / tone): a source call
returns before the new RF output reaches the DUT (Pluto TX: queued libiio buffers), so every
capture after a stimulus change must hold only samples taken after the source settled."""

from __future__ import annotations

import pytest

from eds.measure.common import (
    QUEUE_LATENCY_FACTOR,
    SCPI_SETTLE_S,
    SOURCE_SETTLE_MARGIN_S,
    source_settle_s,
    stimulus_changed,
)
from eds.measure.stimulus import probe_tone

from .spec_fixtures import DelayedSimSource, replace_source, sim_app, sim_context

FC, FS = 915e6, 2e6


class _Stated:
    settle_s = 0.123


class _PlutoTx:
    name = "pluto_tx"
    estimated_queue_latency_s = 4 * 8192 / 1e6          # rx_pluto.toml source_args: fs 1 MS/s, buffer_len 8192


class _Scpi:
    name = "scpi"


class _Broken:
    name = "pluto_tx"

    @property
    def settle_s(self):                                   # e.g. a dead worker-process proxy
        raise RuntimeError("bağlantı yok")


def test_source_settle_time_rules():
    assert source_settle_s(None) == 0.0
    assert source_settle_s(_Stated()) == 0.123
    assert source_settle_s(_PlutoTx()) == pytest.approx(QUEUE_LATENCY_FACTOR * 0.032768 + SOURCE_SETTLE_MARGIN_S)
    assert source_settle_s(_PlutoTx()) > 0.04                       # ≥ the 32.8 ms TX queue of the finding
    assert source_settle_s(_Scpi()) == SCPI_SETTLE_S
    assert source_settle_s(_Broken()) == 0.0


@pytest.mark.parametrize("reported, visible", [(0.0, False), (0.25, True)])
def test_capture_after_a_switch_holds_only_the_new_stimulus(tmp_path, reported, visible):
    """The source turns on 0.15 s after ``set_enabled`` returns. Reporting that delay
    (``settle_s``) makes the tone visible at its true level; a source that claims to switch
    instantly (the old behaviour: no wait) gives a capture of the OFF state."""
    with sim_context(sim_app(tmp_path, fc=FC, fs=FS)) as ctx:
        replace_source(ctx, lambda w: DelayedSimSource(w, delay_s=0.15, settle_s=reported, freq_hz=FC + 0.1 * FS))
        dev = ctx.dut
        pr = probe_tone(ctx, dev, FC + 0.1 * FS, -60.0, nfft=4096, averages=8, window="blackmanharris", overlap=0.5,
                        search_hz=40e3)
        assert pr.visible is visible
        if visible:
            assert pr.level_dbfs == pytest.approx(dev.level_dbfs(-60.0), abs=0.3)
        assert stimulus_changed(ctx) == pytest.approx(reported)
