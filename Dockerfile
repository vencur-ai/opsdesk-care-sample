FROM python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f
WORKDIR /app
COPY runtime /app/runtime
COPY tasks /app/tasks
COPY host_data /app/host_data
COPY private /app/private
RUN pip install --no-cache-dir --require-hashes -r /app/runtime/requirements.lock \
    && pip install --no-cache-dir --no-deps --no-build-isolation /app/runtime
ENV OPSDESK_TASKS_ROOT=/app/tasks OPSDESK_STRICT_HONESTY=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
CMD ["uvicorn", "opsdesk.api.server:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
