from django.shortcuts import render
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@permission_classes([AllowAny])
def ping(request):
    """React Phase 0 smoke test only - proves the build pipeline (React
    build -> Django static files -> DRF endpoint) works end to end before
    any real page is converted. Not meant to gate anything by auth, so it
    stays AllowAny even though every other endpoint in this app defaults
    to IsAuthenticated (see config/settings.py's REST_FRAMEWORK block)."""
    return Response({"status": "ok", "message": "pong"})


def react_test(request):
    """Standalone (no base.html) mount point for frontend/'s React build -
    proves the whole pipeline (React build -> static/react/ -> Django
    static serving -> DRF /api/ping/) before any real page gets converted.
    Throwaway - remove once Phase 1 starts converting a real page."""
    return render(request, "api/react_test.html")
