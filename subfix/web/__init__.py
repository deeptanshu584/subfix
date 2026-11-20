"""Local web UI for subfix (``subfix web``): FastAPI app, job queue, uploads, file picker.

See ``docs/WEB_API.md`` for the HTTP contract. The server binds 127.0.0.1 only and runs one
subtitle job at a time by calling :func:`subfix.pipeline.run_job` in-process.
"""
