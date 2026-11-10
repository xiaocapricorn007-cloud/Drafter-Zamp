"""Connections to the five outside services. Nothing else in the app talks to the network."""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
import httpx

from . import config

log = logging.getLogger("drafter")

SERPER_URL = "https://google.serper.dev/search"
TAVILY_URL = "https://api.tavily.com/search"
HUNTER_URL = "https://api.hunter.io/v2/email-finder"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TRIES = 6
GROQ_LONGEST_WAIT = 65  # the free plan's limits are per minute, so a minute is always enough
REST_SECONDS = 120


class ProviderError(Exception):
    """An outside service failed or gave an answer we could not use."""

    def __init__(self, message: str, busy: bool = False):
        super().__init__(message)
        self.busy = busy  # True when waiting and trying again could help


@dataclass
class AgentTurn:
    stop_reason: str
    content: list  # passed back unchanged on the next request
    tool_calls: list = field(default_factory=list)  # (id, name, input)


def matches_schema(schema: dict, value, path: str = "$") -> list[str]:
    """A small JSON Schema check for the parts of the standard our schemas use."""
    kind = schema.get("type")
    type_ok = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
    }.get(kind, True)
    if not type_ok:
        return [f"{path} should be {kind}"]
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} has an unexpected value")
    if kind == "object":
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key} is missing")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors += matches_schema(sub, value[key], f"{path}.{key}")
    if kind == "array":
        for i, item in enumerate(value):
            errors += matches_schema(schema["items"], item, f"{path}[{i}]")
    return errors


