# TZ 6.7: container image for the paper/alerts mode of xauusd-alert-system.
#
# MetaTrader5 (live trading) is a Windows-only package; in requirements.txt it
# is guarded by `platform_system == "Windows"`, so on this Linux base image the
# marker excludes it automatically and pip installs the rest of the pinned
# stack. Inside the container the bot runs against the MT5 shim
# (simulation/mt5_shim, already on pytest/pythonpath) — i.e. the virtual
# simulator + Telegram control bot — never against a real terminal.
#
# No models/, .env, *.sqlite, logs/ or backups/ are copied: see .dockerignore.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer is cached across code-only changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Paper/alerts entrypoint: virtual simulator + Telegram control bot.
# The MT5 shim (simulation/mt5_shim) resolves the `MetaTrader5` import.
CMD ["python", "-m", "scripts.run_bot"]
