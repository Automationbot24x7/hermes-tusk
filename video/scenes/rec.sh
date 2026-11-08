. /root/mw-venv/bin/activate
mkdir -p /mnt/c/Hackathon/hermes-plugin/video/casts
cd /root
asciinema rec --overwrite --cols 104 --rows 30 -q -c "bash /mnt/c/Hackathon/hermes-plugin/video/scenes/$1.sh" /mnt/c/Hackathon/hermes-plugin/video/casts/$1.cast < /dev/null 2>&1 | tail -5
ls -la /mnt/c/Hackathon/hermes-plugin/video/casts/
