import io
import wave


def wav(seconds=.2):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * int(16000 * seconds))
    return stream.getvalue()


def _upload(alice, key, filename="批量录音.wav"):
    return alice.post("/api/transcriptions", params={
        "customer_mode": "auto", "auto_analyze": "true", "filename": filename,
        "idempotency_key": key, "data_mode": "synthetic",
    }, content=wav(), headers={"Content-Type": "application/octet-stream"})


def test_auto_audio_creates_customer_and_analysis(team, monkeypatch):
    app, admin, alice, bob = team
    app.state.settings.asr_key = "test-asr-key"
    app.state.settings.api_key = "test-llm-key"
    monkeypatch.setattr("sdr.audio.transcribe", lambda *a, **kw: (
        "销售：您好，请问有什么需求？\n客户：我们做全屋定制，出图慢，想下个月试用。",
        {"model": "test-asr", "tokens": 0}))
    monkeypatch.setattr("sdr.audio.classify_roles", lambda *a, **kw: {
        "transcript": "销售：您好，请问有什么需求？\n客户：我们做全屋定制，出图慢，想下个月试用。",
        "turns": [], "uncertain_count": 0, "method": "text_context", "model": "test-llm"})
    monkeypatch.setattr(app.state.audio.pool, "submit", lambda *a: None)
    submitted = []
    monkeypatch.setattr(app.state.engine, "submit", lambda aid: submitted.append(aid))
    response = _upload(alice, "auto-audio-test-1")
    assert response.status_code == 202, response.text
    tid = response.json()["id"]
    app.state.audio.run(tid)
    row = alice.get(f"/api/transcriptions/{tid}").json()
    assert row["status"] == "confirmed"
    assert row["customer_id"]
    assert row["auto_analysis_status"] == "submitted"
    assert row["auto_analysis_id"]
    assert row["auto_role_fallback"] == 0
    assert "客户：" in row["confirmed_text"]
    with app.state.store.connection() as db:
        customer = db.execute("SELECT * FROM customers WHERE id=?", (row["customer_id"],)).fetchone()
    assert customer["name"] == "批量录音"
    analysis = alice.get(f"/api/analyses/{row['auto_analysis_id']}").json()
    assert analysis["status"] == "queued"
    assert analysis["request"]["transcription_id"] == tid
    assert analysis["request"]["customer_id"] == row["customer_id"]
    assert analysis["request"]["transcript"] == row["confirmed_text"]
    assert submitted == [row["auto_analysis_id"]]


def test_auto_audio_without_text_model_uses_single_customer_fallback(team, monkeypatch):
    app, admin, alice, bob = team
    app.state.settings.asr_key = "test-asr-key"
    app.state.settings.api_key = ""
    monkeypatch.setattr("sdr.audio.transcribe", lambda *a, **kw: (
        "客户说想下个月试用企业版，出图慢。", {"model": "test-asr", "tokens": 0}))
    monkeypatch.setattr(app.state.audio.pool, "submit", lambda *a: None)
    monkeypatch.setattr(app.state.engine, "submit", lambda aid: None)
    response = _upload(alice, "auto-audio-test-2", "fallback.wav")
    assert response.status_code == 202, response.text
    tid = response.json()["id"]
    app.state.audio.run(tid)
    row = alice.get(f"/api/transcriptions/{tid}").json()
    assert row["status"] == "confirmed"
    assert row["auto_role_fallback"] == 1
    assert row["confirmed_text"].startswith("客户：")
    assert row["auto_analysis_status"] == "submitted"


