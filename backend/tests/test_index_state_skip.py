"""
Module : test_index_state_skip.py
Rôle   : services/index_state.py + le saut de sync des sources dont l'index
         amont n'a pas bougé (package_index_apt/rpm/apk).

         Chaque cycle de sync re-téléchargeait et re-parsait les 120 sources
         intégralement, y compris celles inchangées depuis des semaines —
         mesuré à ~18 min pour un run complet, dont ~17 min pour les seules
         58 sources RPM. L'empreinte ingérée est désormais mémorisée et
         comparée à celle publiée en amont (SHA256 de Packages via InRelease
         signée, SHA-256 de primary.xml via repomd.xml, SHA-256 de
         l'APKINDEX.tar.gz), ce qui permet de sauter le gros du travail.

         Ces tests verrouillent les garde-fous qui rendent ce saut sûr :
           - jamais de skip sans empreinte mémorisée
           - jamais de skip si la table a été vidée par ailleurs
           - jamais d'empreinte laissée derrière une ingestion interrompue
           - force=True ignore le mécanisme

Dépend : pytest, unittest.mock.patch, db_test_engine (fixture conftest.py,
         SQLite in-memory).
"""
import hashlib
from unittest.mock import MagicMock, patch


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _apt_source(source_id="ubuntu-jammy"):
    return {
        "id": source_id,
        "label": "Ubuntu 22.04 (Jammy) main",
        "url": "https://archive.ubuntu.com/ubuntu/dists/jammy/main/binary-amd64/Packages.gz",
        "distro": "jammy",
        "component": "main",
        "arch": "amd64",
    }


def _rpm_source(source_id="fedora42"):
    return {
        "id": source_id,
        "label": "Fedora 42",
        "repomd_url": "https://example.invalid/fedora/42/repodata/repomd.xml",
        "distro": "fedora42",
        "arch": "x86_64",
    }


def _insert_package(source_id):
    from sqlalchemy import text

    from db.engine import db_conn
    with db_conn() as conn:
        conn.execute(text("""
            INSERT INTO packages (source_id, name, version, arch, synced_at)
            VALUES (:sid, 'curl', '8.0.0', 'amd64', '2026-01-01T00:00:00+00:00')
        """), {"sid": source_id})


class TestIndexState:

    def test_unknown_source_is_never_unchanged(self, db_test_engine):
        from services import index_state
        assert index_state.is_unchanged("jamais-vue", "abc123") is False

    def test_remember_then_unchanged(self, db_test_engine):
        from services import index_state
        index_state.remember("ubuntu-jammy", "abc123")
        assert index_state.is_unchanged("ubuntu-jammy", "abc123") is True
        assert index_state.is_unchanged("ubuntu-jammy", "autre") is False

    def test_empty_upstream_fingerprint_never_matches(self, db_test_engine):
        """Une source amont qui ne publie aucun hash exploitable ne doit
        jamais être considérée comme inchangée — sinon deux sources sans
        empreinte se ressembleraient et l'index ne serait plus jamais
        rafraîchi."""
        from services import index_state
        index_state.remember("ubuntu-jammy", "abc123")
        assert index_state.is_unchanged("ubuntu-jammy", None) is False
        assert index_state.is_unchanged("ubuntu-jammy", "") is False

    def test_remember_ignores_empty_fingerprint(self, db_test_engine):
        from services import index_state
        index_state.remember("ubuntu-jammy", None)
        assert index_state.fingerprint_of("ubuntu-jammy") is None

    def test_clear_forces_resync(self, db_test_engine):
        from services import index_state
        index_state.remember("ubuntu-jammy", "abc123")
        index_state.clear("ubuntu-jammy")
        assert index_state.is_unchanged("ubuntu-jammy", "abc123") is False

    def test_missing_table_degrades_to_no_skip(self, db_test_engine):
        """Migration non jouée ou DB indisponible : on ne sait pas si l'index a
        bougé, donc on resynchronise. Jamais d'exception remontée dans le job."""
        from services import index_state
        with patch.object(index_state, "db_conn", side_effect=RuntimeError("no such table")):
            assert index_state.is_unchanged("ubuntu-jammy", "abc123") is False
            index_state.remember("ubuntu-jammy", "abc123")   # ne lève pas
            index_state.clear("ubuntu-jammy")                # ne lève pas


