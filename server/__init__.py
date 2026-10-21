"""Edit OS API service: upload a manuscript, run the pipeline in the background, review and download.

    uvicorn --factory server.app:create_app      (or python -m server)

Configuration is read from the environment (see `server.config`); nothing secret lives in code.
"""
