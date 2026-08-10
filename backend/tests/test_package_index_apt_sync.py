# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2024-present repod contributors
# See LICENSE for terms. Commercial use: LICENSE-COMMERCIAL.md
"""
Module : test_package_index_apt_sync.py
Rôle   : services/package_index_apt.py:sync_source() — seule
         urllib.error.URLError persistait un échec dans sync_status ; toute
         autre exception (échec de vérification d'intégrité SHA256 via
         InRelease, échec de décompression/parsing) était renvoyée à
         l'appelant (donc bien visible dans le flux de logs du job) mais
         jamais écrite en base. Conséquence concrète : une source touchée
         par ce type d'erreur restait affichée "jamais synchronisée" dans
         l'UI (GET /import/sync-status), indéfiniment, même après plusieurs
         tentatives — get_sync_status() synthétise status="never" pour
         toute source absente de sync_status, ce qui est indiscernable
         d'une source qui n'a simplement encore jamais été synchronisée.

         Ces tests couvrent le comportement corrigé : _write_sync_error()
         est maintenant appelée pour TOUT type d'exception, pas seulement
         URLError.

Dépend : pytest, unittest.mock.patch, db_test_engine (fixture conftest.py,
         SQLite in-memory, autouse).
"""
import hashlib
import urllib.error
from unittest.mock import MagicMock, patch


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source(source_id="ubuntu-jammy"):
    return {
        "id": source_id,
        "label": "Ubuntu 22.04 (Jammy) main",
        "url": "https://archive.ubuntu.com/ubuntu/dists/jammy/main/binary-amd64/Packages.gz",
        "distro": "jammy",
        "component": "main",
        "arch": "amd64",
    }


def _sync_status_row(source_id):
    from sqlalchemy import text

    from db.engine import db_conn
    with db_conn() as conn:
        row = conn.execute(
            text("SELECT * FROM sync_status WHERE source_id = :sid"),
            {"sid": source_id},
        ).mappings().fetchone()
    return dict(row) if row else None


class TestSyncSourcePersistsEveryFailureType:

    def test_url_error_persists_status_error(self, db_test_engine):
        """Comportement déjà correct avant le correctif — non-régression.
        Patch time.sleep : sync_source() retente désormais 2 fois sur une
        URLError (services/http_retry.py) avant d'abandonner — sans ce
        patch, ce test attendrait réellement 2s+5s pour rien.

        L'URLError frappe ici sur InRelease, désormais téléchargée en premier
        (avant Packages.gz) pour connaître l'empreinte d'index sans payer le
        gros téléchargement."""
        import services.package_index_apt as pia

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connexion refusée")), \
             patch("services.http_retry.time.sleep"):
            result = pia.sync_source(_source())

        assert result["status"] == "error"
        row = _sync_status_row("ubuntu-jammy")
        assert row is not None, "aucune trace persistée pour une URLError"
        assert row["status"] == "error"
        assert "connexion refusée" in row["error"]

    def test_integrity_check_failure_persists_status_error(self, db_test_engine):
        """C'est le bug corrigé : un échec d'authentification de l'index lève
        un ValueError, capturé par la branche générique `except Exception` —
        avant le correctif, rien n'était écrit."""
        import services.package_index_apt as pia

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"contenu falsifie"
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(pia, "_authenticated_packages_sha256",
                           return_value=(None, "SHA256 invalide — possible attaque MitM")):
            result = pia.sync_source(_source())

        assert result["status"] == "error"
        assert "SHA256 invalide" in result["error"]

        row = _sync_status_row("ubuntu-jammy")
        assert row is not None, (
            "échec de vérification d'intégrité non persisté — la source "
            "resterait affichée 'jamais synchronisée' indéfiniment"
        )
        assert row["status"] == "error"
        assert "SHA256 invalide" in row["error"]

    def test_decompression_failure_persists_status_error(self, db_test_engine):
        """Même bug, autre déclencheur : _parse_packages_gz() qui échoue
        (ex. Packages.gz corrompu) doit aussi être persisté."""
        import services.package_index_apt as pia

        payload = b"pas du gzip valide"
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(pia, "_authenticated_packages_sha256",
                           return_value=(_sha256(payload), "ok")), \
             patch.object(pia, "_parse_packages_gz", side_effect=ValueError("Impossible de décompresser")):
            result = pia.sync_source(_source())

        assert result["status"] == "error"
        row = _sync_status_row("ubuntu-jammy")
        assert row is not None
        assert row["status"] == "error"
        assert "décompresser" in row["error"]

    def test_first_ever_attempt_failing_is_distinguishable_from_never_synced(self, db_test_engine):
        """Le symptôme observé en production : après le correctif, une
        source dont la toute première tentative échoue doit apparaître dans
        get_sync_status() avec status='error', pas 'never' — sinon elle est
        indiscernable d'une source jamais synchronisée."""
        import services.package_index_apt as pia

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")), \
             patch("services.http_retry.time.sleep"), \
             patch.object(pia, "DEFAULT_SOURCES", [_source("ubuntu-jammy")]):
            pia.sync_source(_source())
            statuses = pia.get_sync_status()

        entry = next(s for s in statuses if s["source_id"] == "ubuntu-jammy")
        assert entry["status"] == "error"
        assert entry["status"] != "never"

    def test_success_path_still_persists_status_ok(self, db_test_engine):
        """Non-régression du chemin nominal (inchangé par le correctif)."""
        import services.package_index_apt as pia

        payload = b"contenu"
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(pia, "_authenticated_packages_sha256",
                           return_value=(_sha256(payload), "ok")), \
             patch.object(pia, "_parse_packages_gz", return_value=[]):
            result = pia.sync_source(_source())

        assert result["status"] == "ok"
        row = _sync_status_row("ubuntu-jammy")
        assert row["status"] == "ok"
        assert row["error"] is None

    def test_tampered_packages_gz_is_rejected(self, db_test_engine):
        """Le SHA256 authentifié via InRelease doit toujours être confronté aux
        octets réellement reçus. Le skip d'index inchangé ne doit pas devenir
        une porte dérobée : un Packages.gz qui ne correspond pas au hash
        annoncé fait échouer la sync, comme avant."""
        import services.package_index_apt as pia

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"charge utile falsifiee"
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(pia, "_authenticated_packages_sha256",
                           return_value=(_sha256(b"le vrai contenu"), "ok")), \
             patch.object(pia, "_parse_packages_gz", return_value=[]) as mock_parse:
            result = pia.sync_source(_source())

        assert result["status"] == "error"
        assert "MitM" in result["error"]
        mock_parse.assert_not_called(), "le contenu falsifié n'aurait jamais dû être parsé"

    def test_write_sync_error_itself_never_raises(self, db_test_engine):
        """_write_sync_error() est appelée depuis un bloc except — si la
        persistance elle-même échoue (ex. DB indisponible), elle ne doit
        jamais faire remonter une nouvelle exception par-dessus l'erreur
        d'origine qu'on est justement en train de rapporter."""
        import services.package_index_apt as pia

        with patch.object(pia, "db_conn", side_effect=RuntimeError("DB indisponible")):
            pia._write_sync_error("ubuntu-jammy", "Ubuntu 22.04 (Jammy) main", "erreur d'origine")
        # Aucune exception levée = test réussi.
