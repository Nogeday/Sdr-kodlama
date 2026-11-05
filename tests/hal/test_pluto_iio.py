"""Thin libiio layer (eds.hal.pluto_iio) against the pylibiio fake."""

from __future__ import annotations

import errno

import pytest

from eds.core.errors import BackendUnavailable, ConfigError, DeviceError, DeviceNotFound, NotSupported
from eds.hal.pluto_iio import (
    ADC_DMA_STATUS_OVERFLOW,
    ADC_DMA_STATUS_REG,
    EXTERNAL_REF_NOTE,
    PlutoIIO,
    attr_text,
    parse_number,
    parse_range,
    scan_serials,
)
from tests.hal.fakes import fake_iio

URI = "ip:192.168.2.1"
P = "ad9361-phy"


@pytest.fixture
def board(monkeypatch):
    b = fake_iio.make_pluto_board(URI)
    fake_iio.install(monkeypatch, b)
    return b


def test_backend_unavailable_without_iio(monkeypatch):
    fake_iio.uninstall(monkeypatch)
    with pytest.raises(BackendUnavailable, match="python3-libiio"):
        PlutoIIO(URI)


def test_open_errors(monkeypatch):
    busy = fake_iio.make_pluto_board("usb:1.5.5", open_error=(errno.EBUSY, "Device or resource busy"))
    fake_iio.install(monkeypatch, busy)
    with pytest.raises(DeviceNotFound, match="busy"):
        PlutoIIO("usb:1.5.5")
    with pytest.raises(DeviceNotFound, match="ip:10.0.0.9"):
        PlutoIIO("ip:10.0.0.9")
    with pytest.raises(ConfigError):
        PlutoIIO("none")


def test_open_without_phy(monkeypatch):
    b = fake_iio.make_pluto_board(URI)
    b.devices = [d for d in b.devices if d.name != P]
    fake_iio.install(monkeypatch, b)
    with pytest.raises(DeviceNotFound, match="ad9361-phy"):
        PlutoIIO(URI)


def test_xo_correction_roundtrip_exact_int(board):
    with PlutoIIO(URI) as pl:
        assert pl.xo_boot_hz == 40_000_000
        assert pl.get_xo_correction() == 40_000_000
        assert pl.set_xo_correction(40_000_133) == 40_000_133
        assert board.writes[-1] == (f"{P}/xo_correction", "40000133")
        # float input is rounded to an integer string (sysfs rejects "4.0000133e7")
        assert pl.set_xo_correction(39_999_708.4) == 39_999_708
        assert board.writes[-1] == (f"{P}/xo_correction", "39999708")
        with pytest.raises(DeviceError, match="xo_correction"):
            pl.set_xo_correction(41_000_000)          # driver range ±200 ppm → EINVAL
        assert pl.get_xo_correction() == 39_999_708   # unchanged after the rejected write
        with pytest.raises(ConfigError):
            pl.set_xo_correction(0)


def test_xo_correction_missing_on_old_firmware(monkeypatch):
    b = fake_iio.make_pluto_board(URI, has_xo=False)
    fake_iio.install(monkeypatch, b)
    pl = PlutoIIO(URI)
    assert pl.xo_boot_hz is None
    with pytest.raises(NotSupported):
        pl.get_xo_correction()
    with pytest.raises(NotSupported):
        pl.set_xo_correction(40_000_100)
    assert b.writes == []


def test_tracking_set_get(board):
    pl = PlutoIIO(URI)
    assert pl.get_tracking() == {"bb_dc": True, "rf_dc": True, "quad": True}
    st = pl.set_tracking(bb_dc=False, quad=False)
    assert st == {"bb_dc": False, "rf_dc": True, "quad": False}
    assert board.writes == [(f"{P}/in/voltage0/bb_dc_offset_tracking_en", "0"),
                            (f"{P}/in/voltage0/quadrature_tracking_en", "0")]
    assert pl.set_tracking(rf_dc=False) == {"bb_dc": False, "rf_dc": False, "quad": False}
    assert board.value(f"{P}/in/voltage0/rf_dc_offset_tracking_en") == "0"


def test_gain_control_mode(board):
    pl = PlutoIIO(URI)
    assert pl.gain_control_modes_available() == ("manual", "fast_attack", "slow_attack", "hybrid")
    assert pl.gain_control_mode == "slow_attack"
    assert pl.set_gain_control_mode("fast_attack") == "fast_attack"
    pl.gain_control_mode = "manual"
    assert board.value(f"{P}/in/voltage0/gain_control_mode") == "manual"
    with pytest.raises(ConfigError, match="turbo"):
        pl.set_gain_control_mode("turbo")
    assert board.writes == [(f"{P}/in/voltage0/gain_control_mode", "fast_attack"),
                            (f"{P}/in/voltage0/gain_control_mode", "manual")]


def test_hardwaregain_available_parsing(board):
    assert PlutoIIO(URI).hardwaregain_available() == (-3.0, 1.0, 71.0)
    assert parse_range("[0 1 73]") == (0.0, 1.0, 73.0)
    assert parse_range(" [ -1.5 0.25 73.0 ] ") == (-1.5, 0.25, 73.0)
    with pytest.raises(DeviceError):
        parse_range("manual slow_attack")


def test_temperature_and_rssi(board):
    pl = PlutoIIO(URI)
    assert pl.temperature_c() == pytest.approx(36.25, abs=1e-9)
    assert pl.rssi_db() == pytest.approx(105.25)
    assert parse_number("71.000000 dB") == 71.0


def test_temperature_raw_scale_fallback(monkeypatch):
    fake_iio.install(monkeypatch, fake_iio.make_pluto_board(URI, temp_mode="raw", temp_milli=41000.0))
    assert PlutoIIO(URI).temperature_c() == pytest.approx(41.0, abs=1e-3)


