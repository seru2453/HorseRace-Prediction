from pathlib import Path

from services.parser.race_parser import RaceParser


def test_parse_structured_entries_fixture():
    html = Path("fixtures/sample_entries.html").read_text(encoding="utf-8")
    parsed = RaceParser().parse_entries_html(html)

    assert parsed.race.race_id == "20260816-sapporo-11"
    assert parsed.race.race_date.value == "2026-08-16"
    assert parsed.race.racecourse.value == "札幌"
    assert parsed.race.race_number.value == 11
    assert parsed.quality.status == "ok"
    assert parsed.quality.recognized_entries == 2
    assert parsed.entries[0].horse.horse_name.value == "アオゾラノート"
    assert parsed.entries[0].win_odds.value == 4.8
    assert parsed.entries[0].popularity.value == 2
    assert parsed.entries[0].career_starts.value == 12
    assert parsed.entries[0].career_win_rate.value == 4 / 12
    assert parsed.entries[0].career_top3_rate.value == 7 / 12
    assert parsed.entries[0].total_prize_money.value == 98000000


def test_parser_detects_duplicate_horse_numbers():
    html = """
    <section id="race-meta" data-race-id="r1" data-race-date="2026-08-16" data-racecourse="札幌" data-race-number="11" data-field-size="2"></section>
    <table id="entries"><tbody>
      <tr data-frame-number="1" data-horse-number="1" data-horse-name="A" data-jockey-name="佐藤" data-carried-weight="57"></tr>
      <tr data-frame-number="2" data-horse-number="1" data-horse-name="B" data-jockey-name="田中" data-carried-weight="55"></tr>
    </tbody></table>
    """
    parsed = RaceParser().parse_entries_html(html)

    assert parsed.quality.status == "needs_review"
    assert any(issue.code == "duplicate_horse_number" for issue in parsed.quality.issues)


def test_parse_jra_official_race_card_shape():
    html = Path("fixtures/jra_official_sample.html").read_text(encoding="utf-8")
    parsed = RaceParser().parse_entries_html(html)

    assert parsed.quality.status == "ok"
    assert parsed.race.race_date.value == "2026-08-16"
    assert parsed.race.racecourse.value == "札幌"
    assert parsed.race.race_number.value == 11
    assert parsed.race.race_name.value == "札幌記念"
    assert parsed.race.surface.value == "芝"
    assert parsed.race.distance.value == 2000
    assert parsed.entries[0].horse.horse_name.value == "オニャンコポン"
    assert parsed.entries[0].horse.sire.value == "エイシンフラッシュ"
    assert parsed.entries[0].horse.damsire.value == "ヴィクトワールピサ"
    assert parsed.entries[0].sex.value == "せん"
    assert parsed.entries[0].age.value == 7
    assert parsed.entries[0].win_odds.value == 12.3
    assert parsed.entries[0].popularity.value == 4
    assert parsed.entries[0].career_starts.value == 22
    assert parsed.entries[0].career_wins.value == 3
    assert parsed.entries[0].career_top3_rate.value == 7 / 22
    assert parsed.entries[0].total_prize_money.value == 123456000
    first_history = parsed.histories[parsed.entries[0].horse.horse_id]
    assert len(first_history) == 2
    assert first_history[0]["race_date"] == "2026-07-12"
    assert first_history[0]["racecourse"] == "福島"
    assert first_history[0]["finish_position"] == 3
    assert first_history[0]["surface"] == "芝"
    assert first_history[0]["distance"] == 2000
    assert first_history[0]["final_three_furlongs"] == 35.4
    assert first_history[0]["corner_positions"] == "14-14-13-7"


def test_parse_jra_obstacle_race_is_not_treated_as_flat_turf():
    html = Path("fixtures/jra_obstacle_sample.html").read_text(encoding="utf-8")

    parsed = RaceParser().parse_entries_html(html)

    assert parsed.race.surface.value == "障害"
    assert "障害" in parsed.race.race_class.value


def test_parse_jra_official_result_shape():
    html = Path("fixtures/jra_result_sample.html").read_text(encoding="utf-8")
    result = RaceParser().parse_result_html(html)

    assert result.race_id == "20260808-札幌-11"
    assert result.entries[0].horse_number == 3
    assert result.entries[0].horse_name == "ルクソールカフェ"
    assert result.entries[0].finish_position == 1
    assert result.entries[0].finish_time == "1:43.8"
    assert result.entries[0].final_three_furlongs == 37.1
    assert result.entries[0].corner_positions == "10-9-5-4"
    assert result.entries[0].popularity == 3
    assert result.entries[2].horse_number == 7
    assert result.entries[2].finish_position is None
    assert result.entries[2].result_status == "中止"
    assert result.entries[2].horse_weight == 500
    assert result.entries[2].horse_weight_change == 10
    assert result.entries[3].horse_number == 8
    assert result.entries[3].finish_position is None
    assert result.entries[3].result_status == "除外"
    assert result.entries[3].finish_time is None
    assert result.entries[3].horse_weight is None
