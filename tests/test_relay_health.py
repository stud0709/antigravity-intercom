import asyncio
import base64
import json
import os
from pathlib import Path
import types
import urllib.request
from unittest import mock

from test_intercom import IsolatedStateTestCase, nostr_relay as relay, runtime_adapter as runtime
import relay_health as health


class RelayHealthTests(IsolatedStateTestCase):
    def setUp(self):
        super().setUp()
        self.url, self.other = relay.DEFAULT_RELAYS[:2]
        health._deadlines.clear()

    def test_nip11_uses_https_accept_header_and_bounded_read(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"limitation": {"max_message_length": 8192,
            "max_content_length": 1024, "auth_required": False, "unexpected": "SECRET"}}).encode()
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(urllib.request, "build_opener", return_value=opener):
            limits = health.fetch_information(self.url)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, self.url.replace("wss:", "https:"))
        self.assertEqual(request.get_header("Accept"), "application/nostr+json")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)
        response.read.assert_called_once_with(health.MAX_DOCUMENT_BYTES + 1)
        self.assertEqual(limits["max_message_length"], 8192)
        self.assertNotIn("unexpected", limits)

    def test_discovery_cache_and_unknown_metadata_do_not_invent_limits(self):
        with mock.patch.object(health, "fetch_information", side_effect=ValueError("PRIVATE")) as fetch:
            self.assertEqual(asyncio.run(health.discover([self.url])), {self.url: {}})
            asyncio.run(health.discover([self.url]))
            fetch.assert_called_once()
        snapshot = health.snapshot([self.url])[0]
        self.assertEqual(snapshot["metadata"], "unknown")
        self.assertEqual(health.eligible([self.url], content="anything", frame_bytes=100000), [self.url])
        self.assertNotIn("PRIVATE", health._path().read_text())

    def test_small_limits_are_respected_without_a_minimum_floor(self):
        with mock.patch.object(health, "fetch_information", return_value={"max_message_length": 100,
                "max_content_length": 5}):
            asyncio.run(health.discover([self.url]))
        for content, size in (("small", 101), ("toolong", 90)):
            with self.assertRaises(health.PublishError) as raised:
                health.eligible([self.url], content=content, frame_bytes=size)
            self.assertEqual(raised.exception.code, "message_too_large")
        self.assertEqual(health.eligible([self.url], content="small", frame_bytes=100), [self.url])

    def test_cooldown_is_per_relay_persistent_and_expires_on_monotonic_clock(self):
        with mock.patch.object(health.time, "time", return_value=100), \
             mock.patch.object(health.time, "monotonic", return_value=50), \
             mock.patch.object(health.random, "uniform", return_value=0):
            health.observe(self.url + "/", "rate_limited")
            self.assertEqual(health.eligible([self.url, self.other]), [self.other])
            health._deadlines.clear()  # Simulate a worker restart.
            self.assertEqual(health.snapshot([self.url])[0]["retry_after_seconds"], 10)
            with self.assertRaises(health.PublishError) as raised:
                health.eligible([self.url])
            self.assertEqual(raised.exception.result()["publication"], "not_sent")
        with mock.patch.object(health.time, "time", return_value=100), \
             mock.patch.object(health.time, "monotonic", return_value=61):
            self.assertEqual(health.eligible([self.url]), [self.url])

    def test_unknown_notice_invalid_and_duplicate_do_not_disable_relay(self):
        for reason in ("server says SECRET", "invalid: SECRET", "duplicate: SECRET"):
            health.observe(self.url, health.classify(reason))
            self.assertEqual(health.eligible([self.url]), [self.url])
        self.assertNotIn("SECRET", health._path().read_text())
        health.observe(self.url, "blocked")
        self.assertGreater(health.snapshot([self.url])[0]["retry_after_seconds"], 0)
        health.observe(self.url, "accepted", accepted=True)
        self.assertEqual(health.eligible([self.url]), [self.url])

    def test_redirects_cannot_escape_configured_relay(self):
        request = urllib.request.Request("https://relay.damus.io")
        with self.assertRaises(ValueError):
            health._Redirect().redirect_request(request, None, 302, "", {}, "http://127.0.0.1/secrets")

    def test_incompatible_access_requirements_are_reported(self):
        with mock.patch.object(health, "fetch_information", return_value={"payment_required": True}):
            asyncio.run(health.discover([self.url]))
        with self.assertRaises(health.PublishError) as raised:
            health.eligible([self.url])
        self.assertEqual(raised.exception.code, "relay_access_required")

    def test_sdk_warnings_are_typed_and_event_bodies_are_never_serialized(self):
        import nostr_sdk
        msg = mock.Mock()
        msg.as_enum.return_value = nostr_sdk.RelayMessageEnum.NOTICE("rate-limited: SECRET")
        health.observe_message(self.url, msg)
        self.assertGreater(health.snapshot([self.url])[0]["retry_after_seconds"], 0)
        msg.as_json.assert_not_called()
        self.assertNotIn("SECRET", health._path().read_text())

    def test_subscription_refresh_reuses_one_quota_slot(self):
        async def scenario():
            client = mock.Mock()
            client.subscribe_with_id = mock.AsyncMock(return_value=types.SimpleNamespace(success=[self.url], failed={}))
            client.unsubscribe = mock.AsyncMock()
            await relay.subscribe_topics(client, {"agy_" + "a" * 32})
            await relay.subscribe_topics(client, {"agy_" + "b" * 32})
            self.assertEqual([call.args[0] for call in client.subscribe_with_id.call_args_list], [relay.SUBSCRIPTION_ID] * 2)
            await relay.subscribe_topics(client, set())
            client.unsubscribe.assert_awaited_once_with(relay.SUBSCRIPTION_ID)
        asyncio.run(scenario())

    def test_transport_measures_signed_encrypted_frame_and_uses_healthy_relays_once(self):
        import nostr_sdk
        async def scenario():
            signer = nostr_sdk.NostrSigner.keys(nostr_sdk.Keys.generate())
            client = mock.Mock()
            client.add_relay = mock.AsyncMock()
            client.connect = mock.AsyncMock()
            client.shutdown = mock.AsyncMock()
            client.sign_event_builder = mock.AsyncMock(side_effect=lambda builder: builder.sign(signer))
            # AsyncMock side effects do not await a returned coroutine.
            async def sign(builder):
                return await builder.sign(signer)
            client.sign_event_builder.side_effect = sign
            async def notices(handler):
                await asyncio.Event().wait()
            client.handle_notifications = mock.AsyncMock(side_effect=notices)
            client.send_event_to = mock.AsyncMock(return_value=types.SimpleNamespace(
                success=[nostr_sdk.RelayUrl.parse(self.other)], failed={}, id=types.SimpleNamespace(to_hex=lambda: "event")))
            health.observe(self.url, "rate_limited")
            with mock.patch.object(health, "fetch_information", return_value={}), \
                 mock.patch.object(relay.nostr_sdk, "Client", return_value=client):
                result = await relay._async_publish_raw("agy_" + "a" * 32, "remote", {"content": "SECRET"}, b"k" * 32, [self.url, self.other])
            self.assertEqual(result, "event")
            client.send_event_to.assert_awaited_once()
            urls, event = client.send_event_to.call_args.args
            self.assertEqual([str(url).rstrip("/") for url in urls], [self.other])
            self.assertNotIn("SECRET", event.content())
            self.assertEqual(relay.decrypt_payload_aes_gcm(event.content(), b"k" * 32)["content"], "SECRET")
            client.shutdown.assert_awaited_once()
        asyncio.run(scenario())

    def test_inline_attachment_switches_to_encrypted_blossom_when_event_will_not_fit(self):
        share = self.state_dir / "share"
        share.mkdir(parents=True)
        path = share / "sample.bin"
        path.write_bytes(os.urandom(2048))
        connection = {"protocol": "intercom-private-session-v1", "topic": "agy_" + "a" * 32,
                      "preshared_key": base64.b64encode(b"k" * 32).decode()}
        async def publish(topic, recipient, payload, key, urls):
            self.assertEqual(payload["attachment"]["encoding"], "blossom+aes256gcm")
            self.assertNotIn("data", payload["attachment"])
            return "event"
        with mock.patch.object(health, "fetch_information", return_value={"max_message_length": 2000}), \
             mock.patch.object(relay, "resolve_attachment_path", return_value=str(path)), \
             mock.patch.object(relay, "upload_to_blossom", return_value="https://blossom.primal.net/file") as upload, \
             mock.patch.object(relay, "_async_publish_raw", side_effect=publish):
            result = asyncio.run(relay._async_publish("local", "remote", "hello", str(path), connection["topic"], [self.url],
                connection=connection, sign_payload=lambda body: {**body, "signature": "s" * 88}, attachment_root=share))
        self.assertEqual(result, "event")
        upload.assert_called_once()
        self.assertNotEqual(upload.call_args.args[0], path.read_bytes())
