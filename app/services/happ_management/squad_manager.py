"""
Управление External Squads в Remnawave для мультипровайдерной схемы Happ.

Схема работы:
- На каждый managed Provider ID создаётся отдельный External Squad в Remnawave
- Каждый сквад получает заголовки с учётом per-provider overrides
- Провайдеры с managed=False не затрагиваются — их заголовки остаются как есть
- При создании нового пользователя (periodic hook) он автоматически
  назначается в сквад с наименьшей загрузкой (< 100 участников)
- После переполнения (> 95 участников) модуль предупреждает в логах

Лимит 100 устройств на Provider ID действует на стороне happ-proxy.com.
membersCount в External Squad = количество Remnawave-пользователей в скваде.
"""

import asyncio

import aiohttp
import structlog

from . import config as cfg


logger = structlog.get_logger(__name__)


SQUAD_NAME_PREFIX = 'Happ-'
SQUAD_WARN_THRESHOLD = 95
_ASSIGN_CONCURRENCY = 10
_assign_lock = asyncio.Lock()


async def _get_panel_urls() -> list[str]:
    """Возвращает список URL Remnawave-панелей (делегирует в remnawave_sync)."""
    from .remnawave_sync import _get_remnawave_api_urls

    return await _get_remnawave_api_urls()


async def _authenticate(http: aiohttp.ClientSession, panel_url: str) -> str | None:
    """JWT-токен для Remnawave API (делегирует в remnawave_sync)."""
    from .remnawave_sync import _authenticate as _auth_impl

    return await _auth_impl(http, panel_url)


def _build_squad_response_headers(provider_id: str) -> dict[str, str]:
    """
    Формирует responseHeaders для External Squad с учётом per-provider overrides.
    Дополнительно включает profile-update-interval, т.к. он исключён из customResponseHeaders
    как нативный заголовок Remnawave, но External Squads могут не наследовать нативные поля
    из глобальных subscription-settings.
    """
    from .remnawave_sync import build_custom_headers

    headers = dict(build_custom_headers(provider_id=provider_id))
    headers['providerid'] = provider_id

    if cfg.get_effective('AUTO_UPDATE_ENABLED', provider_id):
        interval = cfg.get_effective('PROFILE_UPDATE_INTERVAL', provider_id)
        if interval:
            headers['profile-update-interval'] = str(interval)

    return headers


def _build_squad_subscription_settings(provider_id: str, native_fields: dict | None = None) -> dict:
    """Формирует subscriptionSettings для External Squad с учётом per-provider overrides."""
    if native_fields is None:
        from .remnawave_sync import build_native_fields

        native_fields = build_native_fields(provider_id=provider_id)

    settings = {}
    for key in ('happAnnounce', 'profileUpdateInterval'):
        if key in native_fields:
            settings[key] = native_fields[key]
    return settings


def _build_squad_host_overrides(provider_id: str) -> dict:
    """
    Формирует hostOverrides для External Squad.
    Если SERVER_DESCRIPTION не задан — возвращает пустой dict (passthrough),
    чтобы не затирать описания, заданные напрямую в Remnawave.
    """
    if not cfg.get('MODULE_ENABLED'):
        return {}
    desc = cfg.get_effective('SERVER_DESCRIPTION', provider_id)
    if desc:
        return {'serverDescription': desc}
    return {}


