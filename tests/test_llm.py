"""Gratis-KI (Gemini/Groq) mit einem nachgebildeten OpenAI-kompatiblen Server: Reihenfolge, Ausweichen, Korrektur."""
import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from pydantic import BaseModel

from app import config, llm, store

SCRIPT: dict[str, list] = {}   # Anbieter → Liste von (Status, Antworttext)
SEEN: list[tuple[str, dict, dict]] = []


class Answer(BaseModel):
    title: str
    items: list[str]


def ok(text, finish="stop"):
    return 200, json.dumps({"choices": [{"message": {"content": text}, "finish_reason": finish}],
                            "usage": {"prompt_tokens": 100, "completion_tokens": 50}})


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        prov = self.path.strip("/").split("/")[0]
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append((prov, body, dict(self.headers)))
        code, text = SCRIPT[prov].pop(0) if SCRIPT.get(prov) else (500, '{"error":{"message":"leer"}}')
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FreeLLMTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.tmp = Path(tempfile.mkdtemp())
        providers = {k: {**v, "base_url": f"{base}/{k}"} for k, v in config.FREE_PROVIDERS.items()}
        cls.patches = [mock.patch.object(config, "FAKE_MODE", False), mock.patch.object(config, "DATA_DIR", cls.tmp),
                       mock.patch.object(config, "DB_PATH", cls.tmp / "kompass.db"),
                       mock.patch.object(config, "ANTHROPIC_API_KEY", ""),
                       mock.patch.object(config, "FREE_PROVIDERS", providers)]
        for p in cls.patches:
            p.start()
        store.reset_connection()
        store.save_key("gemini", "gem-test-1234")
        store.save_key("groq", "gsk_test-5678")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        store.reset_connection()
        for p in cls.patches:
            p.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        SCRIPT.clear()
        SEEN.clear()

    def ask(self, purpose="nachricht_entwurf", content="Hallo"):
        return llm.ask(purpose, system="Test", content=content, schema=Answer, fake=lambda: None)

    def test_routing_light_and_heavy(self):
        SCRIPT["groq"] = [ok('{"title":"A","items":["x"]}')]
        self.assertEqual(self.ask().title, "A")
        self.assertEqual([s[0] for s in SEEN], ["groq"])
        SCRIPT["gemini"] = [ok('```json\n{"title":"Plan","items":[]}\n```')]
        self.assertEqual(self.ask("reiseplan").title, "Plan")
        self.assertEqual(SEEN[-1][0], "gemini")
        body, headers = SEEN[-1][1], SEEN[-1][2]
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertIn("JSON-Schema", body["messages"][0]["content"])
        self.assertTrue(headers["User-Agent"].startswith("Kompass/"))
        self.assertEqual(headers["Authorization"], "Bearer gem-test-1234")

    def test_fallback_on_daily_limit(self):
        SCRIPT["groq"] = [(429, '{"error":{"message":"Rate limit reached: tokens per day (TPD)"}}')]
        SCRIPT["gemini"] = [ok('{"title":"B","items":[]}')]
        self.assertEqual(self.ask().title, "B")
        self.assertEqual([s[0] for s in SEEN], ["groq", "gemini"])

    def test_invalid_json_is_corrected(self):
        SCRIPT["groq"] = [ok('{"title": 5}'), ok('{"title":"C","items":["a","b"]}')]
        self.assertEqual(self.ask().items, ["a", "b"])
        self.assertEqual(len(SEEN), 2)
        self.assertIn("passt nicht zum Schema", SEEN[1][1]["messages"][-1]["content"])

    def test_unsupported_json_mode_is_dropped(self):
        SCRIPT["groq"] = [(400, '{"error":{"message":"response_format json_object is not supported"}}'),
                          ok('Hier: {"title":"D","items":[]} fertig')]
        self.assertEqual(self.ask().title, "D")
        self.assertNotIn("response_format", SEEN[1][1])

    def test_image_only_to_gemini(self):
        SCRIPT["gemini"] = [ok('{"title":"Karte","items":[]}')]
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}},
                   {"type": "text", "text": "Übersetzen"}]
        self.assertEqual(self.ask("bild_uebersetzen", content).title, "Karte")
        self.assertEqual([s[0] for s in SEEN], ["gemini"])
        parts = SEEN[0][1]["messages"][1]["content"]
        self.assertEqual(parts[0]["image_url"]["url"], "data:image/jpeg;base64,QUJD")

    def test_all_fail_names_each_provider(self):
        SCRIPT["groq"] = [(401, '{"error":{"message":"invalid key"}}')]
        SCRIPT["gemini"] = [(503, '{"error":{"message":"high demand"}}'), (503, '{"error":{"message":"high demand"}}')]
        with mock.patch("time.sleep"):
            with self.assertRaises(llm.LLMError) as cm:
                self.ask()
        msg = str(cm.exception)
        self.assertIn("Groq: Schlüssel abgelehnt", msg)
        self.assertIn("Gemini: Fehler 503: high demand", msg)

    def test_truncated_answer_skips_to_next(self):
        SCRIPT["gemini"] = [ok('{"title":"x"', finish="length")]
        SCRIPT["groq"] = [ok('{"title":"E","items":[]}')]
        self.assertEqual(self.ask("zielvorschlaege").title, "E")

    def test_costs_recorded_as_free(self):
        SCRIPT["groq"] = [ok('{"title":"F","items":[]}')]
        self.ask()
        row = store.recent_costs(1)[0]
        self.assertEqual((row["model"], row["usd"]), ("groq:openai/gpt-oss-120b", 0.0))

    def test_key_info_never_returns_key(self):
        info = store.key_info()
        self.assertEqual(info["groq"]["hint"], "5678")
        self.assertNotIn("gsk_test", json.dumps(info))


if __name__ == "__main__":
    unittest.main()
