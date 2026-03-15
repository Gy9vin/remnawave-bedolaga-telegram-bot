"""
Синхронизация настроек Happ Management с Remnawave через REST API.

При каждом изменении настроек модуля вызывается sync_to_remnawave(), который:
1. Получает список уникальных Remnawave-панелей из БД
2. Авторизуется (по токену или логин/пароль)
3. Читает текущие subscription-settings (GET) для получения uuid
4. Собирает customResponseHeaders из настроек модуля
5. Объединяет с чужими заголовками (сохраняя не-Happ заголовки)
6. Полностью заменяет customResponseHeaders (PATCH), обновляет happAnnounce и hosts

ВАЖНО: PATCH /api/subscription-settings с customResponseHeaders ЗАМЕНЯЕТ весь объект,
а не мёржит. Поэтому мы всегда отправляем полный набор: чужие + наши активные.
announce управляется через выделенное поле happAnnounce (не через customResponseHeaders).
"""

import asyncio
import base64
import re

import aiohttp
import structlog

from . import config as cfg


logger = structlog.get_logger(__name__)


def _encode_header_value(value: str) -> str:
    """
    HTTP/2 запрещает не-ASCII символы в заголовках.
    Кириллица и прочий UTF-8 кодируется в base64.
    """
    try:
        value.encode('ascii')
        return value
    except UnicodeEncodeError:
        encoded = base64.b64encode(value.encode('utf-8')).decode('ascii')
        return f'base64:{encoded}'


MANAGED_HEADER_KEYS = frozenset(
    {
        'providerid',
        'hide-settings',
        'sub-expire',
        'sub-expire-button-link',
        'sub-info-text',
        'sub-info-color',
        'sub-info-button-text',
        'sub-info-button-link',
        'color-profile',
        'subscription-always-hwid-enable',
        'notification-subs-expire',
        'subscription-auto-update-enable',
        'fragmentation-enable',
        'fragmentation-packets',
        'fragmentation-length',
        'fragmentation-interval',
        'fragmentation-maxsplit',
        'noises-enable',
        'noises-type',
        'noises-packet',
        'noises-delay',
        'noises-applyto',
        'change-user-agent',
        'mux-enable',
        'mux-tcp-connections',
        'mux-xudp-connections',
        'mux-quic',
        'subscription-autoconnect',
        'subscription-autoconnect-type',
        'subscription-ping-onopen-enabled',
        'subscription-auto-update-open-enable',
        'ping-type',
        'check-url-via-proxy',
        'ping-result',
        'subscriptions-collapse',
        'subscriptions-expand-now',
    }
)

REMNAWAVE_NATIVE_HEADER_KEYS = frozenset(
    {
        'announce',
        'routing',
        'content-disposition',
        'support-url',
        'profile-title',
        'profile-update-interval',
        'subscription-userinfo',
        'profile-web-page-url',
        'subscription-refill-date',
        'x-hwid-limit',
    }
)


def _normalize_panel_url(api_url: str) -> str:
    url = api_url.rstrip('/')
    url = re.sub(r'/api$', '', url)
    return url


async def _get_remnawave_api_urls() -> list[str]:
    """Возвращает URL Remnawave-панели из конфигурации."""
    from app.config import settings

    url = getattr(settings, 'REMNAWAVE_URL', None)
    if not url:
        return []
    return [_normalize_panel_url(str(url))]


