export PATH=/root/.local/bin:$PATH
export TERM=xterm-256color
cd /root
t() { printf '%s' "$P"; local s="$1"; for ((i=0;i<${#s};i++)); do printf '%s' "${s:$i:1}"; sleep 0.022; done; sleep 0.6; printf '\n'; eval "$1"; sleep 1.5; }
