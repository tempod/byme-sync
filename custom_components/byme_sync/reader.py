"""Lettura dello stato dei canali By-me tramite PropertyValueRead senza connessione.

È la stessa interrogazione che fa il web server Vimar al riavvio:
proprietà 202 dell'oggetto 0, un elemento, a partire dall'indice
(blocco - 1) * 4 + 1. Il dispositivo risponde con 0x00 (spento) o 0x01 (acceso).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from xknx.telegram import IndividualAddress, Telegram
from xknx.telegram.apci import PropertyValueRead, PropertyValueResponse
from xknx.telegram.tpci import TDataIndividual

_LOGGER = logging.getLogger(__name__)

OBJECT_INDEX = 0
PROPERTY_ID = 202


def start_index(block: int) -> int:
    """Indice della proprietà per il blocco funzionale (FB1=1, FB2=5, FB3=9, FB4=13)."""
    return (block - 1) * 4 + 1


class ByMeReadError(Exception):
    """Lettura non riuscita."""


class _ManagementTap:
    """Si mette davanti a xknx.management per intercettare le risposte senza connessione.

    xknx inoltra a `management.process()` i telegrammi indirizzati al nostro
    indirizzo fisico. Le risposte senza connessione (T_Data_Individual) lì
    vengono scartate, quindi le prendiamo noi e passiamo tutto il resto
    all'oggetto originale.
    """

    def __init__(self, inner: Any, reader: ByMeReader) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_reader", reader)

    def process(self, telegram: Telegram) -> None:
        try:
            if self._reader.handle(telegram):
                return
        except Exception:  # noqa: BLE001 - non bloccare mai la gestione di xknx
            _LOGGER.exception("Errore nella gestione di %s", telegram)
        self._inner.process(telegram)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)


class ByMeReader:
    """Invia PropertyValueRead e aspetta la risposta del dispositivo."""

    def __init__(self, xknx: Any, timeout: float, retries: int) -> None:
        self.xknx = xknx
        self.timeout = timeout
        self.retries = retries
        self._waiters: dict[tuple[str, int, int, int], asyncio.Future[PropertyValueResponse]] = {}
        self._lock = asyncio.Lock()
        self._tap: _ManagementTap | None = None

    def install(self) -> None:
        """Aggancia il ricevitore a xknx."""
        if self._tap is not None:
            return
        self._tap = _ManagementTap(self.xknx.management, self)
        self.xknx.management = self._tap

    def uninstall(self) -> None:
        """Rimette xknx com'era."""
        if self._tap is None:
            return
        if self.xknx.management is self._tap:
            self.xknx.management = self._tap._inner  # noqa: SLF001
        self._tap = None
        for fut in self._waiters.values():
            if not fut.done():
                fut.cancel()
        self._waiters.clear()

    @property
    def connected(self) -> bool:
        return self.xknx.connection_manager.connected.is_set()

    def handle(self, telegram: Telegram) -> bool:
        """Restituisce True se il telegramma era una risposta attesa."""
        payload = telegram.payload
        if not isinstance(telegram.tpci, TDataIndividual) or not isinstance(
            payload, PropertyValueResponse
        ):
            return False
        key = (
            str(telegram.source_address),
            payload.object_index,
            payload.property_id,
            payload.start_index,
        )
        fut = self._waiters.get(key)
        if fut is None or fut.done():
            return False
        fut.set_result(payload)
        return True

    async def read(self, address: str, block: int) -> int:
        """Legge lo stato del canale. Restituisce il byte ricevuto (0 = spento)."""
        if not self.connected:
            raise ByMeReadError("collegamento KNX non attivo")
        ia = IndividualAddress(address)
        index = start_index(block)
        key = (str(ia), OBJECT_INDEX, PROPERTY_ID, index)
        telegram = Telegram(
            destination_address=ia,
            tpci=TDataIndividual(),
            payload=PropertyValueRead(
                object_index=OBJECT_INDEX,
                property_id=PROPERTY_ID,
                count=1,
                start_index=index,
            ),
        )
        async with self._lock:
            last_error = "nessuna risposta"
            for attempt in range(self.retries + 1):
                fut: asyncio.Future[PropertyValueResponse] = (
                    asyncio.get_running_loop().create_future()
                )
                self._waiters[key] = fut
                try:
                    await self.xknx.cemi_handler.send_telegram(telegram)
                    async with asyncio.timeout(self.timeout):
                        response = await fut
                except TimeoutError:
                    last_error = f"nessuna risposta entro {self.timeout} s"
                    _LOGGER.debug("%s FB%s: tentativo %s senza risposta", ia, block, attempt + 1)
                    continue
                except Exception as exc:  # noqa: BLE001
                    last_error = f"invio non riuscito: {exc}"
                    _LOGGER.debug("%s FB%s: tentativo %s fallito: %s", ia, block, attempt + 1, exc)
                    await asyncio.sleep(0.3)
                    continue
                finally:
                    self._waiters.pop(key, None)

                if response.count == 0 or not response.data:
                    raise ByMeReadError(
                        f"il dispositivo {ia} non ha la proprietà per il blocco {block}"
                    )
                return response.data[0]
        raise ByMeReadError(f"{ia} FB{block}: {last_error}")
