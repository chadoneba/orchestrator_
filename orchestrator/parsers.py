import re
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup


class MathNetParser:
    """Парсер страницы конференций mathnet.ru."""

    MONTHS_RU = {
        "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
        "мая": 5, "июня": 6, "июля": 7, "августа": 8,
        "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    }

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko)"
        )
    }

    ROW_RE = re.compile(r"^row1[0-9]{5}$")
    ROW2_RE = re.compile(r"^row2[0-9]{5}$")
    DONE_IMG_SRC = "/refsvg/pics/camera00_2.svg"

    def __init__(self, url: str, min_date: Optional[date] = None):
        self.url = url
        self.min_date = min_date

    def parse(self) -> list[dict]:
        resp = requests.get(self.url, headers=self.HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Основные строки с данными (row1)
        tr_elements = [
            tr for tr in soup.find_all("tr")
            if tr.has_attr("id") and self.ROW_RE.match(tr.get("id"))
        ]

        # row2 → номера записей с признаком готовой записи
        done_numbers = set()
        for tr in soup.find_all("tr"):
            if not (tr.has_attr("id") and self.ROW2_RE.match(tr.get("id"))):
                continue
            img = tr.find("img", src=lambda s: s and self.DONE_IMG_SRC in s)
            if img is not None:
                done_numbers.add(tr.get("id")[4:])

        parsed = []
        for tr in tr_elements:
            cols = tr.get_text(separator="|", strip=True).split("|")
            number = tr.get("id")[4:]     # "51397" — везде он

            try:
                rec = self._parse_row(cols, number, number in done_numbers)
                if rec is None:
                    continue
                parsed.append(rec)
            except (IndexError, KeyError, ValueError):
                continue

        return parsed

    def _parse_row(self, cols: list[str], number: str, is_done: bool) -> Optional[dict]:
        day_str, month_ru, year_str, _ = cols[-3].split(" ")
        dt = datetime(int(year_str), self.MONTHS_RU[month_ru], int(day_str))

        if self.min_date and dt.date() < self.min_date:
            return None

        status = "done" if is_done else "planned"

        title, author = self._try_get_title_list(" ".join(cols[1:-3]))

        return {
            "external_id": number,
            "number":      cols[1],
            "title":       title,
            "author":      author,
            "date":        dt.strftime("%Y-%m-%d"),
            "time_range":  cols[-2],
            "room":        cols[-1],
            "status":      status,
        }

    def _try_get_title_list(self, data:str) -> tuple[str,str]:
        RE_AUTHOR = r"[A-ZА-Я][a-z]?\.\s*(?:[A-ZА-Я][a-z]?\.\s*)?[A-Za-zА-Яа-яё]+"
        author_match = re.search(RE_AUTHOR,data)
        if author_match == None:
            return (data,"")
        else:
            return (data[:author_match.start()-1],data[author_match.start():])