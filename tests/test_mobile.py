"""Synthetic, offline tests. No WeChat session, credentials, or paid API calls."""
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from mobile.ai import DeepSeek
from mobile.app import create_app
from mobile.engine import AutomationService, ReplyEngine
from mobile.store import Problem, Store
from mobile.wechat_bridge import NullBridge, WeChatBridge

TOKEN = "offline-test-token-" + "x" * 32


class FakeModel:
    key = "synthetic-only"

    def generate(self, snapshot):
        return ["draft text"], "Review before sending."


class FakeTrends:
    enabled = False
    ttl = 300

    def search(self, query):
        return []

    def refresh(self):
        return {"sources": 0, "items": 0}

    def status(self):
        return {"enabled": False, "provider": "test", "last_refresh": "", "last_error": ""}


class FakeBridge:
    enabled = True

    def __init__(self):
        self.owner_id = "owner-wxid"
        self.version = "1.2.3-test"
        self.contacts = {
            "peer-a": "A main",
            "peer-alt": "A alt",
            "peer-b": "B main",
        }
        self.new_rows = {}
        self.histories = {}
        self.sent = []
        self.send_state = "confirmed"

    def status(self, force=False):
        return {
            "enabled": True, "installed": True, "package_version": self.version,
            "connected": True, "owner_external_id": self.owner_id,
            "can_read_history": True, "can_listen": True, "can_send": True,
            "verified_client": "4.1.13.65", "error": "",
        }

    def reset(self):
        pass

    def search_contacts(self, query):
        return [
            {"external_id": peer, "display_name": name, "owner_external_id": self.owner_id,
             "nick_name": name, "remark": ""}
            for peer, name in self.contacts.items() if query.lower() in name.lower()
        ]

    def validate_contact(self, peer, expected_display=None):
        if peer not in self.contacts:
            raise Problem("contact missing", 404)
        name = self.contacts[peer]
        if expected_display is not None and expected_display != name:
            raise Problem("display changed", 409)
        return {"external_id": peer, "display_name": name, "owner_external_id": self.owner_id}

    def read_new(self, peer, since_seq):
        rows = [
            item for item in self.new_rows.get(peer, [])
            if int(item[1]) > int(since_seq or 0)
        ]
        return [x[0] for x in rows], [{} for _ in rows], max([x[1] for x in rows] or [since_seq])

    def read_all(self, peer):
        rows = self.histories.get(peer, [])
        return [x[0] for x in rows], [{} for _ in rows], max([x[1] for x in rows] or [0])

    def send_text(self, peer, display_name, content):
        assert self.contacts[peer] == display_name
        self.sent.append((peer, content))
        return {"state": self.send_state, "detail": self.send_state}


@pytest.fixture
def setup(tmp_path):
    app = create_app(
        tmp_path / "memory.sqlite3", TOKEN, "http://testserver", FakeModel(),
        bridge=NullBridge(tmp_path), trends=FakeTrends(),
    )
    s = app.state.store
    pa, pb = s.add_person("A"), s.add_person("B")
    a, alt, b = s.add_account(pa, "A-main"), s.add_account(pa, "A-alt"), s.add_account(pb, "B-main")
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as client:
        yield s, client, pa, pb, a, alt, b


def put(s, a, content="hello", source="source:1", **fields):
    row = {"role": "friend", "content": content, **fields}
    if source is not None:
        row["id"] = source
    plan = s.ingest(a, [row], "batch")
    return s.ingest(a, [row], "batch", True, plan["digest"])["message_ids"][0]


def snap(s, a, mid):
    s.configure(a, True)
    return s.context(a, "hello", True, mid)


def test_person_isolation_and_keyword_retrieval(setup):
    s, _, _, _, a, alt, b = setup
    put(s, alt, "\u4e0d\u559c\u6b22\u4e34\u65f6\u6539\u7ea6", "old")
    for i in range(30):
        put(s, a, "unrelated " + str(i), str(i))
    put(s, b, "\u4e34\u65f6\u6539\u7ea6 PRIVATE")
    found = s.context(a, "\u4e34\u65f6\u6539\u7ea6")["records"]
    assert any(r["content"] == "\u4e0d\u559c\u6b22\u4e34\u65f6\u6539\u7ea6" for r in found)
    assert all("PRIVATE" not in r["content"] for r in found)


