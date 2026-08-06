FROM python:3.12-slim

# Attaches the published package to the repository on GHCR, which is what makes
# the repository's collaborator list govern who can pull it. Without this the
# package is a free-floating thing with an access list of its own, kept in step
# with the repository's by hand and forgotten the first time someone is added.
LABEL org.opencontainers.image.source=https://github.com/anagkan/ColoMealCheck

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# Photos live on a named volume so they survive image rebuilds.
RUN mkdir -p /srv/data/photos

EXPOSE 8000

CMD ["/srv/app/entrypoint.sh"]