class Providers:
    def __init__(self):
        self._http = httpx.Client(timeout=45)
        self._claude = None
        self._resting = {}  # model -> time it can be tried again

    # ---- search ----------------------------------------------------------

    def _post(self, url: str, attempts: int = 2, **kwargs) -> dict:
        last = None
        for _ in range(attempts):
            try:
                resp = self._http.post(url, **kwargs)
            except httpx.HTTPError as exc:
                last = exc
                continue
            if resp.status_code == 429:  # too many requests: wait briefly and try again
                last = resp.status_code
                if attempts > 1:
                    time.sleep(4)
                continue
            if resp.status_code < 500:
                break
            last = resp.status_code
        else:
            raise ProviderError(f"{url} did not answer: {last}", busy=True)
        if resp.status_code != 200:
            raise ProviderError(f"{url} answered {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def web_search(self, query: str) -> list[dict]:
        key = config.env("SERPER_API_KEY")
        if not key:
            raise ProviderError("Serper key is missing")
        body = self._post(SERPER_URL, headers={"X-API-KEY": key}, json={"q": query, "num": 8})
        return [
            {"title": r.get("title", ""), "url": r["link"], "snippet": r.get("snippet", "")}
            for r in body.get("organic", [])
            if r.get("link")
        ]

    def search_answers(self, query: str, news: bool = False) -> list[dict]:
        """Like web_search, but can look at news, and keeps the date Google shows."""
        key = config.env("SERPER_API_KEY")
        if not key:
            raise ProviderError("Serper key is missing")
        url = SERPER_URL.replace("/search", "/news") if news else SERPER_URL
        body = self._post(url, headers={"X-API-KEY": key}, json={"q": query, "num": 10})
        return [
            {"title": r.get("title", ""), "url": r["link"], "snippet": r.get("snippet", ""), "date": r.get("date", "")}
            for r in body.get("news" if news else "organic", [])
            if r.get("link")
        ]

    def research(self, query: str, topic: str = "general") -> list[dict]:
        key = config.env("TAVILY_API_KEY")
        if not key:
            raise ProviderError("Tavily key is missing")
        body = self._post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "query": query,
                "topic": topic,
                "search_depth": "basic",
                "max_results": 5,
                "include_raw_content": True,
                # LinkedIn is never read directly.
                "exclude_domains": ["linkedin.com"],
            },
        )
        return [
            {
                "title": r.get("title", ""),
                "url": r["url"],
                "text": r.get("raw_content") or r.get("content") or "",
                "published": r.get("published_date", ""),
            }
            for r in body.get("results", [])
            if r.get("url")
        ]

    def find_email(self, first: str, last: str, company: str) -> dict | None:
        key = config.env("HUNTER_API_KEY")
        if not key:
            raise ProviderError("Hunter key is missing")
        try:
            resp = self._http.get(
                HUNTER_URL,
                params={"company": company, "first_name": first, "last_name": last, "api_key": key},
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Hunter did not answer: {type(exc).__name__}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ProviderError(f"Hunter answered {resp.status_code}")
        data = resp.json().get("data") or {}
        if not data.get("email"):
            return None
        sources = [s.get("uri", "") for s in data.get("sources", []) if s.get("uri")]
        return {"email": data["email"], "score": data.get("score") or 0, "sources": sources[:3]}

    # ---- AI --------------------------------------------------------------

    @property
    def has_claude(self) -> bool:
        return bool(config.env("ANTHROPIC_API_KEY"))

    # ---- Claude Code on this computer (no API key; local use only) ------

    @property
    def use_cli(self) -> bool:
        return config.env("AI_BACKEND") == "claude-code"

    def _cli_json(self, system: str, user: str, schema: dict, effort: str) -> dict:
        exe = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
        command = [
            exe, "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--system-prompt", system,
            "--model", config.CLAUDE_MODEL,
            "--effort", effort,
            # It reads text from web pages, so it gets no tools and none of this computer's settings.
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
        }
        with tempfile.TemporaryDirectory() as empty_folder:
            try:
                done = subprocess.run(
                    command, input=user, capture_output=True, encoding="utf-8", errors="replace",
                    timeout=300, cwd=empty_folder, env=env,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ProviderError(f"Claude Code did not answer: {type(exc).__name__}", busy=True) from exc
        try:
            reply = json.loads(done.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Claude Code failed: {(done.stderr or done.stdout)[:300]}") from exc
        if reply.get("is_error") or done.returncode != 0:
            raise ProviderError(f"Claude Code failed: {str(reply.get('result'))[:300]}")
        data = reply.get("structured_output")
        problems = ["no structured answer"] if data is None else matches_schema(schema, data)
        if problems:
            raise ProviderError(f"Claude Code's answer had the wrong shape: {problems[:3]}")
        return data

    @staticmethod
    def _cli_transcript(messages: list) -> str:
        parts = []
        for message in messages:
            if message["role"] == "assistant":
                parts.append(f"YOUR ACTION:\n{json.dumps(message['content'])}")
            elif isinstance(message["content"], str):
                parts.append(message["content"])
            else:
                parts.extend(f"RESULT:\n{r['content']}" for r in message["content"])
        return "\n\n".join(parts)

    def _cli_turn(self, system: str, messages: list, tools: list) -> AgentTurn:
        """One step of the writer. Each step replays the conversation so far."""
        by_name = {t["name"]: t["input_schema"] for t in tools}
        draft = by_name["submit_draft"]
        schema = {
            "type": "object",
            "properties": {"tool": {"type": "string", "enum": list(by_name)}, **draft["properties"]},
            "required": ["tool", *draft["required"]],
            "additionalProperties": False,
        }
        guide = "\n".join(f"- {t['name']}: {t['description']}" for t in tools)
        instruction = (
            f"{system}\n\nEach reply is exactly one action. Set \"tool\" to one of these and fill the other "
            f"fields with your work. Fields a tool does not use are ignored.\n{guide}"
        )
        action = self._cli_json(instruction, self._cli_transcript(messages), schema, "medium")
        name = action["tool"]
        args = {key: action[key] for key in by_name[name]["properties"]}
        return AgentTurn("tool_use", action, [(f"call-{len(messages)}", name, args)])

    @property
    def claude(self) -> anthropic.Anthropic:
        if self._claude is None:
            self._claude = anthropic.Anthropic()
        return self._claude

    def _claude_create(self, output_config: dict, **params):
        try:
            response = self.claude.beta.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=16000,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                output_config=output_config,
                **params,
            )
        except anthropic.APIError as exc:
            raise ProviderError(f"Claude failed: {type(exc).__name__}: {exc}") from exc
        if response.stop_reason == "refusal":
            raise ProviderError("Claude declined this request")
        return response

    # ---- Groq ------------------------------------------------------------

    @property
    def use_groq(self) -> bool:
        return config.env("AI_BACKEND") == "groq"

    def _groq_json(self, system: str, user: str, schema: dict, effort: str, longest: int) -> dict:
        key = config.env("GROQ_API_KEY")
        if not key:
            raise ProviderError("Groq key is missing")
        body = {
            "model": config.GROQ_MODEL,
            "reasoning_effort": effort,
            "include_reasoning": False,
            "max_completion_tokens": longest,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "strict": True, "schema": schema}},
        }
        for _ in range(GROQ_TRIES):
            try:
                resp = self._http.post(GROQ_URL, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=120)
            except httpx.HTTPError as exc:
                raise ProviderError(f"Groq did not answer: {type(exc).__name__}", busy=True) from exc
            if resp.status_code == 429:
                time.sleep(min(float(resp.headers.get("retry-after", 15)), GROQ_LONGEST_WAIT))
                continue
            if resp.status_code == 400 and "json_validate_failed" in resp.text:
                continue  # it occasionally misses the format, and another try usually lands
            break
        else:
            raise ProviderError("Groq stayed busy", busy=True)
        if resp.status_code != 200:
            raise ProviderError(f"Groq answered {resp.status_code}: {resp.text[:200]}")
        try:
            answer = json.loads(resp.json()["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(f"Groq gave an unreadable answer: {type(exc).__name__}") from exc
        wrong = matches_schema(schema, answer)
        if wrong:
            raise ProviderError(f"Groq gave the wrong shape: {wrong[:3]}")
        return answer

    def check_json(self, purpose: str, system: str, user: str, schema: dict) -> dict:
        """The reader tests. On Groq when there's a key, so a Claude writer isn't marking its own work."""
        if not config.env("GROQ_API_KEY"):
            return self.light_json(purpose, system, user, schema)
        try:
            return self._groq_json(system, user, schema, "low", 1500)
        except ProviderError as exc:
            if self.use_groq:
                raise
            log.warning("Groq couldn't run the %s check, using the main AI: %s", purpose, exc)
            return self.light_json(purpose, system, user, schema)

    def deep_json(self, purpose: str, system: str, user: str, schema: dict) -> dict:
        """The hard thinking. Claude if a key is set, otherwise Gemini."""
        if self.use_groq:
            return self._groq_json(system, user, schema, "medium", 5000)
        if self.use_cli:
            return self._cli_json(system, user, schema, "medium")
        if not self.has_claude:
            return self._gemini_json(system, user, schema, config.GEMINI_DEEP_MODEL)
        response = self._claude_create(
            {"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if response.stop_reason == "max_tokens":
            raise ProviderError("Claude ran out of room")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)

    def light_json(self, purpose: str, system: str, user: str, schema: dict) -> dict:
        """Simple, high-volume work. Gemini first; Claude steps in only if a Claude key is set."""
        if self.use_groq:
            return self._groq_json(system, user, schema, "low", 1500)
        if self.use_cli:
            return self._cli_json(system, user, schema, "low")
        try:
            return self._gemini_json(system, user, schema)
        except ProviderError as exc:
            if not self.has_claude:
                raise
            log.warning("Gemini failed on %s, using Claude: %s", purpose, exc)
            return self.deep_json(purpose, system, user, schema)

    def _gemini_call(self, model: str, payload: dict) -> dict:
        key = config.env("GEMINI_API_KEY") or config.env("GOOGLE_API_KEY")
        if not key:
            raise ProviderError("Gemini key is missing")
        return self._post(
            GEMINI_URL.format(model=model), attempts=1, timeout=90, headers={"x-goog-api-key": key}, json=payload
        )

    def _gemini_try(self, first: str, payload: dict) -> tuple[dict, str]:
        """Use the first model that answers.

        A model that fails rests for a while, so runs stay quick. If every model is
        busy we wait and go round again, because busy spells are usually short.
        """
        chain = list(dict.fromkeys([first, *config.GEMINI_BACKUPS]))
        last = None
        for wait in (6, 12, 0):
            busy = False
            awake = [m for m in chain if self._resting.get(m, 0) <= time.time()]
            for model in awake or chain:
                try:
                    return self._gemini_call(model, payload), model
                except ProviderError as exc:
                    last, busy = exc, busy or exc.busy
                    self._resting[model] = time.time() + REST_SECONDS
                    log.warning("Gemini model %s unavailable: %s", model, exc)
            if not busy or not wait:
                break
            time.sleep(wait)
        raise last

    def _gemini_pinned(self, model: str, payload: dict) -> dict:
        """A conversation must stay on one model, so wait for it rather than switch."""
        last = None
        for _ in range(3):
            try:
                return self._gemini_call(model, payload)
            except ProviderError as exc:
                last = exc
                time.sleep(5)
        raise last

    def _gemini_json(self, system: str, user: str, schema: dict, model: str | None = None) -> dict:
        body, _used = self._gemini_try(
            model or config.GEMINI_MODEL,
            {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseJsonSchema": schema,
                },
            },
        )
        try:
            text = "".join(p.get("text", "") for p in body["candidates"][0]["content"]["parts"])
            data = json.loads(text)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Gemini gave an unreadable answer: {exc}") from exc
        problems = matches_schema(schema, data)
        if problems:
            raise ProviderError(f"Gemini's answer had the wrong shape: {problems[:3]}")
        return data

    @staticmethod
    def _gemini_contents(messages: list) -> list:
        """Convert the running conversation to Gemini's format.

        The model's own turns are sent back untouched, because Gemini needs the
        thought signatures attached to its function calls.
        """
        contents, last_model_parts = [], []
        for message in messages:
            if message["role"] == "assistant":
                last_model_parts = message["content"]
                contents.append({"role": "model", "parts": last_model_parts})
            elif isinstance(message["content"], str):
                contents.append({"role": "user", "parts": [{"text": message["content"]}]})
            else:
                calls = [p["functionCall"] for p in last_model_parts if "functionCall" in p]
                parts = []
                for call, result in zip(calls, message["content"]):
                    reply = {"name": call["name"], "response": json.loads(result["content"])}
                    if call.get("id"):
                        reply["id"] = call["id"]
                    parts.append({"functionResponse": reply})
                contents.append({"role": "user", "parts": parts})
        return contents

    def _gemini_turn(self, system: str, messages: list, tools: list) -> AgentTurn:
        declarations = []
        for tool in tools:
            item = {"name": tool["name"], "description": tool["description"]}
            if tool["input_schema"]["properties"]:
                item["parametersJsonSchema"] = tool["input_schema"]
            declarations.append(item)
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": self._gemini_contents(messages),
            "tools": [{"functionDeclarations": declarations}],
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        }
        # Gemini's reasoning signatures belong to one model, so the writer never switches midway.
        pinned = messages[0].get("gemini_model")
        if pinned:
            body = self._gemini_pinned(pinned, payload)
        else:
            body, messages[0]["gemini_model"] = self._gemini_try(config.GEMINI_DEEP_MODEL, payload)
        try:
            candidate = body["candidates"][0]
            parts = candidate["content"]["parts"]
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"Gemini gave no usable answer: {str(body)[:200]}") from exc
        calls = [
            (p["functionCall"].get("id") or f"call-{len(messages)}-{i}", p["functionCall"]["name"], p["functionCall"].get("args", {}))
            for i, p in enumerate(parts)
            if "functionCall" in p
        ]
        return AgentTurn("tool_use" if calls else candidate.get("finishReason", "STOP"), parts, calls)

    def agent_turn(self, system: str, messages: list, tools: list) -> AgentTurn:
        if self.use_cli:
            return self._cli_turn(system, messages, tools)
        if not self.has_claude:
            return self._gemini_turn(system, messages, tools)
        response = self._claude_create({"effort": "medium"}, system=system, messages=messages, tools=tools)
        calls = [(b.id, b.name, b.input) for b in response.content if b.type == "tool_use"]
        return AgentTurn(response.stop_reason, response.content, calls)
