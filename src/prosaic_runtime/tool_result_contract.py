"""Private result normalization. Model projection is a separate checked phase."""
from jsonschema import Draft202012Validator
from .conversation import encode_owned
from .tool_contracts import ToolResultV1, EvidenceDraftV1, owned, pointer
from .tool_effects import ToolFailure, snapshot_outcome


class ToolResultValidationError(ValueError):
    def __init__(self, code='invalid_result'):
        self.code = code
        super().__init__(code)


def select_pointer(data, path):
    pointer(path)
    current = data
    for token in path.split('/')[1:] if path else ():
        token = token.replace('~1', '/').replace('~0', '~')
        if type(current) is dict:
            if token not in current: raise ValueError('missing selection')
            current = current[token]
        elif type(current) is list:
            if not token.isascii() or not token.isdecimal() or (len(token)>1 and token.startswith('0')):
                raise ValueError('invalid array selection')
            index = int(token)
            if index >= len(current): raise ValueError('missing selection')
            current = current[index]
        else:
            raise ValueError('invalid selection')
    return owned(current)


def validate_tool_result_outcome(tool, outcome):
    try:
        outcome = snapshot_outcome(outcome, tool.max_result_bytes)
        if outcome['status'] == 'error':
            return outcome
        value = outcome['result']
        if type(value) is not dict or set(value) != {'version', 'data', 'sources'} or type(value['version']) is not int or value['version'] != 1:
            raise ValueError('invalid private result')
        sources = value['sources']
        if type(sources) is not list or len(sources) > 64:
            raise ValueError('invalid evidence declarations')
        schema = tool.result_contract.output_schema
        if schema is not None:
            Draft202012Validator(schema).validate(value['data'])
        for source in sources:
            draft = EvidenceDraftV1.from_dict(source)
            select_pointer(value['data'], draft.selector)
        return outcome
    except Exception:
        raise ToolResultValidationError() from None


def normalize_tool_result(tool, value):
    if type(value) is ToolFailure:
        return snapshot_outcome({'status': 'error', 'error': value.code}, tool.max_result_bytes)
    if type(value) is not ToolResultV1 or tool.result_contract is None:
        raise ToolResultValidationError()
    outcome = {'status': 'ok', 'result': value.to_dict()}
    try:
        # Match both private journal framing and the current ASCII wire framing.
        import json
        if len(json.dumps(outcome, allow_nan=False).encode('utf-8')) > tool.max_result_bytes:
            raise ToolResultValidationError('result_limit')
        encode_owned(outcome, max_bytes=tool.max_result_bytes)
    except ToolResultValidationError:
        raise
    except Exception:
        raise ToolResultValidationError('result_limit') from None
    return validate_tool_result_outcome(tool, outcome)
