"""Keep integers outside JavaScript's exact range intact on the wire."""
from fastapi.responses import JSONResponse

MAX_SAFE_INTEGER = (1 << 53) - 1


def json_safe(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > MAX_SAFE_INTEGER else value
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


class SafeJSONResponse(JSONResponse):
    def render(self, content):
        return super().render(json_safe(content))
