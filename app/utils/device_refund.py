"""Возврат за отключаемое место устройства.

Зеркало формулы покупки из `get_device_price`: цена места пропорциональна
оставшимся дням подписки при базе в 30 дней. Держать её отдельной функцией
важно, чтобы списание и возврат не разъехались — две независимые арифметики на
одних и тех же деньгах рано или поздно дают расхождение, которое всплывает
жалобой.
"""

from __future__ import annotations

_BASE_PERIOD_DAYS = 30


def calculate_device_refund_kopeks(device_price_kopeks: object, slots: int, days_left: int) -> int:
    """Сколько вернуть за `slots` освобождённых мест при `days_left` до конца.

    Округление вниз: возврат не должен превысить уплаченное из-за копеек.
    Ноль возвращается честно — если место бесплатно или срок вышел, возвращать
    нечего, и подставлять минимальную сумму было бы выдумыванием долга.
    """
    if not isinstance(device_price_kopeks, int) or isinstance(device_price_kopeks, bool):
        return 0
    if device_price_kopeks <= 0 or slots <= 0 or days_left <= 0:
        return 0
    return device_price_kopeks * slots * days_left // _BASE_PERIOD_DAYS
