"""DataUpdateCoordinator for the Ninja Woodfire grill."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ._lib.api.aws import AwsAuthError, AwsTransportError
from ._lib.api.ayla import AuthError, AylaCloudClient, TransportError
from ._lib.const import BACKEND_AYLA
from ._lib.capabilities import GrillCapabilities, for_oem_model
from ._lib.models import CombinedState

from .const import (
    ACTIVE_STATES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_COOK_DONE,
    EVENT_COOK_HALFTIME,
    EVENT_COOK_STARTED,
    EVENT_PREHEAT_COMPLETE,
    EVENT_PROBE_HALFWAY,
    EVENT_PROBE_TARGET_REACHED,
    SCAN_INTERVAL_ACTIVE,
    SCAN_INTERVAL_IDLE,
    STATE_MAX_AGE,
)

_LOGGER = logging.getLogger(__name__)


class NinjaWoodfireCoordinator(DataUpdateCoordinator[CombinedState]):
    """Polls the cloud every N seconds for the full grill state."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AylaCloudClient,
        dsn: str,
        capabilities: GrillCapabilities,
        device_key: int,
        device_info_extra: dict[str, str] | None = None,
        reader: Any | None = None,
        commander: Any | None = None,
        backend: str = BACKEND_AYLA,
    ) -> None:
        """
        Args:
            client: the Ayla client. All commands go through it — Ayla accepts
                writes even for a grill that has been migrated to AWS and no
                longer reports state there.
            reader: where state is read from. Defaults to `client`; on a
                migrated grill this is the AWS client instead. Anything with
                an async `read_state(dsn)` returning a CombinedState will do.
            commander: where cook commands are sent. Defaults to `client`.
                A grill that has been migrated to AWS no longer receives Ayla
                datapoints at all — they are accepted by the cloud, never
                acknowledged by the grill, and time out — so commands have to
                follow the reads onto the new backend.
            backend: which backend `reader` talks to, for diagnostics.
        """
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {dsn}",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client
        self.reader = reader if reader is not None else client
        self.commander = commander if commander is not None else client
        self.backend = backend
        self.dsn = dsn
        self.capabilities = capabilities
        self._device_key: int = device_key
        self.device_info_extra = device_info_extra or {}
        # Staged cook settings (consumed by Start when no live cook is
        # running). During an active cook, the live grill state takes
        # precedence in the live_or_staged_* properties.
        default_mode = capabilities.modes[0]
        self.cook_setting_mode: str = default_mode.name
        self.cook_setting_temp: int = default_mode.temp_default
        self.cook_setting_seconds: int = default_mode.duration_default_s
        self.cook_setting_smoke: bool = default_mode.smoke_default
        self.cook_setting_skip_preheat: bool = False
        self.cook_setting_probe0_enabled: bool = False
        self.cook_setting_probe0_setpoint: int = 60
        self.cook_setting_probe1_enabled: bool = False
        self.cook_setting_probe1_setpoint: int = 60
        # Lifecycle-event tracking — one-shot flags reset on each cook.
        self._prev_grill_state: str | None = None
        self._prev_cook_state: str | None = None
        # Latches so the connectivity diagnosis is logged on transition only,
        # not once per poll.
        self._logged_offline: bool = False
        self._logged_frozen: bool = False
        self._halftime_fired: bool = False
        self._probe_target_fired: dict[int, bool] = {0: False, 1: False}
        self._probe_halfway_fired: dict[int, bool] = {0: False, 1: False}

    async def _async_update_data(self) -> CombinedState:
        try:
            state = await self.reader.read_state(self.dsn)
        except (AuthError, AwsAuthError) as err:
            raise UpdateFailed(f"auth: {err}") from err
        except (TransportError, AwsTransportError) as err:
            raise UpdateFailed(f"transport: {err}") from err

        self._diagnose_liveness(state)

        active = (
            state.is_live(int(STATE_MAX_AGE.total_seconds()))
            and (
                state.grill.state in ACTIVE_STATES
                or state.cook.state in ACTIVE_STATES
            )
        )
        new_interval = SCAN_INTERVAL_ACTIVE if active else SCAN_INTERVAL_IDLE
        if self.update_interval != new_interval:
            self.update_interval = new_interval
            _LOGGER.debug(
                "ninja_woodfire %s: scan interval -> %s (state=%s, cook=%s)",
                self.dsn, new_interval, state.grill.state, state.cook.state,
            )

        self._emit_lifecycle_events(state)
        return state

    def _diagnose_liveness(self, state: CombinedState) -> None:
        """Log *why* there is no live data, once per transition.

        This grill is a cloud-reporting outlier: the Wi-Fi module pushes a
        state snapshot when it connects and then goes quiet, serving live
        state only to LAN/BLE clients. The cloud keeps returning that one
        snapshot forever, so without this the integration looks like it is
        working — it reports a plausible "idle" grill that is actually
        mid-cook. See README § Known limitations.
        """
        age = state.state_age_seconds()
        age_txt = "unknown" if age is None else f"{age / 60:.0f} min"

        if not state.online:
            if not self._logged_offline:
                _LOGGER.warning(
                    "ninja_woodfire %s [%s]: grill is not connected to the "
                    "cloud (connection_status=%s); its last report is %s old. "
                    "Entities are unavailable rather than showing that stale "
                    "snapshot as current state.",
                    self.dsn, self.backend, state.connection_status, age_txt,
                )
                self._logged_offline = True
            self._logged_frozen = False
            return
        self._logged_offline = False

        if state.is_stale(int(STATE_MAX_AGE.total_seconds())):
            if not self._logged_frozen:
                _LOGGER.warning(
                    "ninja_woodfire %s [%s]: grill reports connected but has "
                    "not sent an update for %s; the backend is serving a "
                    "cached snapshot (state=%s), so entities are unavailable. "
                    "On the Ayla backend this usually means the grill has been "
                    "migrated to SharkNinja's AWS service and no longer "
                    "reports there at all — see README.",
                    self.dsn, self.backend, age_txt, state.grill.state,
                )
                self._logged_frozen = True
            return
        self._logged_frozen = False

    @property
    def state_is_live(self) -> bool:
        """Whether the last snapshot describes the grill right now.

        Entities that mirror grill state gate their availability on this.
        """
        return self.data is not None and self.data.is_live(
            int(STATE_MAX_AGE.total_seconds())
        )

    def _event_identity(self) -> dict[str, Any]:
        """Who a lifecycle event is about, in terms an automation can use.

        The DSN alone is a poor handle. A blueprint keyed on it has to be
        configured with a serial the user must go and dig out of a diagnostic
        sensor, and the notification it sends says "ac000w000000000 is done".

        The device id gives a blueprint a real device picker; the name gives
        the message something to call the grill. Both are read from the
        registry on every emit rather than cached, so renaming the device in
        the UI takes effect immediately — `name_by_user` is that rename.

        The device is created by the first entity, which is added after the
        first refresh, so the earliest polls may find nothing. The keys are
        always present; they are None until the device exists.
        """
        device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, self.dsn)}
        )
        return {
            "dsn": self.dsn,
            "device_id": device.id if device else None,
            "device_name": (
                (device.name_by_user or device.name) if device else None
            ),
        }

    def _emit_lifecycle_events(self, state: CombinedState) -> None:
        """Fire HA events on cook-lifecycle transitions.

        Called once per poll. Compares the new state against the previous
        snapshot and fires an event for each transition we care about.
        Automations subscribe via `event_type` (see const.EVENT_*).
        """
        # A stale or offline snapshot is not evidence of a transition. Without
        # this guard, a grill dropping off the cloud mid-cook would leave the
        # last-seen state frozen and then fire a bogus "cook done" the moment
        # it reconnected showing "idle".
        if not state.is_live(int(STATE_MAX_AGE.total_seconds())):
            self._prev_grill_state = None
            self._prev_cook_state = None
            return

        prev_grill = self._prev_grill_state
        prev_cook = self._prev_cook_state
        new_grill = state.grill.state
        new_cook = state.cook.state

        identity = self._event_identity()
        common = {
            **identity,
            "mode": state.grill.mode,
            "setpoint": state.grill.setpoint,
        }

        # Cook started: any transition idle/unknown -> active state.
        if (
            prev_grill is not None
            and prev_grill not in ACTIVE_STATES
            and new_grill in ACTIVE_STATES
        ):
            self._halftime_fired = False
            self._probe_target_fired = {0: False, 1: False}
            self._probe_halfway_fired = {0: False, 1: False}
            self.hass.bus.async_fire(
                EVENT_COOK_STARTED,
                {
                    **common,
                    "seconds_set": state.grill.seconds_set,
                    "smoke": state.grill.smoke,
                },
            )

        # Preheat complete: leaving "preheat" while staying active.
        # The grill state moves preheat -> cooking; cook.state may move
        # preheat -> heat / cooking. Trigger on either transition.
        preheat_done_grill = prev_grill == "preheat" and new_grill != "preheat"
        preheat_done_cook = prev_cook == "preheat" and new_cook != "preheat"
        if (preheat_done_grill or preheat_done_cook) and new_grill in ACTIVE_STATES:
            self.hass.bus.async_fire(EVENT_PREHEAT_COMPLETE, common)

        # Halftime: fired once per cook when seconds_left crosses 50% of
        # seconds_set. Reset on cook start (above) and on done/idle.
        if (
            not self._halftime_fired
            and new_grill in ACTIVE_STATES
            and state.grill.seconds_set
            and state.grill.seconds_left is not None
            and state.grill.seconds_set > 0
            and state.grill.seconds_left <= state.grill.seconds_set / 2
        ):
            self._halftime_fired = True
            self.hass.bus.async_fire(
                EVENT_COOK_HALFTIME,
                {
                    **common,
                    "seconds_left": state.grill.seconds_left,
                    "seconds_set": state.grill.seconds_set,
                },
            )

        # Cook done: any transition into "done", or active -> idle.
        was_active = prev_grill in ACTIVE_STATES if prev_grill else False
        became_done = new_grill == "done" and prev_grill != "done"
        became_idle_from_active = was_active and new_grill == "idle"
        if became_done or became_idle_from_active:
            self.hass.bus.async_fire(
                EVENT_COOK_DONE,
                {
                    **common,
                    "reason": "done" if became_done else "stopped",
                },
            )
            self._halftime_fired = False
            self._probe_target_fired = {0: False, 1: False}
            self._probe_halfway_fired = {0: False, 1: False}

        # Probe target reached: per-probe one-shot when current temp
        # crosses the manual setpoint while the probe is active. Fires
        # only during an active cook (otherwise stale post-cook readings
        # would trigger it).
        if new_grill in ACTIVE_STATES:
            for idx, probe in enumerate(state.probes.probes[:2]):
                if not probe.active:
                    continue
                target = probe.target.setpoint
                if target is None:
                    continue

                # Probe halfway: fire once when reaching 50% of target temp
                if not self._probe_halfway_fired.get(idx):
                    halfway = target / 2
                    if probe.temp >= halfway:
                        self._probe_halfway_fired[idx] = True
                        self.hass.bus.async_fire(
                            EVENT_PROBE_HALFWAY,
                            {
                                **identity,
                                "probe_index": idx,
                                "target": target,
                                "halfway": halfway,
                                "current": probe.temp,
                            },
                        )

                # Probe target reached
                if self._probe_target_fired.get(idx):
                    continue
                if probe.temp >= target:
                    self._probe_target_fired[idx] = True
                    self.hass.bus.async_fire(
                        EVENT_PROBE_TARGET_REACHED,
                        {
                            **identity,
                            "probe_index": idx,
                            "target": target,
                            "current": probe.temp,
                        },
                    )

        self._prev_grill_state = new_grill
        self._prev_cook_state = new_cook

    async def async_set_property(self, name: str, value) -> None:
        """Write a settable cloud property."""
        await self.client.set_property_datapoint(self.dsn, name, value)
        await self._burst_refresh()

    async def _burst_refresh(self) -> None:
        """Poll a few times rapidly after a write so the UI catches up."""
        import asyncio
        await self.async_request_refresh()
        await asyncio.sleep(1.0)
        await self.async_request_refresh()
        await asyncio.sleep(1.5)
        await self.async_request_refresh()

    def _build_probe_payload(self, setpoint: int) -> dict[str, Any]:
        return {"mode": "manual", "setpoint": int(setpoint)}

    async def async_start_cook(self) -> None:
        """Send the configured cook settings to the grill."""
        mode = self.capabilities.get_mode(self.cook_setting_mode)
        if mode is None:
            raise ValueError(
                f"mode {self.cook_setting_mode!r} not supported by "
                f"{self.capabilities.display_name}"
            )
        # A locked mode takes smoke whatever the staged setting says — it is
        # not a choice there, and sending 0 would compose a cook the appliance
        # does not offer.
        smoke = (mode.smoke_locked or self.cook_setting_smoke) and mode.supports_smoke
        probe_0 = (
            self._build_probe_payload(self.cook_setting_probe0_setpoint)
            if self.cook_setting_probe0_enabled and mode.supports_probe
            else None
        )
        probe_1 = (
            self._build_probe_payload(self.cook_setting_probe1_setpoint)
            if self.cook_setting_probe1_enabled
            and mode.supports_probe
            and self.capabilities.has_two_probes
            else None
        )
        temp = max(mode.temp_min, min(mode.temp_max, self.cook_setting_temp))
        seconds = max(
            mode.duration_min_s,
            min(mode.duration_max_s, self.cook_setting_seconds),
        )
        await self.commander.start_cook(
            self.dsn,
            mode=self.cook_setting_mode,
            seconds=seconds,
            temp=temp,
            smoke=smoke,
            skip_preheat=self.cook_setting_skip_preheat,
            probe_0=probe_0,
            probe_1=probe_1,
            device_key=self._device_key,
        )
        await self._burst_refresh()

    async def async_stop_cook(self) -> None:
        await self.commander.stop_cook(self.dsn)
        await self._burst_refresh()

    async def async_skip_preheat(self) -> None:
        """Skip preheat by re-issuing current settings with skip_preheat=True.

        Critical: when re-issuing mid-cook we must send the *remaining*
        time, not the original cook duration. Otherwise the firmware
        treats this as a fresh cook and resets the timer to seconds_set.
        Use the live `seconds_left` (or recompute from end_time_utc)
        from the current snapshot. Falls back to the configured
        cook_setting_seconds only if no active cook state is available.
        """
        live = self.data
        seconds = self.cook_setting_seconds
        mode = self.cook_setting_mode
        temp = self.cook_setting_temp
        smoke = self.cook_setting_smoke
        if live is not None and live.grill.state in ACTIVE_STATES:
            # Prefer end_time-derived remaining (immune to poll latency)
            # over the snapshot's seconds_left field.
            if live.grill.end_time_utc:
                from datetime import datetime, timezone
                remaining = int(
                    live.grill.end_time_utc
                    - datetime.now(tz=timezone.utc).timestamp()
                )
                if remaining > 0:
                    seconds = remaining
            elif live.grill.seconds_left:
                seconds = live.grill.seconds_left
            # Mirror everything else from the live cook so we don't
            # accidentally change mode/temp/smoke when the user just
            # wanted to skip preheat.
            if live.grill.mode:
                mode = live.grill.mode
            if live.grill.setpoint is not None:
                temp = live.grill.setpoint
            smoke = bool(live.grill.smoke)

        await self.commander.skip_preheat(
            self.dsn,
            mode=mode,
            seconds=seconds,
            temp=temp,
            smoke=smoke,
            device_key=self._device_key,
        )
        await self._burst_refresh()

    async def async_set_grill_name(self, new_name: str) -> None:
        await self.client.set_grill_name(self.dsn, new_name)
        await self._burst_refresh()

    # ------------------------------------------------------------------
    # Live-state accessors
    #
    # While a cook is active, HA entities should reflect what the grill
    # is *actually doing*, not what's staged for the next start press.
    # While idle, they fall back to the staged values so the user can
    # configure their next cook.
    # ------------------------------------------------------------------

    @property
    def is_cook_active(self) -> bool:
        """Whether a cook is running *right now*.

        Requires a live snapshot: a cached "cooking" state from a grill that
        has since gone offline must not make the live_or_staged_* accessors
        report a phantom cook.
        """
        return self.state_is_live and self.data.grill.state in ACTIVE_STATES

    @property
    def live_or_staged_mode(self) -> str:
        if self.is_cook_active and self.data.grill.mode:
            return self.data.grill.mode
        return self.cook_setting_mode

    @property
    def live_or_staged_temp(self) -> int:
        if self.is_cook_active and self.data.grill.setpoint is not None:
            return int(self.data.grill.setpoint)
        return self.cook_setting_temp

    @property
    def live_or_staged_seconds(self) -> int:
        """Original cook duration. Live: from grill.seconds_set. Staged: from setting."""
        if self.is_cook_active and self.data.grill.seconds_set:
            return int(self.data.grill.seconds_set)
        return self.cook_setting_seconds

    @property
    def live_or_staged_smoke(self) -> bool:
        if self.is_cook_active:
            return bool(self.data.grill.smoke)
        return self.cook_setting_smoke

    async def async_modify_cook(
        self,
        *,
        mode: str | None = None,
        temp: int | None = None,
        seconds: int | None = None,
        smoke: bool | None = None,
    ) -> None:
        """Apply a single-field change.

        Idle: just updates the staged setting (next Start press picks it up).
        Active: re-issues the cook command preserving all other live
        values + remaining time + current phase. Preserving the phase
        is critical — naively re-issuing during preheat would either
        reset the preheat counter or skip preheat entirely.
        """
        live = self.data
        active = self.is_cook_active and live is not None

        # Always update the staged setting first so subsequent reads
        # (and the next Start press) see the new value.
        if mode is not None:
            self.cook_setting_mode = mode
        if temp is not None:
            self.cook_setting_temp = temp
        if seconds is not None:
            self.cook_setting_seconds = seconds
        if smoke is not None:
            self.cook_setting_smoke = smoke

        if not active:
            return

        # Active cook with no actual delta requested: nothing to do.
        if mode is None and temp is None and seconds is None and smoke is None:
            return

        # Skip the live re-issue while preheating — every cook command
        # the firmware accepts during preheat either resets the preheat
        # ramp or jumps straight to the cook phase. Stage-only is the
        # only safe behavior. Tell HA to refresh so the entity reads
        # bounce back to the live values; user's change is staged for
        # the next press / phase transition.
        if (
            live.grill.state in ("preheat", "preheating")
            or live.cook.state in ("preheat", "preheating")
        ):
            _LOGGER.debug(
                "modify_cook skipped during preheat: only staged "
                "(mode=%s temp=%s seconds=%s smoke=%s)",
                mode, temp, seconds, smoke,
            )
            await self._burst_refresh()
            return

        # Active cook past preheat: build a re-issue payload from live
        # state, then patch the requested field. Preserve remaining time.
        from datetime import datetime, timezone
        cur_mode = live.grill.mode or self.cook_setting_mode
        cur_temp = (
            live.grill.setpoint if live.grill.setpoint is not None
            else self.cook_setting_temp
        )
        cur_smoke = bool(live.grill.smoke)
        if live.grill.end_time_utc:
            cur_seconds = max(
                1,
                int(live.grill.end_time_utc - datetime.now(tz=timezone.utc).timestamp()),
            )
        elif live.grill.seconds_left:
            cur_seconds = live.grill.seconds_left
        else:
            cur_seconds = self.cook_setting_seconds

        new_mode = mode if mode is not None else cur_mode
        new_temp = temp if temp is not None else cur_temp
        new_smoke = smoke if smoke is not None else cur_smoke
        new_seconds = seconds if seconds is not None else cur_seconds

        # Sanity: clamp the proposal to what the new mode supports
        # before we send it. Otherwise the firmware would either
        # silently snap (e.g. smoke=1 in dehydrate) or reject the
        # whole command. Better to do it explicitly here.
        new_mode_caps = self.capabilities.get_mode(new_mode)
        if new_mode_caps is not None:
            if new_temp < new_mode_caps.temp_min or new_temp > new_mode_caps.temp_max:
                new_temp = new_mode_caps.temp_default
            if not new_mode_caps.supports_smoke and new_smoke:
                new_smoke = False
            elif new_mode_caps.smoke_locked and not new_smoke:
                new_smoke = True
            if new_seconds > new_mode_caps.duration_max_s:
                new_seconds = new_mode_caps.duration_max_s

        await self.commander.start_cook(
            self.dsn,
            mode=new_mode,
            seconds=new_seconds,
            temp=int(new_temp),
            smoke=new_smoke,
            skip_preheat=True,
            device_key=self._device_key,
        )
        # Mirror the (possibly clamped) values into the staged state
        # so subsequent reads agree with what we sent.
        self.cook_setting_mode = new_mode
        self.cook_setting_temp = int(new_temp)
        self.cook_setting_seconds = int(new_seconds)
        self.cook_setting_smoke = new_smoke
        await self._burst_refresh()
