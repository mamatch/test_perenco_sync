# systemref_lite — simplified MDM (Django + SQLite)

```bash
uv sync
uv run manage.py migrate
uv run manage.py seed_masterdata            # loads fixtures/masterdata.json
uv run manage.py createsuperuser            # optional, to browse /admin/
uv run manage.py runserver 8000             # http://localhost:8000/admin/
uv run manage.py scenario list              # events you can simulate
```

The database is the file `systemref.sqlite3`: it is the MDM application's own database, the way
PostgreSQL is in production. Your pipeline has to get master data out of it and to write systems
and equipments back into it; how you do both is part of your design. The data model is documented
in `systemref/models.py`.
