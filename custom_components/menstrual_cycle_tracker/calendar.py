"""Calendar entity for the Menstrual Cycle Tracker integration."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent, CalendarEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE

SYMPTOM_MAP_DE = {
    "cramps": "Krämpfe",
    "headache": "Kopfschmerzen",
    "fatigue": "Müdigkeit",
    "bloating": "Blähungen",
    "mood_swings": "Stimmungsschwankungen",
    "temperature_sensitivity": "Temperaturempfinden",
}

SEVERITY_MAP_DE = {
    "mild": "Leicht",
    "moderate": "Mittel",
    "severe": "Stark",
    "very_cold": "Viel zu kalt",
    "slightly_cold": "Leicht kühl",
    "normal": "Normal",
    "slightly_warm": "Leicht warm",
    "very_hot": "Viel zu heiß",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up calendar from a config entry."""
    cycle_data = hass.data[DOMAIN][entry.entry_id]
    name = entry.data.get("name", "Cycle Tracker")
    async_add_entities([CycleCalendar(cycle_data, entry, name)])


class CycleCalendar(CalendarEntity):
    """Calendar showing past, current, and predicted periods."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, cycle_data: Any, entry: ConfigEntry, tracker_name: str) -> None:
        """Initialize the calendar entity."""
        self._cycle_data = cycle_data
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_calendar"
        self._attr_name = tracker_name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=tracker_name,
            manufacturer="Custom",
            model="Menstrual Cycle Tracker",
            sw_version="2.0.0",
        )

    async def async_added_to_hass(self) -> None:
        """Register dispatcher."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_UPDATE}_{self._entry.entry_id}",
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        """Handle update signal."""
        self.async_write_ha_state()

    @property
    def supported_features(self) -> CalendarEntityFeature:
        """Return supported features."""
        return CalendarEntityFeature.DELETE_EVENT

    async def async_delete_event(
        self,
        uid: str,
        recurrence_range: str | None = None,
        recurrence_id: str | None = None,
    ) -> None:
        """Delete an event on the calendar."""
        updated = False
        if uid.startswith("period_"):
            start_date_str = uid[7:]
            try:
                start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                if await self._cycle_data.delete_cycle(start_date):
                    updated = True
            except ValueError:
                pass
        elif uid.startswith("symptom_"):
            parts = uid.split("_", 2)
            if len(parts) == 3:
                date_str = parts[1]
                s_name = parts[2]
                try:
                    s_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    if await self._cycle_data.delete_symptom(s_date, s_name):
                        updated = True
                except ValueError:
                    pass
        
        if updated:
            async_dispatcher_send(self.hass, f"{SIGNAL_UPDATE}_{self._entry.entry_id}")
            self.async_write_ha_state()

    @property
    def event(self) -> CalendarEvent | None:
        """Return the current or next upcoming event."""
        today = date.today()
        cd = self._cycle_data
        period_len = cd.average_period_length

        # If a period is currently active, show it
        if cd.is_period_active:
            start = cd.last_period_start
            if start:
                end = start + timedelta(days=period_len)
                if end < today:
                    end = today
                return CalendarEvent(
                    summary="Period (Active)",
                    start=start,
                    end=end + timedelta(days=1),
                    uid=f"period_{start.isoformat()}"
                )

        # Otherwise show the next predicted period
        next_date = cd.next_period_date
        if next_date:
            return CalendarEvent(
                summary="Period (Predicted)",
                start=next_date,
                end=next_date + timedelta(days=period_len),
                uid=f"predicted_{next_date.isoformat()}"
            )

        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return calendar events within a date range."""
        events: list[CalendarEvent] = []
        cd = self._cycle_data
        period_len = cd.average_period_length
        range_start = start_date.date()
        range_end = end_date.date()

        # Past and current periods from logged cycles
        for cycle in cd.cycles:
            c_start_str = cycle.get("start_date")
            if not c_start_str:
                continue
            try:
                c_start = datetime.strptime(c_start_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            c_end_str = cycle.get("end_date")
            if c_end_str:
                try:
                    c_end = datetime.strptime(c_end_str, "%Y-%m-%d").date()
                except ValueError:
                    continue
                summary = "Period"
            else:
                # Active period with no end date yet
                c_end = c_start + timedelta(days=period_len - 1)
                if c_end < date.today():
                    c_end = date.today()
                summary = "Period (Active)"

            # Check if event overlaps with requested range
            # CalendarEvent end is exclusive for all-day events, so add 1 day
            event_end = c_end + timedelta(days=1)
            if c_start < range_end and event_end > range_start:
                events.append(CalendarEvent(
                    summary=summary,
                    start=c_start,
                    end=event_end,
                    uid=f"period_{c_start_str}",
                ))

        # Future predicted periods - repeat forward through the requested range
        cycle_len = cd.average_cycle_length
        next_date = cd.next_period_date
        if next_date:
            # If a period is active, start predictions from the cycle after
            if cd.is_period_active:
                next_date = next_date + timedelta(days=cycle_len)
            while next_date < range_end:
                pred_end = next_date + timedelta(days=period_len)
                if pred_end > range_start:
                    events.append(CalendarEvent(
                        summary="Period (Predicted)",
                        start=next_date,
                        end=pred_end,
                        uid=f"predicted_{next_date.isoformat()}"
                    ))
                next_date = next_date + timedelta(days=cycle_len)

        # Symptoms
        is_de = hass.config.language == "de"
        for symp in cd.symptoms:
            date_str = symp.get("date")
            if not date_str:
                continue
            try:
                s_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            # Event overlaps if s_date is within [range_start, range_end)
            if range_start <= s_date < range_end:
                s_name = symp.get("symptom", "Symptom")
                sev = symp.get("severity")
                if is_de:
                    s_name = SYMPTOM_MAP_DE.get(s_name, s_name)
                    if sev:
                        sev = SEVERITY_MAP_DE.get(sev, sev)

                summary = s_name
                if sev:
                    summary += f" ({sev})"

                events.append(CalendarEvent(
                    summary=summary,
                    start=s_date,
                    end=s_date + timedelta(days=1),
                    uid=f"symptom_{date_str}_{symp.get('symptom', '')}"
                ))

        return events
