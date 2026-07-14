#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TVBox URL Checker Pro v4.9
完美無重複版 (徹底根治過濾後殘留重複行問題)

更新重點
-------------------------
1. 【精準整行去重】優化去重邏輯：只要某一行的網址在之前已經出現過，這整行直接判定為「重複行」並全行剔除，絕對不會在輸出檔案中留下任何帶有殘留中文的重複行。
2. 延續 v4.8 的智慧強放機制：`gh-proxy`、`githubusercontent`、`gitlab.com`、`iptv365.org` 等知名源與代理直通放行，防誤殺、防重複轉碼。
"""

from __future__ import annotations
import json
import re
import shutil
import socket
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Set, Dict
from dataclasses import dataclass, field
import requests
import yaml
import urllib3
from urllib.parse import urlparse, quote, urlunparse

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================================
# 設定載入
# ============================================================================

try:
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
except Exception:
    cfg = {}

INPUT_FILE = cfg.get("input", "data/source.txt")
OUTPUT_FILE = cfg.get("output", "data/source_clean.txt")
INVALID_FILE = cfg.get("invalid", "data/invalid_urls.txt")
DUPLICATE_FILE = cfg.get("duplicate", "data/duplicate_urls.txt")
REPORT_FILE = cfg.get("report", "data/report.md")
MAX_WORKERS = cfg.get("workers", 50)
TIMEOUT = cfg.get("timeout", 12)
RETRY = cfg.get("retry", 3)
BACKUP_ENABLED = cfg.get("backup", True)
HISTORY_DIR = cfg.get("history", "data/history")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive"
}

# ============================================================================
# 常數與資料結構
# ============================================================================

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
SAFE_EXT_PATTERN = re.compile(r"\.(json|md5|js)(?:\?|$)", re.IGNORECASE)

FORCE_VALID_DOMAINS = ["iptv365.org"]
TRUSTED_PLATFORMS = ["gh-proxy", "githubusercontent", "gitlab.com"]

SHORT_URL_DOMAINS = {
    "t.cn", "url.cn", "suo.yt", "suo.im", "dwz.cn", "bit.ly", "tinyurl.com", 
    "git.io", "cutt.ly", "shorturl.at", "rebrand.ly", "t.ly", "is.gd"
}

PROXY_KEYWORDS = ["scrapeops", "scraperapi", "proxy", "agent", "api?url=", "?url=", "&url="]
INVALID_KEYWORDS = ["404 not found", "access denied", "502 bad gateway", "503 service unavailable"]

@dataclass
class CheckResult:
    url: str
    is_valid: bool
    error_message: Optional[str] = None

@dataclass
class LineResult:
    original_line: str
    cleaned_line: str
    is_duplicate_line: bool = False  # 標記這行是否因為網址重複而需要被整行捨棄
    urls: List[str] = field(default_factory=list)
    valid_urls: List[str] = field(default_factory=list)
    invalid_urls: List[str] = field(default_factory=list)
    duplicate_urls: List[str] = field(default_factory=list)

# ============================================================================
# URL 檢查器主類別
# ============================================================================

class URLChecker:
    def __init__(self):
        self.total = 0
        self.valid = 0
        self.invalid = 0
        self.duplicate = 0
        self.empty_lines = 0
        self.no_url_lines = 0
        
        self.seen_urls: Set[str] = set()
        self.invalid_urls: List[str] = []
        self.duplicate_urls: List[str] = []
        self.url_status: Dict[str, bool] = {}
        
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=MAX_WORKERS,
            pool_maxsize=MAX_WORKERS,
            max_retries=RETRY
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

    def clean_url(self, url: str) -> str:
        try:
            if "%" in url:
                return url.strip()
            parsed = urlparse(url.strip())
            safe_path = quote(parsed.path, safe='/')
            safe_query = quote(parsed.query, safe='=&?/')
            sanitized_url = urlunparse((
                parsed.scheme,
                parsed.netloc,
                safe_path,
                parsed.params,
                safe_query,
                parsed.fragment
            ))
            return sanitized_url
        except Exception:
            return url

    def load(self) -> List[str]:
        p = Path(INPUT_FILE)
        if not p.exists():
            raise FileNotFoundError(f"輸入檔案不存在: {INPUT_FILE}")
        return p.read_text(encoding="utf-8", errors="ignore").splitlines()

    def save(self, lines: List[str]) -> None:
        output_path = Path(OUTPUT_FILE)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        if BACKUP_ENABLED and output_path.exists():
            self._backup_file(output_path)
        
        filtered_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                self.empty_lines += 1
                continue
            if not URL_PATTERN.search(line):
                self.no_url_lines += 1
                continue
            filtered_lines.append(line)
        
        output_path.write_text("\n".join(filtered_lines), encoding="utf-8")
        print(f"\n📊 過濾統計：")
        print(f"  - 移除空白行：{self.empty_lines} 行")
        print(f"  - 移除無網址/重複行：{self.no_url_lines} 行")
        print(f"  - 保留有效行：{len(filtered_lines)} 行")
        print(f"  - 輸出檔案：{OUTPUT_FILE}")

    def _backup_file(self, file_path: Path) -> None:
        history_path = Path(HISTORY_DIR)
        history_path.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(file_path, history_path / f"backup_{ts}.txt")

    def save_invalid(self) -> None:
        if self.invalid_urls: 
            Path(INVALID_FILE).write_text("\n".join(self.invalid_urls), encoding="utf-8")

    def save_duplicate(self) -> None:
        if self.duplicate_urls: 
            Path(DUPLICATE_FILE).write_text("\n".join(self.duplicate_urls), encoding="utf-8")

    def extract_urls(self, line: str) -> List[str]:
        return URL_PATTERN.findall(line)

    def is_short_or_proxy_url(self, url: str) -> bool:
        url_lower = url.lower()
        if any(kw in url_lower for kw in PROXY_KEYWORDS):
            return True
        try:
            domain = urlparse(url).netloc.lower().split(':')[0]
            if domain in SHORT_URL_DOMAINS:
                return True
        except Exception:
            pass
        return False

    def is_safe_ext_url(self, url: str) -> bool:
        return bool(SAFE_EXT_PATTERN.search(url))

    def is_smart_force_valid(self, url: str) -> bool:
        url_lower = url.lower()
        if any(domain in url_lower for domain in FORCE_VALID_DOMAINS):
            return True
        if any(pf in url_lower for pf in TRUSTED_PLATFORMS) and self.is_safe_ext_url(url_lower):
            return True
        return False

    def process_url(self, url: str) -> Optional[CheckResult]:
        self.total += 1
        # 這裡不直接在多執行緒裡進行全局 seen_urls 判斷，改在 check_all 主循環中按順序去重，確保精準度
        safe_url = self.clean_url(url)
        
        if self.is_smart_force_valid(url):
            return CheckResult(url=url, is_valid=True)
            
        is_valid = self.url_status.get(safe_url) if safe_url in self.url_status else self.check_url(safe_url)
        self.url_status[safe_url] = is_valid
        
        return CheckResult(url=url, is_valid=is_valid)

    def check_all(self) -> None:
        lines = self.load()
        line_results: List[LineResult] = []
        all_tasks = []
        
        print(f"🔍 開始檢查網址有效性 (執行緒數: {MAX_WORKERS})...")
        
        # 第一階段：分發網路檢查任務
        for line in lines:
            urls = self.extract_urls(line)
            if not urls:
                line_results.append(LineResult(original_line=line, cleaned_line=line, urls=[]))
                continue
            
            line_result = LineResult(original_line=line, cleaned_line=line, urls=urls)
            for url in urls:
                future = self.executor.submit(self.process_url, url)
                all_tasks.append((future, url, line_result))
            line_results.append(line_result)

        # 第二階段：等待執行緒響應並建立臨時狀態字典
        task_outputs = {}
        for future, url, line_result in all_tasks:
            try:
                result = future.result(timeout=TIMEOUT + 5)
                task_outputs[url] = result.is_valid if result else False
            except Exception:
                task_outputs[url] = False
        
        self.executor.shutdown(wait=True)

    # ========================================================================
    # 【核心重構】第三階段：按原始順序「嚴格整行去重與清洗」
    # ========================================================================
        cleaned_lines = []
        for result in line_results:
            if not result.urls:
                # 沒網址的行，直接保留（如果是空行會在 save() 被過濾）
                cleaned_lines.append(result.original_line)
                continue
            
            is_line_valid = True
            current_line_text = result.original_line
            
            for url in result.urls:
                # 1. 檢查是否為重複網址
                if url in self.seen_urls:
                    self.duplicate += 1
                    self.duplicate_urls.append(url)
                    result.is_duplicate_line = True
                    is_line_valid = False
                    break # 只要這行包含任何一個重複網址，整行直接作廢！
                
                # 2. 檢查網路狀態是否有效
                url_valid = task_outputs.get(url, False)
                if url_valid:
                    self.seen_urls.add(url)
                    self.valid += 1
                else:
                    self.invalid += 1
                    self.invalid_urls.append(url)
                    # 擦除無效網址
                    current_line_text = current_line_text.replace(url, "")
                    is_line_valid = False
            
            # 如果這行沒有因為「網址重複」被作廢，且擦除無效網址後仍有內容，才保留
            if not result.is_duplicate_line:
                cleaned_text = re.sub(r'\s+', ' ', current_line_text).strip()
                if cleaned_text:
                    cleaned_lines.append(cleaned_text)

        self.save(cleaned_lines)
        self.save_invalid()
        self.save_duplicate()
        self.generate_report()

    # ========================================================================
    # 網路連線與深度內容校驗
    # ========================================================================

    def check_url(self, url: str) -> bool:
        for attempt in range(RETRY):
            try:
                try:
                    head_response = self.session.head(url, timeout=TIMEOUT, allow_redirects=True, verify=False)
                    if head_response.status_code < 400:
                        if self.is_short_or_proxy_url(url) or self.is_safe_ext_url(url):
                            return True
                except Exception:
                    pass
                
                response = self.session.get(
                    url, timeout=TIMEOUT, allow_redirects=True, stream=True, verify=False
                )
                
                if response.status_code >= 400:
                    if attempt < RETRY - 1:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    return False
                
                if self.is_short_or_proxy_url(url) or self.is_safe_ext_url(url):
                    return True
                
                content = self._read_content(response)
                if self.validate_content(url, content):
                    return True
                
                if attempt < RETRY - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return False
            except (requests.exceptions.RequestException, socket.error):
                if attempt < RETRY - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return False
        return False

    def _read_content(self, response: requests.Response, max_size: int = 2048) -> str:
        content = ""
        try:
            for chunk in response.iter_content(chunk_size=512):
                if chunk:
                    try:
                        content += chunk.decode('utf-8', errors='ignore')
                        if len(content) >= max_size: break
                    except Exception:
                        pass
        except Exception:
            pass
        return content

    def validate_content(self, url: str, content: str) -> bool:
        if not content or len(content.strip()) < 5: return False
        url_lower = url.lower()
        if url_lower.endswith('.xml'): return self._validate_xml(content)
        elif url_lower.endswith(('.m3u', '.m3u8')): return self._validate_m3u(content)
        elif url_lower.endswith('.txt'): return self._validate_txt(content)
        return self._validate_common(content)

    def _validate_common(self, content: str) -> bool:
        content_lower = content.lower()
        for keyword in INVALID_KEYWORDS:
            if keyword in content_lower: return False
        if len(content.strip()) < 15: return False
        tvbox_indicators = ['url', 'name', 'title', 'channel', 'group', 'http', 'https', '://', 'm3u8', 'flv', 'spider']
        return sum(1 for ind in tvbox_indicators if ind in content_lower) >= 1

    def _validate_xml(self, content: str) -> bool:
        return any(i in content.lower() for i in ['<?xml', '<tv', '<rss', '<channel'])

    def _validate_m3u(self, content: str) -> bool:
        return '#EXTM3U' in content.upper() or 'HTTP' in content.upper()

    def _validate_txt(self, content: str) -> bool:
        if '<html' in content.lower(): return False
        return any(URL_PATTERN.search(line) for line in content.splitlines() if line.strip())

    def generate_report(self) -> None:
        lines = [
            "# 📊 TVBox URL 檢查報告", "", "## 📈 統計摘要", "",
            "| 項目 | 數量 | 比例 |", "|------|------|------|",
            f"| 總網址數 | {self.total} | 100% |",
            f"| ✅ 有效 | {self.valid} | {(self.valid/self.total*100):.1f}% |" if self.total > 0 else "| ✅ 有效 | 0 | 0% |",
            f"| ❌ 失效 | {self.invalid} | {(self.invalid/self.total*100):.1f}% |" if self.total > 0 else "| ❌ 失效 | 0 | 0% |",
            f"| 🔄 重複 | {self.duplicate} | {(self.duplicate/self.total*100):.1f}% |" if self.total > 0 else "| 🔄 重複 | 0 | 0% |",
            "", "## 🧹 清理統計", "",
            f"- **移除空白行**：{self.empty_lines} 行", f"- **移除無網址/重複行**：{self.no_url_lines} 行", "",
            f"## ✅ 有效網址 ({self.valid})", "", f"有效網址已儲存至：`{OUTPUT_FILE}`", ""
        ]
        lines.extend(["## ❌ 無效網址列表", ""])
        if self.invalid_urls:
            for url in self.invalid_urls[:30]: lines.append(f"- `{url}`")
            lines.append(f"完整清單請查看：`{INVALID_FILE}`")
        else:
            lines.append("✅ 沒有無效網址")
        lines.extend(["", "## 🔄 重複網址列表", ""])
        if self.duplicate_urls:
            for url in self.duplicate_urls[:30]: lines.append(f"- `{url}`")
            lines.append(f"完整清單請查看：`{DUPLICATE_FILE}`")
        else:
            lines.append("✅ 沒有重複網址")
        lines.extend(["", "---", f"🕐 更新時間：{time.strftime('%Y-%m-%d %H:%M:%S')}", "", "✅ 報告由 TVBox URL Checker Pro v4.9 自動生成"])
        Path(REPORT_FILE).write_text("\n".join(lines), encoding="utf-8")

if __name__ == "__main__":
    print("=" * 70)
    print("🚀 TVBox URL Checker Pro v4.9 (完美無重複版)")
    print("=" * 70)
    start_time = time.time()
    try:
        checker = URLChecker()
        checker.check_all()
        print("\n" + "=" * 70)
        print("✅ 檢查完成！")
        print("=" * 70)
        print(f"📊 總網址 : {checker.total} | ✅ 有效 : {checker.valid} | ❌ 失效 : {checker.invalid}")
        print(f"⏱️ 耗時   : {time.time() - start_time:.2f} 秒")
        print("=" * 70)
    except Exception as e:
        print(f"💥 程式執行失敗: {e}")
