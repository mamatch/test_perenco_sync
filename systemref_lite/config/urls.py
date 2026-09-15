from django.contrib import admin
from django.http import JsonResponse
from django.urls import path


def index(_request):
    return JsonResponse(
        {
            "service": "systemref-lite (simplified MDM)",
            "admin": "/admin/",
            "database": "systemref.sqlite3",
            "hint": "The SQLite file is the application's database; see docs/04_mdm_and_iot.md",
        }
    )


urlpatterns = [path("", index), path("admin/", admin.site.urls)]
