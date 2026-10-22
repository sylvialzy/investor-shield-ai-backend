import json
import os
import re
import threading
import time
import uuid
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Callable

import pandas as pd
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel, Field
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

W_TIME = {"T": 0.937, "T1": 0.063}
W_PLATFORM = {"东方财富": 0.5, "证券时报": 0.3, "新浪财经": 0.2}
SOURCE_NAMES = ["东方财富", "新浪财经", "证券时报"]
SOURCE_TIMEOUT_SECONDS = 45
PAGE_LOAD_TIMEOUT_SECONDS = 25
ANALYSIS_TIMEOUT_SECONDS = 240
NEWS_LOOKBACK_DAYS = 1
API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()

# 搜索词不是单向“缩短”。用户输入任意一个词，都要扩展到同一组相关词。
# 这样输入“茅台”能搜到“贵州茅台”，输入“贵州茅台”也能搜到“茅台”。
STOCK_KEYWORD_GROUPS = [
    {"贵州茅台", "茅台", "600519", "贵州茅台酒", "茅台集团"},
    {"五粮液", "000858", "五粮液集团"},
    {"宁德时代", "300750"},
    {"三六零", "360", "601360"},
    {"比亚迪", "002594"},
    {"药明康德", "603259"},
    {"中信证券", "600030"},
    {"紫金矿业", "601899"},
]


def expand_stock_keywords(stock: str) -> list[str]:
    """返回原始输入 + 命中的相关关键词，保持双向扩展。"""
    query = str(stock or "").strip()
    if not query:
        return []
    keywords = [query]
    for group in STOCK_KEYWORD_GROUPS:
        if any(term == query or term in query or query in term for term in group):
            keywords.extend(sorted(group, key=lambda item: (-len(item), item)))
    result = []
    seen = set()
    for keyword in keywords:
        normalized = keyword.casefold()
        if normalized not in seen:
            result.append(keyword)
            seen.add(normalized)
    return result


def title_matches_keywords(title: str, keywords: list[str]) -> bool:
    text = str(title or "").casefold()
    return any(keyword.casefold() in text for keyword in keywords if keyword)

app = FastAPI(title="InvestorShield AI API", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://sylvialzy.github.io",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()


class AnalysisRequest(BaseModel):
    stock: str = Field(min_length=1, max_length=30)


def log(message: str) -> None:
    print(f"[InvestorShield] {message}", flush=True)


def set_job(job_id: str, **updates: Any) -> None:
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(updates)


def universal_date_parse(value: Any):
    now = datetime.now()
    today = now.date()
    text = str(value or "").strip()
    try:
        if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", text) or any(
            token in text for token in ["分钟", "小时", "秒", "今天", "刚刚"]
        ):
            return today
        if "昨天" in text:
            return today - timedelta(days=1)
        full = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
        if full:
            date = datetime(int(full.group(1)), int(full.group(2)), int(full.group(3))).date()
            return date if date <= today else date.replace(year=date.year - 1)
        short = re.search(r"(\d{1,2})[-/.月](\d{1,2})", text)
        if short:
            year = now.year - 1 if now.month == 1 and int(short.group(1)) == 12 else now.year
            date = datetime(year, int(short.group(1)), int(short.group(2))).date()
            return date if date <= today else date.replace(year=date.year - 1)
    except (TypeError, ValueError):
        return None
    return None


def get_stealth_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
    )
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
    driver.set_script_timeout(15)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def safe_get(driver, url: str, source: str) -> bool:
    try:
        driver.get(url)
        return True
    except TimeoutException as exc:
        log(f"{source} PAGE_TIMEOUT url={url} error={exc.__class__.__name__}")
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
        return False
    except WebDriverException as exc:
        log(f"{source} DRIVER_ERROR error={exc.__class__.__name__}: {exc}")
        return False


def within_budget(start: float, source: str) -> bool:
    elapsed = time.monotonic() - start
    if elapsed >= SOURCE_TIMEOUT_SECONDS:
        log(f"{source} SOURCE_TIMEOUT elapsed={elapsed:.1f}s")
        return False
    return True


