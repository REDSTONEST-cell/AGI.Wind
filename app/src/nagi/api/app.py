from contextlib import asynccontextmanager
import asyncio
import base64
from collections import defaultdict, deque
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any,Literal
from uuid import UUID,uuid4
from fastapi import FastAPI,Depends,UploadFile,File,Header,Query,Request,Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse,StreamingResponse,FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine,select,text,func
from pydantic import Field,ValidationError,SecretStr
from ..core.settings import Settings,load_settings,apply_persisted_settings,persist_settings,ProviderSettings,ProactiveSettings,SETTINGS_WRITE_LOCK
from ..core.secrets import SecretStore,credential_ref_for
from ..core.types import DTO,Actor,ChatRequest,ModelRef,Scope
from ..core.errors import AppError
from ..archive import models as m
from ..archive.repository import Database,authorized_conversation,lock
from ..archive.assets import AssetStore
from ..archive.stickers import StickerService, attachment
from ..archive.exports import ExportService
from ..archive.imports import ImportService
from ..archive.deletions import DeletionLedger,DeletionService
from ..identity.service import IdentityService,authorize
from ..memory.retrieval import MemoryService
from ..memory.corrections import CorrectionService
from ..memory.extraction import Extractor
from ..memory.jobs import MEMBER_EXTRACT, MEMORY_EXTRACT, enqueue_manual_extraction, run_extraction, run_member_extraction
from ..memory.lifecycle import maintain as maintain_memories
from ..memory.consolidation import MEMORY_CONSOLIDATE, enqueue_consolidation, run_consolidation, SUMMARY_PROMPT
from ..memory.autobiography import AutobiographyConfig,AutobiographyOrganizer,GROUPINGS,SqlAutobiographyWriter
from ..memory.budget import MemoryBudget
from ..delivery.service import GenerationService,generation_scope,TERMINAL
from ..delivery.proactive import ProactiveService
from ..delivery.idle_cognition import IdleCognitionService
from ..delivery.stream import encode_sse
from ..delivery import models as dm
from ..voice.service import VoiceService
from ..jobs.worker import Worker
from .auth import bearer_auth
from .browser import BrowserSessions
from .logs import SystemLogReader
from ..usage import ProcessMonitor,usage_overview

class ConversationCreate(DTO): companion_id:UUID; title:str=Field(default="",max_length=200)
class ConversationUpdate(DTO):
    title:str|None=Field(default=None,max_length=200)
    state:Literal["active","archived","hidden"]|None=None
class TicketLogin(DTO): ticket:str
class KeyLogin(DTO): key:str=Field(min_length=1,max_length=2000)
class ProviderUpsert(DTO):
    base_url:str=Field(min_length=1,max_length=2000)
    protocol:Literal["openai_compatible","vendor_native"]="openai_compatible"
    supported_parameters:list[str]=Field(default_factory=list)
    capabilities:list[Literal["text","image","streaming","embedding","asr","tts","rerank"]]=Field(default_factory=lambda:["text","streaming"])
    timeout_seconds:float=Field(default=60,gt=0,le=600)
    api_key:str|None=Field(default=None,max_length=4000)
    model_id:str|None=Field(default=None,max_length=200)
    model_parameters:dict[str,Any]=Field(default_factory=dict)
    role:Literal["dialogue","vision","memory_extract","memory_reflect","embedding","asr","tts","rerank"]|None=None
class ModelRoleUpdate(DTO):
    provider_id:str=Field(min_length=1,max_length=200)
    model_id:str=Field(min_length=1,max_length=200)
    parameters:dict[str,Any]=Field(default_factory=dict)
class PersonaPatch(DTO): expected_version:int=Field(ge=1); patch:dict[str,Any]
class InterruptRequest(DTO): command_id:UUID
class SearchRequest(DTO): companion_id:UUID; conversation_id:UUID; query:str=Field(min_length=1,max_length=2000)
class CorrectionRequest(DTO): memory_id:UUID; correction_text:str; source_event_id:UUID
class MemoryItemPatch(DTO):
    status:Literal["active","hidden"]|None=None
    importance:float|None=Field(default=None,ge=0,le=1)
    is_pinned:bool|None=None
    ttl_days:float|None=Field(default=None,ge=1,le=3650)
class MemoryExtractionRequest(DTO): companion_id:UUID
class MemorySummaryRequest(DTO):
    companion_id:UUID
    level:Literal['daily','weekly']='daily'
class RecallTestRequest(DTO): companion_id:UUID; query:str=Field(min_length=1,max_length=500)
class AutobiographyPreviewRequest(DTO):
    companion_id:UUID
    group_by:Literal['period','kind','subject']|None=None
class AutobiographyActionRequest(DTO): companion_id:UUID
class MemorySettingsPatch(DTO):
    extraction_enabled:bool|None=None
    batch_events:int|None=Field(default=None,ge=1,le=32)
    max_candidates:int|None=Field(default=None,ge=1,le=8)
    timeout_seconds:float|None=Field(default=None,gt=0,le=300)
    maintenance_enabled:bool|None=None
    maintenance_hours:float|None=Field(default=None,gt=0,le=720)
    semantic_enabled:bool|None=None
    external_embedding_enabled:bool|None=None
    external_embedding_dimension:int|None=Field(default=None,ge=8,le=4096)
    rerank_enabled:bool|None=None
    retention_days:dict[str,float]|None=None
    daily_budget_usd:float|None=Field(default=None,ge=0,le=100)
    daily_token_limit:int|None=Field(default=None,ge=0,le=100_000_000)
    input_price_per_million:float|None=Field(default=None,ge=0,le=1000)
    output_price_per_million:float|None=Field(default=None,ge=0,le=1000)
    max_output_tokens:int|None=Field(default=None,ge=256,le=8192)
    consolidation_enabled:bool|None=None
    worker_mode:Literal['gateway','separate']|None=None
    autobiography_enabled:bool|None=None
class MemoryProbe(DTO):
    model_id:str|None=Field(default=None,max_length=200)
    parameters:dict[str,Any]=Field(default_factory=dict)
class VoiceSettingsPatch(DTO):
    enabled:bool|None=None
    base_url:str|None=Field(default=None,max_length=2000)
    voice_id:str|None=Field(default=None,max_length=64)
    probability:float|None=Field(default=None,ge=0,le=1)
    text_break_probability:float|None=Field(default=None,ge=0,le=1)
    max_chars:int|None=Field(default=None,ge=20,le=1000)
class VoicePreviewRequest(DTO):
    text:str=Field(default="哥哥，今天也辛苦啦。",max_length=200)
    emotion:Literal["开心","平静","难过","担心","撒娇","生气","惊讶","温柔"]|None=None
class ProactiveSettingsPatch(DTO):
    enabled:bool|None=None
    min_idle_minutes:int|None=Field(default=None,ge=5,le=1440)
    min_gap_minutes:int|None=Field(default=None,ge=5,le=1440)
    max_per_day:int|None=Field(default=None,ge=1,le=20)
    quiet_start:str|None=Field(default=None,max_length=5)
    quiet_end:str|None=Field(default=None,max_length=5)
    after_reply_enabled:bool|None=None
    after_reply_qq_enabled:bool|None=None
    after_reply_tools_enabled:bool|None=None
    after_reply_media_enabled:bool|None=None
    after_reply_max_tool_calls:int|None=Field(default=None,ge=0,le=3)
    after_reply_delay_seconds:int|None=Field(default=None,ge=30,le=3600)
    after_reply_max_rounds:int|None=Field(default=None,ge=1,le=3)
    after_reply_max_steps:int|None=Field(default=None,ge=1,le=8)
    after_reply_max_runs_per_day:int|None=Field(default=None,ge=1,le=20)
    continuation_enabled:bool|None=None
    continuation_max_rounds:int|None=Field(default=None,ge=1,le=12)
    continuation_max_messages:int|None=Field(default=None,ge=1,le=5)
    continuation_max_wait_seconds:int|None=Field(default=None,ge=10,le=3600)
    continuation_lifetime_minutes:int|None=Field(default=None,ge=1,le=120)
class ProactiveSendRequest(DTO): companion_id:UUID
class ExportRequest(DTO): companion_id:UUID
class StickerCreate(DTO):
    companion_id:UUID
    asset_id:UUID
    name:str=Field(default='',max_length=200)
    tags:list[str]=Field(default_factory=list,max_length=20)
class StickerPatch(DTO):
    name:str=Field(default='',max_length=200)
    tags:list[str]=Field(default_factory=list,max_length=20)
    state:Literal['active','hidden']='active'

