#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TVBox URL Checker Pro v4.6
頂配穩定版 (完全解決 .js / .md5 / 中文與代理誤判)

更新重點
-------------------------
1. 【擴大字尾放行】將 `.js` 納入智慧放行清單：與 `.json`、`.md5` 享有同等免校驗待遇。完美解決 `index.js` 等純腳本因缺乏內文特徵指標被誤殺的問題。
2. 【完美支援 gh-proxy】優化代理判定：明確放行包含 `gh-proxy` 在內的加速節點，只要網路能正常通訊即直接視為有效。
3. 【全面抗阻斷】保留標準 URL 中文編碼 (完美相容 /騷零/ /白嫖/ /小米/ 等中文路徑)、偽裝瀏覽器 Header 與指數退避重試機制。
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

# 關閉 SSL 未驗證的警告提示
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
TIMEOUT = cfg.get("timeout", 12)  # 稍微拉長因應 gh-proxy 轉導延遲
RETRY = cfg.get("retry", 3)
BACKUP_ENABLED = cfg.get("backup", True)
HISTORY_DIR = cfg.get("history", "data/history")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive"
}

# ============================================================================
# 常數與資料結構
# ============================================================================

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")

# 智慧免內文校驗字尾 (只要連線成功即代表有效)：涵蓋 json、md5、js 腳本
SAFE_EXT_PATTERN = re.compile(r"\.(json|md5|js)(?:\?|$)", re.IGNORECASE)

SHORT_URL_DOMAINS = {
    "t.cn", "url.cn", "suo.yt", "suo.im", "dwz.cn", "bit.ly", "tinyurl.com", 
    "git.io", "cutt.ly", "shorturl.at", "rebrand.ly", "t.ly", "is.gd"
}

