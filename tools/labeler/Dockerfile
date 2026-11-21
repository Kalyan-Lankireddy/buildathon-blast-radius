# Labeling environment: the image the organizers labeled in, plus coverage tooling.
# Used only on the dev machine to build the committed maps. Not part of the submission image.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock coverage pytest-cov
COPY work/sqlglot.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir /src && tar -xzf /tmp/sqlglot.tar.gz -C /src && mv /src/sqlglot-* /src/sqlglot
WORKDIR /src/sqlglot
ENV PYTHONPATH=/src/sqlglot SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1
