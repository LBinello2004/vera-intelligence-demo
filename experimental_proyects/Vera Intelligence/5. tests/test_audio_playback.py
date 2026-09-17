from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import audio_playback  # noqa: E402


class _FakeCursor:
    def __init__(self, row: tuple | None) -> None:
        self._row = row
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class ResolveRecordingFromPostgresTests(unittest.TestCase):
    """Aislamiento por tenant -mismo criterio que sql_security.py/vector_search.py: nunca confiar
    en un conversation_id ajeno al cliente activo."""

    def test_scopes_query_by_tenant(self) -> None:
        cursor = _FakeCursor(("rid1", "archivo.opus", 120.0))
        connection = _FakeConnection(cursor)
        with patch("audio_playback.get_postgres_connection") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = connection
            result = audio_playback._resolve_recording_from_postgres(
                tenant="Mens Fashion", conversation_id="conv1"
            )
        sql, params = cursor.executed[0]
        self.assertIn("r.seller_id = %s", sql)
        self.assertEqual(params, ("conv1", "Mens Fashion"))
        self.assertEqual(result, {
            "recording_id": "rid1", "filename": "archivo.opus", "duration_seconds": 120.0
        })

    def test_returns_none_when_no_row_found(self) -> None:
        cursor = _FakeCursor(None)
        connection = _FakeConnection(cursor)
        with patch("audio_playback.get_postgres_connection") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = connection
            result = audio_playback._resolve_recording_from_postgres(
                tenant="Mens Fashion", conversation_id="conv-inexistente"
            )
        self.assertIsNone(result)

    def test_returns_none_when_filename_is_missing(self) -> None:
        cursor = _FakeCursor(("rid1", None, 120.0))
        connection = _FakeConnection(cursor)
        with patch("audio_playback.get_postgres_connection") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = connection
            result = audio_playback._resolve_recording_from_postgres(
                tenant="Mens Fashion", conversation_id="conv1"
            )
        self.assertIsNone(result)


class ResolveBucketNameTests(unittest.TestCase):
    """bucketName vacío en Firestore = default de la plataforma, no un dato faltante -confirmado
    en vivo (2026-09-14) probando un archivo real de Mens Fashion en `_DEFAULT_BUCKET`."""

    def test_returns_declared_bucket_when_present(self) -> None:
        doc = MagicMock()
        doc.exists = True
        doc.to_dict.return_value = {"bucketName": "audios-to-analyze-tigo"}
        mock_db = MagicMock()
        mock_db.collection.return_value.document.return_value.get.return_value = doc
        with patch("audio_playback._ensure_firebase_app"), patch(
            "firebase_admin.firestore.client", return_value=mock_db
        ):
            bucket = audio_playback._resolve_bucket_name("rid1")
        self.assertEqual(bucket, "audios-to-analyze-tigo")

    def test_falls_back_to_default_when_field_is_empty(self) -> None:
        doc = MagicMock()
        doc.exists = True
        doc.to_dict.return_value = {"bucketName": None}
        mock_db = MagicMock()
        mock_db.collection.return_value.document.return_value.get.return_value = doc
        with patch("audio_playback._ensure_firebase_app"), patch(
            "firebase_admin.firestore.client", return_value=mock_db
        ):
            bucket = audio_playback._resolve_bucket_name("rid1")
        self.assertEqual(bucket, audio_playback._DEFAULT_BUCKET)

    def test_falls_back_to_default_when_doc_does_not_exist(self) -> None:
        doc = MagicMock()
        doc.exists = False
        mock_db = MagicMock()
        mock_db.collection.return_value.document.return_value.get.return_value = doc
        with patch("audio_playback._ensure_firebase_app"), patch(
            "firebase_admin.firestore.client", return_value=mock_db
        ):
            bucket = audio_playback._resolve_bucket_name("rid1")
        self.assertEqual(bucket, audio_playback._DEFAULT_BUCKET)

    def test_falls_back_to_default_when_firestore_call_raises(self) -> None:
        with patch(
            "audio_playback._ensure_firebase_app", side_effect=RuntimeError("sin credenciales")
        ):
            bucket = audio_playback._resolve_bucket_name("rid1")
        self.assertEqual(bucket, audio_playback._DEFAULT_BUCKET)


