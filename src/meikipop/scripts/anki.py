# meikipop/scripts/anki.py
import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from meikipop.config.config import config

import requests

logger = logging.getLogger(__name__)

DEFAULT_ANKI_CONNECT_URL = "http://127.0.0.1:8765"
ANKI_CONNECT_VERSION = 6


class AnkiConnectError(Exception):
    """Custom exception for AnkiConnect-related errors."""
    pass


class AnkiConnect:
    def __init__(self, url: Optional[str] = None):
        # defaults to config first, then falls back to constructor argument
        if url is None:
            c_anki_ip = config.anki_connect_ip
            c_anki_port = config.anki_connect_port
            if c_anki_ip and c_anki_port:
                url = f"http://{c_anki_ip}:{c_anki_port}"
        self.url = url

    def _invoke(self, action: str, **params) -> Any:
        """Invoke an action on AnkiConnect with the given parameters."""
        payload = {"action": action, "version": ANKI_CONNECT_VERSION, "params": params}
        try:
            response = requests.post(self.url, json=payload, timeout=5)
            response.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise AnkiConnectError("Could not connect to Anki. Make sure Anki is running with the AnkiConnect add-on installed.")
        except requests.exceptions.Timeout:
            raise AnkiConnectError("Connection to Anki timed out.")
        except requests.exceptions.RequestException as e:
            raise AnkiConnectError(f"Request failed: {e}")

        result = response.json()
        if result.get("error"):
            raise AnkiConnectError(f"AnkiConnect: {result['error']}")
        return result["result"]

    def test_connection(self) -> bool:
        """Test if AnkiConnect is reachable and responding."""
        try:
            self._invoke("version")
            return True
        except AnkiConnectError:
            return False

    def get_deck_names(self) -> List[str]:
        """Get a list of all deck names in the Anki collection."""
        return sorted(self._invoke("deckNames"))

    def get_model_names(self) -> List[str]:
        """Get a list of all model names in the Anki collection."""
        return sorted(self._invoke("modelNames"))

    def get_model_field_names(self, model_name: str) -> List[str]:
        """Get a list of field names for a given model."""
        return self._invoke("modelFieldNames", modelName=model_name)

    def store_media_file(self, filename: str, file_path: str) -> str:
        """Store a media file in Anki's collection. Returns the stored filename."""
        path = Path(file_path)
        if not path.exists():
            raise AnkiConnectError(f"File not found: {file_path}")
        data = base64.b64encode(path.read_bytes()).decode("utf-8")
        return self._invoke("storeMediaFile", filename=filename, data=data)

    def add_note(
        self,
        deck_name: str,
        model_name: str,
        fields: Dict[str, str],
        tags: Optional[List[str]] = None,
        screenshot_path: Optional[str] = None,
        screenshot_field: Optional[str] = None,
    ) -> int:
        """
        Add a note to Anki. Returns the new note ID.

        If screenshot_path and screenshot_field are provided, the image is stored
        in Anki's media collection and embedded in the specified field.
        """
        note: Dict[str, Any] = {
            "deckName": deck_name,
            "modelName": model_name,
            "fields": dict(fields),
            "tags": tags or [],
            "options": {"allowDuplicate": True},
        }

        if screenshot_path and screenshot_field:
            filename = f"meikipop_{Path(screenshot_path).name}"
            note["picture"] = [
                {
                    "path": screenshot_path,
                    "filename": filename,
                    "fields": [screenshot_field],
                }
            ]

        return self._invoke("addNote", note=note)
