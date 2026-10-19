"""v0.9.0 — filtro de URLs de fontes web (http/https, sem redirects/ads do DDG)."""

import httpx

from app import web_search


def _ddg_transport() -> httpx.MockTransport:
    """Instant Answer com fallback duckduckgo.com + HTML com ad/redirect e link real."""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.duckduckgo.com":
            return httpx.Response(200, json={
                "Heading": "Rome",
                "AbstractText": "Rome is the capital of Italy.",
                "AbstractURL": "https://duckduckgo.com",  # fallback junk — deve cair
                "RelatedTopics": [
                    {"Text": "Roman Empire history", "FirstURL": "https://en.wikipedia.org/wiki/Roman_Empire"},
                    {"Text": "Malicious", "FirstURL": "javascript:alert(1)"},
                ],
            })
        if host == "html.duckduckgo.com":
            body = (
                # Forma real vista no smoke: y.js com u3= e SEM uddg.
                '<a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=viator.com&u3=abcdef">Ad</a>'
                '<a class="result__snippet">Buy tickets</a>'
                # y.js cujo uddg aponta para o ANUNCIANTE — não pode passar.
                '<a class="result__a" href="https://duckduckgo.com/y.js?uddg=https%3A%2F%2Fwww.viator.com%2FItaly-tourism%2F">Tour</a>'
                '<a class="result__snippet">Skip the line</a>'
                '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.livius.org%2Farticles%2Fplace%2Frome%2F">Rome - Livius</a>'
                '<a class="result__snippet">Ancient Rome history</a>'
            )
            return httpx.Response(200, text=body)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_ddg_sources_drop_ads_redirects_and_bad_schemes():
    web_search.clear_cache()
    results = web_search._search_duckduckgo("rome", max_results=6, transport=_ddg_transport())
    urls = [r["url"] for r in results]
    # Abstract com AbstractURL=duckduckgo.com cai; javascript: cai; ads y.js caem
    # (mesmo quando o uddg aponta para o anunciante).
    assert all(u.startswith("https://") for u in urls)
    assert all("duckduckgo.com" not in u for u in urls)
    assert all("viator.com" not in u for u in urls)
    assert "https://en.wikipedia.org/wiki/Roman_Empire" in urls
    assert "https://www.livius.org/articles/place/rome/" in urls


def test_safe_url_unit():
    assert web_search._safe_url("javascript:alert(1)") == ""
    assert web_search._safe_url("data:text/html,x") == ""
    assert web_search._safe_url("https://duckduckgo.com/y.js?x=1") == ""
    assert web_search._safe_url("") == ""
    assert web_search._safe_url("https://en.wikipedia.org/wiki/Rome") == "https://en.wikipedia.org/wiki/Rome"
