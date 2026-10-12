import asyncio
import json
import os
from playwright.async_api import async_playwright
import random

class TikTokUploader:
    def __init__(self, cookies_data=None):
        self.cookies_data = cookies_data or []
        
    async def upload(self, video_path, description, hashtags=""):
        """Загрузка видео с готовыми cookies"""
        
        if not os.path.exists(video_path):
            raise FileNotFoundError(f" Видео не найдено: {video_path}")
        
        async with async_playwright() as p:
            # Запускаем браузер
            browser = await p.chromium.launch(
                headless=False,  # Для отладки
                args=[
                    '--window-size=390,844',
                    '--disable-blink-features=AutomationControlled'
                ]
            )
            
            # Создаём контекст с мобильным User-Agent
            context = await browser.new_context(
                viewport={'width': 390, 'height': 844},
                user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1',
                locale='ru-RU'
            )
            
            # Добавляем ваши cookies
            await context.add_cookies(self.cookies_data)
            print(" Cookies загружены!")
            
            page = await context.new_page()
            
            # Переходим на страницу загрузки
            print(" Переход на страницу загрузки...")
            await page.goto('https://www.tiktok.com/upload', wait_until='networkidle')
            
            # Проверяем авторизацию
            await asyncio.sleep(3)
            
            # Проверяем, авторизованы ли мы
            is_logged_in = await page.evaluate('''
                () => {
                    return !!document.querySelector('[data-e2e="profile-icon"]');
                }
            ''')
            
            if is_logged_in:
                print(" Авторизация успешна!")
            else:
                print(" Возможно, cookies устарели. Попробуйте обновить.")
                # Показываем кнопку входа для ручной авторизации
                await page.screenshot(path='login_check.png')
            
            # Загружаем видео
            print(f" Загрузка видео: {video_path}")
            
            # Находим input для загрузки
            try:
                file_input = await page.wait_for_selector(
                    'input[type="file"][accept*="video"]',
                    timeout=10000
                )
                await file_input.set_input_files(video_path)
                print(" Начинается загрузка видео...")
            except Exception as e:
                print(f" Не найден input загрузки: {e}")
                # Альтернативный поиск
                file_input = await page.query_selector('input[type="file"]')
                if file_input:
                    await file_input.set_input_files(video_path)
                    print(" Видео загружено через альтернативный input")
            
            # Ожидаем завершения загрузки
            await self.wait_for_upload_complete(page)
            
            # Заполняем описание
            full_description = f"{description}\n\n{hashtags}" if hashtags else description
            await self.fill_description(page, full_description)
            
            # Публикуем
            await self.publish_video(page)
            
            await asyncio.sleep(5)
            await browser.close()
    
    async def wait_for_upload_complete(self, page):
        """Ожидание завершения загрузки"""
        print(" Ожидание завершения загрузки...")
        
        # Ждём появления кнопки "Next" или индикатора прогресса
        try:
            # Ждём, пока пропадёт индикатор загрузки
            await page.wait_for_selector(
                'button:has-text("Next"), button:has-text("Далее")',
                timeout=120000
            )
            print(" Видео загружено!")
            await asyncio.sleep(2)
        except:
            print(" Проверяем статус загрузки...")
            # Делаем скриншот для диагностики
            await page.screenshot(path='upload_status.png')
    
    async def fill_description(self, page, description):
        """Заполнение описания"""
        print(f" Заполняем описание: {description[:50]}...")
        
        try:
            # Ищем поле ввода описания
            description_input = await page.wait_for_selector(
                'textarea[placeholder*="описание"], textarea[placeholder*="description"], div[contenteditable="true"][role="textbox"]',
                timeout=5000
            )
            
            if description_input:
                await description_input.fill(description)
                print(" Описание заполнено!")
                await asyncio.sleep(1)
        except:
            # Пробуем найти textarea
            try:
                textarea = await page.query_selector('textarea')
                if textarea:
                    await textarea.fill(description)
                    print(" Описание заполнено через textarea!")
            except:
                print(" Не найдено поле для описания")
    
    async def publish_video(self, page):
        """Публикация видео"""
        print(" Публикация...")
        
        # Жмём "Далее" или "Next"
        try:
            next_btn = await page.wait_for_selector(
                'button:has-text("Next"), button:has-text("Далее")',
                timeout=5000
            )
            await next_btn.click()
            print(" Нажата кнопка 'Далее'")
            await asyncio.sleep(2)
        except:
            print(" Кнопка 'Далее' не найдена, пробуем сразу публиковать...")
        
        # Ищем кнопку публикации
        try:
            publish_btn = await page.wait_for_selector(
                'button:has-text("Post"), button:has-text("Опубликовать"), button[data-e2e="post-button"]',
                timeout=5000
            )
            await publish_btn.click()
            print(" Нажата кнопка публикации!")
            
            # Ждём завершения
            await asyncio.sleep(5)
            
            # Проверяем успешность
            try:
                success = await page.wait_for_selector(
                    'text="Видео опубликовано", text="Video posted", text="Your video is being processed"',
                    timeout=10000
                )
                if success:
                    print(" ВИДЕО УСПЕШНО ОПУБЛИКОВАНО!")
                    await page.screenshot(path='success.png')
            except:
                print(" Проверьте статус вручную")
                await page.screenshot(path='final_state.png')
                
        except Exception as e:
            print(f" Ошибка при публикации: {e}")
            await page.screenshot(path='error_state.png')