async def _authenticate(http: aiohttp.ClientSession, panel_url: str) -> str | None:
    """Возвращает JWT-токен для Remnawave API."""
    from app.config import settings

    # Try access token first
    access_token = getattr(settings, 'REMNAWAVE_ACCESS_TOKEN', None)
    auth_type = getattr(settings, 'REMNAWAVE_AUTH_TYPE', 'api_key')

    if access_token and auth_type in ('api_key', 'bearer'):
        logger.debug('[HappSync] Используем токен', panel_url=panel_url)
        return access_token

    login = getattr(settings, 'REMNAWAVE_LOGIN', None)
    password = getattr(settings, 'REMNAWAVE_PASSWORD', None)

    if not login or not password:
        logger.warning(
            '[HappSync] Не заданы учётные данные Remnawave',
            token='задан' if access_token else 'пуст',
            login='задан' if login else 'пуст',
        )
        return None

    try:
        async with http.post(
            f'{panel_url}/api/auth/login',
            json={'username': login, 'password': password},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                logger.warning(
                    '[HappSync] Логин не удался',
                    status=resp.status,
                    panel_url=panel_url,
                    body=body[:200],
                )
                return None
            data = await resp.json()
            token = (data.get('response') or {}).get('accessToken')
            if token:
                logger.debug('[HappSync] Логин OK', panel_url=panel_url)
            else:
                logger.warning('[HappSync] Нет accessToken в ответе', panel_url=panel_url)
            return token
    except Exception as e:
        logger.error('[HappSync] Ошибка логина', panel_url=panel_url, error=e)
        return None


def build_custom_headers(provider_id: str | None = None) -> dict[str, str]:
    """
    Собирает dict ТОЛЬКО активных заголовков для customResponseHeaders.

    PATCH заменяет весь объект customResponseHeaders целиком,
    поэтому неактивные ключи просто не включаем — они исчезнут из Remnawave.
    announce управляется через happAnnounce (не через customResponseHeaders).

    provider_id: если указан, настройки берутся с учётом per-provider overrides.
    """
    if not cfg.get('MODULE_ENABLED'):
        return {}

    def _g(key: str):
        return cfg.get_effective(key, provider_id)

    headers: dict[str, str] = {}

    pid = provider_id or cfg.get('HAPP_PROVIDER_ID')
    if pid:
        headers['providerid'] = str(pid)

    headers['hide-settings'] = '1' if _g('HIDE_SERVER_SETTINGS') else '0'

    headers['sub-expire'] = '1' if _g('SUB_EXPIRE_ENABLED') else '0'
    if _g('SUB_EXPIRE_ENABLED'):
        link = _g('SUB_EXPIRE_BUTTON_LINK')
        if link:
            headers['sub-expire-button-link'] = str(link)

    info_text = _g('SUB_INFO_TEXT')
    if info_text:
        headers['sub-info-text'] = str(info_text)
        headers['sub-info-color'] = str(_g('SUB_INFO_COLOR') or 'blue')
        btn_text = _g('SUB_INFO_BUTTON_TEXT')
        if btn_text:
            headers['sub-info-button-text'] = str(btn_text)
        btn_link = _g('SUB_INFO_BUTTON_LINK')
        if btn_link:
            headers['sub-info-button-link'] = str(btn_link)

    color_profile = _g('COLOR_PROFILE')
    if color_profile:
        headers['color-profile'] = str(color_profile)

    headers['subscription-always-hwid-enable'] = '1' if _g('ALWAYS_HWID_ENABLED') else '0'

    if _g('DISABLE_COLLAPSE'):
        headers['subscriptions-collapse'] = '0'
        headers['subscriptions-expand-now'] = '1'

    headers['notification-subs-expire'] = '1' if _g('NOTIFICATION_SUBS_EXPIRE') else '0'

    headers['subscription-auto-update-enable'] = '1' if _g('AUTO_UPDATE_ENABLED') else '0'
    headers['subscription-auto-update-open-enable'] = '1' if _g('AUTO_UPDATE_ON_OPEN') else '0'

    headers['fragmentation-enable'] = '1' if _g('FRAGMENTATION_ENABLED') else '0'
    if _g('FRAGMENTATION_ENABLED'):
        headers['fragmentation-packets'] = str(_g('FRAGMENTATION_PACKETS') or 'tlshello')
        headers['fragmentation-length'] = str(_g('FRAGMENTATION_LENGTH') or '50-100')
        headers['fragmentation-interval'] = str(_g('FRAGMENTATION_INTERVAL') or '5')
        maxsplit = _g('FRAGMENTATION_MAXSPLIT')
        if maxsplit:
            headers['fragmentation-maxsplit'] = str(maxsplit)

    headers['noises-enable'] = '1' if _g('NOISES_ENABLED') else '0'
    if _g('NOISES_ENABLED'):
        headers['noises-type'] = str(_g('NOISES_TYPE') or 'rand')
        headers['noises-packet'] = str(_g('NOISES_PACKET') or '10-20')
        headers['noises-delay'] = str(_g('NOISES_DELAY') or '10-16')
        applyto = _g('NOISES_APPLYTO')
        if applyto:
            headers['noises-applyto'] = str(applyto)

    ua = _g('CHANGE_USER_AGENT')
    if ua:
        headers['change-user-agent'] = str(ua)

    headers['mux-enable'] = '1' if _g('MUX_ENABLED') else '0'
    if _g('MUX_ENABLED'):
        headers['mux-tcp-connections'] = str(_g('MUX_TCP_CONNECTIONS') or '8')
        headers['mux-xudp-connections'] = str(_g('MUX_XUDP_CONNECTIONS') or '8')
        mux_quic = _g('MUX_QUIC')
        if mux_quic:
            headers['mux-quic'] = str(mux_quic)

    headers['subscription-autoconnect'] = '1' if _g('AUTOCONNECT_ENABLED') else '0'
    if _g('AUTOCONNECT_ENABLED'):
        headers['subscription-autoconnect-type'] = str(_g('AUTOCONNECT_TYPE') or 'lastused')

    headers['subscription-ping-onopen-enabled'] = '1' if _g('PING_ONOPEN_ENABLED') else '0'

    ping_type = _g('PING_TYPE')
    if ping_type:
        headers['ping-type'] = str(ping_type)
        check_url = _g('PING_CHECK_URL')
        if check_url:
            headers['check-url-via-proxy'] = str(check_url)

    ping_result = _g('PING_RESULT')
    if ping_result:
        headers['ping-result'] = str(ping_result)

    for native_key in REMNAWAVE_NATIVE_HEADER_KEYS:
        headers.pop(native_key, None)

    return {k: _encode_header_value(v) for k, v in headers.items()}


def build_native_fields(*, force_clear_announce: bool = False, provider_id: str | None = None) -> dict:
    """
    Собирает нативные поля Remnawave для PATCH /api/subscription-settings.

    happAnnounce — чистый текст (без base64), Remnawave кодирует сам.
    Если ANNOUNCE_TEXT пуст — поле не включается в payload (passthrough),
    чтобы не затирать объявление, выставленное напрямую в Remnawave.
    force_clear_announce=True отправит пробел для явной очистки.
    profileUpdateInterval — интервал автообновления в часах (integer).

    provider_id: если указан, настройки берутся с учётом per-provider overrides.
    """

    def _g(key: str):
        return cfg.get_effective(key, provider_id)

    enabled = cfg.get('MODULE_ENABLED')
    announce = _g('ANNOUNCE_TEXT') if (enabled and cfg.is_announce_active(provider_id)) else ''
    fields: dict = {}
    if announce:
        fields['happAnnounce'] = announce
    elif force_clear_announce:
        fields['happAnnounce'] = ' '

    if enabled and _g('AUTO_UPDATE_ENABLED'):
        interval = _g('PROFILE_UPDATE_INTERVAL')
        if interval and str(interval).isdigit():
            fields['profileUpdateInterval'] = int(interval)

    return fields


def _build_final_headers(existing: dict | None, new_happ: dict[str, str]) -> dict[str, str]:
    """
    Собирает итоговый объект customResponseHeaders:
    чужие (не-Happ, не-native) заголовки + активные Happ-заголовки.

    PATCH с {} очищает весь объект (спецслучай в Remnawave).
    PATCH с непустым объектом — мёржит с существующими.
    Поэтому всегда делаем два шага: сначала очистка {}, затем установка нужных.
    """
    exclude = MANAGED_HEADER_KEYS | REMNAWAVE_NATIVE_HEADER_KEYS
    result = {k: v for k, v in (existing or {}).items() if k not in exclude}
    result.update(new_happ)
    return result


async def _sync_one_panel(panel_url: str, native_fields: dict | None = None) -> bool:
    """Синхронизирует настройки с одной Remnawave-панелью."""
    timeout = aiohttp.ClientTimeout(total=15)
    settings_url = f'{panel_url}/api/subscription-settings'

    logger.info(f'[HappSync] Запрос к {settings_url}')

    async with aiohttp.ClientSession(timeout=timeout) as http:
        token = await _authenticate(http, panel_url)
        if not token:
            logger.warning(f'[HappSync] Не удалось авторизоваться в {panel_url}')
            return False

        auth = {'Authorization': f'Bearer {token}'}

        # --- GET subscription-settings ---
        try:
            async with http.get(settings_url, headers=auth) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f'[HappSync] GET settings: HTTP {resp.status} ({panel_url}): {body[:300]}')
                    return False
                data = await resp.json()
                settings = data.get('response') or {}
                uuid = settings.get('uuid')
                if not uuid:
                    logger.warning(f'[HappSync] Нет uuid в subscription-settings ({panel_url})')
                    return False
                existing_headers = settings.get('customResponseHeaders') or {}
        except Exception as e:
            logger.error(f'[HappSync] GET ошибка ({panel_url}): {e}')
            return False

        new_happ = build_custom_headers()
        final_headers = _build_final_headers(existing_headers, new_happ)
        if native_fields is None:
            native_fields = build_native_fields()
        ann = native_fields.get('happAnnounce', '').strip()

        # Атомарный PATCH: отправляем ПОЛНЫЙ объект customResponseHeaders за один запрос.
        # Remnawave при непустом объекте делает merge, поэтому удалённые ключи
        # могут остаться. Но CLEAR ({}) обнуляет ВСЕ заголовки на время между
        # двумя PATCH-ами, что роняет subscription-page. Лучше оставить
        # «лишний» ключ, чем обрушить подписки.
        existing_managed = {k for k in (existing_headers or {}) if k in MANAGED_HEADER_KEYS}
        new_managed = set(new_happ.keys()) & MANAGED_HEADER_KEYS
        stale_keys = existing_managed - new_managed

        if stale_keys:
            for k in stale_keys:
                final_headers[k] = ''
            logger.info(f'[HappSync] Обнулены устаревшие ключи: {stale_keys}')

        payload: dict = {'uuid': uuid, 'customResponseHeaders': final_headers}
        payload.update(native_fields)

        set_ok = False
        for attempt in range(3):
            try:
                async with http.patch(settings_url, headers=auth, json=payload) as resp:
                    if resp.status == 200:
                        set_ok = True
                        break
                    body = await resp.text()
                    logger.warning(
                        f'[HappSync] SET PATCH attempt {attempt + 1}: HTTP {resp.status} ({panel_url}): {body[:300]}'
                    )
            except Exception as e:
                logger.error(f'[HappSync] SET PATCH attempt {attempt + 1} ошибка ({panel_url}): {e}')
            if attempt < 2:
                await asyncio.sleep(0.5)

        if not set_ok:
            logger.error(
                f'[HappSync] SET PATCH не удался после 3 попыток ({panel_url}). '
                f'customResponseHeaders могут быть пусты — запустите повторную синхронизацию!'
            )
            return False

        ann_status = 'задан' if ann else ('очистка' if 'happAnnounce' in native_fields else 'passthrough')
        logger.info(f'[HappSync] {panel_url} — {len(new_happ)} заголовков, happAnnounce={ann_status}')

        # --- PATCH hosts (serverDescription) ---
        global_desc = cfg.get('SERVER_DESCRIPTION') if cfg.get('MODULE_ENABLED') else None
        host_descs = cfg.get_host_descriptions() if cfg.get('MODULE_ENABLED') else {}
        if global_desc is not None or host_descs:
            await asyncio.sleep(0.3)
            await _sync_hosts_description(http, auth, panel_url, global_desc, host_descs)

        # --- External Squads (multi-provider) ---
        if cfg.get_providers():
            await asyncio.sleep(0.3)
            from .squad_manager import sync_provider_squads

            await sync_provider_squads(http, auth, panel_url)

        return True


