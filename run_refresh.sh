#!/bin/zsh
# launchd entry point (every minute): refresh from Zoho, encrypt, publish. Log: logs/refresh.log
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "${0:A:h}"
mkdir -p logs raw out
if [ ! -f "$HOME/.config/pawkit-cs360/zoho.json" ]; then
  echo "$(date '+%Y-%m-%d %H:%M') waiting: Zoho access not set up yet (run python3 zoho_setup.py)" >> logs/refresh.log
else
  # the fast job (run_fast.sh) shares this lock; the full run waits up to 5 minutes for it
  lockf -t 300 raw/.run.lock zsh -c 'python3 refresh.py && python3 build.py raw/crm.json --no-bundle && ./publish.sh && echo "$(date "+%Y-%m-%d %H:%M") published"' >> logs/refresh.log 2>&1
fi
tail -n 400 logs/refresh.log > logs/.r && mv logs/.r logs/refresh.log
# when DNS for the custom domain appears, switch the site to it (never affects the data refresh)
./domain_watch.sh >> logs/domain.log 2>&1 || true
