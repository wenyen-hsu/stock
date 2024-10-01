import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import random
from typing import Dict, List, Tuple, Optional
from tqdm import tqdm
import logging
import sys
import os
import openai
import json
from bs4 import BeautifulSoup
import backoff

# 設置日誌
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 設置 OpenAI API key
openai.api_key = os.getenv("OPENAI_API_KEY")

session = requests.Session()
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.twse.com.tw/en/page/trading/fund/T86.html',
    'X-Requested-With': 'XMLHttpRequest'
}
session.headers.update(headers)

def get_valid_date(days_back: int = 0) -> str:
    current_date = datetime.now().date() - timedelta(days=days_back)
    while current_date.weekday() >= 5:  # Saturday = 5, Sunday = 6
        current_date -= timedelta(days=1)
    return current_date.strftime('%Y%m%d')

def get_stock_info(stock_id: str, max_retries: int = 5) -> Tuple[str, str, float]:
    for days_back in range(30):  # Try up to 30 days back
        date = get_valid_date(days_back)
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={date}&stockNo={stock_id}&response=json"
        
        for attempt in range(max_retries):
            try:
                logging.info(f"正在獲取股票 {stock_id} 的信息 (嘗試 {attempt + 1}，日期 {date})")
                response = session.get(url, timeout=60)
                data = response.json()
                if data['stat'] == 'OK':
                    full_name = data['title']
                    parts = full_name.split()
                    if len(parts) >= 2:
                        code, name = parts[1], ' '.join(parts[2:])
                        name = name.replace('各日成交資訊', '').strip()
                        
                        # 獲取最後一個交易日的收盤價
                        last_close = float(data['data'][-1][6].replace(',', ''))
                        
                        logging.info(f"成功獲取股票 {stock_id} 的信息: {code} {name}, 最後收盤價: {last_close}")
                        return code, name, last_close
                else:
                    logging.warning(f"無法獲取股票 {stock_id} 的信息: {data['stat']}")
            except Exception as e:
                logging.error(f"獲取股票 {stock_id} 信息時發生錯誤 (嘗試 {attempt + 1}): {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(random.uniform(5, 10))
    return stock_id, "未知", 0.0

@backoff.on_exception(backoff.expo, requests.exceptions.RequestException, max_tries=5)
def fetch_url(url: str) -> requests.Response:
    logging.info(f"正在請求 URL: {url}")
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response

def fetch_stock_data(date: str, cache: Dict[str, Dict[str, Tuple[int, int]]]) -> Optional[Dict[str, Tuple[int, int]]]:
    if date in cache:
        logging.info(f"使用 {date} 的緩存數據")
        return cache[date]

    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={date}&selectType=ALL&response=json"
    max_retries = 10
    for attempt in range(max_retries):
        try:
            logging.info(f"正在獲取 {date} 的股票數據 (嘗試 {attempt + 1})")
            response = session.get(url, timeout=60)
            data = response.json()
            
            if data['stat'] == 'OK':
                df = pd.DataFrame(data['data'], columns=data['fields'])
                df['證券代號'] = df['證券代號'].astype(str)
                cache[date] = {row['證券代號']: (
                    int(row['外陸資買進股數(不含外資自營商)'].replace(',', '')),
                    int(row['外陸資賣出股數(不含外資自營商)'].replace(',', ''))
                ) for _, row in df.iterrows()}
                logging.info(f"成功獲取 {date} 的股票數據")
                return cache[date]
            elif '很抱歉' in data['stat']:
                logging.info(f"{date} 無可用數據")
                return None
        except Exception as e:
            logging.error(f"獲取 {date} 的股票數據時發生錯誤 (嘗試 {attempt + 1}): {str(e)}")
        if attempt < max_retries - 1:
            time.sleep(random.uniform(10, 20))
    return None

def get_stock_price_change(stock_id: str, days: int = 10) -> float:
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=days+5)
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={end_date.strftime('%Y%m%d')}&stockNo={stock_id}&response=json"
    
    max_retries = 5
    for attempt in range(max_retries):
        try:
            logging.info(f"正在獲取股票 {stock_id} 的價格變化 (嘗試 {attempt + 1})")
            response = session.get(url, timeout=60)
            data = response.json()
            if data['stat'] == 'OK':
                df = pd.DataFrame(data['data'], columns=data['fields'])
                df['日期'] = df['日期'].apply(lambda x: f"{int(x.split('/')[0])+1911}/{x.split('/')[1]}/{x.split('/')[2]}")
                df['收盤價'] = df['收盤價'].str.replace(',', '').astype(float)
                
                df = df[df['日期'] >= start_date.strftime('%Y/%m/%d')]
                if len(df) >= 2:
                    start_price = df.iloc[-min(len(df), days)]['收盤價']
                    end_price = df.iloc[-1]['收盤價']
                    change = (end_price - start_price) / start_price * 100
                    logging.info(f"成功計算股票 {stock_id} 的價格變化: {change:.2f}%")
                    return change
                else:
                    logging.warning(f"股票 {stock_id} 的數據不足以計算價格變化")
            else:
                logging.warning(f"無法獲取股票 {stock_id} 的價格數據: {data['stat']}")
        except Exception as e:
            logging.error(f"計算股票 {stock_id} 的價格變化時發生錯誤 (嘗試 {attempt + 1}): {str(e)}")
        if attempt < max_retries - 1:
            time.sleep(random.uniform(5, 10))
    return 0.0