async def _sync_hosts_description(
    http: aiohttp.ClientSession,
    auth: dict,
    panel_url: str,
    global_description: str | None,
    host_descriptions: dict[str, str] | None = None,
) -> None:
    """Устанавливает serverDescription на хостах панели. Per-host описания приоритетнее глобального."""
    hosts_url = f'{panel_url}/api/hosts'
    try:
        async with http.get(hosts_url, headers=auth) as resp:
            if resp.status != 200:
                return
            data = await resp.json()
            hosts = data.get('response') or []
    except Exception as e:
        logger.error(f'[HappSync] GET hosts ошибка ({panel_url}): {e}')
        return

    per_host = host_descriptions or {}
    global_desc = (global_description or '').strip()[:30]
    updated = 0
    for host in hosts:
        host_uuid = host.get('uuid')
        if not host_uuid:
            continue
        # Per-host описание приоритетнее глобального
        if host_uuid in per_host:
            target_desc = per_host[host_uuid].strip()[:30]
        else:
            target_desc = global_desc
        current_desc = host.get('serverDescription') or ''
        if current_desc == target_desc:
            continue
        try:
            async with http.patch(
                hosts_url,
                headers=auth,
                json={'uuid': host_uuid, 'serverDescription': target_desc},
            ) as resp:
                if resp.status == 200:
                    updated += 1
                else:
                    body = await resp.text()
                    logger.warning(f'[HappSync] Host PATCH: HTTP {resp.status}: {body[:200]}')
        except Exception as e:
            logger.error(f'[HappSync] Host PATCH ошибка: {e}')
        await asyncio.sleep(0.15)
    if updated:
        logger.info(f'[HappSync] serverDescription обновлено на {updated} хостах')


