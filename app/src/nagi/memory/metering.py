"""Meter strict extraction calls, including the bounded repair and failed validations."""
from contextvars import ContextVar
import json
from .extraction import Extractor, _SYSTEM_PROMPT


class MeteredExtractor:
    def __init__(self, provider, model, *, max_candidates=8, timeout=30, max_output_tokens=2048, system_prompt=None):
        self.provider, self.model = provider, model
        self.max_output_tokens = max_output_tokens
        self.system_prompt = system_prompt or _SYSTEM_PROMPT
        self._usage = ContextVar('memory_usage_' + str(id(self)), default=None)
        options = {'max_candidates':max_candidates, 'timeout':timeout}
        if system_prompt is not None:
            options['system_prompt'] = system_prompt
        self.extractor = Extractor(self._complete, model, **options)

    def reservation_tokens(self, events):
        offered = json.dumps({'events':events}, ensure_ascii=False, default=str)
        # UTF-8 bytes conservatively bound input tokens, including a full repair prompt
        # plus its capped 4000-character prior output and envelope overhead.
        return (2 * (len(offered.encode()) + len(self.system_prompt.encode())) + 16000 + 1024,
                2 * self.max_output_tokens)

    async def _complete(self, model, messages):
        usage = self._usage.get()
        usage['calls'] += 1
        previous_unknown = usage['usage_estimated']
        usage['usage_estimated'] = True
        metered = getattr(self.provider, 'complete_memory', None)
        if metered is None:
            usage['usage_estimated'] = True
            return await self.provider.complete(model, messages)
        result = await metered(model, messages, max_tokens=self.max_output_tokens)
        measured = result.get('usage') or {}
        usage['usage_estimated'] = previous_unknown or measured.get('usage_estimated', True)
        usage['prompt_tokens'] += measured.get('prompt_tokens', 0)
        usage['completion_tokens'] += measured.get('completion_tokens', 0)
        return result['text']

    async def extract(self, scope, events):
        measured = {'calls':0, 'prompt_tokens':0, 'completion_tokens':0, 'usage_estimated':False}
        binding = self._usage.set(measured)
        try:
            candidates, usage = await self.extractor.extract(scope, events)
            return candidates, {**usage, **measured}
        except BaseException as error:
            # A failed network request has unknown billing; the budget retains its bound.
            if measured['calls'] and measured['prompt_tokens'] + measured['completion_tokens'] == 0:
                measured['usage_estimated'] = True
            error.memory_usage = dict(measured)
            raise
        finally:
            self._usage.reset(binding)
