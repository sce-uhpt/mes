"""Transportregel: braucht der Uebergang Von -> Nach einen Stapler?

Ziel (Fertigungssteuerung, in Klaerung): Die Produktion wird in Sektoren aufgeteilt,
ein Transport entsteht bei Sektorwechsel. Sektoren werden in sce_mes.arbeitsplatz.sektor
gepflegt.

Solange fuer einen der beiden Arbeitsplaetze kein Sektor gepflegt ist, gilt die
Fallback-Regel aus MES_FALLBACK_REGEL:
- 'arbeitsplatz': Transport, wenn sich der Arbeitsplatz (PPS_WORK_CNTR) aendert
- 'alle':         jeder Uebergang ist ein Transport
"""


def _leer(x) -> bool:
    return x is None or (isinstance(x, float) and x != x) or str(x).strip() == ""


def transport_noetig(von_work_cntr, nach_work_cntr, von_sektor=None, nach_sektor=None,
                     fallback_regel: str = "arbeitsplatz") -> bool:
    if not _leer(von_sektor) and not _leer(nach_sektor):
        return str(von_sektor).strip() != str(nach_sektor).strip()
    if fallback_regel == "alle":
        return True
    if _leer(von_work_cntr) or _leer(nach_work_cntr):
        return True  # im Zweifel anzeigen statt verschlucken
    return str(von_work_cntr).strip() != str(nach_work_cntr).strip()
