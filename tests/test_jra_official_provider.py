import hashlib
import os
import time
from pathlib import Path

from services.data_provider.jra_official import JraOfficialProvider
from services.parser.race_parser import RaceParser


def test_jra_provider_extracts_course_and_race_urls(tmp_path):
    provider = JraOfficialProvider(cache_dir=tmp_path, min_interval_seconds=0)
    selection_html = """
    <a href="#" onclick="return doAction('/JRADB/accessD.html', 'pw01drl00012026010820260816/D1');">1回札幌8日</a>
    <a href="#" onclick="return doAction('/JRADB/accessD.html', 'pw01drl00042026020820260816/E0');">2回新潟8日</a>
    """
    race_list_html = """
    <a href="/JRADB/accessD.html?CNAME=pw01dde0101202601081120260816/50"><img alt="11レース"></a>
    """

    assert provider._find_course_cname(selection_html, "2026-08-16", "札幌") == "pw01drl00012026010820260816/D1"
    assert provider._find_race_card_href(race_list_html, 11) == "/JRADB/accessD.html?CNAME=pw01dde0101202601081120260816/50"


def test_jra_provider_parses_daily_schedule(tmp_path):
    provider = JraOfficialProvider(cache_dir=tmp_path, min_interval_seconds=0)
    selection_html = """
    <a href="#" onclick="return doAction('/JRADB/accessD.html', 'pw01drl00042026030420260830/93');">3回新潟4日</a>
    <a href="#" onclick="return doAction('/JRADB/accessD.html', 'pw01drl00072026030420260830/71');">3回中京4日</a>
    """
    html = Path("fixtures/jra_race_schedule_sample.html").read_text(encoding="utf-8")

    assert provider._parse_course_options(selection_html, "2026-08-30") == [
        ("新潟", "pw01drl00042026030420260830/93"),
        ("中京", "pw01drl00072026030420260830/71"),
    ]
    schedule = provider._parse_race_schedule(html, "2026-08-30", "新潟")
    assert [(item.race_number, item.start_time) for item in schedule] == [(1, "09:40"), (2, "10:15"), (11, "15:45")]
    assert schedule[1].race_type == "obstacle"
    assert schedule[2].source_url == "https://www.jra.go.jp/JRADB/accessD.html?CNAME=race11"


def test_jra_provider_ignores_expired_cache(tmp_path):
    cache_id = "POST https://www.jra.go.jp/JRADB/accessD.html cname=pw01dli00/F3"
    cache_key = hashlib.sha256(cache_id.encode("utf-8")).hexdigest()
    body_path = tmp_path / f"{cache_key}.html"
    body_path.write_text("old", encoding="utf-8")
    old_time = time.time() - 600
    os.utime(body_path, (old_time, old_time))
    provider = JraOfficialProvider(cache_dir=tmp_path, min_interval_seconds=0, cache_ttl_seconds=300)

    assert provider._read_cached(cache_id, force_refresh=False) is None

    body_path.write_text("fresh", encoding="utf-8")
    assert provider._read_cached(cache_id, force_refresh=False) == "fresh"


def test_jra_provider_extracts_result_urls(tmp_path):
    provider = JraOfficialProvider(cache_dir=tmp_path, min_interval_seconds=0)
    selection_html = """
    <a href="#" onclick="return doAction('/JRADB/accessS.html', 'pw01srl10012026010520260808/F9');">1回札幌5日</a>
    """
    race_list_html = """
    <a href="/JRADB/accessS.html?CNAME=pw01sde1001202601051120260808/B2"><img alt="11レース"></a>
    """

    assert (
        provider._find_course_cname(
            selection_html,
            "2026-08-08",
            "札幌",
            action_path="/JRADB/accessS.html",
            cname_marker="srl",
        )
        == "pw01srl10012026010520260808/F9"
    )
    assert provider._find_race_href(race_list_html, 11, "/JRADB/accessS.html") == "/JRADB/accessS.html?CNAME=pw01sde1001202601051120260808/B2"


def test_jra_provider_returns_parsed_histories(tmp_path):
    provider = JraOfficialProvider(cache_dir=tmp_path, min_interval_seconds=0)
    parsed = RaceParser().parse_entries_html(open("fixtures/jra_official_sample.html", encoding="utf-8").read())

    provider._last_race = parsed.race
    provider._last_entries = parsed.entries
    provider._last_histories = parsed.histories or {}

    history = provider.get_horse_history(parsed.entries[0].horse)
    assert len(history) == 2
    assert history[0]["finish_position"] == 3


def test_jra_provider_extracts_baba_tab_and_condition(tmp_path):
    provider = JraOfficialProvider(cache_dir=tmp_path, min_interval_seconds=0)
    html = """
    <a href="/facilities/race/sapporo/">札幌競馬場</a>
    <div class="kaisai_tab">
      <div><a href="index.html">新潟競馬場</a></div>
      <div><a href="index2.html">中京競馬場</a></div>
      <div><a href="index3.html">札幌競馬場</a></div>
    </div>
    """

    assert provider._find_baba_href(html, "札幌") == "index3.html"

    condition_html = """
    <div id="condition"><h2>第1回札幌競馬第8日<span class="date">（2026年8月16日（日曜））</span></h2></div>
    <div id="course_condition">
      <h3>馬場状態<span class="time">（8月16日（日曜）午前9時現在）</span></h3>
      <div class="cell weather"><div class="txt">天候：<strong>晴</strong></div></div>
      <div class="cell turf"><div class="content"><p>良</p></div></div>
      <div class="cell dirt"><div class="content"><p>稍重</p></div></div>
    </div>
    """
    condition = provider._parse_baba_condition(condition_html)

    assert condition["weather"] == "晴"
    assert condition["turf"] == "良"
    assert condition["dirt"] == "稍重"
    assert "午前9時" in condition["updated_at_label"]


def test_jra_provider_applies_baba_condition_to_race(tmp_path):
    provider = JraOfficialProvider(cache_dir=tmp_path, min_interval_seconds=0)
    parsed = RaceParser().parse_entries_html(open("fixtures/jra_official_sample.html", encoding="utf-8").read())
    provider.fetch_baba_condition = lambda racecourse: {"weather": "晴", "turf": "良", "dirt": "稍重", "source": "jra_baba"}

    provider._apply_baba_condition(parsed.race)

    assert parsed.race.weather.value == "晴"
    assert parsed.race.track_condition.value == "良"
    assert parsed.race.track_condition.source == "jra_baba"