def fetch_eastmoney(driver, stock: str, deadline):
    source = "东方财富"
    start = time.monotonic()
    result, seen = [], set()
    keywords = expand_stock_keywords(stock)
    log(f"{source} SOURCE_START stock={stock} keywords={keywords}")
    try:
        for keyword in keywords:
            if not within_budget(start, source):
                break
            url_keyword = urllib.parse.quote(keyword)
            safe_get(driver, f"https://so.eastmoney.com/news/s?keyword={url_keyword}", source)
            time.sleep(2)
            try:
                WebDriverWait(driver, 4).until(
                    EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), '默认排序')]"))
                ).click()
                time.sleep(0.5)
                driver.find_element(By.XPATH, "//li[contains(text(), '按时间排序')]").click()
                time.sleep(1)
            except Exception:
                log(f"{source} SORT_SKIP keyword={keyword}")
            no_new_rounds = 0
            for page in range(1, 13):
                if not within_budget(start, source):
                    break
                items = driver.find_elements(By.CSS_SELECTOR, ".news-item, .news_item")
                added = 0
                for item in items:
                    if not within_budget(start, source):
                        break
                    try:
                        date = universal_date_parse(item.text)
                        links = item.find_elements(By.TAG_NAME, "a")
                        if date and date < deadline:
                            no_new_rounds = 2
                            break
                        if date and date >= deadline and links:
                            link = links[0]
                            url, title = link.get_attribute("href"), link.text.strip()
                            if url and title and url not in seen and title_matches_keywords(title, keywords):
                                result.append({"日期": date, "标题": title, "链接": url, "来源": source})
                                seen.add(url)
                                added += 1
                    except Exception:
                        continue
                log(f"{source} KEYWORD={keyword} PAGE={page} ADDED={added} TOTAL={len(result)}")
                no_new_rounds = no_new_rounds + 1 if added == 0 else 0
                if no_new_rounds >= 2:
                    break
                try:
                    next_button = driver.find_element(
                        By.XPATH, "//a[text()='>'] | //a[contains(text(), '下一页')]"
                    )
                    driver.execute_script("arguments[0].click();", next_button)
                    time.sleep(1.5)
                except Exception:
                    break
    except Exception as exc:
        log(f"{source} SOURCE_ERROR type={exc.__class__.__name__}: {exc}")
    log(f"{source} SOURCE_END count={len(result)} elapsed={time.monotonic() - start:.1f}s keywords={keywords}")
    return result


def fetch_sina(driver, stock: str, deadline):
    source = "新浪财经"
    start = time.monotonic()
    result, seen = [], set()
    keywords = expand_stock_keywords(stock)
    log(f"{source} SOURCE_START stock={stock} keywords={keywords}")
    try:
        for keyword in keywords:
            if not within_budget(start, source):
                break
            url_keyword = urllib.parse.quote(keyword)
            safe_get(driver, f"https://search.sina.com.cn/?q={url_keyword}&c=news", source)
            time.sleep(2)
            no_new_rounds = 0
            for page in range(1, 13):
                if not within_budget(start, source):
                    break
                items = driver.find_elements(By.CSS_SELECTOR, ".box-result, .result, article")
                added = 0
                for item in items:
                    if not within_budget(start, source):
                        break
                    try:
                        text = item.text.strip()
                        date_nodes = item.find_elements(By.CLASS_NAME, "fgray_time")
                        date = universal_date_parse(date_nodes[0].text if date_nodes else text)
                        title_nodes = item.find_elements(By.CSS_SELECTOR, "h2 a, h3 a, .c-title a")
                        link = title_nodes[0] if title_nodes else (item.find_elements(By.TAG_NAME, "a") or [None])[0]
                        if date and date < deadline:
                            no_new_rounds = 2
                            break
                        if date and date >= deadline and link is not None:
                            url, title = link.get_attribute("href"), link.text.strip()
                            if url and title and url not in seen and title_matches_keywords(title, keywords):
                                result.append({"日期": date, "标题": title, "链接": url, "来源": source})
                                seen.add(url)
                                added += 1
                    except Exception:
                        continue
                log(f"{source} KEYWORD={keyword} PAGE={page} ADDED={added} TOTAL={len(result)}")
                no_new_rounds = no_new_rounds + 1 if added == 0 else 0
                if no_new_rounds >= 2:
                    break
                try:
                    next_button = driver.find_element(By.XPATH, "//a[contains(text(), '下一页')]")
                    driver.execute_script("arguments[0].click();", next_button)
                    time.sleep(1.5)
                except Exception:
                    break
    except Exception as exc:
        log(f"{source} SOURCE_ERROR type={exc.__class__.__name__}: {exc}")
    log(f"{source} SOURCE_END count={len(result)} elapsed={time.monotonic() - start:.1f}s keywords={keywords}")
    return result


