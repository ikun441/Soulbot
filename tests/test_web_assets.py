from fastapi.testclient import TestClient

from scripts.web_assets import stamp, sync
from soulbot import VERSION
from soulbot.app import create_web_app


def test_version_stamp_is_idempotent_and_updates_nested_module_imports():
    source = """import {x} from './voice-routes.js';\nimport "./page-session.js?v=v2.4.0";\n<img src="/static/assets/logo.png"><script src="/static/js/app.js"></script>"""
    updated = stamp(source, VERSION)
    assert updated.count("?v=" + VERSION) == 4
    assert stamp(updated, VERSION) == updated
    assert "v2.4.0" not in updated
    assert stamp("https://example.org/file.js", VERSION) == "https://example.org/file.js"


def test_current_release_assets_match_version_and_require_revalidation(tmp_path):
    sync(check=True)
    with TestClient(create_web_app(tmp_path)) as c:
        page = c.get("/")
        assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
        assert "/static/js/app.js?v=" + VERSION in page.text
        script = c.get("/static/js/speech.js?v=" + VERSION)
        assert script.status_code == 200 and script.headers["cache-control"] == "no-cache"
        assert "./voice-routes.js?v=" + VERSION in script.text
