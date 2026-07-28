# UiOne, as an image an air-gapped operator can be handed.
#
# Two stages, and the split is not decoration: the build stage carries a
# compiler and a package index, and neither of those belongs on a machine
# running in a datacentre with no internet. What ships is the virtualenv and
# the source, nothing else.

FROM python:3.12-slim AS build

# uv resolves and installs an order of magnitude faster than pip, which matters
# because this image is rebuilt on every change to a dependency.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, source second: the dependency layer is the slow one and
# only changes when pyproject.toml does, so an ordinary code change rebuilds in
# seconds rather than minutes.
#
# The list is *compiled from pyproject.toml* rather than repeated here. Writing
# it out again would be a second description of the dependencies, and the first
# draft of this file already proved the point by omitting sse-starlette and
# jsonschema — an image that builds and then fails to import.
COPY pyproject.toml README.md ./
RUN uv venv /opt/venv \
    && uv pip compile pyproject.toml --output-file /tmp/requirements.txt \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache -r /tmp/requirements.txt

COPY src/ ./src/
COPY alembic.ini ./
RUN VIRTUAL_ENV=/opt/venv uv pip install --no-cache --no-deps .


FROM python:3.12-slim AS runtime

# curl is here for the healthcheck and nothing else. An image with a shell full
# of network tools is a nicer place to be for anyone who gets into it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Not root. A container that writes to a mounted file share as root writes
# root-owned files onto somebody's NAS, and the permission model this product
# is built around then describes files nobody can fix.
RUN useradd --create-home --uid 10001 uione

COPY --from=build /opt/venv /opt/venv
COPY --from=build /build/alembic.ini /app/alembic.ini

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Inside a container the service must listen on every interface; the
    # host default of 127.0.0.1 would make it unreachable from outside, which
    # looks exactly like a crashed app.
    UIONE_HOST=0.0.0.0 \
    UIONE_PORT=8000 \
    # The database lives on a volume, not in the image layer.
    UIONE_DATABASE_URL=sqlite+aiosqlite:////data/uione.db

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

WORKDIR /app
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /data /run/estate \
    && chown uione:uione /data /run/estate
USER uione
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=6 \
    CMD curl -fsS http://localhost:8000/system/health || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "uione.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
