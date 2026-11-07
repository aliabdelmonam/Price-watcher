# Hugging Face Space (SDK: Docker, CPU basic). If a store ever needs Playwright, replace the
# FROM line with mcr.microsoft.com/playwright/python:v1.49.0-jammy and add playwright to requirements.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/tmp/data \
    RUNNING_ON=space

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .

EXPOSE 7860
# Exactly one worker: two workers would mean two loops and double alerts.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]
