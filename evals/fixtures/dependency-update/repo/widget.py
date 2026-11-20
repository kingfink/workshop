import json
from pathlib import Path

version = json.loads(Path("dependencies.lock").read_text())["widget"]
if version == 1:
    def render(text, *, uppercase):
        return text.upper() if uppercase else text
else:
    def render(text, *, upper):
        return text.upper() if upper else text
