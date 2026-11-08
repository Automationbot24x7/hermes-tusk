source /mnt/c/Hackathon/hermes-plugin/video/scenes/common.sh
P=$'\e[1;38;5;81mlaptop\e[0m \e[38;5;245m~\e[0m \e[1m$\e[0m '
clear
t 'hermes chat -q "Remember this for the long term: I am Rajeev. I am building a recipe app called Saffron. It deploys on Fly.io, the database is Postgres on Neon, and I review pull requests every morning at 9:30 IST."'
t 'hermes tusk flush'
t 'hermes tusk verify --limit 2'
printf '%s' "$P"; sleep 2