# 代理網址特徵 (擴大納入 gh-proxy)
PROXY_KEYWORDS = ["scrapeops", "scraperapi", "proxy", "agent", "api?url=", "?url=", "&url=", "gh-proxy"]
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
        """對網址進行規範化，對中文路徑安全轉碼"""
        try:
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
        print(f"  - 移除無網址行：{self.no_url_lines} 行")
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
        """智慧判定網址是否為免校驗的特殊字尾 (涵蓋 .json, .md5, .js)"""
        return bool(SAFE_EXT_PATTERN.search(url))

    def process_url(self, url: str) -> Optional[CheckResult]:
        self.total += 1
        if url in self.seen_urls:
            self.duplicate += 1
            self.duplicate_urls.append(url)
            return CheckResult(url=url, is_valid=False, error_message="重複 URL")
        
        self.seen_urls.add(url)
        safe_url = self.clean_url(url)
        
        is_valid = self.url_status.get(safe_url) if safe_url in self.url_status else self.check_url(safe_url)
        self.url_status[safe_url] = is_valid
        
        if is_valid:
            self.valid += 1
            return CheckResult(url=url, is_valid=True)
        else:
            self.invalid += 1
            self.invalid_urls.append(url)
            return CheckResult(url=url, is_valid=False, error_message="連線失敗或內容無效")

    def check_all(self) -> None:
        lines = self.load()
        line_results: List[LineResult] = []
        all_tasks = []
        
        print(f"🔍 開始檢查網址有效性 (執行緒數: {MAX_WORKERS})...")
        
        for line_num, line in enumerate(lines, 1):
            urls = self.extract_urls(line)
            if not urls:
                line_results.append(LineResult(original_line=line, cleaned_line=line, urls=[]))
                continue
            
            line_result = LineResult(original_line=line, cleaned_line=line, urls=urls)
            for url in urls:
                future = self.executor.submit(self.process_url, url)
                all_tasks.append((future, url, line_result))
            
            line_results.append(line_result)
            if line_num % 50 == 0:
                print(f"  進度: {line_num}/{len(lines)} 行")
        
        print(f"  ⏳ 等待所有連線響應...")
        processed_urls: Set[str] = set()
        
        for future, url, line_result in all_tasks:
            try:
                result = future.result(timeout=TIMEOUT + 5)
                if result and url not in processed_urls:
                    processed_urls.add(url)
                    if result.is_valid:
                        line_result.valid_urls.append(url)
                    else:
                        line_result.invalid_urls.append(url)
                        line_result.cleaned_line = line_result.cleaned_line.replace(url, "")
            except Exception as e:
                self.invalid += 1
                self.invalid_urls.append(url)
                line_result.invalid_urls.append(url)
                line_result.cleaned_line = line_result.cleaned_line.replace(url, "")
                print(f"  ⚠️ 檢查 URL 失敗: {url[:50]}... - {str(e)}")
        
        print(f"  ✅ 完成所有網路檢查")
        self.executor.shutdown(wait=True)
        
        cleaned_lines = []
        for result in line_results:
            cleaned = re.sub(r'\s+', ' ', result.cleaned_line).strip()
            if cleaned:
                cleaned_lines.append(cleaned)
        
        self.save(cleaned_lines)
        self.save_invalid()
        self.save_duplicate()
        self.generate_report()

    # ========================================================================
    # 網路連線與深度內容校驗
    # ========================================================================

    def check_url(self, url: str) -> bool:
        """核心連線校驗 - 升級抗封鎖與 gh-proxy / .js 相容性"""
        for attempt in range(RETRY):
            try:
                # 1. HEAD 預檢
                try:
                    head_response = self.session.head(url, timeout=TIMEOUT, allow_redirects=True, verify=False)
                    if head_response.status_code < 400:
                        if self.is_short_or_proxy_url(url) or self.is_safe_ext_url(url):
                            return True
                except Exception:
                    pass
                
                # 2. GET 串流請求
                response = self.session.get(
                    url, timeout=TIMEOUT, allow_redirects=True, stream=True, verify=False
                )
                
                if response.status_code >= 400:
                    if attempt < RETRY - 1:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    return False
                
                # 如果是短網址、代理網址、或者免校驗字尾 (.json, .md5, .js)，有成功響應直接放行！
                if self.is_short_or_proxy_url(url) or self.is_safe_ext_url(url):
                    return True
                
                # 3. 其他常規網址（如 .txt, .m3u8 等）才讀取前 2KB 進行特徵比對
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

    # ========================================================================
    # 報告生成與主程序
    # ========================================================================

    def generate_report(self) -> None:
        lines = [
            "# 📊 TVBox URL 檢查報告", "", "## 📈 統計摘要", "",
            "| 項目 | 數量 | 比例 |", "|------|------|------|",
            f"| 總網址數 | {self.total} | 100% |",
            f"| ✅ 有效 | {self.valid} | {(self.valid/self.total*100):.1f}% |" if self.total > 0 else "| ✅ 有效 | 0 | 0% |",
            f"| ❌ 失效 | {self.invalid} | {(self.invalid/self.total*100):.1f}% |" if self.total > 0 else "| ❌ 失效 | 0 | 0% |",
            f"| 🔄 重複 | {self.duplicate} | {(self.duplicate/self.total*100):.1f}% |" if self.total > 0 else "| 🔄 重複 | 0 | 0% |",
            "", "## 🧹 清理統計", "",
            f"- **移除空白行**：{self.empty_lines} 行", f"- **移除無網址行**：{self.no_url_lines} 行", "",
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
        lines.extend(["", "---", f"🕐 更新時間：{time.strftime('%Y-%m-%d %H:%M:%S')}", "", "✅ 報告由 TVBox URL Checker Pro v4.6 自動生成"])
        Path(REPORT_FILE).write_text("\n".join(lines), encoding="utf-8")

if __name__ == "__main__":
    print("=" * 70)
    print("🚀 TVBox URL Checker Pro v4.6 (頂配穩定版)")
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
