# mes — Proxia → SCE (Stapler-Leitsystem / Milk Run)

Poller, der alle 30 s Rückmeldungen aus dem MES **Proxia** liest, daraus
**Transportaufträge** ableitet und in das SCE-Warehouse (Schema `sce_mes`)
schreibt. Die Shiny-App `app_transport` (Repo `shiny`) liest nur `sce_mes`.

```
Proxia (User report, nur lesen) ──► mes_sync (VM, systemd) ──► SCE.sce_mes ◄── app_transport (/transport/)
```

## Datenquellen in Proxia

| Tabelle | Wofür | Hinweise |
|---|---|---|
| `TSF_RUECKMELDUNG` | Auslöser | `RUECK_ID` eindeutig, `RUECK_TS` **in UTC**, `RUECK_TYPE_ID`: `C_START`, `C_TFRTG` (teilfertig), `C_FRTG` (fertig), `C_RUECK`. `WKPL_RES_ID` = Ist-Arbeitsplatz |
| `TSF_WT` | Arbeitsplan | enthält auch fertige Vorgänge (`C_FRTG`) — `VSF_U_UHPT_PLANDATEN_2` blendet die aus, deshalb nicht verwendet |
| `TRS_RES` | Namen | `RES_TYPE_ID`: `C_WKPL`, `C_MACH`, `C_DELIVERER`, **`C_PERS` (Person)** |

**Nie** `TSF_WT_QTY.RES_ID` bzw. `VSF_U_UHPT_RUECK_24STD.DISPLAYNAME` als Standort
verwenden — dort steht bei Handarbeitsplätzen der Werker. Standort = geplanter
Arbeitsplatz `TSF_WT.PLANNED_RES_ID` (+ `PPS_WORK_CNTR`).

`VSF_U_UHPT_RUECK_24STD` ist übrigens keine 24-h-Sicht: `CAL_DAY > GETDATE()-1`
liefert praktisch nur den aktuellen Kalendertag.

Proxia wird mit `READ UNCOMMITTED` gelesen (wie Proxias eigene Views mit `NOLOCK`),
damit der Poller nie Buchungen an den Terminals blockiert.

## Logik

1. Neue Rückmeldungen seit dem Wasserzeichen (−10 min Überlappung, Dubletten über `RUECK_ID` gefiltert).
2. Auslöser: `C_TFRTG` / `C_FRTG`. **Modus je Von-Arbeitsplatz** (`sce_mes.arbeitsplatz.transport_modus`, sonst `MES_STANDARD_MODUS`):
   - `teil`: jede Teil-/Fertigmeldung mit neuer Gutmenge → ein Transport
   - `voll`: nur die Fertigmeldung → genau ein Transport je Vorgang
3. Nachfolger: nächsthöhere `AFO_NR` im Auftrag (`CHAINED_WT_ID` ist nur in 32 von 34k Vorgängen gefüllt; parallele/gesplittete Vorgänge: 0 — Stand 02.10.2026).
4. Transportregel: Sektorwechsel (`sce_mes.arbeitsplatz.sektor`). Solange Sektoren fehlen: `MES_FALLBACK_REGEL=arbeitsplatz` → Transport bei Wechsel des Arbeitsplatzes.
5. Menge = `QTY_CONFIRMED_GUT` − bereits auf frühere Transporte dieses Vorgangs verteilte Menge.
6. **Auto-Erledigung**: Bekommt der Folgevorgang nach dem Auslöser ein `C_START` oder `C_FRTG` (oder ist er komplett fertig), gilt die Ware als angekommen → Status `auto_erledigt`.

Alles in einer Transaktion je Lauf; Rückmeldungen werden erst nach den Transporten
als „gesehen“ gespeichert, dadurch geht bei Fehlern nichts verloren.

## Tabellen (`sce_mes`)

| Tabelle | Inhalt |
|---|---|
| `transport_auftrag` | Transporte, `quelle_key` UNIQUE (`T|<RUECK_ID>` bzw. `V|<WT_ID>`) |
| `transport_event` | Protokoll: erstellt, uebernommen, zurueckgegeben, erledigt, auto_erledigt |
| `pps_rueckmeldung` | gelesene Proxia-Rückmeldungen |
| `pps_vorgang` | Arbeitsplan-Cache der betroffenen Aufträge |
| `arbeitsplatz` | Stammdaten — `sektor`, `transport_modus`, `lagerort_code` werden **gepflegt**, der Poller legt nur neue Zeilen an |
| `fahrer` | Auswahlliste der App |
| `poller_status` | Heartbeat + Wasserzeichen |

Alle Zeitstempel UTC. Definition: `src/mes_sync/schema.py` (DDL zum Nachlesen: `sql/001_sce_mes.sql`).

## Befehle

```
uv sync
uv run python -m mes_sync init-db                 # Schema + Tabellen anlegen (idempotent)
uv run python -m mes_sync --once                  # ein Durchlauf mit Statistik
uv run python -m mes_sync                         # Dauerschleife (systemd)
uv run python -m mes_sync export-arbeitsplaetze   # Excel zur Sektorpflege -> output/
uv run python -m mes_sync ddl                     # sql/001_sce_mes.sql neu erzeugen
uv run pytest                                     # Tests (inkl. End-to-End mit simuliertem Proxia)
```

### Offline-Demo ohne Proxia (simuliert, SQLite)

PowerShell:
```
$env:PROXIA_DB_URL="sqlite:///output/proxia_fake.sqlite"; $env:SCE_DB_URL="sqlite:///output/sce_demo.sqlite"
uv run python -m mes_sync.tools.fake_proxia init output/proxia_fake.sqlite
uv run python -m mes_sync init-db
uv run python -m mes_sync --once
```
Die Shiny-App läuft dagegen mit `MES_TEST_SQLITE=<Pfad>\output\sce_demo.sqlite` (braucht `RSQLite`).

## Stammdaten pflegen

```sql
-- Sektor / Modus für einen Arbeitsplatz setzen
UPDATE sce_mes.arbeitsplatz SET sektor = N'Dreherei', transport_modus = 'voll' WHERE work_cntr = '10000324';
-- Fahrer ergänzen
INSERT INTO sce_mes.fahrer (name, aktiv) VALUES (N'Max Mustermann', 1);
```

## Vor einer Vorführung aufräumen

Beim ersten Start werden `MES_BACKFILL_STUNDEN` Stunden nachgeladen. Alte, nie
quittierte Transporte lassen sich stornieren:

```sql
UPDATE sce_mes.transport_auftrag
SET status = 'storniert', geaendert_am = SYSUTCDATETIME()
WHERE status IN ('offen','uebernommen') AND rueck_ts < DATEADD(HOUR, -4, SYSUTCDATETIME());
```

## Offene Punkte / bewusst nicht enthalten

- Sektoren (Tabelle da, Pflege durch Fertigungssteuerung)
- Entscheidung Teil-/Vollmengen je Arbeitsplatz
- Login (Fahrer wählt sich aus; `?fahrer=Name` in der URL für Tablets)
- Scan / Lagerortbuchung (Basis: `transport_event` + `arbeitsplatz.lagerort_code`)
- Auto-Erledigung ist eine Heuristik — bei Teillosen kann ein `C_START` am Folgevorgang zu einem früheren Los gehören
