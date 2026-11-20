import json
from pathlib import Path

versions = json.loads(Path("dependencies.json").read_text())
assert all(version in (1, 2) for version in versions.values())
Path("dependencies.lock").write_text(json.dumps(versions, sort_keys=True) + "\n")
