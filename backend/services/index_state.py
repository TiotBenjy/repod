"""
Empreinte de l'index amont réellement ingéré, par source.

Sert à sauter une source dont le catalogue upstream n'a pas bougé depuis la
dernière ingestion réussie. Avant ça, chaque cycle de sync re-téléchargeait et
re-parsait les 120 sources intégralement, y compris celles inchangées depuis longtemps.

L'empreinte est le SHA-256 que la source publie elle-même et que la chaîne de
confiance authentifie déjà (voir 0011_source_index_state.py). On ne se fie donc
jamais à un simple horodatage ou à un ETag serveur : si l'empreinte est égale,
c'est bit pour bit le même index que celui déjà en base.

Protocole d'écriture (impérativement dans cet ordre) :

    clear(source_id)          # avant de commencer à réécrire la table
    ... DELETE + INSERT ...
    remember(source_id, fp)   # seulement après succès complet

Une ingestion interrompue (crash, annulation, erreur réseau à mi-parcours)
laisse ainsi la source sans empreinte → resynchronisation complète au cycle
suivant, jamais un skip sur une base partiellement écrite.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from db.engine import db_conn

logger = logging.getLogger("index_state")


def fingerprint_of(source_id: str) -> str | None:
    """Empreinte de l'index amont ingéré avec succès en dernier, ou None."""
    try:
        with db_conn() as conn:
            row = conn.execute(
                text("SELECT fingerprint FROM source_index_state WHERE source_id = :sid"),
                {"sid": source_id},
            ).mappings().fetchone()
        return row["fingerprint"] if row else None
    except Exception as exc:
        # Table absente (migration non jouée) ou DB indisponible : on ne sait pas
        # on ne saute rien. Dégradation vers l'ancien comportement.
        logger.debug("[index_state] lecture impossible pour %s : %s", source_id, exc)
        return None


def is_unchanged(source_id: str, fingerprint: str | None) -> bool:
    """
    True si l'index amont est identique à celui déjà ingéré pour cette source.

    Une empreinte amont vide/inconnue retourne toujours False : sans preuve
    que rien n'a changé, on resynchronise.
    """
    if not fingerprint:
        return False
    return fingerprint_of(source_id) == fingerprint


def remember(source_id: str, fingerprint: str | None) -> None:
    """Enregistre l'empreinte ingérée. À n'appeler qu'après un succès complet."""
    if not fingerprint:
        return
    try:
        with db_conn() as conn:
            conn.execute(text("""
                INSERT INTO source_index_state (source_id, fingerprint, updated_at)
                VALUES (:sid, :fp, :ts)
                ON CONFLICT (source_id) DO UPDATE SET
                    fingerprint = EXCLUDED.fingerprint,
                    updated_at  = EXCLUDED.updated_at
            """), {
                "sid": source_id,
                "fp": fingerprint,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as exc:
        # Sans empreinte mémorisée, la source sera resynchronisée au prochain
        # cycle : coûteux mais correct. Jamais bloquant pour la sync en cours.
        logger.warning("[index_state] mémorisation impossible pour %s : %s", source_id, exc)


def clear(source_id: str) -> None:
    """Efface l'empreinte : à appeler avant de commencer à réécrire la table."""
    try:
        with db_conn() as conn:
            conn.execute(
                text("DELETE FROM source_index_state WHERE source_id = :sid"),
                {"sid": source_id},
            )
    except Exception as exc:
        logger.warning("[index_state] effacement impossible pour %s : %s", source_id, exc)
