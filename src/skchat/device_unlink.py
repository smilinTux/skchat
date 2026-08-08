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
best-effort and never raise, because a capauth or registry hiccup must not leave
the earlier steps unreported.

Prekey removal calls :func:`skchat.pq_prekeys.remove_peer_bundle`, the SAME
primitive behind ``DELETE /v1/prekey/{peer}/{key_id}``, so the multi-device
revoke path and this unlink path can never diverge (spec R4).

Slot removal can only act on ``key_ids`` the registry knows about. A device
that published before its registry row existed (or that has no row at all)
leaves ``registry_had_no_slots`` True in the report and a WARNING in the log:
its slots cannot be located from the registry and may survive unlink. This is
NOT resolved by scanning the peer directory: without the registry there is no
reliable way to tell one device's slot from another's, and removing the wrong
slot would silently break a device that is still linked. A publish landing
mid-unlink (the app path is not yet closed by step 1 alone; in production
``SKCHAT_DATAPLANE_AUTH=1`` bounds this to requests already in flight) is swept
by re-reading the registry row after each removal pass, capped at 3 passes.

Inherited limitation: :func:`skchat.pq_prekeys.remove_peer_bundle` only removes
the per-device slot file (``peers/<short>/<key_id>.json``); it cannot remove a
LEGACY flat ``peers/<short>.json`` bundle from a pre-multislot deployment. This
is currently inert for the default owner (``chef``), whose legacy flat file is
already retired to a ``.bak``, but would resurface for an owner that still has
one.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("skchat.device_unlink")

#: Cap on prekey-slot removal sweep passes (finds key_ids re-read from the
#: registry after each pass). Bounds the loop against a device that somehow
#: keeps publishing new slots for the whole unlink; 3 is generous for a single
#: in-flight-request race and cannot spin.
_MAX_SLOT_SWEEP_PASSES = 3


def unlink_device(device_fp: str, *, device_store, owner: str = "chef") -> dict:
    """Revoke *device_fp* everywhere. Returns a per-step report.

    Args:
        device_fp: The device to unlink.
        device_store: The live :class:`skchat.operator_auth.DeviceStore`.
        owner: Short name whose prekey slots hold this device (default ``chef``).

    Returns:
        dict: ``device_fp``, ``sessions_revoked`` (always True on return),
        ``slots_removed`` (key_ids actually deleted from disk),
        ``slots_failed`` (key_ids the registry claimed existed but whose
        removal raised or returned False), ``registry_had_no_slots`` (True
        when the registry row was missing or had no ``key_ids``, meaning slot
        removal could not even be attempted), ``store_removed``,
        ``capauth_revoked`` (True only if at least one capauth pairing record
        was revoked this call), ``capauth_records_failed`` (count of matching
        records whose revoke call raised), and ``registry_marked``.

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

    initial_key_ids = list((row or {}).get("key_ids") or [])
    registry_had_no_slots = row is None or not initial_key_ids
    if registry_had_no_slots:
        logger.warning(
            "unlink: %s has no registry key_ids (row %s); its prekey slots "
            "cannot be located from the registry and may survive unlink",
            device_fp,
            "missing" if row is None else "is empty",
        )

    # 2. Drop its prekey slots so the next fanout cannot seal to it. A key_id
    #    failure (raise, or a False "no such slot" from a swallowed OSError)
    #    is recorded rather than silently treated as removed. After each pass,
    #    re-read the registry row: a publish landing mid-unlink adds a new
    #    key_id that the initial snapshot could not have known about, and
    #    without the sweep it would survive forever.
    slots_removed: list[str] = []
    slots_failed: list[str] = []
    seen_key_ids: set[str] = set()
    pending = list(initial_key_ids)
    for _pass in range(_MAX_SLOT_SWEEP_PASSES):
        new_this_pass = [k for k in pending if k not in seen_key_ids]
        if not new_this_pass:
            break
        for key_id in new_this_pass:
            seen_key_ids.add(key_id)
            try:
                if PQ.remove_peer_bundle(owner, key_id):
                    slots_removed.append(key_id)
                else:
                    slots_failed.append(key_id)
                    logger.warning(
                        "unlink: prekey slot %s already absent for %s", key_id, device_fp
                    )
            except Exception:
                slots_failed.append(key_id)
                logger.warning(
                    "unlink: prekey slot %s not removed for %s", key_id, device_fp, exc_info=True
                )
        latest_row = DR.get_device(device_fp)
        pending = list((latest_row or {}).get("key_ids") or [])

    # 3. Remove the auth key so no NEW session can be minted.
    store_removed = device_store.remove(device_fp)

    # 4. Best-effort: revoke every capauth pairing record behind the PDP grant.
    capauth_revoked, capauth_records_failed = _revoke_capauth_subject(device_fp)

    # 5. Keep the row for audit, hidden from the default list. Never raises:
    #    an OSError from the registry's file write must not swallow the report
    #    for steps 1-4, which already succeeded.
    try:
        registry_marked = DR.mark_revoked(device_fp)
    except Exception:
        registry_marked = False
        logger.warning("unlink: registry mark_revoked failed for %s", device_fp, exc_info=True)

    logger.info(
        "unlinked device %s (slots=%d failed=%d no_slots=%s store=%s capauth=%s/%d)",
        device_fp,
        len(slots_removed),
        len(slots_failed),
        registry_had_no_slots,
        store_removed,
        capauth_revoked,
        capauth_records_failed,
    )
    return {
        "device_fp": device_fp,
        "sessions_revoked": True,
        "slots_removed": slots_removed,
        "slots_failed": slots_failed,
        "registry_had_no_slots": registry_had_no_slots,
        "store_removed": store_removed,
        "capauth_revoked": capauth_revoked,
        "capauth_records_failed": capauth_records_failed,
        "registry_marked": registry_marked,
    }


def _revoke_capauth_subject(device_fp: str) -> tuple[bool, int]:
    """Revoke every capauth pairing device record for ``operator:<device_fp>``.

    Never raises. Returns ``(revoked_any, records_failed)``:

    * ``revoked_any`` is True only if at least one matching record was
      successfully revoked THIS call. A bare False is ambiguous by itself
      (no record exists for this subject, every record was already revoked
      by a prior idempotent unlink, or a record existed but its revoke call
      failed), which is why ``records_failed`` exists.
    * ``records_failed`` is the count of matching records whose ``revoke``
      call raised. Each record is tried independently: one failing record
      does not stop the rest from being attempted, since a left-alone
      non-revoked record is exactly what lets ``capauth.authz.decide`` keep
      granting capabilities to this device.

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
    except Exception:
        logger.debug("capauth revoke unavailable for %s (best-effort)", device_fp, exc_info=True)
        return False, 0

    revoked_any = False
    records_failed = 0
    for device in devices:
        try:
            revoke(device.device_id, "device unlinked", base_dir=base)
            revoked_any = True
        except Exception:
            records_failed += 1
            logger.warning(
                "unlink: capauth revoke failed for device_id=%s subject=%s",
                device.device_id,
                subject,
                exc_info=True,
            )
    return revoked_any, records_failed
