# The scorer builds this on linux/amd64 and runs it with no network.
# The digest is the image the organizers labeled every commit in; keep it.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# sqlglot at the pinned commit, so the selector can read the source a patch changes.
# The only network access: sqlglot's own repository at a fixed commit, at build time.
# The package is precompiled so the selector's import check (br/importcheck.py) starts fast on a read-only disk.
COPY tools/fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py eb11aec4be8cb2122afe713a4816662c3f0fb2fa /opt/sqlglot \
    && python -m compileall -q /opt/sqlglot/sqlglot \
    && chmod -R a+rX /opt/sqlglot && rm /tmp/fetch_sqlglot.py

COPY selector.py /app/selector.py
COPY br/ /app/br/
COPY maps/ /app/maps/
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
