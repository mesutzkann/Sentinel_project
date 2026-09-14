# One image, seven servers. The entrypoint picks which one, because the servers differ only in
# which tools they register — seven near-identical images would be seven things to keep in step.
FROM python:3.11-slim AS runtime

# git-mcp shells out to git through GitPython, so the binary has to be present. Nothing else is
# installed: this image reads telemetry and source, it does not build anything.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so editing a server does not invalidate the install layer.
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
        "mcp>=2.0.0" \
        "uvicorn[standard]>=0.32.0" \
        "pydantic>=2.9.0" \
        "pydantic-settings>=2.6.0" \
        "httpx>=0.27.0" \
        "asyncpg>=0.30.0" \
        "GitPython>=3.1.43"

COPY _shared/ _shared/
COPY logs_mcp/ logs_mcp/
COPY metrics_mcp/ metrics_mcp/
COPY traces_mcp/ traces_mcp/
COPY database_mcp/ database_mcp/
COPY git_mcp/ git_mcp/
COPY source_code_mcp/ source_code_mcp/
COPY docker_mcp/ docker_mcp/

# The repository is mounted here read-only for git-mcp and source-code-mcp. Declared so the
# mount point exists even when nothing is mounted, and git does not refuse a directory owned by
# a different user than the one running the process.
RUN mkdir -p /repo && git config --system --add safe.directory /repo

RUN useradd --create-home --uid 1001 mcp
USER mcp

# Overridden per service in compose.
ENV PORT=7000
EXPOSE 7000

CMD ["python", "-m", "logs_mcp.server"]