def get_top_stocks(num_stocks: int = 10, num_days: int = 10) -> Tuple[List[Tuple[str, int, int, List[str]]], datetime, datetime]:
    stock_buys = {}
    stock_sells = {}
    failed_dates = {}
    end_date = datetime.now().date() - timedelta(days=1)
    start_date = end_date  # 初始化開始日期
    cache = {}
    days_with_data = 0
    current_date = end_date
    
    pbar = tqdm(total=num_days, desc="正在獲取股票數據")
    
    while days_with_data < num_days and (end_date - current_date).days < 30:
        date_str = get_valid_date((end_date - current_date).days)
        logging.info(f"正在處理日期: {date_str}")
        data = fetch_stock_data(date_str, cache)
        if data is not None:
            for stock_id, (buy_amount, sell_amount) in data.items():
                stock_buys[stock_id] = stock_buys.get(stock_id, 0) + buy_amount
                stock_sells[stock_id] = stock_sells.get(stock_id, 0) + sell_amount
                if stock_id not in failed_dates:
                    failed_dates[stock_id] = []
            days_with_data += 1
            start_date = current_date  # 更新開始日期為最後一個有數據的日期
            pbar.update(1)
            logging.info(f"已處理 {date_str} 的數據。總計有數據的天數: {days_with_data}")
        else:
            logging.info(f"{date_str} 無可用數據")
            for stock_id in set(stock_buys.keys()) | set(stock_sells.keys()):
                if stock_id not in failed_dates:
                    failed_dates[stock_id] = []
                failed_dates[stock_id].append(current_date.strftime('%Y/%m/%d'))
        current_date -= timedelta(days=1)
        time.sleep(random.uniform(5, 10))
    
    pbar.close()
    logging.info(f"處理完成。總計有數據的天數: {days_with_data}")
    
    if not stock_buys:
        logging.warning("未能獲取任何股票數據")
        return [], start_date, end_date
    
    all_stock_ids = set(stock_buys.keys()) | set(stock_sells.keys())
    
    sorted_stocks = sorted(
        [(stock_id, 
          stock_buys.get(stock_id, 0), 
          stock_sells.get(stock_id, 0), 
          failed_dates.get(stock_id, []))
         for stock_id in all_stock_ids],
        key=lambda x: x[1],
        reverse=True
    )[:num_stocks]
    
    return sorted_stocks, start_date, end_date

def analyze_stock_with_openai(stock_code: str, stock_name: str) -> str:
    prompt = f"請分析台灣股票 {stock_code} ({stock_name}) 的前景。請提供簡潔的分析，包括公司的主要業務、市場地位、未來成長潛力，以及可能影響公司表現的主要風險因素。"
    
    try:
        logging.info(f"開始使用 OpenAI 分析股票 {stock_code}")
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a financial analyst specializing in Taiwan stock market."},
                {"role": "user", "content": prompt}
            ]
        )
        analysis = response.choices[0].message['content']
        logging.info(f"成功獲取 OpenAI 對股票 {stock_code} 的分析")
        return analysis.strip()
    except Exception as e:
        logging.error(f"使用 OpenAI 分析股票 {stock_code} 時發生錯誤: {str(e)}")
        return f"無法獲取分析結果。錯誤: {str(e)}"

def fetch_recent_news(stock_code: str, stock_name: str) -> List[str]:
    url = f"https://news.google.com/rss/search?q={stock_code}+OR+{stock_name}+site:cnyes.com+OR+site:money.udn.com+when:1m&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'xml')
        items = soup.find_all('item')
        
        news = []
        for item in items[:5]:  # 只取前5條新聞
            title = item.title.text
            link = item.link.text
            news.append(f"{title} ({link})")
        
        return news
    except Exception as e:
        logging.error(f"獲取股票 {stock_code} 的新聞時發生錯誤: {str(e)}")
        return []

