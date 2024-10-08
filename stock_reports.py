import yfinance as yf
import pandas as pd
import os
import openai
from dotenv import load_dotenv
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
import json
import re

# Load environment variables
load_dotenv()

# Set up OpenAI API key
openai.api_key = os.getenv("OPENAI_API_KEY")

def get_company_name(stock_code):
    url = f"https://www.twse.com.tw/zh/api/codeQuery?query={stock_code}"
    response = requests.get(url)
    data = json.loads(response.text)
    if data['suggestions']:
        return data['suggestions'][0].split('\t')[1]
    return None

def analyze_financial_data(symbol, financial_data, news_data):
    prompt = f"""作為一名金融分析師，請對以下 {symbol} 的財務數據和新聞進行簡要分析：

財務數據：
{financial_data}

近期新聞：
{news_data}

請包括：
1. 公司財務健康狀況概述
2. 關鍵財務比率及其解釋
3. 收入、利潤率和現金流的趨勢
4. 月營收趨勢分析
5. 近期新聞對公司可能的影響
6. 潛在風險和機會
7. 對投資者的建議

請以清晰、簡潔的方式提供分析，適合專業投資者和金融分析新手閱讀。"""

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "你是一位專業的金融分析師，對財務報表、市場趨勢和投資策略有深入的了解。請用中文回答。"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=1500
        )
        return response.choices[0].message['content'].strip()
    except Exception as e:
        return f"生成分析時發生錯誤：{str(e)}"

def format_financial_data(df):
    return df.head().to_string()  # Only return the first few rows

def get_monthly_revenue(stock):
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365)
    revenue_data = stock.history(start=start_date, end=end_date, interval="1mo")
    monthly_revenue = revenue_data['Close'] * revenue_data['Volume']
    return monthly_revenue.sort_index(ascending=False)

def fetch_recent_news(stock_code: str, stock_name: str) -> list:
    company_name = get_company_name(stock_code)
    if company_name:
        search_query = f"{stock_code} OR {company_name}"
    else:
        search_query = f"{stock_code} OR {stock_name}"
    
    url = f"https://news.google.com/rss/search?q={search_query}+site:cnyes.com+OR+site:money.udn.com+when:7d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    
    try:
        print(f"正在從以下 URL 獲取新聞: {url}")  # Debug information
        response = requests.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'xml')
        items = soup.find_all('item')
        
        news = []
        for item in items:
            title = item.title.text
            link = item.link.text
            # Check if the title contains the stock code, company name, or stock name
            if re.search(f"{stock_code}|{company_name}|{stock_name}", title, re.IGNORECASE):
                news.append(f"{title} ({link})")
            if len(news) == 5:  # Only get 5 relevant news items
                break
        
        print(f"成功獲取 {len(news)} 條相關新聞")  # Debug information
        return news
    except Exception as e:
        print(f"獲取股票 {stock_code} 的新聞時發生錯誤: {str(e)}")
        return []

