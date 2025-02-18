import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


def get_wb_product_info(article):
    url = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"

    # Настройки браузера
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    try:
        driver.get(url)

        # Название товара
        title_element = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, "product-page__header"))
        )
        title = title_element.text.strip()

        # Цена товара
        try:
            price_element = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CLASS_NAME, "price-block__final-price"))
            )
            price = int(price_element.text.strip().replace(" ", "").replace("₽", ""))
        except Exception as e:
            print(f"Ошибка при получении цены: {e}")
            price = 0

        # Отзывы
        try:
            reviews_element = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CLASS_NAME, "product-review__count-review"))
            )
            reviews = reviews_element.text.strip()
        except Exception as e:
            print(f"Ошибка при получении отзывов: {e}")
            reviews = "Нет отзывов"

        driver.quit()

        return {
            "Название": title,
            "Цена": f"{price} ₽",
            "Отзывы": reviews,
        }

    except Exception as e:
        driver.quit()
        return {"Ошибка": str(e)}


# ✅ Тестируем код
article_number = "233372164"  # 🔄 Укажи артикул товара
data = get_wb_product_info(article_number)
print(data)