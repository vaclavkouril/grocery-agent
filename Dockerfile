FROM python:3.13-slim AS builder
WORKDIR /build
COPY requirements-dev.lock ./
RUN python -m pip install --no-cache-dir -r requirements-dev.lock
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .

FROM python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    GROCERY_DATABASE_URL=sqlite:////var/lib/grocery-agent/grocery.db \
    GROCERY_SNAPSHOT_DIR=/var/lib/grocery-agent/snapshots \
    GROCERY_REPORT_DIR=/var/lib/grocery-agent/reports \
    GROCERY_LOCK_PATH=/var/lib/grocery-agent/workflow.lock \
    GROCERY_MEAL_CONFIG=/app/config/meals.toml \
    GROCERY_COLLECTOR_CONFIG=/app/config/collector.toml \
    GROCERY_CONTROL_DATABASE_URL=sqlite:////var/lib/grocery-agent/control.db
COPY requirements-runtime.lock /tmp/requirements-runtime.lock
RUN python -m pip install --no-cache-dir -r /tmp/requirements-runtime.lock
COPY --from=builder /wheels /tmp/wheels
RUN python -m pip install --no-cache-dir --no-deps /tmp/wheels/*.whl
RUN useradd --create-home --uid 10001 grocery
RUN mkdir -p /var/lib/grocery-agent/snapshots /var/lib/grocery-agent/reports
RUN chown -R grocery:grocery /var/lib/grocery-agent
WORKDIR /app
COPY config ./config
USER 10001:10001
ENTRYPOINT ["grocery-agent"]
CMD ["collector"]
