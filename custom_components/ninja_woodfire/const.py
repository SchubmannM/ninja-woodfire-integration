"""Constants for the Ninja Woodfire HA integration."""
from __future__ import annotations

from datetime import timedelta

from ._lib.models import ACTIVE_COOK_STATES

DOMAIN = "ninja_woodfire"

CONF_REGION = "region"
CONF_DSN = "dsn"
# Optional credential overrides — empty in the entry means the
# integration uses its bundled per-region defaults.
CONF_AUTH0_AUDIENCE = "auth0_audience"
CONF_AUTH0_CLIENT_ID = "auth0_client_id"
CONF_AYLA_APP_ID = "ayla_app_id"
CONF_AYLA_APP_SECRET = "ayla_app_secret"

# Adaptive polling cadence — fast while cooking, relaxed while idle.
SCAN_INTERVAL_ACTIVE = timedelta(seconds=1)        # while cooking / preheat / rest (matches app's ~750ms)
SCAN_INTERVAL_IDLE = timedelta(seconds=10)         # while idle
DEFAULT_SCAN_INTERVAL = SCAN_INTERVAL_IDLE         # initial; coordinator adapts
MIN_SCAN_INTERVAL = timedelta(seconds=1)
DEFAULT_REGION = "EU"

# Cook states that should trigger fast polling. Re-exported from the
# transport-agnostic layer so there is exactly one definition — the two
# copies had already drifted apart (this one was missing "heat").
ACTIVE_STATES = ACTIVE_COOK_STATES

# How long the cloud's last-reported timestamp may lag before a snapshot
# stops counting as a description of the grill right now. Entities that
# mirror grill state go unavailable past this point rather than rendering
# a stale snapshot as fact.
#
# Generous on purpose: the grill reports irregularly, and flapping
# entities are worse than a slightly late one. Staleness *within* this
# window is still visible on the "last reported" diagnostic sensor.
STATE_MAX_AGE = timedelta(minutes=5)

# The device record (which carries connection_status) is refreshed on its
# own slower cadence — it is a second HTTP request, and connectivity does
# not change between one-second property polls.
DEVICE_META_INTERVAL = timedelta(seconds=30)

# Cook-lifecycle events fired on the HA event bus. See coordinator
# `_emit_lifecycle_events`. Automations subscribe via `event_type`.
EVENT_COOK_STARTED = "ninja_woodfire_cook_started"
EVENT_PREHEAT_COMPLETE = "ninja_woodfire_preheat_complete"
EVENT_COOK_HALFTIME = "ninja_woodfire_cook_halftime"
EVENT_COOK_DONE = "ninja_woodfire_cook_done"
EVENT_PROBE_TARGET_REACHED = "ninja_woodfire_probe_target_reached"
EVENT_PROBE_HALFWAY = "ninja_woodfire_probe_halfway"