def test_temperature_missing(monkeypatch):
    fake_iio.install(monkeypatch, fake_iio.make_pluto_board(URI, temp_mode="none"))
    with pytest.raises(NotSupported):
        PlutoIIO(URI).temperature_c()


@pytest.mark.parametrize("objects", [False, True])
def test_identity_from_ctx_attrs_str_or_objects(monkeypatch, objects):
    fake_iio.install(monkeypatch, fake_iio.make_pluto_board(URI, ctx_attr_objects=objects, fw_version="v0.39"))
    pl = PlutoIIO(URI)
    assert pl.firmware_version() == "v0.39"
    assert pl.serial() == "104473dc5993000917000e00c83b2e5e35"


def test_serial_falls_back_to_usb_serial(monkeypatch):
    fake_iio.install(monkeypatch, fake_iio.make_pluto_board(URI, include_serial=False, hw_serial="ABC123"))
    assert PlutoIIO(URI).serial() == "ABC123"


def test_attr_text_helper():
    assert attr_text(" v0.38\n") == "v0.38"
    assert attr_text(fake_iio._ValueAttr("x ")) == "x"


def test_refclk_info_is_read_only(board):
    pl = PlutoIIO(URI)
    pl.set_xo_correction(40_000_500)
    n_writes = len(board.writes)
    info = pl.refclk_info()
    assert len(board.writes) == n_writes                    # nothing written (firmware env untouched)
    assert info["xo_correction_hz"] == 40_000_500
    assert info["xo_correction_boot_hz"] == 40_000_000
    assert info["xo_correction_available"] == "[39992000 1 40008000]"
    assert info["fw_version"] == "v0.38"
    assert "fw_setenv" in info["external_ref_note"] and "ASLA" in info["external_ref_note"]
    assert info["external_ref_note"] == EXTERNAL_REF_NOTE


def test_io_error_maps_to_device_error(board):
    pl = PlutoIIO(URI)
    board.read_errors.add(f"{P}/in/voltage0/rssi")
    with pytest.raises(DeviceError, match="rssi"):
        pl.rssi_db()


def test_close(board):
    pl = PlutoIIO(URI)
    assert not pl.closed
    pl.close()
    assert pl.closed
    with pytest.raises(DeviceError, match="kapalı"):
        pl.get_xo_correction()
    with pytest.raises(DeviceError):
        pl.firmware_version()
    pl.close()   # idempotent


# ----------------------------------------------------------------------------- RX DMA overflow register
def test_rx_dma_overflow_bit_read_and_write_one_to_clear(board):
    pl = PlutoIIO(URI)
    assert pl.rx_overflow_supported()
    assert pl.read_rx_dma_status() == 0 and pl.read_clear_rx_overflow() is False
    board.dma_overflow()
    board.rx_regs[ADC_DMA_STATUS_REG] |= 0x2                 # another sticky status bit
    assert pl.read_rx_dma_status() & ADC_DMA_STATUS_OVERFLOW
    assert pl.read_clear_rx_overflow() is True
    # exactly the iio_adi_xflow_check access: reg 0x80000088, bit 2 written back to clear it
    assert board.reg_log[-1] == ("w", "cf-ad9361-lpc", 0x80000088, 0x4)
    assert board.rx_regs[ADC_DMA_STATUS_REG] == 0x2          # only the overflow bit cleared
    assert pl.read_clear_rx_overflow() is False
    assert board.writes == []                                # no IIO attribute was touched
    assert (ADC_DMA_STATUS_REG, ADC_DMA_STATUS_OVERFLOW) == (0x80000088, 0x4)


def test_rx_dma_register_errors(monkeypatch):
    b = fake_iio.make_pluto_board(URI, dma_regs=False)
    fake_iio.install(monkeypatch, b)
    pl = PlutoIIO(URI)
    assert not pl.rx_overflow_supported()                   # no direct_reg_access
    with pytest.raises(NotSupported):
        pl.read_clear_rx_overflow()
    b2 = fake_iio.make_pluto_board(URI)
    fake_iio.install(monkeypatch, b2)
    pl = PlutoIIO(URI)
    b2.reg_error = (errno.ETIMEDOUT, "Connection timed out")
    with pytest.raises(DeviceError, match="DMA"):
        pl.read_clear_rx_overflow()
    pl.close()
    with pytest.raises(DeviceError, match="kapalı"):
        pl.read_rx_dma_status()


# ----------------------------------------------------------------------------- libiio scan
def test_scan_serials_maps_uri_to_serial(monkeypatch):
    a = fake_iio.make_pluto_board("usb:1.5.5", hw_serial="104473dc5993000917000e00c83b2e5e35")
    n = fake_iio.make_pluto_board("ip:192.168.3.1", hw_serial="1044734c9605000f12003300af7c5b6a8e")
    mod = fake_iio.install(monkeypatch, a, n, scan_extra={"local:": "Local devices"})
    assert scan_serials() == {"usb:1.5.5": "104473dc5993000917000e00c83b2e5e35",
                              "ip:192.168.3.1": "1044734c9605000f12003300af7c5b6a8e"}
    assert mod.scan_contexts()["usb:1.5.5"].startswith("0456:b673 (Analog Devices Inc. PlutoSDR")


def test_scan_serials_errors(monkeypatch):
    fake_iio.uninstall(monkeypatch)
    with pytest.raises(BackendUnavailable):
        scan_serials()
    mod = fake_iio.install(monkeypatch)

    def broken():
        raise OSError(errno.EIO, "Input/output error")

    mod.scan_contexts = broken
    with pytest.raises(DeviceError, match="tarama"):
        scan_serials()
