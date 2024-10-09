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
import re
from io import StringIO

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

def get_company_name(stock_code):
    stock_code = stock_code.split(".")[0]  # 提取数字部分 '2888'
    url = f"https://www.twse.com.tw/zh/api/codeQuery?query={stock_code}"
    response = requests.get(url)
    
    try:
        data = json.loads(response.text)
        if data['suggestions'] and '無符合' not in data['suggestions'][0]:
            company_info = data['suggestions'][0]
            company_name = company_info.split('\t')[1]  # 提取公司名称
            return company_name
        else:
            print(f"找不到代號 {stock_code} 的股票名稱")
            return None
    except Exception as e:
        print(f"獲取股票名稱時發生錯誤: {str(e)}")
        return None

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

def monthly_report(year, month, name_use):
    if year > 1990:
        year -= 1911

    url = f'https://mops.twse.com.tw/nas/t21/sii/t21sc03_{year}_{month}_0.html'
    if year <= 98:
        url = f'https://mops.twse.com.tw/nas/t21/sii/t21sc03_{year}_{month}.html'

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}

    r = requests.get(url, headers=headers)
    r.encoding = 'big5'

    try:
        # 默认值为空的 DataFrame
        dfs = pd.read_html(StringIO(r.text), encoding='big5')
        # 如果找不到表格数据，直接返回空的 DataFrame
        if not dfs:
            print(f"未找到表格数据: {url}")
            return pd.DataFrame()
    except Exception as e:
        print(f"解析 HTML 数据时出错: {str(e)}")
        return pd.DataFrame()  # 出错时返回空的 DataFrame

    # 合并并选择合适的表格
    try:
        df = pd.concat([df for df in dfs if df.shape[1] <= 11 and df.shape[1] > 5], ignore_index=True)

        # 处理多重索引
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(1)  # 简化多重索引
 

        # 打印列名以调试
        print("表格列名:", df.columns)

        if '公司 代號' not in df.columns:
            print("未找到 '公司 代號' 列")
            return pd.DataFrame()

        if '當月營收' not in df.columns:
            print("未找到 '當月營收' 列")
            return pd.DataFrame()

        df['當月營收'] = pd.to_numeric(df['當月營收'], errors='coerce')
        df = df[~df['當月營收'].isnull()]
        df = df[df['公司 代號'] != '合計']  # 移除汇总行
        return df

    except Exception as e:
        print(f"合并表格时出错: {str(e)}")
        return pd.DataFrame()  # 出错时返回空的 DataFrame

def get_monthly_revenue(stock_code):
    # 使用正确的公司名称
    company_name = get_company_name(stock_code)
    if company_name:
        name_use = company_name.strip()  # 去掉多余的空格
        print(f"使用公司名稱進行查找: {name_use}")
    else:
        # 如果公司名称不可用，使用股票代码
        name_use = stock_code.split(".")[0]  # 使用股票代号
        print(f"找不到公司名稱，使用股票代號進行查找: {name_use}")

    # 檢查是否為 ETF 或槓桿產品
    if "ETF" in name_use or "正2" in name_use or "反1" in name_use:
        print(f"{name_use} 可能是 ETF 或槓桿產品，不提供月營收數據")
        return pd.Series()  # 返回空的 Series

    now = datetime.now()
    revenues = []
    
    # 获取过去12个月的月营收数据
    for i in range(12):
        date = now - timedelta(days=30 * i)
        year, month = date.year, date.month - 1
        if month == 0:  # 如果月份为0，调整为前一年的12月
            month = 12
            year -= 1

        #print(f"查询年份: {year}, 月份: {month}")
        df = monthly_report(year, month, name_use)

        if df.empty:
            print(f"月份 {year}/{month} 的数据为空")
            continue

        # 使用包含匹配而不是精确匹配
        revenue = df[df['公司名稱'].str.contains(name_use, na=False, regex=False)]['當月營收'].values
        if len(revenue) > 0:
            revenues.append((f"{year}/{month}", revenue[0]))
        else:
            print(f"找不到名稱 {name_use} 的營收數據")
        
        if len(revenues) >= 12:
            break
    
    # 打印查询到的营收数据
    #if revenues:
    #    print(f"找到的營收數據: {revenues}")
    #else:
    #    print(f"未找到股票代號 {stock_code} 的任何月營收數據")
    
    return pd.Series(dict(revenues[::-1]))

