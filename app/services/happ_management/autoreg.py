"""
Авторегистратор аккаунтов happ-proxy.com.

Два режима работы:
1. Через nodriver (бесплатно) — патченный Chrome обходит Cloudflare Turnstile
2. Через HTTP + rucaptcha API (платно, ~3₽ за регистрацию) — без браузера

Полный цикл:
1. Создание временного email через mail.tm API
2. Регистрация на happ-proxy.com
3. Подтверждение email по ссылке из письма
4. Логин и извлечение Provider ID + Auth Key
5. Привязка домена подписки через API
6. Добавление Provider ID в модуль
"""

import asyncio
import hashlib
import html as html_lib
import os
import re
import secrets
import shutil
import string
import subprocess
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import aiohttp
import structlog


logger = structlog.get_logger(__name__)


MAIL_TM_API = 'https://api.mail.tm'
HAPP_BASE = 'https://happ-proxy.com'
REGISTER_DELAY = 2
PARALLEL_WORKERS = 2
CAPTCHA_POLL_INTERVAL = 5
CAPTCHA_MAX_WAIT = 180
AUTOREG_STEPS = 7

BROWSER_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'


# ── Temp Mail Client (mail.tm) ────────────────────────────────────────────────


class TempMailClient:
    """Работа с mail.tm — бесплатный API для временной почты."""

    def __init__(self, http: aiohttp.ClientSession):
        self._http = http
        self.address: str = ''
        self.password: str = ''
        self._token: str = ''

    async def create_mailbox(self) -> str:
        """Создаёт временный ящик. Возвращает email-адрес."""
        domains = await self._get_domains()
        if not domains:
            raise RuntimeError('mail.tm: нет доступных доменов')

        domain = domains[0]
        local = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(12))
        self.address = f'{local}@{domain}'
        self.password = secrets.token_urlsafe(16)

        payload = {'address': self.address, 'password': self.password}
        async with self._http.post(f'{MAIL_TM_API}/accounts', json=payload) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                raise RuntimeError(f'mail.tm: ошибка создания ящика ({resp.status}): {text}')

        auth_payload = {'address': self.address, 'password': self.password}
        async with self._http.post(f'{MAIL_TM_API}/token', json=auth_payload) as resp:
            data = await resp.json()
            self._token = data.get('token', '')

        logger.info(f'[HappAutoreg] Создан ящик: {self.address}')
        return self.address

    async def wait_for_email(self, timeout: int = 120) -> dict:
        """Ждёт входящее письмо. Возвращает данные сообщения."""
        headers = {'Authorization': f'Bearer {self._token}'}
        elapsed = 0
        while elapsed < timeout:
            async with self._http.get(f'{MAIL_TM_API}/messages', headers=headers) as resp:
                data = await resp.json()
                messages = data.get('hydra:member', [])
                if messages:
                    msg_id = messages[0].get('id')
                    return await self._get_message(msg_id, headers)
            await asyncio.sleep(5)
            elapsed += 5
        raise TimeoutError(f'mail.tm: письмо не пришло за {timeout}с')

    @staticmethod
    def extract_code(text) -> str | None:
        """Ищет числовой код подтверждения (4-6 цифр) в теле письма."""
        if not text:
            return None
        if isinstance(text, list):
            text = ' '.join(str(t) for t in text)
        text = str(text)
        match = re.search(r'(?:code|код)[:\s]*(\d{4,6})', text, re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r'\b(\d{5})\b', text)
        if match:
            return match.group(1)
        match = re.search(r'\b(\d{4,6})\b', text)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def extract_link(text) -> str | None:
        """Ищет ссылку подтверждения в теле письма."""
        if not text:
            return None
        if isinstance(text, list):
            text = ' '.join(str(t) for t in text)
        text = str(text)
        patterns = [
            r'href=["\']?(https?://[^\s"\'<>]*(?:confirm|verify|activate|token)[^\s"\'<>]*)',
            r'(https?://happ-proxy\.com/[^\s"\'<>]+)',
            r'(https?://[^\s"\'<>]*happ[^\s"\'<>]*(?:confirm|verify|activate|token)[^\s"\'<>]*)',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                link = html_lib.unescape(match.group(1))
                return link
        return None

    async def _get_domains(self) -> list[str]:
        async with self._http.get(f'{MAIL_TM_API}/domains') as resp:
            data = await resp.json()
            return [d['domain'] for d in data.get('hydra:member', []) if d.get('isActive')]

    async def _get_message(self, msg_id: str, headers: dict) -> dict:
        async with self._http.get(f'{MAIL_TM_API}/messages/{msg_id}', headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f'mail.tm: не удалось получить письмо {msg_id}')
            return await resp.json()


# ── Xvfb virtual display ─────────────────────────────────────────────────────


class _VirtualDisplay:
    """Виртуальный дисплей для headed-режима на серверах без GUI."""

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._display: str = ''
        self._old_display: str = ''

    @staticmethod
    def is_available() -> bool:
        return shutil.which('Xvfb') is not None

    def start(self) -> bool:
        if not self.is_available():
            return False
        import random

        display_num = random.randint(100, 999)
        self._display = f':{display_num}'
        try:
            self._process = subprocess.Popen(  # noqa: S603
                ['Xvfb', self._display, '-screen', '0', '1280x800x24', '-nolisten', 'tcp', '-ac'],  # noqa: S607
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._old_display = os.environ.get('DISPLAY', '')
            os.environ['DISPLAY'] = self._display
            logger.info(f'[HappAutoreg] Xvfb запущен на {self._display}')
            return True
        except Exception as e:
            logger.warning(f'[HappAutoreg] Не удалось запустить Xvfb: {e}')
            return False

    def stop(self):
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        if self._old_display:
            os.environ['DISPLAY'] = self._old_display
        elif 'DISPLAY' in os.environ:
            del os.environ['DISPLAY']


# ── Nodriver registration (free, bypasses Cloudflare) ─────────────────────────


def _check_nodriver() -> bool:
    try:
        import nodriver  # noqa: F401

        return True
    except ImportError:
        return False


def _find_system_chrome() -> str | None:
    for name in ['google-chrome-stable', 'google-chrome', 'chromium-browser', 'chromium']:
        path = shutil.which(name)
        if path:
            return path
    for path in [
        '/usr/bin/google-chrome-stable',
        '/usr/bin/google-chrome',
        '/usr/bin/chromium-browser',
        '/usr/bin/chromium',
        '/snap/bin/chromium',
    ]:
        if os.path.isfile(path):
            return path
    return None


class NodriverRegistrar:
    """
    Регистрация через nodriver — патченный Chrome, невидимый для Cloudflare.
    Turnstile решается автоматически без внешних сервисов.
    Xvfb управляется снаружи (_register_via_nodriver), чтобы все воркеры
    использовали один дисплей и не конфликтовали.
    """

    def __init__(self):
        self._browser = None

    async def __aenter__(self):
        import nodriver as uc

        chrome_path = _find_system_chrome()

        config = uc.Config()
        if chrome_path:
            config.browser_executable_path = chrome_path
        config.sandbox = False

        logger.info(f'[HappAutoreg] nodriver: запуск (chrome={chrome_path or "auto"})')
        self._browser = await uc.start(config)
        return self

    async def __aexit__(self, *args):
        if self._browser:
            try:
                self._browser.stop()
            except Exception:
                pass
            self._browser = None
            await asyncio.sleep(0.5)

    async def register_step1(self, email: str, password: str) -> bool:
        """Этап 1: заполнение формы, Turnstile, отправка → появится поле кода."""
        page = await self._browser.get(f'{HAPP_BASE}/security/signup')
        await asyncio.sleep(3)

        try:
            email_field = await page.select("input[type='email']", timeout=15)
            if not email_field:
                email_field = await page.find('E-mail', best_match=True)
            if not email_field:
                logger.error('[HappAutoreg] Поле email не найдено')
                return False
            await email_field.click()
            await asyncio.sleep(0.3)
            await email_field.send_keys(email)
            await asyncio.sleep(0.5)

            pass_field = await page.select("input[type='password']", timeout=5)
            if not pass_field:
                logger.error('[HappAutoreg] Поле пароля не найдено')
                return False
            await pass_field.click()
            await asyncio.sleep(0.3)
            await pass_field.send_keys(password)
            await asyncio.sleep(0.5)

            checkbox = await page.select("input[type='checkbox']", timeout=5)
            if checkbox:
                await checkbox.click()
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f'[HappAutoreg] Ошибка заполнения формы: {e}')
            return False

        logger.info('[HappAutoreg] Форма заполнена, решаем Turnstile...')
        await asyncio.sleep(2)

        if not await self._click_turnstile(page):
            return False

        try:
            submit = await page.select("button[type='submit']", timeout=5)
            if not submit:
                submit = await page.find('Sign up', best_match=True)
            if not submit:
                submit = await page.find('Регистрация', best_match=True)
            if not submit:
                logger.error('[HappAutoreg] Кнопка отправки не найдена')
                return False
            await submit.click()
            logger.info('[HappAutoreg] Форма отправлена, ждём поле кода подтверждения...')
        except Exception as e:
            logger.error(f'[HappAutoreg] Ошибка отправки формы: {e}')
            return False

        await asyncio.sleep(3)
        return True

    async def register_step2(self, code: str) -> bool:
        """Этап 2: ввод кода подтверждения из email."""
        page = list(self._browser.tabs)[-1] if self._browser.tabs else None
        if not page:
            logger.error('[HappAutoreg] Нет активной вкладки для ввода кода')
            return False

        try:
            code_field = None
            for selector in [
                "input[name*='code']:not([type='hidden'])",
                "input[name*='Code']:not([type='hidden'])",
                "input[name*='confirm']:not([type='hidden'])",
                "input[name*='Confirm']:not([type='hidden'])",
                "input[name*='verification']:not([type='hidden'])",
            ]:
                try:
                    code_field = await page.select(selector, timeout=3)
                except Exception:
                    pass
                if code_field:
                    break

            if not code_field:
                try:
                    code_field = await page.find('Confirmation code', best_match=True)
                except Exception:
                    pass
            if not code_field:
                try:
                    code_field = await page.find('Код подтверждения', best_match=True)
                except Exception:
                    pass

            if not code_field:
                all_inputs = await page.select_all(
                    "input:not([type='hidden']):not([type='password']):not([type='email']):not([type='checkbox'])"
                )
                visible = [inp for inp in (all_inputs or []) if inp]
                if visible:
                    code_field = visible[-1]
                    logger.info('[HappAutoreg] Fallback: используем последний видимый input')

            if not code_field:
                logger.error('[HappAutoreg] Поле кода подтверждения не найдено')
                try:
                    await page.save_screenshot('/tmp/code_field_debug.png')  # noqa: S108
                except Exception:
                    pass
                return False

            await code_field.click()
            await asyncio.sleep(0.3)
            await code_field.send_keys(code)
            await asyncio.sleep(0.5)

            logger.info(f'[HappAutoreg] Код {code} введён')

            for attempt in range(3):
                if await self._click_turnstile(page):
                    break
                if attempt < 2:
                    logger.info(f'[HappAutoreg] Turnstile retry {attempt + 2}/3...')
                    await asyncio.sleep(2)

            submit = await page.select("button[type='submit']", timeout=5)
            if not submit:
                all_buttons = await page.select_all('button')
                if all_buttons:
                    submit = all_buttons[-1]
            if not submit:
                submit = await page.find('Регистрация', best_match=True)
            if not submit:
                submit = await page.find('Sign up', best_match=True)

            if submit:
                await submit.click()
                logger.info('[HappAutoreg] Форма с кодом отправлена')
            else:
                logger.warning('[HappAutoreg] Кнопка отправки кода не найдена')
                return False

            await asyncio.sleep(3)

            url = ''
            try:
                result = await page.evaluate('window.location.href')
                url = str(result) if result else ''
            except Exception:
                pass

            logger.info(f'[HappAutoreg] URL после отправки кода: {url}')

            if url and any(s in url for s in ['login', 'dashboard', 'success']):
                logger.info(f'[HappAutoreg] Регистрация завершена → {url}')
                return True

            body = ''
            try:
                result = await page.evaluate('document.body.innerText')
                body = str(result) if result else ''
            except Exception:
                pass

            if body and any(s in body.lower() for s in ['success', 'успешно', 'account created', 'log in', 'войти']):
                logger.info('[HappAutoreg] Регистрация завершена (по тексту)')
                return True

            if url and 'signup' not in url:
                logger.info(f'[HappAutoreg] Регистрация: редирект → {url}')
                return True

            logger.warning(f'[HappAutoreg] Результат неизвестен, URL={url}')
            try:
                await page.save_screenshot('/tmp/register_result.png')  # noqa: S108
            except Exception:
                pass
            return False

        except Exception as e:
            logger.error(f'[HappAutoreg] Ошибка ввода кода: {e}')
            return False

    async def verify_email(self, link: str) -> bool:
        try:
            page = await self._browser.get(link)
            await asyncio.sleep(3)
            url = ''
            try:
                result = await page.evaluate('window.location.href')
                url = str(result) if result else ''
            except Exception:
                pass
            logger.info(f'[HappAutoreg] Email подтверждён → {url}')
            return True
        except Exception as e:
            logger.error(f'[HappAutoreg] Ошибка подтверждения email: {e}')
            return False

    async def _click_turnstile(self, page) -> bool:
        """Универсальный метод для клика по Turnstile и ожидания решения."""
        try:
            import nodriver.cdp.input_ as cdp_input

            coord_str = await page.evaluate("""
                (() => {
                    const cf = document.querySelector('.cf-turnstile');
                    if (!cf) return '';
                    const rect = cf.getBoundingClientRect();
                    return (rect.left + 30) + ',' + (rect.top + rect.height / 2);
                })()
            """)
            if not coord_str or ',' not in str(coord_str):
                return True

            parts = str(coord_str).split(',')
            x, y = float(parts[0]), float(parts[1])
            logger.info(f'[HappAutoreg] Turnstile на странице, кликаем ({x:.0f}, {y:.0f})')

            await page.send(cdp_input.dispatch_mouse_event(type_='mouseMoved', x=x, y=y))
            await asyncio.sleep(0.2)
            await page.send(
                cdp_input.dispatch_mouse_event(
                    type_='mousePressed',
                    x=x,
                    y=y,
                    button=cdp_input.MouseButton.LEFT,
                    click_count=1,
                )
            )
            await asyncio.sleep(0.1)
            await page.send(
                cdp_input.dispatch_mouse_event(
                    type_='mouseReleased',
                    x=x,
                    y=y,
                    button=cdp_input.MouseButton.LEFT,
                    click_count=1,
                )
            )

            for i in range(30):
                token = await page.evaluate("""
                    (() => {
                        const el = document.querySelector('[name="cf-turnstile-response"]');
                        return el && el.value && el.value.length > 10 ? 'ok' : '';
                    })()
                """)
                if token:
                    logger.info(f'[HappAutoreg] Turnstile решена за {i + 1}с')
                    return True
                await asyncio.sleep(1)

            logger.warning('[HappAutoreg] Turnstile не решилась за 30с')
            return False
        except Exception as e:
            logger.warning(f'[HappAutoreg] Ошибка Turnstile: {e}')
            return True

    async def login(self, email: str, password: str) -> bool:
        page = await self._browser.get(f'{HAPP_BASE}/security/login')
        await asyncio.sleep(3)

        try:
            url_after_nav = ''
            try:
                result = await page.evaluate('window.location.href')
                url_after_nav = str(result) if result else ''
            except Exception:
                pass

            logger.info(f'[HappAutoreg] Страница логина URL: {url_after_nav}')

            if url_after_nav and 'login' not in url_after_nav and 'signup' not in url_after_nav:
                logger.info(f'[HappAutoreg] Уже залогинены (редирект → {url_after_nav})')
                return True

            body_text = ''
            try:
                result = await page.evaluate('document.body.innerText')
                body_text = str(result) if result else ''
            except Exception:
                pass

            if body_text and any(s in body_text.lower() for s in ['dashboard', 'provider', 'дашборд', 'профиль']):
                logger.info('[HappAutoreg] Уже залогинены (обнаружен дашборд)')
                return True

            email_field = await page.select("input[type='email']", timeout=10)
            if not email_field:
                try:
                    email_field = await page.find('E-mail', best_match=True)
                except Exception:
                    pass
            if not email_field:
                try:
                    email_field = await page.select("input[name*='email']", timeout=3)
                except Exception:
                    pass
            if not email_field:
                try:
                    email_field = await page.select("input[type='text']", timeout=3)
                except Exception:
                    pass

            if not email_field:
                logger.warning(f'[HappAutoreg] Поле email не найдено. URL={url_after_nav}, текст: {body_text[:200]}')
                try:
                    await page.save_screenshot('/tmp/login_debug.png')  # noqa: S108
                except Exception:
                    pass
                if url_after_nav and 'login' not in url_after_nav:
                    logger.info('[HappAutoreg] Считаем что уже залогинены')
                    return True
                return False

            await email_field.click()
            await email_field.send_keys(email)
            await asyncio.sleep(0.5)

            pass_field = await page.select("input[type='password']", timeout=5)
            if not pass_field:
                logger.error('[HappAutoreg] Поле пароля не найдено на странице логина')
                return False
            await pass_field.click()
            await pass_field.send_keys(password)
            await asyncio.sleep(0.5)

            await self._click_turnstile(page)

            submit = await page.select("button[type='submit']", timeout=5)
            if not submit:
                submit = await page.find('Log in', best_match=True)
            if not submit:
                submit = await page.find('Sign in', best_match=True)
            if not submit:
                submit = await page.find('Войти', best_match=True)
            if not submit:
                all_buttons = await page.select_all('button')
                if all_buttons:
                    submit = all_buttons[-1]

            if not submit:
                logger.error('[HappAutoreg] Кнопка логина не найдена')
                return False

            await submit.click()
            await asyncio.sleep(3)

            url = ''
            try:
                result = await page.evaluate('window.location.href')
                url = str(result) if result else ''
            except Exception:
                pass
            if not url or 'login' not in url or 'dashboard' in url:
                logger.info(f'[HappAutoreg] Логин успешен → {url}')
                return True
            logger.warning(f'[HappAutoreg] Логин не удался → {url}')
            return False
        except Exception as e:
            logger.error(f'[HappAutoreg] Ошибка логина: {e}')
            return False

    async def _dismiss_onboarding(self, page) -> None:
        """Закрывает обучающий тур (onboarding), если он появился."""
        try:
            has_overlay = await page.evaluate("""
                (() => {
                    const el = document.querySelector('.introjs-overlay, .introjs-helperLayer, .shepherd-modal-overlay-container, [class*="onboard"], [class*="tour"]');
                    return el ? 'yes' : '';
                })()
            """)
            if not has_overlay:
                return

            dismissed = await page.evaluate("""
                (() => {
                    const skip = document.querySelector('.introjs-skipbutton, .introjs-donebutton, [class*="skip"], [class*="Skip"]');
                    if (skip) { skip.click(); return 'skip'; }
                    const overlay = document.querySelector('.introjs-overlay');
                    if (overlay) { overlay.remove(); }
                    const layers = document.querySelectorAll('.introjs-helperLayer, .introjs-tooltipReferenceLayer, .introjs-tooltip, .introjs-overlay, .introjs-fixedTooltip');
                    layers.forEach(l => l.remove());
                    return layers.length > 0 ? 'removed' : '';
                })()
            """)
            if dismissed:
                logger.info(f'[HappAutoreg] Onboarding-тур закрыт ({dismissed})')
                await asyncio.sleep(1)
        except Exception:
            pass

    async def get_credentials(self) -> dict[str, str]:
        result = {'provider_id': '', 'auth_key': ''}

        pages_to_check = [None, '/', '/dashboard', '/profile']
        for url_path in pages_to_check:
            try:
                if url_path is not None:
                    page = await self._browser.get(f'{HAPP_BASE}{url_path}')
                    await asyncio.sleep(3)
                else:
                    page = list(self._browser.tabs)[-1] if self._browser.tabs else None
                    if not page:
                        continue

                await self._dismiss_onboarding(page)

                r = await page.evaluate('document.body.innerHTML')
                body = str(r) if r else ''

                inner_text = ''
                try:
                    r2 = await page.evaluate('document.body.innerText')
                    inner_text = str(r2) if r2 else ''
                except Exception:
                    pass
            except Exception as e:
                logger.debug(f'[HappAutoreg] get_credentials({url_path}): {e}')
                continue

            if not body:
                continue

            _ui_words = {
                'copied',
                'copy',
                'click',
                'button',
                'close',
                'submit',
                'provider',
                'string',
                'number',
                'hidden',
                'domain',
                'balance',
                'tariff',
                'delete',
                'filter',
                'limited',
                'domains',
                'tariffs',
                'auction',
                'contacts',
                'payment',
                'history',
                'remote',
                'statistics',
                'documentation',
            }

            pid_match = re.search(r'Provider\s*ID[\s\xa0]+([A-Za-z0-9]{8})', inner_text)
            if not pid_match:
                pid_match = re.search(r'Provider\s*ID\s*\n[\s\xa0]*\n?\s*([A-Za-z0-9]{8})', inner_text)
            if pid_match:
                candidate = pid_match.group(1)
                if candidate.lower() not in _ui_words:
                    result['provider_id'] = candidate
                    logger.info(f'[HappAutoreg] Provider ID из innerText: {candidate}')

            if not result['provider_id']:
                for pattern in [
                    r'data-clipboard-text="([A-Za-z0-9]{8})"',
                    r'provider[_\-]?id["\s:=]+([A-Za-z0-9]{8})',
                    r'Provider\s*ID[\s\S]{0,30}?>([A-Za-z0-9]{8})<',
                ]:
                    for m in re.finditer(pattern, body, re.IGNORECASE):
                        candidate = m.group(1)
                        if candidate.lower() not in _ui_words:
                            result['provider_id'] = candidate
                            break
                    if result['provider_id']:
                        break

            for pattern in [
                r'[Aa]uth\s*[Kk]ey[^<]{0,30}?([-_A-Za-z0-9]{32,})',
                r'auth[_\-]?key["\s:=]+([-_A-Za-z0-9]{32,})',
                r'[Ss]ecret\s*[Kk]ey[^<]{0,30}?([-_A-Za-z0-9]{32,})',
            ]:
                auth_match = re.search(pattern, body, re.IGNORECASE)
                if auth_match:
                    result['auth_key'] = auth_match.group(1)
                    break

            if result['provider_id']:
                logger.info(f'[HappAutoreg] Provider ID: {result["provider_id"]} (из {url_path or "текущей страницы"})')
                break

        if not result['provider_id']:
            logger.warning('[HappAutoreg] Provider ID не найден ни на одной странице')
            try:
                page = list(self._browser.tabs)[-1] if self._browser.tabs else None
                if page:
                    await page.save_screenshot('/tmp/provider_id_debug.png')  # noqa: S108
            except Exception:
                pass
        return result

    async def add_domain_via_ui(self, domain: str) -> bool:
        """Добавление домена: XHR POST на /lk-domain/create-domain."""
        try:
            page = await self._browser.get(f'{HAPP_BASE}/domains')
            await asyncio.sleep(3)

            await self._dismiss_onboarding(page)
            await asyncio.sleep(1)

            domain_clean = domain.strip().lower()
            if domain_clean.startswith('http'):
                from urllib.parse import urlparse

                domain_clean = urlparse(domain_clean).hostname or domain_clean

            domain_hash = hashlib.sha256(domain_clean.encode()).hexdigest()

            create_result = await page.evaluate(f"""
                (() => {{
                    const csrf = document.querySelector('meta[name="csrf-token"]');
                    if (!csrf) return 'NO_CSRF';
                    const token = csrf.getAttribute('content');

                    const body = 'domain_name='
                        + '&domain_for_hash=' + encodeURIComponent('{domain_clean}')
                        + '&domain_hash=' + encodeURIComponent('{domain_hash}');

                    const xhr = new XMLHttpRequest();
                    xhr.open('POST', '/lk-domain/create-domain', false);
                    xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
                    xhr.setRequestHeader('X-CSRF-Token', token);
                    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
                    xhr.send(body);

                    return 'status:' + xhr.status + ' resp:' + xhr.responseText.substring(0, 300);
                }})()
            """)
            logger.info(f'[HappAutoreg] Создание домена: {create_result}')

            if 'status:200' not in str(create_result):
                logger.warning(f'[HappAutoreg] Домен не добавлен (не 200): {create_result}')
                return False

            await asyncio.sleep(2)
            page = await self._browser.get(f'{HAPP_BASE}/domains')
            await asyncio.sleep(3)

            body = ''
            try:
                r = await page.evaluate('document.body.innerText')
                body = str(r) if r else ''
            except Exception:
                pass

            if domain_hash[:12] in body.lower():
                logger.info(f'[HappAutoreg] Домен {domain_clean} добавлен')
                return True

            logger.warning(f'[HappAutoreg] Домен не найден после добавления: {body[:500]}')
            return False

        except Exception as e:
            logger.error(f'[HappAutoreg] Ошибка добавления домена: {e}')
            return False


# ── Cloudflare Turnstile Solver (rucaptcha, fallback) ─────────────────────────


class TurnstileSolver:
    """Решает Cloudflare Turnstile через API rucaptcha.com / 2captcha.com."""

    BASE_URL = 'https://rucaptcha.com'
    SITEKEY = '0x4AAAAAABDsyzR4rH6jFIAj'

    def __init__(self, http: aiohttp.ClientSession, api_key: str):
        self._http = http
        self._api_key = api_key

    async def solve(self, page_url: str) -> str | None:
        try:
            task_id = await self._create_task(page_url)
            if not task_id:
                return None
            return await self._poll_result(task_id)
        except Exception as e:
            logger.error(f'[HappAutoreg] Ошибка решения Turnstile: {e}')
            return None

    async def _create_task(self, page_url: str) -> str | None:
        params = {
            'key': self._api_key,
            'method': 'turnstile',
            'sitekey': self.SITEKEY,
            'pageurl': page_url,
            'json': '1',
        }
        async with self._http.post(f'{self.BASE_URL}/in.php', data=params) as resp:
            data = await resp.json(content_type=None)
            if data.get('status') == 1:
                logger.info(f'[HappAutoreg] Turnstile отправлена, task={data.get("request")}')
                return data.get('request')
            logger.warning(f'[HappAutoreg] Turnstile не принята: {data}')
            return None

    async def _poll_result(self, task_id: str) -> str | None:
        await asyncio.sleep(10)
        elapsed = 10
        while elapsed < CAPTCHA_MAX_WAIT:
            params = {'key': self._api_key, 'action': 'get', 'id': task_id, 'json': '1'}
            async with self._http.get(f'{self.BASE_URL}/res.php', params=params) as resp:
                data = await resp.json(content_type=None)
            if data.get('status') == 1:
                logger.info(f'[HappAutoreg] Turnstile решена за ~{elapsed}с')
                return data.get('request', '')
            if data.get('request') == 'CAPCHA_NOT_READY':
                await asyncio.sleep(CAPTCHA_POLL_INTERVAL)
                elapsed += CAPTCHA_POLL_INTERVAL
                continue
            logger.warning(f'[HappAutoreg] Ошибка Turnstile: {data}')
            return None
        logger.warning(f'[HappAutoreg] Таймаут Turnstile ({CAPTCHA_MAX_WAIT}с)')
        return None


# ── HTTP Registration (fallback with rucaptcha) ──────────────────────────────


class HappHttpClient:
    """Регистрация через HTTP + rucaptcha (платный fallback)."""

    def __init__(self, http: aiohttp.ClientSession, captcha_api_key: str):
        self._http = http
        self._captcha_api_key = captcha_api_key
        self._reg_email = ''
        self._reg_password = ''

    async def register_step1(self, email: str, password: str) -> bool:
        """Этап 1: отправка формы регистрации → ожидание поля кода."""
        self._reg_email = email
        self._reg_password = password
        csrf = await self._get_csrf('/security/signup')
        if not csrf:
            return False

        solver = TurnstileSolver(self._http, self._captcha_api_key)
        token = await solver.solve(f'{HAPP_BASE}/security/signup')
        if not token:
            return False

        form_data = {
            '_csrf': csrf,
            'SignupForm[email]': email,
            'SignupForm[password]': password,
            'SignupForm[privacy_policy]': '1',
            'cf-turnstile-response': token,
            'SignupForm[captcha]': token,
            'signup-button': '',
        }
        headers = {
            'User-Agent': BROWSER_UA,
            'Referer': f'{HAPP_BASE}/security/signup',
            'Origin': HAPP_BASE,
        }
        async with self._http.post(
            f'{HAPP_BASE}/security/signup',
            data=form_data,
            headers=headers,
            allow_redirects=True,
        ) as resp:
            self._last_body = await resp.text()
            self._last_url = str(resp.url)

            body_lower = self._last_body.lower()
            has_code_field = (
                'signupform[code]' in body_lower
                or 'confirmation_code' in body_lower
                or 'confirmation code' in body_lower
                or 'enter the code' in body_lower
                or 'введите код' in body_lower
            )
            if has_code_field:
                fields = re.findall(r'name=["\']([^"\']*SignupForm[^"\']*)["\']', self._last_body)
                logger.info(f'[HappAutoreg] Этап 1 ОК — форма подтверждения, поля: {fields}')
                return True

            if 'signup' not in self._last_url:
                logger.info(f'[HappAutoreg] Регистрация сразу прошла → {self._last_url}')
                return True

            errs = [m.strip() for m in re.findall(r'invalid-feedback[^>]*>([^<]+)<', self._last_body) if m.strip()]
            alert = re.search(r'class="[^"]*alert-danger[^"]*"[^>]*>(.*?)</\w+>', self._last_body, re.DOTALL)
            alert_text = re.sub(r'<[^>]+>', '', alert.group(1)).strip() if alert else ''
            logger.warning(
                f'[HappAutoreg] Этап 1 не удался: url={self._last_url}, '
                f'errs={errs}, alert={alert_text!r}, '
                f'body_len={len(self._last_body)}'
            )
            return False

    async def register_step2(self, code: str) -> bool:
        """Этап 2: повторная отправка полной формы с pin_code."""
        csrf = ''
        if hasattr(self, '_last_body') and self._last_body:
            m = re.search(r'name=["\']_csrf["\'][^>]*value=["\']([^"\']+)', self._last_body)
            if not m:
                m = re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']_csrf', self._last_body)
            if m:
                csrf = m.group(1)

        if not csrf:
            csrf = await self._get_csrf('/security/signup')

        solver = TurnstileSolver(self._http, self._captcha_api_key)
        token = await solver.solve(f'{HAPP_BASE}/security/signup')

        form_data = {
            '_csrf': csrf,
            'SignupForm[email]': self._reg_email,
            'SignupForm[password]': self._reg_password,
            'SignupForm[privacy_policy]': '1',
            'SignupForm[pin_code]': code,
            'signup-button': '',
        }
        if token:
            form_data['cf-turnstile-response'] = token
            form_data['SignupForm[captcha]'] = token

        headers = {
            'User-Agent': BROWSER_UA,
            'Referer': f'{HAPP_BASE}/security/signup',
            'Origin': HAPP_BASE,
        }
        async with self._http.post(
            f'{HAPP_BASE}/security/signup',
            data=form_data,
            headers=headers,
            allow_redirects=True,
        ) as resp:
            body = await resp.text()
            url = str(resp.url)

            if 'signup' not in url:
                logger.info(f'[HappAutoreg] Регистрация завершена → {url}')
                return True

            has_login_form = 'LoginForm' in body
            if has_login_form:
                logger.info('[HappAutoreg] Регистрация завершена — форма логина')
                return True

            has_pin_code = 'pin_code' in body.lower()
            errs = [m.strip() for m in re.findall(r'invalid-feedback[^>]*>([^<]+)<', body) if m.strip()]
            logger.warning(
                f'[HappAutoreg] Этап 2: url={url}, errs={errs}, still_pin_code={has_pin_code}, body_len={len(body)}'
            )
            return False

    async def login(self, email: str, password: str) -> bool:
        csrf = await self._get_csrf('/security/login')
        if not csrf:
            return False

        form_data = {
            '_csrf': csrf,
            'LoginForm[email]': email,
            'LoginForm[password]': password,
            'LoginForm[rememberMe]': '1',
            'login-button': '',
        }
        headers = {
            'User-Agent': BROWSER_UA,
            'Referer': f'{HAPP_BASE}/security/login',
            'Origin': HAPP_BASE,
        }
        async with self._http.post(
            f'{HAPP_BASE}/security/login',
            data=form_data,
            headers=headers,
            allow_redirects=True,
        ) as resp:
            url = str(resp.url)
            body = await resp.text()

            if 'login' not in url or 'dashboard' in url:
                logger.info(f'[HappAutoreg] Логин успешен → {url}')
                return True

            errs = [m.strip() for m in re.findall(r'invalid-feedback[^>]*>([^<]+)<', body) if m.strip()]
            logger.warning(f'[HappAutoreg] Логин не удался → {url}, errors={errs}')
            return False

    async def get_credentials(self) -> dict[str, str]:
        result = {'provider_id': '', 'auth_key': ''}
        try:
            async with self._http.get(
                f'{HAPP_BASE}/',
                headers={'User-Agent': BROWSER_UA},
                allow_redirects=True,
            ) as resp:
                body = await resp.text()
                final_url = str(resp.url)
        except Exception as e:
            logger.error(f'[HappAutoreg] get_credentials ошибка: {e}')
            return result

        if 'login' in final_url:
            logger.warning('[HappAutoreg] get_credentials → редирект на логин (не авторизован)')
            return result

        m = re.search(r'id=["\']provider-id-text["\'][^>]*>([A-Za-z0-9]{8})<', body)
        if m:
            result['provider_id'] = m.group(1)
            logger.info(f'[HappAutoreg] Provider ID: {result["provider_id"]}')
        else:
            logger.warning(f'[HappAutoreg] Provider ID не найден в HTML, url={final_url}, body_len={len(body)}')

        am = re.search(r'auth[_\-]?key["\s:=]+([-_A-Za-z0-9]{32,})', body, re.IGNORECASE)
        if am:
            result['auth_key'] = am.group(1)

        return result

    async def add_domain_via_session(self, domain: str) -> bool:
        """Добавление домена через сессию (POST /lk-domain/create-domain)."""
        try:
            async with self._http.get(
                f'{HAPP_BASE}/domains',
                headers={'User-Agent': BROWSER_UA},
                allow_redirects=True,
            ) as resp:
                page_body = await resp.text()

            csrf = ''
            m = re.search(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)', page_body)
            if m:
                csrf = m.group(1)
            if not csrf:
                m = re.search(r'name=["\']_csrf["\'][^>]*value=["\']([^"\']+)', page_body)
                if m:
                    csrf = m.group(1)
            if not csrf:
                logger.warning('[HappAutoreg] add_domain: CSRF не найден на /domains')
                return False

            domain_clean = domain.strip().lower()
            if domain_clean.startswith('http'):
                from urllib.parse import urlparse

                domain_clean = urlparse(domain_clean).hostname or domain_clean

            domain_hash = hashlib.sha256(domain_clean.encode()).hexdigest()

            form_data = {
                'domain_name': '',
                'domain_for_hash': domain_clean,
                'domain_hash': domain_hash,
            }
            headers = {
                'User-Agent': BROWSER_UA,
                'Referer': f'{HAPP_BASE}/domains',
                'X-CSRF-Token': csrf,
                'X-Requested-With': 'XMLHttpRequest',
            }
            async with self._http.post(
                f'{HAPP_BASE}/lk-domain/create-domain',
                data=form_data,
                headers=headers,
                allow_redirects=True,
            ) as resp:
                text = await resp.text()
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {'raw': text[:300]}

                if isinstance(data, dict) and data.get('rc') == 1:
                    logger.info(f'[HappAutoreg] Домен {domain_clean} добавлен через сессию')
                    return True

                msg = data.get('msg', '') if isinstance(data, dict) else ''
                if 'already' in str(msg).lower() or 'exist' in str(msg).lower():
                    logger.info(f'[HappAutoreg] Домен {domain_clean} уже существует')
                    return True

                logger.warning(f'[HappAutoreg] add_domain ответ: {data}')
                return False

        except Exception as e:
            logger.error(f'[HappAutoreg] add_domain ошибка: {e}')
            return False

    async def _get_csrf(self, path: str) -> str:
        try:
            async with self._http.get(
                f'{HAPP_BASE}{path}',
                headers={'User-Agent': BROWSER_UA},
                allow_redirects=True,
            ) as resp:
                body = await resp.text()
                m = re.search(r'name=["\']_csrf["\'][^>]*value=["\']([^"\']+)', body)
                if not m:
                    m = re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']_csrf', body)
                if not m:
                    m = re.search(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)', body)
                if m:
                    return m.group(1)
                logger.warning(f'[HappAutoreg] CSRF не найден на {path} (url={resp.url})')
                return ''
        except Exception as e:
            logger.error(f'[HappAutoreg] Ошибка загрузки {path}: {e}')
            return ''


