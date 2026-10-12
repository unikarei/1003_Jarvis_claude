# Jarvis の日次処理（run_daily.py）を 1 回実行するコンテナ。統合ワークスペースの architecture.md §12。
# 音声（faster-whisper・sounddevice など）はホストで使うため入れない。秘密情報・DB はイメージに入れない。
# 統合ワークスペースと同じ並び（/work/<リポジトリ>）にして、../1004_insight_reporter などの相対参照を保つ。
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Tokyo

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 app

WORKDIR /work/1003_Jarvis_claude
COPY requirements.txt .
RUN grep -viE '^(faster-whisper|av|sounddevice|soundfile|numpy)([<>=!~ ]|$)' requirements.txt > requirements-docker.txt \
 && pip install --no-cache-dir -r requirements-docker.txt
COPY --chown=app:app . .

USER app
CMD ["python", "run_daily.py"]
