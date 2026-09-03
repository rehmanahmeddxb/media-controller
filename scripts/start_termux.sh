#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
#  Ahmed Reaction Studio — Android/Termux startup (zero npm/Node.js)
#  pkg install python ffmpeg + venv + pip + uvicorn + prints LAN URL.
# ============================================================
set -e
cd "$(dirname "$0")/.."

echo "=== Ahmed Reaction Studio (Termux) ==="

# ---- packages --------------------------------------------------------
if ! command -v python >/dev/null 2>&1; then
    echo "Installing Python via pkg…"
    pkg update -y
    pkg install -y python ffmpeg
fi

# FFmpeg check with exact remediation (self-healing)
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[WARN] FFmpeg not found. Installing…"
    pkg install -y ffmpeg || {
        echo "[ERROR] FFmpeg is required for proxies and exports:  pkg install ffmpeg"
        echo "        Without it the studio runs preview-only."
    }
fi

# ---- venv -------------------------------------------------------------
if [ ! -x ".venv/bin/python" ]; then
    echo "Creating virtual environment…"
    python -m venv .venv
fi
source .venv/bin/activate

echo "Installing Python dependencies (first run only)…"
pip install --quiet --upgrade pip
pip install --quiet --only-binary :all: -r requirements.txt

# ---- config ------------------------------------------------------------
[ -f config.json ] || cp config.example.json config.json

# ---- diagnostics --------------------------------------------------------
python scripts/diagnostics.py || true

# ---- launch --------------------------------------------------------------
HOST_IP=$(ifconfig wlan0 2>/dev/null | awk '/inet /{print $2}' || echo "127.0.0.1")
PORT=8642
echo
echo "Starting local server…"
echo "  On this phone:   http://127.0.0.1:${PORT}"
echo "  From another device on the same Wi-Fi:  http://${HOST_IP}:${PORT}"
echo "  (Cameras require a secure context: open the phone URL ON the phone itself.)"
echo "Press Ctrl+C to stop."
echo

exec python -m uvicorn app.server:build_app --host 0.0.0.0 --port "${PORT}"
