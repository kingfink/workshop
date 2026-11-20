# Dependency fixture

The dependency versions are simulated locally; no registry or credentials are needed.

`dependencies.json` requests the widget and helper versions. Regenerate `dependencies.lock` with `python3 resolve.py`. The resolver is the only supported way to write the lockfile.

Run `python3 -m unittest discover -s tests` before finishing. Widget 2 renamed the `uppercase` keyword to `upper`. Widget 1 only accepts `uppercase`.
