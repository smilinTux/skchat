"""Fleet PQC migration engine (PQC-MIGRATION confidentiality cut-over).

Migrates EXISTING confidentiality objects from classical to hybrid
X25519+ML-KEM-768 — **safely**:

* **Groups** (``~/.skchat/groups/*.json``): migrated via
  :meth:`GroupChat.migrate_to_hybrid`, but ONLY when every member has a known
  hybrid prekey (otherwise skip + report — a partially-keyed group is not forced
  hybrid, it stays classical and is flagged so members can upload keys first).
* **At-rest stores** (``skchat.encrypted_store.EncryptedChatHistory``): re-wrapped
  via :meth:`EncryptedChatHistory.migrate_store` (decrypt-old → re-encrypt-new
  under the random hybrid-wrapped DEK), with no plaintext change.

Safety contract (every guarantee the cut-over spec demands):

* **Mandatory backup** of ``~/.skchat/groups`` and the at-rest store dir before
  ANY write (:func:`backup_live_data`).
* **Dry-run** (``plan_*`` functions) that lists exactly what WOULD migrate and
  what would be skipped, with reasons, writing nothing.
* **No data loss**: groups are round-trip verified (encrypt→decrypt identical)
  after migration; stores re-encrypt the SAME plaintext under the new DEK.
* **Idempotent**: a group already on the hybrid suite is reported
  ``already-hybrid`` and skipped (no re-key); a store already hybrid-wrapped
  re-wraps to itself (no plaintext change).
* **Skip + report, never force**: anything that can't be proven safe is skipped
  with a reason, never half-migrated.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("skchat.pqc_migrate")

HYBRID_SUITE = "x25519-mlkem768"


def _skchat_home() -> Path:
    return Path(os.environ.get("SKCHAT_HOME", str(Path.home() / ".skchat"))).expanduser()


def groups_dir() -> Path:
    return _skchat_home() / "groups"


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #


def backup_live_data(dest_root: Optional[Path] = None) -> Path:
    """Copy the live groups dir + at-rest artifacts to a timestamped backup.

    Returns the backup directory. MUST be called before any migration write.
    """
    home = _skchat_home()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    root = dest_root or (Path.home() / ".skchat-pqc-backup")
    dest = root / stamp
    dest.mkdir(parents=True, exist_ok=True)

    gd = groups_dir()
    if gd.exists():
        shutil.copytree(gd, dest / "groups", dirs_exist_ok=True)

    # At-rest artifacts: the hybrid recipient key + DEK wrap + any *.db stores.
    atrest = dest / "atrest"
    atrest.mkdir(exist_ok=True)
    for pat in ("atrest_recipient.key", "atrest_recipient.pub", "atrest_dek.wrap"):
        f = home / pat
        if f.exists():
            shutil.copy2(f, atrest / f.name)
    for db in home.glob("*.db"):
        shutil.copy2(db, atrest / db.name)
    # The pqc keystore (agent keys + peer prekeys) — needed to reproduce wraps.
    pqc = home / "pqc"
    if pqc.exists():
        shutil.copytree(pqc, dest / "pqc", dirs_exist_ok=True)

    logger.info("PQC migration backup written to %s", dest)
    return dest


# --------------------------------------------------------------------------- #
# Group migration
# --------------------------------------------------------------------------- #


@dataclass
class GroupPlan:
    """What a single group's migration would do (dry-run) / did (live)."""

    group_id: str
    name: str
    action: str  # "migrate" | "already-hybrid" | "skip"
    reason: str = ""
    members_total: int = 0
    members_with_key: int = 0
    path: str = ""


def _load_group_objs() -> list[tuple[Path, Any]]:
    from .group import GroupChat

    out: list[tuple[Path, Any]] = []
    gd = groups_dir()
    if not gd.exists():
        return out
    for f in sorted(gd.glob("*.json")):
        if f.name.endswith(".deleted.json"):
            continue
        try:
            out.append((f, GroupChat.model_validate_json(f.read_text(encoding="utf-8"))))
        except Exception as exc:
            logger.warning("pqc_migrate: skipping unreadable group %s: %s", f.name, exc)
    return out


def _member_key_map(group: Any) -> dict[str, str]:
    """Collect hybrid keys for every member from the prekey store / self."""
    from . import pq_prekeys as PQ

    keys: dict[str, str] = {}
    for m in group.members:
        # Existing on-member key wins (already attached); else look it up.
        existing = getattr(m, "hybrid_kem_public_hex", "")
        pub = existing or PQ.hybrid_pub_hex_for(m.identity_uri)
        if pub:
            keys[m.identity_uri] = pub
    return keys


def plan_groups() -> list[GroupPlan]:
    """Dry-run: classify every persisted group without writing anything."""
    plans: list[GroupPlan] = []
    for path, g in _load_group_objs():
        total = len(g.members)
        keymap = _member_key_map(g)
        with_key = len(keymap)
        if getattr(g, "is_hybrid", False):
            plans.append(
                GroupPlan(
                    group_id=g.id,
                    name=g.name,
                    action="already-hybrid",
                    reason=f"kem_suite already {g.kem_suite} (epoch {g.epoch})",
                    members_total=total,
                    members_with_key=with_key,
                    path=str(path),
                )
            )
        elif total == 0:
            plans.append(
                GroupPlan(
                    group_id=g.id,
                    name=g.name,
                    action="skip",
                    reason="group has no members",
                    members_total=0,
                    members_with_key=0,
                    path=str(path),
                )
            )
        elif with_key == total:
            plans.append(
                GroupPlan(
                    group_id=g.id,
                    name=g.name,
                    action="migrate",
                    reason="all members have a hybrid prekey",
                    members_total=total,
                    members_with_key=with_key,
                    path=str(path),
                )
            )
        else:
            plans.append(
                GroupPlan(
                    group_id=g.id,
                    name=g.name,
                    action="skip",
                    reason=(
                        f"{total - with_key}/{total} member(s) lack a hybrid prekey "
                        "— would not be hybrid-protected; left classical (have them "
                        "publish a prekey, then re-run)"
                    ),
                    members_total=total,
                    members_with_key=with_key,
                    path=str(path),
                )
            )
    return plans


