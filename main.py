import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import OpenAI
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

W_TIME = {"T": 0.937, "T1": 0.063}
W_PLATFORM = {"东方财富": 0.5, "证券时报": 0.3, "新浪财经": 0.2}
SOURCE_NAMES = ["东方财富", "新浪财经", "证券时报"]
API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()

app = FastAPI(title="InvestorShield AI API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://sylvialzy.github.io",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8770",
        "http://localhost:8770",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()


class AnalysisRequest(BaseModel):
    stock: str = Field(min_length=1, max_length=30)


def set_job(job_id: str, **updates: Any) -> None:
    with jobs_lock:
        jobs[job_id].update(updates)


def universal_date_parse(date_str: Any):
    now = datetime.now()
    today = now.date()
    text = str(date_str).strip()
    try:
        if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", text) or any(x in text for x in ["分钟", "小时", "秒", "今天", "刚刚"]):
            return today
        if "昨天" in text:
            return today - timedelta(days=1)
        full = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
        if full:
            date = datetime(int(full.group(1)), int(full.group(2)), int(full.group(3))).date()
            return date if date <= today else date.replace(year=date.year - 1)
        short = re.search(r"(\d{1,2})[-月](\d{1,2})", text)
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
    options.add_argument("user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(35)
    driver.set_script_timeout(20)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    return driver


def fetch_eastmoney(driver, stock: str, deadline):
    result, seen = [], set()
    try:
        driver.get(f"https://so.eastmoney.com/news/s?keyword={stock}")
        time.sleep(3)
        try:
            WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), '默认排序')]"))
            ).click()
            time.sleep(1)
            driver.find_element(By.XPATH, "//li[contains(text(), '按时间排序')]").click()
            time.sleep(2)
        except Exception:
            pass
        keep_running = True
        for _ in range(1, 101):
            if not keep_running:
                break
            items = driver.find_elements(By.CSS_SELECTOR, ".news-item, .news_item")
            if not items:
                break
            for item in items:
                date = universal_date_parse(item.text)
                if date and date >= deadline:
                    try:
                        link = item.find_element(By.TAG_NAME, "a")
                        url = link.get_attribute("href")
                        if url and url not in seen:
                            result.append({"日期": date, "标题": link.text.strip(), "链接": url, "来源": "东方财富"})
                            seen.add(url)
                    except Exception:
                        continue
                elif date and date < deadline:
                    keep_running = False
                    break
            if keep_running:
                try:
                    next_button = driver.find_element(By.XPATH, "//a[text()='>'] | //a[contains(text(), '下一页')]")
                    driver.execute_script("arguments[0].click();", next_button)
                    time.sleep(2)
                except Exception:
                    break
    except Exception:
        return result
    return result


def fetch_sina(driver, stock: str, deadline):
    result, seen = [], set()
    try:
        driver.get(f"https://search.sina.com.cn/?q={stock}&c=news")
        time.sleep(3)
        keep_running = True
        for _ in range(1, 51):
            if not keep_running:
                break
            items = driver.find_elements(By.CSS_SELECTOR, ".box-result")
            if not items:
                break
            for item in items:
                try:
                    date = universal_date_parse(item.find_element(By.CLASS_NAME, "fgray_time").text)
                    if date and date >= deadline:
                        link = item.find_element(By.TAG_NAME, "h2").find_element(By.TAG_NAME, "a")
                        url = link.get_attribute("href")
                        if url and url not in seen:
                            result.append({"日期": date, "标题": link.text.strip(), "链接": url, "来源": "新浪财经"})
                            seen.add(url)
                    elif date and date < deadline:
                        keep_running = False
                        break
                except Exception:
                    continue
            if keep_running:
                try:
                    next_button = driver.find_element(By.XPATH, "//a[contains(text(), '下一页')]")
                    driver.execute_script("arguments[0].click();", next_button)
                    time.sleep(3)
                except Exception:
                    break
    except Exception:
        return result
    return result


def fetch_stcn(driver, stock: str, deadline):
    """采集证券时报。页面是动态加载的，限制滚动次数并用无新增轮次避免任务卡死。"""
    result, seen = [], set()
    try:
        driver.get(f"https://www.stcn.com/article/search.html?keyword={stock}")
        time.sleep(5)
        no_new_rounds = 0
        last_height = 0
        for _ in range(1, 21):
            items = driver.find_elements(
                By.CSS_SELECTOR,
                "li[class*='item'], li[class*='news'], div.item, div[class*='item'], div[class*='news'], article",
            )
            added_this_round = 0
            found_old = False
            for item in items:
                try:
                    text = item.text.strip()
                    if len(text) < 8:
                        continue
                    date = universal_date_parse(text)
                    link_nodes = item.find_elements(By.TAG_NAME, "a")
                    if not link_nodes:
                        continue
                    link = link_nodes[0]
                    url = link.get_attribute("href")
                    title = link.text.strip()
                    if not url or not title or url in seen:
                        continue
                    if date and date >= deadline and stock.lower() in title.lower():
                        result.append({"日期": date, "标题": title, "链接": url, "来源": "证券时报"})
                        seen.add(url)
                        added_this_round += 1
                    elif date and date < deadline:
                        found_old = True
                except Exception:
                    continue
            if found_old:
                break
            no_new_rounds = no_new_rounds + 1 if added_this_round == 0 else 0
            if no_new_rounds >= 3:
                break
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height and added_this_round == 0:
                break
            last_height = new_height
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
    except Exception as exc:
        print(f"证券时报采集异常: {type(exc).__name__}: {exc}")
    return result


