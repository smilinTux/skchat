"""DM self-delivery: an operator-authored outbound reaches the operator's OTHER
devices IN the real DM thread (same ``dm:<a>|<b>`` conversation id), not a
device-scoped side channel.

The bug: a DM sent from device A was delivered only to the peer (Lumina); the
operator's OWN sibling devices never received their own outbound, so device B
rendered a one-sided thread. These tests pin the fix: the per-device delivery
projection stores each sibling's copy under the SAME DM conversation id, skips
the originating device, and is idempotent on a send retry.
"""

from __future__ import annotations

from datetime import datetime, timezone

from skchat import daemon_proxy as dp
from skchat.message_log import MessageLog, conversation_id_for
from skchat.models import ChatMessage
from skchat.operator_auth import DeviceStore

OPERATOR = dp.OPERATOR_ID
LUMINA = dp.LUMINA_URI
DM_CONV = conversation_id_for(ChatMessage(sender=OPERATOR, recipient=LUMINA, content="x"))


# --------------------------------------------------------------------------- #
# MessageLog per-device delivery primitives
# --------------------------------------------------------------------------- #
def test_deliver_to_device_stores_under_real_conversation_id(tmp_path):
    log = MessageLog(str(tmp_path / "message_log.db"))
    msg = ChatMessage(id="m1", sender=OPERATOR, recipient=LUMINA, content="hey")
    log.deliver_message_to_device("dev-b", msg)

    rows = log.read_for_device("dev-b", DM_CONV)
    assert len(rows) == 1
    # The stored copy carries the REAL DM conversation id, NOT device-sync:<fp>.
    assert rows[0]["conversation_id"] == DM_CONV
    assert not rows[0]["conversation_id"].startswith("device-sync:")
    assert rows[0]["sender"] == OPERATOR and rows[0]["content"] == "hey"


def test_deliver_to_device_is_idempotent_on_retry(tmp_path):
    log = MessageLog(str(tmp_path / "message_log.db"))
    msg = ChatMessage(id="m1", sender=OPERATOR, recipient=LUMINA, content="hey")
    first = log.deliver_message_to_device("dev-b", msg)
    second = log.deliver_message_to_device("dev-b", msg)  # send retry
    assert first["deduped"] is False
    assert second["deduped"] is True
    assert second["seq"] == first["seq"]
    assert len(log.read_for_device("dev-b", DM_CONV)) == 1  # no duplicate


def test_read_for_device_is_scoped_per_device(tmp_path):
    log = MessageLog(str(tmp_path / "message_log.db"))
    msg = ChatMessage(id="m1", sender=OPERATOR, recipient=LUMINA, content="hey")
    log.deliver_message_to_device("dev-b", msg)
    assert len(log.read_for_device("dev-b", DM_CONV)) == 1
    assert log.read_for_device("dev-c", DM_CONV) == []  # a different device sees nothing


# --------------------------------------------------------------------------- #
# _self_deliver_own_devices fan-out
# --------------------------------------------------------------------------- #
def _fresh_store(tmp_path, monkeypatch):
    """A DeviceStore with two enrolled operator devices, wired into daemon_proxy,
    plus a message log rooted in *tmp_path*. Returns (store, dev_a_fp, dev_b_fp).
    """
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.delenv("SKCHAT_DM_SELF_DELIVER", raising=False)
    store = DeviceStore(tmp_path / "operator_devices.json")
    dev_a = store.enroll("pubkey-device-a")
    dev_b = store.enroll("pubkey-device-b")
    monkeypatch.setattr(dp, "_DEVICE_STORE", store)
    monkeypatch.setattr(dp, "_MSGLOG", MessageLog(str(tmp_path / "message_log.db")))
    return store, dev_a, dev_b


def test_self_deliver_reaches_sibling_in_dm_thread_skipping_origin(tmp_path, monkeypatch):
    _store, dev_a, dev_b = _fresh_store(tmp_path, monkeypatch)
    msg = ChatMessage(id="m1", sender=OPERATOR, recipient=LUMINA, content="hi lumina")

    n = dp._self_deliver_own_devices(msg, origin_device_fp=dev_a)
    assert n == 1  # only the sibling, not the origin

    log = dp._get_message_log()
    # Sibling device B retrieves the outbound UNDER THE SAME DM conversation id.
    sib = log.read_for_device(dev_b, DM_CONV)
    assert len(sib) == 1
    assert sib[0]["conversation_id"] == DM_CONV
    assert sib[0]["sender"] == OPERATOR and sib[0]["content"] == "hi lumina"
    # The originating device is NOT echoed its own message.
    assert log.read_for_device(dev_a, DM_CONV) == []


def test_self_deliver_is_idempotent_on_send_retry(tmp_path, monkeypatch):
    _store, dev_a, dev_b = _fresh_store(tmp_path, monkeypatch)
    msg = ChatMessage(id="m1", sender=OPERATOR, recipient=LUMINA, content="hi lumina")

    dp._self_deliver_own_devices(msg, origin_device_fp=dev_a)
    dp._self_deliver_own_devices(msg, origin_device_fp=dev_a)  # retry of same send

    log = dp._get_message_log()
    assert len(log.read_for_device(dev_b, DM_CONV)) == 1  # exactly one copy


def test_self_deliver_ignores_non_operator_sender(tmp_path, monkeypatch):
    _store, _dev_a, dev_b = _fresh_store(tmp_path, monkeypatch)
    # Lumina's own reply is addressed to the operator's devices already; her
    # send must NOT be self-delivered as if the operator authored it.
    reply = ChatMessage(id="r1", sender=LUMINA, recipient=OPERATOR, content="hi chef")
    n = dp._self_deliver_own_devices(reply, origin_device_fp=None)
    assert n == 0
    assert dp._get_message_log().read_for_device(dev_b) == []


def test_self_deliver_ignores_group_messages(tmp_path, monkeypatch):
    _store, dev_a, dev_b = _fresh_store(tmp_path, monkeypatch)
    grp = ChatMessage(
        id="g1",
        sender=OPERATOR,
        recipient="group:team",
        content="team msg",
        metadata={"group_id": "team"},
        timestamp=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    n = dp._self_deliver_own_devices(grp, origin_device_fp=dev_a)
    assert n == 0  # group fan-out owns its own delivery
    assert dp._get_message_log().read_for_device(dev_b) == []


def test_self_deliver_disabled_by_flag(tmp_path, monkeypatch):
    _store, dev_a, dev_b = _fresh_store(tmp_path, monkeypatch)
    monkeypatch.setenv("SKCHAT_DM_SELF_DELIVER", "0")
    msg = ChatMessage(id="m1", sender=OPERATOR, recipient=LUMINA, content="hi")
    n = dp._self_deliver_own_devices(msg, origin_device_fp=dev_a)
    assert n == 0
    assert dp._get_message_log().read_for_device(dev_b) == []