def get_stock_reports(symbol):
    if symbol.isdigit() and len(symbol) == 4:
        symbol += '.TW'
    
    stock = yf.Ticker(symbol)
    
    # Fetch financial data
    income_statement = stock.financials
    balance_sheet = stock.balance_sheet
    cash_flow = stock.cashflow
    monthly_revenue = get_monthly_revenue(stock)
    
    # Fetch recent news
    stock_name = stock.info.get('longName', '')
    print(f"正在獲取 {symbol} ({stock_name}) 的新聞...")  # Debug information
    recent_news = fetch_recent_news(symbol.replace('.TW', ''), stock_name)
    
    # Additional metrics
    key_stats = {
        "市值": stock.info.get('marketCap'),
        "本益比": stock.info.get('trailingPE'),
        "股息收益率": stock.info.get('dividendYield'),
        "52週最高價": stock.info.get('fiftyTwoWeekHigh'),
        "52週最低價": stock.info.get('fiftyTwoWeekLow'),
        "股東權益報酬率": stock.info.get('returnOnEquity'),
        "負債權益比": stock.info.get('debtToEquity')
    }
    
    print(f"\n{symbol} 的財務報告和分析：")
    
    print("\n關鍵統計數據：")
    for key, value in key_stats.items():
        print(f"{key}: {value}")
    
    print("\n損益表（最近幾年）：")
    print(format_financial_data(income_statement))
    
    print("\n資產負債表（最近幾年）：")
    print(format_financial_data(balance_sheet))
    
    print("\n現金流量表（最近幾年）：")
    print(format_financial_data(cash_flow))
    
    print("\n月營收（最近12個月）：")
    print(monthly_revenue)
    
    print(f"\n近期新聞（共 {len(recent_news)} 條）：")
    if recent_news:
        for news_item in recent_news:
            print(news_item)
    else:
        print("無法獲取相關新聞。")
    
    # Prepare data for analysis (reduced version)
    financial_summary = f"""
    股票代碼: {symbol}
    
    關鍵統計數據:
    {pd.DataFrame([key_stats]).transpose().to_string()}
    
    損益表（最近年度）:
    {format_financial_data(income_statement.iloc[:, 0])}
    
    資產負債表（最近年度）:
    {format_financial_data(balance_sheet.iloc[:, 0])}
    
    現金流量表（最近年度）:
    {format_financial_data(cash_flow.iloc[:, 0])}
    
    月營收（最近12個月）:
    {monthly_revenue.to_string()}
    """
    
    news_summary = "\n".join(recent_news) if recent_news else "無法獲取相關新聞。"
    
    print("\n正在生成AI分析...")
    analysis = analyze_financial_data(symbol, financial_summary, news_summary)
    
    print("\nAI財務分析：")
    print(analysis)
    
    print("\n數據來源：Yahoo Finance（通過yfinance庫）和 Google News")
    
    return analysis, financial_summary, news_summary

def chat_with_ai(analysis, financial_summary, news_summary):
    conversation_history = [
        {"role": "system", "content": "你是一位專業的金融分析師，對財務報表、市場趨勢和投資策略有深入的了解。請用中文回答用戶的問題。"},
        {"role": "user", "content": f"以下是股票的財務分析、數據摘要和新聞摘要：\n\n分析：{analysis}\n\n數據摘要：{financial_summary}\n\n新聞摘要：{news_summary}\n\n請根據這些信息回答我的問題。"},
        {"role": "assistant", "content": "好的，我已經理解了這支股票的財務分析、數據摘要和新聞摘要。請問您有什麼具體的問題想問嗎？我可以為您提供更詳細的解釋或分析特定方面的信息。"}
    ]

    while True:
        user_input = input("\n請輸入您的問題（輸入'q'退出當前股票分析，輸入'n'分析新的股票）：")
        if user_input.lower() == 'q':
            break
        elif user_input.lower() == 'n':
            return True

        conversation_history.append({"role": "user", "content": user_input})

        try:
            response = openai.ChatCompletion.create(
                model="gpt-4",
                messages=conversation_history,
                max_tokens=1000
            )
            ai_response = response.choices[0].message['content'].strip()
            print(f"\nAI回答：\n{ai_response}")
            conversation_history.append({"role": "assistant", "content": ai_response})
        except Exception as e:
            print(f"生成回答時發生錯誤：{str(e)}")
    
    return False

# 主程序
if __name__ == "__main__":
    while True:
        symbol = input("請輸入股票代碼（或輸入'q'退出程序）：").upper()
        if symbol == 'Q':
            break
        
        try:
            analysis, financial_summary, news_summary = get_stock_reports(symbol)
            new_stock = chat_with_ai(analysis, financial_summary, news_summary)
            if not new_stock:
                print("\n" + "="*50 + "\n")
        except Exception as e:
            print(f"獲取股票數據時發生錯誤：{e}")
            print("\n" + "="*50 + "\n")

    print("感謝您使用高級股票分析程序！")