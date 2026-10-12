"""OpenAI v1-compatible HTTP client."""

import time

import requests
from rich.markup import escape
from rich.table import Table

from . import console as con
from . import menu
from .config import ConfigError

MODEL_PAGE_SIZE = 15
MAX_FALLBACK_ATTEMPTS = 10
MANUAL_ENTRY_LABEL = "✎ Enter model ID manually…"
_MANUAL = object()  # sentinel: the picker's manual-entry row was chosen


class ApiError(Exception):
    def __init__(self, message: str, status: int = None):
        super().__init__(message)
        self.status = status


class ApiClient:
    def __init__(self):
        self._no_json_format_models = set()

    def _headers(self, cfg) -> dict:
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        return headers

    def list_models(self, cfg) -> list[dict]:
        """Query GET /v1/models and return normalized model records."""
        url = f"{cfg.base_url}/models"
        try:
            resp = requests.get(url, headers=self._headers(cfg), timeout=min(cfg.timeout, 30))
        except requests.RequestException as e:
            raise ApiError(f"Could not connect to {url}: {e}")

        if resp.status_code != 200:
            raise ApiError(f"GET /models returned HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)

        try:
            payload = resp.json()
        except ValueError:
            raise ApiError("Endpoint returned invalid non-JSON output for /models")

        raw_data = payload.get("data") if isinstance(payload, dict) else None
        if raw_data is None and isinstance(payload, dict):
            raw_data = payload.get("models")
        if raw_data is None and isinstance(payload, list):
            raw_data = payload

        if not isinstance(raw_data, list):
            raise ApiError("Unexpected JSON structure returned from /models")

        models = []
        for item in raw_data:
            if isinstance(item, str):
                models.append({"id": item, "owned_by": ""})
            elif isinstance(item, dict) and item.get("id"):
                models.append({"id": str(item["id"]), "owned_by": str(item.get("owned_by", ""))})

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for m in models:
            if m["id"] not in seen:
                seen.add(m["id"])
                unique.append(m)
        return unique

    # ------------------------------------------------------------------
    # Model listing & selection
    # ------------------------------------------------------------------

    def print_models(self, cfg) -> bool:
        """Print the endpoint's models as a table without asking for a choice."""
        models = self._fetch_models(cfg)
        if not models:
            return False
        con.console.print(self._model_table(models, 0, f"Available Models ({len(models)})"))
        return True

    def select_model(self, cfg, allow_manual: bool = True) -> bool:
        """Pick a model with the arrow-key menu (numbered list when there is no terminal)."""
        models = self._fetch_models(cfg)
        if not models:
            return self._prompt_manual_model(cfg) if allow_manual else False
        try:
            choice = self._pick_model(cfg, models, allow_manual)
        except menu.MenuUnavailable as e:
            con.dim(f"Arrow-key menu unavailable ({escape(str(e))}); using numbered list.")
            return self._select_model_fallback(cfg, models)
        if choice is None:
            con.warn("Model selection cancelled.")
            return False
        if choice is _MANUAL:
            return self._prompt_manual_model(cfg)
        return self._apply_model(cfg, choice)

    def _fetch_models(self, cfg) -> list[dict]:
        try:
            models = self.list_models(cfg)
        except ApiError as e:
            con.warn(f"Could not retrieve model list from {escape(cfg.base_url)}: {escape(str(e))}")
            return []
        if not models:
            con.warn(f"No models found on {escape(cfg.base_url)}")
        return models

    def _pick_model(self, cfg, models: list[dict], allow_manual: bool):
        """Returns a model id, the _MANUAL sentinel, or None when cancelled."""
        options = [m["id"] for m in models]
        hints = [m["owned_by"] for m in models]
        if allow_manual:
            options.append(MANUAL_ENTRY_LABEL)
            hints.append("")
        current = cfg.model
        model_ids = options[:len(models)]
        default = model_ids.index(current) if current and current in model_ids else 0
        title = f"Select model ({len(models)} available on {cfg.base_url})"
        idx = menu.select_option(title, options, hints, default)
        if idx is None:
            return None
        return _MANUAL if idx >= len(models) else models[idx]["id"]

    def _select_model_fallback(self, cfg, models: list[dict]) -> bool:
        """Numbered, paginated picker used when the arrow-key menu cannot run."""
        total_pages = (len(models) + MODEL_PAGE_SIZE - 1) // MODEL_PAGE_SIZE
        page = 0
        for _ in range(total_pages + MAX_FALLBACK_ATTEMPTS):
            start = page * MODEL_PAGE_SIZE
            title = f"Available Models (Page {page + 1}/{total_pages})"
            con.rule("Model Selection")
            con.console.print(self._model_table(models[start:start + MODEL_PAGE_SIZE], start, title))
            is_last = page >= total_pages - 1
            extra = "or type a model ID" if is_last else "'m' for more"
            try:
                ans = con.console.input(f"[bold]Select model # ({extra}, 'q' to cancel): [/bold]").strip()
            except (KeyboardInterrupt, EOFError):
                return False
            if ans.lower() in ("q", "quit"):
                return False
            if ans.lower() == "m" and not is_last:
                page += 1
                continue
            chosen = self._resolve_model_answer(models, ans)
            if chosen:
                return self._apply_model(cfg, chosen)
            con.warn("Invalid selection, please try again.")
        return False

    @staticmethod
    def _resolve_model_answer(models: list[dict], ans: str) -> str | None:
        if not ans:
            return None
        if not ans.isdigit():
            return ans
        idx = int(ans) - 1
        return models[idx]["id"] if 0 <= idx < len(models) else None

    @staticmethod
    def _model_table(models: list[dict], start: int, title: str) -> Table:
        table = Table(title=title)
        table.add_column("#", justify="right", style="cyan", no_wrap=True)
        table.add_column("Model ID", style="green")
        table.add_column("Owned By", style="dim")
        for idx, m in enumerate(models, start=start + 1):
            table.add_row(str(idx), escape(m["id"]), escape(m["owned_by"]))
        return table

    @staticmethod
    def _apply_model(cfg, model_id: str) -> bool:
        try:
            cfg.model = model_id
        except ConfigError as e:
            con.fail(str(e))
            return False
        con.ok(f"Selected model: [bold cyan]{escape(cfg.model)}[/bold cyan]")
        return True

    def _prompt_manual_model(self, cfg) -> bool:
        try:
            ans = con.console.input("[bold]Enter model ID manually (blank to cancel): [/bold]").strip()
        except (KeyboardInterrupt, EOFError):
            return False
        return self._apply_model(cfg, ans) if ans else False

    # ------------------------------------------------------------------
    # Chat completions
    # ------------------------------------------------------------------

    def chat(self, cfg, messages: list[dict], max_tokens: int | None = None) -> tuple[str, dict | None]:
        """Send chat completion request with exponential backoff.

        max_tokens limits this call only; when cfg.max_tokens is also set, the smaller one wins.
        """
        if not cfg.model:
            raise ApiError("No model specified. Use the model command to configure one.")

        payload = {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature,
        }
        limits = [n for n in (cfg.max_tokens, max_tokens) if isinstance(n, int) and not isinstance(n, bool) and n > 0]
        if limits:
            payload["max_tokens"] = min(limits)

        wants_json = cfg.json_mode == "on" or (
            cfg.json_mode == "auto" and cfg.model not in self._no_json_format_models
        )
        if wants_json:
            payload["response_format"] = {"type": "json_object"}

        url = f"{cfg.base_url}/chat/completions"
        attempts = max(1, cfg.retries + 1)
        last_err = None

        for attempt in range(attempts):
            try:
                resp = requests.post(url, headers=self._headers(cfg), json=payload, timeout=cfg.timeout)
            except requests.Timeout:
                last_err = ApiError(f"Request timed out after {cfg.timeout}s")
            except requests.RequestException as e:
                last_err = ApiError(f"HTTP connection failed: {e}")
            else:
                if resp.status_code == 200:
                    try:
                        res_json = resp.json()
                        choice = res_json["choices"][0]
                        content = choice["message"]["content"]
                        usage = res_json.get("usage")
                        return content, usage
                    except (KeyError, IndexError, ValueError) as err:
                        raise ApiError(f"Invalid completion schema in response: {err}")

                body_sample = resp.text[:300]
                # Fallback for models not supporting response_format in auto mode
                if resp.status_code in (400, 422) and "response_format" in body_sample and cfg.json_mode == "auto":
                    self._no_json_format_models.add(cfg.model)
                    payload.pop("response_format", None)
                    con.warn(f"Model '{cfg.model}' rejected response_format, retrying without it...")
                    continue

                if resp.status_code in (401, 403):
                    raise ApiError(f"Authentication failed (HTTP {resp.status_code}). Check your API key.", resp.status_code)

                if resp.status_code == 429:
                    retry_after = 5
                    if "Retry-After" in resp.headers:
                        try:
                            retry_after = int(resp.headers["Retry-After"])
                        except ValueError:
                            pass
                    con.warn(f"Rate limited (HTTP 429). Retrying in {retry_after}s...")
                    time.sleep(retry_after)
                    continue

                last_err = ApiError(f"API returned HTTP {resp.status_code}: {body_sample}", resp.status_code)

            if attempt < attempts - 1:
                backoff = min(30, 2 ** attempt)
                con.warn(f"Attempt {attempt + 1} failed ({last_err}). Backing off {backoff}s...")
                time.sleep(backoff)

        raise last_err or ApiError("All API completion attempts failed.")