# ── Domain binding via API ────────────────────────────────────────────────────


async def add_domain_via_api(
    http: aiohttp.ClientSession,
    provider_code: str,
    auth_key: str,
    domain: str,
) -> bool:
    domain_clean = domain.strip().lower()
    if domain_clean.startswith('http'):
        from urllib.parse import urlparse

        domain_clean = urlparse(domain_clean).hostname or domain_clean
    domain_hash = hashlib.sha256(domain_clean.encode()).hexdigest()
    url = f'{HAPP_BASE}/api/add-domain?provider_code={provider_code}&auth_key={auth_key}&domain_hash={domain_hash}'
    try:
        async with http.get(url) as resp:
            data = await resp.json()
            if data.get('rc') == 1:
                logger.info(f'[HappAutoreg] Домен {domain_clean} привязан')
                return True
            msg = data.get('msg', '')
            if 'already' in msg.lower() or 'exist' in msg.lower():
                logger.info(f'[HappAutoreg] Домен {domain_clean} уже привязан')
                return True
            logger.warning(f'[HappAutoreg] add-domain: {data}')
            return False
    except Exception as e:
        logger.error(f'[HappAutoreg] Ошибка привязки домена: {e}')
        return False


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_code_from_msg(mail: TempMailClient, msg: dict, tag: str = '') -> str | None:
    """Извлекает код подтверждения из сообщения mail.tm."""
    body_html = msg.get('html', '') or ''
    body_text = msg.get('text', '') or ''
    subject = msg.get('subject', '') or ''
    if isinstance(body_html, list):
        body_html = ' '.join(str(t) for t in body_html)
    if isinstance(body_text, list):
        body_text = ' '.join(str(t) for t in body_text)

    code = mail.extract_code(f'{subject} {body_text} {body_html}')
    logger.info(f'[HappAutoreg] {tag} Письмо: code={code}')
    return code


