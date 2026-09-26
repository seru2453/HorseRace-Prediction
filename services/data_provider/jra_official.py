from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from services.common.models import HorseData, RaceData, RaceEntryData, RaceResultData, RaceScheduleItem, field
from services.data_provider.base import ProviderUnavailableError, RacingDataProvider


class JraOfficialProvider(RacingDataProvider):
    allowed_hosts = {"www.jra.go.jp", "jra.jp"}

    def __init__(
        self,
        cache_dir: Path | None = None,
        min_interval_seconds: float = 5.0,
        timeout: float = 10.0,
        cache_ttl_seconds: float | None = 300.0,
    ) -> None:
        self.cache_dir = cache_dir or Path("data/cache/jra_official")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval_seconds = min_interval_seconds
        self.timeout = timeout
        self.cache_ttl_seconds = cache_ttl_seconds
        self._last_fetch_at = 0.0
        self._last_race: RaceData | None = None
        self._last_entries: list[RaceEntryData] = []
        self._last_histories: dict[str, list] = {}
        self._last_baba_condition: dict = {}
        self._race_url_cache: dict[str, str] = {}

    def list_races(self, race_date: str) -> list[RaceScheduleItem]:
        selection_html = self.fetch_action("/JRADB/accessD.html", "pw01dli00/F3")
        courses = self._parse_course_options(selection_html, race_date)
        if not courses:
            raise ProviderUnavailableError("JRA official race-card selection did not list the requested date.")

        schedule: list[RaceScheduleItem] = []
        for racecourse, cname in courses:
            race_list_html = self.fetch_action("/JRADB/accessD.html", cname)
            items = self._parse_race_schedule(race_list_html, race_date, racecourse)
            for item in items:
                if item.source_url:
                    self._race_url_cache[self._race_lookup_key(race_date, racecourse, item.race_number)] = item.source_url
            schedule.extend(items)
        if not schedule:
            raise ProviderUnavailableError("JRA official race schedule was found, but no races could be parsed.")
        return sorted(schedule, key=lambda item: (item.start_time, item.racecourse, item.race_number))

    def get_race(self, race_date: str, racecourse: str, race_number: int) -> RaceData:
        url = self.resolve_race_card_url(race_date, racecourse, int(race_number))
        html = self.fetch_url(url)
        from services.parser.race_parser import RaceParser

        parsed = RaceParser().parse_entries_html(html, fallback_date=race_date, fallback_course=racecourse, fallback_number=race_number)
        if not parsed.entries:
            raise ProviderUnavailableError("JRA official race card was found, but entries were not available or could not be parsed.")
        self._apply_baba_condition(parsed.race)
        self._last_race = parsed.race
        self._last_entries = parsed.entries
        self._last_histories = parsed.histories or {}
        return parsed.race

    def get_race_entries(self, race_id: str) -> list[RaceEntryData]:
        if not self._last_race or self._last_race.race_id != race_id:
            raise ProviderUnavailableError("Race entries are available only immediately after get_race in live MVP mode.")
        return self._last_entries

    def get_horse_history(self, horse: HorseData) -> list:
        return self._last_histories.get(horse.horse_id, [])

    def get_race_results(self, race_id: str) -> RaceResultData | None:
        race_date, racecourse, race_number = self._parse_race_id(race_id)
        url = self.resolve_race_result_url(race_date, racecourse, race_number)
        html = self.fetch_url(url)
        from services.parser.race_parser import RaceParser

        try:
            return RaceParser().parse_result_html(html, fallback_race_id=race_id)
        except ValueError as exc:
            raise ProviderUnavailableError("JRA official race result was not available or could not be parsed.") from exc

    def get_race_updates(self, race_id: str) -> dict:
        return {
            "race_id": race_id,
            "source": "jra_official",
            "has_late_changes": None,
            "baba_condition": self._last_baba_condition or None,
        }

    def resolve_race_card_url(self, race_date: str, racecourse: str, race_number: int) -> str:
        cached = self._race_url_cache.get(self._race_lookup_key(race_date, racecourse, race_number))
        if cached:
            return cached
        indexed = self._resolve_from_url_index(race_date, racecourse, race_number)
        if indexed:
            return indexed

        selection_html = self.fetch_action("/JRADB/accessD.html", "pw01dli00/F3")
        course_cname = self._find_course_cname(selection_html, race_date, racecourse, action_path="/JRADB/accessD.html", cname_marker="drl")
        if not course_cname:
            raise ProviderUnavailableError("JRA official race-card selection did not list the requested date/course.")
        race_list_html = self.fetch_action("/JRADB/accessD.html", course_cname)
        race_url = self._find_race_href(race_list_html, race_number, "/JRADB/accessD.html")
        if not race_url:
            raise ProviderUnavailableError("JRA official race-card selection did not list the requested race number.")
        return urllib.parse.urljoin("https://www.jra.go.jp", race_url)

    def resolve_race_result_url(self, race_date: str, racecourse: str, race_number: int) -> str:
        selection_html = self.fetch_action("/JRADB/accessS.html", "pw01sli00/AF")
        course_cname = self._find_course_cname(selection_html, race_date, racecourse, action_path="/JRADB/accessS.html", cname_marker="srl")
        if not course_cname:
            raise ProviderUnavailableError("JRA official result selection did not list the requested date/course.")
        race_list_html = self.fetch_action("/JRADB/accessS.html", course_cname)
        race_url = self._find_race_href(race_list_html, race_number, "/JRADB/accessS.html")
        if not race_url:
            raise ProviderUnavailableError("JRA official result selection did not list the requested race number.")
        return urllib.parse.urljoin("https://www.jra.go.jp", race_url)

    def resolve_baba_url(self, racecourse: str) -> str:
        base_url = "https://www.jra.go.jp/keiba/baba/"
        html = self.fetch_url(base_url)
        href = self._find_baba_href(html, racecourse)
        if not href:
            raise ProviderUnavailableError("JRA official baba page did not list the requested racecourse.")
        return urllib.parse.urljoin(base_url, href)

    def fetch_baba_condition(self, racecourse: str) -> dict:
        url = self.resolve_baba_url(racecourse)
        html = self.fetch_url(url)
        condition = self._parse_baba_condition(html)
        if not condition.get("turf") and not condition.get("dirt"):
            raise ProviderUnavailableError("JRA official baba condition could not be parsed.")
        condition["url"] = url
        return condition

    def fetch_action(self, path: str, cname: str, force_refresh: bool = False) -> str:
        if not path.startswith("/JRADB/"):
            raise ValueError("JRA action fetches are restricted to JRADB paths.")
        url = urllib.parse.urljoin("https://www.jra.go.jp", path)
        cache_id = f"POST {url} cname={cname}"
        cached = self._read_cached(cache_id, force_refresh)
        if cached is not None:
            return cached
        raw, status = self._network_request(url, data=urllib.parse.urlencode({"cname": cname}).encode("ascii"))
        return self._write_cached(cache_id, raw, status, url, {"method": "POST", "cname": cname})

    def fetch_url(self, url: str, force_refresh: bool = False) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.netloc not in self.allowed_hosts:
            raise ValueError("JRA provider fetches are restricted to approved HTTPS JRA official hosts.")

        cached = self._read_cached(url, force_refresh)
        if cached is not None:
            return cached

        raw, status = self._network_request(url)
        return self._write_cached(url, raw, status, url, {"method": "GET"})

    def _apply_baba_condition(self, race: RaceData) -> None:
        racecourse = race.racecourse.value
        if not racecourse:
            return
        try:
            condition = self.fetch_baba_condition(str(racecourse))
        except (ProviderUnavailableError, ValueError):
            self._last_baba_condition = {}
            return

        self._last_baba_condition = condition
        surface = str(race.surface.value or "")
        track_condition = condition.get("dirt") if "ダ" in surface else condition.get("turf")
        if track_condition and race.track_condition.value in (None, ""):
            race.track_condition = field(track_condition, 0.75, "jra_baba")
        if condition.get("weather") and race.weather.value in (None, ""):
            race.weather = field(condition["weather"], 0.75, "jra_baba")

    def _read_cached(self, cache_id: str, force_refresh: bool) -> str | None:
        cache_key = hashlib.sha256(cache_id.encode("utf-8")).hexdigest()
        body_path = self.cache_dir / f"{cache_key}.html"
        cache_is_fresh = (
            self.cache_ttl_seconds is None
            or time.time() - body_path.stat().st_mtime <= self.cache_ttl_seconds
        ) if body_path.exists() else False
        if body_path.exists() and not force_refresh and cache_is_fresh:
            self._log_fetch(cache_id, "cache", 200, body_path.read_bytes())
            return body_path.read_text(encoding="utf-8")
        return None

    def _network_request(self, url: str, data: bytes | None = None) -> tuple[bytes, int]:
        elapsed = time.monotonic() - self._last_fetch_at
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)

        req = urllib.request.Request(url, headers={"User-Agent": "HorsePrediction-MVP/0.1 personal-use"})
        if data is not None:
            req.data = data
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                status = response.status
                raw = response.read(3_000_000)
        except urllib.error.URLError as exc:
            self._append_log({"url": url, "status": "error", "error": str(exc), "fetched_at": time.time()})
            raise ProviderUnavailableError("JRA official information could not be fetched.") from exc
        finally:
            self._last_fetch_at = time.monotonic()
        return raw, status

    def _write_cached(self, cache_id: str, raw: bytes, status: int, url: str, extra: dict) -> str:
        cache_key = hashlib.sha256(cache_id.encode("utf-8")).hexdigest()
        body_path = self.cache_dir / f"{cache_key}.html"
        meta_path = self.cache_dir / f"{cache_key}.json"
        digest = hashlib.sha256(raw).hexdigest()
        text = self._decode_jra_html(raw)
        body_path.write_bytes(raw)
        meta_path.write_text(
            json.dumps(
                {"url": url, "http_status": status, "content_hash": digest, "fetched_at": time.time(), **extra},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._log_fetch(url, "network", status, raw)
        body_path.write_text(text, encoding="utf-8")
        return text

    def _decode_jra_html(self, raw: bytes) -> str:
        for encoding in ("utf-8", "cp932", "shift_jis", "euc_jp"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _resolve_from_url_index(self, race_date: str, racecourse: str, race_number: int) -> str | None:
        index_path = Path(os.getenv("JRA_RACE_URL_INDEX", self.cache_dir / "race_url_index.json"))
        if not index_path.exists():
            return None
        data = json.loads(index_path.read_text(encoding="utf-8"))
        key = self._race_lookup_key(race_date, racecourse, race_number)
        value = data.get(key)
        return str(value) if value else None

    def _find_course_cname(
        self,
        html: str,
        race_date: str,
        racecourse: str,
        action_path: str = "/JRADB/accessD.html",
        cname_marker: str = "drl",
    ) -> str | None:
        date_compact = race_date.replace("-", "")
        for path, cname, label in self._iter_do_actions(html):
            if path != action_path:
                continue
            if date_compact in cname and racecourse in label and cname_marker in cname:
                return cname
        return None

    def _parse_course_options(self, html: str, race_date: str) -> list[tuple[str, str]]:
        date_compact = race_date.replace("-", "")
        racecourses = ("札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉")
        options: list[tuple[str, str]] = []
        for path, cname, label in self._iter_do_actions(html):
            if path != "/JRADB/accessD.html" or "drl" not in cname or date_compact not in cname:
                continue
            racecourse = next((name for name in racecourses if name in label), None)
            if racecourse and (racecourse, cname) not in options:
                options.append((racecourse, cname))
        return options

    def _parse_race_schedule(self, html: str, race_date: str, racecourse: str) -> list[RaceScheduleItem]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        items: list[RaceScheduleItem] = []
        seen: set[int] = set()
        for row in soup.select("tr"):
            link = row.select_one("a[href*='/JRADB/accessD.html?CNAME=']")
            if link is None:
                continue
            label = " ".join([link.get_text(" ", strip=True), *(img.get("alt", "") for img in link.select("img"))])
            number_match = re.search(r"(\d{1,2})レース", label)
            time_node = row.select_one("td.time, .time")
            time_match = re.search(r"(\d{1,2})時(\d{2})分", time_node.get_text(" ", strip=True) if time_node else "")
            if not number_match or not time_match:
                continue
            race_number = int(number_match.group(1))
            if race_number in seen:
                continue
            seen.add(race_number)
            href = urllib.parse.urljoin("https://www.jra.go.jp", link.get("href") or "")
            race_type = "obstacle" if "障害" in row.get_text(" ", strip=True) else "flat"
            items.append(
                RaceScheduleItem(
                    race_date=race_date,
                    racecourse=racecourse,
                    race_number=race_number,
                    start_time=f"{int(time_match.group(1)):02d}:{time_match.group(2)}",
                    race_type=race_type,
                    source_url=href,
                )
            )
        return items

    def _find_race_card_href(self, html: str, race_number: int) -> str | None:
        return self._find_race_href(html, race_number, "/JRADB/accessD.html")

    def _find_baba_href(self, html: str, racecourse: str) -> str | None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for link in soup.select(".kaisai_tab a"):
            label = link.get_text(" ", strip=True)
            if racecourse in label and "競馬場" in label:
                return link.get("href")
        title = self._clean_text(soup.select_one("title").get_text(" ", strip=True) if soup.select_one("title") else None)
        if title and racecourse in title:
            return "index.html"
        return None

    def _find_race_href(self, html: str, race_number: int, path: str) -> str | None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for link in soup.select(f"a[href*='{path}?CNAME=']"):
            href = link.get("href") or ""
            alt = " ".join(img.get("alt", "") for img in link.select("img"))
            text = link.get_text(" ", strip=True)
            if f"{race_number}レース" in f"{alt} {text}":
                return href
        escaped_path = re.escape(path)
        pattern = re.compile(rf'href="([^"]*{escaped_path}\?CNAME=[^"]*{race_number:02d}\d{{8}}/[^"]+)"')
        match = pattern.search(html)
        return match.group(1) if match else None

    def _iter_do_actions(self, html: str) -> list[tuple[str, str, str]]:
        pattern = re.compile(r"doAction\('([^']+)'\s*,\s*'([^']+)'\).*?</a>", re.DOTALL)
        actions = []
        for match in pattern.finditer(html):
            label = re.sub(r"<[^>]+>", "", match.group(0))
            label = re.sub(r"\s+", " ", label).strip()
            actions.append((match.group(1), match.group(2), label))
        return actions

    def _parse_baba_condition(self, html: str) -> dict:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        root = soup.select_one("#course_condition") or soup
        weather_text = self._clean_text(root.select_one(".cell.weather .txt strong").get_text(" ", strip=True) if root.select_one(".cell.weather .txt strong") else None)
        turf_text = self._clean_text(root.select_one(".cell.turf .content p").get_text(" ", strip=True) if root.select_one(".cell.turf .content p") else None)
        dirt_text = self._clean_text(root.select_one(".cell.dirt .content p").get_text(" ", strip=True) if root.select_one(".cell.dirt .content p") else None)
        updated_text = self._clean_text(root.select_one("h3 .time").get_text(" ", strip=True) if root.select_one("h3 .time") else None)
        meeting_text = self._clean_text(soup.select_one("#condition h2").get_text(" ", strip=True) if soup.select_one("#condition h2") else None)
        return {
            "weather": weather_text,
            "turf": turf_text,
            "dirt": dirt_text,
            "updated_at_label": updated_text,
            "meeting_label": meeting_text,
            "source": "jra_baba",
        }

    def _clean_text(self, value: object) -> str | None:
        if value is None:
            return None
        text = re.sub(r"\s+", " ", str(value)).strip()
        return text or None

    def _race_lookup_key(self, race_date: str, racecourse: str, race_number: int) -> str:
        return f"{race_date}|{racecourse}|{int(race_number)}"

    def _parse_race_id(self, race_id: str) -> tuple[str, str, int]:
        match = re.match(r"^(\d{4})(\d{2})(\d{2})-(.+)-(\d{1,2})$", race_id)
        if not match:
            raise ProviderUnavailableError("JRA result lookup needs a provider race_id in YYYYMMDD-競馬場-R format.")
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}", match.group(4), int(match.group(5))

    def _log_fetch(self, url: str, source: str, http_status: int, body: bytes) -> None:
        self._append_log(
            {
                "url": url,
                "source": source,
                "http_status": http_status,
                "content_hash": hashlib.sha256(body).hexdigest(),
                "fetched_at": time.time(),
            }
        )

    def _append_log(self, row: dict) -> None:
        with (self.cache_dir / "fetch_log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