async def get_all_hosts() -> list[dict]:
    """Возвращает список хостов со всех панелей: [{"uuid", "remark", "address", "port", "serverDescription", "panel_url"}]."""
    panel_urls = await _get_remnawave_api_urls()
    if not panel_urls:
        return []

    timeout = aiohttp.ClientTimeout(total=15)
    all_hosts: list[dict] = []
    seen: set[str] = set()

    for panel_url in panel_urls:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as http:
                token = await _authenticate(http, panel_url)
                if not token:
                    continue
                auth = {'Authorization': f'Bearer {token}'}
                async with http.get(f'{panel_url}/api/hosts', headers=auth) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    hosts = data.get('response') or []
                    for h in hosts:
                        uid = h.get('uuid')
                        if not uid or uid in seen:
                            continue
                        seen.add(uid)
                        all_hosts.append(
                            {
                                'uuid': uid,
                                'remark': h.get('remark', ''),
                                'address': h.get('address', ''),
                                'port': h.get('port', ''),
                                'serverDescription': h.get('serverDescription', ''),
                                'panel_url': panel_url,
                            }
                        )
        except Exception as e:
            logger.error(f'[HappSync] GET hosts ошибка ({panel_url}): {e}')

    all_hosts.sort(key=lambda h: h.get('remark', '').lower())
    return all_hosts


