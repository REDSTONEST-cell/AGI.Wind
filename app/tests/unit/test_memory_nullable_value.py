import json
from uuid import uuid4


def test_documented_empty_optional_value_accepts_json_null_without_inventing_a_slot():
    from nagi.memory.extraction import Extractor
    raw=json.dumps({'schema_version':1,'candidates':[{'kind':'episode','content':'合成经历',
        'predicate':'episode','value':None,'epistemic_type':'inferred',
        'source_refs':[{'event_id':str(uuid4()),'quote':'合成经历'}]}]})
    parsed=Extractor._parse(raw)
    assert parsed.candidates[0].value == ''
