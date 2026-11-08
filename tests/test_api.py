import pytest


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_create_unknown_engine_returns_400(client):
    r = await client.post(
        "/translator/create",
        json={"original_text": "hi", "translate_model": "nope", "language": "zh"},
    )
    assert r.status_code == 400
    assert "unknown engine" in r.json()["detail"]


async def test_translate_mock_happy_path(client):
    # 1) create task (mock engine runs eagerly in-process)
    create = await client.post(
        "/translator/create",
        json={"original_text": "hello", "translate_model": "mock", "language": "zh"},
    )
    assert create.status_code == 200
    body = create.json()
    # With task_always_eager the result may already be SUCCESS, otherwise PENDING.
    if body.get("status") == "SUCCESS":
        assert body["data"]["translated_text"] == "[zh] hello"
        return

    task_id = body["task_id"]
    got = await client.get(f"/translator/get/{task_id}")
    assert got.status_code == 200
    payload = got.json()
    assert payload["status"] == "SUCCESS"
    assert payload["data"]["translated_text"] == "[zh] hello"


async def test_cache_hit_on_second_call(client):
    payload = {"original_text": "cache me", "translate_model": "mock", "language": "zh"}

    first = await client.post("/translator/create", json=payload)
    assert first.status_code == 200
    # Ensure first task completed and wrote cache.
    fb = first.json()
    if fb.get("status") != "SUCCESS":
        await client.get(f"/translator/get/{fb['task_id']}")

    second = await client.post("/translator/create", json=payload)
    assert second.status_code == 200
    sb = second.json()
    assert sb["status"] == "SUCCESS"
    assert sb["cached"] is True
    assert sb["data"]["translated_text"] == "[zh] cache me"


async def test_upper_engine(client):
    create = await client.post(
        "/translator/create",
        json={"original_text": "abc", "translate_model": "upper", "language": "en"},
    )
    body = create.json()
    if body.get("status") == "SUCCESS":
        assert body["data"]["translated_text"] == "ABC"
        return
    got = await client.get(f"/translator/get/{body['task_id']}")
    assert got.json()["data"]["translated_text"] == "ABC"
