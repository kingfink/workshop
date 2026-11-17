# Example scheduled imports

The `imports/sync` function loads source records into the reporting tables. Its logs include `mode`, the processed interval, and whether the load committed.

In `replay` mode, the cursor advances only after the whole load commits. The next call resumes at the last committed cursor and merges by record ID, so it includes any unfinished interval without adding duplicates. A completed replay covering the failed interval establishes that it caught up.

In `current-window` mode, each call starts at its own scheduled window and does not replay an earlier window. A successful later call does not recover an earlier failure; the missed interval needs a replay.

The customer key is part of the reporting contract. This repository has no standing decision to retire or replace it. The model joins customers to orders on that key.
