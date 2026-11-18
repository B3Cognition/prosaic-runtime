# Tool result format

`tool_result_format` is a profile feature that chooses how tool results are written in the request sent to the
model: `json` (default) or `toon`.

## Why

Tool results are input tokens, and every later request in a turn sends them again. JSON repeats every field name
on every row of a list. TOON writes a list whose rows share the same plain fields as one table:

```
status: ok
result:
  rows[2]{id,name}:
    1,alpha
    2,beta
```

The saving depends on the shape of the data. 20,000 rows of `{id, name, v}` are 910 KB as JSON and 450 KB as
TOON, encoded in about 60 ms; a live turn with 60 five-field rows sent 1,909 prompt tokens instead of 3,040. To
reproduce the first figure:

```python
import json
from toon_format import encode
rows = {"status": "ok", "result": {"rows": [{"id": i, "name": f"n{i}", "v": i * 1.5} for i in range(20000)]}}
print(len(json.dumps(rows)), len(encode(rows)))
```

## What changes

Only the copy of the messages sent to the provider. The conversion runs where the request payload is built, so
live tool results, results replayed from conversation history and the admitted first request are all encoded the
same way. Everything else keeps JSON: the tool journal, guards, result contracts, compaction, progress events and
conversation records.

- Error results (`status: error`) stay JSON, and native Anthropic `tool_result` blocks keep `is_error`.
- A result that cannot be encoded is sent as JSON unchanged; a request never fails because of its format.
- Tool definitions and tool call arguments stay JSON: they are small and nested, and models call tools in JSON.
- `max_result_bytes` is still measured on the JSON result. TOON text is normally smaller, so the limit still holds.
- Guards and rule hooks still receive tool results as JSON data. The model reads the same data as TOON text.
- Input byte and token limits are checked on the request as sent, so they count the TOON text.
- Each result is encoded once per run and reused in the run's later requests.
- Default turns keep their bytes. With `toon`, the request bytes of a turn whose history holds tool results
  change; preparation and execution encode alike, so the first-request check still passes. Switching a profile's
  format between preparing (or freezing) a turn and running it has not been tested.
- The conformance suite runs with a fixed feature list and does not exercise this feature.

## Getting the most from it

The saving comes from lists of flat rows. Nested objects inside rows make TOON fall back to a list layout and save
much less, so tools should return lists of rows with plain values (counts or lists as one text field). Single
records save little.

## Configuration

```sh
python -m pip install 'b3-prosaic-runtime[toon]'
```

```yaml
profiles:
  local:
    features:
      tool_result_format: toon
```

An unknown value, or `toon` without the `toon-format` package, is refused when the profile is loaded.