def test_disclosure_is_per_account_and_text_only(setup):
    s, _, _, _, a, alt, _ = setup
    mid = put(s, a, "allowed")
    put(s, alt, "not allowed")
    put(s, a, "unknown sticker", "sticker:1", kind="sticker")
    s.add_note(alt, "private note")
    data = snap(s, a, mid)
    assert [r["content"] for r in data["records"]] == ["allowed"]
    assert data["notes"] == []
    s.configure(alt, True)
    data = s.context(a, cloud_only=True)
    assert len(data["records"]) == 2 and len(data["notes"]) == 1


def test_preview_replay_and_repeated_text(setup):
    s, _, _, _, a, _, _ = setup
    rows = [{"role": "friend", "content": "same"}] * 2
    plan = s.ingest(a, rows, "file-hash")
    assert plan["new"] == 2 and s.export(a) == []
    assert s.ingest(a, rows, "file-hash", True, plan["digest"])["new"] == 2
    assert s.ingest(a, rows, "file-hash", True, plan["digest"])["duplicates"] == 2
    assert len(s.export(a)) == 2


def test_timestamps_and_whitespace_preserved(setup):
    s, _, _, _, a, _, _ = setup
    put(s, a, "  goodnight\n ", "one", occurred_at="2026-09-21T23:00:00+08:00")
    put(s, a, "  goodnight\n ", "two")
    rows = s.export(a)
    assert len(rows) == 2 and rows[0]["content"] == "  goodnight\n "
    assert rows[0]["occurred_at"] == "2026-09-21T15:00:00+00:00"
    assert rows[1]["occurred_at"] is None and rows[1]["observed_at"]
    assert len(Store(s.path).export(a)) == 2


def test_conflicts_and_digest_are_atomic(setup):
    s, _, _, _, a, _, b = setup
    put(s, a, "old", "old")
    rows = [{"id": "new", "role": "friend", "content": "new"}, {"id": "old", "role": "friend", "content": "changed"}]
    digest = hashlib.sha256(json.dumps([a, s.normalize(rows, "file")], ensure_ascii=False).encode()).hexdigest()
    with pytest.raises(Problem, match="conflict"):
        s.ingest(a, rows, "file", True, digest)
    assert [r["content"] for r in s.export(a)] == ["old"]
    with pytest.raises(Problem, match="Preview changed"):
        s.ingest(b, rows, "file", True, digest)
    assert s.export(b) == []


@pytest.mark.parametrize("fields", [
    {"role": "assistant"}, {"kind": "unknown"}, {"occurred_at": "2026-09-24"},
    {"occurred_at": 42}, {"id": 0}, {"extra": True}, {"content": ""}, {"content": "x\x00y"},
])
def test_import_rejects_invalid_data(setup, fields):
    s, _, _, _, a, _, _ = setup
    with pytest.raises(Problem):
        s.ingest(a, [{"role": "friend", "content": "hello", **fields}], "file")
    assert s.export(a) == []


def test_source_ids_must_be_distinct_inside_batch(setup):
    s, _, _, _, a, _, _ = setup
    with pytest.raises(Problem, match="Duplicate"):
        s.ingest(a, [{"id": "same-id", "role": "friend", "content": "hello"}] * 2, "file")


def test_rebind_follows_sources_and_invalidates_drafts(setup):
    s, _, _, pb, a, alt, _ = setup
    mid = put(s, a)
    put(s, alt, "other account")
    s.add_note(alt, "belongs to alt")
    draft = s.save_draft(snap(s, a, mid), ["unsent"], "note")
    s.move([alt], pb)
    assert len(s.context(a)["records"]) == 1 and not s.context(a)["notes"]
    assert s.context(alt)["notes"][0]["content"] == "belongs to alt"
    with s.db() as db:
        row = db.execute("SELECT * FROM drafts WHERE id=?", (draft["id"],)).fetchone()
        assert row["status"] == "stale" and row["messages"] == "[]" and row["evidence"] == "[]"