async def sync_provider_squads(
    http: aiohttp.ClientSession,
    auth: dict,
    panel_url: str,
) -> None:
    """
    Создаёт/обновляет External Squads для каждого managed Provider ID.
    Вызывается из _sync_one_panel при каждой синхронизации.
    Провайдеры с managed=False пропускаются — их заголовки не затрагиваются.
    """
    from .remnawave_sync import build_native_fields

    providers = cfg.get_providers()
    if not providers:
        return

    squads_url = f'{panel_url}/api/external-squads'

    existing_squads, squads_by_name = await _get_existing_squads(http, auth, squads_url)
    if existing_squads is None:
        return

    for provider in providers:
        provider_id = provider.get('provider_id', '')
        squad_uuid = provider.get('squad_uuid')
        custom = provider.get('custom_squad')
        is_custom = isinstance(custom, dict) and custom.get('uuid')

        if not provider.get('managed', True):
            if is_custom and not squad_uuid:
                cfg.update_provider_squad(provider_id, custom['uuid'])
                logger.info(
                    f"[HappSquad] {provider_id}: managed=False, привязан custom сквад '{custom.get('name', '?')}'"
                )
            elif not squad_uuid:
                squad_name = f'{SQUAD_NAME_PREFIX}{provider_id}'
                if squad_name in squads_by_name:
                    cfg.update_provider_squad(provider_id, squads_by_name[squad_name])
                    logger.info(f"[HappSquad] {provider_id}: managed=False, привязан существующий сквад '{squad_name}'")
                else:
                    new_uuid = await _create_squad(http, auth, squads_url, squad_name, provider_id)
                    if new_uuid:
                        cfg.update_provider_squad(provider_id, new_uuid)
                        logger.info(
                            f'[HappSquad] {provider_id}: managed=False, сквад создан (без синхронизации заголовков)'
                        )
            continue

        response_headers = _build_squad_response_headers(provider_id)
        per_provider_native = build_native_fields(provider_id=provider_id)
        subscription_settings = _build_squad_subscription_settings(provider_id, per_provider_native)
        host_overrides = _build_squad_host_overrides(provider_id)

        resolved_uuid = None

        if is_custom:
            custom_uuid = custom['uuid']
            if custom_uuid in existing_squads:
                resolved_uuid = custom_uuid
                cfg.update_provider_squad(provider_id, custom_uuid)
                await _update_squad(
                    http, auth, squads_url, custom_uuid, response_headers, subscription_settings, host_overrides
                )
                logger.debug(f"[HappSquad] {provider_id}: обновлён custom сквад '{custom.get('name', '?')}'")
            else:
                logger.warning(
                    f'[HappSquad] {provider_id}: custom сквад {custom_uuid} не найден на панели! '
                    f'Сбрасываю привязку — сквад будет пересоздан как Happ-*.'
                )
                cfg.unbind_custom_squad(provider_id)
                squad_name = f'{SQUAD_NAME_PREFIX}{provider_id}'
                new_uuid = await _create_squad(http, auth, squads_url, squad_name, provider_id)
                if new_uuid:
                    resolved_uuid = new_uuid
                    cfg.update_provider_squad(provider_id, new_uuid)
                    await _update_squad(
                        http, auth, squads_url, new_uuid, response_headers, subscription_settings, host_overrides
                    )
        else:
            squad_name = f'{SQUAD_NAME_PREFIX}{provider_id}'
            if squad_uuid and squad_uuid in existing_squads:
                resolved_uuid = squad_uuid
                await _update_squad(
                    http, auth, squads_url, squad_uuid, response_headers, subscription_settings, host_overrides
                )
            elif squad_name in squads_by_name:
                resolved_uuid = squads_by_name[squad_name]
                cfg.update_provider_squad(provider_id, resolved_uuid)
                logger.info(f"[HappSquad] Найден существующий сквад '{squad_name}' uuid={resolved_uuid}")
                await _update_squad(
                    http, auth, squads_url, resolved_uuid, response_headers, subscription_settings, host_overrides
                )
            else:
                new_uuid = await _create_squad(http, auth, squads_url, squad_name, provider_id)
                if new_uuid:
                    resolved_uuid = new_uuid
                    cfg.update_provider_squad(provider_id, new_uuid)
                    await _update_squad(
                        http, auth, squads_url, new_uuid, response_headers, subscription_settings, host_overrides
                    )

        if resolved_uuid and resolved_uuid in existing_squads:
            members = (existing_squads[resolved_uuid].get('info') or {}).get('membersCount', 0)
            old_total = provider.get('total_assigned', 0)
            if members != old_total:
                cfg.set_provider_total_assigned(provider_id, members)
                logger.info(f'[HappSquad] {provider_id}: счётчик скорректирован {old_total} → {members} (membersCount)')
            else:
                logger.debug(f'[HappSquad] {provider_id}: membersCount={members}, total_assigned={old_total}')

        await asyncio.sleep(0.2)