# ====================================================
# ВАШИ COOKIES (скопированы из вашего сообщения)
# ====================================================
YOUR_COOKIES = [
    {
        "domain": ".www.tiktok.com",
        "expirationDate": 1812426912,
        "hostOnly": False,
        "httpOnly": False,
        "name": "delay_guest_mode_vid",
        "path": "/",
        "sameSite": None,
        "secure": True,
        "session": False,
        "storeId": None,
        "value": "5"
    },
    {
        "domain": ".tiktok.com",
        "expirationDate": 1787390918.362794,
        "hostOnly": False,
        "httpOnly": False,
        "name": "msToken",
        "path": "/",
        "sameSite": "no_restriction",
        "secure": True,
        "session": False,
        "storeId": None,
        "value": "065G_olD65jI9u7ik4Q_TCuusNiBnZTjIkVQ3NvMFPKh8LZ8jjZBK4TM3o5AittLHgc2HokcxN4ALJyLLP3UpZmBTVt8xZOw4SL1WuY7rk-lcISXOPjIC6bOIZKmEcvUpJACE2U4no8p9BEYpwVTqPM8DvRyzKvH"
    },
    {
        "domain": ".tiktok.com",
        "expirationDate": 1801996527.899836,
        "hostOnly": False,
        "httpOnly": True,
        "name": "tt_session_tlb_tag",
        "path": "/",
        "sameSite": "no_restriction",
        "secure": True,
        "session": False,
        "storeId": None,
        "value": "sttt%7C4%7CejqDXFGAxw6dMmlS702ymP________-rtXJ23EnbrOhsGjEsGwmDsHOyPqXZ5XqiaCJRF_rxmtk%3D"
    },
    {
        "domain": ".tiktok.com",
        "expirationDate": 1817557425.899394,
        "hostOnly": False,
        "httpOnly": True,
        "name": "sid_guard",
        "path": "/",
        "sameSite": None,
        "secure": True,
        "session": False,
        "storeId": None,
        "value": "7a3a835c5180c70e9d326952ef4db298%7C1786453444%7C15543102%7CSun%2C+07-Feb-2027+10%3A35%3A46+GMT"
    },
    {
        "domain": ".tiktok.com",
        "expirationDate": 1818062917.684143,
        "hostOnly": False,
        "httpOnly": True,
        "name": "ttwid",
        "path": "/",
        "sameSite": "no_restriction",
        "secure": True,
        "session": False,
        "storeId": None,
        "value": "1%7CJ_ZHv2muZRMwAhfcUFZPsu1YwGCkXt6aZWHG3Rg6lJo%7C1786526937%7C15669cd1f8be2fbc5a45c0aa38518df1a66c59c697198e84f7c6aac773065928"
    },
    {
        "domain": ".www.tiktok.com",
        "expirationDate": 1787131711,
        "hostOnly": False,
        "httpOnly": False,
        "name": "perf_feed_cache",
        "path": "/",
        "sameSite": None,
        "secure": True,
        "session": False,
        "storeId": None,
        "value": "{%22expireTimestamp%22:1787130000000%2C%22itemIds%22:[%227664235477750484247%22%2C%227670688277422148894%22]}"
    },
    {
        "domain": ".tiktok.com",
        "expirationDate": 1801996527.899532,
        "hostOnly": False,
        "httpOnly": True,
        "name": "uid_tt",
        "path": "/",
        "sameSite": None,
        "secure": True,
        "session": False,
        "storeId": None,
        "value": "7f6f6caf1d8402b967b9e22e7daea22bbc6181a034403610bf04fd4739a88356"
    },
    {
        "domain": ".www.tiktok.com",
        "expirationDate": 1812446905,
        "hostOnly": False,
        "httpOnly": False,
        "name": "tiktok_webapp_theme",
        "path": "/",
        "sameSite": None,
        "secure": True,
        "session": False,
        "storeId": None,
        "value": "dark"
    },
    {
        "domain": ".tiktok.com",
        "expirationDate": 1801996527.899718,
        "hostOnly": False,
        "httpOnly": True,
        "name": "sessionid",
        "path": "/",
        "sameSite": None,
        "secure": True,
        "session": False,
        "storeId": None,
        "value": "7a3a835c5180c70e9d326952ef4db298"
    },
    {
        "domain": ".tiktok.com",
        "expirationDate": 1801996527.899776,
        "hostOnly": False,
        "httpOnly": True,
        "name": "sessionid_ss",
        "path": "/",
        "sameSite": "no_restriction",
        "secure": True,
        "session": False,
        "storeId": None,
        "value": "7a3a835c5180c70e9d326952ef4db298"
    },
    {
        "domain": ".tiktok.com",
        "expirationDate": 1801996527.899658,
        "hostOnly": False,
        "httpOnly": True,
        "name": "sid_tt",
        "path": "/",
        "sameSite": None,
        "secure": True,
        "session": False,
        "storeId": None,
        "value": "7a3a835c5180c70e9d326952ef4db298"
    },
    {
        "domain": ".www.tiktok.com",
        "expirationDate": 1812446905,
        "hostOnly": False,
        "httpOnly": False,
        "name": "tiktok_webapp_theme_source",
        "path": "/",
        "sameSite": None,
        "secure": True,
        "session": False,
        "storeId": None,
        "value": "auto"
    },
    {
        "domain": ".tiktok.com",
        "expirationDate": 1802078903.789616,
        "hostOnly": False,
        "httpOnly": True,
        "name": "tt_chain_token",
        "path": "/",
        "sameSite": None,
        "secure": True,
        "session": False,
        "storeId": None,
        "value": "XSBRWa8o17b0hDT/5cxHiA=="
    },
    {
        "domain": ".tiktok.com",
        "hostOnly": False,
        "httpOnly": True,
        "name": "tt_csrf_token",
        "path": "/",
        "sameSite": "lax",
        "secure": True,
        "session": True,
        "storeId": None,
        "value": "cPU7amXe-GwUSNMTRAM_a7DDFPqG53xlT5Ew"
    },
    {
        "domain": ".tiktok.com",
        "expirationDate": 1801996527.899596,
        "hostOnly": False,
        "httpOnly": True,
        "name": "uid_tt_ss",
        "path": "/",
        "sameSite": "no_restriction",
        "secure": True,
        "session": False,
        "storeId": None,
        "value": "7f6f6caf1d8402b967b9e22e7daea22bbc6181a034403610bf04fd4739a88356"
    }
]

# ====================================================
# ЗАПУСК
# ====================================================
async def main():
    uploader = TikTokUploader(YOUR_COOKIES)
    
    # Укажите ваши данные
    video_path = "video.mp4"  # Путь к вашему видео
    description = "Моё крутое видео"  # Описание
    hashtags = "#fyp #viral #tiktok"  # Хэштеги (опционально)
    
    try:
        await uploader.upload(video_path, description, hashtags)
    except Exception as e:
        print(f" Ошибка: {e}")

if __name__ == "__main__":
    asyncio.run(main())