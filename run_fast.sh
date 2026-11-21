#!/bin/zsh
# Every ~2 minutes (LaunchAgent com.pawkit.cs360-fast): WhatsApp reply status + Live Status only.
# Small Zoho reads (see refresh.py fast()); everything else comes from the 15-minute full run.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "${0:A:h}"
mkdir -p logs raw out
[ -f "$HOME/.config/pawkit-cs360/zoho.json" ] || exit 0
# skip this turn if the full run is busy
lockf -t 0 raw/.run.lock zsh -c 'python3 refresh.py --fast && python3 build.py raw/crm.json --no-bundle >/dev/null && ./publish.sh && echo "$(date "+%Y-%m-%d %H:%M:%S") fast published"' >> logs/fast.log 2>&1
tail -n 400 logs/fast.log > logs/.f && mv logs/.f logs/fast.log
