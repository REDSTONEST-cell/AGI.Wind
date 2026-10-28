import json

import httpx

from test_client import client_app, connect


async def test_provider_settings_require_credential_and_mask_the_key(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    bearer={"Authorization":"Bearer synthetic-private-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert (await client.get("/api/v1/settings/providers")).status_code==401
        empty=await client.get("/api/v1/settings/providers",headers=bearer)
        assert empty.status_code==200 and empty.json()["providers"]==[]
        missing=await client.put("/api/v1/settings/providers/openai",headers=bearer,json={"base_url":"https://api.example.invalid/v1"})
        assert missing.status_code==422 and missing.json()["error"]["code"]=="CREDENTIAL_REQUIRED"
        created=await client.put("/api/v1/settings/providers/openai",headers=bearer,json={"base_url":"https://api.example.invalid/v1","api_key":"sk-synthetic-secret","model_id":"example-model"})
        assert created.status_code==200,created.text
        body=created.json()
        assert body["credential_configured"] is True
        assert "sk-synthetic-secret" not in created.text
        assert body["models"]["dialogue"]["model_id"]=="example-model"
        listed=(await client.get("/api/v1/settings/providers",headers=bearer)).json()
        assert listed["providers"][0]["id"]=="openai" and listed["providers"][0]["credential_configured"] is True
        settings_file=json.loads((tmp_path/"settings.json").read_text(encoding="utf-8"))
        assert "sk-synthetic-secret" not in json.dumps(settings_file)
        assert settings_file["providers"]["openai"]["credential_ref"].startswith("NAGI_PROVIDER_")
        secrets_file=json.loads((tmp_path/"secrets.json").read_text(encoding="utf-8"))
        assert secrets_file["values"][settings_file["providers"]["openai"]["credential_ref"]]=="sk-synthetic-secret"


async def test_provider_settings_from_cookie_session_still_require_csrf(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        _,csrf=await connect(client)
        payload={"base_url":"https://api.example.invalid/v1","api_key":"sk-cookie-secret"}
        assert (await client.put("/api/v1/settings/providers/vendor",json=payload)).status_code==403
        evil=await client.put("/api/v1/settings/providers/vendor",json=payload,headers={"Origin":"https://untrusted.invalid","X-AGI-CSRF":csrf})
        assert evil.status_code==403
        good=await client.put("/api/v1/settings/providers/vendor",json=payload,headers={"Origin":"http://test","X-AGI-CSRF":csrf})
        assert good.status_code==200
        assert "sk-cookie-secret" not in good.text


async def test_model_role_requires_configured_provider(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    bearer={"Authorization":"Bearer synthetic-private-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert (await client.put("/api/v1/settings/models/dialogue",headers=bearer,json={"provider_id":"ghost","model_id":"m"})).status_code==422
        await client.put("/api/v1/settings/providers/ghost",headers=bearer,json={"base_url":"https://api.example.invalid/v1","api_key":"sk-ghost"})
        allowed=await client.put("/api/v1/settings/models/dialogue",headers=bearer,json={"provider_id":"ghost","model_id":"m"})
        assert allowed.status_code==200
        assert allowed.json()["dialogue"]["provider_id"]=="ghost"


async def test_delete_provider_removes_secret_and_model_binding(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    bearer={"Authorization":"Bearer synthetic-private-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        await client.put("/api/v1/settings/providers/openai",headers=bearer,json={"base_url":"https://api.example.invalid/v1","api_key":"sk-delete-me","model_id":"m"})
        removed=await client.delete("/api/v1/settings/providers/openai",headers=bearer)
        assert removed.status_code==200
        listed=(await client.get("/api/v1/settings/providers",headers=bearer)).json()
        assert listed["providers"]==[] and listed["models"]["dialogue"] is None
        assert (await client.delete("/api/v1/settings/providers/openai",headers=bearer)).status_code==404
        settings_file=json.loads((tmp_path/"settings.json").read_text(encoding="utf-8"))
        assert settings_file["providers"]=={}
        secrets_file=json.loads((tmp_path/"secrets.json").read_text(encoding="utf-8"))
        assert secrets_file["values"]=={}


async def test_capability_tabs_can_configure_asr_tts_rerank_embedding_and_vision_roles(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    bearer={"Authorization":"Bearer synthetic-private-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        created=await client.put("/api/v1/settings/providers/voice",headers=bearer,json={"base_url":"https://api.example.invalid/v1","api_key":"sk-voice","model_id":"tts-model","role":"tts","capabilities":["tts"]})
        assert created.status_code==200,created.text
        assert created.json()["models"]["tts"]["model_id"]=="tts-model"
        assert created.json()["capabilities"]==["tts"]
        for role in ("asr","rerank","embedding","vision"):
            updated=await client.put("/api/v1/settings/models/"+role,headers=bearer,json={"provider_id":"voice","model_id":role+"-model"})
            assert updated.status_code==200,updated.text
            assert updated.json()[role]["model_id"]==role+"-model"
        listed=(await client.get("/api/v1/settings/providers",headers=bearer)).json()
        assert set(listed["models"])>={"asr","tts","rerank","embedding","vision","dialogue"}
        invalid=await client.put("/api/v1/settings/models/secret_role",headers=bearer,json={"provider_id":"voice","model_id":"x"})
        assert invalid.status_code==422
        settings_file=json.loads((tmp_path/"settings.json").read_text(encoding="utf-8"))
        assert settings_file["models"]["tts"]["model_id"]=="tts-model"
        assert "sk-voice" not in json.dumps(settings_file)


async def test_memory_settings_can_be_read_updated_and_persisted(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    bearer={"Authorization":"Bearer synthetic-private-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        initial=await client.get("/api/v1/memory/settings",headers=bearer)
        assert initial.status_code==200
        assert initial.json()["extraction_enabled"] is True
        assert initial.json()["batch_events"]==8
        assert initial.json()["embedding"]=="keyword_only"
        updated=await client.patch("/api/v1/memory/settings",headers=bearer,json={
            "extraction_enabled":False,"batch_events":4,"max_candidates":5,
            "timeout_seconds":12,"maintenance_enabled":False,"maintenance_hours":48,
        })
        assert updated.status_code==200
        body=updated.json()
        assert body["extraction_enabled"] is False and body["batch_events"]==4
        assert body["max_candidates"]==5 and body["maintenance_hours"]==48
        assert app.state.service.memory_extraction_enabled is False
        stored=json.loads((tmp_path/"settings.json").read_text(encoding="utf-8"))
        assert stored["memory"]["memory_extraction_enabled"] is False
        assert stored["memory"]["memory_extract_batch_events"]==4
        from nagi.core.settings import Settings,apply_persisted_settings
        reloaded=apply_persisted_settings(Settings(data_dir=tmp_path),tmp_path)
        assert reloaded.memory_extraction_enabled is False
        assert reloaded.memory_extract_batch_events==4
        assert reloaded.memory_maintenance_hours==48
