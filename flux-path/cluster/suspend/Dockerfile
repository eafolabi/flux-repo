FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt /app
RUN --mount=type=secret,target=/root/.config/pip/pip.conf pip install --no-cache-dir -r requirements.txt

COPY . /app

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
ENTRYPOINT ["gunicorn", "--config", "gunicorn-config.py", "flux-suspend-checker:app"]
