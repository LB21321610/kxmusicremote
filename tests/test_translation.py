"""Deterministic translation contracts. No real credentials or paid requests."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from types import SimpleNamespace
import requests
from lyrics_translation import (TranslationService, LocalTranslationClient, LocalModelError,
                                batches, request_body, MAX_BYTES, TRANSLATION_POLICY)
from test_lyrics import remote


class FakeClient:
    def __init__(self, errors=None, delay=0, detected="EN"):
        self.calls = []
        self.errors = list(errors or [])
        self.delay = delay
        self.detected = detected

    def translate_text(self, texts, **kw):
        self.calls.append((list(texts), kw))
        if self.delay:
            time.sleep(self.delay)
        error = self.errors.pop(0) if self.errors else None
        if isinstance(error, Exception):
            raise error
        if error == "wrong_count":
            return []
        return [SimpleNamespace(text="译文："+text, detected_source_lang=self.detected) for text in texts]


def register(service, texts, plain=False, metadata=None):
    lrc = "\n".join(f"[{i//60:02}:{i%60:02}.00]{text}" for i, text in enumerate(texts))
    result = dict(ok=True, state="plain" if plain else "synced", record=metadata or {},
                  lines=[] if plain else remote.parse_lrc(lrc), plain="\n".join(texts) if plain else "")
    return service.register(result, "" if plain else lrc, "\n".join(texts))


def wait(service, document):
    end = time.monotonic()+10
    while time.monotonic() < end:
        value = service.snapshot(document["lyrics_id"])
        if value["state"] != "pending":
            return value
        time.sleep(.01)
    raise AssertionError("translation worker did not finish")


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.services = []
        self.env = patch.dict(os.environ, {"HY_MT2_URL":"", "HY_MT2_MODEL":""})
        self.env.start()

    def tearDown(self):
        for service in self.services:
            service.pool.shutdown(wait=True)
        self.env.stop()
        self.temp.cleanup()

    def service(self, client=None, **kw):
        service = TranslationService(Path(self.temp.name)/"cache.sqlite3", client=client, sleeper=lambda _: None, **kw)
        self.services.append(service)
        return service

    def translate(self, service, document):
        service.start(document["lyrics_id"])
        return wait(service, document)

    def test_foreign_rows_batch_mapping_blank_and_duplicates(self):
        client = FakeClient()
        service = self.service(client)
        texts = ["", "♪", "[Instrumental]", "Same refrain", "Same refrain"] + [f"English line {i}" for i in range(52)]
        doc = register(service, texts)
        original = copy.deepcopy(doc)
        value = self.translate(service, doc)
        self.assertEqual(value["state"], "complete")
        self.assertEqual([len(c[0]) for c in client.calls], [1]*54)
        self.assertEqual([c[0][0] for c in client.calls[:2]], ["Same refrain", "Same refrain"])
        for row in value["rows"]:
            source = next(r for r in doc["display_rows"] if r["id"] == row["id"])
            self.assertEqual(row["text"], source["text"])
            if source["text"] and source["text"] not in ("♪", "[Instrumental]"):
                self.assertEqual(row["translation"], "译文："+source["text"])
        for _, opts in client.calls:
            self.assertEqual(opts["target_lang"], "ZH-HANS")
            self.assertIn("English line 51", opts["context"])
        self.assertEqual(doc, original)

    def test_local_traditional_simplified_and_mixed_no_key(self):
        service = self.service()
        doc = register(service, ["今天的風輕輕吹過我的心", "我愛你直到永遠", "今天的风轻轻吹过我的心", "繁體與简体相遇"])
        result = self.translate(service, doc)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["error"], "")
        self.assertEqual(result["rows"][0]["text"], "今天的风轻轻吹过我的心")
        self.assertEqual(result["rows"][2]["text"], doc["display_rows"][2]["text"])
        self.assertEqual(result["rows"][3]["text"], "繁体与简体相遇")
        self.assertTrue(all(r["translation"] == "" for r in result["rows"]))
        self.assertIn("風", doc["display_rows"][0]["text"])

    def test_opencc_phrase_conversion(self):
        service = self.service()
        doc = register(service, ["乾杯之後頭髮隨風飛揚", "乾坤之中乾燥的天空"])
        result = self.translate(service, doc)
        self.assertEqual(result["rows"][0]["text"], "干杯之后头发随风飞扬")
        self.assertIn("乾坤", result["rows"][1]["text"])
        self.assertIn("干燥", result["rows"][1]["text"])

    def test_mixed_language_each_line_and_script_fragment(self):
        client = FakeClient()
        service = self.service(client)
        doc = register(service, ["我愛你直到永遠", "I love you", "今天的風 I will remember you 永遠吹過我的心"])
        result = self.translate(service, doc)
        sent = [t for texts, _ in client.calls for t in texts]
        self.assertEqual(sent, ["I love you", "I will remember you"])
        self.assertEqual(result["rows"][0]["translation"], "")
        self.assertEqual(result["rows"][2]["text"], "今天的风 I will remember you 永远吹过我的心")
        self.assertEqual(result["rows"][2]["translation"], "译文：I will remember you")

    def test_japanese_kanji_uses_song_context_not_han_only(self):
        client = FakeClient(detected="JA")
        service = self.service(client)
        doc = register(service, ["君の声が聞こえる", "夜空", "愛", "東京夜景"])
        result = self.translate(service, doc)
        self.assertEqual([c[0][0] for c in client.calls], ["君の声が聞こえる", "夜空", "愛", "東京夜景"])
        self.assertEqual(client.calls[0][1]["source_lang"], "JA")
        self.assertEqual([r["text"] for r in result["rows"]], [r["text"] for r in doc["display_rows"]])
        self.assertEqual(result["rows"][2]["translation"], "译文：爱")
        kanji = register(service, ["夜空", "愛"], metadata={"artistName":"宇多田ヒカル", "language":"ja"})
        self.translate(service, kanji)
        self.assertEqual([c[0][0] for c in client.calls[-2:]], ["夜空", "愛"])

    def test_trustworthy_same_timestamp_translation_reused(self):
        service = self.service(FakeClient())
        lrc = "[00:00]I will remember you\n[00:00]我會永遠記住你\n[00:10]君の声が聞こえる\n[00:10]我仍然聽得到你的聲音"
        result = dict(ok=True, state="synced", record={}, lines=remote.parse_lrc(lrc), plain="")
        doc = service.register(result, lrc, "")
        translated = self.translate(service, doc)
        self.assertEqual(service.client.calls, [])
        self.assertEqual(translated["rows"][0]["text"], "I will remember you\n我会永远记住你")
        self.assertEqual(translated["rows"][1]["text"], "君の声が聞こえる\n我仍然听得到你的声音")
        self.assertTrue(all(r["translation"] == "" for r in translated["rows"]))
        self.assertEqual(result["lines"][0]["text"], "I will remember you\n我會永遠記住你")

    def test_japanese_source_hint_cannot_be_overridden_by_chinese_detection(self):
        service = self.service(FakeClient(detected="ZH"))
        doc = register(service, ["愛"], metadata={"language":"ja"})
        result = self.translate(service, doc)
        self.assertEqual(service.client.calls[0][1]["source_lang"], "JA")
        self.assertEqual(result["error"], "invalid_response")
        self.assertEqual(result["rows"][0]["text"], "愛")
        self.assertEqual(result["rows"][0]["translation"], "")

    def test_plain_rows_preserve_blanks_and_no_timestamps(self):
        service = self.service(FakeClient())
        doc = register(service, ["English", "", "今天的風輕輕吹過我的心", "Again"], plain=True)
        result = self.translate(service, doc)
        self.assertEqual(len(result["rows"]), 4)
        self.assertEqual(result["rows"][1]["text"], "")
        self.assertTrue(all(not r["timestamps"] and "time" not in r for r in doc["display_rows"]))
        # Adjacent ordinary lyrics are not silently assumed to be paired translations.
        self.assertEqual(result["rows"][0]["translation"], "译文：English")

    def test_stable_ids_order_and_all_timestamps(self):
        service = self.service()
        lrc = "[offset:-100]\n[00:02.00][00:05.50]Refrain\n[00:08]Next"
        result = dict(ok=True, state="synced", record={}, lines=remote.parse_lrc(lrc), plain="")
        one = service.register(result, lrc, "")
        two = service.register(result, lrc, "")
        self.assertEqual(one["lyrics_id"], two["lyrics_id"])
        self.assertEqual(one["display_rows"][0]["timestamps"], [1.9, 5.4])
        self.assertEqual(one["display_rows"][1]["timestamps"], [1.9, 5.4])
        self.assertEqual(len({r["id"] for r in one["display_rows"]}), 3)
        self.assertEqual([r["time"] for r in one["display_rows"]], [1.9, 5.4, 7.9])

    def test_persistent_cache_content_and_config_invalidation(self):
        first = self.service(FakeClient())
        doc = register(first, ["English text", "Same refrain"])
        original = self.translate(first, doc)
        second = self.service(FakeClient())
        fresh = register(second, ["English text", "Same refrain"])
        cached = self.translate(second, fresh)
        self.assertTrue(cached["cached"])
        self.assertEqual(second.client.calls, [])
        self.assertEqual(cached["rows"], original["rows"])
        changed = register(second, ["Different English text", "Same refrain"])
        self.translate(second, changed)
        self.assertEqual(len(second.client.calls), 2)
        third = self.service(FakeClient(), process_version="changed-config")
        self.translate(third, register(third, ["English text", "Same refrain"]))
        self.assertEqual(len(third.client.calls), 2)

    def test_concurrent_devices_merge_and_poll_does_not_start(self):
        service = self.service(FakeClient(delay=.2))
        doc = register(service, ["English text"])
        self.assertEqual(service.snapshot(doc["lyrics_id"])["state"], "idle")
        self.assertEqual(service.client.calls, [])
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: service.start(doc["lyrics_id"]), range(20)))
        wait(service, doc)
        self.assertEqual(len(service.client.calls), 1)
        for _ in range(10):
            service.start(doc["lyrics_id"])
        self.assertEqual(len(service.client.calls), 1)

    def test_partial_success_only_missing_batch_retried(self):
        client = FakeClient(errors=[None]*50 + ["wrong_count"])
        service = self.service(client)
        doc = register(service, [f"English line {i}" for i in range(60)])
        result = self.translate(service, doc)
        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["missing"], 10)
        self.assertEqual(sum(bool(r["translation"]) for r in result["rows"]), 50)
        service.jobs[service._job_key(service.documents[doc["lyrics_id"]])]["retry_at"] = 0
        service.start(doc["lyrics_id"], retry=True)
        result = wait(service, doc)
        self.assertEqual(result["state"], "complete")
        self.assertEqual([c[0][0] for c in client.calls[-10:]], [f"English line {i}" for i in range(50,60)])

    def test_error_categories_retries_and_local_conversion_survives(self):
        failures = [(LocalModelError("model_not_found"), "model_not_found", 1),
                    (requests.exceptions.ConnectionError("secret"), "unavailable", 1),
                    (requests.exceptions.Timeout("secret"), "timeout", 3)]
        for code, category in [(429, "rate_limited"), (503, "temporary_error")]:
            response = requests.Response()
            response.status_code = code
            failures.append((requests.exceptions.HTTPError("secret", response=response), category, 3))
        for exc, category, calls in failures:
            with self.subTest(category=category):
                client = FakeClient(errors=[exc]*3)
                service = self.service(client, process_version=category)
                result = self.translate(service, register(service, ["English text", "今天的風輕輕吹過我的心"]))
                self.assertEqual(result["error"], category)
                self.assertEqual(len(client.calls), calls)
                self.assertEqual(result["rows"][1]["text"], "今天的风轻轻吹过我的心")
                self.assertNotIn("secret", json.dumps(result))
                if category == "rate_limited":
                    self.translate(service, register(service, ["Another song"]));self.assertEqual(len(client.calls), calls)

    def test_temporary_error_backoff_then_success(self):
        sleeps = []
        response = requests.Response()
        response.status_code = 500
        service = self.service(FakeClient(errors=[requests.exceptions.HTTPError("busy", response=response), None]))
        service.sleep = sleeps.append
        result = self.translate(service, register(service, ["English text"]))
        self.assertEqual(result["state"], "complete")
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], .5)

    def test_detected_chinese_does_not_add_duplicate_and_cache_restores(self):
        service = self.service(FakeClient(detected="ZH"))
        doc = register(service, ["愛"])
        result = self.translate(service, doc)
        self.assertEqual(result["rows"][0]["translation"], "")
        self.assertEqual(result["rows"][0]["text"], "爱")
        second = self.service(FakeClient())
        cached = self.translate(second, register(second, ["愛"]))
        self.assertEqual(cached["rows"], result["rows"])
        self.assertEqual(second.client.calls, [])

    def test_byte_limit_includes_context_and_json_escaping(self):
        tasks = [dict(text="外语翻译🌟"*600, id=str(i)) for i in range(80)]
        context = "完整上下文"*1000
        groups = list(batches(tasks, context))
        self.assertGreater(len(groups), 2)
        self.assertEqual(sum(len(b) for b in groups), 80)
        for batch in groups:
            self.assertEqual(len(batch), 1)
            self.assertLess(len(json.dumps(request_body([t["text"] for t in batch], context)).encode()), MAX_BYTES)
        with self.assertRaises(ValueError):
            list(batches([dict(text="x"*MAX_BYTES)], context))

    def test_unknown_id_oversized_lyrics_and_bounded_pending_jobs(self):
        service = self.service(FakeClient(delay=.1))
        self.assertFalse(service.start("unknown")["ok"])
        too_big = register(service, ["x"*9000])
        self.assertNotIn("lyrics_id", too_big)
        docs = [register(service, [f"English song {i}"]) for i in range(10)]
        for doc in docs[:8]:
            service.start(doc["lyrics_id"])
        self.assertEqual(service.start(docs[8]["lyrics_id"])["error"], "busy")

    def test_local_endpoint_no_proxy_and_remote_config_rejected(self):
        with patch.dict(os.environ, {"HTTPS_PROXY":"http://example.invalid", "DEEPL_AUTH_KEY":"unused-secret"}):
            service = self.service()
        self.assertEqual(service.client.base_url, "http://127.0.0.1:11434")
        self.assertEqual(service.client.model, "hy-mt2-translate")
        self.assertFalse(service.client.session.trust_env)
        self.assertNotIn("unused-secret", json.dumps(service.config))
        for url in ["https://third-party.invalid", "http://10.0.0.1:11434", "http://localhost:11434/path",
                    "http://user:password@localhost:11434", "http://localhost:11434?url=evil"]:
            with self.subTest(url=url), patch.dict(os.environ, {"HY_MT2_URL":url}):
                bad = self.service()
                self.assertIsNone(bad.client)
                self.assertEqual(bad.config_error, "invalid_config")

    def test_ollama_serialization_direct_output_and_tone_policy(self):
        service = self.service()
        observed = []
        def send(prepared, **options):
            self.assertEqual(prepared.url, "http://127.0.0.1:11434/api/generate")
            self.assertNotIn("Authorization", prepared.headers)
            self.assertEqual(options["timeout"], (3, 120))
            payload = json.loads(prepared.body)
            self.assertEqual(payload["model"], "hy-mt2-translate")
            self.assertIs(payload["stream"], False)
            self.assertIn("English two", payload["system"])
            self.assertIn(TRANSLATION_POLICY, payload["system"])
            self.assertIn("不删减、不打码", payload["system"])
            self.assertLess(len(prepared.body), MAX_BYTES)
            observed.append(payload["prompt"])
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(dict(response="他妈的，滚开！", done=True, done_reason="stop")).encode()
            return response
        with patch.object(service.client.session, "send", side_effect=send):
            result = self.translate(service, register(service, ["English one", "English two"]))
        self.assertEqual(result["state"], "complete")
        self.assertEqual(observed, ["English one", "English two"])
        self.assertTrue(all(r["translation"] == "他妈的，滚开！" and r["method"] == "hy-mt2" for r in result["rows"]))

    def test_model_missing_redirects_reasoning_and_truncation_are_not_published(self):
        client = LocalTranslationClient()
        cases = [(404, {}, LocalModelError), (302, {}, LocalModelError),
                 (200, {"response":"<think>解释</think>译文", "done":True}, ValueError),
                 (200, {"response":"未完成", "done":True, "done_reason":"length"}, ValueError),
                 (200, {"response":"", "done":True}, ValueError),
                 (200, {"response":"译文", "done":False}, ValueError),
                 (200, {"response":"译文", "done":True, "thinking":"解释"}, ValueError)]
        for code, body, error in cases:
            with self.subTest(code=code, body=body):
                response = requests.Response()
                response.status_code = code
                response._content = json.dumps(body).encode()
                with patch.object(client.session, "post", return_value=response) as post:
                    with self.assertRaises(error):
                        client.translate_text(["Hello"])
                    self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_model_and_policy_changes_invalidate_previous_translations(self):
        first = self.service(FakeClient())
        self.translate(first, register(first, ["English text"]))
        with patch.dict(os.environ, {"HY_MT2_MODEL":"different-model"}):
            second = self.service(FakeClient())
        self.translate(second, register(second, ["English text"]))
        self.assertEqual(len(second.client.calls), 1)
        with patch("lyrics_translation.TRANSLATION_POLICY", TRANSLATION_POLICY + "changed"):
            third = self.service(FakeClient())
        self.translate(third, register(third, ["English text"]))
        self.assertEqual(len(third.client.calls), 1)

    def test_completed_line_is_published_while_next_line_is_pending(self):
        service = self.service(FakeClient(delay=.2))
        doc = register(service, ["Line one", "Line two"])
        service.start(doc["lyrics_id"])
        end = time.monotonic() + 5
        saw_partial = False
        while time.monotonic() < end:
            snapshot = service.snapshot(doc["lyrics_id"])
            if snapshot["state"] == "pending" and sum(bool(r["translation"]) for r in snapshot["rows"]) == 1:
                saw_partial = True
                break
            time.sleep(.01)
        self.assertTrue(saw_partial)
        self.assertEqual(wait(service, doc)["state"], "complete")

    def test_http_rejects_arbitrary_text_bad_origin_and_poll_without_start(self):
        service = self.service(FakeClient(delay=.2))
        doc = register(service, ["English text"])
        server = ThreadingHTTPServer(("127.0.0.1", 0), remote.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch.object(remote, "translation_service", return_value=service):
                with urlopen(base+"/lyrics/translation?lyrics_id="+doc["lyrics_id"]) as r:
                    self.assertEqual(json.load(r)["state"], "idle")
                for body, headers, expected in [({"text":"arbitrary","lyrics_id":doc["lyrics_id"]},{},400),
                                               ({"lyrics_id":doc["lyrics_id"]},{"Origin":"https://evil.invalid"},403),
                                               ({"lyrics_id":"fake"},{},400),
                                               ({"lyrics_id":doc["lyrics_id"],"padding":"x"*300},{},400)]:
                    with self.assertRaises(HTTPError) as e:
                        urlopen(Request(base+"/lyrics/translation",json.dumps(body).encode(),
                                        headers={"Content-Type":"application/json",**headers}))
                    self.assertEqual(e.exception.code, expected)
                self.assertEqual(service.client.calls, [])
                with urlopen(Request(base+"/lyrics/translation",json.dumps({"lyrics_id":doc["lyrics_id"]}).encode(),headers={"Content-Type":"application/json"})) as r:
                    self.assertEqual(json.load(r)["state"], "pending")
                started = time.monotonic()
                with urlopen(base+"/") as r:
                    self.assertEqual(r.status,200)
                self.assertLess(time.monotonic()-started,.15)
                wait(service,doc)
        finally:
            server.shutdown();server.server_close()


if __name__ == "__main__":
    unittest.main()
