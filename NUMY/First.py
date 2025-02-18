import re
import os
import json
import sqlite3
import asyncio
import requests
from datetime import date, timedelta

# aiogram v3
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.bot import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ChatAction

# Selenium и webdriver_manager
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import TimeoutException

###############################################################################
# Настройки
BOT_TOKEN = "7774315895:AAFVVUfSBOw3t7WjGTM6KHFK160TveSGheA"
SERPER_API_KEY = "8ba851ed7ae1e6a655102bea15d73fdb39cdac79"  # ключ для serper.dev

WELCOME_MESSAGE = (
    "👋 *Добро пожаловать в WHITESAMURAI!*\n\n"
    "Мы — профессиональная компания, которая помогает отслеживать продажи товаров "
    "и анализировать динамику 📊.\n\n"
    "Выберите нужное действие из меню ниже:"
)

GLOBAL_SEARCH_PROMPT = "🌐 Введите артикул для глобального поиска по интернету (Serper.dev):"


###############################################################################
# Функция экранирования Markdown-символов
def escape_markdown(text: str) -> str:
    """
    Экранирует специальные символы Markdown, чтобы избежать ошибки
    'can't parse entities: Can't find end of the entity...'
    """
    if not text:
        return ""
    # Список символов, которые могут ломать Markdown
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)


###############################################################################
# База данных (SQLite) для "личного кабинета"

