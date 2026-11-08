#!/bin/bash
# Double-click to start YakTube, then open http://localhost:8080
cd "$(dirname "$0")"
( sleep 1.5; open "http://localhost:8080" ) &
exec python3 app.py
