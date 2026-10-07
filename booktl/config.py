"""Thread-safe mutable runtime configuration."""

import os
import threading
from urllib.parse import urlparse

DEFAULT_BASE_URL = "http://localhost:34501/v1"
DEFAULT_LIBRARY = "../TranslateBook"
DEFAULT_BATCH_SIZE = 10
DEFAULT_CONCURRENCY = 1

VALID_JSON_MODES = ("auto", "on", "off")


class ConfigError(ValueError):
    """Raised when configuration validation fails."""


def load_dotenv(path: str = ".env") -> None:
    """Load key-value pairs from .env without overwriting existing environment variables."""
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


def _require_http_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"'{value}' is not a valid http(s) URL")
    return candidate


def _require_int_in_range(value, name: str, min_val: int, max_val: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer")
    if num < min_val or num > max_val:
        raise ConfigError(f"{name} must be between {min_val} and {max_val}")
    return num


class Config:
    """Mutable settings instance protected by a recursive lock."""

    def __init__(self):
        self._lock = threading.RLock()
        self._base_url = DEFAULT_BASE_URL
        self._api_key = ""
        self._model = ""
        self._temperature = 0.3
        self._max_tokens = None
        self._batch_size = DEFAULT_BATCH_SIZE
        self._concurrency = DEFAULT_CONCURRENCY
        self._timeout = 600
        self._retries = 2
        self._json_mode = "auto"
        self._library_dir = DEFAULT_LIBRARY
        self._book_dir = ""
        self._book_name = ""
        self._dry_run = False
        self._save_raw = False
        self._host = "127.0.0.1"
        self._port = 5001

    @property
    def base_url(self) -> str:
        with self._lock:
            return self._base_url

    @base_url.setter
    def base_url(self, val: str):
        clean = _require_http_url(val)
        with self._lock:
            self._base_url = clean

    @property
    def api_key(self) -> str:
        with self._lock:
            return self._api_key

    @api_key.setter
    def api_key(self, val: str):
        clean = val.strip() if val else ""
        if clean.lower() in ("none", "clear", "null"):
            clean = ""
        with self._lock:
            self._api_key = clean

    @property
    def model(self) -> str:
        with self._lock:
            return self._model

    @model.setter
    def model(self, val: str):
        clean = val.strip() if val else ""
        if not clean:
            raise ConfigError("Model name cannot be empty")
        with self._lock:
            self._model = clean

    @property
    def temperature(self) -> float:
        with self._lock:
            return self._temperature

    @temperature.setter
    def temperature(self, val):
        try:
            f = float(val)
        except (TypeError, ValueError):
            raise ConfigError("Temperature must be a float")
        if f < 0.0 or f > 2.0:
            raise ConfigError("Temperature must be between 0.0 and 2.0")
        with self._lock:
            self._temperature = f

    @property
    def max_tokens(self):
        with self._lock:
            return self._max_tokens

    @max_tokens.setter
    def max_tokens(self, val):
        if val is None or str(val).strip().lower() in ("none", "null", "0", ""):
            with self._lock:
                self._max_tokens = None
            return
        num = _require_int_in_range(val, "max_tokens", 1, 1000000)
        with self._lock:
            self._max_tokens = num

    @property
    def batch_size(self) -> int:
        with self._lock:
            return self._batch_size

    @batch_size.setter
    def batch_size(self, val):
        num = _require_int_in_range(val, "batch_size", 1, 100)
        with self._lock:
            self._batch_size = num

    @property
    def concurrency(self) -> int:
        with self._lock:
            return self._concurrency

    @concurrency.setter
    def concurrency(self, val):
        num = _require_int_in_range(val, "concurrency", 1, 16)
        with self._lock:
            self._concurrency = num

    @property
    def timeout(self) -> int:
        with self._lock:
            return self._timeout

    @timeout.setter
    def timeout(self, val):
        num = _require_int_in_range(val, "timeout", 5, 3600)
        with self._lock:
            self._timeout = num

    @property
    def retries(self) -> int:
        with self._lock:
            return self._retries

    @retries.setter
    def retries(self, val):
        num = _require_int_in_range(val, "retries", 0, 10)
        with self._lock:
            self._retries = num

    @property
    def json_mode(self) -> str:
        with self._lock:
            return self._json_mode

    @json_mode.setter
    def json_mode(self, val: str):
        clean = val.strip().lower()
        if clean not in VALID_JSON_MODES:
            raise ConfigError(f"json_mode must be one of {VALID_JSON_MODES}")
        with self._lock:
            self._json_mode = clean

    @property
    def library_dir(self) -> str:
        with self._lock:
            return self._library_dir

    @library_dir.setter
    def library_dir(self, val: str):
        with self._lock:
            self._library_dir = val.strip()

    @property
    def book_dir(self) -> str:
        with self._lock:
            return self._book_dir

    @property
    def book_name(self) -> str:
        with self._lock:
            return self._book_name

    def set_book(self, directory: str, name: str) -> None:
        with self._lock:
            self._book_dir = directory
            self._book_name = name

    @property
    def dry_run(self) -> bool:
        with self._lock:
            return self._dry_run

    @dry_run.setter
    def dry_run(self, val: bool):
        with self._lock:
            self._dry_run = bool(val)

    @property
    def save_raw(self) -> bool:
        with self._lock:
            return self._save_raw

    @save_raw.setter
    def save_raw(self, val: bool):
        with self._lock:
            self._save_raw = bool(val)

    @property
    def host(self) -> str:
        with self._lock:
            return self._host

    @property
    def port(self) -> int:
        with self._lock:
            return self._port

    @classmethod
    def from_args(cls, args) -> "Config":
        load_dotenv()
        cfg = cls()

        base_url = getattr(args, "base_url", None) or os.environ.get("TL_BASE_URL") or DEFAULT_BASE_URL
        cfg.base_url = base_url

        api_key = getattr(args, "api_key", None) or os.environ.get("TL_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        cfg.api_key = api_key

        model = getattr(args, "model", None) or os.environ.get("TL_MODEL") or ""
        if model:
            cfg.model = model

        library = getattr(args, "library", None) or os.environ.get("TL_LIBRARY") or DEFAULT_LIBRARY
        cfg.library_dir = library

        batch_size = getattr(args, "batch_size", None) or os.environ.get("TL_BATCH_SIZE") or DEFAULT_BATCH_SIZE
        cfg.batch_size = batch_size

        concurrency = getattr(args, "concurrency", None) or os.environ.get("TL_CONCURRENCY") or DEFAULT_CONCURRENCY
        cfg.concurrency = concurrency

        timeout = getattr(args, "timeout", None) or os.environ.get("TL_TIMEOUT") or 600
        cfg.timeout = timeout

        retries = getattr(args, "retries", None) or os.environ.get("TL_RETRIES") or 2
        cfg.retries = retries

        json_mode = getattr(args, "json_mode", None) or os.environ.get("TL_JSON_MODE") or "auto"
        cfg.json_mode = json_mode

        temp = getattr(args, "temperature", None) or os.environ.get("TL_TEMPERATURE") or 0.3
        cfg.temperature = temp

        max_tok = getattr(args, "max_tokens", None) or os.environ.get("TL_MAX_TOKENS") or None
        cfg.max_tokens = max_tok

        cfg.dry_run = bool(getattr(args, "dry_run", False))
        cfg.save_raw = bool(getattr(args, "save_raw", False))

        port = getattr(args, "port", None) or os.environ.get("TL_PORT") or 5001
        cfg._port = int(port)
        host = getattr(args, "host", None) or os.environ.get("TL_HOST") or "127.0.0.1"
        cfg._host = str(host)

        return cfg
