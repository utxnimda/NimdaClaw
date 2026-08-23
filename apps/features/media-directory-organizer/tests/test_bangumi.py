from __future__ import annotations

import io
import json
import unittest
from urllib.error import HTTPError, URLError

from media_directory_organizer.bangumi import (
    BangumiLookupError,
    search_bangumi_anime,
)


class _FakeResponse:
    def __init__(
        self,
        payload: object = None,
        *,
        raw: bytes | None = None,
        status: int = 200,
        url: str = "https://api.bgm.tv/v0/search/subjects?limit=6&offset=0",
        headers: object = None,
    ) -> None:
        self._raw = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.status = status
        self._url = url
        self.headers = headers or {}
        self.read_amount: int | None = None
        self.closed = False

    def read(self, amount: int) -> bytes:
        self.read_amount = amount
        return self._raw[:amount]

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        self.closed = True


class _CaptureOpener:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.request = None
        self.timeout = None

    def open(self, request, *, timeout):
        self.request = request
        self.timeout = timeout
        return self.response


def _subject(
    subject_id: int,
    name: str,
    *,
    name_cn: str = "",
    air_date: str = "",
    subject_type: int = 2,
    infobox: object = None,
    platform: str = "TV",
) -> dict[str, object]:
    return {
        "id": subject_id,
        "type": subject_type,
        "name": name,
        "name_cn": name_cn,
        "date": air_date,
        "platform": platform,
        "infobox": infobox or [],
        "eps": 25,
        "total_episodes": 25,
        "summary": "summary",
        "rating": {"score": 4.7, "rank": 10022},
    }


