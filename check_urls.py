#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TVBox URL Checker Pro v3 - 改善版
強化的網址有效性偵測
"""

from __future__ import annotations
import json
import re
import socket
import time
import hashlib
from typing import Optional, Tuple, Dict, Any
from urllib.parse import urlparse, urljoin
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import yaml

# ============================================================================
# 設定載入
# ============================================================================

with open("config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

INPUT_FILE = cfg.get("input", "data/source.txt")
OUTPUT_FILE = cfg.get("output", "data/source_clean.txt")
INVALID_FILE = cfg.get("invalid", "data/invalid_urls.txt")
DUPLICATE_FILE = cfg.get("duplicate", "data/duplicate_urls.txt")
REPORT_FILE = cfg.get("report", "data/report.md")
MAX_WORKERS = cfg.get("workers", 30)
TIMEOUT = cfg.get("timeout", 10)
RETRY = cfg.get("retry", 3)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/119.0.0.0 Safari/537.36"
)

# ============================================================================
# URL 模式
# ============================================================================

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")

# ============================================================================
# 網址有效性檢查器
# ============================================================================

class URLValidityChecker:
    """進階網址有效性檢查器"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        
        # 設定重試策略
        retry_strategy = requests.adapters.Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504]
        )
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=MAX_WORKERS,
            pool_maxsize=MAX_WORKERS,
            max_retries=retry_strategy
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        
        # 快取機制
        self.cache = {}
        self.cache_ttl = 3600  # 1小時
        
        # 統計資料
        self.stats = {
            "total": 0,
            "valid": 0,
            "invalid": 0,
            "redirect": 0,
            "timeout": 0,
            "connection_error": 0,
            "ssl_error": 0,
            "content_error": 0
        }

    # ========================================================================
    # 多層級檢查策略
    # ========================================================================

    def check_url(self, url: str) -> Tuple[bool, Dict[str, Any]]:
        """
        多層級檢查網址有效性
        返回: (是否有效, 詳細資訊)
        """
        # 1. 基本驗證
        if not self._basic_validation(url):
            return False, {"error": "基本驗證失敗"}
        
        # 2. DNS 解析檢查
        if not self._dns_check(url):
            return False, {"error": "DNS 解析失敗"}
        
        # 3. 網路連線檢查（多種策略）
        result = self._network_check(url)
        if not result["success"]:
            return False, result
        
        # 4. 內容驗證
        content_result = self._content_validation(url, result)
        if not content_result["success"]:
            return False, content_result
        
        return True, {
            "status_code": result.get("status_code"),
            "content_type": result.get("content_type"),
            "response_time": result.get("response_time"),
            "content_length": result.get("content_length"),
            "redirects": result.get("redirect_count", 0)
        }

    # ========================================================================
    # 1. 基本驗證
    # ========================================================================

    def _basic_validation(self, url: str) -> bool:
        """基本 URL 格式驗證"""
        try:
            parsed = urlparse(url)
            
            # 檢查協議
            if parsed.scheme not in ['http', 'https']:
                return False
            
            # 檢查域名
            if not parsed.netloc:
                return False
            
            # 檢查域名格式
            domain = parsed.netloc.lower()
            # 移除端口號
            if ':' in domain:
                domain = domain.split(':')[0]
            
            # 檢查是否為 IP 或有效域名
            if not (self._is_valid_ip(domain) or self._is_valid_domain(domain)):
                return False
            
            # 檢查 URL 長度
            if len(url) > 2048:
                return False
            
            return True
            
        except Exception:
            return False

    def _is_valid_ip(self, domain: str) -> bool:
        """檢查是否為有效 IP"""
        # IPv4
        ipv4_pattern = re.compile(
            r'^(\d{1,3}\.){3}\d{1,3}$'
        )
        if ipv4_pattern.match(domain):
            parts = domain.split('.')
            return all(0 <= int(p) <= 255 for p in parts)
        return False

    def _is_valid_domain(self, domain: str) -> bool:
        """檢查是否為有效域名"""
        domain_pattern = re.compile(
            r'^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?'
            r'(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$'
        )
        return bool(domain_pattern.match(domain))

    # ========================================================================
    # 2. DNS 解析檢查
    # ========================================================================

    def _dns_check(self, url: str) -> bool:
        """DNS 解析檢查（快取）"""
        domain = urlparse(url).netloc
        if ':' in domain:
            domain = domain.split(':')[0]
        
        # 檢查快取
        cache_key = f"dns_{domain}"
        if cache_key in self.cache:
            cached_time, result = self.cache[cache_key]
            if time.time() - cached_time < self.cache_ttl:
                return result
        
        try:
            # 嘗試 DNS 解析
            socket.setdefaulttimeout(3)
            socket.gethostbyname(domain)
            result = True
        except (socket.gaierror, socket.timeout):
            result = False
        
        # 更新快取
        self.cache[cache_key] = (time.time(), result)
        return result

    # ========================================================================
    # 3. 網路連線檢查（智慧策略）
    # ========================================================================

    def _network_check(self, url: str) -> Dict[str, Any]:
        """
        網路連線檢查
        策略：
        1. 先 HEAD（快速）
        2. HEAD 失敗改 GET（完整）
        3. 支援重定向追蹤
        """
        result = {
            "success": False,
            "status_code": None,
            "content_type": None,
            "response_time": None,
            "content_length": None,
            "redirect_count": 0,
            "error": None
        }
        
        for attempt in range(RETRY):
            try:
                start_time = time.time()
                
                # 策略 1: 先嘗試 HEAD
                try:
                    response = self.session.head(
                        url,
                        timeout=TIMEOUT,
                        allow_redirects=True,
                        headers={
                            "Accept": "*/*",
                            "Accept-Encoding": "gzip, deflate",
                            "Connection": "keep-alive"
                        }
                    )
                    head_success = response.status_code < 400
                except Exception:
                    head_success = False
                
                # 策略 2: HEAD 失敗或沒有 Content-Type 時使用 GET
                if not head_success or not response.headers.get('content-type'):
                    response = self.session.get(
                        url,
                        timeout=TIMEOUT,
                        allow_redirects=True,
                        stream=True,
                        headers={
                            "Accept": "*/*",
                            "Accept-Encoding": "gzip, deflate",
                            "Connection": "keep-alive"
                        }
                    )
                
                response_time = time.time() - start_time
                
                # 檢查狀態碼
                if response.status_code >= 400:
                    result["error"] = f"HTTP {response.status_code}"
                    continue
                
                # 提取資訊
                result["success"] = True
                result["status_code"] = response.status_code
                result["content_type"] = response.headers.get('content-type', '')
                result["response_time"] = round(response_time, 3)
                result["redirect_count"] = len(response.history)
                
                # 檢查 Content-Length
                content_length = response.headers.get('content-length')
                if content_length:
                    result["content_length"] = int(content_length)
                    # 如果內容為空，視為無效
                    if result["content_length"] == 0:
                        result["success"] = False
                        result["error"] = "內容為空"
                
                # 更新統計
                if result["redirect_count"] > 0:
                    self.stats["redirect"] += 1
                
                return result
                
            except requests.exceptions.Timeout:
                self.stats["timeout"] += 1
                if attempt < RETRY - 1:
                    time.sleep(0.5)
                continue
                
            except requests.exceptions.ConnectionError:
                self.stats["connection_error"] += 1
                if attempt < RETRY - 1:
                    time.sleep(0.5)
                continue
                
            except requests.exceptions.SSLError:
                self.stats["ssl_error"] += 1
                # SSL 錯誤可能只是憑證問題，嘗試忽略
                try:
                    response = self.session.get(
                        url,
                        timeout=TIMEOUT,
                        verify=False,
                        allow_redirects=True,
                        stream=True
                    )
                    if response.status_code < 400:
                        result["success"] = True
                        result["status_code"] = response.status_code
                        result["error"] = "SSL 驗證失敗但內容可存取"
                        return result
                except Exception:
                    pass
                continue
                
            except Exception as e:
                result["error"] = str(e)
                continue
        
        return result

    # ========================================================================
    # 4. 內容驗證（智能分析）
    # ========================================================================

    def _content_validation(self, url: str, network_result: Dict) -> Dict[str, Any]:
        """
        智慧內容驗證
        根據檔案類型採用不同策略
        """
        result = {
            "success": False,
            "error": None,
            "content_type": network_result.get("content_type", ""),
            "file_type": None
        }
        
        # 獲取內容樣本
        content = self._fetch_content_sample(url)
        if content is None:
            result["error"] = "無法獲取內容"
            return result
        
        # 判斷檔案類型
        file_type = self._detect_file_type(url, result["content_type"], content)
        result["file_type"] = file_type
        
        # 根據類型驗證
        validators = {
            "json": self._validate_json,
            "xml": self._validate_xml,
            "m3u": self._validate_m3u,
            "txt": self._validate_txt,
            "html": self._validate_html,
            "image": self._validate_image,
            "unknown": self._validate_common
        }
        
        validator = validators.get(file_type, self._validate_common)
        is_valid, error_msg = validator(content)
        
        if is_valid:
            result["success"] = True
        else:
            result["error"] = error_msg
            self.stats["content_error"] += 1
        
        return result

    def _fetch_content_sample(self, url: str) -> Optional[str]:
        """獲取內容樣本（只讀取必要部分）"""
        try:
            response = self.session.get(
                url,
                timeout=TIMEOUT,
                stream=True,
                headers={
                    "Range": "bytes=0-8191"  # 只讀取前 8KB
                }
            )
            
            if response.status_code >= 400:
                return None
            
            # 嘗試解碼
            content = b''
            for chunk in response.iter_content(chunk_size=1024):
                content += chunk
                if len(content) >= 8192:
                    break
            
            # 嘗試解碼
            try:
                return content.decode('utf-8', errors='ignore')
            except Exception:
                # 可能是二進位檔案
                return content.hex()[:500]
                
        except Exception:
            return None

    def _detect_file_type(self, url: str, content_type: str, content: str) -> str:
        """檢測檔案類型"""
        url_lower = url.lower()
        
        # 從 URL 副檔名判斷
        if url_lower.endswith('.json'):
            return 'json'
        elif url_lower.endswith(('.xml', '.xspf')):
            return 'xml'
        elif url_lower.endswith(('.m3u', '.m3u8')):
            return 'm3u'
        elif url_lower.endswith('.txt'):
            return 'txt'
        elif url_lower.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
            return 'image'
        
        # 從 Content-Type 判斷
        content_lower = content_type.lower()
        if 'json' in content_lower:
            return 'json'
        elif 'xml' in content_lower:
            return 'xml'
        elif 'mpegurl' in content_lower or content_lower == 'audio/x-mpegurl':
            return 'm3u'
        elif 'html' in content_lower:
            return 'html'
        elif 'image' in content_lower:
            return 'image'
        elif 'plain' in content_lower:
            return 'txt'
        
        # 從內容判斷
        content_lower = content.lower()
        if content_lower.strip().startswith('{') or content_lower.strip().startswith('['):
            return 'json'
        elif '<?xml' in content_lower or '<tv' in content_lower:
            return 'xml'
        elif '#extm3u' in content_lower:
            return 'm3u'
        elif '<html' in content_lower or '<!doctype html' in content_lower:
            return 'html'
        
        return 'unknown'

    # ========================================================================
    # 各類型內容驗證器
    # ========================================================================

    def _validate_json(self, content: str) -> Tuple[bool, Optional[str]]:
        """驗證 JSON 內容"""
        content = content.strip()
        if not content:
            return False, "內容為空"
        
        # 檢查是否為 HTML 錯誤頁面
        if '<html' in content.lower():
            return False, "不是 JSON（疑似 HTML 錯誤頁面）"
        
        try:
            data = json.loads(content)
            # 檢查是否有實際內容
            if isinstance(data, dict) and not data:
                return False, "JSON 為空物件"
            if isinstance(data, list) and not data:
                return False, "JSON 為空陣列"
            return True, None
        except json.JSONDecodeError as e:
            return False, f"JSON 解析錯誤: {str(e)[:50]}"

    def _validate_xml(self, content: str) -> Tuple[bool, Optional[str]]:
        """驗證 XML 內容"""
        content_lower = content.lower()
        if not content.strip():
            return False, "內容為空"
        
        # 檢查 XML 標記
        if '<?xml' in content_lower:
            return True, None
        if '<tv' in content_lower and '</tv>' in content_lower:
            return True, None
        if '<rss' in content_lower and '</rss>' in content_lower:
            return True, None
        if '<channel' in content_lower and '</channel>' in content_lower:
            return True, None
        
        return False, "不是有效的 XML 格式"

    def _validate_m3u(self, content: str) -> Tuple[bool, Optional[str]]:
        """驗證 M3U 內容"""
        content_upper = content.upper()
        if not content.strip():
            return False, "內容為空"
        
        # 檢查 #EXTM3U 標記
        if '#EXTM3U' in content_upper:
            return True, None
        
        # 檢查是否有任何 M3U 特徵
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        if not lines:
            return False, "內容為空行"
        
        # 檢查是否有 URL 或檔案路徑
        has_url = any(re.search(r'https?://', l) for l in lines)
        has_extinf = any('#EXTINF' in l.upper() for l in lines)
        
        if has_url or has_extinf:
            return True, None
        
        return False, "不是有效的 M3U 格式"

    def _validate_txt(self, content: str) -> Tuple[bool, Optional[str]]:
        """驗證 TXT 內容"""
        content_lower = content.lower()
        
        # 檢查是否為錯誤頁面
        error_keywords = [
            '404', 'not found', 'forbidden', 'access denied',
            'nginx', '<html', 'error', 'bad gateway',
            'service unavailable', 'permission denied'
        ]
        
        for keyword in error_keywords:
            if keyword in content_lower:
                return False, f"包含錯誤關鍵字: {keyword}"
        
        # 檢查是否有實際內容
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        if len(lines) == 0:
            return False, "內容為空"
        
        # 檢查是否包含 URL（至少有一些有意義的內容）
        has_http = any(re.search(r'https?://', l) for l in lines)
        if not has_http:
            # 沒有 URL 但有一些內容，可能是有效的
            return len(lines) >= 3, "內容太少" if len(lines) < 3 else None
        
        return True, None

    def _validate_html(self, content: str) -> Tuple[bool, Optional[str]]:
        """驗證 HTML 內容"""
        content_lower = content.lower()
        
        # 檢查是否為錯誤頁面
        error_keywords = ['404', 'not found', 'error', 'bad gateway']
        for keyword in error_keywords:
            if keyword in content_lower and '404' in content_lower:
                return False, "404 錯誤頁面"
        
        # 檢查是否是有效的 HTML
        if '<html' in content_lower or '<!doctype html' in content_lower:
            # 檢查是否有實際內容
            if len(content.strip()) < 100:
                return False, "HTML 內容太少"
            return True, None
        
        return False, "不是有效的 HTML"

    def _validate_image(self, content: str) -> Tuple[bool, Optional[str]]:
        """驗證圖片內容"""
        # 檢查是否是二進位內容
        if content.startswith(('ffd8', '89504e47', '47494638')):
            return True, None
        return False, "不是有效的圖片格式"

    def _validate_common(self, content: str) -> Tuple[bool, Optional[str]]:
        """通用內容驗證"""
        content_lower = content.lower()
        
        # 檢查常見錯誤
        error_keywords = [
            '404', 'not found', 'access denied', 'forbidden',
            'error', '502 bad gateway', '503 service',
            'nginx', '<html>', 'internal server error'
        ]
        
        for keyword in error_keywords:
            if keyword in content_lower:
                return False, f"包含錯誤關鍵字: {keyword}"
        
        # 檢查是否有內容
        if len(content.strip()) < 10:
            return False, "內容太少"
        
        return True, None

    # ========================================================================
    # 其他輔助功能
    # ========================================================================

    def get_url_hash(self, url: str) -> str:
        """計算 URL 的 hash"""
        return hashlib.md5(url.encode()).hexdigest()

    def is_same_domain(self, url1: str, url2: str) -> bool:
        """檢查兩個 URL 是否為同一域名"""
        domain1 = urlparse(url1).netloc
        domain2 = urlparse(url2).netloc
        return domain1 == domain2

    def get_response_time_grade(self, response_time: float) -> str:
        """根據響應時間評級"""
        if response_time < 0.5:
            return "優良 ⭐⭐⭐"
        elif response_time < 1.0:
            return "良好 ⭐⭐"
        elif response_time < 2.0:
            return "普通 ⭐"
        elif response_time < 5.0:
            return "較慢"
        else:
            return "極慢"

    def clear_cache(self):
        """清除快取"""
        self.cache.clear()

