FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY mock_gmao ./mock_gmao
COPY data ./data
ENV PATH="/opt/venv/bin:$PATH" MOCK_DATA_DIR=/app/data MOCK_STATE_DIR=/app/state
VOLUME ["/app/state"]
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8080/_admin/health')"
CMD ["uvicorn", "mock_gmao.main:app", "--host", "0.0.0.0", "--port", "8080"]