def test_auto_audio_can_use_existing_customer(team, monkeypatch):
    app, admin, alice, bob = team
    app.state.settings.asr_key = "test-asr-key"
    app.state.settings.api_key = "test-llm-key"
    monkeypatch.setattr("sdr.audio.transcribe", lambda *a, **kw: (
        "销售：您好\n客户：我们想下个月试用企业版软件。", {"model": "test-asr", "tokens": 0}))
    monkeypatch.setattr("sdr.audio.classify_roles", lambda *a, **kw: {
        "transcript": "销售：您好\n客户：我们想下个月试用企业版软件。",
        "turns": [], "uncertain_count": 0, "method": "text_context", "model": "test-llm"})
    monkeypatch.setattr(app.state.audio.pool, "submit", lambda *a: None)
    monkeypatch.setattr(app.state.engine, "submit", lambda aid: None)
    customer = alice.post("/api/customers", json={"name": "已有客户"}).json()
    response = alice.post("/api/transcriptions", params={
        "customer_id": customer["id"], "customer_mode": "existing", "auto_analyze": "true",
        "filename": "existing.wav", "idempotency_key": "auto-audio-existing", "data_mode": "synthetic",
    }, content=wav(), headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 202, response.text
    tid = response.json()["id"]
    app.state.audio.run(tid)
    row = alice.get(f"/api/transcriptions/{tid}").json()
    assert row["customer_id"] == customer["id"]
    assert row["auto_analysis_status"] == "submitted"
    analysis = alice.get(f"/api/analyses/{row['auto_analysis_id']}").json()
    assert analysis["request"]["customer_id"] == customer["id"]


def test_auto_audio_keeps_pending_label_and_excludes_it_from_facts(team, monkeypatch):
    from sdr.preprocess import split_turns

    app, admin, alice, bob = team
    app.state.settings.asr_key = "test-asr-key"
    app.state.settings.api_key = "test-llm-key"
    monkeypatch.setattr("sdr.audio.transcribe", lambda *a, **kw: (
        "销售：您好\n客户：我们做全屋定制\n待确认：这句听不清", {"model": "test-asr", "tokens": 0}))
    monkeypatch.setattr("sdr.audio.classify_roles", lambda *a, **kw: {
        "transcript": "销售：您好\n客户：我们做全屋定制\n待确认：这句听不清",
        "turns": [], "uncertain_count": 1, "method": "text_context", "model": "test-llm"})
    monkeypatch.setattr(app.state.audio.pool, "submit", lambda *a: None)
    monkeypatch.setattr(app.state.engine, "submit", lambda aid: None)
    response = _upload(alice, "auto-audio-pending", "pending.wav")
    assert response.status_code == 202, response.text
    tid = response.json()["id"]
    app.state.audio.run(tid)
    row = alice.get(f"/api/transcriptions/{tid}").json()
    assert "待确认：这句听不清" in row["confirmed_text"]
    assert row["provider"]["auto_pending_count"] == 1
    assert row["auto_role_fallback"] == 0
    turns = split_turns(row["confirmed_text"])
    assert any(turn.speaker == "pending" for turn in turns)
    business = [turn for turn in turns if turn.speaker not in ("other", "excluded", "pending")]
    assert all(turn.speaker != "pending" for turn in business)


def test_existing_customer_roles_ready_auto_analyzes_without_flag(team, monkeypatch):
    app, admin, alice, bob = team
    app.state.settings.asr_key = "test-asr-key"
    app.state.settings.api_key = "test-llm-key"
    monkeypatch.setattr("sdr.audio.transcribe", lambda *a, **kw: (
        "销售：您好\n客户：我们做全屋定制，出图慢，想下个月试用。", {"model": "test-asr", "tokens": 0}))
    monkeypatch.setattr("sdr.audio.classify_roles", lambda *a, **kw: {
        "transcript": "销售：您好\n客户：我们做全屋定制，出图慢，想下个月试用。",
        "turns": [], "uncertain_count": 0, "method": "text_context", "model": "test-llm"})
    monkeypatch.setattr(app.state.audio.pool, "submit", lambda *a: None)
    monkeypatch.setattr(app.state.engine, "submit", lambda aid: None)
    customer = alice.post("/api/customers", json={"name": "自动分析客户"}).json()
    response = alice.post("/api/transcriptions", params={
        "customer_id": customer["id"], "customer_mode": "existing",
        "filename": "no-flag.wav", "idempotency_key": "auto-no-flag", "data_mode": "synthetic",
    }, content=wav(), headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 202, response.text
    tid = response.json()["id"]
    app.state.audio.run(tid)
    row = alice.get(f"/api/transcriptions/{tid}").json()
    assert row["auto_analyze"] == 1
    assert row["auto_analysis_status"] == "submitted"
    assert row["auto_analysis_id"]
    assert row["auto_role_fallback"] == 0
