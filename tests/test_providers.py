"""Checks each real connection builds a sensible request and reads a typical reply."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import httpx
import pytest

from app import prompts
from app.providers import ProviderError, Providers, matches_schema

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}


def wired(monkeypatch, handler):
    for key in ("SERPER_API_KEY", "TAVILY_API_KEY", "HUNTER_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(key, "k")
    p = Providers()
    p._http = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def test_serper(monkeypatch):
    def handler(req):
        assert req.headers["X-API-KEY"] == "k"
        return httpx.Response(200, json={"organic": [{"title": "T", "link": "https://a", "snippet": "S"}]})
    assert wired(monkeypatch, handler).web_search("q") == [{"title": "T", "url": "https://a", "snippet": "S"}]


def test_tavily_skips_linkedin(monkeypatch):
    def handler(req):
        body = json.loads(req.content)
        assert body["exclude_domains"] == ["linkedin.com"] and body["include_raw_content"] is True
        return httpx.Response(200, json={"results": [{"title": "T", "url": "https://a", "raw_content": "R", "content": "C"}]})
    assert wired(monkeypatch, handler).research("q")[0]["text"] == "R"


def test_hunter(monkeypatch):
    def handler(req):
        assert req.url.params["company"] == "Acme"
        return httpx.Response(200, json={"data": {"email": "a@acme.com", "score": 88, "sources": [{"uri": "https://s"}]}})
    assert wired(monkeypatch, handler).find_email("A", "B", "Acme")["score"] == 88


def test_hunter_no_result(monkeypatch):
    assert wired(monkeypatch, lambda r: httpx.Response(200, json={"data": {"email": None}})).find_email("A", "B", "C") is None


def gemini_reply(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_gemini_reads_json(monkeypatch):
    def handler(req):
        assert json.loads(req.content)["generationConfig"]["responseJsonSchema"] == SCHEMA
        return httpx.Response(200, json=gemini_reply('{"a": "b"}'))
    assert wired(monkeypatch, handler)._gemini_json("s", "u", SCHEMA) == {"a": "b"}


def test_gemini_wrong_shape_falls_back_to_claude_when_key_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    p = wired(monkeypatch, lambda r: httpx.Response(200, json=gemini_reply('{"b": 1}')))
    p.deep_json = lambda *a: {"a": "from claude"}
    assert p.light_json("x", "s", "u", SCHEMA) == {"a": "from claude"}


def test_gemini_failure_is_an_error_without_claude(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    p = wired(monkeypatch, lambda r: httpx.Response(404, text="no such model"))
    with pytest.raises(ProviderError):
        p.light_json("x", "s", "u", SCHEMA)


def test_gemini_does_the_deep_work_without_claude(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    p = wired(monkeypatch, lambda r: httpx.Response(200, json=gemini_reply('{"a": "b"}')))
    assert p.deep_json("x", "s", "u", SCHEMA) == {"a": "b"}


def test_gemini_writer_round_trip_keeps_signatures(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    sent = []
    part = {"functionCall": {"name": "try_next_hook", "args": {}}, "thoughtSignature": "SIG"}

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [part]}, "finishReason": "STOP"}]})

    p = wired(monkeypatch, handler)
    messages = [{"role": "user", "content": "go"}]
    turn = p.agent_turn("s", messages, prompts.WRITER_TOOLS)
    call_id, name, args = turn.tool_calls[0]
    assert (name, args) == ("try_next_hook", {}) and turn.stop_reason == "tool_use"
    messages.append({"role": "assistant", "content": turn.content})
    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": '{"ok": true}'}]})
    p.agent_turn("s", messages, prompts.WRITER_TOOLS)

    declared = {d["name"]: d for d in sent[0]["tools"][0]["functionDeclarations"]}
    assert "parametersJsonSchema" not in declared["try_next_hook"]
    assert "strict" not in declared["submit_draft"] and "parametersJsonSchema" in declared["submit_draft"]
    contents = sent[1]["contents"]
    assert contents[1] == {"role": "model", "parts": [part]}
    assert contents[2]["parts"][0]["functionResponse"] == {"name": "try_next_hook", "response": {"ok": True}}


def test_busy_model_is_skipped_and_rests(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr("app.config.GEMINI_BACKUPS", ["backup-model"])
    tried = []

    def handler(req):
        tried.append(req.url.path.split("/")[-1].split(":")[0])
        if tried[-1] == "busy-model":
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=gemini_reply('{"a": "b"}'))

    p = wired(monkeypatch, handler)
    assert p._gemini_json("s", "u", SCHEMA, "busy-model") == {"a": "b"}
    assert p._gemini_json("s", "u", SCHEMA, "busy-model") == {"a": "b"}
    assert tried == ["busy-model", "backup-model", "backup-model"]


def test_waits_and_retries_when_every_model_is_busy(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr("app.config.GEMINI_BACKUPS", ["backup-model"])
    monkeypatch.setattr("app.providers.time.sleep", lambda s: None)
    tried = []

    def handler(req):
        tried.append(req.url.path.split("/")[-1].split(":")[0])
        if len(tried) <= 2:
            return httpx.Response(503 if tried[-1] == "main-model" else 429, text="busy")
        return httpx.Response(200, json=gemini_reply('{"a": "b"}'))

    p = wired(monkeypatch, handler)
    assert p._gemini_json("s", "u", SCHEMA, "main-model") == {"a": "b"}
    assert tried == ["main-model", "backup-model", "main-model"]


def test_does_not_wait_when_retrying_cannot_help(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr("app.config.GEMINI_BACKUPS", [])
    waits = []
    monkeypatch.setattr("app.providers.time.sleep", waits.append)
    p = wired(monkeypatch, lambda r: httpx.Response(404, text="no such model"))
    with pytest.raises(ProviderError):
        p._gemini_json("s", "u", SCHEMA, "main-model")
    assert waits == []


def test_writer_stays_on_one_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr("app.config.GEMINI_BACKUPS", ["backup-model"])
    monkeypatch.setattr("app.config.GEMINI_DEEP_MODEL", "first-model")
    tried = []
    part = {"functionCall": {"name": "try_next_hook", "args": {}}}

    def handler(req):
        tried.append(req.url.path.split("/")[-1].split(":")[0])
        if tried[-1] == "first-model":
            return httpx.Response(429, text="quota")
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [part]}}]})

    p = wired(monkeypatch, handler)
    messages = [{"role": "user", "content": "go"}]
    p.agent_turn("s", messages, prompts.WRITER_TOOLS)
    p._resting.clear()  # even when the first model is available again
    p.agent_turn("s", messages, prompts.WRITER_TOOLS)
    assert tried == ["first-model", "backup-model", "backup-model"]


def test_gemini_blocked_answer_is_an_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    p = wired(monkeypatch, lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "OTHER"}}))
    with pytest.raises(ProviderError):
        p.agent_turn("s", [{"role": "user", "content": "go"}], prompts.WRITER_TOOLS)


class CliResult:
    def __init__(self, reply, returncode=0):
        self.stdout, self.stderr, self.returncode = json.dumps(reply), "", returncode


def cli(monkeypatch, *replies):
    monkeypatch.setenv("AI_BACKEND", "claude-code")
    seen = []

    def fake_run(command, **kwargs):
        seen.append((command, kwargs))
        return CliResult(replies[min(len(seen) - 1, len(replies) - 1)])

    monkeypatch.setattr("app.providers.subprocess.run", fake_run)
    return Providers(), seen


def test_claude_code_gets_no_tools_and_no_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-be-passed")
    p, seen = cli(monkeypatch, {"is_error": False, "structured_output": {"a": "b"}})
    assert p.light_json("x", "sys", "page text", SCHEMA) == {"a": "b"}
    command, kwargs = seen[0]
    assert command[command.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in command and "--no-session-persistence" in command
    assert kwargs["input"] == "page text" and "page text" not in command
    assert "ANTHROPIC_API_KEY" not in kwargs["env"] and "CLAUDECODE" not in kwargs["env"]


def test_claude_code_error_and_bad_shape(monkeypatch):
    p, _ = cli(monkeypatch, {"is_error": True, "result": "limit reached"})
    with pytest.raises(ProviderError):
        p.deep_json("x", "s", "u", SCHEMA)
    p, _ = cli(monkeypatch, {"is_error": False, "structured_output": {"b": 1}})
    with pytest.raises(ProviderError):
        p.deep_json("x", "s", "u", SCHEMA)


def test_claude_code_writer_turns(monkeypatch):
    brief = {"tension": "t", "one_idea": "o", "proof": "p", "sticky_line": "s", "card_line": "c", "ask": "a"}
    action = {"tool": "submit_draft", "subject": "hi", "lines": [], "brief": brief}
    p, seen = cli(
        monkeypatch,
        {"is_error": False, "structured_output": {**action, "tool": "try_next_hook"}},
        {"is_error": False, "structured_output": action},
    )
    messages = [{"role": "user", "content": "the packet"}]
    turn = p.agent_turn("s", messages, prompts.WRITER_TOOLS)
    call_id, name, args = turn.tool_calls[0]
    assert (name, args) == ("try_next_hook", {})
    messages.append({"role": "assistant", "content": turn.content})
    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": '{"ok": 1}'}]})
    turn = p.agent_turn("s", messages, prompts.WRITER_TOOLS)
    assert turn.tool_calls[0][1:] == ("submit_draft", {"brief": brief, "subject": "hi", "lines": []})
    sent = seen[1][1]["input"]
    assert "the packet" in sent and "YOUR ACTION" in sent and '{"ok": 1}' in sent


def test_claude_code_needs_no_gemini_key(monkeypatch):
    from app import config
    monkeypatch.setenv("AI_BACKEND", "claude-code")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    assert config.missing_keys() == []


def test_all_schemas_are_strict():
    def walk(s):
        if s.get("type") == "object":
            assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
            for sub in s["properties"].values():
                walk(sub)
        if s.get("type") == "array":
            walk(s["items"])
    for schema in (prompts.PEOPLE_SCHEMA, prompts.FACTS_SCHEMA, prompts.ANALYST_SCHEMA):
        walk(schema)
    for tool in prompts.WRITER_TOOLS:
        walk(tool["input_schema"])


def test_matches_schema_catches_problems():
    assert matches_schema(SCHEMA, {"a": "x"}) == []
    assert matches_schema(SCHEMA, {}) and matches_schema(SCHEMA, {"a": 1})


@pytest.fixture
def local_claude(monkeypatch):
    """A local server that records what the real Anthropic library sends."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    seen = []
    replies = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append((dict(self.headers), body))
            out = json.dumps(replies.pop(0)).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    p = Providers()
    p._claude = anthropic.Anthropic(api_key="k", base_url=f"http://127.0.0.1:{server.server_port}")
    yield p, seen, replies
    server.shutdown()