def fetch_stcn(driver, stock: str, deadline):
    source = "证券时报"
    start = time.monotonic()
    result, seen = [], set()
    keywords = expand_stock_keywords(stock)
    log(f"{source} SOURCE_START stock={stock} keywords={keywords}")
    try:
        for keyword in keywords:
            if not within_budget(start, source):
                break
            url_keyword = urllib.parse.quote(keyword)
            safe_get(driver, f"https://www.stcn.com/article/search.html?keyword={url_keyword}", source)
            time.sleep(3)
            no_new_rounds = 0
            last_height = 0
            for scroll in range(1, 16):
                if not within_budget(start, source):
                    break
                items = driver.find_elements(
                    By.XPATH,
                    "//li[.//a] | //div[contains(@class,'item')] | //div[contains(@class,'news')] | //article",
                )
                added = 0
                found_old = False
                for item in items:
                    if not within_budget(start, source):
                        break
                    try:
                        text = item.text.strip()
                        if len(text) < 8:
                            continue
                        date = universal_date_parse(text)
                        links = item.find_elements(By.TAG_NAME, "a")
                        if not links:
                            continue
                        link = links[0]
                        url, title = link.get_attribute("href"), link.text.strip()
                        if not url or not title or url in seen:
                            continue
                        if date and date < deadline:
                            found_old = True
                        elif date and date >= deadline and title_matches_keywords(title, keywords):
                            result.append({"日期": date, "标题": title, "链接": url, "来源": source})
                            seen.add(url)
                            added += 1
                    except Exception:
                        continue
                log(f"{source} KEYWORD={keyword} SCROLL={scroll} ADDED={added} TOTAL={len(result)}")
                if found_old:
                    break
                no_new_rounds = no_new_rounds + 1 if added == 0 else 0
                if no_new_rounds >= 3:
                    break
                new_height = driver.execute_script("return document.body.scrollHeight")
                if new_height == last_height and added == 0:
                    break
                last_height = new_height
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1.5)
    except Exception as exc:
        log(f"{source} SOURCE_ERROR type={exc.__class__.__name__}: {exc}")
    log(f"{source} SOURCE_END count={len(result)} elapsed={time.monotonic() - start:.1f}s keywords={keywords}")
    return result


def score_and_report(df: pd.DataFrame, stock: str):
    if not API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")
    client = OpenAI(api_key=API_KEY, base_url="https://api.deepseek.com", timeout=60.0, max_retries=1)
    scores: list[float] = []
    for start in range(0, len(df), 50):
        batch = df.iloc[start:start + 50]
        news_text = "\n".join(f"[{i}] {row['标题']}" for i, (_, row) in enumerate(batch.iterrows()))
        prompt = f"只需打分。严格返回 JSON: {{\"scores\": [数字列表]}}。分数范围[-1, 1]。\n股票：{stock}\n新闻：\n{news_text}"
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        batch_scores = json.loads(response.choices[0].message.content).get("scores", [])
        scores.extend(max(-1.0, min(1.0, float(score))) for score in batch_scores[:len(batch)])
        scores.extend([0.0] * (len(batch) - len(batch_scores)))
    today = datetime.now().date()
    weighted_score = sum(
        scores[index]
        * (W_TIME["T"] if row["日期"] == today else W_TIME["T1"])
        * W_PLATFORM.get(row["来源"], 0.2)
        for index, (_, row) in enumerate(df.iterrows())
    )
    sample = "\n".join(f"- {title}" for title in df["标题"].head(30))
    final_prompt = f"作为资深证券分析师，基于对全部 {len(df)} 条新闻的打分分析（总体信心分：{weighted_score:.4f}），请撰写深度研报。必须返回 JSON，字段为 summary、pros、cons、strategy、decision。summary 不少于200字；pros 和 cons 各3条；strategy 不少于150字；decision 包含评级和理由。股票：{stock}。参考样本：\n{sample}"
    final_response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": final_prompt}],
        response_format={"type": "json_object"},
        max_tokens=4000,
    )
    return json.loads(final_response.choices[0].message.content), weighted_score, scores


def normalize_report(report: dict[str, Any], score: float, df: pd.DataFrame, scores: list[float], warnings: list[str]):
    decision = report.get("decision", "谨慎观察")
    rating = decision.get("rating", "谨慎观察") if isinstance(decision, dict) else str(decision)
    reason = decision.get("reason", "") if isinstance(decision, dict) else str(decision)
    counts = {source: int((df["来源"] == source).sum()) for source in SOURCE_NAMES}
    direction = max(-1.0, min(1.0, score / max(1.0, len(df) * 0.5)))
    evidence = []
    for index, (_, row) in enumerate(df.head(12).iterrows()):
        evidence.append({
            "source": row["来源"],
            "date": row["日期"].isoformat() if hasattr(row["日期"], "isoformat") else str(row["日期"]),
            "sentiment": scores[index] if index < len(scores) else 0,
            "title": row["标题"],
            "url": row["链接"],
            "reason": "由新闻标题情绪分析和平台权重共同纳入研判。",
        })
    return {
        "metrics": {
            "direction": direction,
            "direction_label": "偏向积极" if direction > 0.2 else "偏向消极" if direction < -0.2 else "中性观察",
            "intensity": sum(abs(value) for value in scores) / max(1, len(scores)),
            "heat": len(df),
            "filtered_count": 0,
            "platform_coverage": sum(count > 0 for count in counts.values()),
            "platform_counts": counts,
            "raw_score": score,
        },
        "report": {
            "summary": report.get("summary", ""),
            "pros": report.get("pros", []),
            "cons": report.get("cons", []),
            "strategy": report.get("strategy", ""),
            "decision": {"rating": rating, "reason": reason},
        },
        "evidence": evidence,
        "warnings": warnings,
        "model_info": {
            "normalization": "按时间权重和平台权重汇总，再归一化为方向分。",
            "cleaning": "保留标题中包含目标股票名称的新闻，并按链接去重。",
            "notice": "结果用于资讯整理和研究展示，不构成投资建议。",
        },
    }