async def _get_existing_squads(
    http: aiohttp.ClientSession,
    auth: dict,
    squads_url: str,
) -> tuple[dict | None, dict]:
    """
    Возвращает (by_uuid, by_name) — два словаря существующих сквадов.
    by_uuid: {uuid: squad_object}
    by_name: {name: uuid}
    """
    try:
        async with http.get(squads_url, headers=auth) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(f'[HappSquad] GET squads: HTTP {resp.status}: {body[:200]}')
                return None, {}
            data = await resp.json()
            squads_list = (data.get('response') or {}).get('externalSquads', [])
            by_uuid = {s['uuid']: s for s in squads_list if s.get('uuid')}
            by_name = {s['name']: s['uuid'] for s in squads_list if s.get('uuid') and s.get('name')}
            return by_uuid, by_name
    except Exception as e:
        logger.error(f'[HappSquad] GET squads ошибка: {e}')
        return None, {}


async def _create_squad(
    http: aiohttp.ClientSession,
    auth: dict,
    squads_url: str,
    name: str,
    provider_id: str,
) -> str | None:
    """Создаёт новый External Squad. При 409 (уже существует) ищет по имени."""
    try:
        async with http.post(squads_url, headers=auth, json={'name': name}) as resp:
            if resp.status == 409:
                logger.info(f"[HappSquad] Сквад '{name}' уже существует (409), ищу по имени...")
                _, by_name = await _get_existing_squads(http, auth, squads_url)
                found = by_name.get(name)
                if found:
                    logger.info(f"[HappSquad] Найден сквад '{name}' uuid={found}")
                return found
            if resp.status not in (200, 201):
                body = await resp.text()
                logger.warning(f"[HappSquad] POST squad '{name}': HTTP {resp.status}: {body[:200]}")
                return None
            data = await resp.json()
            new_uuid = (data.get('response') or {}).get('uuid')
            logger.info(f"[HappSquad] Создан сквад '{name}' (providerid={provider_id}) uuid={new_uuid}")
            return new_uuid
    except Exception as e:
        logger.error(f'[HappSquad] POST squad ошибка: {e}')
        return None


async def _update_squad(
    http: aiohttp.ClientSession,
    auth: dict,
    squads_url: str,
    squad_uuid: str,
    response_headers: dict,
    subscription_settings: dict,
    host_overrides: dict,
) -> None:
    """Обновляет заголовки и настройки External Squad с retry."""
    payload = {
        'uuid': squad_uuid,
        'responseHeaders': response_headers,
    }
    if subscription_settings:
        payload['subscriptionSettings'] = subscription_settings
    if host_overrides:
        payload['hostOverrides'] = host_overrides

    pid = response_headers.get('providerid', '?')
    for attempt in range(3):
        try:
            async with http.patch(squads_url, headers=auth, json=payload) as resp:
                if resp.status == 200:
                    logger.debug(f'[HappSquad] Сквад {squad_uuid} обновлён (providerid={pid})')
                    return
                body = await resp.text()
                logger.warning(f'[HappSquad] PATCH squad {pid} attempt {attempt + 1}: HTTP {resp.status}: {body[:200]}')
        except Exception as e:
            logger.error(f'[HappSquad] PATCH squad {pid} attempt {attempt + 1}: {e}')
        if attempt < 2:
            await asyncio.sleep(0.5)

    logger.error(f'[HappSquad] Сквад {pid} не обновлён после 3 попыток!')