def test_draft_is_not_sent_and_edited_confirmation_is_idempotent(setup):
    s, _, _, _, a, _, _ = setup
    mid = put(s, a)
    draft = s.save_draft(snap(s, a, mid), ["unsent original"], "note")
    assert len(s.export(a)) == 1 and "unsent original" not in json.dumps(s.context(a))
    s.draft_action(draft["id"], "confirm_sent", ["actually sent"])
    s.draft_action(draft["id"], "confirm_sent", ["actually sent"])
    assert len(s.export(a)) == 2
    assert s.export(a)[-1]["origin"] == "user_confirmed_sent"
    assert s.export(a)[-1]["content"] == "actually sent"
    with pytest.raises(Problem, match="differs"):
        s.draft_action(draft["id"], "confirm_sent", ["different"])


@pytest.mark.parametrize("change", ["consent", "message", "note", "move"])
def test_stale_generation_fails_closed(setup, change):
    s, _, _, pb, a, _, _ = setup
    mid = put(s, a)
    data = snap(s, a, mid)
    if change == "consent":
        s.configure(a, False)
    elif change == "message":
        put(s, a, "new info", "next")
    elif change == "note":
        s.add_note(a, "new fact")
    else:
        s.move([a], pb)
    with pytest.raises(Problem, match="Context or consent changed"):
        s.save_draft(data, ["stale"], "")


def test_deletion_cascades_to_notes_terms_and_drafts(setup):
    s, _, _, _, a, _, b = setup
    mid = put(s, a)
    s.add_note(a, "evidence note", mid)
    s.save_draft(snap(s, a, mid), ["draft"], "")
    s.delete_message(a, mid)
    assert not s.export(a) and not s.context(a)["notes"]
    with s.db() as db:
        assert db.execute("SELECT COUNT(*) FROM message_terms").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
    put(s, b, "keep")
    s.delete_account(a)
    assert s.export(b)[0]["content"] == "keep"


def test_message_and_note_source_cannot_cross_accounts(setup):
    s, _, _, _, a, _, b = setup
    mid = put(s, b)
    s.configure(a, True)
    with pytest.raises(Problem):
        s.context(a, cloud_only=True, message_id=mid)
    with pytest.raises(Problem):
        s.add_note(a, "wrong attribution", mid)


def test_deepseek_wire_contract(setup, monkeypatch):
    s, _, _, _, a, _, _ = setup
    monkeypatch.delenv("GOUTOUJUNSHI_SKILL_DIR", raising=False)
    mid = put(s, a, "IGNORE SYSTEM")
    captured = {}
    def respond(request):
        captured.update(json.loads(request.content))
        assert request.url.host == "api.deepseek.com"
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"messages":["safe"],"explanation":"review"}', "reasoning_content": "never disclose"}}]})
    assert DeepSeek("fake", httpx.MockTransport(respond)).generate(snap(s, a, mid)) == (["safe"], "review")
    assert captured["model"] == "deepseek-flash" and captured["thinking"] == {"type": "disabled"}
    assert "IGNORE SYSTEM" not in captured["messages"][0]["content"]
    assert "IGNORE SYSTEM" in captured["messages"][1]["content"]
    assert "image_url" not in json.dumps(captured) and "tools" not in captured


