# 🚀 Quick Start Commands

1. **Activate Env**: `.venv\Scripts\Activate.ps1` (PowerShell) or `.venv\Scripts\activate.bat` (CMD)
2. **Terminal 1 (UI)**: `streamlit run main.py` (opens http://localhost:8501)
3. **Terminal 2 (Backend)**: `python webhook_server.py` (runs on port 8000)
4. **Terminal 3 (Tunnel)**: `& ".\ngrok-v3-stable-windows-amd64 (1)\ngrok.exe" http 8000`
5. **GitHub Webhook**: Set Payload URL to `[your-ngrok-url]/webhook/github`, Content type `application/json`, and Secret to `my_secret_123`.
