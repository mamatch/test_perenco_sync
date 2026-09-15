#!/bin/sh
# Migrate, seed on first start, create the admin user, then run the CMD.
set -e
mkdir -p "$(dirname "$SYSTEMREF_DB_PATH")"
FIRST_START=0
[ -f "$SYSTEMREF_DB_PATH" ] || FIRST_START=1
python manage.py migrate --no-input
if [ "$FIRST_START" = "1" ] || [ "$SEED_ON_START" = "1" ]; then
  python manage.py seed_masterdata
fi
python manage.py createsuperuser --no-input 2>/dev/null || true
# The SQLite file is shared with the host through a bind mount: make sure the
# candidate's pipeline can write to it (SQLite also needs to create -journal files).
chmod -R a+rwX "$(dirname "$SYSTEMREF_DB_PATH")" 2>/dev/null || true
exec "$@"