def create_app(settings:Settings,db:Database,actor:Actor,generation_service=None,settings_store:SecretStore|None=None):
    settings=apply_persisted_settings(settings)
    store=settings_store or SecretStore(settings.data_dir)
    log_reader=SystemLogReader(settings.data_dir)
    process_monitor=ProcessMonitor()
    logger=logging.getLogger("nagi")
    if not getattr(logger,"_nagi_console",False):
        handler=logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s","%H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate=False
        logger._nagi_console=True
    assets=AssetStore(db,settings.data_dir/"assets",max_bytes=settings.max_audio_bytes,max_image_bytes=settings.max_image_bytes)
    service=generation_service or GenerationService(db,None,runtime_name=settings.runtime.preferred)
    if getattr(service,"voice",None) is None:
        service.voice=VoiceService(db,assets,lambda: settings.voice)
    if getattr(service,"quota",None) is None:
        from ..usage.service import QuotaService
        service.quota=QuotaService(settings.usage)
    voice=service.voice
    identity=IdentityService(db)
    stickers=StickerService(db)
    ledger=DeletionLedger(settings.data_dir/"governance")
    deletion=DeletionService(db,ledger,assets.root)
    autobiography_writer=SqlAutobiographyWriter(db,retention_days=settings.memory_retention_days)
    autobiography_organizers={}
    autobiography_lock=threading.Lock()

    def autobiography_scope(current,companion_id):
        # Autobiographies are strictly owner-private; group/scene callers never reach here.
        return {'account_id':str(current.account_id),'user_id':str(current.user_id),
                'companion_id':str(companion_id),'pool_kind':'owner','visibility':'owner_private'}

    def autobiography_organizer(current,companion_id):
        key=(current.account_id,current.user_id,companion_id)
        with autobiography_lock:
            organizer=autobiography_organizers.get(key)
            if organizer is None:
                organizer=AutobiographyOrganizer(autobiography_writer,
                    scope=autobiography_scope(current,companion_id),config=AutobiographyConfig())
                autobiography_organizers[key]=organizer
            return organizer

    def autobiography_require(current,companion_id):
        if not settings.memory_autobiography_enabled:
            raise AppError('AUTOBIOGRAPHY_DISABLED','自传整理功能未启用',403)
        with db.engine.connect() as conn:
            authorize(conn,current,companion_id)
        return autobiography_organizer(current,companion_id)

    def json_payload(text):
        value=(text or '').strip()
        if value.startswith('```'):
            value=value[3:].strip()
            if value[:4].lower()=='json': value=value[4:].strip()
            if value.endswith('```'): value=value[:-3].strip()
        try: return json.loads(value)
        except Exception:
            start,end=value.find('{'),value.rfind('}')
            if start>=0 and end>start: return json.loads(value[start:end+1])
            raise

    async def auto_tag_asset(actor,asset_id,fallback_name):
        """Use the configured vision model to name and tag a manually uploaded sticker."""
        vision=getattr(service,'vision',None)
        if vision is None or getattr(vision,'model',None) is None: return None
        try:
            record,path=assets.get(actor,asset_id)
            if not record['mime'].startswith('image/'): return None
            prompt=('为一个表情包生成简短中文名称和 2-5 个中文短标签（画面主题、情绪、动作或梗）。'
                    '只输出严格JSON：{"name":"名称","tags":["标签1","标签2"]}，不要解释。')
            parts=[{'type':'text','text':prompt},{'type':'image_url','image_url':{'url':'data:'+record['mime']+';base64,'+base64.b64encode(path.read_bytes()).decode()}}]
            raw=await vision.complete(vision.model,[{'role':'user','content':parts}])
            value=json_payload(raw)
            tags=[]
            for item in value.get('tags') or []:
                tag=str(item).strip()[:80]
                if tag and tag not in tags: tags.append(tag)
            name=str(value.get('name') or '').strip()[:200]
            if not tags: return None
            return {'tags':tags[:5],'name':name or fallback_name}
        except Exception:
            logger.warning('sticker auto-tag failed asset=%s',asset_id,exc_info=True)
            return None

    authenticate_bearer=bearer_auth(settings,actor)
    sessions=BrowserSessions(persist_path=settings.data_dir/"browser-sessions.json")
    key_attempts=defaultdict(deque)
    client_dir=Path(__file__).with_name("client")

    def same_origin(request):
        """Accept the exact request origin, or an explicitly configured public origin.

        TLS normally terminates at the reverse proxy, so FastAPI sees an internal
        ``http://...`` URL while the browser sends the public ``https://...`` Origin.
        Comparing only the internal URL would reject the legitimate login. The
        configured allow-list is trusted and is also what CORS uses.
        """
        origin=request.headers.get("origin","").rstrip("/")
        if not origin: return False
        if origin==f"{request.url.scheme}://{request.url.netloc}".rstrip("/"): return True
        configured={item.rstrip("/") for item in getattr(settings,"allowed_origins",[]) if item}
        return origin in configured

    def is_loopback(request):
        """True only for a request delivered over loopback with a loopback Host header.

        The Host check blocks DNS-rebinding pages whose Host still names an external domain.
        """
        client=getattr(request,"client",None)
        if client is None or client.host not in {"127.0.0.1","::1"}: return False
        hostname=request.headers.get("host","").split(":")[0].strip("[]").lower()
        return hostname in {"127.0.0.1","localhost","::1"}

    def session_cookie_response(request,session_id,csrf):
        response=JSONResponse({"csrf_token":csrf})
        response.set_cookie(BrowserSessions.SESSION_COOKIE,session_id,httponly=True,samesite="strict",max_age=int(sessions.session_ttl),path="/",secure=request.url.scheme=="https")
        return response

    def log_secrets():
        values=[]
        if settings.access_token: values.append(settings.access_token.get_secret_value())
        if settings.client_key: values.append(settings.client_key.get_secret_value())
        for provider in settings.providers.values():
            value=store.get(provider.credential_ref)
            if value: values.append(value)
        qq_token=store.get('NAGI_QQ_ONEBOT_TOKEN')
        if qq_token: values.append(qq_token)
        return tuple(values)

    async def authenticate(request:Request):
        # Long-lived Bearer stays for scripts/tests; browsers use the HttpOnly session cookie.
        authorization=request.headers.get("authorization")
        if authorization is not None:
            return authenticate_bearer(authorization=authorization)
        csrf=sessions.validate(request.cookies.get(BrowserSessions.SESSION_COOKIE))
        if csrf is None: raise AppError("UNAUTHENTICATED","请提供有效访问凭据",401)
        if request.method not in ("GET","HEAD","OPTIONS"):
            if not same_origin(request): raise AppError("FORBIDDEN","跨站请求被拒绝",403)
            if not secrets.compare_digest(request.headers.get("x-agi-csrf",""),csrf): raise AppError("FORBIDDEN","CSRF 校验失败",403)
        return actor

    wakeup=asyncio.Event()
    service.notify=wakeup.set
    proactive=ProactiveService(db,settings,service)
    idle_cognition=IdleCognitionService(db,settings,service,proactive=proactive)
    proactive.idle_cognition=idle_cognition
    service.idle_cognition=idle_cognition
    from ..purpose.scheduler import PurposeScheduler
    purpose_scheduler=PurposeScheduler(db,settings,actor,idle_cognition)

    @asynccontextmanager
    async def lifespan(app):
        deletion.replay()
        # A process restart never automatically repeats a possibly paid request.
        with db.engine.begin() as conn:
            conn.execute(m.generations.update().where(m.generations.c.state.in_(["running","draining"])).values(state="interrupted",error_code="PROCESS_RESTART",delivery_state="stopped"))
        cleanup=getattr(voice,"cleanup_on_startup",None)
        if cleanup: cleanup()
        worker=Worker(db)
        closing=asyncio.Event()
        async def loop():
            async def handle(job):
                if job["event_type"]=="generation.requested":
                    generation_id=UUID(job["payload"]["generation_id"])
                    ran=await asyncio.create_task(service.run(generation_id))
                    with db.engine.connect() as conn: state=conn.scalar(select(m.generations.c.state).where(m.generations.c.id==generation_id))
                    if not ran and state=="pending": raise AppError("GENERATION_QUEUED","同一角色正在回答",429)
                    return
                if job["event_type"]==MEMORY_EXTRACT:
                    # Extraction is async and budgeted; failure only retries this job and never
                    # touches the already archived chat turn.
                    return await run_extraction(db,getattr(service,"memory_extractor",None),worker,job,batch_events=settings.memory_extract_batch_events,settings=settings)
                if job["event_type"]==MEMBER_EXTRACT:
                    return await run_member_extraction(db,getattr(service,"memory_extractor",None),worker,job,batch_events=settings.memory_extract_batch_events,settings=settings)
                if job['event_type'] == MEMORY_CONSOLIDATE:
                    return await run_consolidation(db,getattr(service,'memory_summary_extractor',None),worker,job,settings=settings)
                raise AppError("HANDLER_UNCONFIGURED","任务处理器未配置",503)
            while not closing.is_set():
                event_types=['generation.requested']
                if settings.memory_worker_mode == 'gateway':
                    if settings.memory_extraction_enabled:
                        event_types.append(MEMORY_EXTRACT)
                        event_types.append(MEMBER_EXTRACT)
                    if settings.memory_consolidation_enabled: event_types.append(MEMORY_CONSOLIDATE)
                jobs=worker.claim("gateway",batch_size=4,event_types=event_types)
                if jobs:
                    for job in jobs:
                        if closing.is_set(): break
                        await worker.run_job(job["id"],"gateway",handle)
                else:
                    wakeup.clear()
                    try: await asyncio.wait_for(wakeup.wait(),timeout=30)
                    except TimeoutError: pass
        task=asyncio.create_task(loop())
        async def memory_maintenance_loop():
            background_state={}
            while not closing.is_set():
                if settings.memory_worker_mode == 'gateway':
                    try:
                        from ..memory.runtime import background_tick
                        await background_tick(db,settings,actor,background_state)
                        wakeup.set()
                    except Exception:
                        logger.warning("memory lifecycle maintenance failed",exc_info=True)
                try:
                    await asyncio.wait_for(closing.wait(),timeout=30)
                except TimeoutError:
                    pass
        maintenance_task=asyncio.create_task(memory_maintenance_loop())
        async def proactive_loop():
            while not closing.is_set():
                if settings.proactive.enabled:
                    try:
                        await proactive.tick()
                    except Exception:
                        logger.warning("proactive tick failed",exc_info=True)
                    timeout=60
                else:
                    timeout=300
                try:
                    await asyncio.wait_for(closing.wait(),timeout=timeout)
                except TimeoutError:
                    pass
        proactive_task=asyncio.create_task(proactive_loop())
        async def idle_cognition_loop():
            while not closing.is_set():
                if settings.proactive.enabled:
                    try:
                        await idle_cognition.tick()
                    except Exception:
                        logger.warning("idle cognition tick failed",exc_info=True)
                    timeout=10
                else:
                    timeout=60
                try:
                    await asyncio.wait_for(closing.wait(),timeout=timeout)
                except TimeoutError:
                    pass
        idle_cognition_task=asyncio.create_task(idle_cognition_loop())
        purpose_task=asyncio.create_task(purpose_scheduler.run())
        try: yield
        finally:
            closing.set(); wakeup.set()
            purpose_scheduler.stop()
            await app.state.qq_bridge.close()
            for pending in (task,maintenance_task,proactive_task,idle_cognition_task,purpose_task):
                pending.cancel()
            await asyncio.gather(task,maintenance_task,proactive_task,idle_cognition_task,purpose_task,return_exceptions=True)
            close_voice=getattr(voice,"close",None)
            if close_voice: await close_voice()
            if service.provider and hasattr(service.provider,"aclose"): await service.provider.aclose()
            registry=getattr(service,"registry",None)
            if registry: await registry.aclose()

    app=FastAPI(title="Nagi Gateway",version="0.1.0",lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    if settings.allowed_origins:
        # Standalone clients authenticate with a Bearer client key; cookie credentials are
        # intentionally not shared across origins.
        app.add_middleware(CORSMiddleware,allow_origins=list(settings.allowed_origins),allow_methods=["*"],allow_headers=["*"],allow_credentials=False,max_age=600)
    app.state.db,app.state.service,app.state.assets=db,service,assets
    app.state.voice=voice
    app.state.proactive=proactive
    app.state.idle_cognition=idle_cognition
    app.state.browser_sessions=sessions
    app.state.settings=settings
    db.memory_settings=settings
    app.state.autobiography_writer=autobiography_writer
    app.state.autobiography_organizers=autobiography_organizers
    app.state.actor=actor
    app.state.settings_store=store
    app.mount("/client",StaticFiles(directory=client_dir),name="client")

    @app.middleware("http")
    async def client_assets_revalidate(request,call_next):
        # Static ES modules are updated in place by local development; never let a browser
        # reuse an old module while index.html/app.js are already new.
        response=await call_next(request)
        if request.url.path.startswith("/client/"):
            response.headers["Cache-Control"]="no-cache, must-revalidate"
        return response

    def provider_view(provider_id):
        config=settings.providers[provider_id]
        models={}
        for role in type(settings.models).model_fields:
            reference=getattr(settings.models,role)
            if reference is not None and reference.provider_id==provider_id: models[role]=reference.model_dump(mode="json")
        return {"id":provider_id,"base_url":config.base_url,"protocol":config.protocol,"supported_parameters":list(config.supported_parameters),"capabilities":list(config.capabilities),"timeout_seconds":config.timeout_seconds,"credential_ref":config.credential_ref,"credential_configured":store.configured(config.credential_ref),"credential_last4":store.last4(config.credential_ref),"models":models}

    def refresh_runtime():
        callback=getattr(app.state,"apply_settings",None)
        if callback is not None: callback()

    def embedding_status():
        try:
            from ..memory.semantic import semantic_status
            return semantic_status(db)
        except ImportError:
            return 'keyword_only'

    def memory_settings_view():
        from ..memory.runtime import worker_status
        from ..memory.external import external_embedding_status
        from ..memory.rerank import rerank_status
        return {
            "extraction_enabled": settings.memory_extraction_enabled,
            "batch_events": settings.memory_extract_batch_events,
            "max_candidates": settings.memory_extract_max_candidates,
            "timeout_seconds": settings.memory_extract_timeout_seconds,
            "maintenance_enabled": settings.memory_maintenance_enabled,
            "maintenance_hours": settings.memory_maintenance_hours,
            "extractor": getattr(app.state,"memory_extractor_source","unconfigured"),
            'embedding': embedding_status(),
            'semantic_enabled':settings.memory_semantic_enabled,
            'external_embedding':external_embedding_status(settings),
            'rerank':rerank_status(settings),
            'retention_days':settings.memory_retention_days,
            'daily_budget_usd':settings.memory_daily_budget_usd,
            'daily_token_limit':settings.memory_daily_token_limit,
            'input_price_per_million':settings.memory_input_price_per_million,
            'output_price_per_million':settings.memory_output_price_per_million,
            'max_output_tokens':settings.memory_extract_max_output_tokens,
            'consolidation_enabled':settings.memory_consolidation_enabled,
            'worker_mode':settings.memory_worker_mode,
            'autobiography_enabled':settings.memory_autobiography_enabled,
            'worker':worker_status(settings),
            'budget':MemoryBudget(db,settings).stats(actor.account_id),
        }

    async def voice_settings_view(force_voices=False):
        health={"available":False,"status":"disabled","sample_rate":24000}
        voices=[]
        if settings.voice.enabled:
            try: health=await voice.health()
            except Exception: health={"available":False,"status":"unreachable","sample_rate":24000}
            try: voices=await voice.list_voices(force=force_voices)
            except Exception: voices=[]
        return {
            "enabled":settings.voice.enabled,
            "base_url":settings.voice.base_url,
            "voice_id":settings.voice.voice_id,
            "voice_version":settings.voice.voice_version,
            "probability":settings.voice.probability,
            "text_break_probability":settings.voice.text_break_probability,
            "max_chars":settings.voice.max_chars,
            "timeout_seconds":settings.voice.timeout_seconds,
            "available":bool(health.get("available")),
            "status":health.get("status") or ("ready" if health.get("available") else "unreachable"),
            "sample_rate":int(health.get("sample_rate") or 24000),
            "voices":voices,
            "active_voice_id":settings.voice.voice_id,
        }

    @app.middleware("http")
    async def contain_internal_errors(request,call_next):
        try: return await call_next(request)
        except Exception:
            # Do not let the server print upstream/DB exception details containing private rows,
            # but keep the stack trace in the gateway console for diagnosis.
            logger.exception("unhandled error on %s %s",request.method,request.url.path)
            return JSONResponse({"error":{"code":"INTERNAL_ERROR","message":"请求未完成，请检查服务状态","retryable":False}},status_code=500)

    @app.middleware("http")
    async def log_write_requests(request,call_next):
        """Structured write-request audit line (no bodies, keys or paths with secrets)."""
        if request.method not in ("POST","PATCH","PUT","DELETE"):
            return await call_next(request)
        started=time.perf_counter()
        response=await call_next(request)
        logger.info("http %s %s -> %s %.0fms",request.method,request.url.path,response.status_code,(time.perf_counter()-started)*1000)
        return response

    @app.exception_handler(AppError)
    async def error(request,exc): return JSONResponse(exc.public(),status_code=exc.status)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc): return JSONResponse({"error":{"code":"INVALID_REQUEST","message":"请求字段或参数无效","retryable":False}},status_code=422)
    @app.exception_handler(Exception)
    async def internal_error(request,exc): return JSONResponse({"error":{"code":"INTERNAL_ERROR","message":"请求未完成，请检查服务状态","retryable":False}},status_code=500)

    @app.get("/",include_in_schema=False)
    def client_home():
        return FileResponse(client_dir/"index.html",media_type="text/html; charset=utf-8",headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})

    @app.get("/status",include_in_schema=False)
    def browser_status():
        return FileResponse(Path(__file__).with_name("home.html"),media_type="text/html; charset=utf-8",headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})

    @app.post("/api/v1/client/launch")
    def client_launch(current=Depends(authenticate_bearer)):
        """Exchange the long-lived Bearer token for a one-time browser login ticket."""
        return {"ticket":sessions.issue_ticket()}

    @app.post("/api/v1/client/session")
    def client_session_exchange(body:TicketLogin,request:Request):
        if not same_origin(request): raise AppError("FORBIDDEN","来源不可信",403)
        issued=sessions.exchange(body.ticket)
        if issued is None: raise AppError("UNAUTHENTICATED","登录票据无效或已使用",401)
        session_id,csrf=issued
        return session_cookie_response(request,session_id,csrf)

    @app.post("/api/v1/client/bootstrap")
    def client_bootstrap(request:Request):
        """Loopback-only automatic session so the local user never pastes the token."""
        csrf=sessions.validate(request.cookies.get(BrowserSessions.SESSION_COOKIE))
        if csrf is not None: return {"csrf_token":csrf}
        if not (settings.local_auto_login and is_loopback(request)):
            raise AppError("AUTH_REQUIRED","需要访问令牌建立浏览器会话",401)
        session_id,new_csrf=sessions.create()
        return session_cookie_response(request,session_id,new_csrf)

    @app.post("/api/v1/client/key-session")
    def client_key_session(body:KeyLogin,request:Request):
        """Exchange the short client key for a same-origin cookie session (server/client split)."""
        if not same_origin(request): raise AppError("FORBIDDEN","来源不可信",403)
        client=getattr(request,"client",None)
        ip=client.host if client else "unknown"
        now=time.monotonic()
        attempts=key_attempts.get(ip)
        if attempts is None:
            attempts=deque()
            key_attempts[ip]=attempts
        while attempts and now-attempts[0]>60: attempts.popleft()
        if not attempts:
            # Do not leak idle IP keys after their 60s window has expired.
            if key_attempts.get(ip) is attempts: key_attempts.pop(ip,None)
            attempts=deque()
        if len(attempts)>=5: raise AppError("RATE_LIMITED","尝试次数过多，请一分钟后再试",429)
        expected=settings.client_key.get_secret_value() if settings.client_key else ""
        if not expected or not secrets.compare_digest(body.key,expected):
            attempts.append(now)
            key_attempts[ip]=attempts
            raise AppError("UNAUTHENTICATED","客户端密钥不正确",401)
        attempts.clear()
        key_attempts.pop(ip,None)
        session_id,csrf=sessions.create()
        logger.info("client key session created from %s",ip)
        return session_cookie_response(request,session_id,csrf)

    @app.get("/api/v1/client/session")
    def client_session_state(request:Request,current=Depends(authenticate)):
        return {"csrf_token":sessions.validate(request.cookies.get(BrowserSessions.SESSION_COOKIE)) or ""}

    @app.delete("/api/v1/client/session")
    def client_logout(request:Request,current=Depends(authenticate)):
        sessions.revoke(request.cookies.get(BrowserSessions.SESSION_COOKIE))
        response=JSONResponse({"status":"signed_out"})
        response.delete_cookie(BrowserSessions.SESSION_COOKIE,path="/")
        return response

    @app.get("/health/live")
    def live(): return {"status":"live"}
    @app.post("/api/v1/admin/shutdown")
    async def shutdown(current=Depends(authenticate)):
        callback=getattr(app.state,"request_shutdown",None)
        if callback is None: raise AppError("NOT_MANAGED","当前实例不支持托管停止",409)
        callback()
        return {"status":"stopping"}
    @app.get("/health/ready")
    async def ready():
        try:
            with db.engine.connect() as conn: conn.execute(text("SELECT 1"))
            database="available"
        except Exception: database="unavailable"
        dialogue="configured" if service.provider and service.model else "unconfigured"
        tts_state="unconfigured"
        if getattr(voice,"enabled",False):
            try: tts_state="available" if (await voice.health()).get("available") else "unavailable"
            except Exception: tts_state="unavailable"
        dependencies={"database":database,"assets":"available" if assets.root.is_dir() else "unavailable","dialogue":dialogue,"runtime":service.runtime_name,"memory_extraction":getattr(app.state,"memory_extractor_source","unconfigured"),"tts":tts_state,"asr":"unconfigured","embedding":embedding_status(),"letta":"not_verified"}
        usable=database=="available" and dialogue=="configured"
        return JSONResponse({"status":"ready" if usable else "degraded","dependencies":dependencies},status_code=200 if usable else 503)

    @app.get("/api/v1/capabilities")
    async def capabilities(current=Depends(authenticate)):
        tts=False
        if getattr(voice,"enabled",False):
            try: tts=bool((await voice.health()).get("available"))
            except Exception: tts=False
        return {"protocol_version":"1.0","text":service.provider is not None,"image":service.vision is not None,"streaming":True,"tts":tts,"asr":bool((await app.state.asr_health()).get('available')),"live2d":False,"runtime":service.runtime_name,"embedding":embedding_status(),"memory_extraction":getattr(app.state,"memory_extractor_source","unconfigured"),"voice_training":"ready" if tts else "unconfigured","voice_id":settings.voice.voice_id if tts else None,"archive_format_version":4}

    @app.get("/api/v1/system-logs")
    def system_logs(tail:int=Query(default=300,ge=1,le=1000),stdout_offset:int|None=Query(default=None,ge=0),stderr_offset:int|None=Query(default=None,ge=0),current=Depends(authenticate)):
        """Tail the gateway console logs for the browser's terminal drawer."""
        return log_reader.read(tail=tail,stdout_offset=stdout_offset,stderr_offset=stderr_offset,secrets=log_secrets())

    @app.get("/api/v1/usage/stats")
    def usage_stats(current=Depends(authenticate)):
        """Read-only usage cards: messages, model calls, estimated tokens, voice, tools."""
        with db.engine.connect() as conn:
            return usage_overview(conn,current.account_id,monitor=process_monitor)

    @app.get("/api/v1/settings/providers")
    def list_providers(current=Depends(authenticate)):
        models={role:(None if getattr(settings.models,role) is None else getattr(settings.models,role).model_dump(mode="json")) for role in type(settings.models).model_fields}
        return {"providers":[provider_view(key) for key in sorted(settings.providers)],"models":models}

    @app.put("/api/v1/settings/providers/{provider_id}")
    def upsert_provider(provider_id:str,body:ProviderUpsert,current=Depends(authenticate)):
        if not provider_id.replace("_","").replace("-","").isalnum(): raise AppError("INVALID_PROVIDER","服务商 ID 只能包含字母、数字、下划线与连字符",422)
        existing=settings.providers.get(provider_id)
        credential_ref=existing.credential_ref if existing else credential_ref_for(provider_id)
        if body.api_key is not None and not body.api_key.strip(): raise AppError("INVALID_CREDENTIAL","API Key 不能为空",422)
        if body.api_key: store.set(credential_ref,body.api_key.strip())
        if not store.configured(credential_ref): raise AppError("CREDENTIAL_REQUIRED","首次配置需要填写 API Key",422)
        config=ProviderSettings(base_url=body.base_url,credential_ref=credential_ref,protocol=body.protocol,supported_parameters=body.supported_parameters,capabilities=body.capabilities,timeout_seconds=body.timeout_seconds)
        model=None
        if body.model_id:
            model=ModelRef(provider_id=provider_id,model_id=body.model_id,parameters=body.model_parameters)
            if set(model.parameters)-set(config.supported_parameters): raise AppError("UNSUPPORTED_PARAMETER","厂商不支持指定参数",422)
        with SETTINGS_WRITE_LOCK:
            settings.providers[provider_id]=config
            if model is not None: setattr(settings.models,body.role or "dialogue",model)
            logger.info("provider %s saved",provider_id)
            persist_settings(settings)
        refresh_runtime()
        return provider_view(provider_id)

    @app.delete("/api/v1/settings/providers/{provider_id}")
    def delete_provider(provider_id:str,current=Depends(authenticate)):
        config=settings.providers.pop(provider_id,None)
        if config is None: raise AppError("NOT_FOUND","服务商不存在",404)
        with SETTINGS_WRITE_LOCK:
            store.delete(config.credential_ref)
            for role in type(settings.models).model_fields:
                reference=getattr(settings.models,role)
                if reference is not None and reference.provider_id==provider_id: setattr(settings.models,role,None)
            persist_settings(settings)
        refresh_runtime()
        logger.info("provider %s deleted",provider_id)
        return {"deleted":provider_id}

    @app.put("/api/v1/settings/models/{role}")
    def set_model_role(role:Literal["dialogue","vision","memory_extract","memory_reflect","embedding","asr","tts","rerank"],body:ModelRoleUpdate,current=Depends(authenticate)):
        config=settings.providers.get(body.provider_id)
        if config is None and body.provider_id!="local": raise AppError("UNKNOWN_PROVIDER","请先配置服务商",422)
        if config is not None and set(body.parameters)-set(config.supported_parameters): raise AppError("UNSUPPORTED_PARAMETER","厂商不支持指定参数",422)
        with SETTINGS_WRITE_LOCK:
            setattr(settings.models,role,ModelRef(provider_id=body.provider_id,model_id=body.model_id,parameters=body.parameters))
            persist_settings(settings)
        refresh_runtime()
        logger.info("model role %s -> %s/%s",role,body.provider_id,body.model_id)
        return {role:getattr(settings.models,role).model_dump(mode="json")}

    @app.post("/api/v1/settings/providers/{provider_id}/test")
    async def test_provider(provider_id:str,current=Depends(authenticate)):
        config=settings.providers.get(provider_id)
        if config is None: raise AppError("NOT_FOUND","服务商不存在",404)
        credential=store.get(config.credential_ref)
        if not credential: raise AppError("CREDENTIAL_REQUIRED","尚未保存 API Key",409)
        import httpx
        try:
            async with httpx.AsyncClient(timeout=min(config.timeout_seconds,8.0)) as probe:
                response=await probe.get(config.base_url.rstrip("/")+"/models",headers={"Authorization":"Bearer "+credential})
                if response.status_code>=400:
                    fallback_model=next((getattr(settings.models,role).model_id for role in type(settings.models).model_fields if getattr(settings.models,role) is not None and getattr(settings.models,role).provider_id==provider_id),None)
                    if fallback_model:
                        chat=await probe.post(config.base_url.rstrip("/")+"/chat/completions",headers={"Authorization":"Bearer "+credential},json={"model":fallback_model,"messages":[{"role":"user","content":"ping"}],"max_tokens":1,"stream":False})
                        if chat.status_code<400:
                            return {"ok":True,"status":chat.status_code,"message":"端点可访问（/models 不可用，chat 探测通过）"}
                        return {"ok":False,"status":chat.status_code,"message":"厂商返回错误状态"}
            return {"ok":response.status_code<400,"status":response.status_code,"message":"端点可访问" if response.status_code<400 else "厂商返回错误状态"}
        except httpx.HTTPError:
            return {"ok":False,"status":0,"message":"无法连接厂商端点"}

    def probe_model_id(provider_id,body):
        # Prefer an explicit probe model, else any role already bound to this provider.
        if body is not None and body.model_id: return body.model_id
        return next((getattr(settings.models,role).model_id for role in type(settings.models).model_fields
                     if getattr(settings.models,role) is not None and getattr(settings.models,role).provider_id==provider_id),None)

    @app.post("/api/v1/settings/providers/{provider_id}/test-embedding")
    async def test_embedding_provider(provider_id:str,body:MemoryProbe|None=None,current=Depends(authenticate)):
        config=settings.providers.get(provider_id)
        if config is None: raise AppError("NOT_FOUND","服务商不存在",404)
        if "embedding" not in config.capabilities: raise AppError("UNSUPPORTED_CAPABILITY","该服务商未启用 embedding 能力",422)
        credential=store.get(config.credential_ref)
        if not credential: raise AppError("CREDENTIAL_REQUIRED","尚未保存 API Key",409)
        model_id=probe_model_id(provider_id,body)
        if not model_id: raise AppError("MODEL_REQUIRED","请先选择 embedding 模型",422)
        import httpx
        from ..memory.external import ExternalEmbeddingClient
        client=ExternalEmbeddingClient(base_url=config.base_url,model_id=model_id,credential=credential,
            dimension=int(settings.memory_external_embedding_dimension),parameters=dict(body.parameters if body else {}),
            timeout_seconds=min(config.timeout_seconds,8.0))
        try:
            vectors=await client.embed(["ping"])
        except httpx.HTTPStatusError as error:
            return {"ok":False,"status":error.response.status_code,"message":"厂商返回错误状态"}
        except httpx.HTTPError:
            return {"ok":False,"status":0,"message":"无法连接厂商端点"}
        except (ValueError,KeyError,TypeError):
            return {"ok":False,"status":0,"message":"响应格式或向量维度不匹配"}
        return {"ok":True,"status":200,"message":"embedding 端点可访问","dimension":len(vectors[0])}

    @app.post("/api/v1/settings/providers/{provider_id}/test-rerank")
    async def test_rerank_provider(provider_id:str,body:MemoryProbe|None=None,current=Depends(authenticate)):
        config=settings.providers.get(provider_id)
        if config is None: raise AppError("NOT_FOUND","服务商不存在",404)
        if "rerank" not in config.capabilities: raise AppError("UNSUPPORTED_CAPABILITY","该服务商未启用 rerank 能力",422)
        credential=store.get(config.credential_ref)
        if not credential: raise AppError("CREDENTIAL_REQUIRED","尚未保存 API Key",409)
        model_id=probe_model_id(provider_id,body)
        if not model_id: raise AppError("MODEL_REQUIRED","请先选择 rerank 模型",422)
        import httpx
        from ..memory.rerank import RerankClient
        client=RerankClient(base_url=config.base_url,model_id=model_id,credential=credential,
            timeout_seconds=min(config.timeout_seconds,8.0))
        try:
            scores=await client.rerank("ping",["ping one","ping two"])
        except httpx.HTTPStatusError as error:
            return {"ok":False,"status":error.response.status_code,"message":"厂商返回错误状态"}
        except httpx.HTTPError:
            return {"ok":False,"status":0,"message":"无法连接厂商端点"}
        except (ValueError,KeyError,TypeError):
            return {"ok":False,"status":0,"message":"响应格式不匹配"}
        return {"ok":True,"status":200,"message":"rerank 端点可访问","results":len(scores)}

    @app.get("/api/v1/companions")
    def companions(current=Depends(authenticate)):
        with db.engine.connect() as conn: return [dict(row) for row in conn.execute(select(m.companions).where(m.companions.c.account_id==current.account_id)).mappings()]

    @app.post("/api/v1/conversations",status_code=201)
    def conversation(body:ConversationCreate,current=Depends(authenticate)):
        """Start the next active web conversation; group rooms and private QQ stay untouched."""
        with db.engine.begin() as conn:
            authorize(conn,current,body.companion_id)
            lock(conn,body.companion_id)
            conn.execute(m.conversations.update().where(m.conversations.c.account_id==current.account_id,m.conversations.c.user_id==current.user_id,m.conversations.c.companion_id==body.companion_id,m.conversations.c.state=="active",m.conversations.c.channel_kind!="group").values(state="archived"))
            row=conn.execute(m.conversations.insert().values(account_id=current.account_id,user_id=current.user_id,companion_id=body.companion_id,title=body.title,state="active",channel_kind="web").returning(m.conversations)).mappings().one()
        logger.info("conversation started %s",row["id"])
        return {**dict(row),"conversation_id":row["id"]}

    @app.get("/api/v1/conversations")
    def conversation_list(companion_id:UUID,state:Literal["active","archived","hidden","all"]="active",current=Depends(authenticate)):
        with db.engine.connect() as conn:
            authorize(conn,current,companion_id)
            query=select(m.conversations).where(m.conversations.c.account_id==current.account_id,m.conversations.c.user_id==current.user_id,m.conversations.c.companion_id==companion_id)
            if state!="all": query=query.where(m.conversations.c.state==state)
            rows=conn.execute(query.order_by(m.conversations.c.created_at.desc())).mappings()
            return [dict(row) for row in rows]

    @app.patch("/api/v1/conversations/{conversation_id}")
    def update_conversation(conversation_id:UUID,body:ConversationUpdate,current=Depends(authenticate)):
        with db.engine.begin() as conn:
            existing=authorized_conversation(conn,current,conversation_id)
            values={}
            if body.title is not None: values["title"]=body.title
            if body.state is not None:
                if body.state=="active" and existing["state"]!="active" and existing.get("channel_kind")!="group":
                    lock(conn,existing["companion_id"])
                    conn.execute(m.conversations.update().where(m.conversations.c.account_id==current.account_id,m.conversations.c.user_id==current.user_id,m.conversations.c.companion_id==existing["companion_id"],m.conversations.c.state=="active",m.conversations.c.channel_kind!="group",m.conversations.c.id!=conversation_id).values(state="archived"))
                values["state"]=body.state
            row=conn.execute(m.conversations.update().where(m.conversations.c.id==conversation_id).values(**values).returning(m.conversations)).mappings().one() if values else existing
        if body.state is not None: logger.info("conversation %s state -> %s",conversation_id,body.state)
        return dict(row)

    @app.post("/api/v1/chat/requests",status_code=202)
    async def chat(body:ChatRequest,current=Depends(authenticate)):
        accepted=await app.state.submit_call_chat(current,body) if body.output.realtime else await service.submit(current,body)
        wakeup.set()
        return accepted

    @app.get("/api/v1/generations/{generation_id}")
    def generation(generation_id:UUID,current=Depends(authenticate)):
        with db.engine.connect() as conn:
            row,_,_=generation_scope(conn,generation_id,current)
            return {"generation_id":generation_id,"state":row["state"],"delivery_state":row["delivery_state"],"epoch":row["epoch"],"error_code":row["error_code"]}

    @app.get("/api/v1/generations/{generation_id}/speech")
    def generation_speech(generation_id:UUID,current=Depends(authenticate)):
        with db.engine.connect() as conn:
            row,_,_=generation_scope(conn,generation_id,current)
        return voice.speech_status(generation_id,row["epoch"])

    @app.get("/api/v1/generations/{generation_id}/events")
    async def events(generation_id:UUID,last_event_id:str|None=Header(default=None),after:int=0,current=Depends(authenticate)):
        try: cursor=int(last_event_id) if last_event_id is not None else after
        except ValueError: raise AppError("INVALID_SEQUENCE","事件序号无效",422)
        service.replay(current,generation_id,cursor)
        async def frames():
            nonlocal cursor
            while True:
                batch=service.replay(current,generation_id,cursor)
                for event in batch:
                    cursor=event.sequence
                    yield encode_sse(event)
                with db.engine.connect() as conn:
                    generation,request,_=generation_scope(conn,generation_id,current)
                if generation["state"] in TERMINAL: break
                if not batch: yield ": heartbeat\n\n"
                await asyncio.sleep(0.05 if request['output'].get('realtime') else 0.25)
        return StreamingResponse(frames(),media_type="text/event-stream",headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})

    @app.delete("/api/v1/conversations/{conversation_id}")
    async def delete_conversation(conversation_id:UUID,current=Depends(authenticate)):
        return await deletion.delete_conversation(current,conversation_id)

    @app.get("/api/v1/conversations/{conversation_id}/live-cursor")
    def live_cursor(conversation_id:UUID,current=Depends(authenticate)):
        with db.engine.connect() as conn:
            authorized_conversation(conn,current,conversation_id)
            sequence=conn.scalar(select(func.max(dm.conversation_live_events.c.sequence)).where(
                dm.conversation_live_events.c.conversation_id==conversation_id)) or 0
        return {'sequence':int(sequence)}

    @app.get("/api/v1/conversations/{conversation_id}/live")
    async def control(conversation_id:UUID,after:int=0,current=Depends(authenticate)):
        with db.engine.connect() as conn: authorized_conversation(conn,current,conversation_id)
        async def frames():
            cursor=after
            while True:
                with db.engine.connect() as conn:
                    rows=list(conn.execute(select(dm.conversation_live_events).where(dm.conversation_live_events.c.conversation_id==conversation_id,dm.conversation_live_events.c.sequence>cursor).order_by(dm.conversation_live_events.c.sequence)).mappings())
                for row in rows:
                    cursor=row["sequence"]
                    payload=json.dumps(dict(row),ensure_ascii=False,default=str)
                    yield f"id: {cursor}\nevent: {row['type']}\ndata: {payload}\n\n"
                if not rows: yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(frames(),media_type="text/event-stream",headers={"Cache-Control":"no-store"})

    @app.post("/api/v1/generations/{generation_id}/interrupt")
    async def interrupt(generation_id:UUID,body:InterruptRequest,current=Depends(authenticate)):
        return await service.interrupt(current,generation_id,body.command_id)

    @app.get("/api/v1/conversations/{conversation_id}/events")
    def history(conversation_id:UUID,cursor:UUID|None=None,limit:int=Query(default=50,ge=1,le=200),current=Depends(authenticate)):
        with db.engine.connect() as conn:
            authorized_conversation(conn,current,conversation_id)
            query=select(m.events).where(m.events.c.conversation_id==conversation_id,m.events.c.visibility=='visible')
            if cursor:
                pivot=conn.execute(select(m.events.c.recorded_at).where(m.events.c.id==cursor,m.events.c.conversation_id==conversation_id)).scalar()
                if pivot is None: raise AppError("INVALID_CURSOR","分页位置无效",422)
                from sqlalchemy import tuple_
                query=query.where(tuple_(m.events.c.recorded_at,m.events.c.id)>tuple_(pivot,cursor))
            rows=[dict(row) for row in conn.execute(query.order_by(m.events.c.recorded_at,m.events.c.id).limit(limit)).mappings()]
            sender_ids={row['sender_principal_id'] for row in rows if row.get('sender_principal_id')}
            senders={}
            if sender_ids:
                for sender in conn.execute(select(m.external_principals).where(m.external_principals.c.id.in_(sender_ids))).mappings():
                    senders[sender['id']]={'principal_id':str(sender['id']),'display_name':sender['display_name'],'card_name':sender['card_name'],'external_user_id':sender['external_user_id'],'is_owner':sender['is_owner']}
            for row in rows:
                row['sender_info']=senders.get(row.get('sender_principal_id'))
            refs=list(conn.execute(select(m.asset_refs.c.event_id,m.assets).join(m.assets,m.asset_refs.c.asset_id==m.assets.c.id).join(m.asset_owners,m.asset_owners.c.asset_id==m.assets.c.id).where(m.asset_refs.c.event_id.in_([row['id'] for row in rows]),m.asset_owners.c.account_id==current.account_id).order_by(m.assets.c.created_at,m.assets.c.id)).mappings())
            for row in rows:
                event_assets={str(ref['id']):attachment(ref,ref['kind']) for ref in refs if ref['event_id']==row['id'] and ref['kind'] in ('image','audio')}
                ordered=row['content'].get('asset_ids') or list(event_assets)
                row['attachments']=row['content'].get('attachments') or [event_assets[str(key)] for key in ordered if str(key) in event_assets]
                row['trace']=row['content'].get('trace',[])
            return rows

    @app.post("/api/v1/assets",status_code=201)
    async def upload(file:UploadFile=File(),current=Depends(authenticate)):
        return await assets.put(current,file.file,file.content_type or "application/octet-stream",original_filename=file.filename)

    @app.get('/api/v1/stickers')
    def list_stickers(companion_id:UUID,current=Depends(authenticate)):
        return stickers.list(current,companion_id)

    @app.post('/api/v1/stickers',status_code=201)
    async def create_sticker(body:StickerCreate,current=Depends(authenticate)):
        row=stickers.save_from_asset(current,body.companion_id,body.asset_id,name=body.name,tags=body.tags)
        if not body.tags:
            tagged=await auto_tag_asset(current,body.asset_id,row['name'])
            if tagged: row=stickers.update(current,row['id'],tagged)
        return row

    @app.post('/api/v1/stickers/{sticker_id}/autotag')
    async def autotag_sticker(sticker_id:UUID,current=Depends(authenticate)):
        row=stickers.get(current,sticker_id)
        tagged=await auto_tag_asset(current,row['asset_id'],row['name'])
        if not tagged: raise AppError('STICKER_TAG_FAILED','自动标签需要已配置的视觉模型',503)
        return stickers.update(current,sticker_id,tagged)

    @app.patch('/api/v1/stickers/{sticker_id}')
    def update_sticker(sticker_id:UUID,body:StickerPatch,current=Depends(authenticate)):
        return stickers.update(current,sticker_id,body.model_dump(exclude_unset=True))

    @app.delete('/api/v1/stickers/{sticker_id}')
    def delete_sticker(sticker_id:UUID,current=Depends(authenticate)):
        return stickers.update(current,sticker_id,delete=True)

    @app.get("/api/v1/assets/{asset_id}")
    def asset(asset_id:UUID,current=Depends(authenticate)):
        record,path=assets.get(current,asset_id)
        return FileResponse(path,media_type=record["mime"],headers={"Cache-Control":"private,no-store"})

    @app.get("/api/v1/companions/{companion_id}/persona")
    async def persona(companion_id:UUID,current=Depends(authenticate)): return await identity.snapshot(current,companion_id)

    @app.patch("/api/v1/companions/{companion_id}/persona")
    async def update_persona(companion_id:UUID,body:PersonaPatch,current=Depends(authenticate)): return await identity.update(current,companion_id,body.expected_version,body.patch)

    @app.post("/api/v1/companions/{companion_id}/model")
    async def bind_model(companion_id:UUID,body:ModelRef,current=Depends(authenticate)):
        if body.provider_id not in settings.providers: raise AppError("UNKNOWN_PROVIDER","请先在服务端配置该厂商",422)
        if set(body.parameters)-set(settings.providers[body.provider_id].supported_parameters): raise AppError("UNSUPPORTED_PARAMETER","厂商不支持指定参数",422)
        return await identity.bind_model(current,companion_id,body)

    @app.post("/api/v1/memory/search")
    async def memory(body:SearchRequest,current=Depends(authenticate)):
        with db.engine.connect() as conn:
            conversation=authorized_conversation(conn,current,body.conversation_id)
            if conversation["companion_id"]!=body.companion_id: raise AppError("SCOPE_MISMATCH","角色与会话不匹配",403)
        scope=Scope(**current.model_dump(),companion_id=body.companion_id,conversation_id=body.conversation_id)
        await MemoryService(db).index_events(scope)
        return await MemoryService(db).search(scope,body.query)

    @app.post("/api/v1/memory/corrections")
    async def correct(body:CorrectionRequest,current=Depends(authenticate)):
        return await CorrectionService(db,retention_days=settings.memory_retention_days).apply_correction(current,body.memory_id,body.correction_text,body.source_event_id)

    @app.get("/api/v1/memory/items")
    def memory_items(companion_id:UUID,q:str|None=Query(default=None,max_length=200),kind:str|None=Query(default=None,max_length=40),status:str|None=Query(default=None,max_length=40),sort:Literal["created","importance","expiry"]=Query(default="created"),limit:int=Query(default=50,ge=1,le=200),pool:Literal["all","owner","member"]=Query(default="all"),room_id:UUID|None=Query(default=None),subject_principal_id:UUID|None=Query(default=None),current=Depends(authenticate)):
        return MemoryService(db).list_items(current,companion_id,kind,status,limit,q,sort,
            pool=None if pool=="all" else pool,room_id=room_id,subject_principal_id=subject_principal_id)

    @app.get("/api/v1/memory/items/{memory_id}")
    def memory_item(memory_id:UUID,current=Depends(authenticate)):
        row=MemoryService(db).get_item(current,memory_id)
        if row is None: raise AppError("NOT_FOUND","记忆不存在",404)
        return row

    @app.patch("/api/v1/memory/items/{memory_id}")
    def update_memory_item(memory_id:UUID,body:MemoryItemPatch,current=Depends(authenticate)):
        if all(value is None for value in (body.status,body.importance,body.is_pinned,body.ttl_days)):
            raise AppError("INVALID_PATCH","至少提供一项记忆修改",422)
        return MemoryService(db).update_item(current,memory_id,status=body.status,importance=body.importance,is_pinned=body.is_pinned,ttl_days=body.ttl_days)

    @app.post("/api/v1/memory/recall-test")
    async def recall_test(body:RecallTestRequest,current=Depends(authenticate)):
        with db.engine.begin() as conn:
            authorize(conn,current,body.companion_id)
            conversation_id=conn.scalar(select(m.conversations.c.id).where(m.conversations.c.account_id==current.account_id,m.conversations.c.user_id==current.user_id,m.conversations.c.companion_id==body.companion_id).order_by(m.conversations.c.created_at.desc()).limit(1))
            if conversation_id is None: raise AppError("NO_CONVERSATION","先开始一段对话",409)
            scope=Scope(**current.model_dump(),companion_id=body.companion_id,conversation_id=conversation_id)
        return await MemoryService(db).recall_debug(scope,body.query,limit=10)

    @app.post("/api/v1/memory/maintenance")
    def memory_maintenance(body:MemoryExtractionRequest,current=Depends(authenticate)):
        with db.engine.begin() as conn:
            authorize(conn,current,body.companion_id)
        return maintain_memories(db,body.companion_id,retention_days=settings.memory_retention_days)

    @app.get("/api/v1/memory/stats")
    def memory_stats(companion_id:UUID,current=Depends(authenticate)):
        data=MemoryService(db).stats(current,companion_id)
        data["settings"]=memory_settings_view()
        return data

    @app.get("/api/v1/memory/settings")
    def get_memory_settings(current=Depends(authenticate)):
        return memory_settings_view()

    @app.patch("/api/v1/memory/settings")
    def update_memory_settings(body:MemorySettingsPatch,current=Depends(authenticate)):
        with SETTINGS_WRITE_LOCK:
            aliases={'batch_events':'extract_batch_events','max_candidates':'extract_max_candidates',
                'timeout_seconds':'extract_timeout_seconds','max_output_tokens':'extract_max_output_tokens'}
            updates={'memory_' + aliases.get(key,key):value for key,value in body.model_dump(exclude_none=True).items()}
            try:
                validated=Settings.model_validate({**settings.model_dump(),**updates})
            except ValueError:
                raise AppError('INVALID_MEMORY_SETTINGS','记忆设置无效：保留期需使用已知类型及1到3650天',422) from None
            for key in updates: setattr(settings,key,getattr(validated,key))
            service.memory_extraction_enabled=settings.memory_extraction_enabled
            persist_settings(settings)
            if any(key in updates for key in ('memory_daily_budget_usd','memory_daily_token_limit','memory_input_price_per_million','memory_output_price_per_million')):
                with db.engine.begin() as conn:
                    conn.execute(m.outbox.update().where(m.outbox.c.event_type.in_([MEMORY_EXTRACT,MEMORY_CONSOLIDATE]),
                        m.outbox.c.state=='pending',m.outbox.c.payload['account_id'].astext==str(current.account_id),
                        m.outbox.c.payload['last_error_code'].astext=='MEMORY_BUDGET_EXCEEDED').values(available_at=m.now()))
        refresh_runtime()
        return memory_settings_view()

    @app.post('/api/v1/memory/consolidations',status_code=201)
    def queue_memory_summary(body:MemorySummaryRequest,current=Depends(authenticate)):
        if not settings.memory_consolidation_enabled:
            raise AppError('MEMORY_SUMMARY_DISABLED','摘要整理已关闭',409)
        with db.engine.begin() as conn:
            authorize(conn,current,body.companion_id)
            conversation_id=conn.scalar(select(m.conversations.c.id).where(m.conversations.c.account_id==current.account_id,
                m.conversations.c.user_id==current.user_id,m.conversations.c.companion_id==body.companion_id)
                .order_by(m.conversations.c.created_at.desc()).limit(1))
            if conversation_id is None: raise AppError('NO_CONVERSATION','先开始一段对话',409)
            scope=Scope(**current.model_dump(),companion_id=body.companion_id,conversation_id=conversation_id)
            job_id=enqueue_consolidation(conn,scope,body.level)
        wakeup.set()
        return {'job_id':job_id,'status':'queued' if job_id else 'noop_or_already_queued','level':body.level}

    @app.get("/api/v1/memory/extractions")
    def memory_extractions(companion_id:UUID,limit:int=Query(default=20,ge=1,le=100),current=Depends(authenticate)):
        with db.engine.connect() as conn:
            authorize(conn,current,companion_id)
            rows=conn.execute(select(m.outbox).where(m.outbox.c.event_type.in_([MEMORY_EXTRACT,MEMORY_CONSOLIDATE]),m.outbox.c.aggregate_id==companion_id).order_by(m.outbox.c.created_at.desc()).limit(limit)).mappings()
            return [{"id":row["id"],"event_type":row['event_type'],"state":row["state"],"attempts":row["attempts"],"created_at":row["created_at"],"available_at":row["available_at"],"last_error_code":(row["payload"] or {}).get("last_error_code"),"result":(row["payload"] or {}).get("result"),"usage":(row["payload"] or {}).get("usage")} for row in rows]

    @app.post("/api/v1/memory/extractions",status_code=201)
    def queue_memory_extraction(body:MemoryExtractionRequest,current=Depends(authenticate)):
        """Queue one extraction batch immediately; useful before the next chat turn completes."""
        with db.engine.begin() as conn:
            authorize(conn,current,body.companion_id)
            conversation_id=conn.scalar(select(m.conversations.c.id).where(m.conversations.c.account_id==current.account_id,m.conversations.c.user_id==current.user_id,m.conversations.c.companion_id==body.companion_id).order_by(m.conversations.c.created_at.desc()).limit(1))
            if conversation_id is None: raise AppError("NO_CONVERSATION","先开始一段对话",409)
            scope=Scope(**current.model_dump(),companion_id=body.companion_id,conversation_id=conversation_id)
            job_id=enqueue_manual_extraction(conn,scope)
        wakeup.set()
        return {"queued":True,"job_id":job_id}

    @app.get("/api/v1/memory/autobiography/status")
    def autobiography_status(companion_id:UUID,current=Depends(authenticate)):
        with db.engine.connect() as conn:
            authorize(conn,current,companion_id)
        enabled=bool(settings.memory_autobiography_enabled)
        organizer=autobiography_organizer(current,companion_id) if enabled else None
        config=organizer.config if organizer is not None else AutobiographyConfig()
        return {"enabled":enabled,"groupings":list(GROUPINGS),"group_by":config.grouping_name(),
                "pending_previews":len(organizer.pending_preview_ids()) if organizer is not None else 0,
                "max_entries":config.max_entries,"max_entries_per_chapter":config.max_entries_per_chapter}

    @app.post("/api/v1/memory/autobiography/previews",status_code=201)
    def create_autobiography_preview(body:AutobiographyPreviewRequest,current=Depends(authenticate)):
        organizer=autobiography_require(current,body.companion_id)
        rows=MemoryService(db).list_items(current,body.companion_id,status="active",
            limit=organizer.config.max_entries,pool="owner")
        selected=[row for row in rows if row.get("summary_level")=="" and row.get("visibility")=="owner_private"
                  and row.get("pool_kind")=="owner"]
        preview=organizer.preview(selected,group_by=body.group_by)
        return {"preview":preview.to_dict()}

    @app.post("/api/v1/memory/autobiography/previews/{preview_id}/confirm")
    def confirm_autobiography_preview(preview_id:str,body:AutobiographyActionRequest,current=Depends(authenticate)):
        organizer=autobiography_require(current,body.companion_id)
        record=organizer.confirm(preview_id)
        return {"record":record.to_dict()}

    @app.post("/api/v1/memory/autobiography/records/{record_id}/undo")
    def undo_autobiography_record(record_id:str,body:AutobiographyActionRequest,current=Depends(authenticate)):
        organizer=autobiography_require(current,body.companion_id)
        record=organizer.undo(record_id)
        return {"record":record.to_dict()}

    @app.get("/api/v1/events/{event_id}")
    def event_source(event_id:UUID,current=Depends(authenticate)):
        with db.engine.connect() as conn:
            row=conn.execute(select(m.events).where(m.events.c.id==event_id,m.events.c.account_id==current.account_id)).mappings().first()
            if row is None: raise AppError("NOT_FOUND","事件不存在",404)
            return dict(row)

    @app.delete("/api/v1/events/{event_id}")
    async def delete_event(event_id:UUID,current=Depends(authenticate)): return await deletion.delete_event(current,event_id)

    @app.post("/api/v1/archive/exports",status_code=201)
    async def export(body:ExportRequest,current=Depends(authenticate)):
        key=uuid4()
        target=settings.data_dir/"exports"/(str(key)+".zip")
        await ExportService(db,assets.root).export(current,body.companion_id,target)
        return {"export_id":key,"download_url":f"/api/v1/archive/exports/{key}"}

    @app.get("/api/v1/archive/exports/{export_id}")
    def download_export(export_id:UUID,current=Depends(authenticate)):
        path=settings.data_dir/"exports"/(str(export_id)+".zip")
        if not path.is_file(): raise AppError("NOT_FOUND","导出包不存在",404)
        return FileResponse(path,media_type="application/zip",filename=path.name)

    @app.get("/api/v1/voice/settings")
    async def get_voice_settings(current=Depends(authenticate)):
        return await voice_settings_view()
    @app.patch("/api/v1/voice/settings")
    async def update_voice_settings(body:VoiceSettingsPatch,current=Depends(authenticate)):
        changes=body.model_dump(exclude_unset=True,exclude_none=True)
        data=settings.voice.model_dump()
        data.update(changes)
        try: updated=type(settings.voice).model_validate(data)
        except ValidationError: raise AppError("INVALID_VOICE_SETTINGS","语音设置无效",422) from None
        if body.voice_id is not None and body.base_url is None and settings.voice.enabled:
            try: rows=await voice.list_voices(force=True)
            except Exception: rows=[]
            if rows and updated.voice_id not in {row["id"] for row in rows}:
                raise AppError("VOICE_NOT_FOUND","本地没有找到这个音色",422)
        with SETTINGS_WRITE_LOCK:
            settings.voice=updated
            persist_settings(settings)
        voice.invalidate()
        logger.info("voice settings updated enabled=%s voice=%s probability=%.2f",updated.enabled,updated.voice_id,updated.probability)
        return await voice_settings_view()

    @app.post("/api/v1/voice/preview")
    async def preview_voice(body:VoicePreviewRequest,current=Depends(authenticate)):
        audio=await voice.preview(body.text,body.emotion)
        return Response(content=audio.data,media_type="audio/wav",headers={"Cache-Control":"no-store","X-Sample-Rate":str(audio.sample_rate),"Content-Disposition":"inline; filename=voice-preview.wav"})

    @app.get("/api/v1/voices")
    async def voices(current=Depends(authenticate)):
        view=await voice_settings_view(force_voices=True)
        return {"voices":view["voices"],"status":view["status"],"active_voice_id":view["active_voice_id"]}

    def proactive_view(current, companion_id=None):
        data=settings.proactive.model_dump(mode="json")
        if companion_id is not None:
            with db.engine.connect() as conn: authorize(conn,current,companion_id)
            data.update(proactive.stats(current, companion_id))
            recent=idle_cognition.recent(current,companion_id,limit=1)
            data["recent_cognition"]=recent[0] if recent else None
        return data

    @app.get("/api/v1/proactive/cognitions")
    def list_idle_cognitions(companion_id:UUID,limit:int=Query(default=10,ge=1,le=50),current=Depends(authenticate)):
        with db.engine.connect() as conn: authorize(conn,current,companion_id)
        return idle_cognition.recent(current,companion_id,limit=limit)

    @app.get("/api/v1/proactive/settings")
    def get_proactive_settings(companion_id:UUID|None=None,current=Depends(authenticate)):
        return proactive_view(current,companion_id)

    @app.patch("/api/v1/proactive/settings")
    def update_proactive_settings(body:ProactiveSettingsPatch,current=Depends(authenticate)):
        data=settings.proactive.model_dump()
        data.update(body.model_dump(exclude_unset=True,exclude_none=True))
        try: updated=ProactiveSettings.model_validate(data)
        except ValidationError: raise AppError("INVALID_PROACTIVE_SETTINGS","主动消息设置无效",422) from None
        with SETTINGS_WRITE_LOCK:
            settings.proactive=updated
            persist_settings(settings)
        if body.enabled is False:
            idle_cognition.cancel_actor(current,reason='disabled')
        elif body.continuation_enabled is False:
            idle_cognition.cancel_continuations(current)
        logger.info("proactive settings updated enabled=%s idle=%s gap=%s max=%s",updated.enabled,updated.min_idle_minutes,updated.min_gap_minutes,updated.max_per_day)
        return proactive_view(current)

    @app.post("/api/v1/proactive/send")
    async def send_proactive(body:ProactiveSendRequest,current=Depends(authenticate)):
        with db.engine.connect() as conn: authorize(conn,current,body.companion_id)
        result=await proactive.send(current,body.companion_id,force=True,trigger="manual")
        if not result.get("sent"):
            reason=result.get("reason") or "unknown"
            if reason in ('silent','empty_message','waiting'):
                return result
            if reason=="model_unconfigured": raise AppError("MODEL_UNCONFIGURED","请先配置聊天厂商和模型",503)
            if reason=="no_active_conversation": raise AppError("NO_ACTIVE_CONVERSATION","当前没有进行中的对话",409)
            if reason=="empty": raise AppError("PROACTIVE_EMPTY","模型没有生成内容，请稍后再试",502)
            if reason=='invalid_output': raise AppError('WAKE_INVALID_OUTPUT','唤醒决定格式无效，本次未发送',502)
            raise AppError("PROACTIVE_BUSY","正在发送主动消息，请稍后再试",409)
        return result

    from ..agent.api import install_agent_routes
    install_agent_routes(app,settings,store,service,authenticate)
    from ..integrations.qq import install_qq_routes
    install_qq_routes(app,settings,store,service,assets,actor,authenticate,wakeup)
    from ..voice.call import install_call_routes
    install_call_routes(app,settings,db,voice,authenticate)
    from .purpose_routes import install_purpose_routes
    install_purpose_routes(app,settings,db,authenticate,authorize)
    from .usage_routes import install_usage_routes
    install_usage_routes(app,settings,db,authenticate)
    from .memory_card_routes import install_memory_card_routes
    install_memory_card_routes(app,db,authenticate,authorize)
    from ..memory.cards import MemoryCardService
    if getattr(service,"memory_cards",None) is None:
        service.memory_cards=MemoryCardService(db)
    return app