class TestAptSkip:

    def test_unchanged_source_skips_download_and_parse(self, db_test_engine):
        import services.package_index_apt as pia
        from services import index_state

        index_state.remember("ubuntu-jammy", "empreinte-amont")
        _insert_package("ubuntu-jammy")

        with patch.object(pia, "_authenticated_packages_sha256",
                          return_value=("empreinte-amont", "ok")), \
             patch.object(pia, "fetch_url") as mock_fetch, \
             patch.object(pia, "_parse_packages_gz") as mock_parse:
            result = pia.sync_source(_apt_source())

        assert result["status"] == "skipped"
        assert result["pkg_count"] == 1
        mock_fetch.assert_not_called(), "Packages.gz téléchargé alors qu'il est inchangé"
        mock_parse.assert_not_called()

    def test_skip_refreshes_last_sync(self, db_test_engine):
        """Une source sautée reste une source à jour : sans ce rafraîchissement,
        le bandeau de fraîcheur de l'UI la verrait vieillir indéfiniment et
        réclamerait une sync que le backend refuserait de faire travailler."""
        from sqlalchemy import text

        import services.package_index_apt as pia
        from db.engine import db_conn
        from services import index_state

        index_state.remember("ubuntu-jammy", "empreinte-amont")
        _insert_package("ubuntu-jammy")

        with patch.object(pia, "_authenticated_packages_sha256",
                          return_value=("empreinte-amont", "ok")):
            pia.sync_source(_apt_source())

        with db_conn() as conn:
            row = conn.execute(
                text("SELECT last_sync, status, pkg_count FROM sync_status WHERE source_id = 'ubuntu-jammy'")
            ).mappings().fetchone()

        assert row is not None, "une source sautée doit quand même apparaître à jour"
        assert row["status"] == "ok"
        assert row["last_sync"]
        assert row["pkg_count"] == 1

    def test_no_skip_when_table_was_emptied(self, db_test_engine):
        """Empreinte présente mais plus aucun paquet en base (purge manuelle,
        restauration partielle) : il faut réindexer, pas faire confiance à
        l'empreinte."""
        import services.package_index_apt as pia
        from services import index_state

        index_state.remember("ubuntu-jammy", "empreinte-amont")
        # aucun paquet inséré

        payload = b"charge"
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__.return_value = mock_resp

        with patch.object(pia, "_authenticated_packages_sha256",
                          return_value=(_sha256(payload), "ok")), \
             patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(pia, "_parse_packages_gz", return_value=[]) as mock_parse:
            result = pia.sync_source(_apt_source())

        assert result["status"] == "ok"
        mock_parse.assert_called_once()

    def test_force_bypasses_skip(self, db_test_engine):
        import services.package_index_apt as pia
        from services import index_state

        index_state.remember("ubuntu-jammy", "empreinte-amont")
        _insert_package("ubuntu-jammy")

        payload = b"charge"
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__.return_value = mock_resp

        with patch.object(pia, "_authenticated_packages_sha256",
                          return_value=(_sha256(payload), "ok")), \
             patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(pia, "_parse_packages_gz", return_value=[]) as mock_parse:
            result = pia.sync_source(_apt_source(), force=True)

        assert result["status"] == "ok"
        mock_parse.assert_called_once()

    def test_parse_failure_never_records_the_new_fingerprint(self, db_test_engine):
        """Un parsing qui échoue survient AVANT toute écriture (le DELETE et
        les INSERT APT sont dans une seule transaction) : la table reflète
        toujours l'ancien index, donc garder l'ancienne empreinte est correct.

        Ce qui compte, et que ce test verrouille : la NOUVELLE empreinte ne doit
        jamais être mémorisée. Le cycle suivant compare l'amont (nouveau hash) à
        l'ancien et resynchronise."""
        import services.package_index_apt as pia
        from services import index_state

        index_state.remember("ubuntu-jammy", "ancienne-empreinte")

        payload = b"charge"
        nouvelle_empreinte = _sha256(payload)
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__.return_value = mock_resp

        with patch.object(pia, "_authenticated_packages_sha256",
                          return_value=(nouvelle_empreinte, "ok")), \
             patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(pia, "_parse_packages_gz", side_effect=RuntimeError("boum")):
            result = pia.sync_source(_apt_source())

        assert result["status"] == "error"
        assert index_state.fingerprint_of("ubuntu-jammy") != nouvelle_empreinte, (
            "empreinte du nouvel index mémorisée alors qu'il n'a jamais été "
            "ingéré — la source serait sautée au cycle suivant"
        )
        assert index_state.is_unchanged("ubuntu-jammy", nouvelle_empreinte) is False

    def test_write_failure_leaves_no_fingerprint(self, db_test_engine):
        """Échec pendant la réécriture de la table : l'empreinte a été effacée
        juste avant, donc rien ne prétend plus que la base est à jour."""
        import services.package_index_apt as pia
        from services import index_state

        index_state.remember("ubuntu-jammy", "ancienne-empreinte")

        payload = b"charge"
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__.return_value = mock_resp

        with patch.object(pia, "_authenticated_packages_sha256",
                          return_value=(_sha256(payload), "ok")), \
             patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(pia, "_parse_packages_gz", return_value=[]), \
             patch.object(pia, "db_conn", side_effect=RuntimeError("DB indisponible")):
            result = pia.sync_source(_apt_source())

        assert result["status"] == "error"
        assert index_state.fingerprint_of("ubuntu-jammy") is None


