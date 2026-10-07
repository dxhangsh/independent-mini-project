# -*- coding: utf-8 -*-
"""阶段三 PWA 测试：manifest / service worker / 图标均可访问且形态正确。"""


def test_manifest_served_with_content_type(api):
    r = api.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "application/manifest+json" in r.headers["content-type"]
    d = r.json()
    assert d["name"].startswith("PicNamer")
    assert d["display"] == "standalone"
    sizes = {i["sizes"] for i in d["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(i.get("purpose") == "maskable" for i in d["icons"])


def test_sw_served_and_never_caches_api(api):
    r = api.get("/sw.js")
    assert r.status_code == 200
    assert "picnamer-v" in r.text
    # API 与写操作必须绕过 SW 缓存（数据实时性红线）
    assert 'startsWith("/api/")' in r.text
    assert 'e.request.method !== "GET"' in r.text


def test_icons_served(api):
    for path in ("/icons/icon-192.png", "/icons/icon-512.png",
                 "/icons/icon-512-maskable.png", "/icons/apple-touch-icon-180.png",
                 "/icons/favicon-32.png"):
        r = api.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("image/png"), path


def test_index_links_pwa_assets(api):
    r = api.get("/")
    assert r.status_code == 200
    assert 'rel="manifest"' in r.text
    assert 'rel="apple-touch-icon"' in r.text
    assert 'name="theme-color"' in r.text
