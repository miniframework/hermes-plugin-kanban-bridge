"""Publisher — resolve profile routing descriptions and share/unshare workers on Hub."""

from __future__ import annotations

import logging
from typing import List

log = logging.getLogger(__name__)


def _read_profile_md(profile_dir, names) -> str:
    """Full stripped content of the first existing file among ``names``."""
    for name in names:
        path = profile_dir / name
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except Exception:
            log.debug("publisher: could not read %s", path)
    return ""


def _read_profile_description(profile_name: str, *, auto_generate: bool = True) -> str:
    """Resolve a profile's routing description for Hub sharing.

    Always starts with ``profiles/<name>/SOUL.md`` (seeded on first run,
    so effectively always present), then appends a second part:
    ``AGENTS.md`` if it exists, otherwise the profile.yaml ``description``.
    Falls back to profile_describer auto-generation, then the profile
    name, only when nothing above is available.
    """
    try:
        from hermes_cli import profiles as profiles_mod

        profile_dir = profiles_mod.get_profile_dir(profile_name)

        soul = _read_profile_md(profile_dir, ("SOUL.md", "soul.md"))
        agents = _read_profile_md(profile_dir, ("AGENTS.md", "agents.md"))
        second = agents or (
            profiles_mod.read_profile_meta(profile_dir).get("description") or ""
        ).strip()

        parts = [p for p in (soul, second) if p]
        if parts:
            return "\n\n".join(parts)
    except Exception:
        log.debug("publisher: could not resolve description for %s", profile_name)

    if auto_generate:
        try:
            from hermes_cli.profile_describer import describe_profile

            outcome = describe_profile(profile_name)
            if outcome.ok and outcome.description:
                return outcome.description
            log.debug(
                "publisher: auto-describe skipped for %s (%s)",
                profile_name,
                getattr(outcome, "reason", "?"),
            )
        except Exception:
            log.debug("publisher: profile_describer failed for %s", profile_name)

    return profile_name


def publish_workers(hub_client, publish_config: List[dict]):
    """Publish workers listed in config to Hub.

    publish_config example:
      - profile: backend-dev
        share: true
      - profile: gpu-worker
        share: false
    """
    for entry in publish_config:
        profile = entry.get("profile", "")
        should_share = entry.get("share", False)
        if not profile:
            continue

        if should_share:
            auto_gen = bool(entry.get("auto_describe", True))
            desc = _read_profile_description(profile, auto_generate=auto_gen)
            max_conc = entry.get("max_concurrent", 1)
            r = hub_client.share_worker(profile, description=desc, max_concurrent=max_conc)
            if r:
                log.info("publisher: shared %s on hub (%s)", profile, r.get("action", "?"))
            else:
                log.warning("publisher: failed to share %s", profile)
        else:
            hub_client.unshare_worker(profile)
            log.debug("publisher: %s not shared (share=false)", profile)