class TestRpmSkip:

    def test_unchanged_source_skips_streaming(self, db_test_engine):
        import services.package_index_rpm as pir
        from services import index_state

        index_state.remember("fedora42", "sha-primary")
        _insert_package("fedora42")

        with patch.object(pir, "_fetch_repomd_bytes", return_value=b"<repomd/>"), \
             patch.object(pir, "_verify_repomd_gpg", return_value=(True, "")), \
             patch.object(pir, "_parse_metadata_info",
                          return_value=("https://example.invalid/primary.xml.gz", "sha-primary")), \
             patch.object(pir, "_stream_download_and_parse") as mock_stream:
            result = pir.sync_source(_rpm_source())

        assert result["status"] == "skipped"
        assert result["pkg_count"] == 1
        mock_stream.assert_not_called(), "primary.xml streamé alors qu'il est inchangé"

    def test_changed_source_is_reindexed(self, db_test_engine):
        import services.package_index_rpm as pir
        from services import index_state

        index_state.remember("fedora42", "ancien-sha")
        _insert_package("fedora42")

        with patch.object(pir, "_fetch_repomd_bytes", return_value=b"<repomd/>"), \
             patch.object(pir, "_verify_repomd_gpg", return_value=(True, "")), \
             patch.object(pir, "_parse_metadata_info",
                          return_value=("https://example.invalid/primary.xml.gz", "nouveau-sha")), \
             patch.object(pir, "_stream_download_and_parse", return_value=42) as mock_stream:
            result = pir.sync_source(_rpm_source())

        assert result["status"] == "ok"
        assert result["pkg_count"] == 42
        mock_stream.assert_called_once()
        assert index_state.fingerprint_of("fedora42") == "nouveau-sha"

    def test_failed_stream_leaves_no_fingerprint(self, db_test_engine):
        import services.package_index_rpm as pir
        from services import index_state

        index_state.remember("fedora42", "ancien-sha")

        with patch.object(pir, "_fetch_repomd_bytes", return_value=b"<repomd/>"), \
             patch.object(pir, "_verify_repomd_gpg", return_value=(True, "")), \
             patch.object(pir, "_parse_metadata_info",
                          return_value=("https://example.invalid/primary.xml.gz", "nouveau-sha")), \
             patch.object(pir, "_stream_download_and_parse", return_value=-1):
            result = pir.sync_source(_rpm_source())

        assert result["status"] == "error"
        assert index_state.fingerprint_of("fedora42") is None

    def test_force_bypasses_skip(self, db_test_engine):
        import services.package_index_rpm as pir
        from services import index_state

        index_state.remember("fedora42", "sha-primary")
        _insert_package("fedora42")

        with patch.object(pir, "_fetch_repomd_bytes", return_value=b"<repomd/>"), \
             patch.object(pir, "_verify_repomd_gpg", return_value=(True, "")), \
             patch.object(pir, "_parse_metadata_info",
                          return_value=("https://example.invalid/primary.xml.gz", "sha-primary")), \
             patch.object(pir, "_stream_download_and_parse", return_value=7) as mock_stream:
            result = pir.sync_source(_rpm_source(), force=True)

        assert result["status"] == "ok"
        mock_stream.assert_called_once()
