import html
from html.parser import HTMLParser
import json
from urllib.parse import urlsplit

import httpx
from ..core.errors import AppError
from .settings import search_secret_ref


class SearchCredentials:
    """Use a dedicated search key first, then a configured official DeepSeek key."""
    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    def get(self, ref):
        dedicated = self.store.get(ref)
        if dedicated or ref != search_secret_ref('deepseek'):
            return dedicated
        settings = self.settings()
        dialogue = settings.models.dialogue
        ids = list(settings.providers)
        if dialogue and dialogue.provider_id in ids:
            ids.remove(dialogue.provider_id)
            ids.insert(0, dialogue.provider_id)
        for provider_id in ids:
            provider = settings.providers[provider_id]
            try:
                url = urlsplit(provider.base_url)
                official = url.scheme == 'https' and url.hostname == 'api.deepseek.com' and url.port in (None, 443) and not url.username and not url.password
            except ValueError:
                official = False
            if official:
                key = self.store.get(provider.credential_ref)
                if key: return key
        return None


class PlainText(HTMLParser):
    def __init__(self): super().__init__(); self.parts = []; self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'): self.skip += 1
    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.skip = max(0, self.skip - 1)
    def handle_data(self, data):
        if not self.skip: self.parts.append(data)


def plain(value, limit):
    parser = PlainText()
    parser.feed(str(value or '')[:10000])
    return ' '.join(' '.join(parser.parts).split())[:limit]


def safe_url(value):
    if not isinstance(value, str) or len(value) > 2000: return None
    try:
        url = urlsplit(value)
        if url.scheme in ('https', 'http') and url.hostname and not url.username and not url.password:
            return value
    except ValueError: pass
    return None


class WebSearch:
    def __init__(self, settings, secrets, client=None):
        self.settings, self.secrets, self.client = settings, secrets, client

    @property
    def configured(self):
        backend = self.settings.search_backend
        return bool(self.secrets and self.secrets.get(search_secret_ref(backend))) if backend in ('brave', 'deepseek') else bool(self.settings.search_url)

    async def search(self, query):
        if not self.settings.web_search_enabled: raise AppError('TOOL_NOT_ALLOWED', '联网检索已关闭', 403)
        if not self.configured: raise AppError('SEARCH_UNCONFIGURED', '请先配置联网检索服务', 422)
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise AppError('TOOL_ARGUMENTS_INVALID', '搜索词长度无效', 422)
        if self.settings.search_backend == 'deepseek' and len(query.strip()) > 128:
            raise AppError('TOOL_ARGUMENTS_INVALID', 'DeepSeek 搜索词最多128个字符', 422)
        client = self.client or httpx.AsyncClient(timeout=self.settings.tool_timeout_seconds, trust_env=False, follow_redirects=False)
        try:
            headers = {'Accept': 'application/json'}
            method, kwargs = 'GET', {}
            if self.settings.search_backend == 'deepseek':
                endpoint = 'https://api.deepseek.com/v1/tools/search'
                headers['Authorization'] = 'Bearer ' + self.secrets.get(search_secret_ref('deepseek'))
                method = 'POST'
                kwargs['json'] = {'query': query.strip(), 'count': self.settings.search_max_results, 'need_content': False}
            elif self.settings.search_backend == 'brave':
                endpoint = 'https://api.search.brave.com/res/v1/web/search'
                headers['X-Subscription-Token'] = self.secrets.get(search_secret_ref('brave'))
                params = {'q': query.strip(), 'count': self.settings.search_max_results, 'safesearch': 'moderate'}
            else:
                endpoint = self.settings.search_url
                if urlsplit(endpoint).path in ('', '/'): endpoint += '/search'
                key = self.secrets.get(search_secret_ref('searxng')) if self.secrets else None
                if key: headers['Authorization'] = 'Bearer ' + key
                params = {'q': query.strip(), 'format': 'json', 'safesearch': 1}
            if method == 'GET': kwargs['params'] = params
            # Endpoint is administrator-configured. Tool arguments cannot select URLs.
            # Never follow redirects or forward the endpoint credential to result links.
            async with client.stream(method, endpoint, headers=headers, follow_redirects=False, **kwargs) as response:
                if response.status_code != 200: raise AppError('SEARCH_UNAVAILABLE', '检索服务未返回可用结果，请检查服务配置', 503)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 256 * 1024: raise ValueError('oversized search response')
            data = json.loads(raw)
            rows = data.get('web', {}).get('results', []) if self.settings.search_backend == 'brave' else data.get('results', [])
            if not isinstance(rows, list): raise ValueError('invalid search results')
            results, seen = [], set()
            for row in rows[:50]:
                if not isinstance(row, dict): continue
                url = safe_url(row.get('url'))
                if not url or url in seen: continue
                seen.add(url)
                results.append({'title': plain(row.get('title'), 200), 'url': url, 'snippet': plain(row.get('summary') or row.get('snippet') or row.get('description') or row.get('content'), 1200)})
                if len(results) >= self.settings.search_max_results: break
            return {'source_type': 'search_snippets', 'backend': self.settings.search_backend, 'results': results}
        except AppError: raise
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise AppError('SEARCH_UNAVAILABLE', '联网检索失败或响应无效', 503) from None
        finally:
            if self.client is None: await client.aclose()

    async def fetch(self, url):
        if not self.settings.web_search_enabled: raise AppError('TOOL_NOT_ALLOWED', '联网检索已关闭', 403)
        if self.settings.search_backend != 'deepseek' or not self.configured:
            raise AppError('SEARCH_UNCONFIGURED', '网页抓取需要配置 DeepSeek 检索服务', 422)
        if not safe_url(url): raise AppError('TOOL_ARGUMENTS_INVALID', '网页地址必须是无内嵌凭据的HTTP(S)链接', 422)
        client = self.client or httpx.AsyncClient(timeout=self.settings.tool_timeout_seconds, trust_env=False, follow_redirects=False)
        try:
            # Only DeepSeek receives the credential; the target URL is JSON data.
            headers = {'Authorization': 'Bearer ' + self.secrets.get(search_secret_ref('deepseek')), 'Accept': 'application/json'}
            async with client.stream('POST', 'https://api.deepseek.com/v1/tools/fetch', json={'url': url}, headers=headers, follow_redirects=False) as response:
                if response.status_code != 200: raise AppError('FETCH_UNAVAILABLE', '网页抓取失败，请检查链接与服务配置', 503)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 256 * 1024: raise ValueError('oversized fetch response')
            data = json.loads(raw)
            content = data.get('content')
            if not isinstance(content, str) or not content.strip(): raise ValueError('empty page')
            limit = max(100, self.settings.max_result_chars - 500)
            return {'source_type': 'web_page', 'backend': 'deepseek', 'url': url, 'title': plain(data.get('title'), 200), 'content': content[:limit], 'truncated': len(content) > limit}
        except AppError: raise
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise AppError('FETCH_UNAVAILABLE', '网页抓取失败或响应无效', 503) from None
        finally:
            if self.client is None: await client.aclose()
