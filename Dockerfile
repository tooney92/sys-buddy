FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS build

WORKDIR /opt/app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/app/.venv

# Install deps first so dependency layers cache independently of source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY README.md ./README.md
COPY src ./src
RUN uv sync --frozen --no-dev


FROM python:3.11-slim-bookworm AS runtime

WORKDIR /opt/app

ENV PATH=/opt/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    SYS_BUDDY_DB=/data/sys_buddy.db \
    SYS_BUDDY_PORT=8787

COPY --from=build /opt/app/.venv /opt/app/.venv
COPY src ./src

RUN useradd -m -u 1000 sysbuddy \
    && mkdir -p /data \
    && chown -R sysbuddy:sysbuddy /data /opt/app
USER sysbuddy

EXPOSE 8787
VOLUME ["/data"]

CMD ["sys-buddy", "serve", "--host", "0.0.0.0", "--port", "8787"]
