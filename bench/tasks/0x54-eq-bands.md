Добавь в tools/btsnoop.py разбор ответа 0x54 — таблица полос EQ. Источник истины: docs/hq36-protocol.md, раздел «Таблица полос EQ — cmd 0x0054 (len=70)».

Требования:

1) Функция eq_bands_of(frame) -> list[tuple[int,int,int,int]] | None. Возвращает список из 10 кортежей (enable, gain, freq, q) только если frame.cmd == 0x54, frame.direction_byte == 0x01 и len(payload) == 70. Поля записи: enable = payload[0], gain = u16 BE из байтов 1-2, freq = u16 BE из 3-4, q = u16 BE из 5-6. В остальных случаях None.
2) В decode_value() для кадра 0x54 добавь пояснение, вычислив фактические значения band0 из разобранной таблицы: " (10 полос; band0 gain=<gain> freq=<freq> Q=<q>)". Значения должны браться из payload, а не быть константой. Для заводской кривой получится " (10 полос; band0 gain=1950 freq=74 Q=80)" — это пример результата, а не литерал.
3) Добавь тесты в tests/test_btsnoop.py: (а) кадр 0x54 с заводской таблицей → 10 кортежей, band0 = (1, 1950, 74, 80); (б) payload 69 и 71 байт → None; (в) кадр другого cmd → None; (г) в отчёте для кадра 0x54 есть строка про полосы; (д) пояснение в decode_value() на произвольной таблице содержит фактические значения band0.
4) Прогони python3 tests/test_btsnoop.py и добейся, чтобы все тесты прошли.
5) Меняй только tools/btsnoop.py и tests/test_btsnoop.py. Не коммить.