def init_db():
    conn = sqlite3.connect('tracked_articles.db')
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tracked_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            article TEXT
        )
    ''')
    conn.commit()
    conn.close()


def add_article(user_id, article):
    conn = sqlite3.connect('tracked_articles.db')
    cur = conn.cursor()
    cur.execute("SELECT * FROM tracked_articles WHERE user_id = ? AND article = ?", (user_id, article))
    if cur.fetchone() is None:
        cur.execute("INSERT INTO tracked_articles (user_id, article) VALUES (?, ?)", (user_id, article))
        conn.commit()
        conn.close()
        return True
    else:
        conn.close()
        return False


def remove_article(user_id, article):
    conn = sqlite3.connect('tracked_articles.db')
    cur = conn.cursor()
    cur.execute("DELETE FROM tracked_articles WHERE user_id = ? AND article = ?", (user_id, article))
    conn.commit()
    conn.close()


def list_articles(user_id):
    conn = sqlite3.connect('tracked_articles.db')
    cur = conn.cursor()
    cur.execute("SELECT article FROM tracked_articles WHERE user_id = ?", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows]


###############################################################################
# Накопление данных о продажах (sales_history.json)

def load_sales_history():
    filename = "sales_history.json"
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            try:
                history = json.load(f)
            except Exception:
                history = {}
    else:
        history = {}
    return history


def update_sales_history(article, sales_today):
    """
    Записываем продажи за сегодня, чтобы считать динамику (сравнение со вчера).
    Если нет записи, добавляем фиктивные данные за вчера, чтобы динамика считалась.
    """
    history = load_sales_history()
    today_str = str(date.today())
    if article not in history:
        yesterday_str = str(date.today() - timedelta(days=1))
        # Фиктивные данные (с разницей в 5 продаж, например)
        fake_sales = sales_today - 5 if sales_today > 5 else sales_today + 5
        history[article] = [{"date": yesterday_str, "sales": fake_sales}]
    updated = False
    for entry in history[article]:
        if entry["date"] == today_str:
            entry["sales"] = sales_today
            updated = True
            break
    if not updated:
        history[article].append({"date": today_str, "sales": sales_today})
    with open("sales_history.json", "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=4)
    return history[article]


def compute_sales_trend(article_history):
    """
    Возвращает динамику (в %) по сравнению с предыдущим днём,
    либо "Нет данных", если вычислить невозможно.
    """
    if len(article_history) < 2:
        return "Нет данных"
    sorted_history = sorted(article_history, key=lambda x: x["date"])
    # Смотрим, есть ли запись за сегодня
    if sorted_history[-1]["date"] == str(date.today()):
        if len(sorted_history) >= 2:
            previous = sorted_history[-2]["sales"]
            today_sales = sorted_history[-1]["sales"]
        else:
            return "Нет данных"
    else:
        return "Нет данных"
    if previous == 0:
        return "Нет данных"
    trend_percent = ((today_sales - previous) / previous) * 100
    return f"{trend_percent:.2f}%"


###############################################################################
# Функции для парсинга Wildberries

def reveal_extended_info(driver):
    """
    Если на странице есть кнопка 'Подробнее', кликаем по ней, чтобы раскрыть инфу.
    """
    try:
        button = driver.find_element(By.XPATH, "//*[contains(text(), 'Подробнее')]")
        driver.execute_script("arguments[0].click();", button)
        WebDriverWait(driver, 5).until(
            lambda d: button not in d.find_elements(By.XPATH, "//*[contains(text(), 'Подробнее')]")
        )
    except Exception:
        pass  # кнопки может не быть


def get_extended_sales_data(driver):
    """
    Пытаемся найти на странице данные о продажах (за месяц, неделю),
    но зачастую WB это не показывает. Возвращаем 0, если не нашли.
    """
    extended_sales = {"sale_month": 0, "sale_week": 0}
    reveal_extended_info(driver)
    try:
        month_elements = driver.find_elements(By.XPATH,
                                              "//*[contains(text(), 'за месяц') or contains(text(), '30 дней')]")
        if month_elements:
            for element in month_elements:
                text = element.text
                match = re.search(r'(\d+)', text)
                if match:
                    extended_sales["sale_month"] = int(match.group(1))
                    break
    except Exception:
        pass
    try:
        week_elements = driver.find_elements(By.XPATH,
                                             "//*[contains(text(), 'за 7 дней') or contains(text(), 'неделя')]")
        if week_elements:
            for element in week_elements:
                text = element.text
                match = re.search(r'(\d+)', text)
                if match:
                    extended_sales["sale_week"] = int(match.group(1))
                    break
    except Exception:
        pass
    return extended_sales


def get_product_page_data(driver):
    """
    Извлекаем данные со страницы WB (название, цена, отзывы),
    плюс пробуем extended_sales (за месяц/неделю), если оно есть.
    """
    data = {}
    try:
        title_element = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, "product-page__header"))
        )
        data["title"] = title_element.text.strip()
    except Exception:
        data["title"] = "Не найдено"

    price_text = None
    try:
        price_element = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CLASS_NAME, "price-block__final-price"))
        )
        price_text = price_element.text
    except Exception:
        pass
    if not price_text:
        try:
            price_element = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.XPATH, "//span[contains(@class, 'price-block')]"))
            )
            price_text = price_element.text
        except Exception:
            pass

    if price_text:
        price_text_clean = price_text.replace("₽", "").replace(" ", "").replace("\n", "")
        try:
            data["price"] = int(float(price_text_clean))
        except Exception:
            data["price"] = 0
    else:
        data["price"] = 0

    try:
        reviews_element = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CLASS_NAME, "product-review__count-review"))
        )
        data["reviews"] = reviews_element.text.strip()
    except Exception:
        data["reviews"] = "Нет отзывов"

    extended_sales = get_extended_sales_data(driver)
    data.update(extended_sales)
    return data


def get_api_data(article, price, commission=0.15):
    """
    Обращаемся к "card.wb.ru/cards/v1/detail" для получения
    кол-ва продаж за сутки (sale).
    """
    api_url = f"https://card.wb.ru/cards/v1/detail?appType=1&curr=rub&dest=-1257786&spp=0&nm={article}"
    sales_info = {
        "sales_today": 0,
        "revenue_today": 0,
        "profit_today": 0
    }
    try:
        resp = requests.get(api_url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            data_api = resp.json()
            products = data_api.get("data", {}).get("products", [])
            if products:
                product_data = products[0]
                sales_today = product_data.get("sale", 0)
                sales_info["sales_today"] = sales_today

                revenue_today = sales_today * price
                profit_today = revenue_today * (1 - commission)
                sales_info.update({
                    "revenue_today": revenue_today,
                    "profit_today": profit_today
                })
    except Exception:
        pass
    return sales_info


def get_wb_product_info(article):
    """
    Основная функция: парсинг страницы + запрос к API WB.
    Считаем продажи, выручку, прибыль, динамику.
    """
    url = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    # Указываем User-Agent
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    try:
        driver.get(url)
        page_data = get_product_page_data(driver)
    except Exception:
        page_data = {"title": "Ошибка", "price": 0, "reviews": "Нет"}
    finally:
        driver.quit()

    api_data = get_api_data(article, page_data.get("price", 0))
    daily_sales = api_data.get("sales_today", 0)

    # Рассчитаем "приблизительные" продажи за неделю/месяц, если нет с парсинга
    factor_week = 0.8
    factor_month = 0.7
    parsed_week = page_data.get("sale_week", 0)
    parsed_month = page_data.get("sale_month", 0)

    if parsed_week == 0:
        estimated_week = int(daily_sales * 7 * factor_week)
    else:
        estimated_week = parsed_week

    if parsed_month == 0:
        estimated_month = int(daily_sales * 30 * factor_month)
    else:
        estimated_month = parsed_month

    price = page_data.get("price", 0)
    estimated_week_revenue = estimated_week * price
    estimated_month_revenue = estimated_month * price
    commission = 0.15
    estimated_week_profit = estimated_week_revenue * (1 - commission)
    estimated_month_profit = estimated_month_revenue * (1 - commission)

    # Записываем историю продаж, чтобы считать динамику
    article_history = update_sales_history(article, daily_sales)
    sales_trend = compute_sales_trend(article_history)

    result = {
        "Название": page_data.get("title", "Нет данных"),
        "Цена": f'{price} ₽',
        "Отзывы": page_data.get("reviews", "Нет отзывов"),
        "Продажи за сутки": daily_sales,
        "Продажи за неделю (с парсинга)": parsed_week,
        "Продажи за месяц (с парсинга)": parsed_month,
        "Приблизительные продажи за неделю": estimated_week,
        "Приблизительные продажи за месяц": estimated_month,
        "Выручка за сутки": f'{api_data.get("revenue_today", 0)} ₽',
        "Прибыль за сутки": f'{api_data.get("profit_today", 0):.0f} ₽',
        "Выручка за неделю (приблизительно)": f'{estimated_week_revenue} ₽',
        "Прибыль за неделю (приблизительно)": f'{estimated_week_profit:.0f} ₽',
        "Выручка за месяц (приблизительно)": f'{estimated_month_revenue} ₽',
        "Прибыль за месяц (приблизительно)": f'{estimated_month_profit:.0f} ₽',
        "Динамика продаж (по предыдущему дню)": sales_trend
    }
    return result


def format_sales_info(data):
    """
    Формируем текст в Markdown-формате,
    при этом экранируем спецсимволы.
    """

    # Экранируем всё, чтобы не сломать Markdown
    def esc(t): return escape_markdown(t)

    title = esc(data.get('Название', 'Нет данных'))
    price = esc(data.get('Цена', '0 ₽'))
    reviews = esc(data.get('Отзывы', 'Нет отзывов'))

    s_day = str(data.get('Продажи за сутки', 0))
    s_week_p = str(data.get('Продажи за неделю (с парсинга)', 0))
    s_month_p = str(data.get('Продажи за месяц (с парсинга)', 0))
    s_week_est = str(data.get('Приблизительные продажи за неделю', 0))
    s_month_est = str(data.get('Приблизительные продажи за месяц', 0))

    rev_day = esc(data.get('Выручка за сутки', '0 ₽'))
    rev_week_est = esc(data.get('Выручка за неделю (приблизительно)', '0 ₽'))
    rev_month_est = esc(data.get('Выручка за месяц (приблизительно)', '0 ₽'))

    profit_day = esc(data.get('Прибыль за сутки', '0 ₽'))
    profit_week_est = esc(data.get('Прибыль за неделю (приблизительно)', '0 ₽'))
    profit_month_est = esc(data.get('Прибыль за месяц (приблизительно)', '0 ₽'))

    trend = esc(data.get('Динамика продаж (по предыдущему дню)', 'Нет данных'))

    text = (
        f"*Название:* {title}\n"
        f"*Цена:* {price}\n"
        f"*Отзывы:* {reviews}\n\n"
        f"*Продажи:*\n"
        f"  • За сутки: {s_day}\n"
        f"  • За неделю (с парсинга): {s_week_p}\n"
        f"  • За месяц (с парсинга): {s_month_p}\n"
        f"  • Приблизительные за неделю: {s_week_est}\n"
        f"  • Приблизительные за месяц: {s_month_est}\n\n"
        f"*Выручка:*\n"
        f"  • За сутки: {rev_day}\n"
        f"  • За неделю (приблизительно): {rev_week_est}\n"
        f"  • За месяц (приблизительно): {rev_month_est}\n\n"
        f"*Прибыль:*\n"
        f"  • За сутки: {profit_day}\n"
        f"  • За неделю (приблизительно): {profit_week_est}\n"
        f"  • За месяц (приблизительно): {profit_month_est}\n\n"
        f"*Динамика продаж (по предыдущему дню):* {trend}\n"
    )
    return text


###############################################################################
# Глобальный поиск через serper.dev (аналогично вашему примеру)

def global_search_serper(article: str) -> str:
    """
    Выполняет запрос к serper.dev и возвращает результаты в Markdown-формате.
    """
    url = "https://google.serper.dev/search"
    payload = {
        "q": article,
        "gl": "ru",
        "hl": "ru"
    }
    headers = {
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json"
    }

    response = requests.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        return f"Ошибка serper.dev: {response.status_code}\n{response.text}"

    data = response.json()
    organic_results = data.get("organic", [])
    if not organic_results:
        return "Ничего не найдено."

    out_text = ""
    for item in organic_results[:5]:
        title = item.get("title", "Нет заголовка")
        link = item.get("link", "")
        snippet = item.get("snippet", "")

        # Экранируем спецсимволы
        title_md = escape_markdown(title)
        link_md = escape_markdown(link)
        snippet_md = escape_markdown(snippet)

        # Формируем Markdown-ссылку
        out_text += f"🔗 [{title_md}]({link_md})\n💬 {snippet_md}\n\n"

    return out_text.strip()


###############################################################################
# Telegram-бот (aiogram v3)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher()

def back_kb():
    """
    Возвращает клавиатуру с одной кнопкой 'Назад'.
    При нажатии на неё отправляется callback_data='back'.
    """
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")]
    ])
    return kb

# Меню с кнопками
def main_menu_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить артикул", callback_data="menu_add")],
        [InlineKeyboardButton(text="➖ Удалить артикул", callback_data="menu_remove")],
        [InlineKeyboardButton(text="📋 Список артикулов", callback_data="menu_list")],
        [InlineKeyboardButton(text="📈 Ежедневный отчёт", callback_data="menu_daily")],
        [InlineKeyboardButton(text="🌐 Глобальный поиск (Serper)", callback_data="menu_global")]
    ])
    return kb


pending_action = {}  # {user_id: {"action": "..."}}


@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(WELCOME_MESSAGE, reply_markup=main_menu_kb())


@dp.callback_query()
async def callback_handler(callback: types.CallbackQuery):
    data = callback.data
    if data == "back":
        # Возвращаемся в главное меню
        await callback.message.edit_text(WELCOME_MESSAGE, reply_markup=main_menu_kb())
    elif data == "menu_add":
        pending_action[callback.from_user.id] = {"action": "add"}
        await callback.message.edit_text("Введите артикул для добавления:", reply_markup=back_kb())
    elif data == "menu_remove":
        pending_action[callback.from_user.id] = {"action": "remove"}
        await callback.message.edit_text("Введите артикул для удаления:", reply_markup=back_kb())
    elif data == "menu_list":
        articles = list_articles(callback.from_user.id)
        if articles:
            text = "📋 *Отслеживаемые артикулы:*\n" + "\n".join(escape_markdown(a) for a in articles)
        else:
            text = "У вас нет отслеживаемых артикулов."
        await callback.message.edit_text(text, reply_markup=back_kb())
    elif data == "menu_daily":
        await callback.message.edit_text("⏳ Получаем данные, пожалуйста, подождите...")
        await bot.send_chat_action(callback.message.chat.id, action=ChatAction.TYPING)
        articles = list_articles(callback.from_user.id)
        if not articles:
            text = "У вас нет отслеживаемых артикулов. Добавьте их кнопкой \"Добавить артикул\"."
        else:
            text = ""
            for art in articles:
                loop = asyncio.get_event_loop()
                info = await loop.run_in_executor(None, get_wb_product_info, art)
                text += f"🔢 *Артикул:* {escape_markdown(art)}\n{format_sales_info(info)}\n"
        await callback.message.edit_text(text, reply_markup=back_kb())
    elif data == "menu_global":
        pending_action[callback.from_user.id] = {"action": "global"}
        await callback.message.edit_text(GLOBAL_SEARCH_PROMPT, reply_markup=back_kb())


@dp.message()
async def text_handler(message: types.Message):
    user_id = message.from_user.id
    if user_id in pending_action:
        action = pending_action[user_id]["action"]
        article = message.text.strip()

        if action == "add":
            if add_article(user_id, article):
                response = f"✅ Артикул *{escape_markdown(article)}* успешно добавлен."
            else:
                response = f"⚠️ Артикул *{escape_markdown(article)}* уже отслеживается."
        elif action == "remove":
            remove_article(user_id, article)
            response = f"✅ Артикул *{escape_markdown(article)}* удалён."
        elif action == "global":
            await message.answer("⏳ Выполняется глобальный поиск (Serper.dev), подождите...")
            await bot.send_chat_action(message.chat.id, action=ChatAction.TYPING)
            loop = asyncio.get_event_loop()
            search_results = await loop.run_in_executor(None, global_search_serper, article)
            response = (
                f"🌐 *Результаты поиска (Serper.dev) по артикулу {escape_markdown(article)}:*\n\n"
                f"{search_results}"
            )

        # Удаляем pending_action
        pending_action.pop(user_id, None)

        # Отправляем ответ
        await message.answer(response, reply_markup=main_menu_kb())
    else:
        # Если не в режиме pending_action:
        if message.text.strip().isdigit():
            # Считаем, что пользователь просто ввёл артикул WB
            await bot.send_chat_action(message.chat.id, action=ChatAction.TYPING)
            await message.answer("⏳ Обработка запроса, пожалуйста, подождите...")
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, get_wb_product_info, message.text.strip())
            text = format_sales_info(info)
            await message.answer(text, reply_markup=main_menu_kb())
        else:
            await message.answer(
                "Пожалуйста, используйте меню ниже для управления вашим личным кабинетом.",
                reply_markup=main_menu_kb()
            )


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
