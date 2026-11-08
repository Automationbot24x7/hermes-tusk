export PATH=/root/.local/bin:$PATH
export HERMES_HOME=/root/brand-new-hermes
rm -rf $HERMES_HOME && mkdir -p $HERMES_HOME && cd /root
hermes config set model.provider gemini >/dev/null 2>&1
hermes config set model.default gemini-3.8-flash >/dev/null 2>&1
hermes config set display.show_reasoning false >/dev/null 2>&1
grep -E "^(GEMINI_API_KEY|MEMWAL_PRIVATE_KEY|MEMWAL_ACCOUNT_ID)=" /root/.hermes/.env > $HERMES_HOME/.env
echo '{"namespace": "rajeev-demo"}' > $HERMES_HOME/tusk.json
ls -A $HERMES_HOME
