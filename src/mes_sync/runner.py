"""Ein Poll-Durchlauf und die Dauerschleife."""

import logging
import time
from datetime import timedelta

import pandas as pd

from . import proxia, state
from .config import Settings
from .transport import build_transporte, finde_auto_erledigt

log = logging.getLogger("mes_sync")


def run_once(settings: Settings, proxia_engine, sce_engine) -> dict:
    jetzt = state.utcnow()

    with sce_engine.connect() as con:
        wz = state.get_wasserzeichen(con)
        start = wz or (jetzt - timedelta(hours=settings.backfill_stunden))
        seit = start - timedelta(minutes=settings.ueberlappung_minuten)
        offene = state.offene_transporte(con)

    t_proxia = time.monotonic()
    events = proxia.fetch_events(proxia_engine, seit)

    with sce_engine.connect() as con:
        neu = state.neue_events(con, events)

    ausloeser_orders = set(neu.loc[neu["rueck_type_id"].isin(("C_TFRTG", "C_FRTG")), "pps_order"].dropna())
    orders = ausloeser_orders | set(offene["pps_order"].dropna())
    vorgaenge = proxia.fetch_vorgaenge(proxia_engine, orders) if orders else pd.DataFrame()
    proxia_ms = int((time.monotonic() - t_proxia) * 1000)

    with sce_engine.begin() as con:  # eine Transaktion: alles oder nichts
        state.speichere_vorgaenge(con, vorgaenge, jetzt)
        n_ap = state.ergaenze_arbeitsplaetze(con, vorgaenge, jetzt)

        von_ids = neu["wt_id"].unique() if not neu.empty else []
        ergebnis = build_transporte(
            neu, vorgaenge,
            state.bisherige_transporte(con, von_ids),
            state.lade_arbeitsplatz(con),
            settings.standard_modus, settings.fallback_regel,
        )
        n_tr = state.speichere_transporte(con, ergebnis.transporte, jetzt)
        state.speichere_events(con, neu, jetzt)  # erst NACH den Transporten: nichts geht verloren

        offene = state.offene_transporte(con)
        nach_ids = offene["nach_wt_id"].dropna().unique()
        status_nach = (vorgaenge[vorgaenge["wt_id"].isin(nach_ids)][["wt_id", "wt_status_id"]]
                       if not vorgaenge.empty else None)
        treffer = finde_auto_erledigt(offene, state.events_an_vorgaengen(con, nach_ids), status_nach)
        n_auto = state.auto_erledigen(con, treffer, jetzt)

        neues_wz = max([x for x in (wz, events["rueck_ts"].max() if not events.empty else None)
                        if x is not None and not pd.isna(x)], default=start)
        stat = {**ergebnis.statistik, "events_gelesen": len(events), "events_neu": len(neu),
                "neue_arbeitsplaetze": n_ap, "transporte_neu": n_tr, "auto_erledigt": n_auto,
                "proxia_ms": proxia_ms}
        meldung = ", ".join(f"{k}={v}" for k, v in stat.items())
        state.heartbeat(con, jetzt, True, meldung, pd.Timestamp(neues_wz).to_pydatetime(), n_tr)

    return stat


def run_forever(settings: Settings, proxia_engine, sce_engine) -> None:
    log.info("Starte Poller: alle %ss, Modus=%s, Fallback=%s",
             settings.poll_sekunden, settings.standard_modus, settings.fallback_regel)
    while True:
        t0 = time.monotonic()
        try:
            stat = run_once(settings, proxia_engine, sce_engine)
            if stat.get("transporte_neu") or stat.get("auto_erledigt"):
                log.info("%s", stat)
            else:
                log.debug("%s", stat)
        except Exception as exc:  # noqa: BLE001 — Schleife darf nicht sterben
            log.exception("Lauf fehlgeschlagen")
            try:
                with sce_engine.begin() as con:
                    state.heartbeat(con, state.utcnow(), False, f"FEHLER: {exc}")
            except Exception:  # noqa: BLE001
                log.exception("Heartbeat konnte nicht geschrieben werden")
        time.sleep(max(1.0, settings.poll_sekunden - (time.monotonic() - t0)))
