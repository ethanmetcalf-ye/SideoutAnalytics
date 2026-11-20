#!/usr/bin/env bash
# Server setup, in the order it was done by hand. Run it by hand on a new
# server (EC2 user data only runs at first launch). Safe to re-run: each step
# checks first. Needs sudo, assumes Ubuntu.
set -euo pipefail

APP_DIR=/opt/sideout/app

# 1. Docker
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sudo sh
id -nG "$USER" | grep -qw docker || sudo usermod -aG docker "$USER"   # applies at next login

# 2. Clone the repo
if [ ! -d "$APP_DIR/.git" ]; then
    sudo mkdir -p "$(dirname "$APP_DIR")"
    sudo chown "$USER": "$(dirname "$APP_DIR")"
    git clone https://github.com/ethanmetcalf-ye/SideoutAnalytics.git "$APP_DIR"
fi

# 3. deploy/.env from the example (never overwritten). Fill it in by hand.
if [ ! -f "$APP_DIR/deploy/.env" ]; then
    cp "$APP_DIR/deploy/.env.example" "$APP_DIR/deploy/.env"
    chmod 600 "$APP_DIR/deploy/.env"
    echo "created deploy/.env: fill in its values before starting the stack"
fi

# 4. AWS CLI v2 (zip installer; uname -m picks x86_64 or aarch64)
command -v aws >/dev/null || {
    command -v unzip >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq unzip; }
    tmp=$(mktemp -d)
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o "$tmp/awscliv2.zip"
    unzip -q "$tmp/awscliv2.zip" -d "$tmp"
    sudo "$tmp/aws/install"
    rm -rf "$tmp"
}

# 5. Automatic security updates, rebooting only at 09:30 UTC (after the 07:30 backup).
dpkg -s unattended-upgrades > /dev/null 2>&1 || sudo apt-get install -y -qq unattended-upgrades
sudo tee /etc/apt/apt.conf.d/52unattended-upgrades-local > /dev/null <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "09:30";
EOF

# 6. Backup cron entry in root's crontab, 07:30 UTC (after the late games are polled)
BACKUP_CRON="30 7 * * * /bin/bash $APP_DIR/deploy/backup.sh >> /var/log/sideout-backup.log 2>&1"
current_cron=$(sudo crontab -l 2>/dev/null || true)
grep -qF "$APP_DIR/deploy/backup.sh" <<< "$current_cron" || {
    printf '%s\n%s\n' "$current_cron" "$BACKUP_CRON" | sudo crontab -
}

# 7. Rotate the backup log weekly, keeping four
sudo tee /etc/logrotate.d/sideout-backup > /dev/null <<'EOF'
/var/log/sideout-backup.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
EOF
