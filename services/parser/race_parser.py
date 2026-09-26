from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from services.common.models import (
    HorseData,
    ParseQuality,
    QualityIssue,
    RaceData,
    RaceEntryData,
    RaceResultData,
    RaceResultEntryData,
    field,
)


@dataclass(frozen=True)
class ParsedRace:
    race: RaceData
    entries: list[RaceEntryData]
    quality: ParseQuality
    histories: dict[str, list[dict]] | None = None


class RaceParser:
    def parse_result_html(self, html: str, fallback_race_id: str | None = None) -> RaceResultData:
        soup = BeautifulSoup(html, "html.parser")
        root = soup.select_one("#race_result")
        if not root:
            raise ValueError("JRA result table was not found.")
        date_text = self._text(root.select_one(".date_line .date"))
        race_date = self._parse_japanese_date(date_text)
        racecourse = self._parse_racecourse(date_text)
        current_race_img = root.select_one("ul.race-num li.current img, .race_number img")
        race_number = self._int_or_none(current_race_img.get("alt") if current_race_img else None)
        race_id = fallback_race_id or self._make_race_id(race_date, racecourse, race_number)

        entries: list[RaceResultEntryData] = []
        for row in root.select("tbody tr"):
            horse_number = self._int_or_none(self._text(row.select_one("td.num")))
            horse_name = self._text(row.select_one("td.horse a"))
            if not horse_number or not horse_name:
                continue
            place_text = self._text(row.select_one("td.place"))
            finish_position = self._finish_position_or_none(place_text)
            horse_weight, horse_weight_change = self._parse_horse_weight_change(self._text(row.select_one("td.h_weight")))
            corner_positions = "-".join(
                value for value in [self._text(node) for node in row.select("td.corner .corner_list li")] if value
            )
            entries.append(
                RaceResultEntryData(
                    horse_number=horse_number,
                    horse_name=horse_name,
                    finish_position=finish_position,
                    result_status=place_text if finish_position is None else None,
                    finish_time=self._none_if_status_text(self._text(row.select_one("td.time"))),
                    margin=self._text(row.select_one("td.margin")),
                    final_three_furlongs=self._float_or_none(self._text(row.select_one("td.f_time"))),
                    corner_positions=corner_positions or None,
                    horse_weight=horse_weight,
                    horse_weight_change=horse_weight_change,
                    jockey_name=self._text(row.select_one("td.jockey a, td.jockey")),
                    popularity=self._int_or_none(self._text(row.select_one("td.pop"))),
                )
            )
        if not entries:
            raise ValueError("JRA result rows were not parsed.")
        return RaceResultData(
            race_id=race_id,
            result_timestamp=datetime.now(timezone.utc).isoformat(),
            entries=entries,
        )

    def parse_entries_html(
        self,
        html: str,
        fallback_date: str | None = None,
        fallback_course: str | None = None,
        fallback_number: int | None = None,
    ) -> ParsedRace:
        soup = BeautifulSoup(html, "html.parser")
        if soup.select_one("#syutsuba"):
            parsed = self._parse_jra_official_html(soup, fallback_date, fallback_course, fallback_number)
            if parsed.entries:
                return parsed

        meta = soup.select_one("[data-race-id], #race-meta")
        race_date = self._meta_value(meta, "race-date") or fallback_date
        racecourse = self._meta_value(meta, "racecourse") or fallback_course
        race_number = self._int_or_none(self._meta_value(meta, "race-number") or fallback_number)
        race_id = self._meta_value(meta, "race-id") or self._make_race_id(race_date, racecourse, race_number)

        rows = soup.select("table#entries tbody tr, tr.race-entry, [data-horse-number]")
        entries: list[RaceEntryData] = []
        for row in rows:
            horse_number = self._int_or_none(self._cell(row, "horse-number"))
            horse_name = self._cell(row, "horse-name")
            if horse_number is None and not horse_name:
                continue
            horse_id = row.get("data-horse-id") or f"{race_id}-{horse_number or len(entries) + 1}"
            career_starts = self._int_or_none(self._cell(row, "career-starts"))
            career_wins = self._int_or_none(self._cell(row, "career-wins"))
            career_seconds = self._int_or_none(self._cell(row, "career-seconds"))
            career_thirds = self._int_or_none(self._cell(row, "career-thirds"))
            career_win_rate, career_top3_rate = self._career_rates(career_starts, career_wins, career_seconds, career_thirds)
            entries.append(
                RaceEntryData(
                    race_id=race_id,
                    horse=HorseData(
                        horse_id=horse_id,
                        horse_name=field(horse_name, 0.95 if horse_name else 0.0, "html"),
                        sire=field(self._cell(row, "sire") or None, 0.8 if self._cell(row, "sire") else 0.0, "html"),
                        dam=field(self._cell(row, "dam") or None, 0.8 if self._cell(row, "dam") else 0.0, "html"),
                        damsire=field(self._cell(row, "damsire") or None, 0.8 if self._cell(row, "damsire") else 0.0, "html"),
                    ),
                    frame_number=field(self._int_or_none(self._cell(row, "frame-number")), 0.95, "html"),
                    horse_number=field(horse_number, 0.98 if horse_number else 0.0, "html"),
                    sex=field(self._cell(row, "sex") or None, 0.8 if self._cell(row, "sex") else 0.0, "html"),
                    age=field(self._int_or_none(self._cell(row, "age")), 0.8 if self._cell(row, "age") else 0.0, "html"),
                    jockey_name=field(self._cell(row, "jockey-name") or None, 0.9 if self._cell(row, "jockey-name") else 0.0, "html"),
                    trainer_name=field(self._cell(row, "trainer-name") or None, 0.8 if self._cell(row, "trainer-name") else 0.0, "html"),
                    carried_weight=field(self._float_or_none(self._cell(row, "carried-weight")), 0.9 if self._cell(row, "carried-weight") else 0.0, "html"),
                    horse_weight=field(self._int_or_none(self._cell(row, "horse-weight")), 0.8 if self._cell(row, "horse-weight") else 0.0, "html"),
                    horse_weight_change=field(
                        self._int_or_none(self._cell(row, "horse-weight-change")),
                        0.8 if self._cell(row, "horse-weight-change") else 0.0,
                        "html",
                    ),
                    win_odds=field(self._float_or_none(self._cell(row, "win-odds")), 0.7 if self._cell(row, "win-odds") else 0.0, "html"),
                    popularity=field(self._int_or_none(self._cell(row, "popularity")), 0.7 if self._cell(row, "popularity") else 0.0, "html"),
                    career_starts=field(career_starts, 0.75 if career_starts is not None else 0.0, "html"),
                    career_wins=field(career_wins, 0.75 if career_wins is not None else 0.0, "html"),
                    career_seconds=field(career_seconds, 0.75 if career_seconds is not None else 0.0, "html"),
                    career_thirds=field(career_thirds, 0.75 if career_thirds is not None else 0.0, "html"),
                    career_win_rate=field(career_win_rate, 0.75 if career_win_rate is not None else 0.0, "html"),
                    career_top3_rate=field(career_top3_rate, 0.75 if career_top3_rate is not None else 0.0, "html"),
                    total_prize_money=field(self._parse_prize_money_yen(self._cell(row, "total-prize-money")), 0.7 if self._cell(row, "total-prize-money") else 0.0, "html"),
                    scratched=field(self._truthy(self._cell(row, "scratched")), 0.9 if self._cell(row, "scratched") else 1.0, "html"),
                )
            )

        expected_entries = self._int_or_none(self._meta_value(meta, "field-size")) or len(entries)
        race = RaceData(
            race_id=race_id,
            race_date=field(race_date, 0.98 if race_date else 0.0, "html" if race_date else "fallback"),
            racecourse=field(racecourse, 0.98 if racecourse else 0.0, "html" if racecourse else "fallback"),
            race_number=field(race_number, 0.98 if race_number else 0.0, "html" if race_number else "fallback"),
            race_name=field(self._meta_value(meta, "race-name"), 0.85 if self._meta_value(meta, "race-name") else 0.0, "html"),
            grade=field(self._meta_value(meta, "grade"), 0.7 if self._meta_value(meta, "grade") else 0.0, "html"),
            race_class=field(self._meta_value(meta, "race-class"), 0.7 if self._meta_value(meta, "race-class") else 0.0, "html"),
            surface=field(self._meta_value(meta, "surface"), 0.9 if self._meta_value(meta, "surface") else 0.0, "html"),
            distance=field(self._int_or_none(self._meta_value(meta, "distance")), 0.9 if self._meta_value(meta, "distance") else 0.0, "html"),
            direction=field(self._meta_value(meta, "direction"), 0.8 if self._meta_value(meta, "direction") else 0.0, "html"),
            start_time=field(self._meta_value(meta, "start-time"), 0.8 if self._meta_value(meta, "start-time") else 0.0, "html"),
            weather=field(self._meta_value(meta, "weather"), 0.7 if self._meta_value(meta, "weather") else 0.0, "html"),
            track_condition=field(self._meta_value(meta, "track-condition"), 0.7 if self._meta_value(meta, "track-condition") else 0.0, "html"),
            field_size=field(expected_entries, 0.95 if expected_entries else 0.0, "html"),
        )
        quality = self.validate(race, entries)
        return ParsedRace(race=race, entries=entries, quality=quality, histories={})

    def _parse_jra_official_html(
        self,
        soup: BeautifulSoup,
        fallback_date: str | None,
        fallback_course: str | None,
        fallback_number: int | None,
    ) -> ParsedRace:
        source = "jra_html"
        date_text = self._text(soup.select_one("#syutsuba .date_line .date"))
        race_date = self._parse_japanese_date(date_text) or fallback_date
        racecourse = self._parse_racecourse(date_text) or fallback_course
        current_race_img = soup.select_one("ul.race-num li.current img, #syutsuba .race_number img")
        race_number = self._int_or_none(current_race_img.get("alt") if current_race_img else None) or fallback_number
        race_id = self._make_race_id(race_date, racecourse, race_number)

        race_name = self._text(soup.select_one("#syutsuba .race_name")) or self._text(soup.select_one("#syutsuba h2 .main"))
        grade_img = soup.select_one("#syutsuba .race_name img[alt], #syutsuba .grade_icon img[alt]")
        grade = grade_img.get("alt") if grade_img else None
        type_values = [self._text(node) for node in soup.select("#syutsuba .type .cell")]
        race_class = " ".join(value for value in type_values if value and not value.startswith("コース"))
        course_text = self._text(soup.select_one("#syutsuba .type .course"))
        distance = self._int_or_none(course_text)
        surface, direction = self._parse_course_detail(course_text)
        if "障害" in race_class:
            surface = "障害"
        start_time = self._text(soup.select_one("#syutsuba .date_line .time strong"))

        entries: list[RaceEntryData] = []
        histories: dict[str, list[dict]] = {}
        for row in soup.select("#syutsuba tbody tr"):
            horse_cell = row.select_one("td.horse")
            jockey_cell = row.select_one("td.jockey")
            horse_number = self._int_or_none(self._text(row.select_one("td.num")))
            horse_name = self._text(row.select_one("td.horse .name a, td.horse .name"))
            if not horse_number or not horse_name:
                continue
            frame_img = row.select_one("td.waku img[alt]")
            frame_number = self._int_or_none(frame_img.get("alt") if frame_img else None)
            horse_link = row.select_one("td.horse .name a[href]")
            horse_id = self._horse_id_from_href(horse_link.get("href") if horse_link else None) or f"{race_id}-{horse_number:02d}"
            sire = self._strip_label(self._text(horse_cell.select_one(".family_line .sire")) if horse_cell else None, "父：")
            mare_text = self._text(horse_cell.select_one(".family_line .mare")) if horse_cell else None
            dam, damsire = self._parse_mare_line(mare_text)
            sex, age = self._parse_sex_age(self._text(jockey_cell.select_one(".age")) if jockey_cell else None)
            win_odds = self._float_or_none(self._text(horse_cell.select_one(".odds_line .num strong, .odds .num strong, .odds .num")) if horse_cell else None)
            popularity = self._int_or_none(self._text(horse_cell.select_one(".odds_line .pop_rank, .odds .pop_rank")) if horse_cell else None)
            career_starts, career_wins, career_seconds, career_thirds = self._parse_career_record(self._text(horse_cell.select_one(".result_line .result")) if horse_cell else None)
            career_win_rate, career_top3_rate = self._career_rates(career_starts, career_wins, career_seconds, career_thirds)
            total_prize_money = self._parse_prize_money_yen(self._text(horse_cell.select_one(".result_line .win")) if horse_cell else None)
            entries.append(
                RaceEntryData(
                    race_id=race_id,
                    horse=HorseData(
                        horse_id=horse_id,
                        horse_name=field(horse_name, 0.95, source),
                        sire=field(sire, 0.85 if sire else 0.0, source),
                        dam=field(dam, 0.85 if dam else 0.0, source),
                        damsire=field(damsire, 0.85 if damsire else 0.0, source),
                    ),
                    frame_number=field(frame_number, 0.95 if frame_number else 0.0, source),
                    horse_number=field(horse_number, 0.98, source),
                    sex=field(sex, 0.85 if sex else 0.0, source),
                    age=field(age, 0.85 if age else 0.0, source),
                    jockey_name=field(self._text(jockey_cell.select_one(".jockey a, .jockey")) if jockey_cell else None, 0.9, source),
                    trainer_name=field(self._text(horse_cell.select_one(".trainer a, .trainer")) if horse_cell else None, 0.85, source),
                    carried_weight=field(self._float_or_none(self._text(jockey_cell.select_one(".weight")) if jockey_cell else None), 0.9, source),
                    horse_weight=field(None, 0.0, source),
                    horse_weight_change=field(None, 0.0, source),
                    win_odds=field(win_odds, 0.8 if win_odds is not None else 0.0, source),
                    popularity=field(popularity, 0.8 if popularity is not None else 0.0, source),
                    career_starts=field(career_starts, 0.8 if career_starts is not None else 0.0, source),
                    career_wins=field(career_wins, 0.8 if career_wins is not None else 0.0, source),
                    career_seconds=field(career_seconds, 0.8 if career_seconds is not None else 0.0, source),
                    career_thirds=field(career_thirds, 0.8 if career_thirds is not None else 0.0, source),
                    career_win_rate=field(career_win_rate, 0.8 if career_win_rate is not None else 0.0, source),
                    career_top3_rate=field(career_top3_rate, 0.8 if career_top3_rate is not None else 0.0, source),
                    total_prize_money=field(total_prize_money, 0.75 if total_prize_money is not None else 0.0, source),
                    scratched=field("cancel" in row.get("class", []), 0.8, source),
                )
            )
            histories[horse_id] = self._parse_jra_past_performances(row)

        race = RaceData(
            race_id=race_id,
            race_date=field(race_date, 0.98 if race_date else 0.0, source),
            racecourse=field(racecourse, 0.98 if racecourse else 0.0, source),
            race_number=field(race_number, 0.98 if race_number else 0.0, source),
            race_name=field(race_name, 0.9 if race_name else 0.0, source),
            grade=field(grade, 0.8 if grade else 0.0, source),
            race_class=field(race_class or None, 0.75 if race_class else 0.0, source),
            surface=field(surface, 0.9 if surface else 0.0, source),
            distance=field(distance, 0.9 if distance else 0.0, source),
            direction=field(direction, 0.8 if direction else 0.0, source),
            start_time=field(start_time, 0.85 if start_time else 0.0, source),
            field_size=field(len(entries), 0.95 if entries else 0.0, source),
        )
        return ParsedRace(race=race, entries=entries, quality=self.validate(race, entries), histories=histories)

    def validate(self, race: RaceData, entries: list[RaceEntryData]) -> ParseQuality:
        issues: list[QualityIssue] = []
        expected = race.field_size.value
        numbers = [entry.horse_number.value for entry in entries if entry.horse_number.value is not None]
        duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
        for number in duplicates:
            issues.append(QualityIssue(code="duplicate_horse_number", message="馬番が重複しています。", field="horse_number", horse_number=number))
        if expected and expected != len(entries):
            issues.append(QualityIssue(code="field_size_mismatch", message="出走頭数と認識数が一致しません。", field="field_size"))
        for required in ("race_date", "racecourse", "race_number"):
            if getattr(race, required).value in (None, ""):
                issues.append(QualityIssue(code="missing_race_field", message=f"{required} が取得できません。", field=required))
        for entry in entries:
            number = entry.horse_number.value
            if not entry.horse.horse_name.value:
                issues.append(QualityIssue(code="missing_horse_name", message="馬名が空です。", field="horse_name", horse_number=number))
            if not entry.jockey_name.value:
                issues.append(QualityIssue(code="missing_jockey", message="騎手名が空です。", field="jockey_name", horse_number=number))
            if entry.frame_number.value is not None and not 1 <= int(entry.frame_number.value) <= 8:
                issues.append(QualityIssue(code="invalid_frame", message="枠番が範囲外です。", field="frame_number", horse_number=number))
            if entry.carried_weight.value is not None and not isinstance(entry.carried_weight.value, (int, float)):
                issues.append(QualityIssue(code="invalid_weight", message="斤量を数値として読めません。", field="carried_weight", horse_number=number))
        status = "ok" if not issues else "needs_review"
        return ParseQuality(status=status, recognized_entries=len(entries), expected_entries=expected, issues=issues)

    def _cell(self, row, name: str) -> str | None:
        data_name = f"data-{name}"
        if row and row.has_attr(data_name):
            return self._clean(row.get(data_name))
        target = row.select_one(f"[data-field='{name}'], .{name}, .{name.replace('-', '_')}")
        if target:
            return self._clean(target.get_text(" ", strip=True))
        return None

    def _meta_value(self, meta, name: str) -> str | None:
        if not meta:
            return None
        data_name = f"data-{name}"
        if meta.has_attr(data_name):
            return self._clean(meta.get(data_name))
        target = meta.select_one(f"[data-field='{name}'], .{name}, .{name.replace('-', '_')}")
        if target:
            return self._clean(target.get_text(" ", strip=True))
        return None

    def _make_race_id(self, race_date: str | None, racecourse: str | None, race_number: int | None) -> str:
        safe_date = (race_date or "unknown").replace("-", "")
        safe_course = racecourse or "unknown"
        safe_number = race_number or 0
        return f"{safe_date}-{safe_course}-{safe_number:02d}"

    def _clean(self, value: object) -> str | None:
        if value is None:
            return None
        text = re.sub(r"\s+", " ", str(value)).strip()
        return text or None

    def _text(self, node) -> str | None:
        if not node:
            return None
        return self._clean(node.get_text(" ", strip=True))

    def _parse_japanese_date(self, value: str | None) -> str | None:
        if not value:
            return None
        match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", value)
        if not match:
            return None
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"

    def _parse_racecourse(self, value: str | None) -> str | None:
        if not value:
            return None
        match = re.search(r"\d+回([^\d\s]+)\d+日", value)
        return match.group(1) if match else None

    def _parse_course_detail(self, value: str | None) -> tuple[str | None, str | None]:
        if not value:
            return None, None
        match = re.search(r"（([^・）]+)(?:・([^）]+))?）", value)
        if not match:
            return None, None
        return match.group(1), match.group(2)

    def _horse_id_from_href(self, href: str | None) -> str | None:
        if not href:
            return None
        match = re.search(r"CNAME=([^/&]+)", href)
        return match.group(1) if match else None

    def _strip_label(self, value: str | None, label: str) -> str | None:
        if not value:
            return None
        return self._clean(value.replace(label, "", 1))

    def _parse_mare_line(self, value: str | None) -> tuple[str | None, str | None]:
        if not value:
            return None, None
        dam = self._strip_label(re.sub(r"\(母の父：.*?[）)]", "", value), "母：")
        damsire_match = re.search(r"母の父：([^）)]+)", value)
        return dam, damsire_match.group(1) if damsire_match else None

    def _parse_sex_age(self, value: str | None) -> tuple[str | None, int | None]:
        if not value:
            return None, None
        match = re.search(r"(牡|牝|せん)(\d+)", value)
        if not match:
            return None, None
        return match.group(1), int(match.group(2))

    def _parse_career_record(self, value: str | None) -> tuple[int | None, int | None, int | None, int | None]:
        if not value:
            return None, None, None, None
        match = re.search(r"\((\d+)\.(\d+)\.(\d+)\.(\d+)\)", value)
        if not match:
            return None, None, None, None
        wins = int(match.group(1))
        seconds = int(match.group(2))
        thirds = int(match.group(3))
        others = int(match.group(4))
        return wins + seconds + thirds + others, wins, seconds, thirds

    def _career_rates(
        self,
        starts: int | None,
        wins: int | None,
        seconds: int | None,
        thirds: int | None,
    ) -> tuple[float | None, float | None]:
        if not starts or wins is None or seconds is None or thirds is None:
            return None, None
        return wins / starts, (wins + seconds + thirds) / starts

    def _parse_prize_money_yen(self, value: str | None) -> int | None:
        if not value:
            return None
        amount = self._float_or_none(value)
        if amount is None:
            return None
        if "万円" in value:
            return int(round(amount * 10_000))
        if "億円" in value:
            return int(round(amount * 100_000_000))
        return int(round(amount))

    def _parse_jra_past_performances(self, row) -> list[dict]:
        history = []
        for cell in row.select("td.past"):
            race_date = self._parse_japanese_date(self._text(cell.select_one(".date_line .date, .date")))
            finish_position = self._int_or_none(self._text(cell.select_one(".place_line .place, .place")))
            distance_surface = self._text(cell.select_one(".dist"))
            distance = self._int_or_none(distance_surface)
            surface = self._surface_from_distance_text(distance_surface)
            horse_weight, horse_weight_change = self._parse_horse_weight_change(self._text(cell.select_one(".h_weight")))
            item = {
                "race_date": race_date,
                "racecourse": self._text(cell.select_one(".date_line .rc, .rc")),
                "race_name": self._text(cell.select_one(".race_line .name, .name")),
                "finish_position": finish_position,
                "field_size": self._int_or_none(self._text(cell.select_one(".place_line .max, .max"))),
                "gate_number": self._int_or_none(self._text(cell.select_one(".place_line .gate, .gate"))),
                "popularity": self._int_or_none(self._text(cell.select_one(".place_line .pop, .pop"))),
                "jockey_name": self._text(cell.select_one(".info_line1 .jockey, .jockey")),
                "carried_weight": self._float_or_none(self._text(cell.select_one(".info_line1 .weight, .weight"))),
                "surface": surface,
                "distance": distance,
                "track_condition": self._text(cell.select_one(".condition")),
                "finish_time": self._text(cell.select_one(".time")),
                "final_three_furlongs": self._final_three_furlongs_or_none(self._text(cell.select_one(".f3"))),
                "horse_weight": horse_weight,
                "horse_weight_change": horse_weight_change,
                "corner_positions": "-".join(
                    value for value in [self._text(node) for node in cell.select(".corner_list li")] if value
                )
                or None,
            }
            if any(value is not None for value in item.values()):
                history.append(item)
        return history

    def _surface_from_distance_text(self, value: str | None) -> str | None:
        if not value:
            return None
        if "芝" in value:
            return "芝"
        if "ダ" in value:
            return "ダート"
        if "障" in value:
            return "障害"
        return None

    def _finish_position_or_none(self, value: str | None) -> int | None:
        if not value:
            return None
        if value in {"取消", "除外", "中止", "失格", "降着"}:
            return None
        return self._int_or_none(value)

    def _none_if_status_text(self, value: str | None) -> str | None:
        if value in {"取消", "除外", "中止", "失格", "降着"}:
            return None
        return value

    def _parse_horse_weight_change(self, value: str | None) -> tuple[int | None, int | None]:
        if not value:
            return None, None
        weight = self._int_or_none(value)
        change_match = re.search(r"[（(]([+-]?\d+|増減なし|計不)[）)]", value)
        if not change_match:
            return weight, None
        raw_change = change_match.group(1)
        if raw_change == "増減なし":
            return weight, 0
        if raw_change == "計不":
            return None, None
        return weight, self._int_or_none(raw_change)

    def _final_three_furlongs_or_none(self, value: str | None) -> float | None:
        if not value:
            return None
        match = re.search(r"3F\s*(\d+(?:\.\d+)?)", value)
        if match:
            return float(match.group(1))
        return self._float_or_none(value)

    def _int_or_none(self, value: object) -> int | None:
        if value in (None, ""):
            return None
        match = re.search(r"-?\d[\d,]*", str(value))
        return int(match.group(0).replace(",", "")) if match else None

    def _float_or_none(self, value: object) -> float | None:
        if value in (None, ""):
            return None
        match = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(value))
        return float(match.group(0).replace(",", "")) if match else None

    def _truthy(self, value: object) -> bool:
        return str(value or "").lower() in {"1", "true", "yes", "取消", "除外", "scratched"}
