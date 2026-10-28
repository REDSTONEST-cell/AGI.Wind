import httpx
import pytest
import json
from nagi.agent.settings import AgentSettings
from nagi.agent.search import WebSearch
from nagi.core.errors import AppError


class Secrets:
    def get(self, ref): return 'synthetic-search-key'


async def test_deepseek_search_and_fetch_use_official_post_and_separate_key():
    from nagi.agent.tools import StationTools
    class Keys:
        def get(self, ref): return 'deepseek-test-key' if ref.endswith('_DEEPSEEK') else None
    seen = []
    def handle(req):
        seen.append(req)
        if req.url.path.endswith('/search'):
            assert json.loads(req.content) == {'query': '测试', 'count': 5, 'need_content': False}
            return httpx.Response(200, json={'results': [{'title': '资料', 'url': 'https://example.org', 'snippet': '短摘要', 'summary': '详细摘要'}]})
        assert json.loads(req.content) == {'url': 'https://example.org'}
        return httpx.Response(200, json={'title': '资料', 'content': '# 正文\n' + '内容' * 5000})
    policy = AgentSettings(enabled=True, web_search_enabled=True, search_backend='deepseek')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        search = WebSearch(policy, Keys(), client)
        result = await search.search('测试')
        page = await search.fetch('https://example.org')
        specs = StationTools(None, None, lambda: policy, search, None).specs()
        assert 'web_fetch' in [tool.name for tool in specs]
        query = next(tool for tool in specs if tool.name == 'web_search')
        assert query.arguments.model_json_schema()['properties']['query']['maxLength'] == 128
        with pytest.raises(AppError): await search.search('字' * 129)
        with pytest.raises(AppError): await search.fetch('https://secret@example.org')
        policy.web_search_enabled = False
        with pytest.raises(AppError): await search.fetch('https://example.org')
    assert len(seen) == 2
    assert all(req.method == 'POST' and req.url.host == 'api.deepseek.com' for req in seen)
    assert all(req.headers['Authorization'] == 'Bearer deepseek-test-key' for req in seen)
    assert result['results'][0]['snippet'] == '详细摘要'
    assert page['content'].startswith('# 正文\n') and page['truncated']
    assert len(page['content']) < policy.max_result_chars


@pytest.mark.parametrize('response', [httpx.Response(302, headers={'location': 'https://other.invalid'}), httpx.Response(403, text='private-upstream-detail'), httpx.Response(200, json={'content': ''}), httpx.Response(200, content=b'x' * (256 * 1024 + 1))])
async def test_deepseek_fetch_errors_are_bounded_and_sanitized(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(AppError) as error:
            await WebSearch(AgentSettings(web_search_enabled=True, search_backend='deepseek'), Secrets(), client).fetch('https://example.org')
    assert error.value.code == 'FETCH_UNAVAILABLE'
    assert 'private-upstream-detail' not in str(error.value)


def test_deepseek_requires_its_own_key():
    class BraveOnly:
        def get(self, ref): return 'brave-key' if ref.endswith('_BRAVE') else None
    assert not WebSearch(AgentSettings(search_backend='deepseek', search_url='https://other.invalid'), BraveOnly()).configured


def test_search_credentials_reuse_official_deepseek_and_follow_changes(tmp_path):
    from nagi.agent.search import SearchCredentials
    from nagi.agent.settings import search_secret_ref
    from nagi.core.settings import Settings, ProviderSettings
    from nagi.core.secrets import SecretStore
    store = SecretStore(tmp_path)
    store.set('MODEL_KEY', 'synthetic-model-key')
    settings = Settings(providers={'ds': ProviderSettings(base_url='https://api.deepseek.com/v1', credential_ref='MODEL_KEY')})
    credentials = SearchCredentials(store, lambda: settings)
    assert credentials.get(search_secret_ref('deepseek')) == 'synthetic-model-key'
    assert credentials.get(search_secret_ref('brave')) is None
    store.set(search_secret_ref('deepseek'), 'synthetic-override')
    assert credentials.get(search_secret_ref('deepseek')) == 'synthetic-override'
    store.delete(search_secret_ref('deepseek'))
    store.set('MODEL_KEY', 'synthetic-rotated')
    assert credentials.get(search_secret_ref('deepseek')) == 'synthetic-rotated'
    settings.providers['ds'].base_url = 'https://api.deepseek.com.other.invalid/v1'
    assert credentials.get(search_secret_ref('deepseek')) is None
    settings.providers['ds'].base_url = 'https://proxy.invalid/deepseek'
    assert credentials.get(search_secret_ref('deepseek')) is None


async def test_searxng_is_explicit_bounded_and_filters_unsafe_urls():
    seen = []
    def request(req):
        seen.append(req)
        return httpx.Response(200,json={'results':[
            {'title':'<b>文档</b>','content':'<script>x</script>正文','url':'https://example.org/docs'},
            {'title':'unsafe','url':'javascript:alert(1)'},
            {'title':'credential','url':'https://secret@host.invalid'},
        ]})
    settings = AgentSettings(web_search_enabled=True,search_url='http://127.0.0.1:8888')
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        value = await WebSearch(settings,Secrets(),client).search('文档')
    assert seen[0].url.path == '/search' and seen[0].url.params['format'] == 'json'
    assert len(value['results']) == 1 and value['results'][0]['title'] == '文档'
    assert value['source_type'] == 'search_snippets' and 'synthetic-search-key' not in str(value)


async def test_brave_uses_fixed_endpoint_and_auth_header():
    seen = []
    def request(req):
        seen.append(req)
        return httpx.Response(200,json={'web':{'results':[{'title':'doc','url':'https://example.org','description':'snippet'}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        value = await WebSearch(AgentSettings(web_search_enabled=True,search_backend='brave'),Secrets(),client).search('weather')
    assert seen[0].url.host == 'api.search.brave.com'
    assert seen[0].headers['X-Subscription-Token'] == 'synthetic-search-key'
    assert value['results'][0]['snippet'] == 'snippet'


async def test_disabled_or_unconfigured_search_does_not_call_network():
    def request(req): raise AssertionError('must not call network')
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        for settings in [AgentSettings(search_url='https://example.org'), AgentSettings(web_search_enabled=True)]:
            with pytest.raises(AppError): await WebSearch(settings,Secrets(),client).search('test')


@pytest.mark.parametrize('response',[httpx.Response(302,headers={'location':'http://127.0.0.1/secret'}),httpx.Response(403,text='private-server-message'),httpx.Response(200,text='not json')])
async def test_bad_search_response_is_sanitized_and_never_redirected(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:response)) as client:
        with pytest.raises(AppError) as err:
            await WebSearch(AgentSettings(web_search_enabled=True,search_url='https://example.org/search'),Secrets(),client).search('test')
        assert 'private-server-message' not in str(err.value)


async def test_backend_switch_does_not_forward_another_backends_secret():
    class ScopedSecrets:
        def get(self,ref): return 'brave-only-secret' if ref.endswith('_BRAVE') else None
    seen=[]
    def handle(req): seen.append(req); return httpx.Response(200,json={'results':[]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await WebSearch(AgentSettings(web_search_enabled=True,search_url='https://search.invalid/search'),ScopedSecrets(),client).search('test')
    assert 'authorization' not in seen[0].headers
