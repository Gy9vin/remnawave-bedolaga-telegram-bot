"""Утилита для определения клиентского приложения из строки User-Agent.

Используется при рассылках для группировки пользователей по клиенту
(Hiddify, Streisand, v2rayNG и т.д.) и формирования таргетированных сообщений.
"""


#: Длина колонки user_clients.app_name — имя клиента обрезаем под неё, иначе
#: «мусорный» UA без раннего разделителя роняет bulk-insert (varchar(64)).
MAX_APP_NAME_LEN = 64


def parse_client_app(user_agent: str | None) -> str:
    """Имя клиентского приложения из userAgent.

    Берёт префикс до САМОГО РАННЕГО разделителя ('/', '(', пробел) — формат
    панели `Happ/3.24.1/Android/<id>` → `Happ`. Пусто/None → 'Unknown'.
    Результат всегда ≤ MAX_APP_NAME_LEN символов (защита от UA без разделителей).
    """
    if not user_agent:
        return 'Unknown'
    s = user_agent.strip()
    cuts = [i for i in (s.find('/'), s.find('('), s.find(' ')) if i > 0]
    if cuts:
        s = s[: min(cuts)]
    s = s.strip()
    if not s:
        return 'Unknown'
    return s[:MAX_APP_NAME_LEN]
