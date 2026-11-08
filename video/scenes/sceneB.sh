source /mnt/c/Hackathon/hermes-plugin/video/scenes/common.sh
P=$'\e[1;38;5;214mfresh\e[0m \e[38;5;245m~\e[0m \e[1m$\e[0m '
clear
t 'export HERMES_HOME=~/brand-new-hermes'
t 'find $HERMES_HOME/memories $HERMES_HOME/sessions -type f | wc -l'
t 'hermes plugins install Automationbot24x7/hermes-tusk/tusk --yes-deps --enable'
t 'hermes chat -q "What do you know about me and what I am building?"'
t 'hermes tusk recall "when do I review pull requests"'
printf '%s' "$P"; sleep 2
