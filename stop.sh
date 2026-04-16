#!/bin/bash
cd "$(dirname "$0")"

if [ ! -f bot.pid ]; then
    echo "No se encontró bot.pid"
    exit 1
fi

PID=$(cat bot.pid)
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" && rm bot.pid
    echo "UPF-Bot detenido (PID $PID)"
else
    echo "El proceso $PID ya no existe"
    rm bot.pid
fi
