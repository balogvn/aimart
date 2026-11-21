# The AImart Parquet Apify Actor. Apify builds this from the repository's apify/ folder (the
# Docker context: see .dockerignore); locally: docker build -t aimart-actor apify/
#
# apify/actor-python:3.13 is Apify's Python 3.13 base (Debian trixie slim, pre-cached on Apify's
# builders), pinned by digest: the tag as pushed on 2026-10-26. It ships no Apify SDK (checked
# on that digest: pip, setuptools, wheel and packaging only), so requirements.txt brings it.
FROM apify/actor-python:3.13@sha256:134b21518882ef053a9f7b1b17f5d5246efc026cd9ef9c2c69d0f7b7f723c56f

# Installed as root into the system site-packages, so the code the Actor runs as myuser is
# read-only to it. --require-hashes: every package, dependencies included, is pinned to the
# bytes in requirements.txt (aimart-parquet 0.1.2 from PyPI, the apify SDK 4.2.0).
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --require-hashes --no-deps -r requirements.txt \
    && python -m pip check \
    && python -c "import importlib.metadata as m; print('aimart-parquet', m.version('aimart-parquet'), 'apify', m.version('apify'))"

COPY aimart_actor ./aimart_actor
RUN python -m compileall -q aimart_actor

# The base image's unprivileged user; the run's temporary files go to /tmp.
USER myuser

# Exec form: Python is PID 1 and receives the platform's signals itself.
CMD ["python", "-m", "aimart_actor"]