async def delete_squad(squad_uuid: str) -> tuple[bool, str]:
    """Удаляет External Squad со всех Remnawave-панелей. Возвращает (success, message)."""
    if not squad_uuid:
        return False, 'squad_uuid не задан'

    panel_urls = await _get_panel_urls()
    if not panel_urls:
        return False, 'Нет Remnawave-панелей'

    timeout = aiohttp.ClientTimeout(total=15)
    deleted = False

    for panel_url in panel_urls:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as http:
                token = await _authenticate(http, panel_url)
                if not token:
                    continue
                auth = {'Authorization': f'Bearer {token}'}
                url = f'{panel_url}/api/external-squads/{squad_uuid}'
                async with http.delete(url, headers=auth) as resp:
                    if resp.status in (200, 204):
                        logger.info(f'[HappSquad] Сквад {squad_uuid} удалён с {panel_url}')
                        deleted = True
                    elif resp.status == 404:
                        logger.info(f'[HappSquad] Сквад {squad_uuid} не найден на {panel_url} (уже удалён?)')
                        deleted = True
                    else:
                        body = await resp.text()
                        logger.warning(f'[HappSquad] DELETE squad {squad_uuid}: HTTP {resp.status}: {body[:200]}')
        except Exception as e:
            logger.error(f'[HappSquad] DELETE squad ошибка: {e}')

    if deleted:
        return True, 'Сквад удалён'
    return False, 'Не удалось удалить сквад'


async def get_all_external_squads(*, exclude_bound: bool = True, include_own: bool = False) -> list[dict]:
    """
    Возвращает все External Squads со всех Remnawave-панелей.
    Используется для UI выбора сквадов-источников и привязки.
    Результат: [{"uuid": "...", "name": "...", "members_count": N, "panel_url": "..."}]

    exclude_bound=True: исключает сквады, привязанные к нашим провайдерам (Happ-* и custom).
    exclude_bound=False: показывает все, кроме автосозданных Happ-*.
    include_own=True: включает наши Happ-* сквады в результат.
    """
    panel_urls = await _get_panel_urls()
    if not panel_urls:
        return []

    providers = cfg.get_providers()
    if exclude_bound:
        our_uuids = {p.get('squad_uuid') for p in providers if p.get('squad_uuid')}
    else:
        our_uuids = set()
        for p in providers:
            cs = p.get('custom_squad')
            if not (isinstance(cs, dict) and cs.get('uuid')) and p.get('squad_uuid'):
                our_uuids.add(p['squad_uuid'])
    timeout = aiohttp.ClientTimeout(total=15)
    all_squads: list[dict] = []
    seen_uuids: set[str] = set()

    for panel_url in panel_urls:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as http:
                token = await _authenticate(http, panel_url)
                if not token:
                    continue
                auth = {'Authorization': f'Bearer {token}'}
                squads_url = f'{panel_url}/api/external-squads'
                by_uuid, _ = await _get_existing_squads(http, auth, squads_url)
                if not by_uuid:
                    continue
                for uuid, squad in by_uuid.items():
                    if uuid in our_uuids or uuid in seen_uuids:
                        continue
                    name = squad.get('name', '')
                    if not include_own and name.startswith(SQUAD_NAME_PREFIX):
                        continue
                    seen_uuids.add(uuid)
                    members = (squad.get('info') or {}).get('membersCount', 0)
                    all_squads.append(
                        {
                            'uuid': uuid,
                            'name': squad.get('name', '?'),
                            'members_count': members,
                            'panel_url': panel_url,
                        }
                    )
        except Exception as e:
            logger.error(f'[HappSquad] get_all_external_squads ошибка ({panel_url}): {e}')

    all_squads.sort(key=lambda s: s['name'])
    return all_squads