def from_env():
    settings=load_settings(os.environ.get("AGI_CONFIG","../config/app.local.yaml"))
    from pydantic import SecretStr
    values=settings.model_dump()
    if os.environ.get("AGI_DATABASE_URL"): values["database_url"]=SecretStr(os.environ["AGI_DATABASE_URL"])
    elif (settings.data_dir/"local-db.json").exists(): values["database_url"]=SecretStr(json.loads((settings.data_dir/"local-db.json").read_text(encoding="utf-8"))["url"])
    if os.environ.get("AGI_ACCESS_TOKEN"): values["access_token"]=SecretStr(os.environ["AGI_ACCESS_TOKEN"])
    if os.environ.get("AGI_CLIENT_KEY"): values["client_key"]=SecretStr(os.environ["AGI_CLIENT_KEY"])
    if os.environ.get("AGI_HOST"): values["host"]=os.environ["AGI_HOST"]
    if os.environ.get("AGI_ALLOWED_ORIGINS"):
        values["allowed_origins"]=[item.strip() for item in os.environ["AGI_ALLOWED_ORIGINS"].split(",") if item.strip()]
    if os.environ.get("AGI_CONTEXT_BUDGET_BYTES"): values["context_budget_bytes"]=int(os.environ["AGI_CONTEXT_BUDGET_BYTES"])
    if os.environ.get("AGI_INPUT_BUDGET_BYTES"): values["input_budget_bytes"]=int(os.environ["AGI_INPUT_BUDGET_BYTES"])
    # Revalidate assigned secret fields and resolve only project-owned identity files.
    settings=Settings.model_validate(values)
    settings=apply_persisted_settings(settings)
    local=settings.data_dir/"local-identity.json"
    if not local.is_file(): raise RuntimeError("Run nagi init-local first")
    saved=json.loads(local.read_text(encoding="utf-8"))
    if settings.access_token is None:
        settings.access_token=SecretStr((settings.data_dir/"access-token.txt").read_text(encoding="utf-8").strip())
    actor=Actor.model_validate(saved["actor"])
    engine=create_engine(settings.database_url.get_secret_value(),hide_parameters=True)
    db=Database(engine)
    store=SecretStore(settings.data_dir)
    from ..providers.registry import configured_provider,ProviderRegistry
    registry=ProviderRegistry(settings,store.get)
    service=GenerationService(db,None,model=None,runtime_name=settings.runtime.preferred,provider_resolver=registry.resolve if settings.runtime.preferred=="direct_vendor" else None,context_budget=settings.context_budget_bytes,input_budget=settings.input_budget_bytes)
    service.registry=registry

    def apply_settings():
        """Re-resolve provider clients after the client settings page edits endpoints or keys."""
        registry.invalidate_all()
        service.memory_extraction_enabled=settings.memory_extraction_enabled
        service.memory_summary_extractor=None
        if settings.runtime.preferred!="direct_vendor":
            service.provider,service.model=None,None
            service.memory_extractor=None
            app.state.memory_extractor_source="unconfigured"
            return
        dialogue,model=configured_provider(settings,"dialogue",store.get)
        service.provider,service.model=dialogue,model
        vision_provider,vision_model=configured_provider(settings,"vision",store.get)
        if settings.models.vision is None:
            vision_provider,vision_model=dialogue,model
        if vision_provider and vision_model:
            from ..providers.vision import VisionAdapter
            service.vision=VisionAdapter(db,AssetStore(db,settings.data_dir/"assets"),vision_provider.complete,vision_model)
        else:
            service.vision=None
        # A dedicated memory_extract role is preferred; otherwise extraction reuses the
        # dialogue model. A configured-but-broken role does not silently fall back.
        if settings.models.memory_extract is None:
            extract_provider,extract_model,source=dialogue,model,"dialogue_fallback"
        else:
            extract_provider,extract_model=configured_provider(settings,"memory_extract",store.get)
            source="memory_extract"
        if settings.memory_extraction_enabled and extract_provider and extract_model:
            from ..memory.metering import MeteredExtractor
            service.memory_extractor=MeteredExtractor(extract_provider,extract_model,max_candidates=settings.memory_extract_max_candidates,timeout=settings.memory_extract_timeout_seconds,max_output_tokens=settings.memory_extract_max_output_tokens)
            app.state.memory_extractor_source=source
        else:
            service.memory_extractor=None
            app.state.memory_extractor_source="unconfigured"

        summary_provider,summary_model=extract_provider,extract_model
        if settings.models.memory_reflect is not None:
            summary_provider,summary_model=configured_provider(settings,'memory_reflect',store.get)
        service.memory_summary_extractor=None
        if settings.memory_consolidation_enabled and summary_provider and summary_model:
            from ..memory.metering import MeteredExtractor
            service.memory_summary_extractor=MeteredExtractor(summary_provider,summary_model,max_candidates=2,
                timeout=settings.memory_extract_timeout_seconds,max_output_tokens=settings.memory_extract_max_output_tokens,
                system_prompt=SUMMARY_PROMPT)

    app=create_app(settings,db,actor,service,settings_store=store)
    app.state.apply_settings=apply_settings
    apply_settings()
    return app
