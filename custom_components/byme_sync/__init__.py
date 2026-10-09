"""By-me Sync.

Allinea in Home Assistant lo stato dei carichi Vimar By-me che non hanno un
indirizzo di stato (relè dei comandi a muro, interfacce contatti), leggendolo
direttamente dal dispositivo come fa il web server Vimar.

Interroga:
  1. all'avvio di Home Assistant, quando il collegamento KNX è pronto;
  2. a ogni riconnessione del collegamento KNX;
  3. con l'azione byme_sync.aggiorna.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

import voluptuous as vol
from xknx.core.connection_state import XknxConnectionState
from xknx.telegram import IndividualAddress

from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import (
    CoreState,
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .reader import ByMeReader, ByMeReadError, start_index

_LOGGER = logging.getLogger(__name__)

DOMAIN = "byme_sync"
KNX_DOMAIN = "knx"

CONF_LOADS = "carichi"
CONF_ENTITY = "entita"
CONF_ADDRESS = "indirizzo"
CONF_BLOCK = "blocco"
CONF_DELAY = "ritardo"
CONF_ALIGN = "allinea"
CONF_TIMEOUT = "timeout"
CONF_RETRIES = "tentativi"

SERVICE_REFRESH = "aggiorna"
SERVICE_READ = "leggi"

# ogni quanto controllare se l'integrazione KNX è stata ricaricata
WATCH_INTERVAL = timedelta(seconds=30)


def _individual_address(value: Any) -> str:
    try:
        return str(IndividualAddress(str(value)))
    except Exception as exc:  # noqa: BLE001
        raise vol.Invalid(f"indirizzo fisico non valido: {value}") from exc


BLOCK = vol.All(vol.Coerce(int), vol.Range(min=1, max=16))

LOAD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENTITY): cv.entity_domain(["switch", "light"]),
        vol.Required(CONF_ADDRESS): _individual_address,
        vol.Required(CONF_BLOCK): BLOCK,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_DELAY, default=10): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=600)
                ),
                vol.Optional(CONF_ALIGN, default=True): cv.boolean,
                vol.Optional(CONF_TIMEOUT, default=2.0): vol.All(
                    vol.Coerce(float), vol.Range(min=0.5, max=10)
                ),
                vol.Optional(CONF_RETRIES, default=2): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=5)
                ),
                vol.Required(CONF_LOADS): vol.All(cv.ensure_list, [LOAD_SCHEMA]),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

REFRESH_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTITY_ID): cv.entity_ids})
READ_SCHEMA = vol.Schema(
    {vol.Required(CONF_ADDRESS): _individual_address, vol.Required(CONF_BLOCK): BLOCK}
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Avvia By-me Sync dalla configurazione YAML."""
    manager = ByMeSync(hass, config[DOMAIN])
    hass.data[DOMAIN] = manager

    async def handle_refresh(call: ServiceCall) -> ServiceResponse:
        result = await manager.async_sync("manuale", call.data.get(ATTR_ENTITY_ID))
        return result if call.return_response else None

    async def handle_read(call: ServiceCall) -> ServiceResponse:
        address = call.data[CONF_ADDRESS]
        block = call.data[CONF_BLOCK]
        reader = manager.reader
        if reader is None:
            raise HomeAssistantError("Integrazione KNX non pronta")
        try:
            value = await reader.read(address, block)
        except ByMeReadError as exc:
            raise HomeAssistantError(str(exc)) from exc
        return {
            "indirizzo": address,
            "blocco": block,
            "canale": start_index(block),
            "valore": value,
            "stato": STATE_ON if value else STATE_OFF,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH,
        handle_refresh,
        schema=REFRESH_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_READ,
        handle_read,
        schema=READ_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    if hass.state is CoreState.running:
        manager.start()
    else:

        @callback
        def _started(_event: Event) -> None:
            manager.start()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _started)

    @callback
    def _stop(_event: Event) -> None:
        manager.stop()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)
    return True