class GuessContentTypeTests(unittest.TestCase):
    """Encontrado al probar en vivo (2026-09-14): GCS sirve estos archivos con Content-Type
    "application/octet-stream" real -sin forzar el tipo correcto en la URL firmada, el <audio> del
    navegador a veces no reproduce aunque el archivo esté bien."""

    def test_known_extensions_map_to_audio_mime_types(self) -> None:
        self.assertEqual(audio_playback._guess_content_type("a.opus"), "audio/ogg")
        self.assertEqual(audio_playback._guess_content_type("a.mp4"), "audio/mp4")
        self.assertEqual(audio_playback._guess_content_type("a.wav"), "audio/wav")

    def test_unknown_extension_returns_none(self) -> None:
        self.assertIsNone(audio_playback._guess_content_type("a.xyz"))

    def test_is_case_insensitive(self) -> None:
        self.assertEqual(audio_playback._guess_content_type("A.OPUS"), "audio/ogg")


class ResolveAudioUrlTests(unittest.TestCase):
    """resolve_audio_url nunca lanza -ver el docstring del módulo: es una acción opcional del
    usuario, un fallo en cualquiera de las tres fuentes (Postgres/Firestore/GCS) devuelve None."""

    def test_returns_none_when_recording_not_found_in_postgres(self) -> None:
        with patch("audio_playback._resolve_recording_from_postgres", return_value=None):
            result = audio_playback.resolve_audio_url(tenant="Mens Fashion", conversation_id="c1")
        self.assertIsNone(result)

    def test_returns_none_without_credentials_path(self) -> None:
        with patch(
            "audio_playback._resolve_recording_from_postgres",
            return_value={"recording_id": "rid1", "filename": "a.opus", "duration_seconds": 10.0},
        ), patch.dict("os.environ", {}, clear=True):
            result = audio_playback.resolve_audio_url(tenant="Mens Fashion", conversation_id="c1")
        self.assertIsNone(result)

    def test_returns_none_when_blob_does_not_exist(self) -> None:
        fake_blob = MagicMock()
        fake_blob.exists.return_value = False
        fake_client = MagicMock()
        fake_client.bucket.return_value.blob.return_value = fake_blob
        with patch(
            "audio_playback._resolve_recording_from_postgres",
            return_value={"recording_id": "rid1", "filename": "a.opus", "duration_seconds": 10.0},
        ), patch("audio_playback._resolve_bucket_name", return_value="audios-to-analyze-app2"), patch(
            "google.cloud.storage.Client.from_service_account_json", return_value=fake_client
        ), patch.dict("os.environ", {"CREDENTIALS_PATH": __file__}):
            result = audio_playback.resolve_audio_url(tenant="Mens Fashion", conversation_id="c1")
        self.assertIsNone(result)

    def test_returns_signed_url_when_blob_exists(self) -> None:
        fake_blob = MagicMock()
        fake_blob.exists.return_value = True
        fake_blob.generate_signed_url.return_value = "https://signed.example/audio.opus"
        fake_client = MagicMock()
        fake_client.bucket.return_value.blob.return_value = fake_blob
        with patch(
            "audio_playback._resolve_recording_from_postgres",
            return_value={"recording_id": "rid1", "filename": "a.opus", "duration_seconds": 42.0},
        ), patch("audio_playback._resolve_bucket_name", return_value="audios-to-analyze-app2"), patch(
            "google.cloud.storage.Client.from_service_account_json", return_value=fake_client
        ), patch.dict("os.environ", {"CREDENTIALS_PATH": __file__}):
            result = audio_playback.resolve_audio_url(tenant="Mens Fashion", conversation_id="c1")
        self.assertEqual(result, {"url": "https://signed.example/audio.opus", "duration_seconds": 42.0})
        fake_blob.generate_signed_url.assert_called_once()
        call_kwargs = fake_blob.generate_signed_url.call_args.kwargs
        self.assertEqual(call_kwargs["version"], "v4")
        self.assertEqual(call_kwargs["method"], "GET")
        self.assertEqual(call_kwargs["response_type"], "audio/ogg")  # filename "a.opus"

    def test_never_raises_on_unexpected_exception(self) -> None:
        with patch(
            "audio_playback._resolve_recording_from_postgres", side_effect=RuntimeError("Postgres caído")
        ):
            result = audio_playback.resolve_audio_url(tenant="Mens Fashion", conversation_id="c1")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
