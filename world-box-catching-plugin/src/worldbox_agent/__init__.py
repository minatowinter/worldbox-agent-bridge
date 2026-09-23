"""worldbox_agent — WorldBox 0.51.2 external nation-state data feed for AI agents.

Pure standard library. No mods, no injection, no memory reading: everything is
derived from files WorldBox itself writes to disk.

Data sources (Windows default paths, auto-discovered):
  * ``%USERPROFILE%\\AppData\\LocalLow\\mkarpenko\\WorldBox\\autosaves\\<ts>\\map.wbax``
        full world state, plain JSON (saveVersion 17 in 0.51.x)
  * ``%USERPROFILE%\\AppData\\LocalLow\\mkarpenko\\WorldBox\\autosaves\\<ts>\\map_stats.s3db``
        SQLite history: per-kingdom YEARLY time series + world event log
  * ``...\\saves\\saveN\\map.wbox``
        same payload, zlib-deflated (manual save / "Save" button)
"""

__version__ = "1.0.0"
SCHEMA_VERSION = "1.0"

__all__ = ["__version__", "SCHEMA_VERSION"]
