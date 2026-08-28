#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
影视仓 / TVBox 配置自动更新脚本
=================================
功能：
  1. 读取 config/sources.json 中的配置源列表
  2. 对每个源依次尝试"主线 + 备用"多个地址，自动做中文域名 punycode 转换
  3. 校验返回内容是否为合法的 TVBox/影视仓 单仓配置（包含 sites/spiders/lives 等字段）
  4. 将每个源的最新配置保存到 output/<id>.json
  5. 生成多仓订阅文件 output/多仓订阅.json（可直接填进影视仓的"订阅"或"多仓"）
  6. 生成聚合单仓 output/单仓聚合.json（所有源 sites/lives/parses 合并去重）
  7. 生成 shields 徽章 output/shield.json，并把各源状态写回 README
兼容：本脚本同时兼容 GitHub Actions 自动运行 与 本地手动运行。
在 GitHub Actions 中，环境变量 GITHUB_REPOSITORY 会被自动注入（格式 owner/repo），
用于拼出多仓订阅里指向本仓库 raw 文件的完整 URL。
用法：
  python scripts/update.py
"""
import datetime
import json
import os
import sys
import time
import urllib.parse

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(ROOT, "config", "sources.json")
OUTPUT_DIR = os.path.join(ROOT, "output")

DEFAULT_REPO = "YOUR_USERNAME/tvbox-ysc-config"  # 本地运行时的占位仓库名，GitHub Actions 会自动覆盖

# 注意：部分接口的反爬会针对"完整 Chrome UA"下发挑战页，反而用简洁 UA 能正常返回 JSON
USER_AGENT = "Mozilla/5.0"

TIMEOUT = 20  # 单个地址超时（秒）
MAX_RETRIES = 2  # 单个地址最大尝试次数（仅对超时/连接类瞬态错误重试）
RETRY_BACKOFF = 3  # 重试基础等待秒数
URL_INTERVAL = 1.5  # 每个地址之间的小间隔秒数（降低被限流概率）
SOURCE_INTERVAL = 3  # 每个源之间的间隔秒数（降低被限流概率）

# 判定"像 TVBox 单仓配置"的顶层字段
TVBOX_KEYS = ("sites", "spiders", "lives", "store", "exts", "parses", "flags")


def _strip_js_comments(text: str) -> str:
    """剔除 JSON 中内嵌的 // 行注释（部分接口会在字段后追加免责注释）。"""
    lines = text.split("\n")
    cleaned = [ln for ln in lines if not ln.lstrip().startswith("//")]
    return "\n".join(cleaned)


def parse_json_lenient(text: str):
    """宽容解析：去 BOM/空白、剔 // 注释后优先整体 json.loads；
    若接口返回了拼接的多段 JSON，取第一段有效对象。"""
    text = text.lstrip("\ufeff").strip()
    text = _strip_js_comments(text)
    try:
        # strict=False 允许字符串中出现未转义的控制字符（部分接口的 JSON 不规范）
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        # 兼容"返回了多个 JSON 拼接在一起"的接口
        decoder = json.JSONDecoder(strict=False)
        try:
            obj, _ = decoder.raw_decode(text)
            return obj
        except Exception:
            raise


