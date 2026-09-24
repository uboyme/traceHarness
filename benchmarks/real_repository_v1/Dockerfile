FROM registry-1.docker.io/library/python@sha256:445b9efb2c047a7ccdb30d293fd6b1aa0f62a55062dc2b69051df91e10848749
COPY requirements.txt /opt/traceh-rr/requirements.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: -r /opt/traceh-rr/requirements.txt && python -m pip freeze --all > /opt/traceh-rr/installed.txt
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8