async def sync_to_remnawave() -> tuple[int, int]:
    """
    Синхронизирует настройки со всеми Remnawave-панелями.
    Возвращает (успешно, всего).
    """
    if not cfg.get('REMNAWAVE_SYNC_ENABLED'):
        return 0, 0

    api_urls = await _get_remnawave_api_urls()
    if not api_urls:
        logger.info('[HappSync] Remnawave-серверы не найдены в БД — синхронизация пропущена')
        return 0, 0

    logger.info(f'[HappSync] Синхронизация с {len(api_urls)} панелями: {api_urls}')

    force_clear = cfg.pop_announce_clear()
    native_fields = build_native_fields(force_clear_announce=force_clear)
    results = await asyncio.gather(
        *[_sync_one_panel(url, native_fields) for url in api_urls],
        return_exceptions=True,
    )

    success = sum(1 for r in results if r is True)
    failed = len(api_urls) - success
    if failed:
        errors = [str(r) for r in results if isinstance(r, Exception)]
        logger.warning(
            f'[HappSync] Результат: {success}/{len(api_urls)} (ошибок: {failed})' + (f' {errors}' if errors else '')
        )
    else:
        logger.info(f'[HappSync] Результат: {success}/{len(api_urls)}')

    return success, len(api_urls)


async def cleanup_remnawave_headers() -> tuple[int, int]:
    """
    Удаляет ВСЕ управляемые Happ-заголовки из customResponseHeaders на всех панелях,
    очищает happAnnounce и сбрасывает serverDescription на хостах.
    PATCH заменяет весь customResponseHeaders — отправляем только чужие заголовки.
    Возвращает (успешно, всего).
    """
    api_urls = await _get_remnawave_api_urls()
    if not api_urls:
        logger.info('[HappSync] Cleanup: Remnawave-серверы не найдены')
        return 0, 0

    logger.info(f'[HappSync] Очистка заголовков на {len(api_urls)} панелях')

    async def _clean_one(panel_url: str) -> bool:
        timeout = aiohttp.ClientTimeout(total=15)
        settings_url = f'{panel_url}/api/subscription-settings'

        async with aiohttp.ClientSession(timeout=timeout) as http:
            token = await _authenticate(http, panel_url)
            if not token:
                return False

            auth = {'Authorization': f'Bearer {token}'}

            try:
                async with http.get(settings_url, headers=auth) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()
                    settings = data.get('response') or {}
                    uuid = settings.get('uuid')
                    if not uuid:
                        return False
                    existing = settings.get('customResponseHeaders') or {}
            except Exception as e:
                logger.error(f'[HappSync] Cleanup GET ошибка ({panel_url}): {e}')
                return False

            exclude = MANAGED_HEADER_KEYS | REMNAWAVE_NATIVE_HEADER_KEYS
            foreign_only = {k: v for k, v in existing.items() if k not in exclude}
            removed = len(existing) - len(foreign_only)

            cleanup_headers = dict(foreign_only)
            for k in existing:
                if k in MANAGED_HEADER_KEYS and k not in cleanup_headers:
                    cleanup_headers[k] = ''

            payload: dict = {
                'uuid': uuid,
                'customResponseHeaders': cleanup_headers if cleanup_headers else {},
                'happAnnounce': ' ',
            }

            set_ok = False
            for attempt in range(3):
                try:
                    async with http.patch(settings_url, headers=auth, json=payload) as resp:
                        if resp.status == 200:
                            set_ok = True
                            break
                        body = await resp.text()
                        logger.warning(
                            f'[HappSync] Cleanup SET attempt {attempt + 1}: HTTP {resp.status} ({panel_url}): {body[:300]}'
                        )
                except Exception as e:
                    logger.error(f'[HappSync] Cleanup SET attempt {attempt + 1} ошибка ({panel_url}): {e}')
                if attempt < 2:
                    await asyncio.sleep(0.5)

            if not set_ok:
                logger.error(
                    f'[HappSync] Cleanup SET не удался после 3 попыток ({panel_url}). '
                    f'customResponseHeaders могут быть пусты!'
                )
                return False

            logger.info(f'[HappSync] {panel_url} — удалено {removed} заголовков, happAnnounce очищен')

            await _sync_hosts_description(http, auth, panel_url, '')

            try:
                from .squad_manager import _get_existing_squads

                squads_url = f'{panel_url}/api/external-squads'
                by_uuid, _ = await _get_existing_squads(http, auth, squads_url)
                if by_uuid:
                    for sq_uuid, sq in by_uuid.items():
                        sq_headers = sq.get('responseHeaders') or {}
                        if not any(k in MANAGED_HEADER_KEYS for k in sq_headers):
                            continue
                        clean_headers = {k: v for k, v in sq_headers.items() if k not in MANAGED_HEADER_KEYS}
                        try:
                            async with http.patch(
                                squads_url,
                                headers=auth,
                                json={'uuid': sq_uuid, 'responseHeaders': clean_headers},
                            ) as resp:
                                if resp.status == 200:
                                    name = sq.get('name', '?')
                                    logger.info(f"[HappSync] Сквад '{name}': очищены Happ-заголовки")
                        except Exception as e:
                            logger.error(f'[HappSync] Cleanup squad {sq_uuid}: {e}')
            except Exception as e:
                logger.error(f'[HappSync] Cleanup squads ошибка ({panel_url}): {e}')

            return True

    results = await asyncio.gather(
        *[_clean_one(url) for url in api_urls],
        return_exceptions=True,
    )
    success = sum(1 for r in results if r is True)
    return success, len(api_urls)


_sync_lock = asyncio.Lock()
_debounce_task: asyncio.Task | None = None


def schedule_sync() -> None:
    """
    Запускает синхронизацию в фоне с debounce.
    Каждый новый вызов отменяет предыдущий ожидающий таск и перезапускает таймер.
    Lock гарантирует, что два PATCH-цикла не наложатся друг на друга.
    """
    if not cfg.get('REMNAWAVE_SYNC_ENABLED'):
        return

    global _debounce_task

    if _debounce_task and not _debounce_task.done():
        _debounce_task.cancel()

    async def _run():
        try:
            await asyncio.sleep(2.5)
        except asyncio.CancelledError:
            return
        async with _sync_lock:
            try:
                await sync_to_remnawave()
            except Exception as e:
                logger.error(f'[HappSync] Фоновая синхронизация: {e}', exc_info=True)

    try:
        loop = asyncio.get_running_loop()
        _debounce_task = loop.create_task(_run())
    except RuntimeError:
        logger.debug('[HappSync] schedule_sync: нет активного event loop — синхронизация отложена')