def idn_encode(url: str) -> str:
    """将 URL 中的中文（国际化）域名转成 punycode，避免 DNS 解析失败。
    例如：http://www.饭太硬.com/tv -> http://www.xn--sss604efuw.com/tv
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    try:
        encoded_host = host.encode("idna").decode("ascii")
    except Exception:
        encoded_host = host  # 已是 ascii 或转换失败，原样使用
    netloc = encoded_host
    if parsed.port:
        netloc = f"{encoded_host}:{parsed.port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


def is_valid_tvbox_config(data) -> bool:
    """粗略校验：返回内容是否像 TVBox/影视仓 单仓配置。"""
    if not isinstance(data, dict):
        return False
    return any(key in data for key in TVBOX_KEYS)


def _should_retry(err: Exception, resp_text_len: int = -1) -> bool:
    """判断失败是否值得重试：
    - 超时/连接类瞬态错误：重试
    - 空响应 / JSON 解析失败（疑似服务端反爬冷却）：重试（加长等待）
    - 4xx/5xx、返回了内容但结构不对（确定性失败）：不重试
    """
    if isinstance(err, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(err, requests.exceptions.HTTPError):
        return False  # 4xx/5xx 为确定性失败
    if isinstance(err, json.JSONDecodeError):
        return True  # 空响应或非 JSON，可能是反爬/冷却，给一次机会
    if resp_text_len <= 0:
        return True  # 没有拿到任何内容
    return False


def fetch_config(url: str) -> dict:
    """抓取单个地址并解析为 JSON dict；失败时抛出异常。"""
    target = idn_encode(url)
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                target,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json,text/plain,*/*",
                },
                timeout=TIMEOUT,
                allow_redirects=True,
            )
            resp.raise_for_status()
            data = parse_json_lenient(resp.text)
            if not is_valid_tvbox_config(data):
                raise ValueError("返回内容不是 TVBox 配置（缺少 sites/spiders/lives 等字段）")
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            text_len = len(getattr(resp, "text", "") or "") if "resp" in locals() else -1
            if _should_retry(e, text_len) and attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF * attempt * 2
                print(f"      (第 {attempt} 次失败：{e}，{wait}s 后重试)")
                time.sleep(wait)
            else:
                break
    raise last_err


def _merge_list(fetched, field):
    """合并各源中的列表字段（lives/parses/doh 等），按 name/url 去重。"""
    seen = set()
    out = []
    for _sid, _name, data in fetched:
        for item in data.get(field, []) or []:
            if isinstance(item, dict):
                ident = item.get("name") or item.get("url") or json.dumps(
                    item, ensure_ascii=False, sort_keys=True
                )
            else:
                ident = str(item)
            if ident in seen:
                continue
            seen.add(ident)
            out.append(item)
    return out


def merge_configs(fetched):
    """把多个单仓配置合并成一个聚合单仓：
    - 以第一个成功源为基底（保留 spider/logo/wallpaper 等）
    - sites 按 key 去重，重复 key 加源 id 前缀
    - lives/parses/doh/rules/flags/exts 等列表合并去重
    fetched: [(sid, name, data_dict), ...]
    """
    if not fetched:
        return None
    _base_sid, base_name, base = fetched[0]
    merged = {}
    skip = {"sites", "lives", "parses", "doh", "rules", "flags", "exts", "ads"}
    for k, v in base.items():
        if k not in skip:
            merged[k] = v
    merged["warningText"] = (
        f"本配置由 {len(fetched)} 个公开源自动聚合（含{base_name}等），仅供学习交流，请勿商用"
    )

    seen_keys = set()
    sites = []
    for sid, _name, data in fetched:
        for s in data.get("sites", []) or []:
            if not isinstance(s, dict):
                continue
            key = s.get("key")
            if key is None:
                key = json.dumps(s, ensure_ascii=False, sort_keys=True)
            new_key = key
            if key in seen_keys:
                new_key = f"{sid}_{key}"
            if new_key in seen_keys:
                continue
            seen_keys.add(new_key)
            if new_key != key:
                s = dict(s)
                s["key"] = new_key
            sites.append(s)
    merged["sites"] = sites

    for field in ("lives", "parses", "doh", "rules", "flags", "exts"):
        merged[field] = _merge_list(fetched, field)

    return merged


README_PATH = os.path.join(ROOT, "README.md")
STATUS_START = "<!-- STATUS_START -->"
STATUS_END = "<!-- STATUS_END -->"


def update_readme(status, ok_count, total):
    """把各源更新状态以表格形式写回 README（在 STATUS_START/END 标记之间）。"""
    if not os.path.exists(README_PATH):
        return
    with open(README_PATH, "r", encoding="utf-8") as f:
        readme = f.read()
    if STATUS_START not in readme or STATUS_END not in readme:
        return  # 未放置标记则不改动

    lines = []
    lines.append(f"> 🔄 最近更新：{status['updated_at']}（北京时间） · 成功 **{ok_count}/{total}** 个源")
    lines.append("")
    lines.append("| 配置源 | 状态 | 使用地址 |")
    lines.append("| --- | --- | --- |")
    for s in status["sources"]:
        if s["ok"]:
            lines.append(f"| {s['name']} | ✅ 成功 | `{s['used_url']}` |")
        else:
            err = (s.get("error") or "").replace("|", "/")
            if len(err) > 60:
                err = err[:57] + "..."
            lines.append(f"| {s['name']} | ❌ 失败 | {err} |")
    block = "\n".join(lines)

    prefix = readme.split(STATUS_START, 1)[0]
    suffix = readme.split(STATUS_END, 1)[1]
    new_readme = f"{prefix}{STATUS_START}\n{block}\n{STATUS_END}{suffix}"
    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(new_readme)


def write_badge(ok_count, total):
    """生成 shields.io endpoint 徽章 JSON。"""
    if total == 0 or ok_count == 0:
        color = "red"
    elif ok_count < total:
        color = "orange"
    else:
        color = "green"
    badge = {
        "schemaVersion": 1,
        "label": "TVBox 配置",
        "message": f"{ok_count}/{total} 可用",
        "color": color,
    }
    with open(os.path.join(OUTPUT_DIR, "shield.json"), "w", encoding="utf-8") as f:
        json.dump(badge, f, ensure_ascii=False, indent=2)


def main() -> int:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 东八区时间，便于阅读
    tz = datetime.timezone(datetime.timedelta(hours=8))
    now_str = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    # GitHub Actions 中自动注入 GITHUB_REPOSITORY=owner/repo
    repo = (
        os.environ.get("GITHUB_REPOSITORY")
        or os.environ.get("REPO")
        or DEFAULT_REPO
    )
    raw_base = f"https://raw.githubusercontent.com/{repo}/main/output"

    status = {"updated_at": now_str, "repo": repo, "sources": []}
    subscriptions = []
    fetched = []  # [(sid, name, data_dict), ...] 用于聚合单仓

    ok_count = 0
    for src in cfg.get("sources", []):
        name = src.get("name", "未知")
        sid = src.get("id", name)
        urls = src.get("urls", [])

        entry = {"id": sid, "name": name, "ok": False, "used_url": None, "error": None}
        saved = False

        for url in urls:
            try:
                data = fetch_config(url)
                out_path = os.path.join(OUTPUT_DIR, f"{sid}.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                entry["ok"] = True
                entry["used_url"] = url
                entry["error"] = None
                subscriptions.append(
                    {"name": name, "url": f"{raw_base}/{sid}.json"}
                )
                fetched.append((sid, name, data))
                ok_count += 1
                saved = True
                print(f"[OK]   {name} <- {url}")
                break
            except Exception as e:  # noqa: BLE001
                print(f"[FAIL] {name}  {url}  ->  {e}")
                entry["error"] = str(e)

            # 地址之间稍作间隔，避免被限流
            time.sleep(URL_INTERVAL)

        if not saved:
            entry["error"] = entry["error"] or "所有地址均失败"
            print(f"[FAIL] {name}  全部地址失败")
        status["sources"].append(entry)

        # 源之间稍作间隔，避免集中请求被限流
        time.sleep(SOURCE_INTERVAL)

    # 生成多仓订阅文件（影视仓"订阅/多仓"格式）
    sub_path = os.path.join(OUTPUT_DIR, "多仓订阅.json")
    with open(sub_path, "w", encoding="utf-8") as f:
        json.dump({"urls": subscriptions}, f, ensure_ascii=False, indent=2)

    # 生成聚合单仓文件（把所有成功源的 sites/lives/parses 合并成一个单仓）
    merged = merge_configs(fetched)
    merged_path = os.path.join(OUTPUT_DIR, "单仓聚合.json")
    if merged is not None:
        with open(merged_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        print(f"聚合单仓：{merged_path}（共 {len(merged.get('sites', []))} 个站点）")

    # 生成更新状态文件（时间取全部抓取完成之后）
    status["updated_at"] = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(OUTPUT_DIR, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    # 生成 shields 徽章并更新 README 状态表
    total = len(cfg.get("sources", []))
    write_badge(ok_count, total)
    update_readme(status, ok_count, total)

    print("\n" + "=" * 50)
    print(f"本次更新：{ok_count}/{total} 个源成功")
    print(f"多仓订阅：{sub_path}")
    if ok_count == 0:
        print("没有任何源更新成功，返回非 0 退出码。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