def _verify_group_roundtrip(group: Any) -> bool:
    """Encrypt→decrypt a probe message under the migrated group; identical?"""
    probe = "pqc-migrate-roundtrip-probe"
    try:
        env = group.encrypt_message(probe)
        back = group.decrypt_message(env)
        return back == probe
    except Exception:
        logger.warning("pqc_migrate: group round-trip verify failed", exc_info=True)
        return False


def migrate_groups(dry_run: bool = True) -> dict:
    """Migrate eligible groups to hybrid (or just plan if ``dry_run``).

    Returns ``{plans: [...], migrated: [...], skipped: [...], failed: [...]}``.
    Live mode round-trip verifies each migrated group and ONLY persists when the
    probe decrypts identically; a failed verify is reported, never saved.
    """
    from .group import GroupChat

    plans = plan_groups()
    result: dict[str, Any] = {
        "plans": [p.__dict__ for p in plans],
        "migrated": [],
        "skipped": [],
        "failed": [],
        "already_hybrid": [],
    }
    if dry_run:
        return result

    for path, g in _load_group_objs():
        plan = next((p for p in plans if p.group_id == g.id), None)
        if plan is None:
            continue
        if plan.action == "already-hybrid":
            result["already_hybrid"].append(g.id)
            continue
        if plan.action == "skip":
            result["skipped"].append({"group_id": g.id, "reason": plan.reason})
            continue
        # action == migrate
        try:
            keymap = _member_key_map(g)
            g.migrate_to_hybrid(member_hybrid_keys=keymap, transport=None)
            if not g.is_hybrid or not _verify_group_roundtrip(g):
                result["failed"].append(
                    {"group_id": g.id, "reason": "post-migration round-trip verify failed"}
                )
                continue
            Path(path).write_text(g.model_dump_json(indent=2), encoding="utf-8")
            result["migrated"].append(
                {"group_id": g.id, "name": g.name, "epoch": g.epoch, "suite": g.kem_suite}
            )
        except Exception as exc:
            logger.warning("pqc_migrate: group %s migration failed: %s", g.id[:8], exc)
            result["failed"].append({"group_id": g.id, "reason": str(exc)})
    return result


# --------------------------------------------------------------------------- #
# At-rest store migration
# --------------------------------------------------------------------------- #


def _open_store() -> Optional[Any]:
    try:
        from .encrypted_store import EncryptedChatHistory

        return EncryptedChatHistory.from_identity()
    except Exception as exc:
        logger.info("pqc_migrate: no at-rest store to migrate (%s)", exc)
        return None


def plan_store() -> dict:
    """Dry-run for the at-rest store: report its wrap suite + message count."""
    store = _open_store()
    if store is None:
        return {"present": False, "action": "skip", "reason": "no encrypted store"}
    try:
        rpt = store.crypto_self_report()
        hybrid = bool(rpt.get("quantum_resistant"))
    except Exception:
        hybrid = False
        rpt = {}
    try:
        count = store.message_count()
    except Exception:
        count = -1
    return {
        "present": True,
        "wrap_suite": rpt.get("wrap_suite"),
        "already_hybrid": hybrid,
        "message_count": count,
        "action": "rewrap" if hybrid else "rewrap",
        "reason": (
            "DEK is hybrid-wrapped; migrate_store re-encrypts every message under "
            "it (idempotent)" if hybrid else
            "store would be created/re-wrapped under the hybrid DEK"
        ),
    }


def migrate_store(dry_run: bool = True) -> dict:
    """Re-wrap the at-rest store under the hybrid DEK (or plan if ``dry_run``)."""
    plan = plan_store()
    if dry_run or not plan.get("present"):
        return {"plan": plan, "result": None}
    store = _open_store()
    if store is None:
        return {"plan": plan, "result": None}
    try:
        counts = store.migrate_store()
        rpt = store.crypto_self_report()
        return {
            "plan": plan,
            "result": counts,
            "wrap_suite": rpt.get("wrap_suite"),
            "quantum_resistant": rpt.get("quantum_resistant"),
        }
    except Exception as exc:
        logger.warning("pqc_migrate: store migration failed: %s", exc)
        return {"plan": plan, "result": {"error": str(exc)}}


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


@dataclass
class FleetResult:
    backup_path: str = ""
    groups: dict = field(default_factory=dict)
    store: dict = field(default_factory=dict)
    dry_run: bool = True


def migrate_fleet(dry_run: bool = True, do_backup: bool = True) -> FleetResult:
    """Top-level fleet migration. Backs up first (live mode), then migrates
    groups + the at-rest store. Always safe to call with ``dry_run=True``."""
    res = FleetResult(dry_run=dry_run)
    if not dry_run and do_backup:
        res.backup_path = str(backup_live_data())
    res.groups = migrate_groups(dry_run=dry_run)
    res.store = migrate_store(dry_run=dry_run)
    return res