# ============================================================================
# 主程式整合
# ============================================================================

class TVBoxChecker:
    """TVBox URL 檢查器主程式"""
    
    def __init__(self):
        self.checker = URLValidityChecker()
        self.total = 0
        self.valid = 0
        self.invalid = 0
        self.duplicate = 0
        self.seen = set()
        self.invalid_urls = []
        self.duplicate_urls = []
        self.results = []
        
        # URL 模式
        self.url_pattern = re.compile(r'https?://[^\s<>"\']+')

    def load_lines(self) -> list:
        """載入輸入檔案"""
        p = Path(INPUT_FILE)
        if not p.exists():
            raise FileNotFoundError(f"找不到檔案: {INPUT_FILE}")
        return p.read_text(encoding='utf-8', errors='ignore').splitlines()

    def save_lines(self, lines: list):
        """儲存輸出檔案"""
        Path(OUTPUT_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(OUTPUT_FILE).write_text('\n'.join(lines), encoding='utf-8')

    def save_invalid(self):
        if self.invalid_urls:
            Path(INVALID_FILE).write_text(
                '\n'.join(self.invalid_urls), encoding='utf-8'
            )

    def save_duplicate(self):
        if self.duplicate_urls:
            Path(DUPLICATE_FILE).write_text(
                '\n'.join(self.duplicate_urls), encoding='utf-8'
            )

    def extract_urls(self, line: str) -> list:
        """從行中提取所有 URL"""
        return self.url_pattern.findall(line)

    def is_duplicate(self, url: str) -> bool:
        """檢查 URL 是否重複"""
        if url in self.seen:
            self.duplicate += 1
            self.duplicate_urls.append(url)
            return True
        self.seen.add(url)
        return False

    def check_all(self):
        """執行完整檢查"""
        lines = self.load_lines()
        cleaned_lines = []
        tasks = []

        print(f"📂 載入 {len(lines)} 行資料...")
        print(f"🔍 開始檢查網址有效性...")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 提交所有任務
            for line in lines:
                urls = self.extract_urls(line)
                
                if not urls:
                    cleaned_lines.append(line)
                    continue

                newline = line
                futures = []

                for url in urls:
                    self.total += 1
                    
                    if self.is_duplicate(url):
                        newline = newline.replace(url, "")
                        continue

                    future = executor.submit(self.checker.check_url, url)
                    futures.append((future, url))

                tasks.append((newline, futures))

            # 收集結果
            for idx, (newline, futures) in enumerate(tasks):
                for future, url in futures:
                    try:
                        is_valid, details = future.result(timeout=TIMEOUT + 5)
                        
                        if is_valid:
                            self.valid += 1
                            # 可選擇記錄詳細資訊
                            # self.results.append((url, details))
                        else:
                            self.invalid += 1
                            self.invalid_urls.append(url)
                            newline = newline.replace(url, "")
                            error = details.get("error", "未知錯誤")
                            print(f"  ❌ {url[:60]}... - {error}")
                    except Exception as e:
                        self.invalid += 1
                        self.invalid_urls.append(url)
                        newline = newline.replace(url, "")
                        print(f"  ❌ {url[:60]}... - 檢查失敗: {str(e)[:30]}")
                
                cleaned_lines.append(newline)
                
                # 顯示進度
                if (idx + 1) % 10 == 0:
                    progress = (idx + 1) / len(tasks) * 100
                    print(f"  進度: {progress:.1f}% ({idx + 1}/{len(tasks)})")

        # 儲存結果
        print(f"\n💾 儲存結果...")
        self.save_lines(cleaned_lines)
        self.save_invalid()
        self.save_duplicate()
        
        # 生成報告
        self.generate_report()

    def generate_report(self):
        """生成詳細報告"""
        lines = [
            "# 📊 TVBox URL 檢查報告",
            "",
            "## 📈 統計摘要",
            "",
            f"| 項目 | 數量 | 比例 |",
            f"|------|------|------|",
            f"| 總網址數 | {self.total} | 100% |",
            f"| ✅ 有效 | {self.valid} | {(self.valid/self.total*100):.1f}%" if self.total > 0 else "| ✅ 有效 | 0 | 0% |",
            f"| ❌ 失效 | {self.invalid} | {(self.invalid/self.total*100):.1f}%" if self.total > 0 else "| ❌ 失效 | 0 | 0% |",
            f"| 🔄 重複 | {self.duplicate} | {(self.duplicate/self.total*100):.1f}%" if self.total > 0 else "| 🔄 重複 | 0 | 0% |",
            "",
            "## 🔍 檢查詳細",
            "",
            f"- **有效比率**: {(self.valid/self.total*100):.1f}%" if self.total > 0 else "- **有效比率**: N/A",
            f"- **失效比率**: {(self.invalid/self.total*100):.1f}%" if self.total > 0 else "- **失效比率**: N/A",
            "",
            f"## 📋 無效網址列表 ({len(self.invalid_urls)} 個)",
            "",
        ]
        
        if self.invalid_urls:
            # 只列出前 20 個
            for url in self.invalid_urls[:20]:
                lines.append(f"- `{url}`")
            if len(self.invalid_urls) > 20:
                lines.append(f"- ... 還有 {len(self.invalid_urls) - 20} 個")
        
        lines.extend([
            "",
            f"## 📋 重複網址列表 ({len(self.duplicate_urls)} 個)",
            ""
        ])
        
        if self.duplicate_urls:
            for url in self.duplicate_urls[:20]:
                lines.append(f"- `{url}`")
            if len(self.duplicate_urls) > 20:
                lines.append(f"- ... 還有 {len(self.duplicate_urls) - 20} 個")
        
        lines.extend([
            "",
            f"---",
            f"🕐 更新時間：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "✅ 報告由 TVBox URL Checker Pro v3 自動生成"
        ])
        
        Path(REPORT_FILE).write_text('\n'.join(lines), encoding='utf-8')

# ============================================================================
# 主程式
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("🚀 TVBox URL Checker Pro v3 - 進階版")
    print("=" * 70)
    
    start_time = time.time()
    
    try:
        checker = TVBoxChecker()
        checker.check_all()
        
        # 輸出結果
        print("\n" + "=" * 70)
        print("✅ 檢查完成！")
        print("=" * 70)
        print(f"📊 總網址 : {checker.total}")
        print(f"✅ 有效   : {checker.valid}")
        print(f"❌ 失效   : {checker.invalid}")
        print(f"🔄 重複   : {checker.duplicate}")
        print(f"⏱️ 耗時   : {time.time() - start_time:.2f} 秒")
        print("=" * 70)
        print(f"\n📁 輸出檔案：")
        print(f"  - 有效清單: {OUTPUT_FILE}")
        print(f"  - 無效清單: {INVALID_FILE}")
        print(f"  - 重複清單: {DUPLICATE_FILE}")
        print(f"  - 檢查報告: {REPORT_FILE}")
        
    except KeyboardInterrupt:
        print("\n\n⚠️ 使用者中斷執行")
    except Exception as e:
        print(f"\n❌ 錯誤：{e}")
        import traceback
        traceback.print_exc()