def _build_account_dict(
    email: str,
    password: str,
    creds: dict[str, str],
    domain: str,
    domain_ok: bool,
) -> dict[str, Any]:
    return {
        'email': email,
        'password': password,
        'provider_id': creds['provider_id'],
        'auth_key': creds.get('auth_key', ''),
        'domain': domain if domain_ok else '',
        'registered_at': datetime.now(UTC).isoformat(),
    }


# ── Main orchestrator ─────────────────────────────────────────────────────────


ProgressCallback = Callable[[int, int, int, int], Awaitable[None]]


def get_available_method() -> str:
    """Определяет лучший доступный метод: 'nodriver', 'http', или 'none'."""
    if _check_nodriver() and _find_system_chrome():
        return 'nodriver'
    return 'http'


async def auto_register(
    count: int,
    domain: str,
    progress_cb: ProgressCallback | None = None,
    captcha_api_key: str = '',
    preferred_method: str = 'auto',
) -> list[dict[str, Any]]:
    """
    Регистрирует N аккаунтов на happ-proxy.com.

    preferred_method: 'auto', 'http', 'nodriver'.
    """
    has_nodriver = get_available_method() == 'nodriver'
    has_http = bool(captcha_api_key)

    if preferred_method == 'http':
        if has_http:
            logger.info('[HappAutoreg] Используем HTTP + rucaptcha (выбор пользователя)')
            return await _register_via_http(count, domain, progress_cb, captcha_api_key)
        raise RuntimeError('Для HTTP-метода укажите API-ключ от rucaptcha.com в настройках.')

    if preferred_method == 'nodriver':
        if has_nodriver:
            logger.info('[HappAutoreg] Используем nodriver (выбор пользователя)')
            return await _register_via_nodriver(count, domain, progress_cb)
        raise RuntimeError('nodriver недоступен. Установите: pip install nodriver && apt install chromium xvfb')

    if has_http:
        logger.info('[HappAutoreg] Используем HTTP + rucaptcha (авто)')
        return await _register_via_http(count, domain, progress_cb, captcha_api_key)
    if has_nodriver:
        logger.info('[HappAutoreg] Используем nodriver (авто)')
        return await _register_via_nodriver(count, domain, progress_cb)

    raise RuntimeError(
        'Нет доступного метода регистрации.\n\n'
        '① HTTP (быстрый): укажите API-ключ от rucaptcha.com\n'
        '② Браузер: pip install nodriver && apt install chromium xvfb'
    )


