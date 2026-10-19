"""Маски robots.txt проверяются без сторонних сетевых библиотек."""

from app.catalog.robots import RobotsPolicy


def test_actual_site_rules():
    policy = RobotsPolicy(
        "User-agent: *\nDisallow: /api/*\nDisallow: *?*\nAllow: *page=", "SvoeVinoCatalogMVP"
    )
    assert policy.allows("/wines")
    assert policy.allows("/wines?page=2")
    assert not policy.allows("/wines?filter=red")
    assert not policy.allows("/api/wines")


def test_specific_group_and_equal_rule_allow():
    policy = RobotsPolicy(
        "User-agent: *\nDisallow: /\nUser-agent: SvoeVino\nDisallow: /wines\nAllow: /wines\nDisallow: /private$",
        "SvoeVinoCatalogMVP",
    )
    assert policy.allows("/wines")
    assert not policy.allows("/private")
    assert policy.allows("/private/other")


def test_multiple_agents_comments_and_delays():
    policy = RobotsPolicy(
        "User-agent: Other\nUser-agent: SvoeVino\nDisallow: /private # комментарий\nCrawl-delay: 2\nUser-agent: SvoeVino\nDisallow: /api\nCrawl-delay: nan",
        "SvoeVino",
    )
    assert not policy.allows("/private")
    assert not policy.allows("/api")
    assert policy.delay == 2
