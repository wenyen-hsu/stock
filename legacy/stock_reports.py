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
from io import StringIO
import time
import cchardet

# Load environment variables
load_dotenv()

# Set up OpenAI API key
openai.api_key = os.getenv("OPENAI_API_KEY")

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

def analyze_financial_data(symbol, analysis, financial_data, news_data):
    prompt = f"""作為一名金融分析師，請對以下 {symbol} 的財務數據和新聞進行簡要分析：

財務數據：
{financial_data} {analysis}

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

def monthly_report(year, month):
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
        #print("表格列名:", df.columns)

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
        df = monthly_report(year, month)

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
    # 确保 symbol 是股票代号字符串，而不是 yfinance.Ticker 对象
    if symbol.isdigit() and len(symbol) == 4:
        symbol += '.TW'  # 拼接完整的股票代码
    
    stock = yf.Ticker(symbol)  # 获取 yfinance 对象
    
    # 获取股票基本信息
    stock_info = stock.info
    if 'longName' in stock_info:
        company_name = stock_info['longName']
        print(f"正在獲取 {symbol} ({company_name}) 的新聞...")
    else:
        print(f"找不到代號 {symbol} 的股票名稱")
        company_name = symbol.split(".")[0]  # 只使用股票代号的数字部分

    # 获取财务数据
    income_statement = stock.financials
    balance_sheet = stock.balance_sheet
    cash_flow = stock.cashflow

    # 使用正确的股票代码调用 get_monthly_revenue
    monthly_revenue = get_monthly_revenue(symbol.split(".")[0])  # 只传递股票代码数字部分 '2888'

    # 打印财务报表（用于调试）
    print(f"\n{symbol} 的财务报表和分析：")

    print("\n損益表（最近幾年）：")
    print(format_financial_data(income_statement))
    
    print("\n資產負債表（最近幾年）：")
    print(format_financial_data(balance_sheet))
    
    print("\n現金流量表（最近幾年）：")
    print(format_financial_data(cash_flow))
    
    # 打印月营收表格
    print("\n月營收（最近12個月）表格：")
    print(f"{'月份':<10} {'營收 (元)':<15}")
    print("-" * 25)
    for month, revenue in monthly_revenue.items():
        print(f"{month:<10} {revenue:<15}")
    print("-" * 25)
    
    # 返回分析结果
    return income_statement, balance_sheet, cash_flow, monthly_revenue, company_name

def chat_with_ai(analysis, financial_summary, news_summary):
    conversation_history = [
        {"role": "system", "content": "你是一位專業的金融分析師，對財務報表、市場趨勢和投資策略有深入的了解。請用中文回答用戶的問題。"},
        {"role": "user", "content": f"以下是股票的財務分析、數據摘要和新聞摘要：\n\n分析：{analysis}\n\n數據摘要：{financial_summary}\n\n新聞摘要：{news_summary}\n\n請根據這些信息先做投資分析並且回答我的問題。"},
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
# 主程序
if __name__ == "__main__":
    while True:
        symbol = input("請輸入股票代碼（或輸入'q'退出程序）：").upper()
        if symbol == 'Q':
            break
        
        try:
            # 获取财务数据和月营收数据
            income_statement, balance_sheet, cash_flow, monthly_revenue, company_name = get_stock_reports(symbol)
            
            # 构建分析摘要，将四个返回值都包括在分析中
            analysis = f"""
            {symbol} 的财务分析：

            損益表（最近幾年）：
            {format_financial_data(income_statement)}
            
            資產負債表（最近幾年）：
            {format_financial_data(balance_sheet)}
            
            現金流量表（最近幾年）：
            {format_financial_data(cash_flow)}
            
            月營收（最近12個月）：
            """
            for month, revenue in monthly_revenue.items():
                analysis += f"{month}: {revenue}\n"

            # 构建财务摘要
            financial_summary = f"""
            股票代碼: {symbol}
            
            損益表（最近年度）：
            {format_financial_data(income_statement.iloc[:, 0])}

            資產負債表（最近年度）：
            {format_financial_data(balance_sheet.iloc[:, 0])}
            
            現金流量表（最近年度）：
            {format_financial_data(cash_flow.iloc[:, 0])}
            
            月營收（最近12個月）： 
            """
            for month, revenue in monthly_revenue.items():
                financial_summary += f"{month}: {revenue}\n"

            # 获取新闻摘要，确认新闻获取成功
            news_summary = fetch_recent_news(symbol.split(".")[0], company_name)

            # 打印新闻摘要以确认正确获取
            print(f"新聞摘要：\n{news_summary}\n")
            
            if not news_summary:
                print(f"無法獲取股票 {symbol} 的新聞。")
            
            ai_analysis = analyze_financial_data(symbol, analysis, financial_summary, "\n".join(news_summary))

            # **打印 AI 分析结果**
            print(f"\nAI分析結果：\n{ai_analysis}")

            # 将构建的分析、财务摘要、新闻摘要传递给 AI 进行对话
            new_stock = chat_with_ai(analysis, financial_summary, "\n".join(news_summary))
            
            if not new_stock:
                print("\n" + "=" * 50 + "\n")

        except Exception as e:
            print(f"獲取股票數據時發生錯誤：{e}")
            print("\n" + "=" * 50 + "\n")

    print("感謝您使用高級股票分析程序！")