class ByMeSync:
    """Segue il collegamento KNX e allinea i carichi configurati."""

    def __init__(self, hass: HomeAssistant, conf: dict[str, Any]) -> None:
        self.hass = hass
        self.loads: list[dict[str, Any]] = conf[CONF_LOADS]
        self.delay: float = conf[CONF_DELAY]
        self.align: bool = conf[CONF_ALIGN]
        self.timeout: float = conf[CONF_TIMEOUT]
        self.retries: int = conf[CONF_RETRIES]
        self.reader: ByMeReader | None = None
        self._xknx: Any = None
        self._unsub_conn: Any = None
        self._unsub_watch: Any = None
        self._pending: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # --- collegamento all'integrazione KNX -------------------------------

    @callback
    def start(self) -> None:
        self._attach()
        self._unsub_watch = async_track_time_interval(
            self.hass, self._watch, WATCH_INTERVAL
        )

    @callback
    def stop(self) -> None:
        if self._unsub_watch:
            self._unsub_watch()
            self._unsub_watch = None
        self._detach()

    @callback
    def _watch(self, _now: Any) -> None:
        self._attach()

    def _current_xknx(self) -> Any:
        module = self.hass.data.get(KNX_DOMAIN)
        return getattr(module, "xknx", None)

    @callback
    def _attach(self) -> None:
        """Si aggancia all'istanza xknx attuale (cambia se l'integrazione KNX viene ricaricata)."""
        xknx = self._current_xknx()
        if xknx is self._xknx:
            return
        self._detach()
        if xknx is None:
            _LOGGER.debug("Integrazione KNX non ancora caricata")
            return
        self._xknx = xknx
        self.reader = ByMeReader(xknx, self.timeout, self.retries)
        self.reader.install()
        self._unsub_conn = xknx.connection_manager.register_connection_state_changed_cb(
            self._connection_changed
        )
        _LOGGER.debug("Agganciato al collegamento KNX")
        if xknx.connection_manager.state is XknxConnectionState.CONNECTED:
            self._schedule("avvio")

    @callback
    def _detach(self) -> None:
        self._cancel_pending()
        if self._unsub_conn:
            self._unsub_conn()
            self._unsub_conn = None
        if self.reader:
            self.reader.uninstall()
            self.reader = None
        self._xknx = None

    @callback
    def _connection_changed(self, state: XknxConnectionState) -> None:
        if state is XknxConnectionState.CONNECTED:
            self._schedule("riconnessione")
        else:
            self._cancel_pending()

    @callback
    def _schedule(self, reason: str) -> None:
        self._cancel_pending()
        self._pending = self.hass.async_create_background_task(
            self._delayed_sync(reason), f"{DOMAIN} {reason}"
        )

    @callback
    def _cancel_pending(self) -> None:
        if self._pending and not self._pending.done():
            self._pending.cancel()
        self._pending = None

    async def _delayed_sync(self, reason: str) -> None:
        await asyncio.sleep(self.delay)
        await self.async_sync(reason)

    # --- allineamento -----------------------------------------------------

    async def async_sync(
        self, reason: str, only: list[str] | None = None
    ) -> dict[str, Any]:
        """Legge i carichi configurati e corregge le entità diverse dallo stato reale."""
        if self.reader is None:
            self._attach()
        reader = self.reader
        if reader is None or not reader.connected:
            _LOGGER.warning("Allineamento (%s) saltato: collegamento KNX non attivo", reason)
            return {"motivo": reason, "eseguito": False, "carichi": []}

        loads = [l for l in self.loads if not only or l[CONF_ENTITY] in only]
        results: list[dict[str, Any]] = []
        fixed = errors = 0

        async with self._lock:
            for load in loads:
                entity_id = load[CONF_ENTITY]
                address = load[CONF_ADDRESS]
                block = load[CONF_BLOCK]
                item: dict[str, Any] = {
                    "entita": entity_id,
                    "indirizzo": address,
                    "blocco": block,
                }
                try:
                    value = await reader.read(address, block)
                except ByMeReadError as exc:
                    errors += 1
                    item["errore"] = str(exc)
                    _LOGGER.warning("%s (%s FB%s): %s", entity_id, address, block, exc)
                    results.append(item)
                    continue

                real = STATE_ON if value else STATE_OFF
                item["stato_reale"] = real
                state = self.hass.states.get(entity_id)
                item["stato_ha"] = state.state if state else None

                if state is None:
                    errors += 1
                    item["errore"] = "entità non trovata"
                    _LOGGER.warning("%s non esiste in Home Assistant", entity_id)
                elif state.state != real:
                    item["corretto"] = self.align
                    if self.align:
                        fixed += 1
                        domain = entity_id.split(".", 1)[0]
                        service = "turn_on" if value else "turn_off"
                        _LOGGER.info(
                            "%s: Home Assistant diceva %s, il relè è %s: allineo",
                            entity_id, state.state, real,
                        )
                        try:
                            await self.hass.services.async_call(
                                domain, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
                            )
                        except Exception as exc:  # noqa: BLE001
                            errors += 1
                            item["errore"] = f"allineamento non riuscito: {exc}"
                            _LOGGER.warning("%s: allineamento non riuscito: %s", entity_id, exc)
                    else:
                        _LOGGER.info(
                            "%s: Home Assistant dice %s, il relè è %s (allinea: false)",
                            entity_id, state.state, real,
                        )
                results.append(item)

        _LOGGER.info(
            "Allineamento (%s): %s carichi letti, %s corretti, %s errori",
            reason, len(loads) - sum(1 for r in results if "stato_reale" not in r), fixed, errors,
        )
        return {
            "motivo": reason,
            "eseguito": True,
            "corretti": fixed,
            "errori": errors,
            "carichi": results,
        }
