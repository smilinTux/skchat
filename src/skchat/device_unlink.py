"""Unlink a linked device across every store that can still reach it.

The security crux of "Linked Devices". A device is present in four places, and
leaving it in ANY of them is a silent hole:

  1. ``revoked_device_fps``   - not there means its live sessions keep working.
  2. prekey slots             - not removed means fanout keeps SEALING NEW
                                MESSAGES the device can still decrypt, which is
                                the worst of the four because it is invisible.
  3. ``DeviceStore``          - not removed means it can mint a brand new session.
  4. capauth pairing record   - not revoked means the PDP still grants it
                                capabilities.

Order matters: sessions die FIRST, so nothing the device does during the rest of
the unlink is authorized. Every step is independently retry-safe, and a partial
failure leaves the device MORE locked out, never less. Steps 4 and 5 are
best-effort and never raise, because a capauth hiccup must not leave the first
three steps unreported.

Prekey removal calls :func:`skchat.pq_prekeys.remove_peer_bundle`, the SAME
primitive behind ``DELETE /v1/prekey/{peer}/{key_id}``, so the multi-device
revoke path and this unlink path can never diverge (spec R4).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("skchat.device_unlink")


def unlink_device(device_fp: str, *, device_store, owner: str = "chef") -> dict:
    """Revoke *device_fp* everywhere. Returns a per-step report.

    Args:
        device_fp: The device to unlink.
        device_store: The live :class:`skchat.operator_auth.DeviceStore`.
        owner: Short name whose prekey slots hold this device (default ``chef``).

    Raises:
        KeyError: if the fingerprint is in neither the registry nor the store.
    """
    from skchat import device_registry as DR
    from skchat import pq_prekeys as PQ
    from skchat.guest import revoke_device

    row = DR.get_device(device_fp)
    if row is None and not device_store.is_enrolled(device_fp):
        raise KeyError(device_fp)

    # 1. Kill every session this device holds, before anything else.
    revoke_device(device_fp)

    # 2. Drop its prekey slots so the next fanout cannot seal to it.
    slots_removed: list[str] = []
    for key_id in list((row or {}).get("key_ids") or []):
        try:
            if PQ.remove_peer_bundle(owner, key_id):
                slots_removed.append(key_id)
        except Exception:
            logger.warning("unlink: prekey slot %s not removed for %s", key_id, device_fp)

    # 3. Remove the auth key so no NEW session can be minted.
    store_removed = device_store.remove(device_fp)

    # 4. Best-effort: revoke the capauth pairing record behind the PDP grant.
    capauth_revoked = _revoke_capauth_subject(device_fp)

    # 5. Keep the row for audit, hidden from the default list.
    registry_marked = DR.mark_revoked(device_fp)

    logger.info(
        "unlinked device %s (slots=%d store=%s capauth=%s)",
        device_fp,
        len(slots_removed),
        store_removed,
        capauth_revoked,
    )
    return {
        "device_fp": device_fp,
        "sessions_revoked": True,
        "slots_removed": slots_removed,
        "store_removed": store_removed,
        "capauth_revoked": capauth_revoked,
        "registry_marked": registry_marked,
    }


def _revoke_capauth_subject(device_fp: str) -> bool:
    """Revoke every capauth pairing device record for ``operator:<device_fp>``.

    Never raises. Mirrors the best-effort posture of
    :mod:`skchat.operator_grants`: capauth is optional at runtime, and a failure
    here must not stop the caller from learning that steps 1 to 3 succeeded.

    Deviation from the brief's draft: :func:`capauth.pairing.revoke` is
    ``revoke(device_id, reason, *, base_dir=None)``. ``device_id`` is the pairing
    ``DeviceRecord.device_id`` (the enrollment id minted at ``enroll_device`` time,
    see :func:`skchat.operator_grants.grant_operator_capabilities`), not the
    ``operator:<fp>`` subject string, and ``reason`` has no default. Calling
    ``revoke(operator_subject(device_fp))`` as drafted would raise a
    ``TypeError`` on the missing ``reason`` even before the wrong-argument
    problem, always landing in the except branch. The real lookup is subject ->
    matching device records (:func:`capauth.pairing.list_devices`) -> each
    record's own ``device_id`` -> :func:`capauth.pairing.revoke`.
    """
    try:
        from capauth.pairing import default_base_dir, list_devices, revoke

        from skchat.dataplane_auth import operator_subject

        subject = operator_subject(device_fp)
        base = default_base_dir()
        devices = list_devices(subject, base_dir=base, include_revoked=False)
        if not devices:
            return False
        for device in devices:
            revoke(device.device_id, "device unlinked", base_dir=base)
        return True
    except Exception:
        logger.debug("capauth revoke unavailable for %s (best-effort)", device_fp, exc_info=True)
        return False