class BangumiClientTest(unittest.TestCase):
    def test_request_contract_normalization_mapping_and_ranking(self) -> None:
        response = _FakeResponse(
            {
                "total": 3,
                "limit": 6,
                "offset": 0,
                "data": [
                    _subject(9, "Fate/stay night", air_date="2006-01-07"),
                    _subject(
                        204855,
                        "Fate/Apocrypha",
                        name_cn="命运/外典",
                        air_date="2017-07-01",
                        infobox=[
                            {
                                "key": "别名",
                                "value": [
                                    {"k": "日文名", "v": "フェイト/アポクリファ"},
                                    {"v": "f/a"},
                                ],
                            },
                            {"key": "播放结束", "value": "2017年12月30日"},
                        ],
                    ),
                    _subject(7, "Fate/Apocrypha", subject_type=6),
                    _subject(-1, "Fate/Apocrypha"),
                ],
            }
        )
        opener = _CaptureOpener(response)

        result = search_bangumi_anime("  Fate／Apocrypha  ", opener=opener)

        self.assertEqual(result["query"], "Fate/Apocrypha")
        self.assertEqual(
            result["search_url"],
            "https://bangumi.tv/subject_search/Fate%2FApocrypha?cat=2",
        )
        self.assertEqual(result["total"], 3)
        self.assertEqual([item["id"] for item in result["candidates"]], [204855, 9])
        selected = result["candidates"][0]
        self.assertEqual(selected["name_cn"], "命运/外典")
        self.assertEqual(selected["aliases"], ["フェイト/アポクリファ", "f/a"])
        self.assertEqual(selected["date"], "2017-07-01")
        self.assertEqual(selected["end_date"], "2017-12-30")
        self.assertEqual(selected["platform"], "TV")
        self.assertEqual(selected["eps"], 25)
        self.assertEqual(selected["total_episodes"], 25)
        self.assertEqual(selected["score"], 4.7)
        self.assertEqual(selected["rank"], 10022)
        self.assertEqual(selected["subject_url"], "https://bangumi.tv/subject/204855")
        self.assertEqual(selected["reasons"], ["原名完全匹配"])
        self.assertNotIn("selected", selected)
        self.assertNotIn("auto_selected", selected)

        request = opener.request
        self.assertIsNotNone(request)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.full_url,
            "https://api.bgm.tv/v0/search/subjects?limit=6&offset=0",
        )
        self.assertEqual(opener.timeout, 6.0)
        headers = {name.casefold(): value for name, value in request.header_items()}
        self.assertEqual(
            headers["user-agent"],
            "utxnimda/NimdaClaw/1.0.0 (https://github.com/utxnimda/NimdaClaw)",
        )
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {
                "keyword": "Fate/Apocrypha",
                "sort": "match",
                "filter": {"type": [2], "nsfw": False},
            },
        )
        self.assertEqual(response.read_amount, 2 * 1024 * 1024 + 1)
        self.assertTrue(response.closed)

    def test_limit_is_capped_at_ten(self) -> None:
        response = _FakeResponse({"total": 0, "data": []})
        opener = _CaptureOpener(response)

        search_bangumi_anime("test", limit=999, timeout=2, opener=opener)

        self.assertEqual(
            opener.request.full_url,
            "https://api.bgm.tv/v0/search/subjects?limit=10&offset=0",
        )
        self.assertEqual(opener.timeout, 2.0)

    def test_query_and_transport_arguments_are_validated_before_open(self) -> None:
        calls = []

        def opener(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("must not open")

        for query in ("", " \t\n ", "x" * 201):
            with self.subTest(query_length=len(query)):
                with self.assertRaises(ValueError):
                    search_bangumi_anime(query, opener=opener)
        for kwargs in ({"limit": 0}, {"limit": True}, {"timeout": 0}, {"timeout": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    search_bangumi_anime("test", opener=opener, **kwargs)
        self.assertEqual(calls, [])

    def test_year_breaks_equal_title_ties_and_upstream_order_is_stable(self) -> None:
        response = _FakeResponse(
            {
                "total": 3,
                "data": [
                    _subject(30, "Example", air_date="2018-01-01"),
                    _subject(10, "Example", air_date="2017-02-03"),
                    _subject(20, "Example", air_date="2017-04-05"),
                ],
            }
        )

        result = search_bangumi_anime("Example 2017", opener=_CaptureOpener(response))

        self.assertEqual([item["id"] for item in result["candidates"]], [10, 20, 30])
        self.assertIn("年份匹配", result["candidates"][0]["reasons"])
        self.assertIn("年份与查询不一致", result["candidates"][2]["reasons"])

    def test_alias_can_produce_an_exact_match_and_end_date_is_tolerant(self) -> None:
        response = _FakeResponse(
            {
                "total": 1,
                "data": [
                    _subject(
                        1,
                        "Original",
                        infobox=[
                            {"key": "Aliases", "value": {"value": "Target Alias"}},
                            {"key": "放送終了", "value": [{"v": "終了 2020/2/3"}]},
                        ],
                    )
                ],
            }
        )

        result = search_bangumi_anime("target alias", opener=_CaptureOpener(response))

        candidate = result["candidates"][0]
        self.assertEqual(candidate["aliases"], ["Target Alias"])
        self.assertEqual(candidate["end_date"], "2020-02-03")
        self.assertEqual(candidate["reasons"], ["别名完全匹配"])

    def test_malformed_and_oversized_responses_use_stable_errors(self) -> None:
        cases = [
            (_FakeResponse(raw=b"not-json"), "invalid_response"),
            (_FakeResponse({"total": 1, "data": {}}), "invalid_response"),
            (
                _FakeResponse(raw=b"x" * (2 * 1024 * 1024 + 1)),
                "response_too_large",
            ),
        ]
        for response, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(BangumiLookupError) as caught:
                    search_bangumi_anime("test", opener=_CaptureOpener(response))
                self.assertEqual(caught.exception.code, expected_code)
                self.assertNotIn("not-json", str(caught.exception))
                self.assertTrue(response.closed)

    def test_rate_limit_preserves_retry_after_without_leaking_upstream_detail(self) -> None:
        def opener(request, *, timeout):
            raise HTTPError(
                request.full_url,
                429,
                "private upstream diagnostic",
                {"Retry-After": "12"},
                io.BytesIO(b"secret body"),
            )

        with self.assertRaises(BangumiLookupError) as caught:
            search_bangumi_anime("test", opener=opener)

        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(caught.exception.retry_after, 12)
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))

    def test_network_failure_and_redirect_do_not_leak_or_follow(self) -> None:
        def failed_opener(request, *, timeout):
            raise URLError("private proxy and host detail")

        with self.assertRaises(BangumiLookupError) as network_error:
            search_bangumi_anime("test", opener=failed_opener)
        self.assertEqual(network_error.exception.code, "unavailable")
        self.assertNotIn("proxy", str(network_error.exception))

        redirected = _FakeResponse(
            {"total": 0, "data": []},
            url="http://127.0.0.1/internal",
        )
        with self.assertRaises(BangumiLookupError) as redirect_error:
            search_bangumi_anime("test", opener=_CaptureOpener(redirected))
        self.assertEqual(redirect_error.exception.code, "redirect_refused")
        self.assertTrue(redirected.closed)


if __name__ == "__main__":
    unittest.main()
