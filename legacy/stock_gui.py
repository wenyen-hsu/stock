import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
from stock_reports import get_stock_reports
import threading

def analyze_stock():
    symbol = symbol_entry.get().strip()
    if not symbol:
        messagebox.showerror("錯誤", "請輸入股票代碼")
        return
    
    # Disable the analyze button and show loading indicator
    analyze_button.config(state=tk.DISABLED)
    loading_label.grid(row=2, column=1, pady=5)
    status_bar.config(text="正在分析...")
    root.update()
    
    def perform_analysis():
        try:
            analysis, financial_summary, news_summary = get_stock_reports(symbol)
            
            # Clear previous results
            result_text.delete(1.0, tk.END)
            
            # Display new results
            result_text.insert(tk.END, f"{symbol} 的分析結果：\n\n")
            result_text.insert(tk.END, "AI財務分析：\n")
            result_text.insert(tk.END, analysis)
            result_text.insert(tk.END, "\n\n財務摘要：\n")
            result_text.insert(tk.END, financial_summary)
            result_text.insert(tk.END, "\n\n近期新聞：\n")
            result_text.insert(tk.END, news_summary)
            
            status_bar.config(text="分析完成")
        except Exception as e:
            messagebox.showerror("錯誤", f"獲取股票數據時發生錯誤：{str(e)}")
            status_bar.config(text="分析失敗")
        finally:
            # Re-enable the analyze button and hide loading indicator
            analyze_button.config(state=tk.NORMAL)
            loading_label.grid_remove()
    
    # Run the analysis in a separate thread to keep the GUI responsive
    threading.Thread(target=perform_analysis, daemon=True).start()

def clear_all():
    symbol_entry.delete(0, tk.END)
    result_text.delete(1.0, tk.END)
    result_text.insert(tk.END, instructions)
    status_bar.config(text="就緒")

def save_results():
    content = result_text.get(1.0, tk.END)
    if content.strip() == instructions.strip():
        messagebox.showinfo("提示", "沒有分析結果可以保存。")
        return
    
    file_path = filedialog.asksaveasfilename(defaultextension=".txt",
                                             filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
    if file_path:
        with open(file_path, "w", encoding="utf-8") as file:
            file.write(content)
        status_bar.config(text=f"結果已保存到 {file_path}")

def show_about():
    about_text = "股票分析 GUI\n\n版本 1.2\n\n作者：Claude Dev"
    messagebox.showinfo("關於", about_text)

def on_enter_key(event):
    analyze_stock()

def on_symbol_entry_focus_in(event):
    if symbol_entry.get().strip() != "":
        symbol_entry.delete(0, tk.END)
        clear_all()

# Create main window
root = tk.Tk()
root.title("股票分析 GUI")
root.geometry("900x700")

# Create menu bar
menu_bar = tk.Menu(root)
root.config(menu=menu_bar)

file_menu = tk.Menu(menu_bar, tearoff=0)
menu_bar.add_cascade(label="文件", menu=file_menu)
file_menu.add_command(label="保存結果", command=save_results)
file_menu.add_separator()
file_menu.add_command(label="退出", command=root.quit)

help_menu = tk.Menu(menu_bar, tearoff=0)
menu_bar.add_cascade(label="幫助", menu=help_menu)
help_menu.add_command(label="關於", command=show_about)

# Create and place widgets
frame = ttk.Frame(root, padding="10")
frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
root.columnconfigure(0, weight=1)
root.rowconfigure(0, weight=1)

ttk.Label(frame, text="輸入股票代碼：").grid(row=0, column=0, sticky=tk.W, pady=5)
symbol_entry = ttk.Entry(frame, width=20)
symbol_entry.grid(row=0, column=1, sticky=(tk.W, tk.E), pady=5)
symbol_entry.bind("<Return>", on_enter_key)
symbol_entry.bind("<FocusIn>", on_symbol_entry_focus_in)

button_frame = ttk.Frame(frame)
button_frame.grid(row=1, column=0, columnspan=2, pady=10)

analyze_button = ttk.Button(button_frame, text="分析股票", command=analyze_stock)
analyze_button.pack(side=tk.LEFT, padx=5)

clear_button = ttk.Button(button_frame, text="清除", command=clear_all)
clear_button.pack(side=tk.LEFT, padx=5)

loading_label = ttk.Label(frame, text="正在分析... 請稍候。")
loading_label.grid(row=2, column=0, columnspan=2, pady=5)
loading_label.grid_remove()  # Hide initially

result_text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, width=80, height=30)
result_text.grid(row=3, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S), pady=10)

frame.columnconfigure(1, weight=1)
frame.rowconfigure(3, weight=1)

# Add status bar
status_bar = ttk.Label(root, text="就緒", relief=tk.SUNKEN, anchor=tk.W)
status_bar.grid(row=1, column=0, sticky=(tk.W, tk.E))

# Add instructions
instructions = """
使用說明：
1. 輸入有效的股票代碼（例如，台積電的代碼為 2330）。
2. 點擊「分析股票」按鈕或按 Enter 鍵開始分析。
3. 等待分析完成（這可能需要幾分鐘時間）。
4. 在下方文本區域查看分析結果。
5. 使用「清除」按鈕重置輸入和結果。
6. 使用「文件 > 保存結果」將分析結果保存到文件。

注意：對於台灣股票，請直接輸入數字代碼（如 2330），系統會自動添加 .TW 後綴。
"""
result_text.insert(tk.END, instructions)

# Start the GUI event loop
root.mainloop()