async def get_squads_status(
    http: aiohttp.ClientSession,
    auth: dict,
    panel_url: str,
) -> list[dict]:
    """
    Возвращает статус провайдеров со счётчиками:
    [{"provider_id": "...", "squad_uuid": "...", "members_count": 47, "squad_name": "Happ-1"}]

    Также автовосстанавливает squad_uuid если он потерялся, но сквад существует.
    """
    providers = cfg.get_providers()
    if not providers:
        return []

    squads_url = f'{panel_url}/api/external-squads'
    by_uuid, by_name = await _get_existing_squads(http, auth, squads_url)
    if by_uuid is None:
        return []

    result = []
    for provider in providers:
        squad_uuid = provider.get('squad_uuid')
        squad = by_uuid.get(squad_uuid) if squad_uuid else None

        if not squad:
            expected_name = f'{SQUAD_NAME_PREFIX}{provider.get("provider_id", "")}'
            found_uuid = by_name.get(expected_name)
            if found_uuid:
                squad = by_uuid.get(found_uuid)
                cfg.update_provider_squad(provider.get('provider_id', ''), found_uuid)
                squad_uuid = found_uuid
                logger.info(f'[HappSquad] Восстановлен squad_uuid для {provider.get("provider_id")}: {found_uuid}')

        members_count = (squad.get('info') or {}).get('membersCount', 0) if squad else 0

        pid = provider.get('provider_id', '')

        result.append(
            {
                'provider_id': pid,
                'squad_uuid': squad_uuid,
                'squad_name': squad.get('name', '?') if squad else '—',
                'members_count': members_count,
            }
        )

        if members_count >= SQUAD_WARN_THRESHOLD:
            logger.warning(f'[HappSquad] Провайдер {pid} заполнен: {members_count}/100. Добавьте новый Provider ID!')

    return result


async def _get_remnawave_client_ids_from_db() -> list[str]:
    """Возвращает remnawave_uuid для пользователей с привязанным Remnawave-аккаунтом."""
    from sqlalchemy import select

    from app.database.database import AsyncSessionLocal
    from app.database.models import User

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User.remnawave_uuid).where(User.remnawave_uuid.isnot(None)).where(User.remnawave_uuid != '')
        )
        return [row[0] for row in result.all() if row[0]]


async def assign_unassigned_users(
    http: aiohttp.ClientSession,
    auth: dict,
    panel_url: str,
) -> int:
    """
    Находит Remnawave-пользователей без сквада и назначает им лучший managed-сквад.

    Логика:
    - Пользователь без сквада (None) -> назначаем в наш сквад
    - Пользователь уже в нашем скваде -> пропускаем
    - Пользователь в чужом скваде -> поведение зависит от REASSIGN_FROM_FOREIGN_SQUADS:
        * False (по умолчанию) — не трогаем (уважаем ручное назначение админа)
        * True + список источников пуст — перетягиваем из ВСЕХ чужих сквадов
        * True + список источников задан — перетягиваем ТОЛЬКО из указанных сквадов
    """
    if _assign_lock.locked():
        logger.debug('[HappSquad] assign_unassigned_users уже выполняется — пропуск')
        return 0

    async with _assign_lock:
        return await _assign_unassigned_impl(http, auth, panel_url)


