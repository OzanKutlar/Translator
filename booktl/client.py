"""OpenAI v1-compatible HTTP client."""

import json
import threading
import time

import requests
from rich.markup import escape
from rich.table import Table

from . import console as con
from . import menu
from .cancel import ApiCancelled, CancelToken
from .config import ConfigError

MODEL_PAGE_SIZE = 15
MAX_FALLBACK_ATTEMPTS = 10
MANUAL_ENTRY_LABEL = "✎ Enter model ID manually…"
_MANUAL = object()  # sentinel: the picker's manual-entry row was chosen
POST_POLL_SECONDS = 0.25
POST_GRACE_SECONDS = 5   # extra wait beyond the request timeout before giving up on the helper thread
CANCEL_MESSAGE = "Request cancelled via console command"
SHORT_ERROR_CHARS = 100     # error text shown in retry lines
ERROR_PARSE_CHARS = 20_000  # most of an error body read when looking for its message


def _clip(text, limit: int = SHORT_ERROR_CHARS) -> str:
    """Text collapsed to one line and cut to at most limit characters."""
    flat = " ".join(str(text or "").split())
    if limit < 2 or len(flat) <= limit:
        return flat
    return flat[:limit - 1].rstrip() + "…"


def _error_message(body) -> str:
    """The human-readable message inside an error body ('error.message' or 'message'), else the body."""
    if not isinstance(body, str) or not body.strip():
        return ""
    try:
        data = json.loads(body)
    except ValueError:
        return body
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str) and err.strip():
            return err
        if data.get("message"):
            return str(data["message"])
    return body


def _short_error(status, body) -> str:
    """'HTTP 503 · The model is overloaded.' instead of the raw JSON body."""
    detail = _clip(_error_message(body))
    return f"HTTP {status} · {detail}" if detail else f"HTTP {status}"


def _labelled(label, text: str) -> str:
    """Prefix a client log line with the caller's label, e.g. 'Batch #7: Term query attempt 1/3 failed'."""
    if label:
        return f"{escape(label)} {text}"
    return text[:1].upper() + text[1:]


def _raise_if_cancelled(cancel) -> None:
    if cancel is not None and cancel.is_cancelled:
        raise ApiCancelled(CANCEL_MESSAGE)


def _sleep(seconds: float, cancel) -> None:
    """Sleep for backoff or rate limits; a cancelled token ends the sleep with ApiCancelled."""
    if cancel is None:
        time.sleep(seconds)
        return
    if cancel.wait(seconds):
        raise ApiCancelled(CANCEL_MESSAGE)


def _post_abortable(url: str, headers: dict, payload: dict, timeout: int, cancel):
    """POST that the caller can abandon when cancel fires.

    requests cannot interrupt a request that is already sent, so the call runs on a daemon
    thread. On cancel the caller stops waiting and the late response is dropped.
    """
    if cancel is None:
        return requests.post(url, headers=headers, json=payload, timeout=timeout)
    outcome = {}
    done = threading.Event()

    def run():
        try:
            outcome["response"] = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except Exception as e:  # handed back to the waiting worker below
            outcome["error"] = e
        finally:
            done.set()

    threading.Thread(target=run, daemon=True, name="booktl-http").start()
    polls = max(1, int((timeout + POST_GRACE_SECONDS) / POST_POLL_SECONDS))
    for _ in range(polls):
        if cancel.is_cancelled:
            raise ApiCancelled("Request abandoned after cancellation")
        if done.wait(POST_POLL_SECONDS):
            break
    else:
        raise requests.Timeout(f"No response within {timeout}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["response"]


class ApiError(Exception):
    """API failure. str() is the full message; summary is a short one-line form for the console."""

    def __init__(self, message: str, status: int = None, summary: str | None = None):
        super().__init__(message)
        self.status = status
        self.summary = summary or _clip(message)


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

    def chat(self, cfg, messages: list[dict], max_tokens: int | None = None,
             cancel: CancelToken | None = None, label: str | None = None) -> tuple[str, dict | None]:
        """Send chat completion request with exponential backoff.

        max_tokens limits this call only; when cfg.max_tokens is also set, the smaller one wins.
        cancel, when set, stops the call (including retries and backoff) with ApiCancelled.
        label, e.g. 'Batch #7: Term query', prefixes every retry and rate-limit log line.
        """
        if cancel is not None and not isinstance(cancel, CancelToken):
            raise TypeError("cancel must be a CancelToken or None")
        if label is not None and not isinstance(label, str):
            raise TypeError("label must be a string or None")
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
            _raise_if_cancelled(cancel)
            try:
                resp = _post_abortable(url, self._headers(cfg), payload, cfg.timeout, cancel)
            except requests.Timeout:
                last_err = ApiError(f"Request timed out after {cfg.timeout}s")
            except requests.RequestException as e:
                last_err = ApiError(f"HTTP connection failed: {e}", summary=_clip(f"connection failed: {e}"))
            else:
                _raise_if_cancelled(cancel)  # a reply that arrives after cancel is dropped
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
                    con.warn(_labelled(label, f"hit a response_format error on '{escape(cfg.model)}' · retrying without it"))
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
                    con.warn(_labelled(label, f"rate limited (HTTP 429) · retrying in {retry_after}s"))
                    _sleep(retry_after, cancel)
                    continue

                last_err = ApiError(
                    f"API returned HTTP {resp.status_code}: {body_sample}", resp.status_code,
                    summary=_short_error(resp.status_code, resp.text[:ERROR_PARSE_CHARS]),
                )

            if attempt < attempts - 1:
                backoff = min(30, 2 ** attempt)
                short = escape(last_err.summary) if last_err else "unknown error"
                con.warn(_labelled(label, f"attempt {attempt + 1}/{attempts} failed: {short} · retrying in {backoff}s"))
                _sleep(backoff, cancel)

        raise last_err or ApiError("All API completion attempts failed.")
