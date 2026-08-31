# MAGI2 always-on server deployment

Target: Ubuntu 24.04 VPS (AWS Lightsail/OCI/etc.)

## 1. Create server
Recommended starter size: 1 GB RAM or more. Open SSH only; MAGI2 monitor itself does not need a public inbound port.

## 2. Install packages and clone
```bash
sudo apt update
sudo apt install -y git python3 python3-venv
sudo useradd -m -s /bin/bash magi || true
sudo mkdir -p /opt/VPD-Investment
sudo chown -R magi:magi /opt/VPD-Investment
sudo -u magi git clone https://github.com/Henryrotaewon/VPD-Investment.git /opt/VPD-Investment
cd /opt/VPD-Investment
sudo -u magi python3 -m venv .venv
sudo -u magi .venv/bin/pip install -r requirements.txt
```

## 3. Telegram secrets
Create `/etc/magi2.env` locally on the server. Never commit this file.

```bash
sudo tee /etc/magi2.env >/dev/null <<'EOF'
TELEGRAM_BOT_TOKEN=PUT_YOUR_TOKEN_HERE
TELEGRAM_CHAT_ID=PUT_YOUR_CHAT_ID_HERE
MAGI2_MONITOR_INTERVAL_SECONDS=60
EOF
sudo chmod 600 /etc/magi2.env
```

## 4. Install service
```bash
sudo cp /opt/VPD-Investment/deploy/magi2-monitor.service /etc/systemd/system/magi2-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now magi2-monitor
```

## 5. Verify
```bash
sudo systemctl status magi2-monitor --no-pager
sudo journalctl -u magi2-monitor -n 100 --no-pager
```
Expected log contains `MAGI2 server runner started` and recurring monitor runs.

## 6. Operational rule
Until server monitoring is verified, keep GitHub Actions schedule as fallback. After server monitor is stable, remove/disable the GitHub 5-minute schedule to prevent duplicate PAPER monitor execution. Manual `morning`, `refill`, and `report` workflows may remain as fallback controls.

## Notes
- `magi2/server_runner.py` currently runs `paper_engine.py monitor` every 60 seconds.
- PAPER engine remains PAPER-only and keeps the existing TP/SL rules.
- For a later LIVE phase, use WebSocket/event-driven price monitoring and an explicit confirmation/kill-switch layer before any real order routing.