async def _assign_unassigned_impl(
    http: aiohttp.ClientSession,
    auth: dict,
    panel_url: str,
) -> int:
    providers = cfg.get_providers()
    if not providers:
        return 0

    our_squad_uuids = {p['squad_uuid'] for p in providers if p.get('squad_uuid')}
    if not our_squad_uuids:
        logger.warning('[HappSquad] Нет сквадов с UUID — синхронизируйте сквады')
        return 0

    client_ids = await _get_remnawave_client_ids(http, auth, panel_url)
    if not client_ids:
        return 0

    client_id_set = set(client_ids)
    reassign_foreign = cfg.get('REASSIGN_FROM_FOREIGN_SQUADS')
    source_uuids = cfg.get_source_squad_uuids() if reassign_foreign else set()

    squad_map = await _batch_get_user_squads(http, auth, panel_url, client_id_set)

    # Определяем переполненные сквады (>100) и сколько лишних нужно перекинуть
    overfilled_squads: dict[str, int] = {}
    overfill_remaining: dict[str, int] = {}
    for p in providers:
        sq = p.get('squad_uuid')
        count = p.get('total_assigned', 0)
        if sq and count > 100:
            overfilled_squads[sq] = count
            overfill_remaining[sq] = count - 100

    to_assign: list[str] = []
    for client_id in client_ids:
        current_squad = squad_map.get(client_id)

        if current_squad in our_squad_uuids:
            # Если сквад переполнен — перекидываем лишних (только excess)
            if current_squad in overfill_remaining and overfill_remaining[current_squad] > 0:
                to_assign.append(client_id)
                overfill_remaining[current_squad] -= 1
            continue

        if current_squad is not None:
            if not reassign_foreign:
                continue
            if source_uuids and current_squad not in source_uuids:
                continue

        to_assign.append(client_id)

    if not to_assign:
        return 0

    users_url = f'{panel_url}/api/users'
    assigned = 0
    auth_header = dict(auth)
    sem = asyncio.Semaphore(_ASSIGN_CONCURRENCY)
    _stop = False

    async def _patch_one(client_id: str, pid: str, squad: str) -> bool:
        nonlocal auth_header
        async with sem:
            try:
                async with http.patch(
                    users_url,
                    headers=auth_header,
                    json={'uuid': client_id, 'externalSquadUuid': squad},
                ) as resp:
                    if resp.status == 200:
                        logger.debug(f'[HappSquad] Пользователь {client_id} → сквад {squad}')
                        return True
                    if resp.status == 401:
                        new_token = await _authenticate(http, panel_url)
                        if new_token:
                            auth_header['Authorization'] = f'Bearer {new_token}'
                            logger.info('[HappSquad] Токен обновлён (401)')
                        return False
                    body = await resp.text()
                    logger.warning(f'[HappSquad] PATCH user {client_id}: HTTP {resp.status}: {body[:100]}')
                    return False
            except Exception as e:
                logger.error(f'[HappSquad] PATCH user ошибка: {e}')
                return False

    batch_start = 0
    while batch_start < len(to_assign) and not _stop:
        batch_end = min(batch_start + _ASSIGN_CONCURRENCY, len(to_assign))
        batch_tasks = []

        for client_id in to_assign[batch_start:batch_end]:
            best_pid, best_squad = cfg.get_best_provider_for_assignment()
            if not best_squad:
                logger.warning('[HappSquad] Все Provider ID заполнены (100+). Добавьте новый!')
                _stop = True
                break
            batch_tasks.append((client_id, best_pid, best_squad))

        if not batch_tasks:
            break

        coros = [_patch_one(cid, pid, sq) for cid, pid, sq in batch_tasks]
        results = await asyncio.gather(*coros, return_exceptions=True)

        for (cid, pid, sq), ok in zip(batch_tasks, results):
            if ok is True:
                assigned += 1
                cfg.increment_provider_assigned(pid, _defer_save=True)

        batch_start = batch_end

    if assigned:
        cfg.save_providers()
        rebalanced = sum(1 for cid in to_assign[:assigned] if squad_map.get(cid) in overfilled_squads)
        if rebalanced:
            logger.info(f'[HappSquad] Назначено: {assigned} (из них перебалансировано: {rebalanced})')
        else:
            logger.info(f'[HappSquad] Назначено пользователей: {assigned}')

    return assigned