_browser_start_lock = asyncio.Lock()
_mailbox_lock = asyncio.Lock()


async def _register_one_nodriver(
    idx: int,
    count: int,
    domain: str,
    http: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    progress_cb: ProgressCallback | None,
) -> dict[str, Any] | None:
    """Регистрация одного аккаунта (запускается параллельно)."""
    w = idx + 1
    tag = f'[{w}/{count}]'

    async def _progress(step: int):
        if progress_cb:
            await progress_cb(w, count, step, AUTOREG_STEPS)

    async with semaphore:
        await _progress(1)
        try:
            async with _mailbox_lock:
                mail = TempMailClient(http)
                email = await mail.create_mailbox()
                await asyncio.sleep(3)
            happ_password = secrets.token_urlsafe(12)
        except Exception as e:
            logger.error(f'[HappAutoreg] {tag} Ошибка ящика: {e}')
            await _progress(-1)
            return None

        await _progress(2)
        reg_cm = NodriverRegistrar()
        reg = None
        try:
            browser_ok = False
            for attempt in range(2):
                try:
                    async with _browser_start_lock:
                        reg = await reg_cm.__aenter__()
                        await asyncio.sleep(2)
                    browser_ok = True
                    break
                except Exception as e:
                    logger.warning(f'[HappAutoreg] {tag} Браузер попытка {attempt + 1}/2: {e}')
                    try:
                        await reg_cm.__aexit__(None, None, None)
                    except Exception:
                        pass
                    if attempt == 0:
                        await asyncio.sleep(5)

            if not browser_ok:
                logger.error(f'[HappAutoreg] {tag} Не удалось запустить браузер')
                await _progress(-1)
                return None

            await _progress(3)
            if not await reg.register_step1(email, happ_password):
                logger.warning(f'[HappAutoreg] {tag} Этап 1 не удался для {email}')
                await _progress(-1)
                return None

            await _progress(4)
            try:
                msg = await mail.wait_for_email(timeout=120)
            except TimeoutError:
                logger.warning(f'[HappAutoreg] {tag} Письмо не пришло для {email}')
                await _progress(-1)
                return None

            code = _extract_code_from_msg(mail, msg, tag)
            if not code:
                logger.warning(f'[HappAutoreg] {tag} Код не найден в письме')
                await _progress(-1)
                return None

            await _progress(5)
            if not await reg.register_step2(code):
                logger.warning(f'[HappAutoreg] {tag} Этап 2 не удался')
                await _progress(-1)
                return None
            logger.info(f'[HappAutoreg] {tag} Регистрация OK для {email}')

            await _progress(6)
            creds = await reg.get_credentials()
            if not creds.get('provider_id'):
                logger.info(f'[HappAutoreg] {tag} Provider ID не найден, пробуем логин...')
                if await reg.login(email, happ_password):
                    creds = await reg.get_credentials()
            if not creds.get('provider_id'):
                logger.warning(f'[HappAutoreg] {tag} Provider ID не найден')
                await _progress(-1)
                return None

            domain_ok = False
            if domain:
                await _progress(7)
                if creds.get('auth_key'):
                    domain_ok = await add_domain_via_api(http, creds['provider_id'], creds['auth_key'], domain)
                if not domain_ok:
                    domain_ok = await reg.add_domain_via_ui(domain)

            await _progress(AUTOREG_STEPS + 1)
            account = _build_account_dict(email, happ_password, creds, domain, domain_ok)
            logger.info(f'[HappAutoreg] {tag} {creds["provider_id"]}, домен: {"ok" if domain_ok else "fail"}')
            return account

        except Exception as e:
            logger.error(f'[HappAutoreg] {tag} Ошибка: {e}')
            await _progress(-1)
            return None
        finally:
            try:
                await reg_cm.__aexit__(None, None, None)
            except Exception:
                pass


