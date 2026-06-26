"""Утилита для определения клиентского приложения из строки User-Agent.

Используется при рассылках для группировки пользователей по клиенту
(Hiddify, Streisand, v2rayNG и т.д.) и формирования таргетированных сообщений.
"""


def parse_client_app(user_agent: str | None) -> str:
    """Имя клиентского приложения из userAgent (префикс до '/', '(' или пробела).
    Пусто/None → 'Unknown'."""
    if not user_agent:
        return 'Unknown'
    s = user_agent.strip()
    for sep in ('/', '(', ' '):
        i = s.find(sep)
        if i > 0:
            s = s[:i]
            break
    s = s.strip()
    return s or 'Unknown'
