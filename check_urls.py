#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TVBox URL Checker Pro v3
Part 1

功能
-------------------------
✓ 讀取 TXT
✓ 保留原格式
✓ 去除重覆網址
✓ 建立網址清單
✓ 多執行緒準備
"""

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import json
import re
import shutil
import socket
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import requests
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
INPUT_FILE = cfg["input"]
OUTPUT_FILE = cfg["output"]

INVALID_FILE = cfg["invalid"]
DUPLICATE_FILE = cfg["duplicate"]
REPORT_FILE = cfg["report"]

MAX_WORKERS = cfg["workers"]
TIMEOUT = cfg["timeout"]
RETRY = cfg["retry"]

input: data/source.txt
output: data/source_clean.txt

INVALID_FILE = "data/invalid_urls.txt"

DUPLICATE_FILE = "data/duplicate_urls.txt"

REPORT_FILE = "data/report.md"

MAX_WORKERS = 50

TIMEOUT = 8

RETRY = 3

USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64)"
)

###########################################################################

URL_PATTERN = re.compile(
    r"https?://[^\s]+"
)

###########################################################################

class URLChecker:

    def __init__(self):

        self.total = 0

        self.valid = 0

        self.invalid = 0

        self.duplicate = 0

        self.seen = set()

        self.invalid_urls = []

        self.duplicate_urls = []

        self.session = requests.Session()

        self.session.headers.update({
            "User-Agent": USER_AGENT
        })

    #######################################################################

    def load(self):

        p = Path(INPUT_FILE)

        if not p.exists():

            raise FileNotFoundError(INPUT_FILE)

        return p.read_text(
            encoding="utf-8",
            errors="ignore"
        ).splitlines()

    #######################################################################

    def save(self, lines):

    Path(OUTPUT_FILE).parent.mkdir(
        parents=True,
        exist_ok=True
    )

    if cfg["backup"]:

        history = Path(cfg["history"])

        history.mkdir(
            parents=True,
            exist_ok=True
        )

        ts = time.strftime("%Y%m%d-%H%M")

        if Path(OUTPUT_FILE).exists():

            shutil.copy2(

                OUTPUT_FILE,

                history / f"{ts}.txt"

            )

    Path(OUTPUT_FILE).write_text(

        "\n".join(lines),

        encoding="utf-8"

    )

        Path(OUTPUT_FILE).write_text(

            "\n".join(lines),

            encoding="utf-8"

        )

    #######################################################################

    def save_invalid(self):

        Path(INVALID_FILE).write_text(

            "\n".join(self.invalid_urls),

            encoding="utf-8"

        )

    #######################################################################

    def save_duplicate(self):

        Path(DUPLICATE_FILE).write_text(

            "\n".join(self.duplicate_urls),

            encoding="utf-8"

        )

    #######################################################################

    def extract(self, line):

        return URL_PATTERN.findall(line)

    #######################################################################

    def is_duplicate(self, url):

        if url in self.seen:

            self.duplicate += 1

            self.duplicate_urls.append(url)

            return True

        self.seen.add(url)

        return False

    #######################################################################

    def check_all(self):

        lines = self.load()

        cleaned = []

        tasks = []

        executor = ThreadPoolExecutor(

            max_workers=MAX_WORKERS

        )

        for line in lines:

            urls = self.extract(line)

            if not urls:

                cleaned.append(line)

                continue

            newline = line

            futures = []

            for url in urls:

                self.total += 1

                if self.is_duplicate(url):

                    newline = newline.replace(url, "")

                    continue

                future = executor.submit(

                    self.check_url,

                    url

                )

                futures.append((future, url))

            tasks.append(

                (newline, futures)

            )

        for newline, futures in tasks:

            for future, url in futures:

                ok = future.result()

                if ok:

                    self.valid += 1

                else:

                    self.invalid += 1

                    self.invalid_urls.append(url)

                    newline = newline.replace(url, "")

            cleaned.append(newline)

        executor.shutdown()

        self.save(cleaned)

        self.save_invalid()

        self.save_duplicate()

        self.report()

###########################################################################

    def check_url(self, url: str) -> bool:
        """
        檢查網址是否有效
        流程：
        1. HEAD
        2. HEAD 失敗改 GET
        3. 驗證 HTTP Status
        4. 驗證內容
        """

        for _ in range(RETRY):

            try:

                try:
                    r = self.session.head(
                        url,
                        timeout=TIMEOUT,
                        allow_redirects=True
                    )

                    if r.status_code >= 400:
                        raise Exception("HEAD failed")

                except Exception:

                    r = self.session.get(
                        url,
                        timeout=TIMEOUT,
                        allow_redirects=True,
                        stream=True
                    )

                if r.status_code >= 400:
                    continue

                text = ""

                try:
                    text = r.text[:2000]
                except Exception:
                    pass

                return self.validate(url, text)

            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.SSLError,
                requests.exceptions.RequestException,
                socket.gaierror,
            ):

                continue

        return False

###########################################################################

    def validate(self, url: str, text: str) -> bool:

        lower = url.lower()

        if lower.endswith(".json"):

            return self.validate_json(text)

        elif lower.endswith(".xml"):

            return self.validate_xml(text)

        elif lower.endswith(".m3u"):

            return self.validate_m3u(text)

        elif lower.endswith(".txt"):

            return self.validate_txt(text)

        else:

            return self.validate_common(text)

###########################################################################

    def validate_common(self, text: str) -> bool:

        bad = [

            "404",

            "not found",

            "access denied",

            "forbidden",

            "error",

            "502 bad gateway",

            "503 service",

            "nginx",

            "<html",

        ]

        t = text.lower()

        for word in bad:

            if word in t:

                return False

        return True

###########################################################################

    def validate_json(self, text: str) -> bool:

        if not text.strip():

            return False

        if "<html" in text.lower():

            return False

        try:

            json.loads(text)

            return True

        except Exception:

            return False

###########################################################################

    def validate_xml(self, text: str) -> bool:

        t = text.lower()

        if "<?xml" in t:

            return True

        if "<tv" in t:

            return True

        if "<rss" in t:

            return True

        return False

###########################################################################

    def validate_m3u(self, text: str) -> bool:

        if "#EXTM3U" in text.upper():

            return True

        return False

###########################################################################

    def validate_txt(self, text: str) -> bool:

        t = text.lower()

        bad = [

            "404",

            "forbidden",

            "access denied",

            "nginx",

            "<html",

            "error"

        ]

        for s in bad:

            if s in t:

                return False

        return True

###########################################################################

    def report(self):

        lines = [

            "# TVBox Weekly Report",

            "",

            f"總網址：{self.total}",

            f"有效：{self.valid}",

            f"失效：{self.invalid}",

            f"重複：{self.duplicate}",

            "",

            time.strftime(
                "更新時間：%Y-%m-%d %H:%M:%S"
            )

        ]

        Path(REPORT_FILE).write_text(

            "\n".join(lines),

            encoding="utf-8"

        )

###########################################################################

if __name__ == "__main__":

    start = time.time()

    checker = URLChecker()

    checker.check_all()

    print("-" * 60)
    print("TVBox Checker 完成")
    print(f"總網址 : {checker.total}")
    print(f"有效   : {checker.valid}")
    print(f"失效   : {checker.invalid}")
    print(f"重複   : {checker.duplicate}")
    print(f"耗時   : {time.time() - start:.2f} 秒")
