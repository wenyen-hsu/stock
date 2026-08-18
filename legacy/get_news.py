import requests
from bs4 import BeautifulSoup
import datetime
import time
import openai
import os
from urllib.parse import quote

# 設置 OpenAI API key
openai.api_key = os.getenv("OPENAI_API_KEY")

def get_news(query, days=30):
    encoded_query = quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}+when:30d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    response = requests.get(url)
    soup = BeautifulSoup(response.content, features='xml')
    
    articles = []
    for item in soup.findAll('item'):
        pub_date = datetime.datetime.strptime(item.pubDate.text, '%a, %d %b %Y %H:%M:%S %Z')
        if (datetime.datetime.now() - pub_date).days <= days:
            articles.append({
                'title': item.title.text,
                'link': item.link.text,
                'pubDate': pub_date,
                'description': item.description.text
            })
    
    return articles

def analyze_news(query, news):
    prompt = f"以下是關於'{query}'的新聞標題，請分析這些新聞的整體趨勢，並給出對'{query}'未來發展的看法：\n\n"
    for article in news:
        prompt += f"- {article['title']}\n"
    prompt += "\n請提供詳細的分析，包括可能的影響和未來展望。"

    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "你是一個專業的分析師，專門分析新聞和趨勢。"},
            {"role": "user", "content": prompt}
        ]
    )
    
    return response.choices[0].message['content']

def main():
    query = input("請輸入您想搜索的關鍵字: ")
    days = int(input("請輸入您想搜索的天數 (默認30天): ") or "30")

    print(f"\n正在獲取關於 '{query}' 的最新新聞...")
    news = get_news(query, days=days)
    print(f"獲取到 {len(news)} 條新聞")

    print("\n新聞標題：")
    for article in news:
        print(f"- {article['title']} ({article['pubDate'].strftime('%Y-%m-%d')})")

    print("\n正在使用 OpenAI 分析新聞...")
    analysis = analyze_news(query, news)

    print("\n分析結果：")
    print(analysis)

if __name__ == "__main__":
    main()