def score_and_report(df: pd.DataFrame, stock: str):
    if not API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")
    client = OpenAI(api_key=API_KEY, base_url="https://api.deepseek.com")
    batch_size = 50
    scores: list[float] = []
    for start in range(0, len(df), batch_size):
        batch = df.iloc[start:start + batch_size]
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
    weighted_score = 0.0
    for index, (_, row) in enumerate(df.iterrows()):
        time_weight = W_TIME["T"] if row["日期"] == today else W_TIME["T1"]
        platform_weight = W_PLATFORM.get(row["来源"], 0.2)
        weighted_score += scores[index] * time_weight * platform_weight
    sample = "\n".join(f"- {title}" for title in df["标题"].head(30))
    final_prompt = f"""作为资深证券分析师，基于对全部 {len(df)} 条新闻的打分分析（总体信心分：{weighted_score:.4f}），请撰写深度研报。必须返回 JSON，字段为 summary、pros、cons、strategy、decision。summary 不少于200字；pros 和 cons 各3条；strategy 不少于150字；decision 包含评级和理由。股票：{stock}。参考样本：\n{sample}"""
    final_response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": final_prompt}],
        response_format={"type": "json_object"},
        max_tokens=4000,
    )
    report = json.loads(final_response.choices[0].message.content)
    return report, weighted_score, scores


def normalize_report(report: dict[str, Any], score: float, df: pd.DataFrame, scores: list[float]):
    decision = report.get("decision", "谨慎观察")
    if isinstance(decision, dict):
        rating = decision.get("rating", "谨慎观察")
        reason = decision.get("reason", "")
    else:
        rating = str(decision)
        reason = str(decision)
    direction = max(-1.0, min(1.0, score / max(1.0, len(df) * 0.5)))
    counts = {source: int((df["来源"] == source).sum()) for source in SOURCE_NAMES}
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
        "model_info": {
            "normalization": "按时间权重和平台权重汇总，再归一化为方向分。",
            "cleaning": "保留标题中包含目标股票名称的新闻，并按链接去重。",
            "notice": "结果用于资讯整理和研究展示，不构成投资建议。",
        },
    }


def run_analysis(job_id: str, stock: str):
    driver = None
    warnings = []
    try:
        deadline = datetime.now().date() - timedelta(days=1)
        set_job(job_id, progress=8, message="正在启动新闻采集", source_status={name: {"status": "waiting", "message": "等待中"} for name in SOURCE_NAMES})
        driver = get_stealth_driver()
        raw_data = []
        for index, (source, fetcher) in enumerate([
            ("东方财富", fetch_eastmoney),
            ("新浪财经", fetch_sina),
            ("证券时报", fetch_stcn),
        ]):
            status = {name: {"status": "waiting", "message": "等待中"} for name in SOURCE_NAMES}
            for name in SOURCE_NAMES[:index]:
                status[name] = {"status": "completed", "message": "已完成"}
            status[source] = {"status": "running", "message": "正在采集"}
            set_job(job_id, progress=15 + index * 22, message=f"正在采集 {source} 新闻", source_status=status, warnings=warnings)
            source_data = fetcher(driver, stock, deadline)
            raw_data.extend(source_data)
            if not source_data:
                warnings.append(f"{source} 本次未抓到有效新闻，继续使用其他来源。")
            status[source] = {"status": "completed" if source_data else "failed", "message": f"已抓取 {len(source_data)} 条" if source_data else "未抓到结果"}
            set_job(job_id, source_status=status, warnings=warnings)
        if not raw_data:
            raise RuntimeError("未抓取到有效新闻，可能是网站结构变化或浏览器环境不可用")
        df = pd.DataFrame(raw_data).drop_duplicates(subset=["链接"])
        df = df[df["标题"].str.contains(stock, case=False, na=False)].copy()
        if df.empty:
            raise RuntimeError("未筛选到与股票名称匹配的新闻")
        set_job(job_id, progress=78, message=f"正在对 {len(df)} 条新闻进行 AI 分析", source_status={name: {"status": "completed", "message": "已完成"} for name in SOURCE_NAMES})
        report, score, scores = score_and_report(df, stock)
        result = normalize_report(report, score, df, scores)
        set_job(job_id, status="completed", progress=100, message="分析完成", result={"stock": stock, **result})
    except Exception as exc:
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