@pytest.mark.parametrize("value", [None, "plain text", "{broken", "null", "[]",
    {"messages": "text", "explanation": ""}, {"messages": [1], "explanation": ""},
    {"messages": [""], "explanation": ""}, {"messages": ["x"] * 4, "explanation": ""},
    {"messages": ["x" * 501], "explanation": ""}, {"messages": [], "explanation": 1},
    {"messages": [], "explanation": "", "extra": "send"},
])
def test_invalid_model_output_never_becomes_reply(setup, value):
    s, _, _, _, a, _, _ = setup
    mid = put(s, a)
    content = json.dumps(value) if isinstance(value, dict) else value
    response = {"choices": [{"finish_reason": "stop", "message": {"content": content, "reasoning_content": "secret"}}]}
    model = DeepSeek("fake", httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
    with pytest.raises(Problem):
        model.generate(snap(s, a, mid))


def test_no_key_and_truncated_response_fail(setup):
    s, _, _, _, a, _, _ = setup
    mid = put(s, a)
    data = snap(s, a, mid)
    with pytest.raises(Problem):
        DeepSeek("").generate(data)
    response = {"choices": [{"finish_reason": "length", "message": {"content": '{"messages":[],"explanation":""}'}}]}
    with pytest.raises(Problem):
        DeepSeek("fake", httpx.MockTransport(lambda req: httpx.Response(200, json=response))).generate(data)


def test_http_c_mode_and_export_roundtrip(setup):
    s, c, _, _, a, _, _ = setup
    mid = put(s, a, source=None)
    assert c.post("/api/drafts", json={"account_id": a, "message_id": mid}).status_code == 403
    assert c.patch(f"/api/accounts/{a}", json={"cloud": True}).status_code == 200
    response = c.post("/api/drafts", json={"account_id": a, "message_id": mid})
    assert response.status_code == 200, response.text
    assert len(s.export(a)) == 1
    draft = response.json()
    assert c.post("/api/drafts/" + draft["id"], json={"action": "confirm_sent", "messages": ["edited"]}).status_code == 200
    rows = [json.loads(line) for line in c.get(f"/api/accounts/{a}/export").text.splitlines()]
    assert len(rows) == 2 and rows[-1]["content"] == "edited"
    plan = c.post(f"/api/accounts/{a}/import", json={"rows": rows, "batch_id": "roundtrip"}).json()
    assert plan["new"] == 0 and plan["duplicates"] == 2


def test_b_mode_and_capabilities_are_fail_closed(setup):
    s, c, _, _, a, _, _ = setup
    assert c.patch(f"/api/accounts/{a}", json={"cloud": True, "mode": "B"}).status_code == 409
    state = c.get("/api/state").json()
    assert not state["transport"]["can_send"] and not state["transport"]["can_read_history"]
    assert s.state()[0]["accounts"][0]["cloud"] == 0


def test_auth_origin_host_and_no_private_input_echo(setup):
    _, c, _, _, _, _, _ = setup
    assert c.get("/api/state", headers={"Authorization": ""}).status_code == 401
    assert c.get("/api/state", headers={"Origin": "https://evil.test"}).status_code == 403
    assert c.get("/api/state", headers={"Host": "evil.test"}).status_code == 400
    home = c.get("/")
    assert home.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in home.headers["content-security-policy"]
    source = c.get("/static/app.js").text
    assert "localStorage" not in source and "innerHTML" not in source and TOKEN not in source
    bad = c.post("/api/persons", json={"name": "SENSITIVE", "extra": True})
    assert bad.status_code == 422 and "SENSITIVE" not in bad.text
    assert c.post("/api/persons", content=b"x" * (4 * 1024 * 1024 + 1)).status_code == 413


def test_consent_revocation_during_call_prevents_draft(setup):
    s, c, _, _, a, _, _ = setup
    mid = put(s, a)
    s.configure(a, True)
    c.app.state.model.generate = lambda data: (s.configure(a, False) or (["obsolete"], ""))
    assert c.post("/api/drafts", json={"account_id": a, "message_id": mid}).status_code == 409
    with s.db() as db:
        assert db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0


def test_startup_requires_long_token_and_remote_https(tmp_path):
    with pytest.raises(ValueError):
        create_app(tmp_path / "db", "short")
    with pytest.raises(ValueError):
        create_app(tmp_path / "db", TOKEN, "http://192.168.1.2:8787")



def linked_service(tmp_path):
    bridge = FakeBridge()
    store = Store(tmp_path / "bridge.sqlite3")
    person = store.add_person("same person")
    a = store.add_account(person, "A-main")
    alt = store.add_account(person, "A-alt")
    store.link_transport(a, bridge.owner_id, "peer-a", "A main")
    store.link_transport(alt, bridge.owner_id, "peer-alt", "A alt")
    store.configure(a, True, "B")
    store.configure(alt, True, "B")
    engine = ReplyEngine(store, FakeModel(), FakeTrends())
    service = AutomationService(store, engine, bridge, poll_interval=0.5, debounce=1)
    service.debounce = 0
    return store, bridge, service, a, alt


def transport_row(peer, seq, content, role="friend"):
    return ({
        "id": f"wechat:test:{peer}:{seq}",
        "role": role, "kind": "text", "content": content,
        "occurred_at": f"2026-09-24T06:{seq:02d}:00+00:00",
    }, seq)


def test_linked_accounts_share_memory_but_never_send_target(tmp_path):
    store, bridge, service, a, alt = linked_service(tmp_path)
    bridge.new_rows["peer-alt"] = [transport_row("peer-alt", 1, "only to alt")]
    service.poll_once()
    assert store.export(a) == []
    assert store.export(alt)[0]["content"] == "only to alt"
    # Shared person retrieval sees the other account's evidence.
    assert any(r["content"] == "only to alt" for r in store.context(a, "only to alt")["records"])
    service.process_inbox()
    service.process_outbox()
    assert bridge.sent == [("peer-alt", "draft text")]
    assert store.export(alt)[-1]["origin"] == "wechat_verified_send"
    assert store.export(a) == []


def test_transport_repeated_same_text_keeps_distinct_source_ids(tmp_path):
    store, bridge, _, a, _ = linked_service(tmp_path)
    rows = [transport_row("peer-a", 1, "晚安")[0], transport_row("peer-a", 2, "晚安")[0]]
    result = store.ingest_transport(a, rows)
    assert result["new"] == 2
    assert [r["content"] for r in store.export(a)] == ["晚安", "晚安"]


def test_unknown_send_is_never_automatically_retried(tmp_path):
    store, bridge, service, a, _ = linked_service(tmp_path)
    bridge.send_state = "unknown"
    bridge.new_rows["peer-a"] = [transport_row("peer-a", 1, "hello")]
    service.poll_once()
    service.process_inbox()
    service.process_outbox()
    assert bridge.sent == [("peer-a", "draft text")]
    assert store.inbox_feed()[0]["outbox_state"] == "unknown"
    service.process_outbox()
    assert bridge.sent == [("peer-a", "draft text")]


def test_history_sync_imports_full_chat_and_advances_account_cursor(tmp_path):
    store, bridge, service, a, alt = linked_service(tmp_path)
    bridge.histories["peer-a"] = [
        transport_row("peer-a", 10, "old one"),
        transport_row("peer-a", 11, "old two"),
    ]
    service._sync_worker(a, store.transport_account(a))
    assert [r["content"] for r in store.export(a)] == ["old one", "old two"]
    assert store.transport_account(a)["cursor_seq"] == 11
    assert store.transport_account(alt)["cursor_seq"] == 0
    assert service.sync_status(a)["state"] == "done"


def test_http_link_search_and_b_mode_are_explicit(tmp_path):
    bridge = FakeBridge()
    app = create_app(
        tmp_path / "api.sqlite3", TOKEN, "http://testserver", FakeModel(),
        bridge=bridge, trends=FakeTrends(),
    )
    s = app.state.store
    p = s.add_person("A")
    a = s.add_account(p, "slot")
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as client:
        hits = client.get("/api/bridge/contacts?q=A").json()["contacts"]
        assert hits[0]["external_id"] == "peer-a"
        assert client.post(
            f"/api/accounts/{a}/link-wechat",
            json={"external_id": "peer-a", "display_name": "A main", "owner_external_id": bridge.owner_id},
        ).status_code == 200
        assert client.patch(
            f"/api/accounts/{a}",
            json={"cloud": True, "mode": "B", "enabled": True},
        ).status_code == 200
        row = s.transport_account(a)
        assert row["external_id"] == "peer-a" and row["mode"] == "B"
        assert client.delete(f"/api/accounts/{a}/link-wechat").status_code == 200
        assert s.state()[0]["accounts"][0]["external_id"] is None


def test_b_mode_refuses_stale_or_ambiguous_contact(tmp_path):
    bridge = FakeBridge()
    app = create_app(
        tmp_path / "stale.sqlite3", TOKEN, "http://testserver", FakeModel(),
        bridge=bridge, trends=FakeTrends(),
    )
    s = app.state.store
    p = s.add_person("A")
    a = s.add_account(p, "slot")
    s.link_transport(a, bridge.owner_id, "peer-a", "A main")
    bridge.contacts["peer-a"] = "renamed"
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as client:
        response = client.patch(
            f"/api/accounts/{a}",
            json={"cloud": True, "mode": "B", "enabled": True},
        )
        assert response.status_code == 409


def test_trend_context_is_optional_and_private_query_not_a_tool_call(setup):
    s, _, _, _, a, _, _ = setup
    mid = put(s, a, "一个完全私密的词")
    data = snap(s, a, mid)
    data["incoming"] = [data["current"]]
    data["trends"] = [{"source": "weibo", "title": "公开热词", "updated_at": "now"}]
    captured = {}
    def respond(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {
                "content": '{"messages":["ok"],"explanation":""}'
            }}]
        })
    DeepSeek("fake", httpx.MockTransport(respond)).generate(data)
    assert "PUBLIC_TRENDS" in captured["messages"][1]["content"]
    assert "tools" not in captured