def get_financial_statement(stock_id, year, season):
    url = "https://mops.twse.com.tw/mops/web/ajax_t164sb03"
    form_data = {
        'encodeURIComponent': '1',
        'step': '1',
        'firstin': '1',
        'off': '1',
        'keyword4': '',
        'code1': '',
        'TYPEK2': '',
        'checkbtn': '',
        'queryName': 'co_id',
        'inpuType': 'co_id',
        'TYPEK': 'all',
        'isnew': 'false',
        'co_id': stock_id,
        'year': str(year),
        'season': str(season),
    }
    
    response = requests.post(url, data=form_data)
    soup = BeautifulSoup(response.text, 'html.parser')
    
    tables = soup.find_all('table', class_='hasBorder')
    
    financial_data = {}
    for table in tables:
        rows = table.find_all('tr')
        for row in rows:
            cols = row.find_all('td')
            if len(cols) >= 2:
                key = cols[0].text.strip()
                value = cols[1].text.strip()
                financial_data[key] = value
    
    return financial_data

def analyze_financial_data_with_openai(stock_code: str, stock_name: str, financial_data: Dict, monthly_revenue: pd.Series) -> str:
    prompt = f"""
    請分析台灣股票 {stock_code} ({stock_name}) 的財務數據和月營收:

    財務數據:
    {json.dumps(financial_data, ensure_ascii=False, indent=2)}

    月營收(最近12個月):
    {monthly_revenue.to_string()}

    請提供以下分析:
    1. 公司財務健康狀況
    2. 收入和利潤趨勢
    3. 月營收趨勢分析
    4. 潛在風險和機會
    5. 對投資者的建議

    請以簡潔清晰的方式提供分析。
    """

    try:
        logging.info(f"開始使用 OpenAI 分析股票 {stock_code} 的財務數據和月營收")
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are a financial analyst specializing in Taiwan stock market."},
                {"role": "user", "content": prompt}
            ]
        )
        analysis = response.choices[0].message['content']
        logging.info(f"成功獲取 OpenAI 對股票 {stock_code} 財務數據和月營收的分析")
        return analysis.strip()
    except Exception as e:
        logging.error(f"使用 OpenAI 分析股票 {stock_code} 的財務數據和月營收時發生錯誤: {str(e)}")
        return f"無法獲取財務數據和月營收分析結果。錯誤: {str(e)}"

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
            model="gpt-4",
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
            model="gpt-4",
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
                
                # 獲取財務報表
                current_year = datetime.now().year - 1911  # 轉換為民國年
                current_season = (datetime.now().month - 1) // 3 + 1
                financial_statement = get_financial_statement(stock_id, current_year, current_season)
                
                # 獲取月營收
                print(f"{stock_id} and {stock_code}")
                monthly_revenue = get_monthly_revenue(stock_id)
                if monthly_revenue.empty:
                    print(f"無法獲取 {stock_name} 的月營收數據，可能是 ETF 或其他特殊金融產品")
                    # 這裡可以選擇跳過後續的財務分析，或者使用其他指標

                # 使用 OpenAI 進行分析
                stock_analysis = analyze_stock_with_openai(stock_code, stock_name)
                financial_analysis = analyze_financial_data_with_openai(stock_code, stock_name, financial_statement, monthly_revenue)
                
                # 獲取並分析最近新聞
                recent_news = fetch_recent_news(stock_code, stock_name)
                news_analysis = analyze_news_with_openai(stock_code, stock_name, recent_news)
                
                results.append((rank, stock_code, stock_name, buy_amount, sell_amount, net_buy, price_change, last_close, 
                                failed_dates_str, stock_analysis, financial_analysis, news_analysis, recent_news, financial_statement, monthly_revenue))
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
        
        for rank, stock_code, stock_name, buy_amount, sell_amount, net_buy, price_change, last_close, failed_dates, stock_analysis, financial_analysis, news_analysis, recent_news, financial_statement, monthly_revenue in results:
            print(f"{rank:^4}{stock_code:^6}{stock_name:^10}{buy_amount:>12,}{sell_amount:>12,}{net_buy:>12,}{price_change:>7.2f}%{last_close:>8.2f} {failed_dates:^20}")
            print(f"\n股票分析:\n{stock_analysis}\n")
            print(f"財務數據分析:\n{financial_analysis}\n")
            print(f"最近新聞:\n" + "\n".join(recent_news) + "\n")
            print(f"新聞分析:\n{news_analysis}\n")
            print(f"財務報表摘要:")
            for key, value in financial_statement.items():
                print(f"{key}: {value}")
            print("\n月營收（最近12個月）：")
            print(monthly_revenue)
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