async def _register_via_nodriver(
    count: int,
    domain: str,
    progress_cb: ProgressCallback | None,
) -> list[dict[str, Any]]:
    workers = min(PARALLEL_WORKERS, count)
    semaphore = asyncio.Semaphore(workers)
    logger.info(f'[HappAutoreg] Запуск {count} регистраций ({workers} параллельно)')

    vdisplay: _VirtualDisplay | None = None
    if _VirtualDisplay.is_available():
        vdisplay = _VirtualDisplay()
        vdisplay.start()

    try:
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            tasks = [_register_one_nodriver(i, count, domain, http, semaphore, progress_cb) for i in range(count)]
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if vdisplay:
            vdisplay.stop()

    results = []
    for r in raw_results:
        if isinstance(r, dict):
            results.append(r)
        elif isinstance(r, Exception):
            logger.error(f'[HappAutoreg] Ошибка в задаче: {r}')

    return results


HTTP_PARALLEL_WORKERS = 5


async def _http_register_one(
    idx: int,
    count: int,
    domain: str,
    captcha_api_key: str,
    semaphore: asyncio.Semaphore,
    mail_lock: asyncio.Lock,
    progress_cb: ProgressCallback | None,
) -> dict[str, Any] | None:
    w = idx + 1
    tag = f'[{w}/{count}]'
    ts = AUTOREG_STEPS

    async def _progress(step: int):
        if progress_cb:
            await progress_cb(w, count, step, ts)

    async with semaphore:
        await _progress(1)

        mail_timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=mail_timeout) as http:
            try:
                async with mail_lock:
                    mail = TempMailClient(http)
                    email = await mail.create_mailbox()
                    await asyncio.sleep(1)
                happ_password = secrets.token_urlsafe(12)
            except Exception as e:
                logger.error(f'[HappAutoreg] {tag} Ошибка ящика: {e}')
                await _progress(-1)
                return None

            await _progress(2)

            jar = aiohttp.CookieJar()
            reg_timeout = aiohttp.ClientTimeout(total=300)
            async with aiohttp.ClientSession(cookie_jar=jar, timeout=reg_timeout) as rhttp:
                client = HappHttpClient(rhttp, captcha_api_key)
                try:
                    if not await client.register_step1(email, happ_password):
                        logger.warning(f'[HappAutoreg] {tag} Этап 1 не удался для {email}')
                        await _progress(-1)
                        return None

                    await _progress(4)
                    try:
                        msg = await mail.wait_for_email(timeout=120)
                    except TimeoutError:
                        logger.warning(f'[HappAutoreg] {tag} Письмо не пришло')
                        await _progress(-1)
                        return None

                    code = _extract_code_from_msg(mail, msg, tag)
                    if not code:
                        logger.warning(f'[HappAutoreg] {tag} Код не найден')
                        await _progress(-1)
                        return None

                    await _progress(5)
                    if not await client.register_step2(code):
                        logger.warning(f'[HappAutoreg] {tag} Этап 2 не прошёл')
                        await _progress(-1)
                        return None
                    logger.info(f'[HappAutoreg] {tag} Регистрация OK для {email}')

                    await _progress(6)
                    creds = await client.get_credentials()
                    if not creds.get('provider_id'):
                        logger.info(f'[HappAutoreg] {tag} Сессия не авторизована, логинимся...')
                        if not await client.login(email, happ_password):
                            logger.warning(f'[HappAutoreg] {tag} Логин не удался')
                            await _progress(-1)
                            return None
                        creds = await client.get_credentials()

                    if not creds.get('provider_id'):
                        logger.warning(f'[HappAutoreg] {tag} Provider ID не найден')
                        await _progress(-1)
                        return None

                    domain_ok = False
                    if domain:
                        await _progress(7)
                        domain_ok = await client.add_domain_via_session(domain)
                        if not domain_ok and creds.get('auth_key'):
                            domain_ok = await add_domain_via_api(rhttp, creds['provider_id'], creds['auth_key'], domain)

                    await _progress(ts + 1)
                    account = _build_account_dict(email, happ_password, creds, domain, domain_ok)
                    logger.info(f'[HappAutoreg] {tag} {creds["provider_id"]}, домен: {"ok" if domain_ok else "fail"}')
                    return account

                except Exception as e:
                    logger.error(f'[HappAutoreg] {tag} Ошибка: {e}')
                    await _progress(-1)
                    return None


async def _register_via_http(
    count: int,
    domain: str,
    progress_cb: ProgressCallback | None,
    captcha_api_key: str,
) -> list[dict[str, Any]]:
    workers = min(HTTP_PARALLEL_WORKERS, count)
    semaphore = asyncio.Semaphore(workers)
    mail_lock = asyncio.Lock()
    logger.info(f'[HappAutoreg] HTTP: запуск {count} регистраций ({workers} параллельно)')

    tasks = [
        _http_register_one(i, count, domain, captcha_api_key, semaphore, mail_lock, progress_cb) for i in range(count)
    ]
    raw = await asyncio.gather(*tasks, return_exceptions=True)
    results = [r for r in raw if isinstance(r, dict)]
    return results