def message(content, stop="end_turn"):
    return {
        "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
        "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def test_claude_json_request(local_claude):
    p, seen, replies = local_claude
    replies.append(message([{"type": "text", "text": '{"a": "b"}'}]))
    assert p.deep_json("x", "sys", "user", SCHEMA) == {"a": "b"}
    headers, body = seen[0]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"]["format"]["schema"] == SCHEMA and "thinking" not in body
    assert "server-side-fallback-2026-07-01" in {k.lower(): v for k, v in headers.items()}["anthropic-beta"]


def test_claude_refusal_is_an_error(local_claude):
    p, _, replies = local_claude
    replies.append(message([], stop="refusal"))
    with pytest.raises(ProviderError):
        p.deep_json("x", "s", "u", SCHEMA)


def test_agent_turn_keeps_content_for_next_request(local_claude):
    p, seen, replies = local_claude
    replies.append(message([{"type": "tool_use", "id": "t1", "name": "try_next_hook", "input": {}}], stop="tool_use"))
    replies.append(message([{"type": "text", "text": "ok"}]))
    messages = [{"role": "user", "content": "go"}]
    turn = p.agent_turn("s", messages, prompts.WRITER_TOOLS)
    assert turn.tool_calls == [("t1", "try_next_hook", {})]
    messages.append({"role": "assistant", "content": turn.content})
    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]})
    p.agent_turn("s", messages, prompts.WRITER_TOOLS)
    sent = seen[1][1]["messages"][1]["content"][0]
    assert sent["type"] == "tool_use" and sent["id"] == "t1"
    assert all(t["strict"] is True for t in seen[0][1]["tools"])
