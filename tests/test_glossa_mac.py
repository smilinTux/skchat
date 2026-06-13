import asyncio

import pytest

from skchat.glossa_mesh.mac import CarrierSenseMAC, FakeAudioMedium


@pytest.mark.asyncio
async def test_two_transmits_collide_without_mac():
    med = FakeAudioMedium()
    # raw concurrent transmits overlap → medium marks a collision window
    await asyncio.gather(med.transmit_raw("a", [1.0] * 10),
                         med.transmit_raw("b", [1.0] * 10))
    assert med.had_collision is True


@pytest.mark.asyncio
async def test_mac_serializes_transmits_no_collision():
    med = FakeAudioMedium()
    mac = CarrierSenseMAC(med)
    await asyncio.gather(mac.send("a", [1.0] * 10),
                         mac.send("b", [1.0] * 10))
    assert med.had_collision is False        # MAC made them take turns
    assert med.transmissions == 2            # both still got through


def test_carrier_sense_reports_busy():
    med = FakeAudioMedium()
    assert med.is_busy() is False
    med._busy = True
    assert med.is_busy() is True
