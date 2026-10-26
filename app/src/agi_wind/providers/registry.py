import os
from .openai_compatible import OpenAICompatibleProvider
from ..core.errors import AppError

class ProviderRegistry:
    """Resolve provider clients from settings, with credentials from env or the local secret store.

    Cached clients are dropped when the client settings page edits a provider so the next
    generation call uses the new endpoint/credential without restarting the gateway.
    """
    def __init__(self,settings,credential_source=None):
        self.settings,self.clients,self.retired=settings,{},{}
        self.credential_source=credential_source or os.environ.get
    def credential(self,config):
        return self.credential_source(config.credential_ref)
    def resolve(self,model):
        config=self.settings.providers.get(model.provider_id)
        if not config or not self.credential(config): raise AppError("PROVIDER_NOT_CONFIGURED","所选供应商尚未配置",503)
        if config.protocol!="openai_compatible": raise AppError("ADAPTER_UNAVAILABLE","该厂商需要专用适配器",503)
        if set(model.parameters)-set(config.supported_parameters): raise AppError("UNSUPPORTED_PARAMETER","厂商不支持指定参数",422)
        if model.provider_id not in self.clients: self.clients[model.provider_id]=OpenAICompatibleProvider(config,self.credential(config))
        return self.clients[model.provider_id]
    def invalidate(self,provider_id):
        client=self.clients.pop(provider_id,None)
        if client is not None: self.retired.setdefault(provider_id,[]).append(client)
    def invalidate_all(self):
        for provider_id in list(self.clients): self.invalidate(provider_id)
    async def aclose(self):
        for client in self.clients.values(): await client.aclose()
        for group in self.retired.values():
            for client in group: await client.aclose()
        self.clients.clear(); self.retired.clear()

def configured_provider(settings,role,credential_source=None):
    model=getattr(settings.models,role)
    if model is None: return None,None
    config=settings.providers[model.provider_id]
    if config.protocol!="openai_compatible": raise AppError("ADAPTER_UNAVAILABLE","该厂商需要专用适配器",503)
    source=credential_source or os.environ.get
    credential=source(config.credential_ref)
    if not credential: return None,model
    return OpenAICompatibleProvider(config,credential),model