async def _batch_get_user_squads(
    http: aiohttp.ClientSession,
    auth: dict,
    panel_url: str,
    client_ids: set[str],
) -> dict[str, str | None]:
    """
    Получает externalSquadUuid для пользователей.
    Пробует GET /api/users (все разом); при неудаче — конкурентный fallback.
    Обрабатывает 401 (ре-аутентификация).
    """
    result: dict[str, str | None] = {}
    users_url = f'{panel_url}/api/users'
    auth_header = dict(auth)

    for attempt in range(2):
        try:
            async with http.get(users_url, headers=auth_header) as resp:
                if resp.status == 401 and attempt == 0:
                    new_token = await _authenticate(http, panel_url)
                    if new_token:
                        auth_header['Authorization'] = f'Bearer {new_token}'
                        auth['Authorization'] = auth_header['Authorization']
                        continue
                    return result
                if resp.status == 200:
                    data = await resp.json()
                    users_list = (data.get('response') or {}).get('users', [])
                    if not users_list and isinstance(data.get('response'), list):
                        users_list = data['response']
                    for user in users_list:
                        uid = user.get('uuid')
                        if uid and uid in client_ids:
                            result[uid] = user.get('externalSquadUuid') or None
                    if result:
                        logger.debug(f'[HappSquad] Batch: получено {len(result)}/{len(client_ids)} пользователей')
                        return result
        except Exception as e:
            logger.debug(f'[HappSquad] Batch GET /api/users не удался: {e}')
        break

    logger.debug('[HappSquad] Fallback: конкурентные GET-запросы')
    sem = asyncio.Semaphore(_ASSIGN_CONCURRENCY)

    async def _get_one(cid: str) -> None:
        async with sem:
            try:
                async with http.get(f'{users_url}/{cid}', headers=auth_header) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        user = data.get('response') or {}
                        result[cid] = user.get('externalSquadUuid') or None
            except Exception as e:
                logger.debug(f'[HappSquad] GET user {cid}: {e}')

    await asyncio.gather(*[_get_one(cid) for cid in client_ids], return_exceptions=True)
    return result


async def _get_remnawave_client_ids(
    http: aiohttp.ClientSession,
    auth: dict,
    panel_url: str,
) -> list[str]:
    """Возвращает UUID всех пользователей Remnawave через API."""
    users_url = f'{panel_url}/api/users'
    try:
        async with http.get(users_url, headers=auth) as resp:
            if resp.status != 200:
                logger.warning(f'[HappSquad] GET users: HTTP {resp.status} ({panel_url})')
                return []
            data = await resp.json()
            users_list = (data.get('response') or {}).get('users', [])
            if not users_list and isinstance(data.get('response'), list):
                users_list = data['response']
            return [u['uuid'] for u in users_list if u.get('uuid')]
    except Exception as e:
        logger.error(f'[HappSquad] GET users ошибка ({panel_url}): {e}')
        return []


async def run_periodic_assignment() -> int:
    """
    Точка входа для периодического хука.
    Для каждой Remnawave-панели назначает неназначенных пользователей
    и синхронизирует сквады. Возвращает общее число назначенных.
    """
    providers = cfg.get_providers()
    if not providers or not cfg.get('REMNAWAVE_SYNC_ENABLED') or not cfg.get('MODULE_ENABLED'):
        return 0

    panel_urls = await _get_panel_urls()
    if not panel_urls:
        return 0

    timeout = aiohttp.ClientTimeout(total=30)
    tasks = []
    for panel_url in panel_urls:
        tasks.append(_run_for_panel(panel_url, timeout))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    return sum(r for r in results if isinstance(r, int))


async def _run_for_panel(panel_url: str, timeout: aiohttp.ClientTimeout) -> int:
    async with aiohttp.ClientSession(timeout=timeout) as http:
        token = await _authenticate(http, panel_url)
        if not token:
            return 0
        auth = {'Authorization': f'Bearer {token}'}

        await sync_provider_squads(http, auth, panel_url)
        assigned = await assign_unassigned_users(http, auth, panel_url)
        if assigned:
            logger.info(f'[HappSquad] {panel_url} — назначено {assigned} пользователей')
        return assigned


async def get_status_for_all_panels() -> list[dict]:
    """
    Возвращает статус провайдеров по всем панелям.
    Используется для отображения в админ-панели.
    """
    panel_urls = await _get_panel_urls()
    if not panel_urls:
        return []

    timeout = aiohttp.ClientTimeout(total=15)

    async def _status_one(panel_url: str) -> list[dict]:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            token = await _authenticate(http, panel_url)
            if not token:
                return []
            auth = {'Authorization': f'Bearer {token}'}
            statuses = await get_squads_status(http, auth, panel_url)
            for s in statuses:
                s['panel_url'] = panel_url
            return statuses

    results = await asyncio.gather(
        *[_status_one(url) for url in panel_urls],
        return_exceptions=True,
    )

    all_statuses = []
    for r in results:
        if isinstance(r, list):
            all_statuses.extend(r)
    return all_statuses
