#!/bin/bash
cd "$(dirname "$0")"

if [ -f bot.pid ] && kill -0 "$(cat bot.pid)" 2>/dev/null; then
    echo "El bot ya está corriendo (PID $(cat bot.pid))"
    exit 1
fi

# Instalar dependencias si faltan
pip3 install -q beautifulsoup4 2>/dev/null

if [ -f .env ]; then set -a; source .env; set +a; fi

nohup python3 bot.py >> upf-bot.log 2>&1 &
echo $! > bot.pid
echo "UPF-Bot arrancado con PID $!"
echo "Logs: tail -f $(pwd)/upf-bot.log"
