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
by re-reading the registry row after each removal pass, capped at
:data:`_MAX_SLOT_SWEEP_PASSES`. If the cap is reached while a publish is still
racing ahead of the sweep, whatever is left unswept is appended to
``slots_failed`` with a WARNING, so the cap cannot turn into a silent hole of
its own. A ``remove_peer_bundle`` call that returns False (rather than
raising) only counts as a failure when the slot file is confirmed still on
disk afterward: the row keeps a device's ``key_ids`` after unlink, so a
second, idempotent unlink of the same device would otherwise report a false
``slots_failed`` for a slot that is genuinely already gone.

Inherited limitation: :func:`skchat.pq_prekeys.remove_peer_bundle` only removes
the per-device slot file (``peers/<short>/<key_id>.json``); it cannot remove a
LEGACY flat ``peers/<short>.json`` bundle from a pre-multislot deployment. This
is currently inert for the default owner (``chef``), whose legacy flat file is
already retired to a ``.bak``, but would resurface for an owner that still has
one.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("skchat.device_unlink")

#: Cap on prekey-slot removal sweep passes (finds key_ids re-read from the
#: registry after each pass). Bounds the loop against a device that somehow
#: keeps publishing new slots for the whole unlink; 3 is generous for a single
#: in-flight-request race and cannot spin.
_MAX_SLOT_SWEEP_PASSES = 3


def _slot_path(owner: str, key_id: str) -> Path:
    """The on-disk path a slot's ``key_id`` would live at, for existence checks.

    Uses :mod:`skchat.pq_prekeys`'s own path-building (``_peer_dir`` /
    ``_safe_slot_id``) so this can never diverge from where
    :func:`skchat.pq_prekeys.remove_peer_bundle` actually looks; there is no
    public accessor for it.
    """
    from skchat import pq_prekeys as PQ

    return PQ._peer_dir(owner) / f"{PQ._safe_slot_id(key_id)}.json"


def unlink_device(device_fp: str, *, device_store, owner: str = "chef") -> dict:
    """Revoke *device_fp* everywhere. Returns a per-step report.

    Args:
        device_fp: The device to unlink.
        device_store: The live :class:`capauth.pairing.DeviceStore`.
        owner: Short name whose prekey slots hold this device (default ``chef``).

    Returns:
        dict: ``device_fp``, ``sessions_revoked`` (always True on return),
        ``slots_removed`` (key_ids actually deleted from disk),
        ``slots_failed`` (key_ids the registry claimed existed whose removal
        raised, whose slot file was confirmed still on disk after a False
        return, or that could not be swept before
        :data:`_MAX_SLOT_SWEEP_PASSES` was reached), ``registry_had_no_slots``
        (True when the registry row was missing or had no ``key_ids``,
        meaning slot removal could not even be attempted), ``store_removed``,
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
    #    failure (raise, or a False return whose slot file is confirmed still
    #    on disk) is recorded rather than silently treated as removed. After
    #    each pass, re-read the registry row: a publish landing mid-unlink
    #    adds a new key_id that the initial snapshot could not have known
    #    about, and without the sweep it would survive forever.
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
                removed = PQ.remove_peer_bundle(owner, key_id)
            except Exception:
                slots_failed.append(key_id)
                logger.warning(
                    "unlink: prekey slot %s not removed for %s", key_id, device_fp, exc_info=True
                )
                continue
            if removed:
                slots_removed.append(key_id)
                continue
            # A False return means "no slot file found" today, which covers
            # both a genuinely already-absent slot (a second, idempotent
            # unlink of the same device: the registry row keeps its key_ids
            # after the first unlink, so a re-run would otherwise report a
            # false slots_failed) and a swallowed OSError mid-delete inside
            # remove_peer_bundle. Only the latter is a real hole, so check
            # the actual file rather than trusting the bool alone.
            if _slot_path(owner, key_id).exists():
                slots_failed.append(key_id)
                logger.warning("unlink: prekey slot %s not removed for %s", key_id, device_fp)
        latest_row = DR.get_device(device_fp)
        pending = list((latest_row or {}).get("key_ids") or [])

    # The sweep is capped so a persistently racing publisher cannot spin
    # forever; whatever is still unseen when the cap is reached is a real,
    # still-live slot that must not disappear from the report silently.
    unswept = [k for k in pending if k not in seen_key_ids]
    if unswept:
        slots_failed.extend(unswept)
        logger.warning(
            "unlink: %d prekey slot(s) unswept after %d passes for %s: %s",
            len(unswept),
            _MAX_SLOT_SWEEP_PASSES,
            device_fp,
            unswept,
        )

    # 3. Remove the auth key so no NEW session can be minted.
    store_removed = device_store.remove(device_fp)

    # 4. Best-effort: revoke every capauth pairing record behind the PDP grant.
    capauth_revoked, capauth_records_failed = revoke_capauth_subject(device_fp)

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


def revoke_capauth_subject(device_fp: str) -> tuple[bool, int]:
    """Revoke every capauth pairing device record for ``device:<device_fp>``.

    Public (not underscore-prefixed) so ``skchat devices reset`` (R1) can call
    it directly for every device being reset, the same way :func:`unlink_device`
    calls it for a single device: the reset path must close the same capauth
    hole a single unlink already closes, not a parallel, weaker mechanism.

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
    ``device:<fp>`` subject string, and ``reason`` has no default. Calling
    ``revoke(operator_subject(device_fp))`` as drafted would raise a
    ``TypeError`` on the missing ``reason`` even before the wrong-argument
    problem, always landing in the except branch. The real lookup is subject ->
    matching device records (:func:`capauth.pairing.list_devices`) -> each
    record's own ``device_id`` -> :func:`capauth.pairing.revoke`.

    Migration-window fallback (coord card N6): ``operator_subject`` now mints
    ``device:<fp>``, but the live pairing store still holds roughly 140 device
    records under the retired ``operator:<fp>`` shape until the separate
    one-shot store-rewrite card runs. ``list_devices`` is an exact-string
    matcher with no aliasing (by design, IDENTITY_NAMING_STANDARD.md sec
    2.4), so a lookup under only the new shape would silently find nothing
    for those records and this security-critical revoke would no-op. This
    function therefore ALSO looks up the legacy ``operator:<fp>`` subject and
    revokes anything found there too, deduplicated by ``device_id`` (the same
    device can only be enrolled under one of the two shapes at a time). Drop
    the legacy lookup once the store rewrite is confirmed complete fleet-wide.
    """
    try:
        from capauth.pairing import default_base_dir, list_devices, revoke

        from skchat.dataplane_auth import operator_subject

        subject = operator_subject(device_fp)
        legacy_subject = f"operator:{device_fp}"
        base = default_base_dir()
        devices = list(list_devices(subject, base_dir=base, include_revoked=False))
        if legacy_subject != subject:
            seen_ids = {d.device_id for d in devices}
            for d in list_devices(legacy_subject, base_dir=base, include_revoked=False):
                if d.device_id not in seen_ids:
                    devices.append(d)
                    seen_ids.add(d.device_id)
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