def run_analysis(job_id: str, stock: str):
    driver = None
    warnings: list[str] = []
    started = time.monotonic()
    try:
        deadline = datetime.now().date() - timedelta(days=NEWS_LOOKBACK_DAYS)
        keywords = expand_stock_keywords(stock)
        log(f"JOB_START id={job_id} stock={stock} keywords={keywords} lookback_days={NEWS_LOOKBACK_DAYS}")
        set_job(job_id, progress=8, message="正在启动新闻采集", source_status={name: {"status": "waiting", "message": "等待中"} for name in SOURCE_NAMES})
        driver = get_stealth_driver()
        raw_data = []
        fetchers: list[tuple[str, Callable]] = [("东方财富", fetch_eastmoney), ("新浪财经", fetch_sina), ("证券时报", fetch_stcn)]
        for index, (source, fetcher) in enumerate(fetchers):
            if time.monotonic() - started >= ANALYSIS_TIMEOUT_SECONDS:
                raise RuntimeError("分析任务超过总时限，已停止采集")
            status = {name: {"status": "waiting", "message": "等待中"} for name in SOURCE_NAMES}
            for previous in SOURCE_NAMES[:index]:
                status[previous] = {"status": "completed", "message": "已完成"}
            status[source] = {"status": "running", "message": "正在采集"}
            set_job(job_id, progress=15 + index * 22, message=f"正在采集 {source} 新闻", source_status=status, warnings=warnings)
            source_data = fetcher(driver, stock, deadline)
            raw_data.extend(source_data)
            status[source] = {"status": "completed" if source_data else "failed", "message": f"已抓取 {len(source_data)} 条" if source_data else "未抓到结果"}
            if not source_data:
                warnings.append(f"{source} 本次未抓到有效新闻，继续使用其他来源。")
            set_job(job_id, source_status=status, warnings=warnings)
        log(f"JOB_COLLECTED id={job_id} total={len(raw_data)} warnings={warnings}")
        if not raw_data:
            raise RuntimeError("未抓取到有效新闻，可能是网站结构变化或浏览器环境不可用")
        df = pd.DataFrame(raw_data).drop_duplicates(subset=["链接"])
        df = df[df["标题"].apply(lambda title: title_matches_keywords(title, keywords))].copy()
        if df.empty:
            raise RuntimeError(f"未筛选到与关键词匹配的新闻：{keywords}")
        set_job(job_id, progress=78, message=f"正在对 {len(df)} 条新闻进行 AI 分析", source_status={name: {"status": "completed", "message": "已完成"} for name in SOURCE_NAMES}, warnings=warnings)
        report, score, scores = score_and_report(df, stock)
        result = normalize_report(report, score, df, scores, warnings)
        log(f"JOB_END id={job_id} status=completed total={len(df)} elapsed={time.monotonic() - started:.1f}s")
        set_job(job_id, status="completed", progress=100, message="分析完成", result={"stock": stock, **result}, warnings=warnings)
    except Exception as exc:
        log(f"JOB_END id={job_id} status=failed type={exc.__class__.__name__}: {exc}")
        warnings.append("请检查 Render 日志、浏览器环境和 DEEPSEEK_API_KEY 配置。")
        set_job(job_id, status="failed", progress=100, message="分析失败", error=str(exc), warnings=warnings)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "investor-shield-ai", "api_key_configured": bool(API_KEY)}


@app.post("/api/analyses")
def create_analysis(request: AnalysisRequest, background_tasks: BackgroundTasks):
    stock = request.stock.strip()
    if not stock:
        raise HTTPException(status_code=400, detail="股票名称不能为空")
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "stock": stock,
            "status": "running",
            "progress": 1,
            "message": "正在创建分析任务",
            "source_status": {},
            "warnings": [],
            "result": None,
        }
    background_tasks.add_task(run_analysis, job_id, stock)
    return {"id": job_id, "status": "running"}


@app.get("/api/analyses/{job_id}")
def get_analysis(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="分析任务不存在")
        return dict(job)