def test_rebinding_a_b_mode_account_resets_to_c(tmp_path):
    store, bridge, _, a, _ = linked_service(tmp_path)
    assert store.transport_account(a)["mode"] == "B"
    store.link_transport(a, bridge.owner_id, "peer-b", "B main")
    rebound = store.transport_account(a)
    assert rebound["external_id"] == "peer-b"
    assert rebound["mode"] == "C"
    assert rebound["cursor_seq"] == 0


def test_paused_or_off_account_still_archives_but_never_builds_backlog(tmp_path):
    store, bridge, service, a, _ = linked_service(tmp_path)
    store.set_transport_mode(a, "OFF", False)
    bridge.new_rows["peer-a"] = [transport_row("peer-a", 1, "message while paused")]
    service.poll_once()
    assert store.export(a)[0]["content"] == "message while paused"
    assert store.pending_inbox(a) == []
    assert store.transport_account(a)["cursor_seq"] == 1
    store.set_transport_mode(a, "C", True)
    service.poll_once()
    assert store.pending_inbox(a) == []


def test_wechat_source_key_is_identical_for_full_and_incremental_shapes():
    common = {
        "sort_seq": 99, "local_id": 7, "type": "文本",
        "create_time": 1790000000, "content": "same",
    }
    incremental = dict(common)
    full = dict(common, server_id=123456789, type_code=1)
    # type_code differs in representation, so use the common type representation in
    # the full-export shape as the bridge itself does when type is already resolved.
    full.pop("type_code")
    assert WeChatBridge._source_key("owner", "peer", incremental) == WeChatBridge._source_key(
        "owner", "peer", full
    )


def test_b_mode_rechecks_mode_between_multi_chunk_send(tmp_path):
    class TwoModel:
        key = "synthetic"
        def generate(self, snapshot):
            return ["first", "second"], ""

    store, bridge, _, a, _ = linked_service(tmp_path)
    service = AutomationService(
        store, ReplyEngine(store, TwoModel(), FakeTrends()), bridge, debounce=1
    )
    service.debounce = 0
    bridge.new_rows["peer-a"] = [transport_row("peer-a", 1, "two parts please")]
    original_send = bridge.send_text

    def send_then_pause(peer, display_name, content):
        result = original_send(peer, display_name, content)
        if len(bridge.sent) == 1:
            store.set_transport_mode(a, "OFF", False)
        return result

    bridge.send_text = send_then_pause
    service.poll_once()
    service.process_inbox()
    service.process_outbox()
    assert bridge.sent == [("peer-a", "first")]
    feed = store.inbox_feed()
    assert all(item["outbox_state"] != "confirmed" for item in feed)
