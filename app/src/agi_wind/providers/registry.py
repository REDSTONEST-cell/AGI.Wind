import os
from .openai_compatible import OpenAICompatibleProvider
from ..core.errors import AppError

class ProviderRegistry:
    def __init__(self,settings): self.settings,self.clients=settings,{}
    def resolve(self,model):
        config=self.settings.providers.get(model.provider_id)
        if not config or not os.environ.get(config.credential_ref): raise AppError("PROVIDER_NOT_CONFIGURED","所选供应商尚未配置",503)
        if config.protocol!="openai_compatible": raise AppError("ADAPTER_UNAVAILABLE","该厂商需要专用适配器",503)
        if set(model.parameters)-set(config.supported_parameters): raise AppError("UNSUPPORTED_PARAMETER","厂商不支持指定参数",422)
        if model.provider_id not in self.clients: self.clients[model.provider_id]=OpenAICompatibleProvider(config,os.environ[config.credential_ref])
        return self.clients[model.provider_id]
    async def aclose(self):
        for client in self.clients.values(): await client.aclose()

def configured_provider(settings,role):
    model=getattr(settings.models,role)
    if model is None: return None,None
    config=settings.providers[model.provider_id]
    if config.protocol!="openai_compatible": raise AppError("ADAPTER_UNAVAILABLE","该厂商需要专用适配器",503)
    credential=os.environ.get(config.credential_ref)
    if not credential: return None,model
    return OpenAICompatibleProvider(config,credential),model
