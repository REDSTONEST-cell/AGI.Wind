import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
from uuid import UUID,uuid4
import yaml
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config
from .archive.repository import Database
from .core.types import Actor,Scope

def migrate(engine,root):
    cfg=Config(str(root/"app/alembic.ini"))
    cfg.set_main_option("script_location",str(root/"app/migrations"))
    with engine.begin() as conn:
        cfg.attributes["connection"]=conn
        command.upgrade(cfg,"head")

def local_context(root):
    data=root/"data"
    url=json.loads((data/"local-db.json").read_text(encoding="utf-8"))["url"]
    identity=json.loads((data/"local-identity.json").read_text(encoding="utf-8"))
    return Database(create_engine(url,hide_parameters=True)),Actor.model_validate(identity["actor"]),Scope.model_validate(identity["scope"])

def init_local(root):
    data=root/"data"
    data.mkdir(exist_ok=True)
    dbfile=data/"local-db.json"
    if not dbfile.exists():
        source=json.loads((data/"dev-db.json").read_text(encoding="utf-8"))["url"]
        url=make_url(source).set(database="agi_wind_local")
        admin=create_engine(url.set(database="postgres"),isolation_level="AUTOCOMMIT",hide_parameters=True)
        with admin.connect() as conn:
            if not conn.execute(text("SELECT 1 FROM pg_database WHERE datname='agi_wind_local'")).first(): conn.exec_driver_sql('CREATE DATABASE "agi_wind_local"')
        admin.dispose()
        dbfile.write_text(json.dumps({"url":url.render_as_string(hide_password=False)}),encoding="utf-8")
    url=json.loads(dbfile.read_text(encoding="utf-8"))["url"]
    engine=create_engine(url,hide_parameters=True)
    migrate(engine,root)
    identityfile=data/"local-identity.json"
    if not identityfile.exists():
        actor=Actor(account_id=uuid4(),user_id=uuid4())
        scope=Database(engine).seed(actor)
        from .archive.deletions import DeletionLedger
        DeletionLedger(data/"governance").initialize()
        identityfile.write_text(json.dumps({"actor":actor.model_dump(mode="json"),"scope":scope.model_dump(mode="json"),"development_only":True},ensure_ascii=False,indent=2),encoding="utf-8")
    tokenfile=data/"access-token.txt"
    if not tokenfile.exists(): tokenfile.write_text(secrets.token_urlsafe(48),encoding="utf-8")
    config=root/"config/app.local.yaml"
    if not config.exists():
        config.write_text("# 本地开发角色；模型由用户后续配置。凭据仅用环境变量引用。\n# 此阶段显式使用 direct_vendor；Letta 尚未接入，不做自动回退。\n"+yaml.safe_dump({"data_dir":str(data).replace("\\","/"),"providers":{},"models":{},"runtime":{"preferred":"direct_vendor","fallback_policy":"explicit_admin_selection"},"embedding_device":"cpu","asr_device":"cpu","proactive_enabled":False},allow_unicode=True),encoding="utf-8")
    engine.dispose()
    print("Local PostgreSQL database, migrations and development identity initialized.")
    print("Access token saved to ignored data/access-token.txt; value is never printed.")

def main():
    parser=argparse.ArgumentParser(prog="agi-wind")
    parser.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[3])
    commands=parser.add_subparsers(dest="command",required=True)
    commands.add_parser("init-local")
    serve=commands.add_parser("serve")
    serve.add_argument("--port",type=int,default=18440)
    archive=commands.add_parser("archive")
    ac=archive.add_subparsers(dest="operation",required=True)
    verify=ac.add_parser("verify"); verify.add_argument("path",type=Path)
    export=ac.add_parser("export"); export.add_argument("--output",required=True,type=Path)
    restore=ac.add_parser("import"); restore.add_argument("path",type=Path); restore.add_argument("--mode",choices=["new_namespace","clone"],default="new_namespace"); restore.add_argument("--latest-ledger",type=Path)
    index=commands.add_parser("index"); index.add_argument("operation",choices=["rebuild"])
    args=parser.parse_args()
    root=args.root.resolve()
    if args.command=="init-local": init_local(root)
    elif args.command=="serve":
        os.environ["AGI_CONFIG"]=str(root/"config/app.local.yaml")
        from .api.app import from_env
        import uvicorn
        app=from_env()
        server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=args.port,log_level="info",access_log=False))
        app.state.request_shutdown=lambda:setattr(server,"should_exit",True)
        server.run()
    elif args.command=="archive":
        from .archive.imports import ImportService
        if args.operation=="verify":
            report=ImportService.validate(args.path)
            print(json.dumps({"valid":True,"records":report["records"]},ensure_ascii=False))
            return
        db,actor,scope=local_context(root)
        if args.operation=="export":
            from .archive.exports import ExportService
            path=asyncio.run(ExportService(db,root/"data/assets").export(actor,scope.companion_id,args.output))
            print(str(path))
        elif args.operation=="import":
            from .archive.deletions import DeletionLedger
            ledger=DeletionLedger(args.latest_ledger) if args.latest_ledger else None
            report=asyncio.run(ImportService(db,root/"data/assets").restore(args.path,args.mode,ledger))
            print(json.dumps(report,ensure_ascii=False,default=str))
        db.engine.dispose()
    elif args.command=="index":
        from .archive.backup import IndexService
        db,actor,scope=local_context(root)
        asyncio.run(IndexService(db).rebuild_keywords(scope))
        db.engine.dispose()
        print("Keyword index rebuilt; original events and persona retained. Vector index not configured.")

if __name__=="__main__": main()
