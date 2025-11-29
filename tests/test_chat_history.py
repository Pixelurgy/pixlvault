
import pytest
from fastapi.testclient import TestClient
from pixlvault.server import Server
import tempfile
import os
import shutil
import time
from pixlvault.picture_tagger import PictureTagger

@pytest.fixture
def test_server():
    # Force CPU for all models during test
    PictureTagger.FORCE_CPU = True
    tmpdir = tempfile.mkdtemp()
    config_path = os.path.join(tmpdir, "config.json")
    server_config_path = os.path.join(tmpdir, "server_config.json")
    server = Server(config_path, server_config_path)
    client = TestClient(server.api)
    yield client
    shutil.rmtree(tmpdir)

def test_chat_history_save_and_load(test_server):
    client = test_server
    payload = {
        "character_id": "char1",
        "session_id": "sess1",
        "timestamp": int(time.time()),
        "role": "user",
        "content": "Hello!",
        "picture_id": 42
    }
    # Save a message
    resp = client.post("/chat/message", json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    # Load history
    resp = client.get("/chat/history", params={"character_id": "char1", "session_id": "sess1"})
    assert resp.status_code == 200
    messages = resp.json()["messages"]
    assert len(messages) == 1
    assert messages[0]["content"] == "Hello!"
    assert int(messages[0]["picture_id"]) == 42

def test_chat_history_clear(test_server):
    client = test_server
    payload = {
        "character_id": "char2",
        "session_id": "sess2",
        "timestamp": int(time.time()),
        "role": "user",
        "content": "To be deleted",
    }
    # Save a message
    resp = client.post("/chat/message", json=payload)
    assert resp.status_code == 200
    # Clear history
    resp = client.delete("/chat/history", params={"character_id": "char2", "session_id": "sess2"})
    assert resp.status_code == 200
    # Load history, should be empty
    resp = client.get("/chat/history", params={"character_id": "char2", "session_id": "sess2"})
    assert resp.status_code == 200
    messages = resp.json()["messages"]
    assert messages == []

def test_chat_history_multiple_sessions(test_server):
    client = test_server
    # Save messages to two sessions
    payload1 = {
        "character_id": "char3",
        "session_id": "sessA",
        "timestamp": int(time.time()),
        "role": "user",
        "content": "Session A",
    }
    payload2 = {
        "character_id": "char3",
        "session_id": "sessB",
        "timestamp": int(time.time()),
        "role": "user",
        "content": "Session B",
    }
    client.post("/chat/message", json=payload1)
    client.post("/chat/message", json=payload2)
    # Clear only session A
    client.delete("/chat/history", params={"character_id": "char3", "session_id": "sessA"})
    # Session A should be empty
    resp = client.get("/chat/history", params={"character_id": "char3", "session_id": "sessA"})
    assert resp.status_code == 200
    assert resp.json()["messages"] == []
    # Session B should still exist
    resp = client.get("/chat/history", params={"character_id": "char3", "session_id": "sessB"})
    assert resp.status_code == 200
    messages = resp.json()["messages"]
    assert len(messages) == 1
    assert messages[0]["content"] == "Session B"
