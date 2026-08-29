"""OpenAI v1-compatible HTTP client."""

import time
import requests
from rich.table import Table
from . import console as con


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

    def select_model(self, cfg, allow_manual: bool = True) -> bool:
        """Interactive model picker querying /models with pagination support."""
        try:
            models = self.list_models(cfg)
        except Exception as e:
            con.warn(f"Could not retrieve model list from {cfg.base_url}: {e}")
            if allow_manual:
                ans = con.console.input("[bold]Enter model ID manually: [/bold]").strip()
                if ans:
                    cfg.model = ans
                    con.ok(f"Model set to [bold cyan]{ans}[/bold cyan]")
                    return True
            return False

        if not models:
            con.warn(f"No models found on {cfg.base_url}")
            if allow_manual:
                ans = con.console.input("[bold]Enter model ID manually: [/bold]").strip()
                if ans:
                    cfg.model = ans
                    return True
            return False

        page_size = 15
        total_pages = (len(models) + page_size - 1) // page_size
        current_page = 0

        while current_page < total_pages:
            start_idx = current_page * page_size
            end_idx = min(start_idx + page_size, len(models))
            page_items = models[start_idx:end_idx]

            table = Table(title=f"Available Models (Page {current_page + 1}/{total_pages})")
            table.add_column("#", justify="right", style="cyan", no_wrap=True)
            table.add_column("Model ID", style="green")
            table.add_column("Owned By", style="dim")

            for idx, m in enumerate(page_items, start=start_idx + 1):
                table.add_row(str(idx), m["id"], m["owned_by"])

            con.rule("Model Selection")
            con.console.print(table)

            prompt_text = "[bold]Select model # (or 'm' for more, 'q' to cancel): [/bold]"
            if current_page == total_pages - 1:
                prompt_text = "[bold]Select model # (or type model ID, 'q' to cancel): [/bold]"

            ans = con.console.input(prompt_text).strip()
            if ans.lower() in ("q", "quit"):
                return False
            if ans.lower() == "m" and current_page < total_pages - 1:
                current_page += 1
                continue

            try:
                selected_idx = int(ans) - 1
                if 0 <= selected_idx < len(models):
                    chosen = models[selected_idx]["id"]
                    cfg.model = chosen
                    con.ok(f"Selected model: [bold cyan]{chosen}[/bold cyan]")
                    return True
            except ValueError:
                if ans:
                    cfg.model = ans
                    con.ok(f"Set model: [bold cyan]{ans}[/bold cyan]")
                    return True
            con.warn("Invalid selection, please try again.")

        return False

    def chat(self, cfg, messages: list[dict]) -> tuple[str, dict | None]:
        """Send chat completion request with exponential backoff."""
        if not cfg.model:
            raise ApiError("No model specified. Use /model to configure.")

        payload = {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature,
        }
        if cfg.max_tokens:
            payload["max_tokens"] = cfg.max_tokens

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