def analyze_news_with_openai(stock_code: str, stock_name: str, news: List[str]) -> str:
    if not news:
        return "無法獲取相關新聞。"
    
    news_text = "\n".join(news)
    prompt = f"以下是關於台灣股票 {stock_code} ({stock_name}) 的最近新聞：\n\n{news_text}\n\n請根據這些新聞，簡要分析這支股票最近的表現和可能的未來趨勢。"
    
    try:
        logging.info(f"開始使用 OpenAI 分析股票 {stock_code} 的新聞")
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a financial analyst specializing in Taiwan stock market."},
                {"role": "user", "content": prompt}
            ]
        )
        analysis = response.choices[0].message['content']
        logging.info(f"成功獲取 OpenAI 對股票 {stock_code} 新聞的分析")
        return analysis.strip()
    except Exception as e:
        logging.error(f"使用 OpenAI 分析股票 {stock_code} 的新聞時發生錯誤: {str(e)}")
        return f"無法獲取新聞分析結果。錯誤: {str(e)}"

if __name__ == "__main__":
    try:
        logging.info("程序開始運行")
        top_stocks, start_date, end_date = get_top_stocks(num_stocks=10, num_days=3)
        
        if not top_stocks:
            print("未能獲取任何股票數據。程序結束。")
            sys.exit(0)
        
        print("\n正在處理個股資訊...")
        results = []
        problem_stocks = []
        for rank, (stock_id, buy_amount, sell_amount, failed_dates) in enumerate(top_stocks, 1):
            try:
                print(f"正在處理第 {rank} 名股票 (股票代碼: {stock_id})...")
                stock_code, stock_name, last_close = get_stock_info(stock_id)
                price_change = get_stock_price_change(stock_id)
                net_buy = buy_amount - sell_amount
                failed_dates_str = ', '.join(failed_dates) if failed_dates else 'None'
                
                # 使用 OpenAI 進行分析
                analysis = analyze_stock_with_openai(stock_code, stock_name)
                
                # 獲取並分析最近新聞
                recent_news = fetch_recent_news(stock_code, stock_name)
                news_analysis = analyze_news_with_openai(stock_code, stock_name, recent_news)
                
                results.append((rank, stock_code, stock_name, buy_amount, sell_amount, net_buy, price_change, last_close, failed_dates_str, analysis, news_analysis, recent_news))
            except Exception as e:
                logging.error(f"處理股票 {stock_id} 時發生錯誤: {str(e)}")
                problem_stocks.append((stock_id, str(e)))
                continue
        
        if not results:
            print("無法獲取任何有效的股票數據。程序結束。")
            sys.exit(0)
        
        print(f"\n近 3 日外資買進前10名 (資料區間 {start_date.strftime('%Y-%m-%d')} 至 {end_date.strftime('%Y-%m-%d')}):")
        print(f"{'排名':^4}{'股票代碼':^6}{'股票名稱':^10}{'買進股數':>12}{'賣出股數':>12}{'淨買入':>12}{'漲幅':>8}{'收盤價':>8}{'抓取失敗日期':^20}")
        print("-" * 100)
        
        for rank, stock_code, stock_name, buy_amount, sell_amount, net_buy, price_change, last_close, failed_dates, analysis, news_analysis, recent_news in results:
            print(f"{rank:^4}{stock_code:^6}{stock_name:^10}{buy_amount:>12,}{sell_amount:>12,}{net_buy:>12,}{price_change:>7.2f}%{last_close:>8.2f} {failed_dates:^20}")
            print(f"\nOpenAI 分析:\n{analysis}\n")
            print(f"最近新聞:\n" + "\n".join(recent_news) + "\n")
            print(f"新聞分析:\n{news_analysis}\n")
            print("-" * 100)
        
        if problem_stocks:
            print("\n無法獲取資料的股票:")
            for stock_id, error in problem_stocks:
                print(f"股票代碼: {stock_id}, 錯誤: {error}")
        
        logging.info("程序成功完成")
    except KeyboardInterrupt:
        print("\n程序被用戶中斷。正在優雅退出...")
        logging.info("程序被用戶中斷")
        sys.exit(0)
    except Exception as e:
        print(f"\n發生錯誤: {str(e)}")
        logging.error(f"發生錯誤: {str(e)}", exc_info=True)
        sys.exit(1)