"""Konfiguration aus .env (Projektordner) bzw. Umgebungsvariablen."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    poll_sekunden: int
    standard_modus: str
    fallback_regel: str
    backfill_stunden: int
    ueberlappung_minuten: int
    # Bestandsmonitor
    bestand_aktiv: bool = True
    bestand_sekunden: int = 120
    bestand_tage: int = 90
    bestand_scope_tage: int = 365
    pseudo_arbeitsplaetze: frozenset = frozenset({"10000167", "10000168"})

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            poll_sekunden=_int("MES_POLL_SEKUNDEN", 30),
            standard_modus=os.environ.get("MES_STANDARD_MODUS", "teil").strip().lower(),
            fallback_regel=os.environ.get("MES_FALLBACK_REGEL", "arbeitsplatz").strip().lower(),
            backfill_stunden=_int("MES_BACKFILL_STUNDEN", 12),
            ueberlappung_minuten=_int("MES_UEBERLAPPUNG_MINUTEN", 10),
            bestand_aktiv=os.environ.get("MES_BESTAND_AKTIV", "1").strip() not in ("0", "false", "nein", ""),
            bestand_sekunden=_int("MES_BESTAND_SEKUNDEN", 120),
            bestand_tage=_int("MES_BESTAND_TAGE", 90),
            bestand_scope_tage=_int("MES_BESTAND_SCOPE_TAGE", 365),
            pseudo_arbeitsplaetze=frozenset(
                x.strip() for x in os.environ.get("MES_PSEUDO_ARBEITSPLAETZE", "10000167,10000168").split(",")
                if x.strip()),
        )
        if s.standard_modus not in ("teil", "voll"):
            raise ValueError(f"MES_STANDARD_MODUS muss teil|voll sein, ist {s.standard_modus!r}")
        if s.fallback_regel not in ("arbeitsplatz", "alle"):
            raise ValueError(f"MES_FALLBACK_REGEL muss arbeitsplatz|alle sein, ist {s.fallback_regel!r}")
        return s
